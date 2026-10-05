# Assistant dock — local LLM inside the TMAG5170 Scope

A chat panel in `tmag_scope.py` (**View → Assistant**, `Ctrl+K`) backed by a
model running in Ollama on this PC. Nothing leaves the machine.

The assistant is an **intermediary**, not an editor. It can:

| Tool | Effect |
|---|---|
| `describe_options`, `get_settings` | what can be set, and to what — computed from the datasheets and firmware (`host/instrument/spec.py`) |
| `set_settings`, `apply_preset` | change settings through the same checks as the GUI controls. Impossible values are **rejected with the reason**; nothing changes |
| `save_preset` | save the current settings under a name |
| `get_live_status` | live statistics, measured rate, board log |
| `list_files`, `read_file`, `search` | read the project to explain how it works (**read-only**) |

It cannot edit files or send raw serial commands.

- **Display** settings (window, refresh, smoothing, channels, view, theme)
  apply at once.
- **Board** settings (averaging, sample rate, range, streaming) show a small
  approval card under the chat. **Apply** or **Reject** closes it, and the
  outcome is one line in the conversation. Only one card exists at a time;
  a newer proposal replaces an undecided older one. Applied changes are sent
  one at a time, and each command is reported as acknowledged, board error,
  or no reply, from the firmware's `# ACK` / `# ERR` answer.
- **Undo** (next to Send) reverts the most recent applied change, display or
  board.

See `host/instrument/README.md` for what the limits are and where they come
from.

## Models

| Model | Use |
|---|---|
| `qwen3-coder:30b` | **default** — most reliable tool use of the installed set |
| `gpt-oss:20b` | good second choice; shows a "Thinking…" phase first |
| `gemma4:*` | fine for questions |

## Settings (`config.py` or environment variables)

```
set ASSISTANT_MODEL=gpt-oss:20b
set ASSISTANT_NUM_CTX=32768        # lower if VRAM is tight; do not go below ~16k
set ASSISTANT_NUM_PREDICT=4096     # longest reply, tokens; -1 = unlimited
set ASSISTANT_TEMPERATURE=0.2
set ASSISTANT_MAX_STEPS=16         # tool calls per question
set OLLAMA_HOST=http://localhost:11434
```

No extra pip packages: the Ollama client uses only the standard library.

## Example requests

- *Give me the quietest readings you can.*
- *What is the fastest I can sample, and what limits it?*
- *Set the sample rate to 399 Hz.* (it will explain why that is impossible)
- *Show only Bz and switch to the spectrum view.*
- *Save this as "bench test".*
