# sasonica-web — the fork, the chat page, the reply box

2026-09-09, out of the session that followed `2026-09-09-react-client-parity.md`.
That note is still the spec and the running order; this one says what exists.

## What exists

**The fork.** `davidj4tech/sasonica-web`, from
`audiobookshelf/audiobookshelf-client-react`, cloned at `~/projects/sasonica-web`.
`main` tracks upstream (remote `upstream` is set); `sasonica` is the staging
line and is pushed. `SASONICA.md` at the root carries the fork rule, the
endpoint table, and the build commands.

Three commits, items **1 and 2** of the parity list:

- `src/lib/sasonica/canvas.ts` — the address, the request, the shapes.
  `src/lib/sasonica/strings.ts` — the fork's English, deliberately not in
  upstream's locale files (they are translated by other people and merged
  daily; fork-only keys would make every merge a conflict).
- `src/hooks/sasonica/useConversationSession.ts` — the probe.
  `src/components/sasonica/ReplyBox.tsx` — the composer, the ghost prompt,
  dictation, "go to %pane".
- `src/hooks/sasonica/useConversationLog.ts` — the poll, the cadence, the live
  clock. `src/components/sasonica/{ConversationLog,ConversationPage}.tsx`.

The only upstream file touched is `LibraryItemClient.tsx`: 30 lines, two
imports and a branch, placed below every hook so the swap cannot change the
hook order.

Follow-along (item 3) came with the log rather than after it — the sentence
timeline is in the same payload and the same component. What is NOT there is
the Settings toggle that turns the timing readout on: `ConversationPage` takes
a `debug` prop and nothing sets it yet. That belongs with item 6.

**agent-media.** `6762352`, canvas CORS. The web client is served from the ABS
port and the canvas is on 8781, so every conversation call it makes is
cross-origin; the Capacitor app never needed this. Only the routes whose
credential is the caller's own ABS bearer are opened (`_CORS_PATHS`);
`/input`, `/show`, `/ctl`, `/say`, `/play` spend a token of ours and stay
same-origin. Refusals carry the header too, or the client shows "network
error" in place of the server's own words. `tests/test_cors.py`, 4 tests.

## Where it runs

red5's canvas is restarted, so CORS is live. The `audiobookshelf-react`
container on :13379 was recreated with the built checkout mounted over the
image's build output:

```
-v ~/projects/sasonica-web/.next:/app/client-react/.next
-v ~/projects/sasonica-web/public:/app/client-react/public:ro
-v ~/projects/sasonica-web/next.config.ts:/app/client-react/next.config.ts:ro
-v ~/projects/sasonica-web/package.json:/app/client-react/package.json:ro
```

Not the whole checkout: the image is Alpine and our `node_modules` were
installed on glibc, so the native `@next/swc` and `@parcel/watcher` binaries
are the wrong libc and the server refuses to start. The image's own
`node_modules` are the right ones and our `package.json` adds no dependencies,
so mounting only the build output is enough. `podman logs` should end with
"Using React client at /app/client-react" and "Listening on port :80".

To go back to stock: `podman rm -f audiobookshelf-react` and
`podman rename audiobookshelf-react-stock audiobookshelf-react` (the original
container is stopped, not deleted), then start it.

**Redeploy after a change is `pnpm build` in the checkout and a container
restart** — the mount is live, but Next reads `.next` at startup.

## What is verified, and what is not

Verified: `pnpm typecheck`, `pnpm find-hardcoded-strings` (0 findings),
eslint over the changed paths, `pnpm build`, the 203 visual tests. Live: the
preflight and the header on the tailnet canvas, and `GET /conversation` +
`GET /conversation/log` for a real conversation item, called with the ABS
bearer from the client's own origin, answering with a live turn on it. The
client serves on :13379.

**Not verified: the page rendered in a browser.** It needs a logged-in ABS
session on :13379, and the only credential on this host is an API key, whose
token has no `exp` — Next's proxy treats it as expired and redirects to
/login. A throwaway preview route is not a way round it either: `pnpm dev`
standalone proxies `/status` back to itself and wedges (the README's dev loop
wants a source checkout of the ABS server beside it). So the next session
should either open it as David and look, or stand up the paired dev loop.

## Next

Item 4 (new chat / ask), then 5 (live shelf and session controls), then 6
(settings: the canvas address — `sasonica.canvasUrl` in `localStorage`, read
by `canvasBaseUrl()`, nothing writes it yet — and the timing-readout toggle).

Still owed from the parity note: a GitHub Actions workflow that builds the
Next app and uploads `.next` as an artifact, so red5 can pull a build instead
of making one. `pnpm lint` over the whole tree aborts on this host — it does
on pristine upstream too, it wants more memory than red5 has — so CI is also
where a full lint would actually run.
