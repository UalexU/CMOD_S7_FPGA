# Assistant dock — local LLM inside the TMAG5170 Scope

A chat panel in `tmag_scope.py` (**View → Assistant**, `Ctrl+K`) backed by a
model running in Ollama on this PC. Nothing leaves the machine.

The model is **not trained** on the project. It gets a map of the project in
its system prompt and reads the real files through tools when it needs them,
so it always works from the code as it is now.

## What it can do

| Tool | Effect |
|---|---|
| `list_files`, `read_file`, `search` | read the project's own sources (no vendor/BSP/generated files) |
| `get_live_status` | connection, firmware config, rate, tare, last-5 s stats per channel, board log tail |
| `propose_edit` | queue a change to `host/`, `fw/*/src/` or `README.md` — **shown as a diff, applied only when you click Apply** |
| `send_board_command` | queue `R/A/G/P/Z` for the board — sent only when you click Apply |

`hw/` (block design, constraints) is read-only. Every applied edit is backed
up to `host/assistant/.backups/`, and **Undo** restores it.

After applying a firmware edit you still rebuild in Vitis and run
`run_board.py`; after a `host/` edit, restart the GUI.

## Models

| Model | Use |
|---|---|
| `qwen3-coder:30b` | **default** — best tool use and code edits of the installed set, fast (MoE, ~3B active) |
| `gpt-oss:20b` | good second choice; shows a "Thinking…" phase first |
| `gemma4:*` | fine for questions; less reliable at exact edits |

Pick in the dock's **Model** box (remembered between runs).

## Settings (`config.py` or environment variables)

```
set ASSISTANT_MODEL=gpt-oss:20b
set ASSISTANT_NUM_CTX=32768        # lower if VRAM is tight; do not go below ~16k
set OLLAMA_HOST=http://localhost:11434
```

No extra pip packages: the Ollama client uses only the standard library.

## Example questions

- *Where is the RTD temperature computed, and which constants does it use?*
- *What limits the sample rate that reaches the PC?*
- *Is Bz noisier than Bx right now?*
- *Add a `--port COM5` argument to tmag_scope.py that auto-connects.*
- *Set the full scale to 25 mT.*
