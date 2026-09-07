# Private sessions

Status: **proposed**, 2026-09-07. The design lives in the private config repo
(`agent-config/docs/2026-09-07-private-sessions.md`) because it spans three
repos and names local paths. The agent-media part in one line: **a session can
be marked private, and a private session is heard and seen but never stored.**

## agent-media's share of the work

Honour a per-session marker (`~/.local/state/agent/private.d/<session-id>`,
also `MEDIA_PRIVATE=1` in the hook environment) in every writer on the reply
path:

- speak as normal — privacy is about storage, not the voice;
- no `.txt` beside the clip; clip rendered under `audio/private/`, which the
  clips server does not serve, and deleted on `end`;
- `speech-events.jsonl` row carries `"text": null, "private": true` so the
  lanes still move and the row still expires;
- no `hook-play.log` line; no canvas push (or live-only render, nothing in
  `pushes.json`/disk, when a figure was explicitly requested);
- journald gets `session=<id> private=1 clips=N`, nothing else.

Add `media private on|off|status [--session <id>] [--for <dur>]` with the
`speech-hold.d` lifecycle code, and `media purge --session <id> [--dry-run]`
that walks the stores above. The companion app must honour `private: true` on
the now-playing row and skip its clip cache.

Estimate: half a day for hook + CLI + purge; small companion-app change on top.
