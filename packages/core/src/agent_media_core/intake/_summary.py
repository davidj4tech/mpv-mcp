"""Optional LLM 'spoken summary' rewrite for the Claude Code Stop hook.

When ``MEDIA_SPEECH_SUMMARY=1``, a long assistant reply is rewritten into a
short, speech-friendly paraphrase before TTS — describing code / tables /
commands in a phrase instead of reading them and dropping URLs and paths —
rather than the mechanical markdown-strip.

It runs in the *detached* playback child (never on the hook's hot path) and
talks to any OpenAI-compatible ``/chat/completions`` endpoint over stdlib
``urllib`` — no SDK or extra interpreter needed. Point it at a local gateway
(e.g. a LiteLLM proxy + local model) via its own ``MEDIA_SUMMARY_BASE_URL`` /
``MEDIA_SUMMARY_API_KEY`` so it never collides with the OpenAI *TTS* fallback's
``OPENAI_BASE_URL`` (which must stay pointed at real OpenAI).

Every failure mode — disabled, too short, no endpoint, HTTP/JSON error,
timeout, empty output — returns ``None`` so the caller keeps the mechanically
-stripped text. It never raises.

Config (env / ~/.config/agent-media.env):
  MEDIA_SPEECH_SUMMARY     "1" to enable (default off)
  MEDIA_SUMMARY_MODEL      chat model (default gpt-4o-mini)
  MEDIA_SUMMARY_BASE_URL   OpenAI-compatible base, incl. /v1 (falls back to
                           OPENAI_BASE_URL, then https://api.openai.com/v1)
  MEDIA_SUMMARY_API_KEY    bearer token; if unset, resolved from the LiteLLM
                           gateway key (LITELLM_MASTER_KEY env, else
                           ~/.config/litellm/litellm.env), else OPENAI_API_KEY —
                           so no key literal is needed in agent-media.env
  MEDIA_SUMMARY_KEY_FILE   override the litellm.env path for key resolution
  MEDIA_SUMMARY_MIN_CHARS  only summarize replies at least this long (default 320)
  MEDIA_SUMMARY_TIMEOUT    request timeout seconds (default 30)
  MEDIA_SUMMARY_PROMPT     override the system prompt

Per-block description (independent of the whole-reply summary above):
  MEDIA_SPEECH_DESCRIBE    "1" to describe un-readable code blocks / tables in
                           one spoken sentence instead of a placeholder (default
                           off). Reuses MEDIA_SUMMARY_MODEL / _BASE_URL / key.
  MEDIA_DESCRIBE_TIMEOUT   per-block request timeout seconds (default 8)
  MEDIA_DESCRIBE_CODE_PROMPT / MEDIA_DESCRIBE_TABLE_PROMPT   prompt overrides
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MIN_CHARS = 320
DEFAULT_TIMEOUT = 30

DEFAULT_PROMPT = (
    "You turn an assistant's chat reply into a short spoken summary for "
    "text-to-speech. Rewrite it as plain spoken prose: 1-3 sentences, no "
    "markdown, no bullet points, no code, no URLs or file paths. Describe any "
    "code, commands, or tables in a brief phrase instead of quoting them. Keep "
    "the key result or answer. Output only the spoken text, nothing else."
)


def summary_enabled() -> bool:
    return os.environ.get("MEDIA_SPEECH_SUMMARY", "0") == "1"


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def summary_min_chars() -> int:
    return _int_env("MEDIA_SUMMARY_MIN_CHARS", DEFAULT_MIN_CHARS)


def _litellm_master_key() -> str:
    """The LiteLLM gateway master key: env, else read from litellm.env (the
    file dotfiles-secrets renders from the sops store). Mirrors the reader in
    venice-mcp / pi-liberator so no key literal is copied into agent-media.env."""
    k = os.environ.get("LITELLM_MASTER_KEY")
    if k:
        return k
    path = (os.environ.get("MEDIA_SUMMARY_KEY_FILE")
            or os.path.expanduser("~/.config/litellm/litellm.env"))
    try:
        with open(path) as fh:
            for line in fh:
                if line.startswith("LITELLM_MASTER_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _resolve_api_key() -> str:
    """Explicit MEDIA_SUMMARY_API_KEY wins; otherwise prefer the gateway master
    key (the usual local-summary target) over OPENAI_API_KEY, which on gateway
    hosts is the real-OpenAI TTS key and would fail auth against the gateway."""
    return (os.environ.get("MEDIA_SUMMARY_API_KEY")
            or _litellm_master_key()
            or os.environ.get("OPENAI_API_KEY") or "")


def _chat(system_prompt: str, user_text: str, timeout: int,
          model: str | None = None) -> str | None:
    """POST one system+user turn to the configured OpenAI-compatible endpoint
    and return the assistant text, or ``None`` on any problem (empty/HTTP/JSON/
    timeout). Never raises. Shared by the summary and describe paths."""
    user_text = (user_text or "").strip()
    if not user_text:
        return None
    base = (os.environ.get("MEDIA_SUMMARY_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL")
            or DEFAULT_BASE_URL).rstrip("/")
    api_key = _resolve_api_key()
    model = model or os.environ.get("MEDIA_SUMMARY_MODEL") or DEFAULT_MODEL

    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": system_prompt},
                     {"role": "user", "content": user_text}],
        "temperature": 0.3,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/chat/completions", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None
    try:
        out = (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        return None
    return out or None


def summarize_for_speech(text: str) -> str | None:
    """Rewrite `text` into a short spoken summary via an OpenAI-compatible chat
    endpoint. Returns the summary, or ``None`` on any problem (caller falls
    back to the mechanically-stripped text). Never raises."""
    prompt = os.environ.get("MEDIA_SUMMARY_PROMPT") or DEFAULT_PROMPT
    return _chat(prompt, text, _int_env("MEDIA_SUMMARY_TIMEOUT", DEFAULT_TIMEOUT))


# --- Optional per-block description (MEDIA_SPEECH_DESCRIBE=1) -----------------
# Unlike the whole-reply summary above, these describe a SINGLE code block or
# table that the mechanical cleaner judged "doesn't read well", so TTS says what
# it is instead of a bare "code block, N lines, omitted" placeholder. Called
# only for those blocks and only when enabled — small/readable blocks are still
# read verbatim with no model call. Falls back to the placeholder on any failure.
DEFAULT_DESCRIBE_TIMEOUT = 8

DESCRIBE_CODE_PROMPT = (
    "You describe a code block for text-to-speech in ONE short spoken sentence. "
    "No markdown, no symbols, do not quote the code. Say what it does or is — "
    "e.g. 'a shell command that force-pushes the main branch'. Output only the "
    "sentence."
)
DESCRIBE_TABLE_PROMPT = (
    "You describe a table for text-to-speech in ONE short spoken sentence. No "
    "markdown, do not read cells verbatim. Say what it contains or compares and "
    "its size — e.g. 'a three-row table comparing hosts by sync status'. Output "
    "only the sentence."
)


def describe_enabled() -> bool:
    return (os.environ.get("MEDIA_SPEECH_DESCRIBE", "0") or "0").strip() == "1"


def _log_describe(kind: str, in_chars: int, secs: float, out: "str | None") -> None:
    """Append one JSONL telemetry line per describe call when MEDIA_DESCRIBE_LOG
    is set (a path). Best-effort: monitoring must never affect playback."""
    path = os.environ.get("MEDIA_DESCRIBE_LOG")
    if not path:
        return
    try:
        import json
        import time as _time
        rec = {"t": round(_time.time()), "kind": kind, "in": in_chars,
               "secs": round(secs, 1), "ok": out is not None, "out": len(out or "")}
        with open(os.path.expanduser(path), "a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:  # noqa: BLE001 — telemetry is never load-bearing
        pass


def _timed_describe(kind: str, prompt: str, text: str) -> str | None:
    import time as _time
    t0 = _time.perf_counter()
    out = _chat(prompt, text, _int_env("MEDIA_DESCRIBE_TIMEOUT", DEFAULT_DESCRIBE_TIMEOUT))
    _log_describe(kind, len(text or ""), _time.perf_counter() - t0, out)
    return out


def describe_code(body: str) -> str | None:
    """One-sentence spoken description of a code block, or ``None`` on failure."""
    prompt = os.environ.get("MEDIA_DESCRIBE_CODE_PROMPT") or DESCRIBE_CODE_PROMPT
    return _timed_describe("code", prompt, body)


def describe_table(table_text: str) -> str | None:
    """One-sentence spoken description of a table, or ``None`` on failure."""
    prompt = os.environ.get("MEDIA_DESCRIBE_TABLE_PROMPT") or DESCRIBE_TABLE_PROMPT
    return _timed_describe("table", prompt, table_text)
