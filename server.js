#!/usr/bin/env node
import { createServer } from "node:http";
import { connect } from "node:net";
import { randomUUID } from "node:crypto";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { z } from "zod";

const PREFIX = process.env.PREFIX || "";
const CHANNELS = {
  music: { socket: process.env.MPV_MUSIC_SOCKET || `${PREFIX}/tmp/mpv.sock`,    baseline: 60,  duck: 15  },
  tts:   { socket: process.env.MPV_TTS_SOCKET   || `${PREFIX}/tmp/mpv-tts.sock`, baseline: 100, duck: 100 },
};
const PRIMARY = "music";
const DUCKERS = ["tts"];
const PORT = Number(process.env.PORT || 8765);
const HOST = process.env.HOST || "0.0.0.0";

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

// --- Ducking ---------------------------------------------------------------
// Watch DUCKERS for idle-active. While any are non-idle, lower PRIMARY volume.
const duckers = new Set();         // set of channel names currently non-idle
let savedPrimaryVol = null;        // primary volume captured before first duck

async function applyDuck() {
  if (duckers.size === 0) {
    if (savedPrimaryVol != null) {
      const restore = savedPrimaryVol;
      savedPrimaryVol = null;
      try { await mpv(PRIMARY, ["set_property", "volume", restore]); console.log(`unduck: ${PRIMARY} -> ${restore}`); }
      catch (e) { console.error("unduck failed:", e.message); }
    }
  } else {
    if (savedPrimaryVol == null) {
      try {
        savedPrimaryVol = await mpv(PRIMARY, ["get_property", "volume"]) ?? CHANNELS[PRIMARY].baseline;
        await mpv(PRIMARY, ["set_property", "volume", CHANNELS[PRIMARY].duck]);
        console.log(`duck: ${PRIMARY} ${savedPrimaryVol} -> ${CHANNELS[PRIMARY].duck}`);
      } catch (e) { console.error("duck failed:", e.message); savedPrimaryVol = null; }
    }
  }
}

function watchDucker(name) {
  const ch = CHANNELS[name];
  let buf = "";
  const sock = connect(ch.socket);
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
          const idle = !!msg.data;
          if (idle) duckers.delete(name); else duckers.add(name);
          await applyDuck();
        }
      } catch {}
    }
  });
  const reconnect = () => { sock.destroy(); duckers.delete(name); applyDuck().catch(() => {}); setTimeout(() => watchDucker(name), 2000); };
  sock.on("close", reconnect);
  sock.on("error", reconnect);
}
DUCKERS.forEach(watchDucker);

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

const UI_HTML = `<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>mpv</title>
<style>
  body{font:16px system-ui;margin:0;background:#111;color:#eee;padding:1em;max-width:560px;margin:auto}
  h1{font-size:1.1em;margin:0 0 .8em}
  .ch{background:#1a1a1a;padding:.8em;border-radius:8px;margin-bottom:1em;border:1px solid #333}
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
</style></head><body>
<h1>mpv remote</h1>

<div class=ch id=ch-music>
  <h2>music <span class=duck id=duck-music style=display:none>ducked</span></h2>
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
  <div class=bar><label>Vol</label><input id=vol-music type=range min=0 max=130 value=60 oninput="setVol('music',this.value)"><span id=volv-music>60</span></div>
</div>

<div class=ch id=ch-tts>
  <h2>tts</h2>
  <div class=now id=now-tts>—</div>
  <div class=row>
    <button id=pp-tts onclick="togglePause('tts')">⏯</button>
    <button onclick="act('tts','stop')">Stop</button>
  </div>
  <div class=bar><label>Vol</label><input id=vol-tts type=range min=0 max=130 value=100 oninput="setVol('tts',this.value)"><span id=volv-tts>100</span></div>
</div>

<script>
const paused={music:false,tts:false};
async function api(path,body){
  const r=await fetch(path,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body||{})});
  return r.json();
}
async function act(channel,name,args){await api('/api/cmd',{channel,name,args:args||{}});refresh()}
async function togglePause(ch){await api('/api/cmd',{channel:ch,name:paused[ch]?'resume':'pause'});refresh()}
async function setVol(ch,v){document.getElementById('volv-'+ch).textContent=v;await api('/api/cmd',{channel:ch,name:'volume',args:{level:Number(v)}})}
function fmt(s){if(s==null)return '-';s=Math.floor(s);return Math.floor(s/60)+':'+String(s%60).padStart(2,'0')}
async function refresh(){
  const j=await api('/api/state');
  for(const ch of Object.keys(j.channels||{})){
    const d=j.channels[ch];
    paused[ch]=!!d.paused;
    const pp=document.getElementById('pp-'+ch); if(pp)pp.textContent=paused[ch]?'▶':'⏸';
    const t=d.title||'(idle)';
    document.getElementById('now-'+ch).textContent=t+'\\n'+fmt(d.pos)+' / '+fmt(d.dur);
    if(d.vol!=null){document.getElementById('vol-'+ch).value=Math.round(d.vol);document.getElementById('volv-'+ch).textContent=Math.round(d.vol)}
  }
  const dm=document.getElementById('duck-music'); dm.style.display=j.ducked?'inline':'none';
}
setInterval(refresh,2000);refresh();
</script></body></html>`;

async function snapshotChannel(name) {
  const [title, path, pos, dur, paused, vol] = await Promise.all([
    mpv(name, ["get_property", "media-title"]).catch(() => null),
    mpv(name, ["get_property", "path"]).catch(() => null),
    mpv(name, ["get_property", "time-pos"]).catch(() => null),
    mpv(name, ["get_property", "duration"]).catch(() => null),
    mpv(name, ["get_property", "pause"]).catch(() => null),
    mpv(name, ["get_property", "volume"]).catch(() => null),
  ]);
  return { title, path, pos, dur, paused, vol };
}

async function handleApi(req, res, body) {
  if (req.url === "/api/state") {
    const out = { channels: {}, ducked: savedPrimaryVol != null };
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
    };
    if (!m[name]) { res.writeHead(400); res.end("unknown"); return; }
    await m[name]();
    res.writeHead(200, { "content-type": "application/json" }); res.end('{"ok":true}');
    return;
  }
  res.writeHead(404); res.end();
}

const http = createServer(async (req, res) => {
  if (req.method === "GET" && req.url === "/health") { res.writeHead(200); res.end("ok"); return; }
  if (req.method === "GET" && (req.url === "/" || req.url === "/index.html")) {
    res.writeHead(200, { "content-type": "text/html; charset=utf-8" }); res.end(UI_HTML); return;
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
