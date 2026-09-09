# Landscape for the picture under a message — what shipped, and what is left

2026-09-09, David's call, straight after the canvas simplify
(`2026-09-09-canvas-simplify.md`). "I like the canvas link and image under the
message. one thing — can it be displayed in landscape when clicked."

He chose **page now, app later**: ship the browser answer today so it works on
every surface, then fold the automatic in-app rotation into the next APK.

## Shipped (4f13448, plus 1162282 and f8045ad)

- The canvas page grew a fullscreen button (top-right, invisible at rest,
  revealed by any touch or mouse move) that also locks the screen landscape.
  Withheld on e-ink — DU4 does not move, and rotating the screen is the largest
  movement there is.
- `/img/<name>` now answers two questions at one address. An `<img>` gets the
  bytes; a top-level navigation gets a viewer page — the picture whole, with
  that same fullscreen-plus-landscape button. `Sec-Fetch-Dest` tells them
  apart; anything that does not say gets the bytes. `?raw=1` declines the
  viewer, `?view=1` demands it.
- **Nothing in the app changed.** `ConversationLog.openPicture` already calls
  `Browser.open` on exactly this URL, so the viewer arrived with a canvas
  restart on red5 and p8a. Same for sasonica-web.

Tests: `packages/visual/tests/test_view.py` (8), browser harness T17a–e.

## Left — the two things that need an APK

Both are in `~/projects/sasonica` (branch `sasonica`), and both want the CI
`sasonica-apk` artifact + `adb install -r` (memory: [[sasonica-fork]]).

1. **`CanvasPanel.vue` frames the canvas with `allow="autoplay"`** and no
   `allowfullscreen`, so `document.fullscreenEnabled` is false inside it and
   the canvas's own button correctly takes itself off the page. Add
   `allowfullscreen` (or `allow="autoplay; fullscreen"`) and the framed canvas
   above a conversation can fill the screen too.

2. **An in-app picture viewer, so it is no taps rather than one.** The browser
   rule is the whole reason the shipped answer costs a tap: a page cannot
   fullscreen or rotate itself on load, only inside a gesture. A native viewer
   has no such rule — a full-screen `<img>` modal in the app plus
   `setRequestedOrientation(LANDSCAPE)` on open and `UNSPECIFIED` on close is
   automatic. The manifest has no `screenOrientation` lock, so nothing has to
   be undone first; there is no `@capacitor/screen-orientation` in
   `package.json` yet, so it is either that plugin or a few lines beside
   `SasonicaControl.kt`.

   Keep the viewer page when this lands: it is the only answer sasonica-web and
   a plain browser tab will ever have, and it is what `Browser.open` falls back
   to if the native viewer is ever skipped.

## Watch for

`Sec-Fetch-Dest` is the hinge. Android's WebView has sent it since Chromium 80,
and if a client ever does not, the failure is the old behaviour (a bare image),
never a broken picture — that is deliberate and worth keeping if this route is
touched.
