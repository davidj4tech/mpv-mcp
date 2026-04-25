#!/usr/bin/env bash
# Start mpv in idle mode with IPC socket. yt-dlp enables YouTube/etc.
SOCKET="${MPV_SOCKET:-/tmp/mpv.sock}"
rm -f "$SOCKET"
exec mpv --idle=yes --input-ipc-server="$SOCKET" --ytdl=yes --force-window=no --no-terminal "$@"
