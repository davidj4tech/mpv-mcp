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

## Install (Termux)

```sh
git clone <repo> ~/projects/mpv-mcp
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

**OpenClaw:**
```sh
openclaw mcp set mpv '{"url":"http://100.94.14.59:8765/mcp"}'
```

## MCP tools

`play`, `queue`, `pause`, `resume`, `stop`, `skip`, `prev`, `seek`, `volume`, `now_playing` — each takes an optional `channel` (default `music`).

## Web UI

`http://<tailscale-ip>:8765/` — both channels stacked, volumes, transport, ducked badge.

## Operations

```sh
sv status   mpv mpv-tts mpv-mcp
sv restart  mpv-mcp
tail $PREFIX/var/log/sv/mpv-mcp/current
```
