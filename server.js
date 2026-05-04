#!/usr/bin/env node
import { createServer } from "node:http";
import { connect } from "node:net";
import { randomUUID } from "node:crypto";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { z } from "zod";

const PREFIX = process.env.PREFIX || "";
const CHANNELS = {
  music: { socket: process.env.MPV_MUSIC_SOCKET || `${PREFIX}/tmp/mpv-music.sock`, baseline: 50  },
  voice: { socket: process.env.MPV_VOICE_SOCKET || `${PREFIX}/tmp/mpv-voice.sock`, baseline: 85  },
  tts:   { socket: process.env.MPV_TTS_SOCKET   || `${PREFIX}/tmp/mpv-tts.sock`,   baseline: 100 },
};
const PORT = Number(process.env.PORT || 8765);
const HOST = process.env.HOST || "0.0.0.0";
const TTS_LATEST = process.env.TTS_LATEST_PATH
  || `${process.env.HOME}/.cache/agent-audio/latest.mp3`;

function mpv(channel, cmd) {
  const ch = CHANNELS[channel];
  if (!ch) return Promise.reject(new Error(`unknown channel: ${channel}`));
  return new Promise((resolve, reject) => {
    const sock = connect(ch.socket);
    const id = Math.floor(Math.random() * 1e9);
    let buf = "";
    const timer = setTimeout(() => { sock.destroy(); reject(new Error(`mpv ${channel} IPC timeout`)); }, 5000);
    sock.on("connect", () => sock.write(JSON.stringify({ command: cmd, request_id: id }) + "\n"));
    sock.on("data", (d) => {
      buf += d.toString();
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl); buf = buf.slice(nl + 1);
        if (!line) continue;
        try {
          const msg = JSON.parse(line);
          if (msg.request_id === id) {
            clearTimeout(timer); sock.end();
            if (msg.error && msg.error !== "success") reject(new Error(msg.error));
            else resolve(msg.data);
            return;
          }
        } catch {}
      }
    });
    sock.on("error", (e) => { clearTimeout(timer); reject(e); });
  });
}

// --- TTS-pauses-voice ------------------------------------------------------
// When tts goes non-idle, pause voice; when tts returns to idle, resume voice
// only if we paused it (don't override a user pause).
let voicePausedByUs = false;
let pauseLock = Promise.resolve();

function applyVoicePause(ttsActive) {
  pauseLock = pauseLock.then(async () => {
    try {
      if (ttsActive) {
        const paused = await mpv("voice", ["get_property", "pause"]).catch(() => null);
        if (paused === false) {
          await mpv("voice", ["set_property", "pause", true]);
          voicePausedByUs = true;
          console.log("pause: voice (tts active)");
        }
      } else if (voicePausedByUs) {
        await mpv("voice", ["set_property", "pause", false]);
        voicePausedByUs = false;
        console.log("resume: voice (tts idle)");
      }
    } catch (e) { console.error("voice pause/resume failed:", e.message); }
  }).catch(() => {});
  return pauseLock;
}

function watchTtsForVoicePause() {
  let buf = "";
  const sock = connect(CHANNELS.tts.socket);
  sock.on("connect", () => {
    sock.write(JSON.stringify({ command: ["observe_property", 1, "idle-active"] }) + "\n");
  });
  sock.on("data", async (d) => {
    buf += d.toString();
    let nl;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, nl); buf = buf.slice(nl + 1);
      if (!line) continue;
      try {
        const msg = JSON.parse(line);
        if (msg.event === "property-change" && msg.name === "idle-active") {
          await applyVoicePause(!msg.data);
        }
      } catch {}
    }
  });
  const reconnect = () => { sock.destroy(); setTimeout(watchTtsForVoicePause, 2000); };
  sock.on("close", reconnect);
  sock.on("error", reconnect);
}
watchTtsForVoicePause();

// --- MCP tools -------------------------------------------------------------
const ok = (text) => ({ content: [{ type: "text", text }] });
const ChannelArg = z.enum(Object.keys(CHANNELS)).optional();

function makeServer() {
  const s = new McpServer({ name: "mpv-mcp", version: "0.2.0" });

  s.registerTool("play", {
    description: "Load and play URL/path on a channel (default music). Replaces current.",
    inputSchema: { url: z.string(), channel: ChannelArg },
  }, async ({ url, channel = "music" }) => {
    await mpv(channel, ["loadfile", url, "replace"]);
    await mpv(channel, ["set_property", "pause", false]);
    return ok(`[${channel}] Playing: ${url}`);
  });

  s.registerTool("queue", {
    description: "Append URL/path to a channel's playlist.",
    inputSchema: { url: z.string(), channel: ChannelArg },
  }, async ({ url, channel = "music" }) => { await mpv(channel, ["loadfile", url, "append-play"]); return ok(`[${channel}] Queued: ${url}`); });

  s.registerTool("pause",  { description: "Pause channel.",  inputSchema: { channel: ChannelArg } },
    async ({ channel = "music" }) => { await mpv(channel, ["set_property", "pause", true]);  return ok(`[${channel}] Paused`); });
  s.registerTool("resume", { description: "Resume channel.", inputSchema: { channel: ChannelArg } },
    async ({ channel = "music" }) => { await mpv(channel, ["set_property", "pause", false]); return ok(`[${channel}] Resumed`); });
  s.registerTool("stop",   { description: "Stop and clear playlist.", inputSchema: { channel: ChannelArg } },
    async ({ channel = "music" }) => { await mpv(channel, ["stop"]); return ok(`[${channel}] Stopped`); });
  s.registerTool("skip",   { description: "Next track.",     inputSchema: { channel: ChannelArg } },
    async ({ channel = "music" }) => { await mpv(channel, ["playlist-next"]); return ok(`[${channel}] Skipped`); });
  s.registerTool("prev",   { description: "Previous track.", inputSchema: { channel: ChannelArg } },
    async ({ channel = "music" }) => { await mpv(channel, ["playlist-prev"]); return ok(`[${channel}] Prev`); });

  s.registerTool("seek", {
    description: "Seek; default relative seconds, absolute=true for absolute.",
    inputSchema: { seconds: z.number(), absolute: z.boolean().optional(), channel: ChannelArg },
  }, async ({ seconds, absolute, channel = "music" }) => {
    await mpv(channel, ["seek", seconds, absolute ? "absolute" : "relative"]);
    return ok(`[${channel}] Seek ${absolute ? "to" : "by"} ${seconds}s`);
  });

  s.registerTool("volume", {
    description: "Set volume 0-130 on a channel.",
    inputSchema: { level: z.number().min(0).max(130), channel: ChannelArg },
  }, async ({ level, channel = "music" }) => { await mpv(channel, ["set_property", "volume", level]); return ok(`[${channel}] Volume ${level}`); });

  s.registerTool("now_playing", {
    description: "Current track info on a channel.",
    inputSchema: { channel: ChannelArg },
  }, async ({ channel = "music" }) => {
    const [title, path, pos, dur, paused, vol] = await Promise.all([
      mpv(channel, ["get_property", "media-title"]).catch(() => null),
      mpv(channel, ["get_property", "path"]).catch(() => null),
      mpv(channel, ["get_property", "time-pos"]).catch(() => null),
      mpv(channel, ["get_property", "duration"]).catch(() => null),
      mpv(channel, ["get_property", "pause"]).catch(() => null),
      mpv(channel, ["get_property", "volume"]).catch(() => null),
    ]);
    return ok(JSON.stringify({ channel, title, path, pos, dur, paused, vol }, null, 2));
  });

  return s;
}

// --- HTTP / UI -------------------------------------------------------------
const transports = {};
function isInit(body) { return body && (body.method === "initialize" || (Array.isArray(body) && body.some(m => m.method === "initialize"))); }
async function readBody(req) {
  return new Promise((resolve) => {
    let s = "";
    req.on("data", (c) => s += c);
    req.on("end", () => resolve(s ? JSON.parse(s) : undefined));
  });
}

const UI_HTML = `<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>mpv</title>
<link rel=manifest href=/manifest.webmanifest>
<meta name=theme-color content="#111111">
<meta name=apple-mobile-web-app-capable content=yes>
<meta name=apple-mobile-web-app-status-bar-style content=black-translucent>
<meta name=apple-mobile-web-app-title content="mpv">
<link rel=apple-touch-icon href=/icon.svg>
<link rel=icon type=image/svg+xml href=/icon.svg>
<style>
  body{font:16px system-ui;margin:0;background:#111;color:#eee;padding:1em;max-width:560px;margin:auto}
  h1{font-size:1.1em;margin:0 0 .8em;display:flex;justify-content:space-between;align-items:baseline}
  h1 .hint{font-size:.7em;color:#888;font-weight:normal}
  .ch{background:#1a1a1a;padding:.8em;border-radius:8px;margin-bottom:1em;border:1px solid #333;cursor:pointer}
  .ch.focus{border-color:#9cf;box-shadow:0 0 0 1px #9cf inset}
  .ch h2{margin:0 0 .4em;font-size:.9em;color:#9cf;display:flex;justify-content:space-between;align-items:center}
  .duck{font-size:.7em;background:#532;color:#fc8;padding:.1em .5em;border-radius:4px}
  .now{background:#222;padding:.6em;border-radius:6px;min-height:2.6em;margin-bottom:.6em;font-size:.85em;white-space:pre-wrap;word-break:break-all}
  .row{display:flex;gap:.4em;margin-bottom:.5em;flex-wrap:wrap}
  button{flex:1;min-width:56px;padding:.7em;font-size:.95em;background:#333;color:#eee;border:0;border-radius:6px;cursor:pointer}
  button:active{background:#555}
  input[type=text]{flex:3;padding:.6em;background:#222;color:#eee;border:1px solid #444;border-radius:6px;font-size:.95em}
  input[type=range]{flex:1;width:100%}
  label{font-size:.8em;color:#aaa;min-width:2em}
  .bar{display:flex;align-items:center;gap:.5em}
  .keys{font-size:.7em;color:#888;margin-top:.6em;line-height:1.5}
  kbd{background:#222;border:1px solid #444;border-radius:3px;padding:0 .3em;font-family:ui-monospace,monospace;color:#ccc}
</style></head><body>
<h1>mpv remote <span class=hint id=focus-hint>music</span></h1>

<div class=ch id=ch-music>
  <h2>music</h2>
  <div class=now id=now-music>—</div>
  <div class=row><input id=url type=text placeholder="URL or path" autocomplete=off><button onclick="act('music','play',{url:url.value})">Play</button><button onclick="act('music','queue',{url:url.value})">Queue</button></div>
  <div class=row>
    <button onclick="act('music','prev')">⏮</button>
    <button onclick="act('music','seek',{seconds:-10})">-10s</button>
    <button id=pp-music onclick="togglePause('music')">⏯</button>
    <button onclick="act('music','seek',{seconds:10})">+10s</button>
    <button onclick="act('music','skip')">⏭</button>
  </div>
  <div class=row><button onclick="act('music','stop')">Stop</button></div>
  <div class=bar><label>Vol</label><input id=vol-music type=range min=0 max=130 value=50 oninput="setVol('music',this.value)"><span id=volv-music>50</span></div>
</div>

<div class=ch id=ch-voice>
  <h2>voice</h2>
  <div class=now id=now-voice>—</div>
  <div class=row><input id=url-voice type=text placeholder="URL or path" autocomplete=off><button onclick="act('voice','play',{url:document.getElementById('url-voice').value})">Play</button><button onclick="act('voice','queue',{url:document.getElementById('url-voice').value})">Queue</button></div>
  <div class=row>
    <button onclick="act('voice','prev')">⏮</button>
    <button onclick="act('voice','seek',{seconds:-30})">-30s</button>
    <button id=pp-voice onclick="togglePause('voice')">⏯</button>
    <button onclick="act('voice','seek',{seconds:30})">+30s</button>
    <button onclick="act('voice','skip')">⏭</button>
  </div>
  <div class=row><button onclick="act('voice','stop')">Stop</button></div>
  <div class=bar><label>Vol</label><input id=vol-voice type=range min=0 max=130 value=85 oninput="setVol('voice',this.value)"><span id=volv-voice>85</span></div>
</div>

<div class=ch id=ch-tts>
  <h2>tts</h2>
  <div class=now id=now-tts>—</div>
  <div class=row>
    <button onclick="act('tts','seek',{seconds:-5})">-5s</button>
    <button id=pp-tts onclick="togglePause('tts')">⏯</button>
    <button onclick="act('tts','seek',{seconds:5})">+5s</button>
    <button onclick="act('tts','play_latest')">Latest</button>
    <button onclick="act('tts','stop')">Stop</button>
  </div>
  <div class=bar><label>Vol</label><input id=vol-tts type=range min=0 max=130 value=100 oninput="setVol('tts',this.value)"><span id=volv-tts>100</span></div>
</div>

<div class=keys>
  <kbd>Space</kbd>/<kbd>k</kbd> play/pause
  &nbsp; <kbd>←</kbd>/<kbd>→</kbd> ±5s
  &nbsp; <kbd>Shift</kbd>+<kbd>←</kbd>/<kbd>→</kbd> ±1s
  &nbsp; <kbd>↑</kbd>/<kbd>↓</kbd> volume
  &nbsp; <kbd>m</kbd> mute
  &nbsp; <kbd>&lt;</kbd>/<kbd>&gt;</kbd> prev/next
  &nbsp; <kbd>[</kbd>/<kbd>]</kbd> speed
  &nbsp; <kbd>BS</kbd> reset speed
  &nbsp; <kbd>Tab</kbd> switch channel
</div>

<script>
const CHS=['music','voice','tts'];
const paused=Object.fromEntries(CHS.map(c=>[c,false]));
const idle=Object.fromEntries(CHS.map(c=>[c,false]));
async function api(path,body){
  const r=await fetch(path,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body||{})});
  return r.json();
}
async function act(channel,name,args){await api('/api/cmd',{channel,name,args:args||{}});refresh()}
async function togglePause(ch){
  // If the channel has no file loaded (idle), tapping ⏯ should start the
  // latest TTS clip rather than no-op pause-cycling.
  if(idle[ch] && ch==='tts'){
    await api('/api/cmd',{channel:ch,name:'play_latest'});
  }else{
    await api('/api/cmd',{channel:ch,name:paused[ch]?'resume':'pause'});
  }
  refresh();
}
async function setVol(ch,v){document.getElementById('volv-'+ch).textContent=v;await api('/api/cmd',{channel:ch,name:'volume',args:{level:Number(v)}})}
function fmt(s){if(s==null)return '-';s=Math.floor(s);return Math.floor(s/60)+':'+String(s%60).padStart(2,'0')}

// --- Channel focus + keyboard control --------------------------------------
let focused='music';
function setFocus(ch){
  focused=ch;
  document.getElementById('focus-hint').textContent=ch;
  for(const c of CHS){
    document.getElementById('ch-'+c).classList.toggle('focus',c===ch);
  }
}
for(const ch of CHS){
  document.getElementById('ch-'+ch).addEventListener('click',e=>{
    if(e.target.closest('button,input')) return;
    setFocus(ch);
  });
}
setFocus('music');

// mpv-style keyboard shortcuts. Skip when typing in the URL field.
document.addEventListener('keydown',ev=>{
  if(ev.target.tagName==='INPUT'||ev.target.tagName==='TEXTAREA') return;
  if(ev.ctrlKey||ev.metaKey||ev.altKey) return;
  const ch=focused;
  const k=ev.key;
  let h=true;
  if(k===' '||k==='k'||k==='p'){togglePause(ch);}
  else if(k==='ArrowLeft'){act(ch,'seek',{seconds:ev.shiftKey?-1:-5});}
  else if(k==='ArrowRight'){act(ch,'seek',{seconds:ev.shiftKey?1:5});}
  else if(k==='ArrowUp'){api('/api/cmd',{channel:ch,name:'volume_delta',args:{delta:2}}).then(refresh);}
  else if(k==='ArrowDown'){api('/api/cmd',{channel:ch,name:'volume_delta',args:{delta:-2}}).then(refresh);}
  else if(k==='m'){api('/api/cmd',{channel:ch,name:'mute_toggle'}).then(refresh);}
  else if(k==='<'){act(ch,'prev');}
  else if(k==='>'){act(ch,'skip');}
  else if(k==='['){api('/api/cmd',{channel:ch,name:'speed_delta',args:{delta:-0.1}}).then(refresh);}
  else if(k===']'){api('/api/cmd',{channel:ch,name:'speed_delta',args:{delta:0.1}}).then(refresh);}
  else if(k==='Backspace'){api('/api/cmd',{channel:ch,name:'speed_set',args:{value:1}}).then(refresh);}
  else if(k==='Tab'){const i=CHS.indexOf(ch);setFocus(CHS[(i+1)%CHS.length]);}
  else if(k==='9'){api('/api/cmd',{channel:ch,name:'volume_delta',args:{delta:-2}}).then(refresh);}
  else if(k==='0'){api('/api/cmd',{channel:ch,name:'volume_delta',args:{delta:2}}).then(refresh);}
  else h=false;
  if(h){ev.preventDefault();}
});
async function refresh(){
  const j=await api('/api/state');
  for(const ch of Object.keys(j.channels||{})){
    const d=j.channels[ch];
    paused[ch]=!!d.paused;
    idle[ch]=!!d.idle;
    const pp=document.getElementById('pp-'+ch);
    if(pp)pp.textContent = idle[ch] ? '▶' : (paused[ch]?'▶':'⏸');
    const t=d.title||'(idle)';
    document.getElementById('now-'+ch).textContent=t+'\\n'+fmt(d.pos)+' / '+fmt(d.dur);
    if(d.vol!=null){document.getElementById('vol-'+ch).value=Math.round(d.vol);document.getElementById('volv-'+ch).textContent=Math.round(d.vol)}
  }
}
setInterval(refresh,2000);refresh();
</script></body></html>`;

async function snapshotChannel(name) {
  const [title, path, pos, dur, paused, vol, idle] = await Promise.all([
    mpv(name, ["get_property", "media-title"]).catch(() => null),
    mpv(name, ["get_property", "path"]).catch(() => null),
    mpv(name, ["get_property", "time-pos"]).catch(() => null),
    mpv(name, ["get_property", "duration"]).catch(() => null),
    mpv(name, ["get_property", "pause"]).catch(() => null),
    mpv(name, ["get_property", "volume"]).catch(() => null),
    mpv(name, ["get_property", "idle-active"]).catch(() => null),
  ]);
  return { title, path, pos, dur, paused, vol, idle };
}

async function handleApi(req, res, body) {
  if (req.url === "/api/state") {
    const out = { channels: {} };
    for (const name of Object.keys(CHANNELS)) {
      out.channels[name] = await snapshotChannel(name).catch(() => ({}));
    }
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify(out));
    return;
  }
  if (req.url === "/api/cmd") {
    const { channel = "music", name, args = {} } = body || {};
    const m = {
      play:   () => mpv(channel, ["loadfile", args.url, "replace"]).then(() => mpv(channel, ["set_property", "pause", false])),
      queue:  () => mpv(channel, ["loadfile", args.url, "append-play"]),
      pause:  () => mpv(channel, ["set_property", "pause", true]),
      resume: () => mpv(channel, ["set_property", "pause", false]),
      stop:   () => mpv(channel, ["stop"]),
      skip:   () => mpv(channel, ["playlist-next"]),
      prev:   () => mpv(channel, ["playlist-prev"]),
      seek:   () => mpv(channel, ["seek", args.seconds, args.absolute ? "absolute" : "relative"]),
      volume: () => mpv(channel, ["set_property", "volume", args.level]),
      // Load the most recent agent-audio clip on the tts channel and play it.
      // Used by the UI's ⏯ button when the channel is idle.
      play_latest: () =>
        mpv(channel, ["loadfile", TTS_LATEST, "replace"])
          .then(() => mpv(channel, ["set_property", "pause", false])),
      // Keyboard-shortcut helpers used by the web UI.
      volume_delta: () => mpv(channel, ["add", "volume", args.delta]),
      mute_toggle:  () => mpv(channel, ["cycle", "mute"]),
      speed_delta:  () => mpv(channel, ["add", "speed", args.delta]),
      speed_set:    () => mpv(channel, ["set_property", "speed", args.value]),
    };
    if (!m[name]) { res.writeHead(400); res.end("unknown"); return; }
    await m[name]();
    res.writeHead(200, { "content-type": "application/json" }); res.end('{"ok":true}');
    return;
  }
  res.writeHead(404); res.end();
}

const MANIFEST = JSON.stringify({
  name: "mpv remote",
  short_name: "mpv",
  start_url: "/",
  display: "standalone",
  background_color: "#111111",
  theme_color: "#111111",
  icons: [
    { src: "/icon.svg", sizes: "any", type: "image/svg+xml", purpose: "any maskable" },
  ],
});
const ICON_SVG = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192">
<rect width="192" height="192" fill="#111"/>
<circle cx="96" cy="96" r="72" fill="#1a1a1a" stroke="#9cf" stroke-width="4"/>
<polygon points="78,64 78,128 132,96" fill="#9cf"/>
</svg>`;

const http = createServer(async (req, res) => {
  if (req.method === "GET" && req.url === "/health") { res.writeHead(200); res.end("ok"); return; }
  if (req.method === "GET" && (req.url === "/" || req.url === "/index.html")) {
    res.writeHead(200, { "content-type": "text/html; charset=utf-8" }); res.end(UI_HTML); return;
  }
  if (req.method === "GET" && req.url === "/manifest.webmanifest") {
    res.writeHead(200, { "content-type": "application/manifest+json" }); res.end(MANIFEST); return;
  }
  if (req.method === "GET" && req.url === "/icon.svg") {
    res.writeHead(200, { "content-type": "image/svg+xml", "cache-control": "max-age=86400" });
    res.end(ICON_SVG); return;
  }
  if (req.url.startsWith("/api/")) {
    try { const body = await readBody(req); await handleApi(req, res, body); }
    catch (e) { console.error(e); if (!res.headersSent) { res.writeHead(500); res.end(String(e)); } }
    return;
  }
  if (req.url !== "/mcp") { res.writeHead(404); res.end(); return; }
  try {
    const body = req.method === "POST" ? await readBody(req) : undefined;
    const sid = req.headers["mcp-session-id"];
    let transport = sid ? transports[sid] : null;
    if (!transport) {
      if (req.method !== "POST" || !isInit(body)) {
        res.writeHead(400, { "content-type": "application/json" });
        res.end(JSON.stringify({ jsonrpc: "2.0", error: { code: -32000, message: "No session" }, id: null }));
        return;
      }
      transport = new StreamableHTTPServerTransport({
        sessionIdGenerator: () => randomUUID(),
        onsessioninitialized: (id) => { transports[id] = transport; },
      });
      transport.onclose = () => { if (transport.sessionId) delete transports[transport.sessionId]; };
      const server = makeServer();
      await server.connect(transport);
    }
    await transport.handleRequest(req, res, body);
  } catch (e) {
    console.error("HTTP error:", e);
    if (!res.headersSent) { res.writeHead(500); res.end(String(e)); }
  }
});

http.listen(PORT, HOST, () => console.log(`mpv-mcp on http://${HOST}:${PORT}/mcp  channels=${Object.keys(CHANNELS).join(",")}`));
