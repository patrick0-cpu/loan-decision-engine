"""One small client for the school's Ollama-compatible LLM server. Every LLM step uses it.

Rules it enforces (CLAUDE2.md rules 12, 13 and 15):
- calls go only to LLM_BASE_URL, with temperature 0 and num_ctx from .env;
- one retry on a timeout or connection error, then a clear LLMError;
- every call, successful or not, writes an audit_log row with actor "model" (model name,
  purpose, duration, tool calls), never the API key or image contents;
- the key is never printed, logged, or included in an error message.
"""
import base64
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import requests
from dotenv import load_dotenv
from sqlalchemy.orm import Session

from db import PROJECT_ROOT, get_engine, log

RETRYABLE = (requests.Timeout, requests.ConnectionError)


class LLMError(RuntimeError):
    """The LLM could not be reached or returned an unusable response."""


@dataclass
class LLMResponse:
    content: str
    thinking: str
    tool_calls: list[dict]
    model: str
    duration_seconds: float
    prompt_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    message: dict = field(default_factory=dict)  # the assistant message, to append to history


def settings() -> dict[str, Any]:
    load_dotenv(PROJECT_ROOT / ".env")
    base = (os.getenv("LLM_BASE_URL") or "").strip().rstrip("/")
    # Accept a URL that already names an endpoint (e.g. .../api/generate): keep the server root.
    base = re.sub(r"/api(/.*)?$", "", base)
    return {
        "base_url": base,
        "api_key": os.getenv("LLM_API_KEY") or "",
        "model": os.getenv("LLM_MODEL") or "",
        "model_final": os.getenv("LLM_MODEL_FINAL") or "",
        "model_active": (os.getenv("LLM_MODEL_ACTIVE") or os.getenv("LLM_MODEL") or ""),
        "num_ctx": int(os.getenv("LLM_NUM_CTX") or 32768),
        "timeout": float(os.getenv("LLM_TIMEOUT_SECONDS") or 120),
    }


def active_model() -> str:
    """The model the dashboard uses: LLM_MODEL_ACTIVE, else LLM_MODEL."""
    return settings()["model_active"]


def _headers(cfg: dict) -> dict:
    return {"Authorization": f"Bearer {cfg['api_key']}"} if cfg["api_key"] else {}


def _clean(text: str, cfg: dict) -> str:
    """Remove the key from any text that might reach a log or the screen."""
    return text.replace(cfg["api_key"], "<hidden>") if cfg["api_key"] else text


def available(timeout: float = 5) -> bool:
    """True if the server answers /api/tags. Used to fall back gracefully when it doesn't."""
    cfg = settings()
    if not cfg["base_url"]:
        return False
    try:
        return requests.get(f"{cfg['base_url']}/api/tags", headers=_headers(cfg),
                            timeout=timeout).ok
    except requests.RequestException:
        return False


def list_models() -> list[str]:
    cfg = settings()
    r = requests.get(f"{cfg['base_url']}/api/tags", headers=_headers(cfg), timeout=30)
    r.raise_for_status()
    return [m.get("name") for m in r.json().get("models", [])]


def encode_image(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _write_audit(session: Optional[Session], application_id, step, purpose, details) -> None:
    if session is not None:
        log(session, step, "model", f"llm call: {purpose}", application_id, **details)
        return
    with Session(get_engine()) as s:  # no caller transaction: record the call on its own
        log(s, step, "model", f"llm call: {purpose}", application_id, **details)
        s.commit()


def chat(messages: list[dict], *, purpose: str, step: str = "llm",
         model: Optional[str] = None, images: Optional[list[bytes]] = None,
         schema: Optional[dict] = None, tools: Optional[list[dict]] = None,
         think: bool = False, session: Optional[Session] = None,
         application_id: Optional[int] = None, tool_calls_so_far: int = 0,
         timeout: Optional[float] = None) -> LLMResponse:
    """Send one chat request and return the assistant's reply.

    images are attached to the last user message (PNG/JPG bytes). schema is a JSON schema the
    reply must follow (Ollama `format`). tools are Ollama tool definitions. The audit row goes
    into `session` if given (the caller commits), otherwise it is committed immediately.
    """
    cfg = settings()
    model = model or cfg["model_active"]
    if not cfg["base_url"] or not model:
        raise LLMError("LLM_BASE_URL and LLM_MODEL must be set in .env.")

    messages = [dict(m) for m in messages]
    if images:
        for m in reversed(messages):
            if m["role"] == "user":
                m["images"] = [encode_image(b) for b in images]
                break
    payload: dict[str, Any] = {
        "model": model, "messages": messages, "stream": False, "think": think,
        "options": {"temperature": 0, "num_ctx": cfg["num_ctx"]},
    }
    if schema is not None:
        payload["format"] = schema
    if tools:
        payload["tools"] = tools

    details: dict[str, Any] = {"model": model, "purpose": purpose, "think": think,
                               "images": len(images or []), "tools_offered": len(tools or []),
                               "tool_calls_so_far": tool_calls_so_far}
    started = time.monotonic()
    error: Optional[str] = None
    body: dict = {}
    for attempt in (1, 2):
        try:
            r = requests.post(f"{cfg['base_url']}/api/chat", json=payload, headers=_headers(cfg),
                              timeout=timeout or cfg["timeout"])
            if not r.ok:
                error = f"server returned HTTP {r.status_code}: {_clean(r.text[:300], cfg)}"
                break
            body = r.json()
            error = None
            break
        except RETRYABLE as exc:
            error = f"{type(exc).__name__}: {_clean(str(exc), cfg)[:300]}"
            details["retried"] = True
            continue
        except (requests.RequestException, ValueError) as exc:
            error = f"{type(exc).__name__}: {_clean(str(exc), cfg)[:300]}"
            break
    duration = round(time.monotonic() - started, 2)
    details["duration_seconds"] = duration

    if error is not None:
        details["error"] = error
        _write_audit(session, application_id, step, purpose, details)
        raise LLMError(f"LLM call failed ({purpose}, model {model}): {error}")

    message = body.get("message") or {}
    tool_calls = message.get("tool_calls") or []
    details.update(tool_calls_returned=len(tool_calls),
                   tool_names=[c.get("function", {}).get("name") for c in tool_calls],
                   prompt_tokens=body.get("prompt_eval_count"),
                   output_tokens=body.get("eval_count"))
    _write_audit(session, application_id, step, purpose, details)
    return LLMResponse(content=message.get("content") or "", thinking=message.get("thinking") or "",
                       tool_calls=tool_calls, model=body.get("model", model),
                       duration_seconds=duration, prompt_tokens=body.get("prompt_eval_count"),
                       output_tokens=body.get("eval_count"), message=message)
