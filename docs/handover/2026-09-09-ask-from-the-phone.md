# A new chat from the phone — the assistant button and "New chat"

2026-09-09. Companion to `2026-09-05-reply-from-the-player.md`: that one
types into the session behind a conversation you are listening to; this one
starts a session that does not exist yet.

## What exists

**agent-media.** `POST /ask {"text"}` on the canvas (8781), gated like
`/reply` (the caller's ABS bearer, root or `MEDIA_REPLY_USERS`). It opens a
fresh Claude Code window with the words as the first message, learns the new
session's uuid from the pane registry, shelves the words as the listener's
turn, and answers `{session, pane}`. `GET /conversation?session=<uuid>` then
says whether the library has an item for it yet (`item` is null until the
first turn is exported and ABS has scanned the folder — about two minutes in
the live test). Where the window lands is copied from the amux registration
named by `MEDIA_ASK_SESSION` (default `scratch`: `~/scratch`,
`--dangerously-skip-permissions`), overridable part by part with
`MEDIA_ASK_TMUX`, `MEDIA_ASK_CWD`, `MEDIA_ASK_FLAGS`.

**Sasonica** (`sasonica` branch). An `ACTION_ASSIST` intent filter on
MainActivity, so the app can be chosen under *Default apps → Digital
assistant app*; `AbsSasonica` turns the intent into an `assist` event
(retained until the web layer listens, for cold starts); `pages/ask.vue`
starts dictation at once, POSTs `/ask`, polls `/conversation?session=` and
moves to the item's chat page when it exists. "New chat" in the side drawer
opens the same page. From the button the words are sent as soon as they are
heard; and pressed with a conversation's page open, the button replies into
that thread instead (Sasonica 60e9ff4c, confirmed on p8a 2026-09-09).

Selecting it once installed: the Settings picker, or over adb shell
`cmd role add-role-holder android.app.role.ASSISTANT com.sasonica.app`
(Termux's uid cannot).

## Four things learned the hard way

- **A tmux session made from under systemd has no `claude` on its PATH.**
  The window looked exactly like a working one and died with `env: 'claude':
  No such file`. claude lives under fnm's per-shell symlink dir in /run;
  `_claude_bin` resolves the stable `~/.local/share/fnm/aliases/default/bin`
  and the window command carries the absolute path.
- **`script -c` runs `$SHELL`, and under the user manager that is zsh**, whose
  `=word` expansion eats a `-t =name` target with a misleading "not found".
  The holder pins `SHELL=/bin/sh` and creates with `-s name`. (The same zsh
  expansion makes `tmux has-session -t =foo` lie in an interactive shell:
  quote it.)
- **A session born detached loses its only window** to the after-new-session
  hook (`tmux-claude-resume` respawns unattended windows), and an empty
  session exits. The holder creates the session *attached* (`new-session -A`
  through `script`, with `TERM` set — systemd has none) as a transient unit
  `agent-media-tmux-hold-<session>`, so it outlives canvas restarts. The
  SessionStart hook then sorts the claude pane into the session named for its
  directory (`scratch`), which is fine: the pane id survives.
- **`pane_ready` is true a beat before the TUI takes Enter.** The message
  went in and sat in the box. `_settle` waits for two identical captures;
  `_ensure_submitted` looks at the box afterwards and presses Enter once if
  the words are still there.

- **The item id has to come from the phone's own server, and after the
  scan.** This host publishes to two ABS instances and each gives the folder
  a different id; and the app's item page cannot open an item ABS has only
  just created. The first live press bounced home with "Failed to get library
  item from server". `/conversation?session=` now looks the folder up with the
  caller's bearer on the caller's server and answers `item` only once
  `numTracks > 0` (`scanning: true` in between).

## What is left

- The ask page opens the item's *chat* page only once the item exists; until
  then it shows the sent text and a pulse. A reply that is never spoken (the
  Stop hook off, say) leaves it waiting out five minutes.
- `quote` is accepted by `/ask` and unused by the app.
- The two smoke-test sessions ("Hello from the phone…", "Second smoke test…")
  are in the library and still live in tmux `scratch`; harmless, delete at will.
