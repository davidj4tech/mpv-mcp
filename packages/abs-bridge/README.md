# agent-media-abs-bridge

Audiobookshelf beside the book channel, in both directions.

**`media-abs-book-bridge`** — while a book plays out to the rooms through mpv,
push its position to ABS, so the phone and web app show the right resume point.
Optionally pull the other way (`ABS_PULL_ON_LOAD=1`): start on the phone, send
it to the rooms, and carry on where you were.

**`media-abs-cast-watcher`** — follows what the ABS app (Sasonica) is playing.
Since 2026-09-09 the phone is the primary player and the watcher only notes a
live session; set `CAST_AUTO=1` to get the old reflex back (detect a session
whose position is genuinely advancing, start the same file on the book channel
at the live position, then close the ABS session so the client stops).

**`media-abs-cast`** — the same handover as an action: send the phone's live
session to the rooms now. `--list` shows open sessions, `--session ID` picks
one, `--dry` decides without touching anything.

Optional, and nothing in agent-media depends on it. Configure in
`~/.config/agent-media/abs-bridge.env`:

```
ABS_URL=http://127.0.0.1:13378
ABS_TOKEN=…            # ABS → Settings → API Keys
ABS_PULL_ON_LOAD=1     # optional
CAST_AUTO=1            # optional: cast every live app session to the rooms
                       # by itself (the pre-2026-09-09 behaviour)
ABS_URLS=…             # optional, comma-separated: OTHER servers the visual
                       # canvas should also accept logins from. Read by
                       # packages/visual, not by these daemons — it lives here
                       # because this is where the ABS config already is.
```

Without `ABS_TOKEN` both daemons log why and exit cleanly, and
`media-setup install-services` skips them on a host with no config file at all.

Installed like the rest: `media-setup install-services abs-book-bridge
abs-cast-watcher --now`.

## History

Both ran for months as untracked files in `~/.local/bin` on one machine — no
version control, no tests, and outside `media doctor`'s health checks, which
only see units named `agent-media-*`. Moving them here was the fix. The cast
watcher also shelled out to a `book-abs` helper that no longer exists anywhere;
it now calls `media book play --start-ms`, resolving the ABS item to a local
path itself.
