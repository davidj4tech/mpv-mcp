#!/bin/bash
# Claude Code TTS Hook - plays through mpv-tts channel (ducks music automatically).

[ "$CLAUDE_TTS_ENABLED" = "0" ] && exit 0

EDGE_TTS="/data/data/com.termux/files/usr/bin/edge-tts"
VOICE="${CLAUDE_TTS_VOICE:-en-US-AriaNeural}"
CACHE_DIR="$HOME/.cache/claude-tts"
TTS_SOCK="${MPV_TTS_SOCKET:-$PREFIX/tmp/mpv-tts.sock}"

mkdir -p "$CACHE_DIR"

# Send a clip to the mpv-tts daemon. Replaces whatever's playing on that channel.
# Ducking is handled server-side via the mpv-mcp idle-active watcher.
play_clip() {
    local path="$1"
    [ -f "$path" ] || return 1
    if [ -S "$TTS_SOCK" ]; then
        printf '{"command":["loadfile","%s","replace"]}\n' "$path" \
            | socat - "UNIX-CONNECT:$TTS_SOCK" >/dev/null 2>&1
    else
        # Fallback: one-shot mpv if the daemon socket is missing
        mpv --really-quiet "$path" 2>/dev/null
    fi
}

input=$(cat)

# Notification events (input prompts) — short, fire immediately
notification_msg=$(echo "$input" | jq -r '.message // empty')
if [ -n "$notification_msg" ]; then
    notif_path="$CACHE_DIR/notif.mp3"
    "$EDGE_TTS" --text "$notification_msg" --voice "$VOICE" --write-media "$notif_path" 2>/dev/null || exit 0
    play_clip "$notif_path"
    exit 0
fi

transcript_path=$(echo "$input" | jq -r '.transcript_path // empty')
if [ -z "$transcript_path" ] || [ ! -f "$transcript_path" ]; then
    exit 0
fi

sleep 0.5

text=$(tac "$transcript_path" \
    | jq -s '
        [.[] | select(.message.role == "assistant")
             | select([.message.content[]? | select(.type == "text")] | length > 0)
        ][0]
        | [.message.content[]? | select(.type == "text") | .text]
        | join("\n")
    ' -r 2>/dev/null)

[ -z "$text" ] && exit 0

clean=$(echo "$text" \
    | sed 's/```[a-z]*//g; s/```//g' \
    | sed 's/^#{1,6} //g' \
    | sed 's/\*\*\([^*]*\)\*\*/\1/g' \
    | sed 's/\*\([^*]*\)\*/\1/g' \
    | sed 's/`\([^`]*\)`/\1/g' \
    | sed 's/^\s*[-*] //g' \
    | sed '/^[[:space:]]*$/d')

[ -z "$clean" ] && exit 0

clip_path="$CACHE_DIR/clip.mp3"
"$EDGE_TTS" --text "$clean" --voice "$VOICE" --write-media "$clip_path" 2>/dev/null || exit 0
play_clip "$clip_path"

exit 0
