# mpv-mcp

Multi-channel mpv control surface for Termux — exposed as an MCP server, an HTTP/JSON API, and a tiny mobile-first web UI. Designed to live on a phone and be driven from anywhere on the same Tailscale network.

Two playback channels by default:

- **music** — interactive: URLs, YouTube (via `yt-dlp`), playlists, transport, seek, volume.
- **tts** — short clips (e.g. Claude Code TTS replies). Driven by writing to its mpv IPC socket.

When the **tts** channel is non-idle the server **ducks** the **music** channel automatically (mpv property events; no polling).

## Architecture

```
┌────────────┐    ┌────────────┐
│ mpv (idle) │    │ mpv-tts    │   one mpv daemon per channel,
│  music     │    │  clips     │   each with its own IPC socket
└─────▲──────┘    └─────▲──────┘
      │                 │
   IPC socket        IPC socket
      │                 │
      └─────┬───────────┘
            │
       ┌────┴─────┐
       │ mpv-mcp  │   node server: MCP (HTTP), JSON API, web UI,
       │ :8765    │   ducking watcher (subscribes to idle-active)
       └──────────┘
            │
   ┌────────┼─────────┐
   ▼        ▼         ▼
 Claude   OpenCode   Web UI
 OpenClaw (browser)
```

All three runtimes (Claude Code, OpenCode, OpenClaw) connect with the same URL: `http://<tailscale-ip>:8765/mcp`.

## See also

- [agent-audio-relay](https://github.com/davidj4tech/agent-audio-relay) —
  the Python relay that delivers Claude Code / Codex / aider TTS clips
  into this server's `mpv-tts` channel via the
  `RELAY_TERMUX_PLAYER=mpv-ipc` backend.

## Install (Termux)

```sh
git clone https://github.com/davidj4tech/mpv-mcp.git ~/projects/mpv-mcp
cd ~/projects/mpv-mcp
./install.sh
```

The installer:

- copies `server.js` + `package.json` to `~/mpv-mcp/`, runs `npm install`
- writes `mpv`, `mpv-tts`, `mpv-mcp` into `$PREFIX/var/service/`
- enables them under runit (auto-start via your existing `~/.termux/boot/start-services`)
- installs the Claude Code Stop hook at `~/.claude/claude-tts-hook.sh` (legacy backed up to `*.legacy.bak`)

Re-run after a `git pull` to update.

## Configure

Environment variables read by `services/mpv-mcp/run`:

| Var                 | Default                          |
|---------------------|----------------------------------|
| `HOST`              | `100.94.14.59` (phone Tailscale) |
| `PORT`              | `8765`                           |
| `MPV_MUSIC_SOCKET`  | `$PREFIX/tmp/mpv.sock`           |
| `MPV_TTS_SOCKET`    | `$PREFIX/tmp/mpv-tts.sock`       |

Channel volumes (baseline / duck) and the duck primary/duckers are constants in `server.js`.

## Wire up clients

**Claude Code:**
```sh
claude mcp add --scope user --transport http mpv http://100.94.14.59:8765/mcp
```

**OpenCode:** add to `~/.config/opencode/opencode.json`:
```json
"mcp": { "mpv": { "type": "remote", "url": "http://100.94.14.59:8765/mcp", "enabled": true } }
```

**OpenClaw:** (note: `transport` is required — openclaw HTTP defaults to SSE, this server speaks streamable-http)
```sh
openclaw mcp set mpv '{"url":"http://100.94.14.59:8765/mcp","transport":"streamable-http"}'
```

## MCP tools

`play`, `queue`, `pause`, `resume`, `stop`, `skip`, `prev`, `seek`, `volume`, `now_playing` — each takes an optional `channel` (default `music`).

## Web UI

`http://<tailscale-ip>:8765/` — both channels stacked, volumes, transport,
ducked badge.

**Installable as a PWA.** The page serves `/manifest.webmanifest` + an
SVG icon and the right `apple-mobile-web-app-*` meta tags, so on Android
Chrome → ⋮ → "Add to Home screen" gives you a fullscreen launcher icon.

**Keyboard shortcuts (mpv-style).** Click a channel to focus it (border
highlights, current channel shown next to the title). Shortcuts skip
when the URL field has focus.

| Keys                          | Action                                |
|-------------------------------|---------------------------------------|
| `Space` / `k` / `p`           | play/pause (Space on idle TTS plays the latest clip) |
| `←` / `→`                     | seek ±5s (Shift = ±1s)                |
| `↑` / `↓` and `9` / `0`       | volume ±2                             |
| `m`                           | mute toggle                           |
| `<` / `>`                     | playlist prev / next                  |
| `[` / `]`                     | speed ±0.1                            |
| `Backspace`                   | reset speed to 1.0                    |
| `Tab`                         | switch focused channel                |

**TTS-specific buttons.** The `tts` row also has a `Latest` button that
loads `~/.cache/agent-audio/latest.mp3` (override with the
`TTS_LATEST_PATH` env var on the server) and ⏯ behaves the same way on
an idle channel.

### HTTP API

The web UI is a thin client over `/api/state` (GET-style POST returning
JSON snapshots) and `/api/cmd` (`{channel, name, args}`). Commands:
`play`, `queue`, `pause`, `resume`, `stop`, `skip`, `prev`, `seek`,
`volume`, `play_latest`, `volume_delta`, `mute_toggle`, `speed_delta`,
`speed_set`.

## Operations

```sh
sv status   mpv mpv-tts mpv-mcp
sv restart  mpv-mcp
tail $PREFIX/var/log/sv/mpv-mcp/current
```
