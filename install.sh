#!/data/data/com.termux/files/usr/bin/sh
# Install/update mpv-mcp on Termux:
#  - drop server.js into $HOME/mpv-mcp + npm install
#  - install runit services for mpv, mpv-tts, mpv-mcp
#  - install Claude Code TTS Stop hook (replaces legacy)
# Idempotent. Re-run after a `git pull`.

set -e

REPO="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$HOME/mpv-mcp"
SVDIR="$PREFIX/var/service"
HOOK_DST="$HOME/.claude/claude-tts-hook.sh"

echo "[mpv-mcp] repo: $REPO"
echo "[mpv-mcp] app:  $APP_DIR"
echo "[mpv-mcp] sv:   $SVDIR"

# 1. App dir + deps
mkdir -p "$APP_DIR"
install -m 755 "$REPO/server.js"   "$APP_DIR/server.js"
install -m 644 "$REPO/package.json" "$APP_DIR/package.json"
( cd "$APP_DIR" && npm install --no-audit --no-fund --silent )

# 2. Services
for svc in mpv mpv-tts mpv-mcp; do
  mkdir -p "$SVDIR/$svc/log"
  install -m 755 "$REPO/services/$svc/run" "$SVDIR/$svc/run"
  ln -sfn "$PREFIX/share/termux-services/svlogger" "$SVDIR/$svc/log/run"
done

# Wait for runsvdir to notice new dirs
sleep 6

# 3. Enable + restart
export SVDIR
sv-enable mpv     >/dev/null 2>&1 || true
sv-enable mpv-tts >/dev/null 2>&1 || true
sv-enable mpv-mcp >/dev/null 2>&1 || true
sv restart mpv-mcp >/dev/null 2>&1 || true

# 4. Hook (back up legacy once, then overwrite)
mkdir -p "$(dirname "$HOOK_DST")"
[ -f "$HOOK_DST" ] && [ ! -f "$HOOK_DST.legacy.bak" ] && cp "$HOOK_DST" "$HOOK_DST.legacy.bak"
install -m 755 "$REPO/hooks/claude-tts-hook.sh" "$HOOK_DST"

sleep 3
echo "[mpv-mcp] status:"
sv status mpv mpv-tts mpv-mcp || true
echo "[mpv-mcp] done. UI: http://$(ifconfig 2>/dev/null | awk '/inet 100\./{print $2; exit}'):8765/"
