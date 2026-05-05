#!/data/data/com.termux/files/usr/bin/sh
# Install/update mpv-mcp on Termux:
#  - server.js + deps in $HOME/mpv-mcp
#  - runit services: mpv (music), mpv-tts (clips), mpv-mcp (server),
#    agent-audio-relay (clip watcher; optional)
#  - Claude Code TTS Stop hook from agent-audio-relay (optional)
# Idempotent. Re-run after a `git pull`.

set -e

REPO="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$HOME/mpv-mcp"
SVDIR="$PREFIX/var/service"
HOOK_DST="$HOME/.claude/claude-tts-hook.sh"
RELAY_SRC="${RELAY_SRC:-}"  # path to agent-audio-relay checkout; opt-in (was $HOME/agent-audio-relay-src and silently downgraded the relay to a stale checkout's version on every install)

echo "[mpv-mcp] repo: $REPO"
echo "[mpv-mcp] app:  $APP_DIR"
echo "[mpv-mcp] sv:   $SVDIR"

# 1. App dir + deps
mkdir -p "$APP_DIR"
install -m 755 "$REPO/server.js"   "$APP_DIR/server.js"
install -m 644 "$REPO/package.json" "$APP_DIR/package.json"
( cd "$APP_DIR" && npm install --no-audit --no-fund --silent )

# 2. Core services. Wrap svlogger with an LOGDIR-exporting shim — without it,
# svlogger crash-loops on root fs (mkdir /sv/<svc> on RO root) when runsvdir
# is launched by Termux:Boot rather than an interactive shell. That tight loop
# pegs CPU, overheats the device, and on some phones triggers reboots.
write_log_wrapper() {
  cat > "$1" <<EOF
#!$PREFIX/bin/sh
export LOGDIR=$PREFIX/var/log
exec $PREFIX/share/termux-services/svlogger
EOF
  chmod +x "$1"
}
for svc in mpv mpv-tts mpv-mcp; do
  mkdir -p "$SVDIR/$svc/log"
  install -m 755 "$REPO/services/$svc/run" "$SVDIR/$svc/run"
  write_log_wrapper "$SVDIR/$svc/log/run"
done

# Also patch ~/.termux/boot/start-services (idempotent) so LOGDIR is exported
# at the runsvdir level. Belt and braces with the per-service wrappers.
BOOT="$HOME/.termux/boot/start-services"
if [ -f "$BOOT" ] && ! grep -q 'LOGDIR=' "$BOOT"; then
  cp "$BOOT" "$BOOT.bak"
  awk -v p="$PREFIX" '/^runsvdir/{print "export LOGDIR="p"/var/log"} {print}' "$BOOT.bak" > "$BOOT"
  chmod +x "$BOOT"
fi

# 3. agent-audio-relay (optional — only if the source checkout is present)
RELAY_INSTALLED=0
if [ -d "$RELAY_SRC" ] && [ -f "$RELAY_SRC/pyproject.toml" ]; then
  echo "[mpv-mcp] agent-audio-relay: installing from $RELAY_SRC"
  pip install --user --break-system-packages --upgrade --quiet "$RELAY_SRC"
  command -v inotifywait >/dev/null 2>&1 || pkg install -y inotify-tools
  command -v ffmpeg      >/dev/null 2>&1 || pkg install -y ffmpeg
  command -v edge-tts    >/dev/null 2>&1 || pip install --user --break-system-packages edge-tts
  mkdir -p "$SVDIR/agent-audio-relay/log"
  install -m 755 "$REPO/services/agent-audio-relay/run" "$SVDIR/agent-audio-relay/run"
  write_log_wrapper "$SVDIR/agent-audio-relay/log/run"

  # Claude TTS Stop hook from the relay (provides edge-tts → drop into watch dir)
  mkdir -p "$HOME/.claude"
  [ -f "$HOOK_DST" ] && [ ! -f "$HOOK_DST.legacy.bak" ] && cp "$HOOK_DST" "$HOOK_DST.legacy.bak"
  install -m 755 "$RELAY_SRC/hooks/claude-code-tts-hook.sh" "$HOOK_DST"
  mkdir -p "$(dirname "$HOOK_DST")/hooks-lib"
  cp -r "$RELAY_SRC/hooks/lib/." "$(dirname "$HOOK_DST")/lib"
  RELAY_INSTALLED=1
else
  echo "[mpv-mcp] agent-audio-relay: skipping (set RELAY_SRC=/path/to/agent-audio-relay to install)"
  # Fallback: direct-IPC hook that pushes clips straight to the tts socket
  mkdir -p "$HOME/.claude"
  [ -f "$HOOK_DST" ] && [ ! -f "$HOOK_DST.legacy.bak" ] && cp "$HOOK_DST" "$HOOK_DST.legacy.bak"
  install -m 755 "$REPO/hooks/claude-tts-hook.sh" "$HOOK_DST"
fi

# Wait for runsvdir to notice new dirs
sleep 6

# 4. Enable + restart
export SVDIR
for svc in mpv mpv-tts mpv-mcp; do sv-enable "$svc" >/dev/null 2>&1 || true; done
[ "$RELAY_INSTALLED" = "1" ] && sv-enable agent-audio-relay >/dev/null 2>&1 || true
sv restart mpv-mcp >/dev/null 2>&1 || true
[ "$RELAY_INSTALLED" = "1" ] && sv restart agent-audio-relay >/dev/null 2>&1 || true

sleep 3
echo "[mpv-mcp] status:"
sv status mpv mpv-tts mpv-mcp ${RELAY_INSTALLED:+agent-audio-relay} 2>/dev/null || true
echo "[mpv-mcp] done. UI: http://$(ifconfig 2>/dev/null | awk '/inet 100\./{print $2; exit}'):8765/"
