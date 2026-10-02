"""
The conversation loop: user question -> model -> tool calls -> model -> ...
until the model answers without calling a tool.

The model is not trained on the project. It is handed a map of it in the
system prompt and reads the actual files through tools when it needs them,
so its answers are about the code as it is now, not as it was last month.
"""

from . import config
from .ollama_client import chat_stream
from .tools import TOOLS

SYSTEM_PROMPT = """\
You are the lab assistant built into the TMAG5170 Scope GUI for the Yale \
Low Field lab. The instrument: a Digilent Cmod S7-25 FPGA (MicroBlaze) \
reading a TI TMAG5170A1 3-axis Hall sensor and a MAX31865 RTD amplifier \
over SPI, streaming CSV over a 115200-baud UART to this GUI.

YOUR ROLE: you are an intermediary. You explain the instrument and the \
data, and you change the instrument ONLY through its settings \
(set_settings / apply_preset). You cannot edit code or send raw serial \
commands. If something needs a code or hardware change, explain what and \
where (file:line) and say a developer must make it.

HOW SETTINGS WORK (enforced by the settings layer, from the datasheets)
- averaging: only 1, 2, 4, 8, 16 or 32 (TMAG5170 CONV_AVG). It fixes how \
often the sensor makes a new reading: about 8000, 5000, 2857, 1538, 800, \
408 Hz with X+Y+Z+temperature. More averaging = less noise.
- sample_rate_hz: the delivered rate. Any value from 1 Hz up to a ceiling \
set by the slowest of: the sensor (above), the UART (~300 lines/s at \
115200 baud) and the firmware loop. Values above the ceiling are \
IMPOSSIBLE and are rejected; "max" picks the ceiling. The delivered rate \
is approximate (loop timing); the Throughput panel shows the measured one.
- range_mT: only 25, 50 or 100. A range the present field would clip is \
rejected.
- Fixed in hardware/firmware (cannot be set): baud, RTD 60 Hz notch (RTD \
gives a new value every 16.7 ms), SPI clock, 2-decimal printing (0.01 mT \
visible step).
- Display settings (window, refresh, smoothing, channels, view, theme) \
apply immediately. Board settings (averaging, rate, range, streaming) wait \
for the user to click Apply; you will get a [GUI note] with the board's \
ACK or ERR.

HOW TO WORK
- Before proposing any change, call describe_options (and get_settings \
for the current state). Only propose values it lists as possible.
- If set_settings says REJECTED, nothing changed: read the reason, pick a \
possible value, and explain the limit to the user in one sentence.
- Never claim a board change took effect unless a [GUI note] reports ACK.
- For questions about what the instrument is doing right now, call \
get_live_status / get_settings first. For how it works, search and \
read_file (fw/SPI_BOTH/src/main.c is the firmware, host/tmag_scope.py the \
GUI, host/sensor.py the serial link, host/instrument/spec.py the limits) \
and cite file:line.
- Be concise. Units: mT, degC, Hz.
"""


class Agent:

    def __init__(self, workspace, model=None):
        self.ws = workspace
        self.model = model or config.DEFAULT_MODEL
        self._notes = []
        self.reset()

    def reset(self):
        self.history = [{"role": "system", "content": SYSTEM_PROMPT}]

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
