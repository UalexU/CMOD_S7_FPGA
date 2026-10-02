"""
The assistant's hands.

It can READ the project (to explain how things work) and it can change the
instrument ONLY through the settings layer (host/instrument/), which knows
the TMAG5170, MAX31865, UART and firmware limits and rejects anything the
hardware cannot do. It cannot edit files or send raw serial commands.

    display settings  -> applied at once (undo available)
    board settings    -> queued; a person clicks Apply; the firmware's
                         ACK/ERR is reported back

Pure Python, no Qt: the GUI hands in a `settings` object whose methods run
on the GUI thread (see dock.py).
"""

import fnmatch
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config


class ToolError(Exception):
    """A mistake the model can fix -- returned to it as the tool result."""


@dataclass
class Proposal:
    id: int
    reason: str
    changes: dict                    # key -> new value (validated)
    previous: dict                   # key -> value before
    notes: list = field(default_factory=list)
    status: str = "pending"          # pending / applied / rejected / undone
    board: bool = True               # needs approval (board settings)
    replies: list = field(default_factory=list)   # firmware ACK/ERR lines
    created: float = field(default_factory=time.time)

    @property
    def title(self):
        parts = [f"{k} {_short(self.previous.get(k))}→{_short(v)}"
                 for k, v in self.changes.items()]
        kind = "board" if self.board else "display"
        return f"#{self.id} {kind}: " + ", ".join(parts)


def _short(v):
    if isinstance(v, list):
        return "[" + ",".join(map(str, v)) + "]"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


# ============================================================ read-only

class Workspace:

    def __init__(self, root=None, status_provider=None, settings=None):
        self.root = Path(root or config.PROJECT_ROOT).resolve()
        self.status_provider = status_provider      # () -> dict
        self.settings = settings                    # instrument.controller API
        self.proposals = []
        self.on_proposal = None                     # (Proposal) -> None

    def _rel(self, path):
        return path.relative_to(self.root).as_posix()

    @staticmethod
    def _under(rel, roots):
        return any(rel == r or rel.startswith(r.rstrip("/") + "/")
                   for r in roots)

    @staticmethod
    def _excluded(rel):
        return any(part in config.EXCLUDE_PARTS for part in rel.split("/"))

    def resolve(self, path):
        if not path or not isinstance(path, str):
            raise ToolError("path is required")
        p = path.strip().replace("\\", "/").lstrip("./")
        full = (self.root / p).resolve()
        try:
            rel = self._rel(full)
        except ValueError:
            raise ToolError(f"{path} is outside the project") from None
        if self._excluded(rel):
            raise ToolError(f"{rel} is generated or vendor code; it is not "
                            "available to the assistant")
        if not self._under(rel, config.READ_ROOTS):
            raise ToolError(f"not allowed to read {rel}. Allowed: "
                            + ", ".join(config.READ_ROOTS))
        return full

    def iter_files(self, subdir=""):
        for root in config.READ_ROOTS:
            base = self.root / root
            if base.is_file():
                paths = [base]
            elif base.is_dir():
                # os.walk with pruning, not rglob: host/.venv alone holds
                # tens of thousands of files that must never be visited.
                paths = []
                for dirpath, dirnames, filenames in os.walk(base):
                    dirnames[:] = sorted(
                        d for d in dirnames
                        if d not in config.EXCLUDE_PARTS
                        and not d.startswith("."))
                    paths.extend(Path(dirpath) / f for f in sorted(filenames))
            else:
                paths = []
            for p in paths:
                if not p.is_file():
                    continue
                rel = self._rel(p)
                if self._excluded(rel):
                    continue
                if subdir and not rel.startswith(subdir.strip("/")):
                    continue
                if p.suffix.lower() not in config.TEXT_SUFFIXES:
                    continue
                yield p, rel

    @staticmethod
    def _read_text(path):
        data = path.read_bytes()
        if len(data) > config.MAX_FILE_BYTES:
            raise ToolError(f"{path.name} is too large to read "
                            f"({len(data):,} bytes)")
        return data.decode("utf-8", errors="replace").replace("\r\n", "\n")

    def list_files(self, subdir=""):
        rows = []
        for p, rel in self.iter_files(subdir or ""):
            try:
                n = p.read_bytes().count(b"\n") + 1
            except OSError:
                continue
            rows.append(f"{rel}  ({n} lines)")
        return "\n".join(rows) or "no files found"

    def read_file(self, path, start_line=1, end_line=None):
        full = self.resolve(path)
        if not full.is_file():
            raise ToolError(f"{path} does not exist")
        lines = self._read_text(full).split("\n")
        start = max(1, int(start_line or 1))
        end = int(end_line) if end_line else start + config.MAX_READ_LINES - 1
        end = min(end, len(lines), start + config.MAX_READ_LINES - 1)
        body = "\n".join(f"{i:5d}| {lines[i - 1]}"
                         for i in range(start, end + 1))
        more = ""
        if end < len(lines):
            more = (f"\n... {len(lines) - end} more lines; call read_file "
                    f"with start_line={end + 1}")
        return f"{self._rel(full)}  lines {start}-{end} of {len(lines)}\n" \
               f"{body}{more}"

    def search(self, query, path="", regex=False, ignore_case=True):
        if not query:
            raise ToolError("query is required")
        flags = re.IGNORECASE if ignore_case else 0
        try:
            pat = re.compile(query if regex else re.escape(query), flags)
        except re.error as e:
            raise ToolError(f"bad regex: {e}") from None
        hits = []
        for p, rel in self.iter_files():
            if path and not fnmatch.fnmatch(rel, path) \
                    and not rel.startswith(path.strip("/")):
                continue
            try:
                text = self._read_text(p)
            except ToolError:
                continue
            for i, line in enumerate(text.split("\n"), 1):
                if pat.search(line):
                    hits.append(f"{rel}:{i}: {line.strip()[:200]}")
                    if len(hits) >= config.MAX_SEARCH_HITS:
                        hits.append("... (more hits; narrow the search)")
                        return "\n".join(hits)
        return "\n".join(hits) or "no matches"

    def get_live_status(self):
        if self.status_provider is None:
            return "the GUI is not attached; no live data"
        return json.dumps(self.status_provider(), indent=1, default=str)

    # ============================================================ settings

    def _need_settings(self):
        if self.settings is None:
            raise ToolError("the settings layer is not attached")
        return self.settings

    def get_settings(self):
        return json.dumps(self._need_settings().snapshot(), indent=1,
                          default=str)

    def describe_options(self, averaging=None):
        s = self._need_settings()
        if averaging is not None:
            try:
                averaging = int(str(averaging).rstrip("xX"))
            except ValueError:
                raise ToolError("averaging must be one of 1 2 4 8 16 32") \
                    from None
        try:
            return json.dumps(s.options(averaging), indent=1, default=str)
        except (KeyError, ValueError) as e:
            raise ToolError(f"averaging must be one of 1 2 4 8 16 32 ({e})") \
                from None

    def set_settings(self, changes, reason="", _snap=False):
        s = self._need_settings()
        if isinstance(changes, str):
            try:
                changes = json.loads(changes)
            except json.JSONDecodeError:
                raise ToolError("changes must be a JSON object, e.g. "
                                '{"averaging": 16, "window_s": 10}') from None
        check = s.check(changes, _snap)
        if not check["ok"]:
            return ("REJECTED -- nothing was changed. Fix these and call "
                    "set_settings again (describe_options lists what is "
                    "possible):\n" + json.dumps(check["errors"], indent=1))
        normalized = check["changes"]
        current = s.snapshot()["requested"]
        display = {k: v for k, v in normalized.items()
                   if k in s.DISPLAY_KEYS and current.get(k) != v}
        board = {k: v for k, v in normalized.items()
                 if k in s.BOARD_KEYS and current.get(k) != v}
        if not display and not board:
            return "No change: those are already the current settings."

        out = []
        if display:
            prev = {k: current[k] for k in display}
            res = s.request(display, "assistant", _snap)
            if not res["ok"]:
                return "REJECTED: " + json.dumps(res["errors"])
            prop = self._queue(Proposal(
                id=len(self.proposals) + 1, reason=reason, changes=display,
                previous=prev, status="applied", board=False,
                notes=res["notes"]))
            out.append(f"Display change #{prop.id} APPLIED: "
                       + ", ".join(f"{k}={_short(v)}"
                                   for k, v in display.items()))
        if board:
            prev = {k: current[k] for k in board}
            prop = self._queue(Proposal(
                id=len(self.proposals) + 1, reason=reason, changes=board,
                previous=prev, notes=check["notes"]))
            out.append(f"Board change #{prop.id} is QUEUED for the user's "
                       "approval (not applied yet): "
                       + ", ".join(f"{k}={_short(v)}" for k, v in board.items()))
        if check["notes"]:
            out.append("Notes:\n- " + "\n- ".join(check["notes"]))
        return "\n".join(out)

    def apply_preset(self, name, reason=""):
        s = self._need_settings()
        try:
            values = s.preset(name)
        except KeyError:
            raise ToolError(f"no preset {name!r}. Presets: "
                            + ", ".join(s.snapshot()["presets"])) from None
        # Preset rates are targets ("about 20 Hz"), so they snap to the
        # nearest achievable rate instead of failing on the 1-Hz steps.
        return f"Preset '{name}':\n" + self.set_settings(
            values, reason or f"load preset '{name}'", _snap=True)

    def save_preset(self, name, keys=None):
        s = self._need_settings()
        try:
            saved = s.save_preset(name, keys)
        except ValueError as e:
            raise ToolError(str(e)) from None
        return f"Saved preset '{name}': {json.dumps(saved)}"

    def _queue(self, prop):
        self.proposals.append(prop)
        if self.on_proposal:
            self.on_proposal(prop)
        return prop

    def get(self, pid):
        for p in self.proposals:
            if p.id == pid:
                return p
        raise KeyError(pid)

    # ------------------------------------------------- dispatch for the model

    def call(self, name, args):
        fn = {
            "list_files": self.list_files,
            "read_file": self.read_file,
            "search": self.search,
            "get_live_status": self.get_live_status,
            "get_settings": self.get_settings,
            "describe_options": self.describe_options,
            "set_settings": self.set_settings,
            "apply_preset": self.apply_preset,
            "save_preset": self.save_preset,
        }.get(name)
        if fn is None:
            return f"ERROR: unknown tool {name}"
        if isinstance(args, str):
            try:
                args = json.loads(args or "{}")
            except json.JSONDecodeError:
                return "ERROR: arguments were not valid JSON"
        try:
            return fn(**(args or {}))
        except ToolError as e:
            return f"ERROR: {e}"
        except TypeError as e:
            return f"ERROR: bad arguments for {name}: {e}"


# ============================================================ tool schemas

def _fn(name, description, props=None, required=()):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props or {},
                       "required": list(required)}}}


_S = {"type": "string"}
_I = {"type": "integer"}
_B = {"type": "boolean"}

TOOLS = [
    _fn("get_settings",
        "Current settings: what is requested, what the board actually "
        "reports, any mismatch, the rate limits now, and preset names."),
    _fn("describe_options",
        "Every setting you may change, with its allowed values computed "
        "from the TMAG5170/MAX31865 datasheets, the UART and the firmware. "
        "Call this before proposing a change. Optionally pass a different "
        "averaging to see the limits it would give.",
        {"averaging": {**_I, "description": "1 2 4 8 16 or 32"}}),
    _fn("set_settings",
        "Change settings. Pass only the keys to change, e.g. "
        '{"averaging": 16, "sample_rate_hz": 250, "window_s": 10}. '
        "sample_rate_hz may also be \"max\". Impossible values are rejected "
        "with the reason and nothing changes. Display settings apply at "
        "once; board settings wait for the user to click Apply.",
        {"changes": {"type": "object",
                     "description": "setting name -> new value"},
         "reason": {**_S, "description": "one line, shown to the user"}},
        ["changes", "reason"]),
    _fn("apply_preset",
        "Load a named preset (see get_settings for names) through the same "
        "checks as set_settings.",
        {"name": _S, "reason": _S}, ["name"]),
    _fn("save_preset",
        "Save the current settings as a named preset. Optionally only "
        "some keys.",
        {"name": _S, "keys": {"type": "array", "items": _S}}, ["name"]),
    _fn("get_live_status",
        "Live instrument data from the GUI: connection, per-channel "
        "statistics over the last 5 s, tare, measured rate, board log tail."),
    _fn("list_files",
        "List the project's source files (read-only), with line counts.",
        {"subdir": {**_S, "description": "optional prefix, e.g. 'host' or "
                                          "'fw/SPI_BOTH/src'"}}),
    _fn("read_file",
        "Read part of a source file, with line numbers (max 400 lines).",
        {"path": _S, "start_line": _I, "end_line": _I}, ["path"]),
    _fn("search",
        "Find lines matching text (or a regex) across the project.",
        {"query": _S, "path": _S, "regex": _B, "ignore_case": _B},
        ["query"]),
]
