"""
The conversation loop: user question -> model -> tool calls -> model -> ...
until the model answers without calling a tool.

The model is not trained on the project. It is handed a map of it in the
system prompt and reads the actual files through tools when it needs them,
so its answers are about the code as it is now, not as it was last month.
"""

from . import config
from .ollama_client import chat_stream
from .tools import BOARD_COMMANDS, TOOLS

SYSTEM_PROMPT = """\
You are the engineering assistant built into the TMAG5170 Scope GUI for the \
Yale Low Field lab. You help with one project: a Digilent Cmod S7-25 FPGA \
board (MicroBlaze soft CPU) reading a TI TMAG5170 3-axis Hall sensor and a \
MAX31865 RTD amplifier over two AXI Quad SPI cores, streaming CSV over \
UART to a PC-side Python GUI.

PROJECT LAYOUT (paths relative to the project root)
- hw/            Vivado project. Block design: \
hw/CMOD_S7 FPGA.srcs/sources_1/bd/design_1/design_1.bd; constraints in \
hw/CMOD_S7 FPGA.srcs/constrs_1. READ-ONLY for you.
- xsa/           exported hardware handed from Vivado to Vitis.
- fw/            Vitis workspace. Firmware apps: fw/SPI_BOTH/src/main.c \
(Hall + RTD, the current app), fw/SPI_Hall_Temp/src/main.c (currently an \
identical copy), fw/SPI_Hull_Sensor/src/SPI_Hull.c (older Hall-only app). \
Unless the user says otherwise, firmware questions and edits mean \
fw/SPI_BOTH/src/main.c.
- host/          PC side. host/tmag_scope.py is the current PySide6 + \
pyqtgraph GUI (the one you live in); host/sensor.py owns the serial link, \
parsing and the command channel; host/theme.py colours; host/run_board.py \
programs the board; host/tmag_gui.py is the older Tk GUI; host/scope_v2/ is \
an older snapshot of the PySide6 GUI -- do not edit it unless asked.
- Workflow: change hardware in Vivado -> export .xsa -> update the Vitis \
platform -> rebuild the app -> python host/run_board.py.

FIRMWARE COMMANDS (UART, one per line, the GUI sends them)
{commands}

HOW TO WORK
- Never guess about code. Use search to locate things, then read_file \
around them, then answer, citing file:line.
- Prefer small, surgical edits. Before propose_edit, read the exact lines; \
copy old_text verbatim (without the line-number prefix) with enough context \
to be unique. One logical change per propose_edit call.
- Edits and board commands are only proposals. The user approves them in \
the GUI. Never claim a change was made unless a [GUI note] says it was \
applied.
- Firmware edits need a rebuild in Vitis and re-programming before they \
take effect; say so. Hardware changes must be done by the user in Vivado; \
describe them, do not attempt them.
- For questions about what the instrument is doing right now, call \
get_live_status first.
- Be concise. Units: mT for field, degC for temperature, Hz for rates.
"""


class Agent:

    def __init__(self, workspace, model=None):
        self.ws = workspace
        self.model = model or config.DEFAULT_MODEL
        self._notes = []
        self.reset()

    def reset(self):
        cmds = "\n".join(f"  {v}" for v in BOARD_COMMANDS.values())
        self.history = [{"role": "system",
                         "content": SYSTEM_PROMPT.format(commands=cmds)}]

    def note(self, text):
        """Something the model should know at its next turn, e.g. that the
        user applied or rejected a proposal."""
        self._notes.append(text)

    # -- context budget -----------------------------------------------------

    def _trim(self):
        """Keep under ~70 % of num_ctx (≈3.5 chars/token) by blanking the
        oldest tool outputs. The system prompt and the dialogue stay."""
        budget = int(config.NUM_CTX * 0.7 * 3.5)
        size = sum(len(m.get("content") or "") for m in self.history)
        for m in self.history:
            if size <= budget:
                return
            if m["role"] == "tool" and len(m["content"]) > 200:
                size -= len(m["content"]) - 60
                m["content"] = "[old tool output removed to save context; " \
                               "call the tool again if needed]"

    # -- the loop -----------------------------------------------------------

    def ask(self, text, emit=lambda *a: None, should_stop=lambda: False):
        """Run one user turn. `emit(kind, *data)` reports progress:
            ('token', str)          streamed answer text
            ('thinking', str)       reasoning text (gpt-oss, qwen3 ...)
            ('tool', name, args)    a tool is about to run
            ('tool_result', name, str)
        """
        if self._notes:
            text = "\n".join(f"[GUI note] {n}" for n in self._notes) \
                   + "\n\n" + text
            self._notes.clear()
        self.history.append({"role": "user", "content": text})
        options = {"num_ctx": config.NUM_CTX,
                   "num_predict": config.NUM_PREDICT,
                   "temperature": config.TEMPERATURE}

        for _step in range(config.MAX_TOOL_STEPS):
            self._trim()
            content, calls = [], []
            for chunk in chat_stream(self.model, self.history, TOOLS,
                                     options, should_stop):
                if chunk["thinking"]:
                    emit("thinking", chunk["thinking"])
                if chunk["content"]:
                    content.append(chunk["content"])
                    emit("token", chunk["content"])
                calls.extend(chunk["tool_calls"])

            msg = {"role": "assistant", "content": "".join(content)}
            if calls:
                msg["tool_calls"] = calls
            self.history.append(msg)

            if should_stop() or not calls:
                return

            for call in calls:
                fn = call.get("function", {})
                name, args = fn.get("name", "?"), fn.get("arguments") or {}
                emit("tool", name, args)
                result = self.ws.call(name, args)
                emit("tool_result", name, result)
                self.history.append({"role": "tool", "tool_name": name,
                                     "content": result})
                if should_stop():
                    return

        emit("token", f"\n\n_(stopped after {config.MAX_TOOL_STEPS} tool "
                      "steps -- ask me to continue)_")
