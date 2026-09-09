# Canvas: take the controls, the agent tree and the transcript off the page

2026-09-09, David's call, out of the session that built the assistant-button
work (`2026-09-09-ask-from-the-phone.md`): Sasonica now does replying,
session management, the transcript and follow-along. The wall canvas goes
back to being a picture with a caption.

## What to remove

In `packages/visual/src/agent_media_visual/static/canvas.{html,css,js}`
(the page is split static; `canvas.js` is ~84 KB, one mode state machine
passive → input → agents → control):

- `#ctl` — the two control rows (channel, marquee, clock, kbd, cc, fit, sfx,
  x; transport, volume, speed, mute) and everything keyed to `control` mode.
- `#agents` (the agent tree) and `#peek` — the `agents` mode, the `/agents`
  poll (T3–T6 in the harness), the tree keys (a, j/k, l, h, g/G, p, q).
- `#tx` / `#txlines` / `#txwork` / `#txlive` / `#txclose`, `#zoom`, `#split`,
  `#bandhit` — the whole-reply transcript, its split view with the picture,
  the pinch-to-resize on the words. (Reading is Sasonica's.)
- `#inp` (`#target`, `#text`, `#send`) and `#sheet` — the reply box and the
  typed-prompt sheet; with them the `/input` POSTs, the 401 pairing-toast
  flow (T7–T9, T13) and `input` mode.
- `#help` — the key legend; nearly every key in it goes.

## What stays

The picture (`#stage`, Ken Burns, the audio-reactive pulse/vignette), the
caption strip (`#cap` / `#sub`: the sentence being spoken — that is the one
follow-along the wall keeps), `#fig`, the SSE connection with its watchdog
and self-heal (`#dot`, `#offbar`, T1–T2, T10–T12), `#toast`, the e-ink theme
(T14–T15), the YouTube layer, `bg.js` (the OWUI background loader — separate
harness, untouched). `c` (captions), `f` (sound fx) and `?`→nothing are the
only keys worth keeping; consider none.

## How to do it

- Start from the harness: `packages/visual/tests/browser/harness.js` (README
  there). Drop T3–T9, T13, T16a–j as the code they test goes; keep the rest
  green. `tx-harness.js` and `band-harness.js` test the transcript and the
  band — retire them with it.
- The mode ring: with input/agents/control gone there is only passive. Take
  the state machine out rather than leaving a one-state machine.
- Server side: `/agents`, `/input`, `/ctl` in `canvas.py` stay (the tmux
  popup and `media` CLI use them); only the page stops calling them.
- Restart both canvases after (memory: [[canvas-restart-after-pull]] — red5
  AND p8a, the app loads the phone's canvas page).
- Check the companion app's `CanvasActivity` WebView and Sasonica's
  `item-canvas-panel` still look right: both embed this page.

Commit in the repo's style (short title, prose why). Commit the harness
change in the same commit as the code it stops testing.
