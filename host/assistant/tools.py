"""
The assistant's hands: what the model may look at and what it may change.

Read tools run immediately. Anything with a side effect -- editing a file or
sending a command to the board -- is only *proposed*: it is queued as a
Proposal and nothing happens until a person clicks Apply in the GUI. The
model is told this in the tool result, so it does not claim a change it has
not been allowed to make.

Pure Python, no Qt, so it can be tested without a display.
"""

import difflib
import fnmatch
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config

# Firmware command grammar, mirrored from handle_command() in main.c.
BOARD_COMMANDS = {
    "R": "R <hz>     main-loop sample rate, 1..5000 Hz",
    "A": "A <n>      TMAG averaging, one of 1 2 4 8 16 32",
    "G": "G <mT>     magnetic full scale, 25 50 or 100",
    "P": "P <0|1>    pause (0) / resume (1) streaming",
    "Z": "Z          re-report the current configuration",
}
_COMMAND_RE = re.compile(r"^(R\s+\d+|A\s+(1|2|4|8|16|32)|G\s+(25|50|100)"
                         r"|P\s+[01]|Z)$")


class ToolError(Exception):
    """A mistake the model can fix -- returned to it as the tool result."""


@dataclass
class Proposal:
    id: int
    kind: str                      # "edit" or "command"
    reason: str
    path: str = ""                 # edit: project-relative path
    old_text: str = ""
    new_text: str = ""
    diff: str = ""
    command: str = ""              # command: the line sent to the board
    status: str = "pending"        # pending / applied / rejected / undone
    backup: str = ""
    created: float = field(default_factory=time.time)

    @property
    def title(self):
        if self.kind == "command":
            return f"#{self.id}  board: {self.command}"
        return f"#{self.id}  {self.path}"


# =============================================================== workspace

class Workspace:

    def __init__(self, root=None, status_provider=None, command_sender=None):
        self.root = Path(root or config.PROJECT_ROOT).resolve()
        self.status_provider = status_provider      # () -> dict
        self.command_sender = command_sender        # (str) -> bool
        self.proposals = []
        self.on_proposal = None                     # (Proposal) -> None

    # -- path policing ----------------------------------------------------

    def _rel(self, path):
        return path.relative_to(self.root).as_posix()

    def _under(self, rel, roots):
        return any(rel == r or rel.startswith(r.rstrip("/") + "/")
                   for r in roots)

    def _excluded(self, rel):
        return any(part in config.EXCLUDE_PARTS for part in rel.split("/"))

    def resolve(self, path, write=False):
        """Project-relative path -> absolute Path, or ToolError."""
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
        if write and self._under(rel, ["host/assistant"]):
            # The assistant may read its own code but never rewrite its own
            # sandbox rules.
            raise ToolError("the assistant's own code is read-only to it")
        roots = config.WRITE_ROOTS if write else config.READ_ROOTS
        if not self._under(rel, roots):
            what = "edit" if write else "read"
            raise ToolError(f"not allowed to {what} {rel}. Allowed: "
                            + ", ".join(roots))
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
        text = data.decode("utf-8", errors="replace")
        eol = "\r\n" if "\r\n" in text else "\n"
        return text.replace("\r\n", "\n"), eol

    # -- read tools -------------------------------------------------------

    def list_files(self, subdir=""):
        rows = []
        for p, rel in self.iter_files(subdir or ""):
            try:
                n = p.read_bytes().count(b"\n") + 1
            except OSError:
                continue
            flag = "  [editable]" if self._under(rel, config.WRITE_ROOTS) \
                else "  [read-only]"
            rows.append(f"{rel}  ({n} lines){flag}")
        return "\n".join(rows) or "no files found"

    def read_file(self, path, start_line=1, end_line=None):
        full = self.resolve(path)
        if not full.is_file():
            raise ToolError(f"{path} does not exist")
        text, _ = self._read_text(full)
        lines = text.split("\n")
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
                text, _ = self._read_text(p)
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

    # -- proposals --------------------------------------------------------

    def _queue(self, prop):
        self.proposals.append(prop)
        if self.on_proposal:
            self.on_proposal(prop)
        return prop

    @staticmethod
    def _locate(text, old):
        """(start, end) of `old` in `text`, exact first, then ignoring
        trailing whitespace per line -- local models often drop a space."""
        count = text.count(old)
        if count == 1:
            i = text.index(old)
            return i, i + len(old)
        if count > 1:
            raise ToolError(f"old_text occurs {count} times; include more "
                            "surrounding lines so it is unique")
        lines = text.split("\n")
        want = [l.rstrip() for l in old.strip("\n").split("\n")]
        n = len(want)
        found = [i for i in range(len(lines) - n + 1)
                 if [l.rstrip() for l in lines[i:i + n]] == want]
        if len(found) == 1:
            i = found[0]
            start = sum(len(l) + 1 for l in lines[:i])
            end = start + len("\n".join(lines[i:i + n]))
            return start, end
        if len(found) > 1:
            raise ToolError("old_text matches several places; include more "
                            "surrounding lines so it is unique")
        raise ToolError("old_text was not found. Re-read the file with "
                        "read_file and copy the lines exactly (without the "
                        "line-number prefix)")

    def propose_edit(self, path, old_text, new_text, reason=""):
        full = self.resolve(path, write=True)
        rel = self._rel(full)
        old_text = (old_text or "").replace("\r\n", "\n")
        new_text = (new_text or "").replace("\r\n", "\n")

        if full.exists():
            text, _ = self._read_text(full)
            if not old_text:
                raise ToolError("old_text is empty but the file exists; give "
                                "the exact lines to replace")
            start, end = self._locate(text, old_text)
            after = text[:start] + new_text + text[end:]
        else:
            if old_text:
                raise ToolError(f"{rel} does not exist; to create it, pass "
                                "an empty old_text")
            text, after = "", new_text

        if after == text:
            raise ToolError("new_text is identical to old_text; nothing to do")

        diff = "".join(difflib.unified_diff(
            text.splitlines(True), after.splitlines(True),
            fromfile=f"a/{rel}", tofile=f"b/{rel}", n=3))
        prop = self._queue(Proposal(
            id=len(self.proposals) + 1, kind="edit", reason=reason or "",
            path=rel, old_text=old_text, new_text=new_text, diff=diff))
        return (f"Edit #{prop.id} to {rel} is queued and shown to the user "
                "as a diff. It is NOT applied yet: the user must click "
                "Apply. Do not say the change has been made.")

    def send_board_command(self, command, reason=""):
        cmd = " ".join((command or "").strip().upper().split())
        if not _COMMAND_RE.match(cmd):
            raise ToolError("not a valid firmware command. Valid commands:\n"
                            + "\n".join(BOARD_COMMANDS.values()))
        prop = self._queue(Proposal(
            id=len(self.proposals) + 1, kind="command",
            reason=reason or "", command=cmd))
        return (f"Command #{prop.id} '{cmd}' is queued; it is sent only "
                "when the user clicks Apply.")

    # -- applying (called by the GUI, never by the model) ------------------

    def get(self, pid):
        for p in self.proposals:
            if p.id == pid:
                return p
        raise KeyError(pid)

    def apply(self, pid):
        prop = self.get(pid)
        if prop.status != "pending":
            raise ToolError(f"#{pid} is already {prop.status}")

        if prop.kind == "command":
            if self.command_sender is None or not self.command_sender(
                    prop.command):
                raise ToolError("the board is not connected; nothing sent")
            prop.status = "applied"
            return f"sent '{prop.command}'"

        full = self.resolve(prop.path, write=True)
        if full.exists():
            text, eol = self._read_text(full)
            # Re-locate: the file may have changed since the proposal.
            start, end = self._locate(text, prop.old_text)
            after = text[:start] + prop.new_text + text[end:]
            config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            backup = config.BACKUP_DIR / f"{stamp}_#{prop.id}_{full.name}"
            shutil.copy2(full, backup)
            prop.backup = str(backup)
        else:
            eol, after = "\r\n", prop.new_text      # Windows project
            full.parent.mkdir(parents=True, exist_ok=True)
            prop.backup = ""

        with open(full, "w", encoding="utf-8", newline="") as f:
            f.write(after.replace("\n", eol))
        prop.status = "applied"
        return f"wrote {prop.path}"

    def reject(self, pid):
        prop = self.get(pid)
        if prop.status == "pending":
            prop.status = "rejected"

    def undo(self, pid):
        prop = self.get(pid)
        if prop.kind != "edit" or prop.status != "applied":
            raise ToolError(f"#{pid} cannot be undone")
        full = self.resolve(prop.path, write=True)
        if prop.backup:
            shutil.copy2(prop.backup, full)
        else:
            full.unlink()                           # it was a new file
        prop.status = "undone"
        return f"restored {prop.path}"

    # -- dispatch ---------------------------------------------------------

    def call(self, name, args):
        fn = {
            "list_files": self.list_files,
            "read_file": self.read_file,
            "search": self.search,
            "get_live_status": self.get_live_status,
            "propose_edit": self.propose_edit,
            "send_board_command": self.send_board_command,
        }.get(name)
        if fn is None:
            return f"ERROR: unknown tool {name}"
        if isinstance(args, str):           # some models send a JSON string
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
    _fn("list_files",
        "List the project's source files with line counts and whether each "
        "is editable.",
        {"subdir": {**_S, "description": "optional prefix, e.g. 'host' or "
                                          "'fw/SPI_BOTH/src'"}}),
    _fn("read_file",
        "Read part of a file, with line numbers. Up to 400 lines per call.",
        {"path": {**_S, "description": "project-relative, e.g. "
                                        "'fw/SPI_BOTH/src/main.c'"},
         "start_line": _I, "end_line": _I},
        ["path"]),
    _fn("search",
        "Find lines matching text (or a regex) across the project. Use this "
        "to locate a symbol before reading around it.",
        {"query": _S,
         "path": {**_S, "description": "optional path prefix or glob"},
         "regex": _B, "ignore_case": _B},
        ["query"]),
    _fn("get_live_status",
        "Current state of the instrument from the running GUI: connection, "
        "sample rate, firmware configuration, per-channel statistics over "
        "the last few seconds, tare, and the latest board log lines."),
    _fn("propose_edit",
        "Propose replacing old_text with new_text in one file. old_text must "
        "be copied exactly from read_file output (without line numbers) and "
        "be unique in the file. For a new file pass old_text=''. The user "
        "reviews the diff and decides; the edit is not applied by this call.",
        {"path": _S, "old_text": _S, "new_text": _S,
         "reason": {**_S, "description": "one line, shown to the user"}},
        ["path", "old_text", "new_text", "reason"]),
    _fn("send_board_command",
        "Propose a firmware command for the connected board. Valid: "
        + "; ".join(BOARD_COMMANDS.values())
        + ". The user must approve before it is sent.",
        {"command": _S, "reason": _S},
        ["command", "reason"]),
]
