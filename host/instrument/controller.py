"""
SettingsController -- keeps settings.json, the GUI controls and the board
in step, and is the only way anything (a person, the file, the assistant)
changes them.

    person moves a control  ─┐
    someone edits the file  ─┼─> validate (spec limits) ─> commit ─> save
    assistant set_settings  ─┘            │                      │
                                          │                      ├─> display: widgets now
                                          │                      └─> board: R/A/G/P, one at
                                          └─> errors back to the        a time, each waiting
                                              caller, nothing applied   for '# ACK' / '# ERR'

Board settings are *requested* in the store; what the board actually runs
comes back in its '# CONFIG' line and is tracked as `actual`.
"""

import copy
import time

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Qt, Signal, Slot

from . import spec
from .settings import (BOARD_KEYS, DISPLAY_KEYS, SCHEMA, VIEWS, Context,
                       SettingsStore, describe_options)

# Order matters: averaging and range first (they change what rates are
# possible), rate after, streaming last.
_BOARD_ORDER = ("averaging", "range_mT", "sample_rate_hz", "streaming")


class SettingsController(QObject):

    changed = Signal(dict)            # {key: (old, new)} after any commit
    board_reply = Signal(str, str, str)   # command, kind(ack/err/none), text
    message = Signal(str)             # for the board log / status

    def __init__(self, win, path=None):
        super().__init__(win)
        self.win = win
        self.store = SettingsStore(path) if path else SettingsStore()
        self.loop = spec.LoopModel()
        self.actual = {}               # board keys, from '# CONFIG'
        self._applying = False
        self._queue = []               # board commands waiting to go out
        self._waiting = None           # (command, deadline, log_mark)
        self._pending_push = False     # board changes made while offline
        self._reader_seen = None
        self._last_r = None
        self._r_since = 0.0

        self.store.load(self.context())
        if self.store.load_error:
            self.message.emit(f"# settings.json: {self.store.load_error}")
        self._apply_display(self.store.values)
        self._update_rate_limit()
        self._hook_widgets()
        self.store.save()

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self.store.save)

        # Atomic saves replace the file, which drops a watch on the file
        # itself; watching the directory as well catches every rewrite.
        self.watcher = QFileSystemWatcher(self)
        self.watcher.addPath(str(self.store.path.parent))
        if self.store.path.exists():
            self.watcher.addPath(str(self.store.path))
        self.watcher.fileChanged.connect(self._file_event)
        self.watcher.directoryChanged.connect(self._file_event)

        self._tick = QTimer(self)
        self._tick.timeout.connect(self._poll)
        self._tick.start(200)

    # ================================================================ context

    def context(self):
        win = self.win
        reader = getattr(win.acq, "reader", None)
        cfg = getattr(reader, "config", None)
        if reader is not None:
            self.loop.line_bytes = reader.mean_line_bytes
        absmax = {}
        try:
            _, cols = win.acq.window(5.0)
            for ax in ("bx", "by", "bz"):
                v = cols.get(ax)
                if v is not None and len(v):
                    raw = v + win.acq.tare.get(ax, 0.0)
                    absmax[ax] = float(abs(raw).max())
        except Exception:                            # noqa: BLE001
            pass
        return Context(loop=self.loop,
                       rtd_hz=(cfg.rtd_hz if cfg and cfg.rtd_hz else 60),
                       field_abs_max_mT=absmax,
                       connected=reader is not None)

    # =============================================================== requests

    def request(self, changes, source, snap=None):
        """The one entry point. source: 'gui', 'file', 'assistant', 'undo'.
        Validates everything first; applies nothing unless all of it is
        possible. Returns the validation Result (as a dict) plus `applied`
        and `queued_for_board`."""
        if snap is None:
            snap = source in ("gui", "file")
        res = self.store.validate(changes, self.context(), snap=snap)
        out = res.as_dict()
        if not res.ok:
            return out
        diff = self.store.commit(res.changes)
        out["applied"] = {k: v for k, (_o, v) in diff.items()}
        display = {k: v for k, v in res.changes.items() if k in DISPLAY_KEYS}
        board = {k: v for k, v in diff.items() if k in BOARD_KEYS}
        if source != "gui":
            self._apply_display(display)
        if board:
            out["board"] = self._push_board({k: v for k, (_o, v)
                                             in board.items()})
        if "averaging" in res.changes:
            self._update_rate_limit()
        if diff:
            self._save_timer.start(300)
            self.changed.emit(diff)
        return out

    # ================================================================== board

    def _push_board(self, board):
        if self.win.acq.reader is None:
            self._pending_push = True
            return "board not connected -- saved, sent when it connects"
        for key in _BOARD_ORDER:
            if key in board:
                self._queue.append(self._command_for(key, board[key]))
        self._pump_queue()
        return "sending " + ", ".join(self._queue_preview())

    def _queue_preview(self):
        cmds = list(self._queue)
        if self._waiting:
            cmds.insert(0, self._waiting[0])
        return cmds

    def _command_for(self, key, value):
        if key == "averaging":
            return f"A {value}"
        if key == "range_mT":
            return f"G {value}"
        if key == "streaming":
            return f"P {1 if value else 0}"
        r = self.loop.r_for_rate(value) or spec.R_MAX
        return f"R {r}"

    def _pump_queue(self):
        """One command at a time: the Uartlite RX FIFO is 16 bytes and the
        firmware only drains it between samples, so a burst of commands
        during a long sleep would overflow it."""
        if self._waiting or not self._queue:
            return
        cmd = self._queue.pop(0)
        mark = self._log_lines()
        self.win._send_command(cmd)
        period = self._period_s()
        deadline = time.monotonic() + max(2.0, 3 * period + 0.5)
        self._waiting = (cmd, deadline, mark)

    def _period_s(self):
        cfg = getattr(self.win.acq.reader, "config", None)
        if cfg and cfg.period_us:
            return cfg.period_us / 1e6
        return 0.5

    def _log_lines(self):
        return self.win.log.blockCount()

    def _check_reply(self):
        cmd, deadline, mark = self._waiting
        lines = self.win.log.toPlainText().splitlines()[max(0, mark - 1):]
        sent = [i for i, l in enumerate(lines) if l.strip() == f"> {cmd}"]
        after = lines[sent[-1] + 1:] if sent else []
        verdict = None
        for l in after:
            if l.startswith("# ERR"):
                verdict = ("err", l[2:].strip())
                break
            if "ACK" in l and cmd in l:
                verdict = ("ack", "acknowledged")
                break
        if verdict is None and time.monotonic() > deadline:
            verdict = ("none", "no reply from the board")
        if verdict is None:
            return
        self._waiting = None
        kind, text = verdict
        self.board_reply.emit(cmd, kind, text)
        if kind != "ack":
            self.message.emit(f"# {cmd}: {text}")
        self._pump_queue()

    def board_busy(self):
        return bool(self._waiting or self._queue)

    # ================================================================ polling

    def _poll(self):
        if self._waiting:
            self._check_reply()
        reader = self.win.acq.reader
        if reader is None:
            self._reader_seen = None
            return
        cfg = reader.config
        if cfg is None:
            return
        r = round(1e6 / cfg.period_us) if cfg.period_us else None
        self.actual = {
            "averaging": cfg.conv_avg, "range_mT": cfg.range_mt,
            "sample_rate_hz": (round(self.loop.rate_for_r(r), 1)
                               if r else None),
            "r_command": r,
        }
        # Learn the real loop overhead once R has been steady a while.
        if r != self._last_r:
            self._last_r, self._r_since = r, time.monotonic()
        elif time.monotonic() - self._r_since > 4.0:
            self.loop.calibrate(self.win.acq.measured_rate(), r)

        if self._reader_seen is not reader:          # first CONFIG after connect
            self._reader_seen = reader
            if self._pending_push:
                self._pending_push = False
                self._push_board({k: self.store.values[k]
                                  for k in _BOARD_ORDER})
                self.message.emit("# settings: sent the requested board "
                                  "settings saved while offline")
            else:
                adopt = {k: v for k, v in self.actual.items()
                         if k in BOARD_KEYS and v is not None}
                res = self.store.validate(adopt, self.context())
                self.store.commit(res.changes)
                self._update_rate_limit()
                self._sync_board_widgets()
                self._save_timer.start(300)

    def mismatch(self):
        """Board keys where what the board runs differs from the request."""
        out = {}
        for k in ("averaging", "range_mT"):
            a = self.actual.get(k)
            if a is not None and a != self.store.values[k]:
                out[k] = {"requested": self.store.values[k], "actual": a}
        want, got = self.store.values["sample_rate_hz"], \
            self.actual.get("sample_rate_hz")
        if got and abs(got - want) > max(0.5, 0.05 * want):
            out["sample_rate_hz"] = {"requested": want, "actual": got}
        return out

    # ================================================================ widgets

    def _hook_widgets(self):
        w = self.win
        w.display.changed.connect(lambda: self._from_widgets("display"))
        w.channels.changed.connect(lambda: self._from_widgets("channels"))
        w.tabs.currentChanged.connect(lambda *_: self._from_widgets("view"))
        w.spectrum.segments_box.currentIndexChanged.connect(
            lambda *_: self._from_widgets("segments"))
        w.theme_action.toggled.connect(lambda *_: self._from_widgets("theme"))
        # Board controls: route through validation instead of straight to
        # the serial port. The rate box now means *delivered* Hz.
        try:
            w.acquisition.command.disconnect()
        except (RuntimeError, TypeError):
            pass
        w.acquisition.command.connect(self._from_panel_command)
        w.acquisition.rate_box.setToolTip(
            "Delivered sample rate. The maximum follows the averaging "
            "setting, the 115200-baud UART and the firmware loop.")

    def _widget_values(self):
        w = self.win
        return {
            "window_s": round(w.display.window_box.value(), 1),
            "fps": w.display.fps_box.value(),
            "smoothing": w.display.smooth_box.value(),
            "autoscale": w.display.autoscale_box.isChecked(),
            "visible_channels": sorted(w.channels.visible(),
                                       key=list(SCHEMA["visible_channels"]
                                                ["choices"]).index),
            "view": VIEWS[w.tabs.currentIndex()],
            "spectrum_segments": w.spectrum.segments_box.currentData(),
            "theme": "light" if w.theme_action.isChecked() else "dark",
        }

    def _from_widgets(self, _what):
        if self._applying:
            return
        now = self._widget_values()
        changes = {k: v for k, v in now.items() if self.store.values[k] != v}
        if not changes:
            return
        res = self.request(changes, "gui")
        if not res["ok"]:
            # Put the control back and say why, rather than keep a value
            # the instrument cannot honour.
            self._apply_display(self.store.values)
            self.message.emit("# settings: " + "; ".join(
                f"{k}: {v}" for k, v in res["errors"].items()))

    def _from_panel_command(self, text):
        head, _, arg = text.partition(" ")
        key = {"R": "sample_rate_hz", "A": "averaging", "G": "range_mT",
               "P": "streaming"}.get(head)
        if key is None:
            self.win._send_command(text)
            return
        value = {"P": lambda a: a == "1"}.get(head, lambda a: a)(arg)
        res = self.request({key: value}, "gui")
        if not res["ok"]:
            self.message.emit("# settings: " + "; ".join(
                f"{k}: {v}" for k, v in res["errors"].items()))
            self._sync_board_widgets()

    def _apply_display(self, values):
        w = self.win
        self._applying = True
        try:
            for key, v in values.items():
                if key == "window_s":
                    w.display.window_box.setValue(float(v))
                elif key == "fps":
                    w.display.fps_box.setValue(int(v))
                elif key == "smoothing":
                    w.display.smooth_box.setValue(int(v))
                elif key == "autoscale":
                    w.display.autoscale_box.setChecked(bool(v))
                elif key == "visible_channels":
                    for ch, box in w.channels.boxes.items():
                        box.setChecked(ch in v)
                elif key == "view":
                    w.tabs.setCurrentIndex(VIEWS.index(v))
                elif key == "spectrum_segments":
                    i = w.spectrum.segments_box.findData(v)
                    if i >= 0:
                        w.spectrum.segments_box.setCurrentIndex(i)
                elif key == "theme":
                    w.theme_action.setChecked(v == "light")
            self._sync_board_widgets()
        finally:
            self._applying = False

    def _sync_board_widgets(self):
        a = self.win.acquisition
        v = self.store.values
        for box, value in ((a.avg_box, v["averaging"]),
                           (a.range_box, v["range_mT"])):
            i = box.findData(value)
            if i >= 0:
                box.blockSignals(True)
                box.setCurrentIndex(i)
                box.blockSignals(False)
        a.rate_box.blockSignals(True)
        a.rate_box.setValue(int(round(v["sample_rate_hz"])))
        a.rate_box.blockSignals(False)
        a.stream_box.blockSignals(True)
        a.stream_box.setChecked(bool(v["streaming"]))
        a.stream_box.blockSignals(False)

    def _update_rate_limit(self):
        """The rate box cannot even be set to an impossible value."""
        top = spec.max_rate_hz(self.store.values["averaging"], self.loop)
        box = self.win.acquisition.rate_box
        box.blockSignals(True)
        box.setMaximum(max(1, int(top)))
        box.blockSignals(False)

    # ================================================================== file

    def _file_event(self, *_):
        p = self.store.path
        if p.exists() and str(p) not in self.watcher.files():
            self.watcher.addPath(str(p))
        if not self.store.file_changed_externally():
            return
        QTimer.singleShot(150, self._reload_file)    # let the editor finish

    def _reload_file(self):
        if not self.store.file_changed_externally():
            return
        probe = SettingsStore(self.store.path)
        if not probe.load(self.context(), base=self.store.values):
            self.message.emit(f"# {probe.load_error or 'settings.json unreadable'}"
                              " -- keeping the current settings")
            return
        changes = {k: v for k, v in probe.values.items()
                   if self.store.values.get(k) != v}
        self.store.presets = probe.presets
        if probe.load_error:
            self.message.emit(f"# settings.json: ignored {probe.load_error}")
        if changes:
            res = self.request(changes, "file")
            if not res["ok"]:
                self.message.emit("# settings.json rejected: " + "; ".join(
                    f"{k}: {v}" for k, v in res["errors"].items()))
            else:
                self.message.emit("# settings.json: applied "
                                  + ", ".join(res.get("applied", {})))
        self.store._last_written = None
        self.store.save()                # normalise what is on disk

    # ============================================================== for model

    def snapshot(self):
        return {"requested": copy.deepcopy(self.store.values),
                "board_actual": dict(self.actual),
                "mismatch": self.mismatch(),
                "board_busy": self.board_busy(),
                "limits": spec.ceilings(self.store.values["averaging"],
                                        self.loop,
                                        self.context().rtd_hz),
                "loop_overhead_us": round(self.loop.overhead_us),
                "presets": self.store.preset_names()}

    def options(self, averaging=None):
        return describe_options(self.store, self.context(), averaging)

    def check(self, changes, snap=False):
        """Validate without applying."""
        return self.store.validate(changes, self.context(),
                                   snap=snap).as_dict()

    def preset(self, name):
        return self.store.preset(name)

    def save_preset(self, name, keys=None):
        p = self.store.save_preset(name, keys)
        self._save_timer.start(300)
        return p


class GuiBridge(QObject):
    """Runs a callable on the GUI thread and hands back its result, so the
    assistant's worker thread never touches widgets directly."""

    _call = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._call.connect(self._run, Qt.BlockingQueuedConnection)

    @Slot(object)
    def _run(self, job):
        try:
            job["result"] = job["fn"]()
        except Exception as e:                       # noqa: BLE001
            job["error"] = e

    def call(self, fn):
        from PySide6.QtCore import QThread
        if QThread.currentThread() is self.thread():
            return fn()
        job = {"fn": fn}
        self._call.emit(job)
        if "error" in job:
            raise job["error"]
        return job.get("result")
