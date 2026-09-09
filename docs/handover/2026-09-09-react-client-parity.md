# Sasonica's conversation features on the Audiobookshelf React client

2026-09-09, David's ask: "all the functionality we've added on the app to be
on the React client". The React client is upstream's replacement for the
Vue web client — `github.com/audiobookshelf/audiobookshelf-client-react`
(Next.js, TypeScript, pnpm; `src/app`, `src/components/{app,player,modals,
ui,widgets,...}`, `src/contexts`, `src/hooks`, `src/lib`). It already runs
on red5 as container `audiobookshelf-react` on **:13379** (tailnet bind),
`REACT_CLIENT_PATH=/app/client-react`, its own config at
`~/.local/share/audiobookshelf-react/`, libraries mounted read-only. The
image carries only the built output; a fork needs its own checkout.

## What "parity" means — the Sasonica feature list

All of it talks to agent-media's canvas on **:8781**, authorised by the ABS
bearer the client already holds (the canvas hands it back to ABS's
`/api/authorize`). Endpoints, in `packages/visual/src/agent_media_visual/
canvas.py` (docstring at the top lists them) and `reply.py`:

1. **Conversation page as a chat** — `GET /conversation?item=` (is this a
   conversation I may reply to; `live`, `pane`, `session`, `resumable`,
   `suggestion`), `GET /conversation/log?item=` (lines with `who`, `text`,
   `start`, pictures; the live turn with `sentences`, `offsets`, `elapsed`,
   `delay`, `sentence`, `measured`, `pending`). Sasonica:
   `components/item/ConversationLog.vue`, the chat branch of
   `pages/item/_id/index.vue`, `item-canvas-panel`.
2. **Reply box** — `POST /reply {item, text, quote?, mode}`; the ghost prompt
   (`suggestion`) offered as placeholder; dictation. Sasonica:
   `components/item/ReplyBox.vue`.
3. **Follow-along** — bold sentence on the live turn from `offsets` +
   `elapsed` − `delay` on a local clock, auto-scroll to it; the timing
   readout (Settings toggle). In `ConversationLog.vue`.
4. **New chat / ask** — `POST /ask {text, target?, player_item?, sticky?,
   parse?, dry?}` (routing: picked target, spoken name, player, last thread,
   else fresh; `dry` to confirm; 300 + candidates when ambiguous),
   `GET /conversations` (the picker), `GET /conversation?session=` (poll for
   the item once shelved). Sasonica: `pages/ask.vue`, the drawer entry.
   (The assistant-button intent is Android-only; the page is not.)
5. **Live shelf and session controls** — items tagged `live`
   (`filter=tags.<base64 live>`), the green dot on cards and the title row,
   `POST /session/resume`, `POST /session/close`, `POST /focus {pane}`.
   Sasonica: `pages/bookshelf/index.vue` (`fetchLiveShelf`),
   `components/cards/LazyBookCard.vue`, `ItemMoreMenuModal.vue`.
6. **Settings** — the canvas address (default: the ABS host on :8781),
   the timing readout toggle. `plugins/localStore.js`, `pages/settings.vue`.
7. **Item trimming** — `GET /item?id=` (the slim item the app reads first;
   `item.py`). Optional on the web; the payload-size problem was the
   WebView bridge's.

Read the Sasonica components first; they are the spec, and their comments
carry the reasons. Then `docs/handover/2026-09-05-reply-from-the-player.md`
and `2026-09-09-ask-from-the-phone.md`.

## How to hold the fork

Same rule as Sasonica ([[sasonica-keep-upstream-mergeable]]): fork logic in
fork-only components, upstream files get hook-sized `// Sasonica:` blocks,
never reindented, so upstream merges stay cheap — this client commits daily.

- Fork `audiobookshelf/audiobookshelf-client-react` under `davidj4tech`
  (name it to match: `sasonica-web`), clone to `~/projects/sasonica-web`,
  branch `sasonica` as the staging line.
- Toolchain: node v24 is on red5 via fnm; `corepack enable` gives pnpm
  (`packageManager: pnpm@10`). `pnpm i && pnpm run build`.
- Run it against the :13379 server: replace the container's client with a
  bind mount of the built checkout at `/app/client-react` (podman run … -v
  ~/projects/sasonica-web:/app/client-react:ro), or point a second ABS at
  it with `REACT_CLIENT_PATH`. The readme's dev loop (`pnpm run dev` + the
  server's `dev.js` `ReactClientPath`) needs a source checkout of the ABS
  server; production build + bind mount is the cheaper first step.
- CI: a GitHub Actions workflow that builds the Next app and uploads the
  `.next` output as an artifact, the way Sasonica's `build-apk.yml` does
  for the APK, so red5 can pull a build without building.

## Order

1 and 2 first (the chat page and the reply box: the thing used most), then
3, then 4, then 5. Each as its own commit series; a handover note at the
end of the session in `docs/handover/`.
