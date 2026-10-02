"""
Settings for the in-GUI project assistant.

Everything a lab user might want to change lives here, so nobody has to go
looking through the agent or the widget to point it at a different model.
Environment variables override the defaults without editing the file:

    set ASSISTANT_MODEL=gpt-oss:20b
    set OLLAMA_HOST=http://192.168.1.20:11434
"""

import os
from pathlib import Path

# -- Ollama ---------------------------------------------------------------

OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
if not OLLAMA_URL.startswith("http"):
    OLLAMA_URL = "http://" + OLLAMA_URL

# qwen3-coder:30b is a mixture-of-experts model (~3B active parameters), so it
# runs fast for its size and is the strongest tool-caller of the installed
# set. gpt-oss:20b is the fallback; gemma4 is fine for plain Q&A.
DEFAULT_MODEL = os.environ.get("ASSISTANT_MODEL", "qwen3-coder:30b")

# Ollama silently truncates the *start* of the conversation once it exceeds
# num_ctx -- the system prompt and the files the model just read are the
# first casualties. 32k is plenty for this project; lower it if VRAM is short.
NUM_CTX = int(os.environ.get("ASSISTANT_NUM_CTX", "32768"))
# Longest single reply, in tokens. -1 = no limit (until num_ctx is full).
NUM_PREDICT = int(os.environ.get("ASSISTANT_NUM_PREDICT", "4096"))
TEMPERATURE = float(os.environ.get("ASSISTANT_TEMPERATURE", "0.2"))
REQUEST_TIMEOUT_S = 600          # first call loads the model; that is slow
MAX_TOOL_STEPS = int(os.environ.get("ASSISTANT_MAX_STEPS", "16"))

# -- project sandbox ------------------------------------------------------

# host/assistant/config.py -> CMOD_S7/
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Paths (relative to PROJECT_ROOT, forward slashes) the assistant may read.
# It can read, never write: changes go through host/instrument/ settings.
READ_ROOTS = [
    "README.md",
    "host",
    "fw/SPI_BOTH/src",
    "fw/SPI_Hall_Temp/src",
    "fw/SPI_Hull_Sensor/src",
    "hw/CMOD_S7 FPGA.srcs/constrs_1",
    "hw/CMOD_S7 FPGA.srcs/sources_1/bd/design_1/design_1.bd",
]

# Never shown, whatever the roots say.
EXCLUDE_PARTS = {
    ".git", ".venv", "__pycache__", "build", "export", "_ide", "bsp",
    ".Xil", "logs", ".backups",
}
TEXT_SUFFIXES = {
    ".py", ".c", ".h", ".md", ".txt", ".xdc", ".tcl", ".bd", ".yaml",
    ".yml", ".cmake", ".json", ".cfg", ".ini",
}

MAX_READ_LINES = 400             # per read_file call
MAX_SEARCH_HITS = 80
MAX_FILE_BYTES = 400_000
