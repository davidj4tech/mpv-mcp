"""media-setup — install hooks and services for agent-media.

Wires the Python intake adapters into Claude Code, Codex, OpenCode,
and (on Termux) the runit service tree. Replaces the manual
settings.json paste in the legacy audio-relay README.

Subcommands:
  media-setup check                     Verify prereq binaries.
  media-setup install-hooks [--dry-run] Merge hook entries into
                                        ~/.claude/settings.json.
  media-setup install-services [--dry-run]
                                        Install services for this host.
                                        Auto-detects runit (Termux /
                                        host-runit) vs systemd --user
                                        (regular Linux); override with
                                        --backend.
  media-setup install-shell [--dry-run] Symlink the tmux popup launcher +
                                        control surface onto PATH
                                        (~/.local/bin, ~/.local/share).
  media-setup status                    Summarize current wiring.

Everything is idempotent. The settings.json writer makes a `.bak` copy
before touching the live file, and only rewrites if the merged content
differs.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import sysconfig
from pathlib import Path
from typing import Iterable


# --- Hook wiring -----------------------------------------------------------

CLAUDE_HOOK_COMMAND = "media-hook-claude-code"
# Substrings that mark "this entry is OUR hook" (current or legacy).
HOOK_MATCH_SUBSTRINGS = (
    "media-hook-claude-code",
    "claude-code-tts-hook",
)
CLAUDE_HOOK_TIMEOUT = 30
# UserPromptSubmit records what the listener typed, so the transcript on the
# shelf has the questions as well as the answers.
CLAUDE_HOOK_EVENTS = ("Stop", "Notification", "UserPromptSubmit")


def claude_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise SystemExit(f"media-setup: failed to read {path}: {e}") from None


def _merge_hooks(settings: dict, command: str) -> tuple[dict, bool]:
    """Return (new_settings, changed). Idempotent: re-runs leave the
    file untouched if our entries are already current.
    """
    settings = json.loads(json.dumps(settings))  # deep copy
    hooks = settings.setdefault("hooks", {})
    changed = False

    target_entry = {
        "type": "command",
        "command": command,
        "timeout": CLAUDE_HOOK_TIMEOUT,
    }

    for event in CLAUDE_HOOK_EVENTS:
        groups = hooks.setdefault(event, [])
        # Look for an existing group containing one of our hooks and
        # rewrite it; otherwise append a new group.
        replaced = False
        for group in groups:
            inner = group.get("hooks") or []
            for i, h in enumerate(inner):
                cmd = (h.get("command") or "")
                if any(s in cmd for s in HOOK_MATCH_SUBSTRINGS):
                    if (h.get("command") != command
                            or h.get("timeout") != CLAUDE_HOOK_TIMEOUT
                            or h.get("type") != "command"):
                        inner[i] = target_entry
                        changed = True
                    replaced = True
                    break
            if replaced:
                break
        if not replaced:
            groups.append({"hooks": [target_entry]})
            changed = True

    return settings, changed


def cmd_install_hooks(args: argparse.Namespace) -> int:
    path = Path(args.settings) if args.settings else claude_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    current = _load_json(path)
    merged, changed = _merge_hooks(current, args.command)
    if not changed:
        print(f"media-setup: {path} already up to date")
        return 0
    rendered = json.dumps(merged, indent=2) + "\n"
    if args.dry_run:
        print(f"# would write {path}:")
        print(rendered)
        return 0
    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        try:
            shutil.copy2(str(path), str(backup))
        except OSError as e:
            raise SystemExit(f"media-setup: backup to {backup} failed: {e}")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(rendered)
    tmp.replace(path)
    print(f"media-setup: wrote {path} (backup at {path}.bak)")
    return 0


# --- Env-var migration -----------------------------------------------------

# Per RESTRUCTURE.md. Empty new-name means "drop the variable".
ENV_RENAME = {
    "CLAUDE_TTS_ENGINE":          "MEDIA_RENDER_ENGINE",
    "CLAUDE_TTS_VOICE":           "MEDIA_RENDER_VOICE",
    "CLAUDE_TTS_EDGE_VOICE":      "MEDIA_EDGE_VOICE",
    "CLAUDE_TTS_OPENAI_MODEL":    "MEDIA_OPENAI_MODEL",
    "CLAUDE_TTS_OPENAI_PYTHON":   "MEDIA_OPENAI_PYTHON",
    "CLAUDE_TTS_REALTIME_PYTHON": "MEDIA_REALTIME_PYTHON",
    "CLAUDE_TTS_DROP_DIR":        "MEDIA_DROP_DIR",
    "CLAUDE_TTS_ENABLED":         "MEDIA_ENABLED",
    "CLAUDE_TTS_LONG_THRESHOLD":  "",   # retired (single stream path)
    "AAR_STREAM_HOST":            "MEDIA_STREAM_HOST",
    "AAR_MOPIDY_DUCK_VOLUME":     "MEDIA_DUCK_VOLUME",
    "RELAY_TTS_DROP_BIN":         "",   # retired
    "RELAY_TTS_STREAM_BIN":       "",   # retired
    "RELAY_LOG_FILE":             "MEDIA_LOG_FILE",
    "RELAY_ENV_FILE":             "MEDIA_ENV_FILE",
}


def _rename_in_json_env(env: dict) -> tuple[dict, list[tuple[str, str | None]]]:
    """Apply ENV_RENAME to a flat dict. Returns (new_env, [(old, new), ...]).

    new=None for retired vars (dropped).
    """
    changes: list[tuple[str, str | None]] = []
    out = dict(env)
    for old, new in ENV_RENAME.items():
        if old in out:
            value = out.pop(old)
            if new:
                # Don't clobber a manually-set new-name entry.
                if new not in out:
                    out[new] = value
                changes.append((old, new))
            else:
                changes.append((old, None))
    return out, changes


def cmd_migrate_env(args: argparse.Namespace) -> int:
    """Rename CLAUDE_TTS_*/AAR_*/RELAY_* envs to MEDIA_* in the user's
    settings.json and (if it exists) ~/.config/agent-audio-relay.env.
    """
    paths: list[tuple[str, Path]] = []

    settings_path = Path(args.settings) if args.settings else claude_settings_path()
    if settings_path.exists():
        paths.append(("settings.json", settings_path))

    relay_env = Path.home() / ".config" / "agent-audio-relay.env"
    if relay_env.exists():
        paths.append(("agent-audio-relay.env", relay_env))

    if not paths:
        print("media-setup: nothing to migrate (no settings.json or "
              "agent-audio-relay.env)")
        return 0

    any_changed = False

    for label, path in paths:
        if path.suffix == ".json" or label == "settings.json":
            data = _load_json(path)
            env = data.get("env") or {}
            new_env, changes = _rename_in_json_env(env)
            if not changes:
                print(f"  {label}: nothing to change")
                continue
            for old, new in changes:
                arrow = f"-> {new}" if new else "-> (dropped)"
                print(f"  {label}: {old} {arrow}")
            if not args.dry_run:
                if path.exists():
                    backup = path.with_suffix(path.suffix + ".bak")
                    shutil.copy2(str(path), str(backup))
                data["env"] = new_env
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_text(json.dumps(data, indent=2) + "\n")
                tmp.replace(path)
                print(f"  {label}: wrote {path} (backup at {backup})")
            any_changed = True
            continue

        # Shell-style env file: simple line-by-line `KEY=VALUE` or
        # `export KEY=VALUE`. Lines we don't recognize are preserved
        # verbatim.
        new_lines: list[str] = []
        changes: list[tuple[str, str | None]] = []
        for raw in path.read_text().splitlines(keepends=True):
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                new_lines.append(raw)
                continue
            line = stripped
            prefix = ""
            if line.startswith("export "):
                prefix = "export "
                line = line[len("export "):]
            if "=" not in line:
                new_lines.append(raw)
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k in ENV_RENAME:
                new = ENV_RENAME[k]
                if new:
                    new_lines.append(f"{prefix}{new}={v}\n")
                    changes.append((k, new))
                else:
                    changes.append((k, None))
                continue
            new_lines.append(raw)
        if not changes:
            print(f"  {label}: nothing to change")
            continue
        for old, new in changes:
            arrow = f"-> {new}" if new else "-> (dropped)"
            print(f"  {label}: {old} {arrow}")
        if not args.dry_run:
            backup = path.with_suffix(path.suffix + ".bak")
            shutil.copy2(str(path), str(backup))
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text("".join(new_lines))
            tmp.replace(path)
            print(f"  {label}: wrote {path} (backup at {backup})")
        any_changed = True

    if args.dry_run:
        print("\n(dry-run — no files written)")
    return 0


# --- Service wiring (Termux runit + systemd --user) ------------------------

def _service_backend(explicit: str | None) -> str:
    """Pick the service supervisor for this host: 'runit' or 'systemd'.

    'auto' (the default) prefers runit whenever a runit service root is
    present (Termux, or host-runit at /etc/service), since those hosts
    are supervised by runsvdir; otherwise falls back to systemd --user
    when `systemctl` is available.
    """
    if explicit and explicit != "auto":
        return explicit
    if services_dir() is not None:
        return "runit"
    if shutil.which("systemctl") is not None:
        return "systemd"
    return "runit"  # last resort; cmd will surface the missing root


def services_dir() -> Path | None:
    """Where runit looks for services on this host, or None when we
    can't infer (e.g. non-Termux Linux with systemd).

    Detection order:
      1. $PREFIX (Termux-native shells)
      2. /data/data/com.termux/files/usr/var/service exists (Termux,
         even when invoked from inside a proot where $PREFIX isn't set)
      3. /etc/service (host-runit on regular Linux)
    """
    prefix = os.environ.get("PREFIX")
    if prefix and prefix.startswith("/data/data/com.termux"):
        return Path(prefix) / "var" / "service"
    termux_sv = Path("/data/data/com.termux/files/usr/var/service")
    if termux_sv.is_dir():
        return termux_sv
    candidate = Path("/etc/service")
    return candidate if candidate.is_dir() else None


def _data_dir(name: str) -> Path:
    """A shipped data directory, wherever this install keeps it.

    Two layouts, and both are real. In the repo these sit beside the package
    (`packages/core/services`); in a wheel they are force-included *inside* it
    (`agent_media_core/services`). Installed-first, because that is the layout
    a stranger has and the one that used to be missing entirely — a wheel
    carried no `services/` at all, so `install-services` found no templates and
    reported success having installed nothing.
    """
    inside = Path(__file__).resolve().parent / name
    if inside.is_dir():
        return inside
    return Path(__file__).resolve().parent.parent.parent / name


def shipped_bin(name: str) -> Path:
    """A shell helper shipped with the package, by name.

    Same two layouts as the service templates, and the same reason they are a
    problem: beside the package in a checkout, inside it in a wheel. A console
    script is the only thing that should ever look this up — the helper itself
    is plain sh, so the entrypoint exists to put it on PATH, not to wrap it.
    """
    return _data_dir("bin") / name


def service_templates_dir() -> Path:
    """Repo-shipped templates under packages/core/services/."""
    return _data_dir("services")


def service_template_names() -> list[str]:
    """Installable service names under services/.

    Underscore-prefixed directories are shared assets, not services —
    `_common/` holds crash-notify, has no `run`, and installing it fails with
    "template missing", which took the whole `install-services` run down with
    it (and with it every ansible rollout of the audio_server role).
    """
    templates = service_templates_dir()
    if not templates.is_dir():
        return []
    return sorted(p.name for p in templates.iterdir()
                  if p.is_dir() and not p.name.startswith("_"))


# --- Host roles: which services belong on THIS machine ---------------------
#
# `install-services` used to install every directory under services/, with the
# only distinction being supervisor type. That is how the phone ended up
# running `call-hold-consumer` — the house-side flag watcher — alongside its
# own `call-guard`, two processes pausing the same speech socket with
# independent debounce timers, which broke barge-in intermittently for a
# fortnight before anyone noticed. red5 has the mirror-image problem: a
# `call-guard` unit it has no mic for, sitting installed and disabled.
#
# The fix is to say what a host *is* rather than what supervisor it runs:
#
#   observe   has a mic or a dialer worth watching  (the phone)
#   render    has audio sinks that can be silenced  (phone, red5, the TV)
#   origin    produces the text to be spoken        (red5 today)
#
# A host that both observes and renders needs one `call-guard`: it detects and
# pauses its own sinks directly. A host that only renders needs the same binary
# with detection switched off. So the two are never both correct, which is why
# a service declares `conflicts:` and not merely `requires:` — plain overlap
# would put the consumer back on the phone, since the phone does render.

ROLES_ENV = "MEDIA_ROLES"


def host_roles_path() -> Path:
    return Path.home() / ".config" / "agent-media-roles"


def _parse_roles(text: str) -> set[str]:
    """Roles from a comma- or newline-separated list, `#` comments allowed."""
    out: set[str] = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0]
        for part in line.replace(",", " ").split():
            out.add(part.strip().lower())
    return out


def host_roles() -> set[str] | None:
    """This host's declared roles, or None when nothing declares any.

    Delegates to `config.host_roles`, which reads MEDIA_ROLES, then
    `[host] roles` in config.toml, then the original plain roles file. Kept as
    a name here because the installer and its tests are the main caller.

    None is not the empty set and the difference is the whole safety argument:
    unconfigured means "filter nothing", so an existing rollout that has never
    heard of roles keeps installing exactly what it installed before. Only a
    host that opts in gets filtered. An empty *declaration* is still a
    declaration and does filter — that is how you say "install only the
    services that make no demands".
    """
    from .config import host_roles as _roles
    return _roles()


def service_config_gate(name: str) -> str:
    """The config file `services/<name>/roles` says this service needs, or "".

    Roles say what a host *is* — a mic, some speakers, a place replies come
    from — and that vocabulary is deliberately three words long. "Has an
    Audiobookshelf to talk to" is not a property of the host in that sense; it
    is a thing somebody configured. Expressing it as a fourth role would put a
    machine's software inventory into a list that describes its hardware.

    So an optional integration declares the file that proves it was set up:

        requires-config: abs-bridge.env

    resolved under ~/.config/agent-media/. Absent, the service is skipped with
    a reason — never installed-and-idle, which is how a host ends up running a
    daemon that logs "not configured" forever.
    """
    try:
        text = (service_templates_dir() / name / "roles").read_text()
    except OSError:
        return ""
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        key, _, rest = line.partition(":")
        if key.strip().lower() == "requires-config" and rest.strip():
            return rest.strip()
    return ""


def service_env_gate(name: str) -> str:
    """The environment variable `services/<name>/roles` says this needs, or "".

    The sibling of `requires-config:`, for settings that live in
    `agent-media.env` rather than in a file of their own:

        requires-env: MEDIA_FEED_BASE_URL

    Same reasoning — "is this switched on here" is not a property of the
    hardware and does not belong in the role vocabulary — and the same
    outcome: skipped with a reason, rather than installed and idle.
    """
    try:
        text = (service_templates_dir() / name / "roles").read_text()
    except OSError:
        return ""
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        key, _, rest = line.partition(":")
        if key.strip().lower() == "requires-env" and rest.strip():
            return rest.strip()
    return ""


def _env_file_has(key: str) -> bool:
    """Is `key` set in agent-media.env, with a value?

    The file, not just `os.environ`: the installer is run from a login shell
    that has never sourced it, while every *service* it installs receives it
    via `EnvironmentFile=`. Reading only the process environment would skip a
    service on the very host where it is configured.
    """
    if (os.environ.get(key) or "").strip():
        return True
    try:
        text = _agent_media_env_path().read_text()
    except OSError:
        return False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == key:
            return bool(v.strip().strip('"').strip("'"))
    return False


def service_roles(name: str) -> tuple[set[str], set[str]]:
    """(requires, conflicts) declared by services/<name>/roles.

    Both empty when the file is absent or unreadable, which means "belongs
    anywhere" — the same fail-open default that keeps undeclared services
    installing as they always did.
    """
    requires: set[str] = set()
    conflicts: set[str] = set()
    try:
        text = (service_templates_dir() / name / "roles").read_text()
    except OSError:
        return requires, conflicts
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        key, _, rest = line.partition(":")
        key = key.strip().lower()
        if key == "requires":
            requires |= _parse_roles(rest)
        elif key == "conflicts":
            conflicts |= _parse_roles(rest)
    return requires, conflicts


def service_wanted(name: str, roles: set[str] | None) -> tuple[bool, str]:
    """Should `name` be installed on a host with `roles`? Plus a reason.

    The reason is returned rather than logged here so the caller can print one
    line per skip. A silent skip would be its own bug: the failure this whole
    mechanism exists to prevent was invisible, and "nothing happened" is what
    it looked like from the outside.
    """
    # Checked before roles, and regardless of whether any are declared: an
    # unconfigured integration is unwanted on a host that declares nothing at
    # all, which is exactly the host most likely to be a fresh install.
    gate = service_config_gate(name)
    if gate and not (Path.home() / ".config" / "agent-media" / gate).exists():
        return False, f"needs ~/.config/agent-media/{gate}"
    env_gate = service_env_gate(name)
    if env_gate and not _env_file_has(env_gate):
        return False, f"needs {env_gate} in agent-media.env"
    if roles is None:
        return True, "no host roles declared"
    requires, conflicts = service_roles(name)
    if not requires and not conflicts:
        return True, "service declares no roles"
    missing = requires - roles
    if missing:
        return False, f"needs role {'+'.join(sorted(missing))}"
    clash = conflicts & roles
    if clash:
        return False, f"conflicts with role {'+'.join(sorted(clash))}"
    return True, "roles match"


def tmux_dir() -> Path:
    """Repo-shipped tmux integration under packages/core/tmux/."""
    return _data_dir("tmux")


def local_bin() -> Path:
    return Path.home() / ".local" / "bin"


def media_share_dir() -> Path:
    return Path.home() / ".local" / "share" / "agent-media"


def _symlink_into(src: Path, dest: Path, *, dry_run: bool) -> bool:
    """Idempotently symlink ``dest -> src``.

    Replaces a stale symlink of ours, and auto-converts a stale *real file*
    (typically a copy from an older install) into a symlink: dropped if it
    matches ``src``, else backed up first so no local edit is lost. A real
    directory or anything else unexpected is left alone.
    """
    if dest.is_symlink():
        if dest.resolve() == src.resolve():
            print(f"media-setup: {dest.name} already linked")
            return True
        if dry_run:
            print(f"# would relink {dest} -> {src}")
            return True
        dest.unlink()
    elif dest.is_file():
        if dry_run:
            print(f"# would convert real file {dest} -> symlink {src}")
            return True
        if dest.read_bytes() == src.read_bytes():
            dest.unlink()
            print(f"media-setup: {dest.name}: replaced identical real file "
                  f"with symlink")
        else:
            backup = _backup_aside(dest, "shell-backups", dest.name)
            print(f"media-setup: {dest.name}: real file differed from source; "
                  f"backed up to {backup} before relinking", file=sys.stderr)
    elif dest.exists():
        # A real directory (or other non-file) at the link path — not ours.
        print(f"media-setup: {dest} exists and is not our symlink; leaving it",
              file=sys.stderr)
        return False
    if dry_run:
        print(f"# would symlink {dest} -> {src}")
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.symlink_to(src)
    print(f"media-setup: linked {dest} -> {src}")
    return True


def _service_dir_matches_template(dest: Path, src: Path) -> bool:
    """True if every file shipped in the template ``src`` exists in ``dest``
    with identical content.

    Extra files in ``dest`` are ignored — a live runit dir carries runtime
    state (``supervise/``, ``log/`` output) that the template never has. We
    only care that nothing the repo manages was hand-edited locally.
    """
    for tpl in src.rglob("*"):
        if not tpl.is_file():
            continue
        live = dest / tpl.relative_to(src)
        if not live.is_file() or live.read_bytes() != tpl.read_bytes():
            return False
    return True


def _backup_aside(path: Path, category: str, name: str) -> Path:
    """Move ``path`` into ``media_share_dir()/category/name`` before we replace
    it with a symlink, so nothing local is lost. The backup lands OUTSIDE both
    the service root (runsvdir scans ``root/*`` and would otherwise supervise a
    backed-up service) and ~/.local/bin (so a backed-up script isn't on PATH).
    A numeric suffix is appended if the target already exists.
    """
    backups = media_share_dir() / category
    backups.mkdir(parents=True, exist_ok=True)
    dest = backups / name
    n = 1
    while dest.exists():
        dest = backups / f"{name}.{n}"
        n += 1
    shutil.move(str(path), str(dest))
    return dest


def _install_one_service(name: str, *, dry_run: bool,
                         root: Path) -> bool:
    """Symlink/copy the template tree into the runit service root.

    Termux's runsvdir scans `service_dir/*` for `run` files. We use
    symlinks so a `git pull` on the repo picks up service edits without
    a re-install.

    A pre-existing *real* directory (typically a stale copy-based install)
    is auto-converted to a symlink: if its tracked files match the template
    we just drop it, otherwise we back it up first so no local edit is lost.
    """
    src = service_templates_dir() / name
    if not src.is_dir():
        print(f"media-setup: template missing: {src}", file=sys.stderr)
        return False
    dest = root / name
    if dest.is_symlink():
        if dest.resolve() == src.resolve():
            print(f"media-setup: {name} already installed")
            return True
        # Our symlink, but pointing elsewhere (e.g. an old repo path): relink.
        if dry_run:
            print(f"# would relink {dest} -> {src}")
            return True
        dest.unlink()
    elif dest.is_dir():
        # A real directory. Convert it to a symlink iff it's recognizably one
        # of our service dirs (has a `run` script); never touch a stranger.
        if not (dest / "run").is_file():
            print(f"media-setup: {dest} exists and is not a service dir; "
                  f"leaving it", file=sys.stderr)
            return False
        if dry_run:
            print(f"# would convert real dir {dest} -> symlink {src}")
            return True
        if _service_dir_matches_template(dest, src):
            shutil.rmtree(dest)
            print(f"media-setup: {name}: replaced identical real dir "
                  f"with symlink")
        else:
            backup = _backup_aside(dest, "service-backups", name)
            print(f"media-setup: {name}: real dir differed from template; "
                  f"backed up to {backup} before relinking", file=sys.stderr)
    elif dest.exists():
        # A real non-directory file sitting at the service path — not ours.
        print(f"media-setup: {dest} exists and is not our symlink; leaving it",
              file=sys.stderr)
        return False
    elif dry_run:
        print(f"# would symlink {dest} -> {src}")
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.symlink_to(src)
    print(f"media-setup: installed {dest} -> {src}")
    return True


# systemd --user backend. We don't translate the run scripts into native
# ExecStart lines — we point ExecStart at the very same `run` script so
# the mpv flags / MCP bind logic stay in one place across both backends.
# The shebang (Termux sh) is bypassed by invoking via `/bin/sh`.

SYSTEMD_UNIT_TEMPLATE = """\
[Unit]
Description=agent-media {name}
PartOf=default.target

[Service]
Type=simple
EnvironmentFile=-%h/.config/agent-media.env
Environment=PATH={bindir}:/usr/local/bin:/usr/bin:/bin
ExecStart=/bin/sh {runscript}
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
"""


# A template that carries a `timer` file is periodic rather than long-running:
# the same run script, started by a timer, exiting when it is done. `Restart`
# would be wrong here — a oneshot that failed should wait for its next window,
# not spin.
SYSTEMD_ONESHOT_TEMPLATE = """\
[Unit]
Description=agent-media {name}

[Service]
Type=oneshot
EnvironmentFile=-%h/.config/agent-media.env
Environment=PATH={bindir}:/usr/local/bin:/usr/bin:/bin
ExecStart=/bin/sh {runscript}
"""

SYSTEMD_TIMER_TEMPLATE = """\
[Unit]
Description=agent-media {name} schedule

[Timer]
{schedule}
Persistent=true

[Install]
WantedBy=timers.target
"""


def systemd_user_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "systemd" / "user"


def _entrypoint_bindir() -> Path:
    """Console-script dir of this install — so a venv install's
    `media-mcp-http` resolves on the unit's PATH without the user having
    it on their login PATH. Uses sysconfig (not `sys.executable`, whose
    venv symlink resolves back to the base interpreter's bin).
    """
    return Path(sysconfig.get_path("scripts"))


def _systemd_unit_name(name: str) -> str:
    """Map a template dir name to a namespaced unit file name.

    Collapses a redundant leading `media-` so `media-mcp` becomes
    `agent-media-mcp.service`, while `sink-speech` becomes
    `agent-media-sink-speech.service`.
    """
    stem = name[len("media-"):] if name.startswith("media-") else name
    return f"agent-media-{stem}.service"


def _systemctl_user(*argv: str) -> int:
    return subprocess.call(["systemctl", "--user", *argv])


def _install_one_systemd(name: str, *, dry_run: bool, root: Path) -> str | None:
    """Write a systemd --user unit for `name`. Returns the unit file
    name on success (so the caller can enable it), or None on failure.
    """
    src = service_templates_dir() / name
    run = src / "run"
    if not run.is_file():
        print(f"media-setup: template missing: {run}", file=sys.stderr)
        return None
    unit = _systemd_unit_name(name)
    dest = root / unit
    # `timer` holds the [Timer] body — OnCalendar=, OnUnitActiveSec=, whatever
    # this job's cadence is. Its presence is what makes the service oneshot,
    # so the two can never disagree about which kind of thing this is.
    schedule = (src / "timer").read_text().strip() if (src / "timer").is_file() else ""
    template = SYSTEMD_ONESHOT_TEMPLATE if schedule else SYSTEMD_UNIT_TEMPLATE
    content = template.format(
        name=name, bindir=_entrypoint_bindir(), runscript=run)
    if dry_run:
        if dest.is_symlink():
            print(f"# would replace symlink {dest} -> {os.readlink(dest)} "
                  f"with a generated unit")
        print(f"# would write {dest}:")
        print(content)
        if schedule:
            print(f"# would write {root / (unit.removesuffix('.service') + '.timer')}"
                  f" ({schedule})")
            return unit.removesuffix(".service") + ".timer"
        return unit
    # A stale symlink here is typically an older stow/dotfiles-deployed unit.
    # Unlink it first so we write a real file rather than following the link and
    # clobbering its target (a tracked source file). A dangling symlink is
    # handled the same way — writing through it would recreate the deleted
    # target.
    if dest.is_symlink():
        print(f"media-setup: {unit}: replacing stale symlink "
              f"(-> {os.readlink(dest)}) with a generated unit")
        dest.unlink()
    if dest.exists() and dest.read_text() == content:
        # Not a return: a periodic service whose schedule changed has an
        # identical unit file and a different timer, and returning here left
        # the old schedule installed while reporting success. Found by
        # shortening a cadence and watching it not change.
        print(f"media-setup: {unit} already up to date")
    else:
        root.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)
        print(f"media-setup: wrote {dest}")
    if schedule:
        # The timer is what gets enabled; enabling the oneshot service itself
        # would run it once at boot and never again, which looks like a
        # working schedule for exactly one day.
        tpath = root / (unit.removesuffix(".service") + ".timer")
        tcontent = SYSTEMD_TIMER_TEMPLATE.format(name=name, schedule=schedule)
        if not (tpath.exists() and tpath.read_text() == tcontent):
            tpath.write_text(tcontent)
            print(f"media-setup: wrote {tpath}")
        return tpath.name
    return unit


def _install_services_systemd(args: argparse.Namespace,
                              names: list[str]) -> int:
    root = systemd_user_dir()
    units: list[str] = []
    ok = True
    for name in names:
        unit = _install_one_systemd(name, dry_run=args.dry_run, root=root)
        if unit is None:
            ok = False
        else:
            units.append(unit)
    if args.dry_run:
        if units and args.now:
            print(f"# would: systemctl --user daemon-reload && enable --now "
                  f"{' '.join(units)}")
        return 0 if ok else 1
    if units:
        _systemctl_user("daemon-reload")
        if args.now:
            ok = _systemctl_user("enable", "--now", *units) == 0 and ok
        else:
            print("media-setup: units written. Start them with:\n"
                  f"  systemctl --user enable --now {' '.join(units)}")
    return 0 if ok else 1


def cmd_install_services(args: argparse.Namespace) -> int:
    templates = service_templates_dir()
    if not templates.is_dir():
        print(f"media-setup: service templates not found at {templates}",
              file=sys.stderr)
        return 1
    explicit = bool(args.services)
    names = args.services or service_template_names()

    # Naming a service explicitly overrides its roles. Filtering an argument
    # the caller typed would be the silent no-op this mechanism exists to
    # prevent -- `install-services call-hold-consumer` must either install it
    # or say why not, never appear to succeed having done nothing.
    if not explicit:
        roles = host_roles()
        kept: list[str] = []
        for name in names:
            wanted, why = service_wanted(name, roles)
            if wanted:
                kept.append(name)
            else:
                print(f"media-setup: skipping {name} — {why}")
        if roles is not None and len(kept) != len(names):
            print(f"media-setup: host roles = "
                  f"{', '.join(sorted(roles)) or '(none)'}")
        names = kept
    else:
        roles = host_roles()
        for name in names:
            wanted, why = service_wanted(name, roles)
            if not wanted:
                print(f"media-setup: warning: {name} {why} on this host — "
                      f"installing anyway because you named it")

    # Before the backend dispatch: both backends read agent-media.env, and a
    # never-overwrites merge is safe to repeat on every run.
    added = _merge_env_defaults(_agent_media_env_path(), _MPV_ENV_DEFAULTS,
                                dry_run=getattr(args, "dry_run", False))
    if added:
        print(f"media-setup: set {', '.join(added)} in agent-media.env")

    backend = _service_backend(getattr(args, "backend", None))
    if backend == "systemd":
        return _install_services_systemd(args, names)

    # runit
    root = Path(args.root) if args.root else services_dir()
    if root is None:
        print("media-setup: no runit service root inferred — pass --root, "
              "or use --backend systemd", file=sys.stderr)
        return 2
    ok = True
    for name in names:
        ok = _install_one_service(name, dry_run=args.dry_run, root=root) and ok
    ok = _link_entrypoints(dry_run=args.dry_run) and ok
    return 0 if ok else 1


def entrypoint_link_dir() -> Path:
    """Where a console script has to appear to be on PATH.

    Termux's own `bin` on Termux — that is what the existing hand-made shims
    used, and `~/.local/bin` is not on the default PATH there. `~/.local/bin`
    everywhere else.
    """
    prefix = os.environ.get("PREFIX", "")
    if prefix.startswith("/data/data/com.termux"):
        return Path(prefix) / "bin"
    return local_bin()


def _link_entrypoints(*, dry_run: bool) -> bool:
    """Put this install's console scripts where a runit `run` script can find
    them.

    The systemd units carry `Environment=PATH=<venv bin>`, so an entrypoint
    resolves whatever the user's login PATH is. runit has no equivalent: its
    `run` scripts inherit runsvdir's environment, which on Termux does not
    include the venv. Every console script therefore needed a hand-made
    symlink, and nobody remembers that when adding one — `media-feed` was
    installed, enabled, and spawn-looping on "media-feed: inaccessible or not
    found" with the binary sitting in the venv the whole time.

    So: link them, every install, idempotently. Only `media*` — the scripts
    this project owns — and only into a directory already on PATH.
    """
    bindir = _entrypoint_bindir()
    if not bindir.is_dir():
        return True
    dest_dir = entrypoint_link_dir()
    ok = True
    for src in sorted(bindir.glob("media*")):
        if src.is_file() and os.access(src, os.X_OK):
            ok = _symlink_into(src, dest_dir / src.name, dry_run=dry_run) and ok
    return ok


# --- Rooms audio hub (server role) -----------------------------------------
# `media-setup server` wires a PipeWire/systemd host as a Snapcast render hub:
# null sinks (am[/am-music]) -> parec -> /tmp/snapfifo-<sink> -> snapserver.
# This is the USER-level half (sinks + parec bridge + rooms env). snapserver
# itself needs root (pkg + /etc/snapserver.conf + a same-user override + the
# tmpfiles FIFO pre-create), so those are printed for sudo / an ansible
# audio_server role. PipeWire/systemd hosts only — Termux keeps the openal AO
# default (it survives BT route changes), so this never runs there.

ROOMS_SPEECH_SINK = "am"
ROOMS_MUSIC_SINK = "am-music"

_AM_SINKS_UNIT = """\
[Unit]
Description=agent-media: PipeWire null sinks for rooms audio
After=pipewire.service pipewire-pulse.service wireplumber.service
Requires=pipewire.service
# The null sinks live in PipeWire's runtime, so restarting pipewire destroys
# them. Without PartOf this RemainAfterExit oneshot stays "active" forever while
# the sinks are silently gone; every parec capture then falls back to the same
# monitor and both Snapcast streams carry identical audio (red5, 2026-07-26).
PartOf=pipewire.service

[Service]
Type=oneshot
RemainAfterExit=yes
{execstarts}
# Pin the default sink so speech sent to the default `local` target lands on the
# whole-house feed. Retried because WirePlumber may not have adopted the
# just-created node yet; never fails the unit.
ExecStartPost=/bin/sh -c 'for i in 1 2 3 4 5; do pactl set-default-sink {speech_sink} && exit 0; sleep 0.5; done; exit 0'
{execstops}

[Install]
WantedBy=default.target
"""

_AM_SNAPFIFO_UNIT = """\
[Unit]
Description=agent-media: parec %i.monitor -> /tmp/snapfifo-%i (snapserver pipe)
After=am-sinks.service
Requires=am-sinks.service

[Service]
# --latency-msec bounds the capture-stream buffer: if the FIFO reader stalls,
# pulse would otherwise queue ~4MB (~21s) that never drains — audio then
# arrives permanently late. Bounded, a stall drops stale audio instead.
ExecStart=/bin/sh -c "exec parec --latency-msec=500 --device=%i.monitor --rate=48000 --format=s16le --channels=2 > /tmp/snapfifo-%i"
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
"""

# PipeWire host: per-clip `audio-device=pulse/<sink>` routing needs the pulse
# AO. (openal — the default that survives Termux BT route changes — does not
# understand pulse device ids.)
# Slow-endpoint breaker for a remote mpv bridge. Seeded on every install
# because the units read ~/.config/agent-media.env directly -- load_env_file()
# (and so defaults.env) is only reached by cli.py and the intake hooks, so a
# value that lives solely in defaults.env never reaches a running service.
#
# The number has to clear the real round trip to the phone. red5 (Hetzner,
# Germany) -> p8a (Melbourne) measures 891-1171ms per mpv IPC call; under a
# threshold below that, every probe records slow and -- because the breaker is
# persisted and shared across processes -- it stays armed forever. Phone-local
# playout then goes invisible: SinkMusicLocal.loaded() swallows the error and
# returns False, so the router never routes to the phone and music_now_playing
# reports "target 'phone' not yet supported" mid-playback. Tighten per-host if
# the phone is on the LAN.
_MPV_ENV_DEFAULTS = (
    ("MEDIA_MPV_SLOW_MS", "2500"),
    ("MEDIA_MPV_BREAKER_S", "20"),
)


_ROOMS_ENV_DEFAULTS = (
    ("MEDIA_SPEECH_DEFAULT_TARGET", "rooms"),
    ("MEDIA_ROOMS_SINK", ROOMS_SPEECH_SINK),
    ("MEDIA_SPEECH_AO", "pulse"),
    ("MEDIA_RENDER_ENGINE", "edge"),
)


def _agent_media_env_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "agent-media.env"


def _merge_env_defaults(env_path: Path, defaults, *, dry_run: bool) -> list[str]:
    """Append missing KEY=value defaults to agent-media.env. Never overwrites a
    key the user already set. Returns the keys added."""
    existing = env_path.read_text() if env_path.exists() else ""
    present = {ln.split("=", 1)[0].strip()
               for ln in existing.splitlines()
               if "=" in ln and not ln.lstrip().startswith("#")}
    add = [(k, v) for k, v in defaults if k not in present]
    if not add:
        return []
    block = "".join(f"{k}={v}\n" for k, v in add)
    if dry_run:
        print(f"# would append to {env_path}:\n{block}", end="")
        return [k for k, _ in add]
    env_path.parent.mkdir(parents=True, exist_ok=True)
    sep = "" if (not existing or existing.endswith("\n")) else "\n"
    with env_path.open("a") as fh:
        fh.write(sep + ("" if existing else "# agent-media host config\n") + block)
    return [k for k, _ in add]


def cmd_server(args: argparse.Namespace) -> int:
    """Wire this host as a Snapcast rooms render hub (PipeWire/systemd only)."""
    if _service_backend(getattr(args, "backend", None) or "auto") != "systemd":
        print("media-setup server: PipeWire/systemd --user hosts only "
              "(Termux/runit hosts are snapclients, not the hub).",
              file=sys.stderr)
        return 1

    sinks = [ROOMS_SPEECH_SINK] + ([ROOMS_MUSIC_SINK] if args.music else [])
    root = systemd_user_dir()
    execstarts = "\n".join(
        f'ExecStart=/bin/sh -c "pactl list short sinks | cut -f2 | grep -qx {s} '
        f'|| pactl load-module module-null-sink sink_name={s} '
        f'sink_properties=device.description={s}"'
        for s in sinks)
    # One unload per sink, in reverse. $$ is systemd's escape for a literal '$':
    # unescaped, systemd would expand $id itself and hand sh an empty string.
    execstops = "\n".join(
        f"""ExecStop=/bin/sh -c 'id=$$(pactl list short modules """
        f"""| grep -E "sink_name={s}[[:space:]]" | cut -f1); """
        f"""[ -n "$$id" ] && pactl unload-module $$id; true'"""
        for s in reversed(sinks))
    for name, content in (("am-sinks.service", _AM_SINKS_UNIT.format(execstarts=execstarts, execstops=execstops,
                                                        speech_sink=ROOMS_SPEECH_SINK)),
                          ("am-snapfifo@.service", _AM_SNAPFIFO_UNIT)):
        dest = root / name
        # A stow-managed dotfiles checkout symlinks these unit paths into the
        # repo, so write_text() would follow the link and silently rewrite the
        # committed file. That clobbered am-sinks.service on red5 (2026-07-26):
        # it dropped the am-music sink, and both Snapcast streams then captured
        # the same monitor. Refuse to write through a symlink -- the file has
        # another owner, and the breakage is invisible until someone reads
        # `git status`.
        if dest.is_symlink():
            print(f"media-setup: {name} is a symlink -> {dest.readlink()} "
                  f"(stow-managed?); refusing to write through it", file=sys.stderr)
            continue
        if args.dry_run:
            print(f"# would write {dest}:\n{content}")
        elif dest.exists() and dest.read_text() == content:
            print(f"media-setup: {name} already up to date")
        else:
            root.mkdir(parents=True, exist_ok=True)
            dest.write_text(content)
            print(f"media-setup: wrote {dest}")
    units = ["am-sinks.service"] + [f"am-snapfifo@{s}.service" for s in sinks]

    added = _merge_env_defaults(_agent_media_env_path(), _ROOMS_ENV_DEFAULTS,
                                dry_run=args.dry_run)
    print(f"media-setup: set {', '.join(added)} in agent-media.env" if added
          else "media-setup: agent-media.env already has the rooms env")

    if args.dry_run:
        print(f"# would: systemctl --user daemon-reload && enable --now {' '.join(units)}")
    else:
        _systemctl_user("daemon-reload")
        if args.now:
            _systemctl_user("enable", "--now", *units)
        else:
            print("media-setup: units written. Start them with:\n"
                  f"  systemctl --user enable --now {' '.join(units)}")

    user = os.environ.get("USER") or Path.home().name
    src_lines = "; ".join(
        f"source = pipe:///tmp/snapfifo-{s}?name={s}&codec=pcm&sampleformat=48000:16:2"
        for s in sinks)
    print("\nmedia-setup: snapserver itself needs root — run the dotfiles "
          "audio_server ansible role, or as root:\n"
          f"  * /etc/snapserver.conf [stream]: {src_lines}\n"
          f"  * snapserver.service override -> User={user} Group={user} "
          "(same-user FIFO constraint)\n"
          f"  * /etc/tmpfiles.d/snapfifo.conf: pre-create "
          f"{', '.join('/tmp/snapfifo-'+s for s in sinks)} owned by {user}\n"
          f"  * loginctl enable-linger {user}; systemctl enable --now snapserver",
          file=sys.stderr)
    return 0


# --- Shell integration (tmux popup + control surface) ----------------------

def cmd_install_shell(args: argparse.Namespace) -> int:
    """Symlink the shell-facing bits onto PATH so the `prefix a` popup works:
    the executable helpers in tmux/ (media-popup, media-popup-open, …) into
    ~/.local/bin, and the tmux control surface (media.tmux) into
    ~/.local/share/agent-media/. Repo-source symlinks, so a `git pull` keeps
    them current; the bin loop enumerates tmux/, so new helper scripts are
    picked up automatically."""
    src_dir = tmux_dir()
    if not src_dir.is_dir():
        print(f"media-setup: tmux dir not found at {src_dir}", file=sys.stderr)
        return 1
    ok = True
    bindir = local_bin()
    for f in sorted(src_dir.iterdir()):
        # Executable scripts (not the .tmux source) → ~/.local/bin.
        if f.is_file() and f.suffix != ".tmux" and os.access(f, os.X_OK):
            ok = _symlink_into(f, bindir / f.name, dry_run=args.dry_run) and ok
    # The tmux control surface, sourced from tmux.conf.local behind an
    # if-shell guard → ~/.local/share/agent-media/.
    tmux_conf = src_dir / "media.tmux"
    if tmux_conf.is_file():
        ok = _symlink_into(tmux_conf, media_share_dir() / "media.tmux",
                           dry_run=args.dry_run) and ok
    return 0 if ok else 1


# --- Prereq check ----------------------------------------------------------

PREREQS: tuple[tuple[str, str], ...] = (
    ("python3", "python (>= 3.11)"),
    ("mpv",     "mpv (used by sink-speech)"),
    ("mpc",     "mpd client (sink-music helper)"),
    ("edge-tts", "edge-tts (default render engine)"),
    ("jq",      "jq (legacy hook helpers; can drop after full retire)"),
)


def cmd_check(_: argparse.Namespace) -> int:
    missing = []
    for bin_, label in PREREQS:
        if shutil.which(bin_) is None:
            missing.append((bin_, label))
            print(f"  MISSING  {bin_:12} ({label})")
        else:
            print(f"  ok       {bin_}")
    return 0 if not missing else 1


# --- Status ----------------------------------------------------------------

def cmd_status(_: argparse.Namespace) -> int:
    path = claude_settings_path()
    if not path.exists():
        print(f"settings: {path} missing")
    else:
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            print(f"settings: {path} unparseable: {e}")
            return 1
        for event in CLAUDE_HOOK_EVENTS:
            groups = (data.get("hooks") or {}).get(event) or []
            cmds = [h.get("command") for g in groups for h in (g.get("hooks") or [])]
            print(f"hook {event}: {cmds or '(none)'}")
    templates = service_templates_dir()
    names = service_template_names()
    backend = _service_backend(None)
    print(f"service backend: {backend}")
    if backend == "runit":
        root = services_dir()
        for name in names:
            link = (root / name) if root else None
            mark = ("installed" if link and (link.exists() or link.is_symlink())
                    else "MISSING")
            print(f"service {name}: {mark}")
    else:  # systemd
        sd = systemd_user_dir()
        for name in names:
            unit = _systemd_unit_name(name)
            if not (sd / unit).exists():
                print(f"service {unit}: MISSING")
                continue
            active = subprocess.run(
                ["systemctl", "--user", "is-active", unit],
                capture_output=True, text=True).stdout.strip() or "unknown"
            print(f"service {unit}: installed ({active})")
    _print_feed_status()
    return 0


def _print_feed_status() -> None:
    """Where the feed is, whether it is guarded, and what is on it.

    Reported here because the feed is the one surface whose *absence* is
    invisible: no error, no log line, just a URL nobody was ever told.
    """
    base = _env_value("MEDIA_FEED_BASE_URL")
    if not base:
        print("feed: off (`media-setup feed` to switch it on)")
        return
    guard = "token set" if _env_value("MEDIA_FEED_TOKEN") else "NO TOKEN"
    print(f"feed: {base} ({guard})")
    if guard == "NO TOKEN" and not _env_value("MEDIA_FEED_BIND").startswith("127."):
        # The server refuses to start this way; say so here rather than let it
        # be discovered as a unit that will not come up.
        print("feed: WARNING — off-loopback with no MEDIA_FEED_TOKEN; "
              "media-feed will refuse to start")
    try:
        from .feed import episodes, feeds
    except Exception:                                    # pragma: no cover
        return
    names = feeds()
    if not names:
        print("feed: nothing published yet "
              "(`media doc play <doc> --feed`, `media feed session`)")
        return
    for name in names:
        eps = episodes(name)
        newest = max((e.published for e in eps), default=0)
        when = time.strftime("%Y-%m-%d", time.localtime(newest)) if newest else "-"
        print(f"feed {name}: {len(eps)} episode(s), newest {when}")


# --- CLI -------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="media-setup",
                                 description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("check", help="Verify prereq binaries")
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("install-hooks",
                        help="Merge hook entries into ~/.claude/settings.json")
    sp.add_argument("--settings", help="Path to settings.json (default: "
                    "~/.claude/settings.json)")
    sp.add_argument("--command", default=CLAUDE_HOOK_COMMAND,
                    help="Hook command name to register")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_install_hooks)

    sp = sub.add_parser("init",
                        help="write a starter config.toml for this host")
    sp.add_argument("--roles", help="comma-separated, e.g. observe,render")
    sp.add_argument("--config", help="write somewhere other than the default")
    sp.add_argument("--force", action="store_true",
                    help="overwrite an existing config")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("feed",
                        help="switch on the podcast feed (token, services, "
                             "subscribe URL)")
    sp.add_argument("--bind", default="",
                    help="address to listen on (default: this host's tailnet "
                         "IPv4). Never 0.0.0.0 — the enclosures are private.")
    sp.add_argument("--port", type=int, default=0)
    sp.add_argument("--base-url", default="",
                    help="what a subscriber types; default http://<bind>:<port>")
    sp.add_argument("--no-services", action="store_true",
                    help="write the config, install nothing")
    sp.add_argument("--backend", choices=("runit", "systemd"))
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_feed)

    sp = sub.add_parser("install-services",
                        help="Install services (runit on Termux, systemd "
                             "--user on regular Linux)")
    sp.add_argument("--backend", choices=("auto", "runit", "systemd"),
                    default="auto",
                    help="Service supervisor (default: auto-detect)")
    sp.add_argument("--root", help="runit service root (default: "
                    "$PREFIX/var/service on Termux; ignored for systemd)")
    sp.add_argument("--now", action="store_true",
                    help="systemd: enable --now the units after writing")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("services", nargs="*",
                    help="Specific service names (default: all in repo)")
    sp.set_defaults(func=cmd_install_services)

    sp = sub.add_parser("server",
                        help="Wire this host as a Snapcast rooms render hub "
                             "(PipeWire null sinks + parec->FIFO + rooms env; "
                             "PipeWire/systemd hosts only)")
    # Music is on by default: a rooms hub without am-music is the broken state.
    # A `media-setup server` run that forgot --music is what dropped the sink on
    # red5 (2026-07-26). --music stays accepted so existing invocations still work.
    sp.add_argument("--music", dest="music", action="store_true", default=True,
                    help="wire the am-music sink/bridge (default: on)")
    sp.add_argument("--no-music", dest="music", action="store_false",
                    help="omit the am-music sink/bridge (am only)")
    sp.add_argument("--now", action="store_true",
                    help="enable --now the units after writing")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_server)

    sp = sub.add_parser("install-shell",
                        help="Symlink the tmux popup launcher + control "
                             "surface onto PATH (~/.local/bin, "
                             "~/.local/share/agent-media)")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_install_shell)

    sp = sub.add_parser("migrate-env",
                        help="Rename CLAUDE_TTS_*/AAR_*/RELAY_* envs to "
                             "MEDIA_* in settings.json + agent-audio-relay.env")
    sp.add_argument("--settings", help="Path to settings.json (default: "
                    "~/.claude/settings.json)")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_migrate_env)

    sp = sub.add_parser("status",
                        help="Show current wiring")
    sp.set_defaults(func=cmd_status)

    return p



# --- First run ---------------------------------------------------------------

_STARTER_CONFIG = """\
# agent-media — what this host is, and who its peers are.
#
# Hostnames appear HERE and nowhere else. Code asks for the machine that can do
# a thing, never for a machine by name.
#
# Roles:
#   observe   this machine has a mic or a dialer worth watching
#   render    this machine has audio sinks that can be silenced
#   origin    this machine produces the text to be spoken
#
# One machine on its own holds all three, and that is the whole of what
# "standalone" means -- there is no mode to switch.

[host]
roles = [{roles}]

# Other machines in the setup, if any. Delete this section when running alone.
#
# [peers.hub]
# host  = "the-hostname"
# roles = ["render", "origin"]
"""


def _guess_roles() -> "list[str]":
    """A starting guess, stated as a guess.

    Deliberately shallow: `render` for anything with an audio stack, `observe`
    only where the companion app could plausibly run. Guessing `origin` is left
    alone because it is about what the machine is FOR, which no probe answers.
    """
    roles = ["render"]
    prefix = os.environ.get("PREFIX", "")
    if prefix.startswith("/data/data/com.termux"):
        roles.insert(0, "observe")
    return roles


FEED_PORT = 8782


def _tailnet_address() -> str:
    """This host's tailnet IPv4, or "".

    The feed is tailnet-only by design, and the address is the one thing
    onboarding cannot guess from anything else on the machine.
    """
    import subprocess

    import socket

    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                             text=True, timeout=8).stdout.strip()
        first = out.splitlines()[0].strip() if out else ""
        if first.startswith("100."):
            return first
    except (OSError, subprocess.SubprocessError):
        pass

    # No CLI: on Android, Tailscale is an app and there is no `tailscale`
    # binary in Termux at all — which is the host where onboarding is hardest
    # and guessing least welcome. Ask the routing table instead: the source
    # address the kernel would use for a tailnet destination IS this host's
    # tailnet address. No packet is sent; connect() on UDP only sets a route.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("100.100.100.100", 53))     # Tailscale's own resolver
            ip = s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return ""
    return ip if ip.startswith("100.") else ""


def cmd_feed(args: argparse.Namespace) -> int:
    """Switch the podcast feed on: token, address, services, subscribe URL.

    Opt-in, and a separate command rather than part of `init`, because this is
    the one part of agent-media that publishes recordings of private
    conversations to anything that can reach a URL. That should be a decision
    somebody makes, never a thing that arrives with an install.

    Everything here is idempotent and never overwrites: run it twice and the
    second run tells you the subscribe URL again.
    """
    import secrets

    env_path = _agent_media_env_path()
    bind = (args.bind or _tailnet_address()).strip()
    if not bind:
        print("media-setup: no tailnet address found — pass --bind with the "
              "address to listen on.\n"
              "  A feed serves recordings of private conversations: bind the "
              "tailnet, never 0.0.0.0.", file=sys.stderr)
        return 1
    if bind in ("0.0.0.0", "::"):
        # Refused rather than warned. The whole security model is "only the
        # tailnet can reach it"; a wildcard bind silently deletes it.
        print("media-setup: refusing --bind 0.0.0.0 — the enclosures are "
              "recordings of private conversations.", file=sys.stderr)
        return 2

    port = args.port or FEED_PORT
    base = (args.base_url or f"http://{bind}:{port}").rstrip("/")
    defaults = [("MEDIA_FEED_BIND", bind),
                ("MEDIA_FEED_PORT", str(port)),
                ("MEDIA_FEED_BASE_URL", base),
                ("MEDIA_FEED_TOKEN", secrets.token_urlsafe(24))]
    added = _merge_env_defaults(env_path, defaults, dry_run=args.dry_run)
    if added:
        print(f"media-setup: set {', '.join(added)} in {env_path}")
    else:
        print(f"media-setup: {env_path} already configures the feed — "
              f"leaving it alone")

    # Serving is one job and filling is another. Every host that switches the
    # feed on gets the server; only a host that *has* conversations gets the
    # publisher and the pruner that follows it, because `publish-quiet` reads
    # speech history and a render-only host has none of its own. Naming all
    # three everywhere would install two units to do nothing, with a
    # role-override warning to explain it.
    roles = host_roles()
    wanted = ["media-feed"]
    if roles is None or "origin" in roles:
        wanted += ["media-feed-publish", "media-feed-gc"]
    else:
        print(f"media-setup: this host is [{', '.join(sorted(roles)) or 'none'}] "
              f"— installing the server only; publishing needs `origin`.")
    if not args.no_services and not args.dry_run:
        rc = cmd_install_services(argparse.Namespace(
            services=wanted, backend=getattr(args, "backend", None), now=True,
            dry_run=False, root=None))
        if rc != 0:
            return rc

    # In a dry run nothing was written, so the token to show is the one that
    # would have been. A subscribe URL without it is not the URL.
    token = _env_value("MEDIA_FEED_TOKEN") or (
        dict(defaults)["MEDIA_FEED_TOKEN"] if args.dry_run else "")
    base = _env_value("MEDIA_FEED_BASE_URL") or base
    print("\nsubscribe:")
    for name in ("talks", "docs"):
        print(f"  {base}/feed/{name}.xml" + (f"?k={token}" if token else ""))
    print(f"  {base}/" + (f"?k={token}" if token else "")
          + "   (all feeds, as a list)")
    published = []
    try:
        from .feed import episodes, feeds
        published = [(n, len(episodes(n))) for n in feeds()]
    except Exception:  # noqa: BLE001 — a nudge, never the command's failure
        pass
    if published:
        print("\non the feed already: "
              + ", ".join(f"{n} ({c})" for n, c in published))
    else:
        print("\nnothing is published yet. Put something on a feed with:")
        print("  media doc play <doc> --feed        # a document")
        print("  media feed session                 # this conversation")
        print("  (or wait — agent-media-feed-publish does finished ones hourly)")
    if token:
        print("\nOn an Android phone, AntennaPod takes the link directly:")
        print(f"  am start -a android.intent.action.VIEW \\\n"
              f"    -d 'antennapod-subscribe://"
              f"{base.split('://', 1)[-1]}/feed/talks.xml?k={token}'")
    return 0


def _env_value(key: str) -> str:
    """`key` from agent-media.env (or the environment), or ""."""
    if (os.environ.get(key) or "").strip():
        return os.environ[key].strip()
    try:
        text = _agent_media_env_path().read_text()
    except OSError:
        return ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == key:
            return v.strip().strip('"').strip("'")
    return ""


def cmd_init(args: argparse.Namespace) -> int:
    """Write a starter config, if there is not one already."""
    from .config import config_path

    path = Path(args.config) if args.config else config_path()
    if path.exists() and not args.force:
        print(f"media-setup: {path} already exists — leaving it alone "
              f"(--force to overwrite)")
        return 0

    roles = args.roles.split(",") if args.roles else _guess_roles()
    rendered = _STARTER_CONFIG.format(
        roles=", ".join(f'"{r.strip()}"' for r in roles if r.strip()))

    if args.dry_run:
        print(f"# would write {path}:")
        print(rendered)
        return 0

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered)
    print(f"media-setup: wrote {path}")
    print(f"media-setup: this host is [{', '.join(roles)}] — a guess; edit it "
          f"if wrong, then:\n"
          f"  media-setup install-services   # installs only what these roles want\n"
          f"  media-setup install-hooks      # wire up the agent side")
    return 0


def main() -> int:
    args = _build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
