"""
AssistantPanel -- the chat dock inside tmag_scope.py.

    from assistant.dock import AssistantPanel
    panel = AssistantPanel(status_provider=..., command_sender=...)

The model runs in a QThread so the plots keep drawing while it thinks.
Proposed edits and board commands land in the "Pending changes" list; only
the Apply button touches a file or the serial port.
"""

import html
import json

from PySide6.QtCore import QSettings, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPlainTextEdit, QPushButton, QSplitter, QTextBrowser, QTextEdit,
    QVBoxLayout, QWidget,
)

from . import config
from .agent import Agent
from .ollama_client import OllamaError, list_models
from .tools import ToolError, Workspace

try:                                    # host/theme.py when run from the GUI
    import theme
except ImportError:                     # pragma: no cover
    theme = None


def _status_colour(name, fallback):
    return getattr(theme, name, fallback) if theme else fallback


# ================================================================ workers

class _ModelLister(QThread):
    done = Signal(list, str)

    def run(self):
        try:
            self.done.emit(list_models(), "")
        except OllamaError as e:
            self.done.emit([], str(e))


class _AgentRun(QThread):
    event = Signal(str, object)         # kind, payload
    failed = Signal(str)

    def __init__(self, agent, text):
        super().__init__()
        self.agent, self.text = agent, text
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        def emit(kind, *data):
            self.event.emit(kind, data)
        try:
            self.agent.ask(self.text, emit, lambda: self._stop)
        except OllamaError as e:
            self.failed.emit(str(e))
        except Exception as e:                       # noqa: BLE001
            self.failed.emit(f"{type(e).__name__}: {e}")


# ================================================================== panel

def _safe_markdown(text):
    """Escape < and > outside code. Qt's markdown reader treats anything
    like <hz> or <n> as an HTML tag, and an unclosed one swallows the rest
    of the transcript -- exactly what firmware help text ("R <hz>") does."""
    out, fenced = [], False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            out.append(line)
            continue
        if fenced:
            out.append(line)
            continue
        parts = line.split("`")
        for i in range(0, len(parts), 2):          # outside inline code
            parts[i] = parts[i].replace("<", "&lt;").replace(">", "&gt;")
        out.append("`".join(parts))
    if fenced:                                     # still streaming a block
        out.append("```")
    return "\n".join(out)


def _describe_call(name, args):
    if not isinstance(args, dict):
        return name
    if name == "read_file":
        rng = ""
        if args.get("start_line"):
            rng = f" {args.get('start_line')}-{args.get('end_line', '')}"
        return f"read {args.get('path', '?')}{rng}"
    if name == "search":
        return f"search '{args.get('query', '')}'"
    if name == "list_files":
        return f"list files {args.get('subdir', '')}".rstrip()
    if name == "propose_edit":
        return f"propose edit to {args.get('path', '?')}"
    if name == "send_board_command":
        return f"propose board command '{args.get('command', '')}'"
    if name == "get_live_status":
        return "read live instrument status"
    return f"{name} {json.dumps(args)[:80]}"


class AssistantPanel(QWidget):

    proposal_added = Signal(int)        # crosses from the worker thread

    def __init__(self, status_provider=None, command_sender=None,
                 parent=None):
        super().__init__(parent)
        self._provider = status_provider
        self._snapshot = {"connected": False}
        self.ws = Workspace(status_provider=lambda: self._snapshot,
                            command_sender=command_sender)
        self.ws.on_proposal = lambda p: self.proposal_added.emit(p.id)
        self.agent = Agent(self.ws)
        self.run = None
        self._transcript = []           # markdown blocks
        self._live = None               # index of the reply being streamed
        self._thinking = False

        self._build()
        self.proposal_added.connect(self._on_proposal)

        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.timeout.connect(self._render)

        # Live status is sampled on the GUI thread, where the acquisition
        # lives, and the worker only ever reads the copy.
        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._sample_status)
        self._status_timer.start(1000)

        self.refresh_models()
        self._say("assistant",
                  "Ask about the firmware, the GUI or the live data. I read "
                  "the project files before answering, and any change I "
                  "suggest waits for you under **Pending changes**.")

    # -- layout -----------------------------------------------------------

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        top = QHBoxLayout()
        top.addWidget(QLabel("Model"))
        self.model_box = QComboBox()
        self.model_box.setMinimumWidth(170)
        self.model_box.currentTextChanged.connect(self._model_changed)
        top.addWidget(self.model_box, 1)
        refresh = QPushButton("Refresh")
        refresh.setToolTip("Re-read the installed Ollama models")
        refresh.clicked.connect(self.refresh_models)
        top.addWidget(refresh)
        new = QPushButton("New chat")
        new.clicked.connect(self.new_chat)
        top.addWidget(new)
        root.addLayout(top)

        split = QSplitter(Qt.Vertical)

        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(False)
        split.addWidget(self.view)

        pending = QWidget()
        pl = QVBoxLayout(pending)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(4)
        head = QLabel("Pending changes")
        head.setProperty("role", "hint")
        pl.addWidget(head)
        self.prop_list = QListWidget()
        self.prop_list.setMaximumHeight(90)
        self.prop_list.currentRowChanged.connect(self._show_proposal)
        pl.addWidget(self.prop_list)
        self.diff = QTextEdit()
        self.diff.setReadOnly(True)
        self.diff.setLineWrapMode(QTextEdit.NoWrap)
        pl.addWidget(self.diff, 1)
        buttons = QHBoxLayout()
        self.apply_btn = QPushButton("Apply")
        self.apply_btn.setProperty("role", "primary")
        self.apply_btn.clicked.connect(self._apply)
        self.reject_btn = QPushButton("Reject")
        self.reject_btn.clicked.connect(self._reject)
        self.undo_btn = QPushButton("Undo")
        self.undo_btn.setToolTip("Restore the file from the backup taken "
                                 "when this edit was applied")
        self.undo_btn.clicked.connect(self._undo)
        for b in (self.apply_btn, self.reject_btn, self.undo_btn):
            buttons.addWidget(b)
        pl.addLayout(buttons)
        split.addWidget(pending)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        # Nothing to review yet -- give the conversation the whole height.
        self.pending = pending
        self.pending.hide()
        root.addWidget(split, 1)

        self.input = QPlainTextEdit()
        self.input.setPlaceholderText(
            "e.g. Why does the RTD read 2 °C above the die sensor?   "
            "(Ctrl+Enter to send)")
        self.input.setMaximumHeight(64)
        root.addWidget(self.input)

        bottom = QHBoxLayout()
        self.state = QLabel("")
        self.state.setProperty("role", "hint")
        bottom.addWidget(self.state, 1)
        self.send_btn = QPushButton("Send")
        self.send_btn.setProperty("role", "primary")
        self.send_btn.clicked.connect(self._send_or_stop)
        bottom.addWidget(self.send_btn)
        root.addLayout(bottom)

        for seq in ("Ctrl+Return", "Ctrl+Enter"):
            sc = QShortcut(QKeySequence(seq), self.input)
            sc.activated.connect(self._send_or_stop)

        self._update_buttons()

    # -- models -----------------------------------------------------------

    def refresh_models(self):
        self.state.setText("Looking for Ollama…")
        self._lister = _ModelLister()
        self._lister.done.connect(self._models_listed)
        self._lister.start()

    def _models_listed(self, names, error):
        if error:
            self.state.setText(error.split("\n")[0])
            if self.model_box.count() == 0:
                self.model_box.addItem(config.DEFAULT_MODEL)
            return
        saved = QSettings("TMAG5170 Scope", "tmag_scope").value(
            "assistant_model", config.DEFAULT_MODEL)
        self.model_box.blockSignals(True)
        self.model_box.clear()
        self.model_box.addItems(names)
        self.model_box.blockSignals(False)
        for want in (saved, config.DEFAULT_MODEL):
            if want in names:
                self.model_box.setCurrentText(want)
                break
        self._model_changed(self.model_box.currentText())
        self.state.setText(f"{len(names)} models at {config.OLLAMA_URL}")

    def _model_changed(self, name):
        if name:
            self.agent.model = name
            QSettings("TMAG5170 Scope", "tmag_scope").setValue(
                "assistant_model", name)

    # -- chat -------------------------------------------------------------

    def new_chat(self):
        if self.run is not None:
            return
        self.agent.reset()
        self._transcript.clear()
        self._live = None
        self._say("assistant", "New conversation.")

    def _send_or_stop(self):
        if self.run is not None:
            self.run.stop()
            self.state.setText("Stopping…")
            return
        text = self.input.toPlainText().strip()
        if not text:
            return
        self.input.clear()
        self._say("user", text)
        self._live = None
        self.run = _AgentRun(self.agent, text)
        self.run.event.connect(self._on_event)
        self.run.failed.connect(self._on_failed)
        self.run.finished.connect(self._on_finished)
        self.state.setText(f"{self.agent.model} is working… (first answer "
                           "loads the model and can take a while)")
        self.send_btn.setText("Stop")
        self.run.start()

    def _on_event(self, kind, data):
        if kind == "token":
            if self._live is None:
                self._transcript.append("**Assistant:** ")
                self._live = len(self._transcript) - 1
            self._transcript[self._live] += data[0]
            self._thinking = False
        elif kind == "thinking":
            if not self._thinking:
                self._thinking = True
                self.state.setText("Thinking…")
        elif kind == "tool":
            name, args = data
            self._transcript.append(f"› `{_describe_call(name, args)}`")
            self._live = None
            self.state.setText(_describe_call(name, args) + "…")
        elif kind == "tool_result":
            name, result = data
            if result.startswith("ERROR"):
                short = result[:160].replace("`", "'").replace("\n", " ")
                self._transcript[-1] += f" — _{short}_"
        self._schedule_render()

    def _on_failed(self, message):
        self._say("error", message)

    def _on_finished(self):
        self.run = None
        self.send_btn.setText("Send")
        self.state.setText("")
        self._schedule_render()

    def _say(self, who, text):
        prefix = {"user": "**You:** ", "assistant": "**Assistant:** ",
                  "note": "", "error": "**Error:** "}[who]
        if who == "note":
            text = f"_{text}_"
        self._transcript.append(prefix + text)
        self._schedule_render()

    def _schedule_render(self):
        if not self._render_timer.isActive():
            self._render_timer.start(80)

    def _render(self):
        bar = self.view.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 4
        self.view.setMarkdown(
            "\n\n".join(_safe_markdown(b) for b in self._transcript))
        if at_bottom:
            bar.setValue(bar.maximum())

    # -- live status ------------------------------------------------------

    def _sample_status(self):
        if self._provider is None or not self.isVisible():
            return
        try:
            self._snapshot = self._provider()
        except Exception as e:                       # noqa: BLE001
            self._snapshot = {"error": f"status unavailable: {e}"}

    # -- proposals --------------------------------------------------------

    def _on_proposal(self, pid):
        prop = self.ws.get(pid)
        self.pending.show()
        item = QListWidgetItem(f"{prop.title}  — pending")
        item.setData(Qt.UserRole, pid)
        self.prop_list.addItem(item)
        self.prop_list.setCurrentItem(item)

    def _current(self):
        item = self.prop_list.currentItem()
        if item is None:
            return None
        return self.ws.get(item.data(Qt.UserRole))

    def _refresh_item(self, prop):
        for i in range(self.prop_list.count()):
            item = self.prop_list.item(i)
            if item.data(Qt.UserRole) == prop.id:
                item.setText(f"{prop.title}  — {prop.status}")

    def _show_proposal(self, _row=None):
        prop = self._current()
        self._update_buttons()
        if prop is None:
            self.diff.clear()
            return
        add = _status_colour("GOOD", "#0ca30c")
        rem = _status_colour("CRITICAL", "#d03b3b")
        lines = [f"<b>{html.escape(prop.reason or '')}</b>", ""]
        if prop.kind == "command":
            lines.append(f"Send to board:  <code>{html.escape(prop.command)}"
                         "</code>")
        else:
            for line in prop.diff.splitlines():
                esc = html.escape(line)
                if line.startswith("+") and not line.startswith("+++"):
                    esc = f"<span style='color:{add}'>{esc}</span>"
                elif line.startswith("-") and not line.startswith("---"):
                    esc = f"<span style='color:{rem}'>{esc}</span>"
                elif line.startswith("@@"):
                    esc = f"<span style='color:gray'>{esc}</span>"
                lines.append(esc)
        self.diff.setHtml("<pre style='font-family:Consolas,monospace'>"
                          + "\n".join(lines) + "</pre>")

    def _update_buttons(self):
        prop = self._current()
        pending = prop is not None and prop.status == "pending"
        self.apply_btn.setEnabled(pending)
        self.reject_btn.setEnabled(pending)
        self.undo_btn.setEnabled(prop is not None and prop.kind == "edit"
                                 and prop.status == "applied")

    def _apply(self):
        prop = self._current()
        if prop is None:
            return
        try:
            msg = self.ws.apply(prop.id)
        except (ToolError, OSError) as e:
            self._say("error", f"#{prop.id} not applied: {e}")
            return
        extra = ""
        if prop.kind == "edit":
            if prop.path.startswith("fw/"):
                extra = " Rebuild in Vitis and run run_board.py to load it."
            elif prop.path.startswith("host/"):
                extra = " Restart the GUI for it to take effect."
        self._say("note", f"Applied #{prop.id}: {msg}.{extra}")
        self.agent.note(f"The user APPLIED proposal #{prop.id} ({msg}).")
        self._refresh_item(prop)
        self._update_buttons()

    def _reject(self):
        prop = self._current()
        if prop is None:
            return
        self.ws.reject(prop.id)
        self._say("note", f"Rejected #{prop.id}.")
        self.agent.note(f"The user REJECTED proposal #{prop.id}.")
        self._refresh_item(prop)
        self._update_buttons()

    def _undo(self):
        prop = self._current()
        if prop is None:
            return
        try:
            msg = self.ws.undo(prop.id)
        except (ToolError, OSError) as e:
            self._say("error", str(e))
            return
        self._say("note", f"Undid #{prop.id}: {msg}.")
        self.agent.note(f"The user UNDID proposal #{prop.id}; the file is "
                        "back to how it was before it.")
        self._refresh_item(prop)
        self._update_buttons()

    def shutdown(self):
        if self.run is not None:
            self.run.stop()
            self.run.wait(2000)
