"""Slash commands, and which of them are part of the conversation.

Two paths reach the transcript and they disagreed. A command typed in the
terminal was dropped on the rule that a leading slash is an instruction to the
harness; the same command sent from the phone's reply box was recorded and
spoken, because that path had no such rule — which is where the lone "You: /"
in the archive came from. One rule now, used by both.

Not every command is a turn. `/model` changes the tool and the conversation
reads the same either way. `/code-review` **is** the instruction the next
reply carries out, and without it that reply sits in the chat with no cause
above it — the same gap that made an answer look like a non-sequitur before
questions were recorded. So the split is by what the command does, and an
unknown one counts as work: a project's own commands are the reason any of
this matters, and there is no list of those to check against.

A repeating command is the trap. `/loop` re-submits its prompt on every
wake-up, so recording each firing would bury the conversation in copies of one
instruction. It is recorded the first time and not again.
"""

from __future__ import annotations

import re
from typing import Optional

#: The harness wraps a command it ran itself, rather than sending the line the
#: user typed: `<command-name>/model</command-name><command-args></command-args>`.
_TAG = re.compile(r"<command-(name|args)>(.*?)</command-\1>", re.DOTALL | re.IGNORECASE)

#: A typed command: a name, then whitespace or nothing. The whitespace matters —
#: without it `/home/ryer/notes.md` parses as the command "home", and a path is
#: the commonest thing a message starts a slash with.
_TYPED = re.compile(r"^/([a-zA-Z][a-zA-Z0-9_:-]*)(?:\s+(.*))?$", re.DOTALL)

#: Commands that change the tool rather than the work: they leave no trace in
#: what was being talked about, so they leave none in the transcript either.
#: Everything not named here is treated as work — see the module docstring.
SETTINGS = frozenset({
    "add-dir", "agents", "artifacts", "bug", "clear", "compact", "config",
    "context", "cost", "doctor", "exit", "export", "fast", "feedback", "help",
    "hooks", "ide", "install-github-app", "keybindings", "login", "logout",
    "mcp", "memory", "model", "output-style", "permissions", "plugin",
    "privacy-settings", "quit", "release-notes", "resume", "rewind", "sandbox",
    "statusline", "status", "terminal-setup", "theme", "todos", "upgrade",
    "usage", "vim",
})

#: How far back to look for the same command in this conversation. A loop can
#: fire for hours; a conversation of more than this many spoken rows has other
#: problems.
_SCAN = 400


def parse(prompt: str) -> Optional[dict]:
    """`{name, args, text}` for a slash command, or None for ordinary words.

    Both spellings are read: the tags the harness sends when it ran the command
    itself, and the line as somebody typed it (which is what arrives from the
    phone's reply box, where there is no harness in the middle).
    """
    tagged = {k.lower(): v.strip() for k, v in _TAG.findall(prompt or "")}
    if "name" in tagged and tagged["name"].startswith("/"):
        name = tagged["name"][1:].strip()
        args = tagged.get("args", "")
    else:
        m = _TYPED.match((prompt or "").strip())
        if not m:
            return None
        name, args = m.group(1), (m.group(2) or "")
    name = name.strip()
    if not name:
        return None
    args = " ".join(args.split())
    return {"name": name, "args": args,
            "text": f"/{name} {args}".strip()}


def is_settings(name: str) -> bool:
    return (name or "").strip().lower() in SETTINGS


def already_recorded(session: str, cmd: dict, *, store=None) -> bool:
    """Has this exact command already become a turn in this conversation?

    Keyed on the command and its arguments rather than on time: a loop fires on
    its own schedule, and any window short enough to be useful for a fast one
    is too short for a slow one.
    """
    try:
        from .state.store import StateStore

        rows = (store or StateStore()).recent_history(sink="speech", limit=_SCAN)
    except Exception:  # noqa: BLE001 — a missed dedup is better than no turn
        return False
    for row in rows:
        ex = row.get("extras")
        if not isinstance(ex, dict) or ex.get("source_session") != session:
            continue
        seen = ex.get("command")
        if isinstance(seen, dict) and seen.get("name") == cmd.get("name") \
                and (seen.get("args") or "") == (cmd.get("args") or ""):
            return True
    return False


def turn_for(prompt: str, session: str, *, store=None) -> Optional[dict]:
    """What a slash command should become, or None to leave it out.

    None covers all three reasons: it is not a command, it is a settings
    command, or this conversation has already recorded it.
    """
    cmd = parse(prompt)
    if cmd is None or is_settings(cmd["name"]):
        return None
    if already_recorded(session, cmd, store=store):
        return None
    return cmd
