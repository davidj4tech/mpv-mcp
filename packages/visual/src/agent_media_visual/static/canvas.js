  const $ = (id) => document.getElementById(id);
  // ---- e-ink mode: ?eink=1 arms it for this device, ?eink=0 back ----------
  const qs = new URLSearchParams(location.search);
  if (qs.has('eink')) {
    localStorage.setItem('eink', qs.get('eink') === '0' ? '0' : '1');
    history.replaceState(null, '', location.pathname);
  }
  // ---- captions: ?subs=0 arms them off for this device, ?subs=1 back on. The
  // same per-device preference the `c` key flips, settable from a URL so a
  // page that FRAMES the canvas next to a transcript (Sasonica's chat) can
  // come up without the words drawn twice.
  if (qs.has('subs')) {
    localStorage.setItem('subs', qs.get('subs') === '0' ? '0' : '1');
    history.replaceState(null, '', location.pathname);
  }
  function einkOn() { return localStorage.getItem('eink') === '1'; }
  if (einkOn()) document.documentElement.classList.add('eink');
  // ---- screen name OVERRIDE: normally the server derives this device's name
  // from its tailnet source IP (nothing to configure). ?screen=<name> pins a
  // different name once (persisted; needs pairing — an override could
  // redirect wakes); ?screen= (empty) clears it.
  if (qs.has('screen')) {
    if (qs.get('screen')) localStorage.setItem('screen', qs.get('screen'));
    else localStorage.removeItem('screen');
    history.replaceState(null, '', location.pathname);
  }
  const SCREEN = localStorage.getItem('screen') || '';
  const layers = [$('a'), $('b')];
  let front = 0, capTimer = null;
  const KB = ['kb1','kb2','kb3','kb4'];

  // ---- fit setting: auto (figures fit, art fills) · fit · fill -------------
  // cover + the Ken Burns zoom crops edges — fatal for a figure's labels on a
  // small screen. Fitted images letterbox (object-fit: contain) and skip the
  // pan/zoom (which would push the letterboxed image off-frame again). A
  // per-device preference, set by hand in localStorage or cleared with
  // ?reset=1; there is no button for it any more.
  function fitMode() { return localStorage.getItem('fit') || 'auto'; }
  function wantFit(purpose) {
    const m = fitMode();
    return m === 'fit' || (m === 'auto' && (purpose === 'figure' || purpose === 'portrait'));
  }
  function kenBurns(el) {
    if (einkOn()) return;            // motion is ghosting on e-ink
    const dur = 28 + Math.random() * 14;
    el.style.animation = KB[Math.floor(Math.random()*KB.length)] +
      ' ' + dur.toFixed(1) + 's ease-in-out infinite alternate';
    if (speaking)
      for (const a of el.getAnimations())
        (a.updatePlaybackRate ? a.updatePlaybackRate(2.6) : a.playbackRate = 2.6);
  }
  function applyFit(el, fit) {
    el.classList.toggle('fit', fit);
    if (fit) el.style.animation = 'none';
  }

  function show(d) {
    const back = 1 - front;
    const el = layers[back];
    const fit = wantFit(d.purpose);
    el.onload = () => {
      applyFit(el, fit);
      el.classList.remove('stale');   // a fresh image is never pre-dimmed
      // Ink-invertible? SVG figures are dark-bg line art — invert() turns
      // them into black-on-white; raster stays grayscale (see .eink CSS).
      el.classList.toggle('inkable', /\.svg(\?|$)/i.test(d.image || ''));
      if (!fit) kenBurns(el);
      el.classList.add('on');
      layers[front].classList.remove('on');
      front = back;
      if (d.caption) {
        $('cap').textContent = d.caption;
        $('cap').classList.add('on');
        clearTimeout(capTimer);
        capTimer = setTimeout(() => $('cap').classList.remove('on'), 15000);
      }
    };
    el.src = d.image;
  }

  // ---- sound effects: tiny synthesized cues, no assets (WebAudio) ----------
  // Whoosh when a new image lands; a two-note chime up when the voice starts,
  // down when it stops. Quiet by design; `f` toggles, state persists per
  // device. Browsers gate audio behind a first user gesture — the first tap
  // or key on the page unlocks it.
  let ac = null;
  function actx() {
    if (!ac) ac = new (window.AudioContext || window.webkitAudioContext)();
    if (ac.state === 'suspended') ac.resume().catch(() => {});
    return ac;
  }
  function sfxOn() { return localStorage.getItem('sfx') !== '0'; }
  function chime(up) {
    if (!sfxOn()) return;
    try {
      const c = actx(), notes = up ? [523, 659] : [659, 523];
      notes.forEach((f, i) => {
        const t = c.currentTime + i * 0.11;
        const o = c.createOscillator(), g = c.createGain();
        o.type = 'sine'; o.frequency.value = f;
        g.gain.setValueAtTime(0.0001, t);
        g.gain.exponentialRampToValueAtTime(0.05, t + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, t + 0.4);
        o.connect(g).connect(c.destination);
        o.start(t); o.stop(t + 0.45);
      });
    } catch (_) {}
  }
  // A figure deserves its own arrival sound: a bright three-note rise that
  // says "look at the screen", distinct from the ambient whoosh.
  function figureCue() {
    if (!sfxOn()) return;
    try {
      const c = actx();
      [523, 659, 784].forEach((f, i) => {
        const t = c.currentTime + i * 0.13;
        const o = c.createOscillator(), g = c.createGain();
        o.type = 'triangle'; o.frequency.value = f;
        g.gain.setValueAtTime(0.0001, t);
        g.gain.exponentialRampToValueAtTime(0.055, t + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, t + 0.5);
        o.connect(g).connect(c.destination);
        o.start(t); o.stop(t + 0.55);
      });
    } catch (_) {}
  }
  function whoosh() {
    if (!sfxOn()) return;
    try {
      const c = actx(), dur = 0.45;
      const buf = c.createBuffer(1, c.sampleRate * dur, c.sampleRate);
      const d = buf.getChannelData(0);
      for (let i = 0; i < d.length; i++) d[i] = Math.random() * 2 - 1;
      const src = c.createBufferSource(); src.buffer = buf;
      const f = c.createBiquadFilter(); f.type = 'bandpass'; f.Q.value = 1.2;
      const t = c.currentTime;
      f.frequency.setValueAtTime(300, t);
      f.frequency.exponentialRampToValueAtTime(1400, t + dur * 0.7);
      const g = c.createGain();
      g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(0.06, t + 0.08);
      g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
      src.connect(f).connect(g).connect(c.destination);
      src.start(t); src.stop(t + dur);
    } catch (_) {}
  }

  // ---- audio-reactive motion: the scene moves with the voice ---------------
  // While speaking: pan/zoom runs faster (seamless via updatePlaybackRate)
  // and the vignette breathes (CSS class). State arrives over the SSE stream.
  let speaking = false, speakStartT = 0;
  // Figure badge has two feeders: the showing image's purpose, and the
  // speaking message's [[visual:]] flag (so it lights before the image lands).
  let figImg = false, figMsg = false;
  function updFig() { $('fig').classList.toggle('on', figImg || figMsg); }
  // Cross-session honesty: remember which session's reply the shown visual
  // belongs to; while a DIFFERENT session speaks, a figure dims to backdrop
  // and drops its badge (it doesn't illustrate that voice). null session on
  // either side = unknown → leave it alone.
  let shownFigure = false, shownSession = null;
  function applyStale(speakSess) {
    const stale = !!(shownFigure && shownSession && speakSess &&
                     speakSess !== shownSession);
    layers[front].classList.toggle('stale', stale);
    figImg = shownFigure && !stale;
    updFig();
  }
  // Subtitles: the sentence being spoken, straight off the same per-clip
  // marker that drives the tmux copy-mode highlight. This is the whole of
  // the wall's follow-along — reading the rest of a reply, replying to it and
  // driving the session all live in Sasonica now.
  function subsOn() { return localStorage.getItem('subs') !== '0'; }
  // The last thing actually said, kept so the band can go on showing it,
  // dimmed, after the voice stops. A screen glanced at from the doorway
  // should say what was just read out, not go blank the instant it ends.
  let lastSaid = null;
  function setSubtitle(text, past) {
    const show = !!(text && subsOn());
    if (show) {
      $('sub').textContent = text;
      if (!past) lastSaid = text;
    }
    $('sub').classList.toggle('on', show);
    // Said, not being said. Same pill, quieter ink — so a screen glanced at
    // from across the room never reports a voice that stopped minutes ago.
    $('sub').classList.toggle('past', show && !!past);
    // The caption is the picture's; the sentence is the voice's. Only one of
    // them at the bottom of the screen at a time.
    if (show) $('cap').classList.add('hide');
    else $('cap').classList.remove('hide');
  }

  // ---- pinch ---------------------------------------------------------------
  // Pinch anywhere zooms the picture. Written with Pointer Events rather than
  // taken from the browser, because the browser's own page zoom would scale
  // the caption along with the figure — and on a canvas that is mostly one
  // image, that is not what the fingers meant.
  const PINCH = { pts: new Map(), d0: 0, base: 1, mid: null };
  // The picture's transform: scale, then offset, with the stage's origin at
  // its top-left corner. One rule governs the whole gesture —
  //
  //     screen = point * scale + offset
  //
  // — where `point` is a spot on the picture. Keeping a spot under the fingers
  // still is just solving that for offset, which is what makes a pinch feel
  // attached to the hand rather than chased across the screen. Two-finger drag
  // is the same equation with the scale unchanged, so panning and zooming are
  // not two behaviours that have to agree; they are one.
  let imgZ = 1, imgX = 0, imgY = 0, imgRaf = 0;
  const IMG_MAX = 10;
  function clampImg() {
    imgZ = Math.min(IMG_MAX, Math.max(1, imgZ));
    if (imgZ === 1) { imgX = imgY = 0; return; }
    // Never past the edge: beyond it there is only black, and finding the way
    // back from black is a puzzle nobody asked for.
    imgX = Math.min(0, Math.max(-innerWidth * (imgZ - 1), imgX));
    imgY = Math.min(0, Math.max(-innerHeight * (imgZ - 1), imgY));
  }
  // Written once per frame, as one property. Three custom properties set on
  // every pointermove is three style invalidations per event, and at touch
  // report rates that is most of why this stuttered.
  function drawImg() {
    imgRaf = 0;
    $('stage').style.transform =
      'translate(' + imgX + 'px,' + imgY + 'px) scale(' + imgZ + ')';
    document.body.classList.toggle('imgzoom', imgZ > 1);
  }
  function applyImg() {
    clampImg();
    if (!imgRaf) imgRaf = requestAnimationFrame(drawImg);
  }
  /** Zoom to `z` while holding the picture-point under `(sx, sy)` still. */
  function zoomAbout(z, sx, sy) {
    const from = imgZ;
    const px = (sx - imgX) / from, py = (sy - imgY) / from;   // the spot held
    imgZ = Math.min(IMG_MAX, Math.max(1, z));
    imgX = sx - px * imgZ;
    imgY = sy - py * imgZ;
    applyImg();
  }
  function panImg(dx, dy) { imgX += dx; imgY += dy; applyImg(); }
  function resetImg() { imgZ = 1; imgX = imgY = 0; applyImg(); }

  function pinchDist() {
    const [a, b] = [...PINCH.pts.values()];
    return Math.hypot(a.x - b.x, a.y - b.y);
  }
  function pinchMid() {
    const [a, b] = [...PINCH.pts.values()];
    return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
  }
  addEventListener('pointerdown', (e) => {
    if (e.pointerType === 'mouse') return;
    PINCH.pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (PINCH.pts.size === 2) {
      PINCH.d0 = pinchDist();
      PINCH.mid = pinchMid();
      PINCH.base = imgZ;
      document.body.classList.add('pinching');
    }
  }, { passive: true });
  addEventListener('pointermove', (e) => {
    if (!PINCH.pts.has(e.pointerId)) return;
    PINCH.pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (PINCH.pts.size !== 2 || !PINCH.d0) return;
    // Gain, because fingers do not travel far on a phone. A raw distance
    // ratio caps out around 2.5x on a 420px screen even with a full spread
    // from pinched-shut to wide, which meant zooming to anything useful was
    // four or five separate gestures. Raising the ratio to a power keeps the
    // gesture continuous and reversible — the same spread that reached 2.5x
    // now reaches about 5x, and pinching back retraces it exactly.
    const ratio = Math.pow(pinchDist() / PINCH.d0, 1.9);
    // Two fingers move the picture as well as size it — the midpoint carries
    // the drag, the spread carries the zoom, and both land in one transform.
    const mid = pinchMid();
    const dx = mid.x - PINCH.mid.x, dy = mid.y - PINCH.mid.y;
    PINCH.mid = mid;
    // Zoom FIRST, then carry the drag. The other order loses the pan whenever
    // the zoom is also clamping: panImg clamps against the OLD scale, and at
    // scale 1 that clamp is "no room at all", so a two-finger drag that was
    // also opening the picture up had its movement thrown away before the
    // zoom could make room for it. That is why two-finger panning looked
    // dead — it worked only if you were already zoomed in.
    zoomAbout(PINCH.base * ratio, mid.x, mid.y);
    if (dx || dy) panImg(dx, dy);
  }, { passive: true });
  function pinchEnd(e) {
    PINCH.pts.delete(e.pointerId);
    if (PINCH.pts.size < 2) {
      PINCH.d0 = 0;
      document.body.classList.remove('pinching');
    }
    // The finger that stays becomes the pan anchor, rather than the picture
    // jumping when the second one lifts.
    if (PINCH.pts.size === 1) {
      const [only] = [...PINCH.pts.values()];
      panFrom = { x: only.x, y: only.y };
    }
  }
  addEventListener('pointerup', pinchEnd, { passive: true });
  addEventListener('pointercancel', pinchEnd, { passive: true });
  // Everything on this page is the picture except one 44px button, and a
  // press that starts on it is the button's: without this it also pans the
  // image and counts toward the double-tap reset.
  const onPicture = (t) => !(t && t.closest && t.closest('#full'));
  // One finger pans too, but only once zoomed.
  let panFrom = null;
  addEventListener('pointerdown', (e) => {
    if (imgZ > 1 && PINCH.pts.size <= 1 && onPicture(e.target))
      panFrom = { x: e.clientX, y: e.clientY };
  }, { passive: true });
  addEventListener('pointermove', (e) => {
    if (!panFrom || PINCH.pts.size >= 2) return;
    panImg(e.clientX - panFrom.x, e.clientY - panFrom.y);
    panFrom = { x: e.clientX, y: e.clientY };
  }, { passive: true });
  addEventListener('pointerup', () => { panFrom = null; }, { passive: true });
  // A zoom you cannot undo with one gesture is a trap on a screen with no
  // keyboard: double-tap the picture returns it whole.
  //
  // The trap this walked into: letting go of a pinch raises TWO fingers a few
  // milliseconds apart, so two pointerups arrive well inside any double-tap
  // window and the zoom you just made snapped straight back to 1. It looked
  // like the zoom refusing to stick, and it was the undo firing on the
  // release of every single pinch.
  //
  // So a tap has to be a tap: one finger, never joined by a second, and put
  // down and lifted in roughly the same place. Anything else is a gesture and
  // gestures do not get counted.
  let lastTap = 0, tapOk = false, tapAt = null;
  addEventListener('pointerdown', (e) => {
    if (PINCH.pts.size > 1) { tapOk = false; return; }   // a second finger: not a tap
    tapOk = onPicture(e.target);
    tapAt = { x: e.clientX, y: e.clientY };
  }, { passive: true });
  addEventListener('pointermove', (e) => {
    if (!tapOk || !tapAt) return;
    if (Math.hypot(e.clientX - tapAt.x, e.clientY - tapAt.y) > 12) tapOk = false;
  }, { passive: true });
  addEventListener('pointerup', (e) => {
    if (PINCH.pts.size > 0) { tapOk = false; return; }   // a finger is still down
    if (!tapOk) { tapOk = false; return; }
    tapOk = false;
    const now = Date.now();
    if (now - lastTap < 320 && imgZ > 1) { resetImg(); lastTap = 0; }
    else lastTap = now;
  }, { passive: true });
  // Reachable for the harness, which cannot pinch.
  window.__imgprobe = { zoomAbout, panImg, resetImg,
                        at: () => ({ z: imgZ, x: imgX, y: imgY }) };

  // ---- reloading onto a new page -------------------------------------------
  let pageId = null, reloadWanted = false;
  // Never mid-sentence. A deploy should not blank the wall in the middle of a
  // reply — the whole point of noticing the new page is that the screen ends
  // up right, and a reload timed badly is its own kind of wrong.
  function wantReload() {
    reloadWanted = true;
    maybeReload();
  }
  function maybeReload() {
    if (!reloadWanted) return;
    if (speaking) return;                       // let the reply finish
    location.reload();
  }

  function setSpeaking(on) {
    if (on === speaking) return;
    speaking = on;
    if (on) speakStartT = Date.now();
    pumpSeq(on);                               // beat pump runs only while speaking
    document.body.classList.toggle('speaking', on);
    for (const el of layers)
      for (const a of el.getAnimations())
        (a.updatePlaybackRate ? a.updatePlaybackRate(on ? 2.6 : 1)
                              : a.playbackRate = on ? 2.6 : 1);
    chime(on);
    vidVisible();                              // video yields while speaking
    if (!on) { setSubtitle(null); figMsg = false; updFig(); maybeReload(); }
    if (!on && seq) setBeat(seq.length - 1);   // speech over → the conclusion
  }

  // ---- beats: a sequence of images that flips in step with the voice -------
  // The pusher sends per-beat start fractions plus an estimated spoken
  // duration; progress = elapsed time since the voice started (or since
  // generation began, for a screen that joined mid-reply) over that estimate.
  // Speech ending parks the canvas on the final beat, whatever the estimate
  // got wrong.
  let seq = null, seqIdx = -1, seqBase = 0, seqEst = 0, seqCap = null;
  function tick() {
    if (!sfxOn()) return;
    try {
      const c = actx(), t = c.currentTime;
      const o = c.createOscillator(), g = c.createGain();
      o.type = 'sine'; o.frequency.value = 880;
      g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(0.03, t + 0.01);
      g.gain.exponentialRampToValueAtTime(0.0001, t + 0.09);
      o.connect(g).connect(c.destination);
      o.start(t); o.stop(t + 0.1);
    } catch (_) {}
  }
  function setBeat(i) {
    if (!seq || i === seqIdx || !seq[i]) return;
    const first = seqIdx < 0;
    seqIdx = i;
    show({ image: seq[i].image, caption: first ? seqCap : null });
    first ? whoosh() : tick();
  }
  function applySeq() {
    if (!seq || seqIdx >= seq.length - 1 || !speaking || seqEst <= 0) return;
    const frac = (Date.now() - seqBase) / 1000 / seqEst;
    let idx = 0;
    for (let i = 0; i < seq.length; i++) if (frac >= seq[i].at) idx = i;
    if (idx > seqIdx) setBeat(idx);
  }
  // The beat pump only means anything while the voice is talking — run its 1s
  // timer only then (and never while backgrounded), started/stopped by
  // setSpeaking, instead of a forever-ticking interval (#141).
  let seqTimer = null;
  function pumpSeq(on) {
    clearInterval(seqTimer); seqTimer = null;
    if (on) seqTimer = setInterval(() => { if (!document.hidden) applySeq(); }, 1000);
  }

  // ---- video sync: muted YouTube mirror of the phone's music ---------------
  // The server streams {"kind":"video", vid, t, paused, rate} while the phone
  // plays a YouTube-cached track. The page keeps a muted IFrame player within
  // ~1.5s of the audio (seek on drift), and yields the screen to figures for a
  // minute whenever one arrives — a figure is content, the video is ambience.
  let ytP = null, ytReady = false, ytVid = null, ytApiAsked = false;
  let pendingV = null, figHold = 0;
  function ytEnsureApi() {
    if (ytApiAsked) return; ytApiAsked = true;
    const s = document.createElement('script');
    s.src = 'https://www.youtube.com/iframe_api';
    document.head.appendChild(s);
  }
  window.onYouTubeIframeAPIReady = () => {
    ytP = new YT.Player('yt', {
      width: '100%', height: '100%',
      playerVars: { autoplay: 1, controls: 0, disablekb: 1, fs: 0, rel: 0,
                    iv_load_policy: 3, playsinline: 1 },
      events: {
        onReady: () => { ytReady = true; ytP.mute();
                         if (pendingV) { const v = pendingV; pendingV = null; syncVideo(v); } },
        // Embed-blocked / removed video → fall back to the ambient artwork.
        onError: () => { ytVid = null; vidVisible(); },
      },
    });
  };
  function vidVisible() {
    // Speech owns the canvas while it's talking (subtitles, artwork,
    // figures) — the video yields and returns when the voice stops.
    // e-ink never shows video (CSS hides the layer; don't even sync it).
    document.getElementById('ytwrap').classList
      .toggle('on', !!ytVid && !speaking && !einkOn() && Date.now() > figHold);
  }
  setInterval(() => { if (!document.hidden) vidVisible(); }, 5000);  // restores video after a fig hold; idle while backgrounded (#141)
  function syncVideo(d) {
    if (einkOn()) return;            // no video on e-ink — don't even load the API
    if (!d.vid) {
      ytVid = null; vidVisible();
      if (ytReady) try { ytP.stopVideo(); } catch (_) {}
      return;
    }
    ytEnsureApi();
    if (!ytReady) { pendingV = d; return; }
    const now = d.t + (Date.now() - d.rx) / 1000;   // rx stamped on arrival
    try {
      if (d.vid !== ytVid) {
        ytVid = d.vid;
        ytP.loadVideoById({ videoId: d.vid, startSeconds: now });
        ytP.mute();
      } else if (!d.paused && Math.abs(ytP.getCurrentTime() - now) > 1.5) {
        ytP.seekTo(now, true);
      }
      if (d.paused) { if (ytP.getPlayerState() === 1) ytP.pauseVideo(); }
      else if (ytP.getPlayerState() !== 1) ytP.playVideo();
      if (d.rate && ytP.setPlaybackRate)
        ytP.setPlaybackRate(Math.max(0.25, Math.min(2, d.rate)));
    } catch (_) {}
    vidVisible();
  }

  // SSE stream + self-heal (#137). A stalled stream (mobile backgrounding,
  // half-open TCP on a days-long wall) silently stops delivering; onerror
  // isn't guaranteed to fire. So the server now sends a real `{"kind":"ping"}`
  // data frame that fires onmessage, the client stamps lastEventTs on EVERY
  // frame, and a watchdog tears the EventSource down and reconnects after ~45s
  // of silence.
  let es = null, lastEventTs = Date.now();
  function onSseMessage(e) {
    lastEventTs = Date.now();               // any frame (incl. ping) = the stream is live
    setDisconnected(false);                 // a live frame clears the reconnect banner
    try {
      const d = JSON.parse(e.data);
      if (d.kind === 'ping') return;        // heartbeat only — nothing to render
      if (d.kind === 'video') { d.rx = Date.now(); syncVideo(d); }
      else if (d.kind === 'hello') {
        // The first one is the page we are running: we loaded it from this
        // server moments ago, so whatever it says is by definition ours. A
        // later one that disagrees means the server's page changed under us —
        // the canvas was restarted with new assets — and this document is now
        // the old version. Nothing else in the client ever noticed that: the
        // watchdog reconnects the STREAM, and the stream is not the page.
        if (!pageId) pageId = d.page;
        else if (d.page && d.page !== pageId) wantReload();
      }
      else if (d.kind === 'state') {
        if (d.speaking) holdWake(45000);   // rolling hold while a voice is live
        setSpeaking(!!d.speaking);
        if (d.speaking) {
          setSubtitle(d.sentence || null);
          figMsg = !!d.visual; updFig();
          applyStale(d.session || null);
        } else {
          // The voice has stopped: keep the last thing said on screen, dimmed.
          setSubtitle(lastSaid, true);
          applyStale(null);            // no voice → nothing is misattributed
        }
        applySeq();
      }
      else if (d.sequence) {
        holdWake(((d.estdur || 60) + 30) * 1000);  // see the whole story out
        seq = d.sequence; seqIdx = -1; seqEst = d.estdur || 0;
        seqCap = d.caption || null;
        shownFigure = false; shownSession = d.session || null;
        figImg = false; updFig();
        figHold = Date.now() + 60000; vidVisible();   // beats own the screen
        // Anchor progress to the real speech start when we saw it; else
        // reconstruct it from how long generation took.
        seqBase = (speaking && speakStartT)
          ? speakStartT : Date.now() - (d.gen_secs || 0) * 1000;
        // If the voice already finished — generation outlasted a short reply,
        // or this is a replay to a late-joining screen — park on the
        // conclusion instead of restarting the story from beat 0.
        const elapsed = (Date.now() - seqBase) / 1000;
        if (!speaking && seqEst > 0 && elapsed > seqEst) setBeat(seq.length - 1);
        else { setBeat(0); applySeq(); }
      }
      else if (d.image) {
        holdWake(90000);
        seq = null; seqIdx = -1; show(d);
        shownFigure = d.purpose === 'figure'; shownSession = d.session || null;
        figImg = shownFigure; updFig();
        if (figImg) { figHold = Date.now() + 60000; vidVisible(); }
        figImg ? figureCue() : whoosh();
      }
    } catch (_) {}
  }
  // Room-legible disconnect (#142), coordinated with the #137 watchdog: a brief
  // blip only dims the 8px dot; after ~10s down, grey the canvas and float the
  // big "reconnecting…" banner. Repeated onerror/retry must NOT keep resetting
  // the escalation timer, or a real outage would never surface.
  let offTimer = null;
  function setDisconnected(on) {
    if (on) {
      if (!offTimer && !$('offbar').classList.contains('on'))
        offTimer = setTimeout(() => {
          offTimer = null; $('offbar').classList.add('on');
        }, 10000);
    } else {
      clearTimeout(offTimer); offTimer = null;
      $('offbar').classList.remove('on');
    }
  }
  function connectEvents() {
    try { if (es) es.close(); } catch (_) {}
    es = new EventSource('/events');
    es.onmessage = onSseMessage;
    es.onopen = () => { lastEventTs = Date.now(); $('dot').classList.remove('off'); setDisconnected(false); };
    es.onerror = () => { $('dot').classList.add('off'); setDisconnected(true); };
  }
  connectEvents();
  // Watchdog: reconnect a stream that has gone quiet past the heartbeat window
  // (a silent stall may never fire onerror, so escalate the banner here too).
  setInterval(() => {
    if (document.hidden) return;            // backgrounded timers throttle; don't churn
    if (Date.now() - lastEventTs > 45000) {
      lastEventTs = Date.now(); setDisconnected(true); connectEvents();
    }
  }, 15000);

  // Hold the screen awake only while something FRESH is showing, then release
  // so a short system screen-off delay works again (a permanent lock meant
  // "awake when idle, dark when a figure lands" — the worst pairing). A page
  // can only PREVENT sleep; turning a dark screen back ON is the per-host
  // wake agent's job (it watches /events for show events stamped wake=<us>).
  let lock = null, wakeUntil = 0;
  async function holdWake(ms) {
    wakeUntil = Math.max(wakeUntil, Date.now() + ms);
    if (!lock) {
      try { lock = await navigator.wakeLock.request('screen'); } catch (_) {}
      if (lock) lock.addEventListener('release', () => { lock = null; });
    }
  }
  setInterval(() => {
    if (lock && Date.now() > wakeUntil) {
      try { lock.release(); } catch (_) {} lock = null;
    }
  }, 10000);
  holdWake(90000);   // fresh page: hold briefly, then obey the system timeout
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      lastEventTs = Date.now();
      if (Date.now() < wakeUntil) holdWake(30000);  // re-grab a dropped lock
    }
  });
  // A tap = eyes on the screen: hold it awake a while, and unlock audio while
  // we have the gesture.
  document.addEventListener('pointerdown', () => { holdWake(90000); },
                            { passive: true });

  // Activity beacon: tell the server this screen has eyes on it (names the
  // wake target for figure pushes). Identity = our tailnet IP, so no pairing
  // needed; only an explicit SCREEN override rides the token.
  function token() { return localStorage.getItem('amux_token') || ''; }
  let seenLast = 0;
  function seen(force, focused) {
    const now = Date.now();
    if (!force && now - seenLast < 30000) return;
    seenLast = now;
    const body = {focused: focused !== undefined ? focused : document.hasFocus()};
    if (SCREEN) body.screen = SCREEN;
    const opts = {method: 'POST', keepalive: true,
                  headers: {'Content-Type': 'application/json'},
                  body: JSON.stringify(body)};
    if (SCREEN && token()) opts.headers['X-Auth-Token'] = token();
    try { fetch('/seen', opts); } catch (_) {}
  }
  for (const ev of ['pointerdown', 'keydown', 'touchstart'])
    document.addEventListener(ev, () => seen(false, true), {passive: true});
  // blur/focus track "is the canvas the active window" — and ONLY that:
  // screen-blank fires neither, so a dark-but-foreground canvas stays
  // wake-eligible, while switching window/tab (blur) rules this screen out.
  window.addEventListener('focus', () => seen(true, true));
  window.addEventListener('blur', () => seen(true, false));
  document.addEventListener('visibilitychange',
    () => { if (!document.hidden) seen(false); });
  // A canvas parked foreground on a big screen stays current without touches.
  setInterval(() => { if (!document.hidden && document.hasFocus()) seen(true); },
              600000);
  seen(true);

  // Transient top-center status message (~2.6s).
  let toastT = null;
  function toast(msg) {
    const t = $('toast');
    t.textContent = msg;
    t.classList.add('on');
    clearTimeout(toastT);
    toastT = setTimeout(() => t.classList.remove('on'), 2600);
  }

  // ---- fullscreen ----------------------------------------------------------
  // A wall is a browser tab, and a tab has chrome around it. There is no F11
  // on a screen with no keyboard, so this is the way — the only control the
  // page kept, because it is the only one that is about the screen rather than
  // about the audio the screen is illustrating.
  const FS_ROOT = document.documentElement;
  const fsReq = FS_ROOT.requestFullscreen || FS_ROOT.webkitRequestFullscreen;
  const fsExit = document.exitFullscreen || document.webkitExitFullscreen;
  function fsOn() {
    return !!(document.fullscreenElement || document.webkitFullscreenElement);
  }
  // An iframe without allowfullscreen reports fullscreenEnabled false, and a
  // button that cannot work is worse than no button: take it off the page
  // rather than leave a dead corner for somebody to keep pressing.
  if (!fsReq || document.fullscreenEnabled === false) $('full').remove();

  // Invisible at rest, revealed by any sign of a person, gone again a few
  // seconds later. The reveal is what makes an always-there button acceptable
  // on a picture: it is there when you are looking for it and not otherwise.
  let fullTimer = null, fullShownAt = 0;
  function revealFull() {
    const b = $('full');
    if (!b) return;
    // mousemove fires at pointer rate; re-arming a timeout on every one of
    // them is a lot of churn for a four-second fade. Once a frame is plenty.
    const now = Date.now();
    if (b.classList.contains('on') && now - fullShownAt < 200) return;
    fullShownAt = now;
    b.classList.add('on');
    clearTimeout(fullTimer);
    fullTimer = setTimeout(() => b.classList.remove('on'), 4000);
  }
  function drawFull() {
    const b = $('full');
    if (!b) return;
    const on = fsOn();
    document.body.classList.toggle('fullscreen', on);
    b.title = on ? 'Leave fullscreen' : 'Fullscreen';
    b.setAttribute('aria-label', b.title);
    revealFull();                  // say so: the icon just changed under a thumb
  }
  async function toggleFull() {
    const want = !fsOn();
    try {
      if (want) await fsReq.call(FS_ROOT);
      else if (fsExit) await fsExit.call(document);
    } catch (_) {}
    // Android's WebView has the API and no fullscreen behind it — it resolves
    // and nothing happens — so believe the document, not the promise. (There
    // the canvas is already drawn to every edge, which is why nobody noticed.)
    if (want && !fsOn()) toast('this screen has no fullscreen to give');
    drawFull();
  }
  if ($('full')) {
    $('full').addEventListener('click', (e) => { e.stopPropagation(); toggleFull(); });
    for (const ev of ['fullscreenchange', 'webkitfullscreenchange'])
      document.addEventListener(ev, drawFull);
    for (const ev of ['pointerdown', 'mousemove', 'keydown'])
      document.addEventListener(ev, revealFull, { passive: true });
    revealFull();                  // a fresh page shows its one control once
  }

  // ---- the two keys left ---------------------------------------------------
  // The wall shows a picture and says one sentence at a time, and both of
  // those are things a passer-by might want off. Everything else this page
  // used to bind — the transport, the agent tree, the reply box, the whole
  // reply — is Sasonica's now, on a device that has a keyboard anyway.
  function toggleCc() {
    localStorage.setItem('subs', subsOn() ? '0' : '1');
    if (subsOn()) { setSubtitle(lastSaid, !speaking); toast('captions on'); }
    else { setSubtitle(null); toast('captions off'); }
  }
  function toggleSfx() {
    localStorage.setItem('sfx', sfxOn() ? '0' : '1');
    if (sfxOn()) { chime(true); toast('sound on'); }   // audible confirmation + unlocks audio
    else toast('sound off');
  }
  document.addEventListener('keydown', (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === 'c') { e.preventDefault(); toggleCc(); }
    else if (e.key === 'f') { e.preventDefault(); toggleSfx(); }
  });

  // ?reset=1 — put this screen back to how a fresh one behaves.
  //
  // Every view preference lives in localStorage, per device: captions on or
  // off, fit mode, e-ink, sound. That is right for preferences and it is
  // exactly why two screens on the SAME page can behave differently — the
  // app's WebView and a browser tab are two devices, and one of them having
  // captions off is enough to make the words vanish on one and not the other.
  // This is the way back, and it deliberately keeps the pairing token:
  // clearing that would silently cost the screen-name override's authority,
  // which is not what anybody means by "reset the view".
  if (/(^|[?&])reset=1(&|$)/.test(location.search)) {
    // txscale/split are the retired transcript's; a screen that ran the old
    // page still has them, and this is the one place that clears them.
    for (const k of ['subs', 'fit', 'eink', 'sfx', 'txscale', 'split'])
      localStorage.removeItem(k);
    location.replace('/');
  }
