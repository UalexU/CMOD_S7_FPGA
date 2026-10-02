"""
Minimal Ollama HTTP client -- standard library only, so the assistant adds
nothing to requirements.txt.

Only two endpoints are needed:
    GET  /api/tags   installed models (for the model picker)
    POST /api/chat   streaming chat with tool calling
"""

import json
import urllib.error
import urllib.request

from . import config


class OllamaError(RuntimeError):
    pass


def _url(path):
    return f"{config.OLLAMA_URL}{path}"


def list_models(timeout=5):
    """Names of the installed models, e.g. ['qwen3-coder:30b', ...]."""
    try:
        with urllib.request.urlopen(_url("/api/tags"), timeout=timeout) as r:
            data = json.load(r)
    except (urllib.error.URLError, OSError) as e:
        raise OllamaError(
            f"Cannot reach Ollama at {config.OLLAMA_URL} -- is it running?"
            f"\n({e})") from e
    return sorted(m["name"] for m in data.get("models", []))


def chat_stream(model, messages, tools=None, options=None, should_stop=None):
    """Yield the streamed chunks of one /api/chat call.

    Each chunk is a dict with (any of) 'content', 'thinking', 'tool_calls'
    and a final 'done': True. `should_stop` is polled between chunks so the
    Stop button takes effect mid-answer.
    """
    body = {
        "model": model,
        "messages": messages,
        "stream": True,
        "options": options or {},
    }
    if tools:
        body["tools"] = tools

    req = urllib.request.Request(
        _url("/api/chat"),
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=config.REQUEST_TIMEOUT_S)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        if "does not support tools" in detail:
            raise OllamaError(
                f"{model} does not support tool calling, so it cannot read "
                "the project. Pick qwen3-coder or gpt-oss.") from e
        raise OllamaError(f"Ollama returned {e.code}: {detail}") from e
    except (urllib.error.URLError, OSError) as e:
        raise OllamaError(
            f"Cannot reach Ollama at {config.OLLAMA_URL} -- is it running?"
            f"\n({e})") from e

    with resp:
        for raw in resp:
            if should_stop and should_stop():
                return
            line = raw.strip()
            if not line:
                continue
            data = json.loads(line)
            if "error" in data:
                raise OllamaError(data["error"])
            msg = data.get("message") or {}
            yield {
                "content": msg.get("content") or "",
                "thinking": msg.get("thinking") or "",
                "tool_calls": msg.get("tool_calls") or [],
                "done": bool(data.get("done")),
            }
