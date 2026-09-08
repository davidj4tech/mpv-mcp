# Documentation

Organised by **lifecycle**, not by topic. The title already says what a file is
about; what you cannot tell from a title is the question that actually gets
asked — *when may I delete this?*

| directory | what it holds | when it goes |
|---|---|---|
| [`reference/`](reference/) | how things are now | when the code it describes goes |
| [`decisions/`](decisions/) | why we chose X | never — superseded, not edited |
| [`proposals/`](proposals/) | wanted, not built | when built, or when abandoned |
| [`notes/`](notes/) | working knowledge, findings, gotchas | trim freely |
| [`handover/`](handover/) | session state, one file per session | supersede, but keep the history |

Dated kinds are named `YYYY-MM-DD-slug.md`, so staleness is visible in a
listing. Reference docs use a bare slug — they are meant to be current.

Each dated file opens with its title, then `Status:` and `Date:`. That is
what lets this index, and the popup's document chooser, be generated rather
than maintained by hand.

## Reference

- [channel-architecture](reference/channel-architecture.md) — how the channels fit together
- [channels](reference/channels.md) — the channels in detail
- [control-surface](reference/control-surface.md) — the control layer
- [extensions](reference/extensions.md) — extending agent-media
- [hermes-voice](reference/hermes-voice.md)
- [music-local-and-snapcast](reference/music-local-and-snapcast.md)
- [input-claim](reference/input-claim.md) — who owns David's next utterance when the owner is off-host (cece live)
- [restructure](reference/restructure.md) — the package layout and how it got there
- [rooms-unit-ownership](reference/rooms-unit-ownership.md)
- [sillytavern](reference/sillytavern.md)
- [speech-phone-render-grade-b](reference/speech-phone-render-grade-b.md) — the phone lane
- [version-skew-monitoring](reference/version-skew-monitoring.md)

## Decisions

- [2026-08-05 speech-state-convergence](decisions/2026-08-05-speech-state-convergence.md) — keep `/speech` and `speech-state.service` separate

## Proposals

- [2026-09-09 book-read-along](proposals/2026-09-09-book-read-along.md) — ePub sentences as subtitles for narrated books, and a book canvas
- [2026-08-09 spoken-docs](proposals/2026-08-09-spoken-docs.md) — this layout, and playing documents from the popup
- [2026-08-09 doc-roots-org-para](proposals/2026-08-09-doc-roots-org-para.md) — how playback meets GTD, PARA, org-roam and Denote
- [2026-08-07 play-video](proposals/2026-08-07-play-video.md) — `media play-video` subcommand

## Notes

- [2026-08-05 speech-controls](notes/2026-08-05-speech-controls.md) — breadcrumb + control channel, OpenWebUI STT

## Handover

- [2026-08-14 night](handover/2026-08-14-night.md) — the app stops guessing; three duckers become one
- [2026-08-14 evening](handover/2026-08-14-evening.md) — audio focus landed; why the earbuds only ever sent pause
- [2026-08-14](handover/2026-08-14.md) — audio focus, the half that retires Automate
- [2026-08-13](handover/2026-08-13.md) — build the phone companion app
- [2026-08-10](handover/2026-08-10.md) — documents you can listen to; the phone lane leaves ssh
- [2026-08-09](handover/2026-08-09.md) — speech routing and the phone lane

Handover used to be a single `HANDOVER.md` that was **gitignored** — the
fastest-rotting document in the repo was the only one with no history, so
every session's context was overwritten by the next. One file per session,
tracked, fixes that.

### How a session ends

Writing the handover is only half of it. The other half is **starting the
session that will read it**, because the moment to do that is while the
outgoing session still knows what the next one needs — not the next time David
happens to sit down.

So a session ends by opening its successor in a new window of the attached
tmux session:

```sh
PROMPT='read docs/handover/<the file you just wrote>.md and follow it. <what to start on>'
tmux new-window -d -t p-agent-media -c ~/projects/agent-media \
    "zsh -ic 'cl \"$PROMPT\"'"
```

**`cl` is a zsh alias** (`exec claude --dangerously-skip-permissions`), so it
exists only in an interactive shell. `tmux new-window` runs its command through
a non-interactive `sh`, where the alias is invisible and the window dies with
status 127 — which looks exactly like nothing happening, because the pane
closes on exit. Hence `zsh -ic`. To see a launch failure rather than guess at
it, `set-window-option remain-on-exit on` and `capture-pane`.

**Do not pass `-n <name>`.** tmux turns `automatic-rename` off for any window
given an explicit name, freezing the tab forever while the pane title keeps
tracking Claude's live session title — so the window ends up disagreeing with
itself. Unnamed, David's `automatic-rename-format` derives the label from the
session title and it stays honest. To thaw one that is already frozen:
`tmux set -w -t <win> automatic-rename on`.

**It must be a `new-window` in an already-attached session.** Claude Code's TUI
needs a tmux client at launch; a detached session, or `amux start`, gives it
none and it dies on startup. Check with `tmux list-sessions` that the target is
`(attached)` first — and note the session is `p-agent-media`, not
`projects-agent-media`.

Hand the successor the handover path in its opening prompt rather than trusting
it to find the right one. There are several, they are all plausible, and the
newest is not always the one that matters.
