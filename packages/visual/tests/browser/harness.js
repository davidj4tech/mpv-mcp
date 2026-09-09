// Headless verification harness for the canvas client JS (#137 #142 #146).
// Runs a throwaway canvas instance on 127.0.0.1, fronts it with a stallable TCP
// proxy (the only way to reproduce a *silently* stalled SSE stream), and drives
// the real client JS with Playwright chromium. Never touches the live wall
// service. See README.md in this directory for setup and safety notes.
//
// The page is a picture with a caption now — the controls, the agent tree, the
// transcript and the reply box moved to Sasonica — so what is left to verify is
// the connection and the theme: the SSE stream and its self-heal, the server's
// load shedding, e-ink legibility, and the one button the page kept.
//
// Point MEDIA_HARNESS_SRC at a worktree's packages/visual/src to test a branch
// without reinstalling; defaults to this repo's source tree.
'use strict';
const { spawn } = require('child_process');
const net = require('net');
const http = require('http');
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const REPO = path.resolve(__dirname, '..', '..', '..', '..');
const SRC = process.env.MEDIA_HARNESS_SRC || path.join(REPO, 'packages', 'visual', 'src');
const PY = path.join(REPO, '.venv', 'bin', 'python');
const SRV_PORT = Number(process.env.MEDIA_HARNESS_PORT || 8791);
const PROXY_PORT = Number(process.env.MEDIA_HARNESS_PROXY_PORT || 8792);
const SHOTS = path.join(__dirname, 'shots');
fs.mkdirSync(SHOTS, { recursive: true });

const results = [];
function rec(name, pass, detail) {
  results.push({ name, pass, detail });
  console.log(`${pass ? 'PASS' : 'FAIL'}  ${name}${detail ? '  — ' + detail : ''}`);
}
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

// ---- throwaway server ------------------------------------------------------
let srv = null;
function startServer() {
  srv = spawn(PY, ['-m', 'agent_media_visual.canvas'], {
    cwd: REPO,
    env: {
      ...process.env,
      // PYTHONPATH beats the venv's editable install, so SRC can be any branch.
      PYTHONPATH: SRC,
      MEDIA_VISUAL_BIND: '127.0.0.1',
      MEDIA_VISUAL_PORT: String(SRV_PORT),
      MEDIA_VISUAL_TRUST_TAILNET: '1',
      MEDIA_VISUAL_VIDEO: '0',
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  srv.stdout.on('data', d => process.stdout.write('[srv] ' + d));
  srv.stderr.on('data', d => process.stdout.write('[srv!] ' + d));
}
function httpGet(port, p, timeoutMs = 4000) {
  return new Promise((resolve) => {
    const req = http.get({ host: '127.0.0.1', port, path: p, timeout: timeoutMs }, (res) => {
      let body = '';
      res.on('data', c => body += c);
      res.on('end', () => resolve({ status: res.statusCode, body }));
    });
    req.on('error', () => resolve(null));
    req.on('timeout', () => { req.destroy(); resolve(null); });
  });
}
async function waitServer(up = true, budget = 15000) {
  const t0 = Date.now();
  while (Date.now() - t0 < budget) {
    const r = await httpGet(SRV_PORT, '/status', 1500);
    if (up ? r && r.status === 200 : !r) return true;
    await sleep(400);
  }
  return false;
}

// ---- stallable proxy: browser :PROXY_PORT -> server :SRV_PORT ---------------
let stalled = false;
const pipes = new Set();
const proxy = net.createServer((c) => {
  const b = net.connect(SRV_PORT, '127.0.0.1');
  const pair = { c, b };
  pipes.add(pair);
  c.on('error', () => {});
  b.on('error', () => { c.destroy(); pipes.delete(pair); });
  c.pipe(b); b.pipe(c);
  if (stalled) { c.pause(); b.pause(); }
  const clean = () => { pipes.delete(pair); c.destroy(); b.destroy(); };
  c.on('close', clean); b.on('close', clean);
});
function stall(on) {
  stalled = on;
  for (const { c, b } of pipes) { if (on) { c.pause(); b.pause(); } else { c.resume(); b.resume(); } }
}

(async () => {
  startServer();
  if (!await waitServer(true)) { console.error('server never came up'); process.exit(1); }
  await new Promise(r => proxy.listen(PROXY_PORT, '127.0.0.1', r));
  console.log('rig up: server :' + SRV_PORT + ', proxy :' + PROXY_PORT + ', src ' + SRC);

  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });

  // Instrument EventSource + native prompt before any page JS runs.
  await page.addInitScript(() => {
    window.__esCount = 0; window.__pings = 0; window.__msgs = 0;
    const Orig = window.EventSource;
    window.EventSource = function (url, opts) {
      window.__esCount++;
      const es = new Orig(url, opts);
      es.addEventListener('message', (e) => {
        window.__msgs++;
        try { if (JSON.parse(e.data).kind === 'ping') window.__pings++; } catch (_) {}
      });
      return es;
    };
    window.EventSource.prototype = Orig.prototype;
    // Watch the orientation lock without replacing it: the real call still
    // runs (and, on a desktop chromium, still refuses), which is exactly the
    // case T17d is about — a refused rotation must not cost the fullscreen.
    window.__orient = { lock: [], unlock: 0, rejected: 0 };
    try {
      const o = screen.orientation, ol = o.lock.bind(o), ou = o.unlock.bind(o);
      o.lock = (kind) => {
        window.__orient.lock.push(kind);
        return ol(kind).catch((e) => { window.__orient.rejected++; throw e; });
      };
      o.unlock = () => { window.__orient.unlock++; return ou(); };
    } catch (_) {}
  });

  // The page no longer posts /input or /ctl — replying and playback control
  // moved to Sasonica. The blocks stay as a standing safety net: with
  // TRUST_TAILNET=1 a real POST /input would inject keystrokes into live tmux
  // `claude` panes, and that must never be one accidental line of test code
  // away.
  await page.route('**/input', (route) => route.abort());
  await page.route('**/ctl', (route) => route.abort());

  // ---- T1: load + SSE connect ----------------------------------------------
  await page.goto(`http://127.0.0.1:${PROXY_PORT}/`, { waitUntil: 'domcontentloaded' });
  try {
    await page.waitForFunction(() => !document.getElementById('dot').classList.contains('off'), null, { timeout: 10000 });
    rec('T1 SSE connects (dot on)', true, `esCount=${await page.evaluate(() => window.__esCount)}`);
  } catch { rec('T1 SSE connects (dot on)', false, 'dot stayed .off'); }
  await page.screenshot({ path: SHOTS + '/01-baseline.png' });

  // ---- T2: heartbeat / liveness frames (#137 server half) -------------------
  // The ping only fires after 15s of a *quiet* event queue; when the house is
  // actively speaking, state frames flow instead — either kind stamps
  // lastEventTs and feeds the watchdog, which is the actual requirement.
  {
    const m0 = await page.evaluate(() => window.__msgs);
    let pings = 0, m1 = m0;
    const t0 = Date.now();
    while (Date.now() - t0 < 40000) {
      await sleep(2000);
      pings = await page.evaluate(() => window.__pings);
      m1 = await page.evaluate(() => window.__msgs);
      if (pings >= 1) break;
    }
    rec('T2 SSE liveness frames stamp watchdog', pings >= 1 || m1 > m0,
      pings >= 1 ? `ping heartbeat seen (pings=${pings})`
                 : `no idle window for a ping, but ${m1 - m0} state frames flowed (watchdog fed)`);
  }

  // ---- T10: kill server -> offbar + dim; restart -> self-heal ---------------
  {
    srv.kill('SIGKILL');
    await waitServer(false, 8000);
    let dotOff = false, barOn = false;
    try {
      await page.waitForFunction(() => document.getElementById('dot').classList.contains('off'), null, { timeout: 8000 });
      dotOff = true;
      await page.waitForFunction(() => document.getElementById('offbar').classList.contains('on'), null, { timeout: 15000 });
      barOn = true;
    } catch {}
    await page.screenshot({ path: SHOTS + '/03-offbar.png' });
    rec('T10a server dead -> dot off + reconnect banner', dotOff && barOn, `dot=${dotOff} offbar=${barOn}`);
    startServer();
    await waitServer(true);
    let healed = false;
    try {
      await page.waitForFunction(() =>
        !document.getElementById('offbar').classList.contains('on') &&
        !document.getElementById('dot').classList.contains('off'), null, { timeout: 25000 });
      healed = true;
    } catch {}
    await page.screenshot({ path: SHOTS + '/04-healed.png' });
    rec('T10b restart -> banner clears, reconnected', healed);
  }

  // ---- T11: silent stall -> watchdog reconnect (#137 client half) -----------
  {
    const esBefore = await page.evaluate(() => window.__esCount);
    stall(true);
    let watchdogFired = false, barOn = false;
    const t0 = Date.now();
    while (Date.now() - t0 < 80000) {
      await sleep(3000);
      const es = await page.evaluate(() => window.__esCount);
      barOn = await page.evaluate(() => document.getElementById('offbar').classList.contains('on'));
      if (es > esBefore && barOn) { watchdogFired = true; break; }
    }
    await page.screenshot({ path: SHOTS + '/05-stalled.png' });
    rec('T11a stalled stream -> watchdog reconnects + banner', watchdogFired,
      `esCount ${esBefore} -> ${await page.evaluate(() => window.__esCount)} after ${Math.round((Date.now() - t0) / 1000)}s, offbar=${barOn}`);
    stall(false);
    let healed = false;
    try {
      await page.waitForFunction(() =>
        !document.getElementById('offbar').classList.contains('on') &&
        !document.getElementById('dot').classList.contains('off'), null, { timeout: 35000 });
      healed = true;
    } catch {}
    await page.screenshot({ path: SHOTS + '/06-unstalled.png' });
    rec('T11b unstall -> stream heals, banner clears', healed);
  }

  // ---- T12: MAX_SSE_CLIENTS cap -> 503 (#137 server half) --------------------
  {
    const held = [];
    let got503 = false, accepted = 0;
    for (let i = 0; i < 70 && !got503; i++) {
      const status = await new Promise((resolve) => {
        const req = http.get({ host: '127.0.0.1', port: SRV_PORT, path: '/events' }, (res) => {
          held.push(req); resolve(res.statusCode);
        });
        req.on('error', () => resolve(0));
      });
      if (status === 503) got503 = true;
      else if (status === 200) accepted++;
      else break;
    }
    for (const r of held) r.destroy();
    rec('T12 SSE client cap sheds load (503)', got503 && accepted <= 64, `accepted ${accepted} extra streams before 503 (page holds ~1; cap 64)`);
  }

  // ---- T14/T15: e-ink theme + solid toast (#146) -----------------------------
  {
    await page.goto(`http://127.0.0.1:${PROXY_PORT}/?eink=1`, { waitUntil: 'domcontentloaded' });
    await sleep(1500);
    const einkOn = await page.evaluate(() => document.documentElement.classList.contains('eink'));
    await page.screenshot({ path: SHOTS + '/07-eink.png' });
    rec('T14 e-ink theme applies (screenshot for eyeballing)', einkOn);
    // The toast is the only overlay left with words in it, so put one up:
    // T15 reads its computed style, and the shot shows it on the white page.
    await page.evaluate(() => document.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'c', bubbles: true })));
    await sleep(400);
    await page.screenshot({ path: SHOTS + '/08-eink-toast.png' });
    // Toast must be a solid black-on-white pill in DU4 — no alpha, blur, shadow.
    const ts = await page.evaluate(() => {
      const s = getComputedStyle(document.getElementById('toast'));
      return { bg: s.backgroundColor, color: s.color, bw: s.borderTopWidth, blur: s.backdropFilter, shadow: s.boxShadow };
    });
    const toastSolid = ts.bg === 'rgb(255, 255, 255)' && ts.color === 'rgb(0, 0, 0)' &&
      ts.bw === '2px' && ts.blur === 'none' && ts.shadow === 'none';
    rec('T15 e-ink toast is solid + legible', toastSolid, JSON.stringify(ts));
  }

  // ---- T17: the fullscreen button ------------------------------------------
  // The page's one control. It rests invisible (and untappable with it, so a
  // corner of the picture does not quietly stop being the picture), appears on
  // any sign of a person, and actually takes the document in and out.
  {
    await page.evaluate(() => localStorage.setItem('eink', '0'));
    await page.goto(`http://127.0.0.1:${PROXY_PORT}/`, { waitUntil: 'domcontentloaded' });
    await sleep(800);
    const cls = () => page.evaluate(() => {
      const b = document.getElementById('full');
      return b ? { on: b.classList.contains('on'),
                   pe: getComputedStyle(b).pointerEvents } : null;
    });
    const shown = await cls();
    await sleep(4500);                       // the reveal is a four-second fade
    const faded = await cls();
    rec('T17a button shows once, then rests invisible + untappable',
      !!shown && shown.on && !!faded && !faded.on && faded.pe === 'none',
      JSON.stringify({ shown, faded }));

    await page.mouse.click(640, 300); await sleep(300);   // bare canvas
    const woken = await cls();
    rec('T17b a tap on the picture reveals it', !!woken && woken.on, JSON.stringify(woken));

    await page.click('#full'); await sleep(600);
    const inFs = await page.evaluate(() => ({
      fs: !!document.fullscreenElement,
      body: document.body.classList.contains('fullscreen'),
      icon: getComputedStyle(document.querySelector('#full .fs-out')).display,
      toast: document.getElementById('toast').textContent }));
    await page.screenshot({ path: SHOTS + '/09-fullscreen.png' });
    await page.click('#full'); await sleep(600);
    const out = await page.evaluate(() => ({
      fs: !!document.fullscreenElement,
      body: document.body.classList.contains('fullscreen') }));
    rec('T17c it takes the document in and back out',
      inFs.fs && inFs.body && inFs.icon !== 'none' && !inFs.toast && !out.fs && !out.body,
      JSON.stringify({ inFs, out }));

    // The pictures are wide, so fullscreen asks for landscape on the way in
    // and hands the rotation back on the way out. A desktop chromium refuses
    // the lock — the point of checking here is that the refusal cost nothing:
    // T17c above ran on the very fullscreen this rejection happened inside.
    const orient = await page.evaluate(() => window.__orient);
    rec('T17d fullscreen asks for landscape; a refusal costs it nothing',
      orient.lock.length >= 1 && orient.lock.every(k => k === 'landscape') &&
      orient.unlock >= 1 && inFs.fs,
      JSON.stringify(orient));

    // ...but not on e-ink. Rotating the whole screen is the largest movement
    // there is, and this page's whole e-ink posture is that DU4 must not move.
    // Fullscreen itself still works there — only the rotation is withheld.
    await page.goto(`http://127.0.0.1:${PROXY_PORT}/?eink=1`, { waitUntil: 'domcontentloaded' });
    await sleep(800);
    await page.click('#full'); await sleep(600);
    const ink = await page.evaluate(() => ({
      eink: document.documentElement.classList.contains('eink'),
      fs: !!document.fullscreenElement,
      locks: window.__orient.lock.length }));
    await page.evaluate(() => document.exitFullscreen()).catch(() => {});
    await page.evaluate(() => localStorage.setItem('eink', '0'));
    rec('T17e e-ink goes fullscreen without rotating',
      ink.eink && ink.fs && ink.locks === 0, JSON.stringify(ink));
  }

  await browser.close();
  proxy.close();
  if (srv) srv.kill('SIGKILL');
  const fails = results.filter(r => !r.pass);
  fs.writeFileSync(path.join(__dirname, 'results.json'), JSON.stringify(results, null, 2));
  console.log(`\n==== ${results.length - fails.length}/${results.length} passed ====`);
  process.exit(fails.length ? 2 : 0);
})().catch(e => { console.error('HARNESS ERROR', e); if (srv) srv.kill('SIGKILL'); process.exit(3); });
