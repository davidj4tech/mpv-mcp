"""Spool + scene state for the visual channel.

The spool is where generated images land (served by the canvas at /img/).
`scenes.json` beside it is the session-continuity memory: the last shaped
scene per session, so the next reply *evolves* the artwork instead of
starting an unrelated picture (see generate.shape_prompt).

Config (env):
  MEDIA_VISUAL_SPOOL_KEEP      newest images kept by gc (default 2000)
  MEDIA_VISUAL_CONTINUITY      "0" disables scene continuity (default on)
  MEDIA_VISUAL_CONTINUITY_TTL  seconds a scene stays alive (default 7200 —
                               walk away for the evening and the canvas
                               starts fresh, not from this morning's scene)
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

# Two thousand rather than two hundred since the transcript started showing a
# reply's picture: at ~10 KB an SVG that is 20 MB for weeks of conversations,
# and a conversation read back later should still have its figures.
DEFAULT_SPOOL_KEEP = 2000
DEFAULT_CONTINUITY_TTL = 7200


def spool_dir() -> Path:
    """Where generated images land: XDG_STATE_HOME/agent-media/visual."""
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    d = root / "agent-media" / "visual"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _int_env(name: str, default: int) -> int:
    try:
        v = int(os.environ.get(name, "") or default)
        return v if v > 0 else default
    except ValueError:
        return default


# --- session-continuity scene memory -----------------------------------------

def continuity_enabled() -> bool:
    return (os.environ.get("MEDIA_VISUAL_CONTINUITY", "1") or "1").strip() != "0"


def continuity_ttl() -> int:
    return _int_env("MEDIA_VISUAL_CONTINUITY_TTL", DEFAULT_CONTINUITY_TTL)


def _scenes_path() -> Path:
    return spool_dir() / "scenes.json"


def _load_scenes() -> dict:
    try:
        with open(_scenes_path()) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def load_scene(session: str) -> str:
    """The session's last shaped scene, or "" when absent/expired/disabled."""
    if not continuity_enabled():
        return ""
    rec = _load_scenes().get(session or "default")
    if not isinstance(rec, dict):
        return ""
    if time.time() - float(rec.get("t") or 0) > continuity_ttl():
        return ""
    return str(rec.get("scene") or "")


def save_scene(session: str, scene: str) -> None:
    """Remember the session's current scene (atomic replace; expired entries
    are pruned on the way through). Best-effort — continuity is never worth
    failing a push over."""
    if not scene:
        return
    try:
        now = time.time()
        ttl = continuity_ttl()
        scenes = {k: v for k, v in _load_scenes().items()
                  if isinstance(v, dict) and now - float(v.get("t") or 0) <= ttl}
        scenes[session or "default"] = {"scene": scene, "t": now}
        tmp = _scenes_path().with_suffix(".json.tmp")
        tmp.write_text(json.dumps(scenes))
        os.replace(tmp, _scenes_path())
    except (OSError, ValueError):
        pass


# --- push memory: what each reply showed, for replays --------------------------
# `pushes.json` beside the spool maps a reply's intake dedup key to the exact
# /show payload its visual pushed (figure or beat sequence). A speech REPLAY
# re-pushes it so "play that again" brings the picture back too — otherwise a
# replayed diagram plays under whatever newer artwork happens to be on screen.

_PUSHES_KEEP = 2000   # matched to DEFAULT_SPOOL_KEEP: the memory of what was pushed is what finds a picture again


def pushes_path() -> Path:
    return spool_dir() / "pushes.json"


def save_push(key: str, payload: dict) -> None:
    """Remember `payload` (as pushed to /show) for the reply keyed `key`.
    Newest _PUSHES_KEEP kept; atomic replace; best-effort."""
    if not key or not payload:
        return
    try:
        p = pushes_path()
        try:
            data = json.loads(p.read_text())
        except (OSError, ValueError):
            data = {}
        data[str(key)] = {"payload": payload, "t": time.time()}
        items = sorted(data.items(),
                       key=lambda kv: kv[1].get("t", 0))[-_PUSHES_KEEP:]
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(items)))
        tmp.replace(p)
    except OSError:
        pass


def load_push(key: str) -> dict | None:
    """The remembered /show payload for `key`, or None."""
    if not key:
        return None
    try:
        data = json.loads(pushes_path().read_text())
    except (OSError, ValueError):
        return None
    return (data.get(str(key)) or {}).get("payload")


# --- spool GC -----------------------------------------------------------------

def gc_spool(keep: int | None = None) -> int:
    """Delete all but the newest `keep` spooled images (default
    MEDIA_VISUAL_SPOOL_KEEP / 200). Returns how many were removed.
    Best-effort — a GC failure must never fail the push that triggered it."""
    if keep is None:
        keep = _int_env("MEDIA_VISUAL_SPOOL_KEEP", DEFAULT_SPOOL_KEEP)
    removed = 0
    try:
        imgs = sorted(spool_dir().glob("img-*"),
                      key=lambda f: f.stat().st_mtime, reverse=True)
        for f in imgs[keep:]:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    except OSError:
        pass
    return removed
