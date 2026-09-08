
  const $ = (id) => document.getElementById(id);
  const icon = (n) => '<svg class="ic"><use href="#i-' + n + '"/></svg>';
  // ---- e-ink mode: ?eink=1 arms it for this device, ?eink=0 (or 'e') back --
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
  // Static icons; stateful ones (pp, sfx, chan, target) are set in their
  // draw functions below.
  $('prev').innerHTML = icon('prev');   $('next').innerHTML = icon('next');
  $('skb').innerHTML = icon('skipb');   $('skf').innerHTML = icon('skipf');
  $('vdn').innerHTML = icon('minus');   $('vup').innerHTML = icon('plus');
  $('sdn').innerHTML = icon('slower');  $('sup').innerHTML = icon('faster');
  $('mute').innerHTML = icon('mute');   $('kbd').innerHTML = icon('kbd');
  $('cc').innerHTML = icon('cc');       $('xbtn').innerHTML = icon('close');
  $('fit').innerHTML = icon('fit');
  $('send').innerHTML = icon('send');   $('chan').innerHTML = icon('note');
  $('pp').innerHTML = icon('play');
  let front = 0, capTimer = null;
  const KB = ['kb1','kb2','kb3','kb4'];

  // ---- fit setting: auto (figures fit, art fills) · fit · fill -------------
  // cover + the Ken Burns zoom crops edges — fatal for a figure's labels on a
  // small screen. Fitted images letterbox (object-fit: contain) and skip the
  // pan/zoom (which would push the letterboxed image off-frame again).
  function fitMode() { return localStorage.getItem('fit') || 'auto'; }
  function wantFit(purpose) {
    const m = fitMode();
    return m === 'fit' || (m === 'auto' && (purpose === 'figure' || purpose === 'portrait'));
  }
  let lastPurpose = null;
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

  // ---- subtitle band -------------------------------------------------------
  // A figure's labels ARE the message, so the sentence stops floating over
  // them: while an image is on screen with subtitles up, the image box shrinks
  // by exactly the band's own height and the words take the strip.
  //
  // This used to require a FITTED image, on the reasoning that ambient (cover)
  // art has no labels to protect and could keep the old floating pill. That
  // reasoning was sound for what the band was then, and is wrong for what it
  // became: the band is now the seam you drag and the way into the whole
  // reply's transcript, and neither of those has anything to do with whether
  // the picture happens to have writing on it. Gating them on the kind of
  // image meant ordinary speech over ambient art got a pill and no way
  // through — reported, exactly, as "the old version without the split
  // screen", on a page that was completely current.
  //
  // The band needs WORDS. It does not need a picture.
  //
  // This gate has been wrong twice, each time by being narrower than the thing
  // it guards. First it demanded a fitted figure; loosening it to "any image"
  // fixed ambient art and left the commonest case of all still broken —
  // speech with nothing on the canvas, which is most speech. No image meant no
  // band, so no seam, no bottom half, and no way into the transcript, on a
  // screen that had plenty of room for all three.
  //
  // The band's job was never "keep words off a picture". It is where the words
  // live, the seam you drag, and the door to the whole reply. A picture, when
  // there is one, yields the strip; when there is not, the strip is simply
  // there. Nothing about that needs an image to exist.
  function bandOn() {
    // Captions off means no band at all — not a band with nothing in it. A
    // reserved strip that stays empty is the shape of the bug that had the
    // picture yielding half the screen to blank space, and it is also how the
    // app came to look different from the browser: one of them had the toggle
    // off, so one of them had words and the other had a hole.
    if (!subsOn()) return false;
    return $('sub').classList.contains('on') || txLines.length > 0;
  }
  // How tall the words' half is, when the reader has said. Null means "as
  // tall as the sentence needs", which is the default and what the band did
  // before there was a handle. Remembered per device: a reading split is a
  // preference, not a per-reply decision.
  function splitPx() {
    const v = parseFloat(localStorage.getItem('split'));
    return v > 0 ? v : null;
  }
  function setSplit(px) {
    if (txOpen())
      requestAnimationFrame(() =>
        document.documentElement.style.setProperty('--txtop', txTop() + 'px'));
    if (px == null) localStorage.removeItem('split');
    else {
      // Never let either half vanish: a divider dragged to an edge is a
      // control that has eaten the thing it was dividing.
      const min = 56, max = innerHeight * 0.8;
      localStorage.setItem('split', String(Math.min(max, Math.max(min, px))));
    }
    updBand();
  }
  function updBand() {
    const on = bandOn();
    document.body.classList.toggle('subband', on);
    // The handle exists only while there are two halves to divide.
    $('split').hidden = !on;
    // Measure AFTER the class lands: the band's padding/width differ from the
    // pill's, so a pill-sized reading would under-reserve on a long sentence.
    const css = document.documentElement.style;
    // The reader's split wins over the measurement when there is one, but
    // never shrinks below what the sentence actually needs — words clipped by
    // a divider the reader forgot they dragged is a bug they cannot diagnose.
    const measured = on ? $('sub').offsetHeight : 0;
    const chosen = splitPx();
    const band = on && chosen ? Math.max(measured, chosen) : measured;
    css.setProperty('--subband', band + 'px');
    // The docks ride up on the band, so they'd land back on the picture. While
    // a figure is being narrated the reserve covers them too — for that minute
    // the picture is smaller and completely unobscured, which is the trade the
    // whole band exists to make.
    let stack = band;
    for (const [id, gap] of [['inp', 14], ['agents', 6]]) {
      const el = $(id);
      if (!on || !el.offsetHeight) continue;              // display:none → 0
      if (getComputedStyle(el).opacity === '0') continue;  // faded out (CONTROL)
      stack = Math.max(stack, band + gap + el.offsetHeight);
    }
    css.setProperty('--figres', stack + 'px');
  }
  // Re-reserve whenever anything in the bottom stack changes size (a sentence
  // rewraps, the reply box grows a row, the agent tree expands) or is shown /
  // hidden by a mode change (class flips on the same elements).
  const stackEls = [$('sub'), $('inp'), $('agents')];
  if (window.ResizeObserver) {
    const ro = new ResizeObserver(() => { if (bandOn()) updBand(); });
    for (const el of stackEls) ro.observe(el);
  }
  if (window.MutationObserver) {
    const mo = new MutationObserver(() => { if (bandOn()) updBand(); });
    for (const el of stackEls.slice(1))
      mo.observe(el, { attributes: true, attributeFilter: ['class'] });
  }

  function show(d) {
    const back = 1 - front;
    const el = layers[back];
    lastPurpose = d.purpose || null;
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
      updBand();
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
  // down when it stops. Quiet by design; the bell button toggles, state
  // persists per device. Browsers gate audio behind a first user gesture —
  // the same tap that opens the controller / takes the wake lock unlocks it.
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
  // marker that drives the tmux copy-mode highlight.
  function subsOn() { return localStorage.getItem('subs') !== '0'; }
  function setSubtitle(text, past) {
    const show = !!(text && subsOn());
    if (show) $('sub').textContent = text;
    $('sub').classList.toggle('on', show);
    // Said, not being said. Same band, quieter ink — so a screen glanced at
    // from across the room never reports a voice that stopped minutes ago.
    $('sub').classList.toggle('past', show && !!past);
    // The door onto the transcript follows the subtitle: no words, no door.
    updDoor();
    updBand();
    if (show) $('cap').classList.add('hide');
    else if (!visible) $('cap').classList.remove('hide');
  }
  // ---- the whole reply -----------------------------------------------------
  // The band carries the sentence being said; this carries the clip it came
  // from. The lines arrive on the same state event (canvas.py puts the
  // splitter's own clip_sentences on the wire), so the transcript is the
  // server's account of the reply rather than an accumulation of whatever this
  // page happened to be awake for — a screen that reloads mid-reply gets the
  // same thing as one that watched it all.
  let txLines = [], txIdx = -1, txFollow = true;
  function setLines(lines, idx) {
    const same = lines.length === txLines.length
              && lines.every((t, i) => t === txLines[i]);
    if (same && idx === txIdx) return;
    txIdx = idx;
    if (!same) { txLines = lines; renderTx(); updDoor(); }
    else markTx();
    if (txOpen()) followTx();
  }
  function renderTx() {
    const box = $('txlines');
    box.textContent = '';
    for (const text of txLines) {
      const p = document.createElement('p');
      p.textContent = text;
      box.appendChild(p);
    }
    markTx();
  }
  function markTx() {
    const ps = $('txlines').children;
    for (let i = 0; i < ps.length; i++) {
      ps[i].classList.toggle('now', i === txIdx);
      // Ahead only while a voice is actually working through the list. With
      // the speech over, the whole clip is just text and hiding the end of it
      // would be theatre.
      ps[i].classList.toggle('ahead', speaking && txIdx >= 0 && i > txIdx);
    }
  }
  // The way into the transcript, and it must outlive the voice.
  //
  // It was tied to the subtitle being on screen, which meant it vanished the
  // instant the reply finished — so the transcript was reachable only while
  // you were already being read to, and gone by the time you thought "what
  // did that say". That is exactly backwards: the transcript is what you go
  // to when the voice STOPS. It now stands as long as there are lines worth
  // opening, which is until the next reply replaces them.
  function updDoor() {
    document.body.classList.toggle('hassub', txLines.length > 0);
  }
  function txOpen() { return !$('tx').hidden; }
  // The /agents poll is the real caller; the browser harness has no amux to
  // drive it with, so the indicator is reachable for a test without one.
  window.__txprobe = (n) => setWorking(
    Array.from({ length: n }, (_, i) => ({ name: 'agent-' + i, state: 'working' })));
  // "Something is still being worked on" — named where there is one, counted
  // where there are several. The voice stopping does not mean the work did,
  // and the transcript is where that question gets asked.
  function setWorking(list) {
    const n = list.length;
    $('txwork').hidden = !n;
    if (!n) return;
    $('txworkt').textContent = n === 1
      ? (list[0].name || 'working').slice(0, 48)
      : n + ' working';
  }
  // Bring the live line to a comfortable reading height — a third down, not
  // centred: the eye wants what comes next to be visible under it.
  function followTx() {
    if (!txFollow) return;
    const el = $('txlines').children[txIdx];
    if (!el) return;
    const to = el.offsetTop - $('tx').clientHeight * 0.34;
    $('tx').scrollTo({ top: Math.max(0, to), behavior: 'smooth' });
  }
  // Where the transcript's top edge sits — the seam, in every case.
  //
  // The band's height when there is a band, so the words simply grow into the
  // strip they were already in; the dragged split when the reader has set one;
  // and half the screen when there is neither. Never the whole screen: a
  // transcript that covers the picture is a pop-up, and this is meant to be
  // the bottom of a split.
  function txTop() {
    const chosen = splitPx();
    if (chosen) return Math.max(0, innerHeight - chosen);
    const band = parseFloat(getComputedStyle(document.documentElement)
                              .getPropertyValue('--subband')) || 0;
    // A one-line band is too thin to read a reply in, so opening the
    // transcript claims half the screen unless the reader has said otherwise.
    return Math.max(0, innerHeight - Math.max(band, innerHeight * 0.5));
  }
  function openTx(on) {
    if (on) document.documentElement.style.setProperty('--txtop', txTop() + 'px');
    $('tx').hidden = !on;
    document.body.classList.toggle('txopen', on);
    if (!on) { setAhead(false); maybeReload(); return; }
    txFollow = true;
    $('txlive').hidden = true;
    renderTx();
    // No smooth scroll on the way in — the panel should already be at the
    // right place when it appears, not seen travelling there.
    const el = $('txlines').children[txIdx];
    if (el) $('tx').scrollTop = Math.max(0, el.offsetTop - $('tx').clientHeight * 0.34);
  }
  function setAhead(on) {
    document.body.classList.toggle('txahead', on);
  }
  // Scrolling away from the live line stops the panel dragging the reader back,
  // and scrolling PAST it is what uncovers the text the voice has not reached.
  // Both are the same gesture, which is the point: reading on is something you
  // do, never something that happens to you.
  function txScrolled() {
    if (!txOpen()) return;
    const el = $('txlines').children[txIdx];
    if (!el) return;
    const view = $('tx').scrollTop + $('tx').clientHeight * 0.34;
    const off = view - el.offsetTop;
    txFollow = Math.abs(off) < 90;
    $('txlive').hidden = txFollow;
    // Reaching the bottom counts as scrolling past, and it is the only thing
    // that can: a clip shorter than the panel has nowhere to scroll TO, so a
    // pure distance test leaves the text ahead permanently unreachable.
    const tx = $('tx');
    const atEnd = tx.scrollTop + tx.clientHeight >= tx.scrollHeight - 4;
    if (off > 60 || (atEnd && off > 0)) setAhead(true);
    else if (txFollow) setAhead(false);
  }

  // ---- pinch ---------------------------------------------------------------
  // One gesture, two jobs: pinching the words changes reading size, pinching
  // anywhere else zooms the picture. Written once with Pointer Events rather
  // than taken from the browser, because the browser's own page zoom would
  // scale the controls and the band along with the figure — and on a canvas
  // that is mostly one image, that is not what the fingers meant.
  const PINCH = { pts: new Map(), d0: 0, base: 1, target: null, mid: null };
  function pinchTarget(el) {
    return (el && el.closest && el.closest('#tx, #sub')) ? 'text' : 'image';
  }
  function txScale() {
    const v = parseFloat(localStorage.getItem('txscale'));
    return v >= 0.6 && v <= 2.6 ? v : 1;
  }
  // Same treatment as the picture: batched to a frame, so a pinch is not
  // paying for a style recalc and a band re-measure on every pointer event.
  // updBand in particular reads offsetHeight, which forces layout — doing that
  // at touch report rate is what made resizing text feel heavier than moving
  // a whole image around.
  let txScaleWant = null, txRaf = 0;
  function drawTxScale() {
    txRaf = 0;
    document.documentElement.style.setProperty('--txscale', txScaleWant);
    localStorage.setItem('txscale', String(txScaleWant));
    updBand();                      // a bigger sentence reserves a taller band
  }
  function setTxScale(v) {
    txScaleWant = Math.min(3.2, Math.max(0.6, v));
    if (!txRaf) txRaf = requestAnimationFrame(drawTxScale);
    return txScaleWant;
  }
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
      PINCH.target = pinchTarget(e.target);
      PINCH.d0 = pinchDist();
      PINCH.mid = pinchMid();
      PINCH.base = PINCH.target === 'text' ? txScale() : imgZ;
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
    if (PINCH.target === 'text') {
      // Gentler gain than the picture. Type has a narrow band where it is
      // readable at all, and the picture's exponent walks straight past both
      // ends of it — the same spread that usefully takes a figure to 5x turns
      // a sentence into two words or a rumour.
      setTxScale(PINCH.base * Math.pow(pinchDist() / PINCH.d0, 1.25));
      // Two fingers drag the transcript as well as size it, which is the same
      // bargain the picture makes and the reason it feels like one gesture
      // rather than a mode.
      const m = pinchMid();
      if (txOpen()) $('tx').scrollTop -= m.y - PINCH.mid.y;
      PINCH.mid = m;
      return;
    }
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
      PINCH.d0 = 0; PINCH.target = null;
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
  // One finger pans too, but only once zoomed — otherwise this swallows the
  // tap that opens the controls.
  let panFrom = null;
  addEventListener('pointerdown', (e) => {
    if (imgZ > 1 && PINCH.pts.size <= 1 && pinchTarget(e.target) === 'image') {
      panFrom = { x: e.clientX, y: e.clientY };
    }
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
    tapOk = pinchTarget(e.target) === 'image';
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

  // ---- the divider ---------------------------------------------------------
  // Drag the seam to give the words more of the screen, or the picture more.
  // Pointer Events with capture, so a finger that leaves the 22px handle
  // mid-drag keeps dragging — the alternative is a control that works only if
  // you are accurate, on the device where you are least accurate.
  let splitFrom = null;
  $('split').addEventListener('pointerdown', (e) => {
    e.stopPropagation();
    splitFrom = { y: e.clientY, h: parseFloat(getComputedStyle(
      document.documentElement).getPropertyValue('--subband')) || 0 };
    $('split').setPointerCapture(e.pointerId);
    document.body.classList.add('splitting');
  });
  $('split').addEventListener('pointermove', (e) => {
    if (!splitFrom) return;
    e.stopPropagation();
    setSplit(splitFrom.h + (splitFrom.y - e.clientY));   // up = more words
  });
  function splitDone(e) {
    if (!splitFrom) return;
    splitFrom = null;
    document.body.classList.remove('splitting');
    try { $('split').releasePointerCapture(e.pointerId); } catch (_) {}
  }
  $('split').addEventListener('pointerup', splitDone);
  $('split').addEventListener('pointercancel', splitDone);
  // Double-tap the handle hands the decision back to the sentence.
  let splitTap = 0;
  $('split').addEventListener('pointerup', (e) => {
    e.stopPropagation();
    const now = Date.now();
    if (now - splitTap < 320) { setSplit(null); splitTap = 0; }
    else splitTap = now;
  });

  // ---- reloading onto a new page -------------------------------------------
  let pageId = null, reloadWanted = false;
  // Never mid-sentence and never mid-typing. A deploy should not blank the
  // wall in the middle of a reply, and it certainly should not eat a reply
  // somebody is halfway through writing — the whole point of noticing the new
  // page is that the screen ends up right, and a reload timed badly is its own
  // kind of wrong.
  function wantReload() {
    reloadWanted = true;
    maybeReload();
  }
  function maybeReload() {
    if (!reloadWanted) return;
    if (speaking) return;                       // let the reply finish
    if (document.activeElement === $('text')) return;   // let the typing finish
    if (!$('tx').hidden) return;                // let the reading finish
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
    if (!on) { setSubtitle(null); figMsg = false; updFig(); markTx(); maybeReload(); }
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
      if (d.kind === 'video') {
        d.rx = Date.now(); syncVideo(d);
        // Follow the selected channel (popup Tab / another canvas) — unless
        // the user just tapped the channel button here (their choice is on
        // its way to the server; adopting a stale event would flip it back).
        if (d.chan && d.chan !== ch && Date.now() - chTouched > 8000) {
          ch = d.chan; histIdx = 1;
          if (visible) { $('title').textContent = '…'; poll(); }
        }
      }
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
        if (d.speaking) stopSaySpin();     // audio started → the play button stops loading
        if (d.speaking) holdWake(45000);   // rolling hold while a voice is live
        setSpeaking(!!d.speaking);
        // Outside the speaking branch: the transcript is the LAST reply's
        // until a new one replaces it, and it is wanted most once the voice
        // has stopped. Inside, it existed only while you were being read to.
        if (d.lines) setLines(d.lines, typeof d.lidx === 'number' ? d.lidx : 0);
        if (d.speaking) {
          setSubtitle(d.sentence || null);
          figMsg = !!d.visual; updFig();
          applyStale(d.session || null);
        } else {
          // The voice has stopped, and the band must not become a reserved
          // strip of nothing: bandOn() is true whenever there is a transcript,
          // so leaving the subtitle empty carved a hole out of the picture and
          // put nothing in it. Show the last thing said, dimmed, which also
          // keeps the door where the eye last saw it — the band is what you
          // tap for the whole reply.
          setSubtitle(txLines.length ? txLines[Math.max(0, txIdx)] : null, true);
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

  // Activity beacon: tell the server this screen has eyes on it (names the
  // wake target for figure pushes). Identity = our tailnet IP, so no pairing
  // needed; only an explicit SCREEN override rides the token.
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

  // ---- audio controller: same verbs as the tmux popup, as touch buttons ----
  const GLYPH = { speech: 'note', music: 'notes', book: 'book' };
  const ORDER = ['speech', 'music', 'book'];
  let ch = 'speech', histIdx = 1, visible = false;
  let hideTimer = null, pollTimer = null;
  let chTouched = 0;   // last local channel tap — wins over server sync briefly

  function speechOnly(showIt) {
    for (const el of document.querySelectorAll('#ctl .sp'))
      el.style.display = showIt ? '' : 'none';
  }

  function render(d) {
    $('chan').innerHTML = icon(GLYPH[ch]);
    const t = $('title');
    if (t.textContent !== d.label) {
      t.textContent = d.label;
      t.classList.remove('scroll');
      requestAnimationFrame(() => {           // measure after reflow
        const over = t.scrollWidth - $('marq').clientWidth;
        if (over > 8) {
          t.style.setProperty('--marq-shift', (-over) + 'px');
          t.style.setProperty('--marq-dur', (8 + over / 20) + 's');
          t.classList.add('scroll');
        }
      });
    }
    // The play/pause BUTTON shows the action it triggers (popup convention):
    // playing (▶ status) → show pause, else show play.
    const playing = d.status.startsWith('▶');
    $('pp').innerHTML = icon(playing ? 'pause' : 'play');
    // "00:12 / 02:05" → "00:12/02:05": two columns saved, same trick as the
    // tmux popup — the difference between fitting and clipping on a phone.
    const clock = (d.status.replace(/^[▶⏸○]\s*/, '') || '○').replace(' / ', '/');
    $('clock').textContent = clock;
    // Background-fill progress: the clock doubles as the bar (no extra row).
    const secs = (s) => s.split(':').reduce((a, v) => a * 60 + (+v || 0), 0);
    const m = clock.match(/^([\d:]+)\/([\d:]+)$/);
    const frac = m && secs(m[2]) > 0
      ? Math.max(0, Math.min(1, secs(m[1]) / secs(m[2]))) : null;
    // e-ink fill dark enough to survive DU4's 4-level quantization (a 16%
    // grey rounds to white there); the track stays as a hairline via border.
    const fill = einkOn() ? 'rgba(0,0,0,.32)' : 'rgba(255,215,95,.28)';
    const track = einkOn() ? 'rgba(0,0,0,.08)' : 'rgba(255,255,255,.07)';
    $('clock').style.background = frac === null ? 'none'
      : 'linear-gradient(90deg, ' + fill + ' ' + (frac * 100).toFixed(1)
        + '%, ' + track + ' ' + (frac * 100).toFixed(1) + '%)';
    $('mute').classList.toggle('lit', !!d.muted);
    speechOnly(ch === 'speech');
  }

  async function poll() {
    if (!visible) return;
    try {
      const d = await fetch('/status?channel=' + ch).then(r => r.json());
      if (d.channel === ch) render(d);
    } catch (_) {}
  }

  // Four focus states, mirroring the tmux-popup model:
  //   passive — just the image; the bottom input rests dim, hotkeys OFF.
  //   input   — bottom field focused; type a reply (Enter sends).
  //   agents  — the tree expanded under a key cursor (drops out when empty).
  //   control — controller focused; single-key hotkeys live (`n` = channel).
  // Tab and bare-canvas taps walk the ring passive→input→agents→control→
  // passive; Esc / q bail out to passive from anywhere. EVERY transition goes
  // through setMode — it owns all class toggles, timers, and the tree cursor.
  let mode = 'passive';
  const RING = { passive: 'input', input: 'agents',
                 agents: 'control', control: 'passive' };

  function tabNext() {
    if (RING[mode] === 'input') openInput();   // also refreshes reply targets
    else setMode(RING[mode]);
  }

  function resetHide() {              // idle non-passive modes unwind to passive
    clearTimeout(hideTimer);
    if (mode === 'passive') return;
    // CONTROL rests quickly (15s, its old cadence); INPUT and AGENTS linger
    // (30s) — and a reply draft in progress is never discarded: re-arm and
    // look again later instead of dropping the mode out from under it.
    hideTimer = setTimeout(() => {
      if (mode === 'input' && $('text').value.trim()) { resetHide(); return; }
      setMode('passive');
    }, mode === 'control' ? 15000 : 30000);
  }

  function setMode(m) {
    if (m === 'agents' && !$('agents').classList.contains('on'))
      m = 'control';                 // no tree → that ring stop drops out
    const was = mode;
    mode = m;
    const ctrl = (m === 'control');
    const active = (ctrl || m === 'input');   // dock is engaged
    visible = ctrl;                          // controller (polled) only in CONTROL
    $('inp').classList.toggle('on', m === 'input');       // focus ring
    $('inp').classList.toggle('under', ctrl);             // hidden beneath controller
    $('ctl').classList.toggle('on', ctrl);
    $('ctl').classList.toggle('focused', ctrl);
    // Hide the tree while typing, and while the controller holds the dock —
    // the pill at the bottom edge would poke through the controller's rows.
    $('agents').classList.toggle('hide', active);
    if (m === 'agents') {
      // Expand under a key cursor; pollAgents freezes re-renders while here.
      agTop = true; agCursor = 0;
      $('agents').classList.add('expanded');
      agPaintCursor();
    } else if (was === 'agents' || active) {
      // Leaving the tree (or burying it under the dock) collapses it back to
      // the pill — agTop is what re-renders read, so clearing the class alone
      // gets re-expanded on the next /agents poll. A tree the user expanded
      // by tapping the pill in PASSIVE stays as they left it.
      agTop = false;
      $('agents').classList.remove('expanded');
      agClearCursor();
    }
    $('cap').classList.toggle('hide', active);
    if (m === 'input') $('text').focus();
    else if (document.activeElement === $('text')) $('text').blur();
    clearInterval(pollTimer);
    if (ctrl) { poll(); pollTimer = setInterval(() => { if (!document.hidden) poll(); }, 2000); }
    if (!active && !$('sub').classList.contains('on'))
      $('cap').classList.remove('hide');
    resetHide();
  }

  async function act(action, arg, sarg) {
    resetHide();
    let r = null;
    try {
      // authed(), not a bare fetch: /ctl is a state-changing POST and needs
      // the same token /input does. This called fetch directly, so every
      // control action — every transport button, and tapping a sentence to
      // play from there — came back 401 and was thrown away by the catch
      // below without a word. It looked exactly like the actions doing
      // nothing, which is what they were doing.
      const res = await authed('/ctl', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ channel: ch, action: action, arg: arg, sarg: sarg }),
      });
      if (!res.ok) {
        // Say so. A silent failure here is indistinguishable from a control
        // that does not work, and that ambiguity cost days.
        toast(res.status === 401 ? 'not paired — cannot control playback'
                                 : 'that did not work (' + res.status + ')');
        return null;
      }
      r = await res.json();
      // Speech ⏮ semantics ride on replay-prev's echoed cursor (the popup
      // folds the same echo into hist_idx).
      if (action === 'prev' && ch === 'speech' && r.out && /^\d+$/.test(r.out))
        histIdx = parseInt(r.out, 10);
    } catch (_) {}
    setTimeout(poll, 300);                     // let the action land, then refresh
    return r;
  }

  // Transient top-center status message (~2.6s).
  let toastT = null;
  function toast(msg) {
    const t = $('toast');
    t.textContent = msg;
    t.classList.add('on');
    clearTimeout(toastT);
    toastT = setTimeout(() => t.classList.remove('on'), 2600);
  }
  // The ring is invisible to a newcomer (the `?` help needs a keyboard), so
  // the first few bare-canvas taps narrate where the walk goes — then it
  // stays quiet forever (per-device counter).
  function ringHint() {
    const n = +(localStorage.getItem('ringhint') || 0);
    if (n >= 3) return;
    localStorage.setItem('ringhint', String(n + 1));
    toast('tap walks: reply → agents → controls → off  ·  ✕ / Esc exits');
  }
  // Popup `w` — open the active channel's web UI (speech → canvas, music →
  // Iris, book → mpvc). No UI configured/installed (empty result) → a toast
  // instead of a dead tab; a blocked popup → surface the URL so it's reachable.
  async function openWeb() {
    const r = await act('web');
    const url = r && r.out && r.out.trim();
    if (!url || url.slice(0, 4) !== 'http') {
      toast(ch + ' web UI not available');
      return;
    }
    // A loopback URL is the media host's own localhost — not reachable from a
    // remote canvas (phone/wall). Surface the address rather than a dead tab.
    if (url.indexOf('//127.0.0.1') >= 0 || url.indexOf('//localhost') >= 0) {
      toast('web UI (on media host): ' + url);
      return;
    }
    if (!window.open(url, '_blank')) toast(url);   // popup blocked → show it
  }
  // In-page input sheet — replaces native prompt() so it honours the e-ink
  // theme and isn't a dead modal on a keyboardless wall (#142). Resolves to the
  // entered string, or null on cancel / Esc / tap-away.
  let sheetResolve = null;
  function askSheet(title, placeholder, value) {
    return new Promise((resolve) => {
      if (sheetResolve) { const r = sheetResolve; sheetResolve = null; r(null); }
      sheetResolve = resolve;
      $('sheettitle').textContent = title;
      const inp = $('sheetin');
      inp.placeholder = placeholder || '';
      inp.value = value || '';
      $('sheet').classList.add('on');
      setTimeout(() => { inp.focus(); inp.select(); }, 30);
    });
  }
  function closeSheet(val) {
    if (!$('sheet').classList.contains('on')) return;
    $('sheet').classList.remove('on');
    const r = sheetResolve; sheetResolve = null;
    if (r) r(val);
  }
  $('sheetok').onclick = (e) => { e.stopPropagation(); closeSheet($('sheetin').value); };
  $('sheetcancel').onclick = (e) => { e.stopPropagation(); closeSheet(null); };
  $('sheet').addEventListener('click', (e) => {
    e.stopPropagation();
    if (e.target === $('sheet')) closeSheet(null);   // tap the scrim → cancel
  });
  $('sheetin').addEventListener('keydown', (e) => {
    e.stopPropagation();                             // the sheet owns its keys
    if (e.key === 'Enter') { e.preventDefault(); closeSheet($('sheetin').value); }
    else if (e.key === 'Escape') { e.preventDefault(); closeSheet(null); }
  });
  // Popup `s` / `o` — typed seek and open-URL (music/book only; speech uses h/l).
  async function typedSeek() {
    if (ch === 'speech') { toast('typed seek — music / book only'); return; }
    const t = await askSheet('seek — ' + ch, 'H:MM:SS · +90 · -5:00', '');
    if (t && t.trim()) act('seek-to', 1, t.trim());
  }
  async function typedOpen() {
    if (ch === 'speech') { toast('open URL — music / book only'); return; }
    const u = await askSheet('open in ' + ch, 'paste a URL to play', '');
    if (u && u.trim()) act('open-url', 1, u.trim());
  }
  async function webSearch() {
    if (ch === 'speech') { toast('search — music / book only'); return; }
    const q = await askSheet('search ' + ch, 'query (title/author/etc)', '');
    if (q !== null) act('search', 1, q.trim());
  }
  function toggleHelp() { $('help').classList.toggle('on'); }

  // ---- input box: reply to whoever just spoke (token-authed) ---------------
  let targets = ['speaker'], tIdx = 0, targetLabels = {};
  function token() { return localStorage.getItem('amux_token') || ''; }
  async function askToken() {
    const t = await askSheet('amux auth token', 'from ~/.amux/auth_token', '');
    if (t && t.trim()) { localStorage.setItem('amux_token', t.trim()); return true; }
    return false;
  }
  async function authed(url, opts) {
    opts = opts || {};
    opts.headers = Object.assign({'X-Auth-Token': token()}, opts.headers);
    let r = await fetch(url, opts);
    if (r.status === 401) {
      // Point at the phone-friendly pairing QR (a 40-char token is misery to
      // type on a wall) and offer the in-page sheet — no native modal (#142).
      toast('not paired — scan the QR at ' + location.host + '/pair, or enter the token');
      if (await askToken()) {
        opts.headers['X-Auth-Token'] = token();
        r = await fetch(url, opts);
      }
    }
    return r;
  }
  function drawTarget() {
    const t = targets[tIdx];
    const label = t === 'speaker' ? 'speaker' : (targetLabels[t] || t.slice(5));
    $('target').innerHTML = (t === 'speaker' ? icon('reply') : icon('book')) + label;
  }
  drawTarget();
  async function openInput() {
    setMode('input');
    try {
      const d = await authed('/sessions').then(r => r.json());
      targets = ['speaker'].concat((d.amux || []).map(n => 'amux:' + n));
      tIdx = 0; drawTarget();
    } catch (_) {}
  }
  function closeInput() { setMode('passive'); }
  $('kbd').onclick = (e) => { e.stopPropagation(); openInput(); };
  $('target').onclick = (e) => {
    e.stopPropagation();
    tIdx = (tIdx + 1) % targets.length;
    drawTarget();
  };
  async function sendText() {
    const text = $('text').value.trim();
    if (!text) return;
    $('send').textContent = '…';
    try {
      const r = await authed('/input', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({text: text, target: targets[tIdx]}),
      }).then(r => r.json());
      if (r.ok) { $('text').value = ''; growText(); $('send').textContent = '✓'; }
      else { $('send').textContent = '✕'; toast(r.detail || 'send failed'); }
    } catch (_) { $('send').textContent = '✕'; }
    setTimeout(() => { $('send').innerHTML = icon('send'); }, 1200);
  }
  $('send').onclick = (e) => { e.stopPropagation(); sendText(); };
  function growText() {                        // auto-grow the reply textarea
    const t = $('text'); t.style.height = 'auto';
    t.style.height = Math.min(t.scrollHeight, 104) + 'px';
  }
  $('text').addEventListener('input', growText);
  $('text').addEventListener('keydown', (e) => {
    // Enter sends; Shift+Enter is a newline (and the box grows to fit).
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendText(); }
    else if (e.key === 'Escape') { e.preventDefault(); setMode('passive'); }
    else if (e.key === 'Tab') { e.preventDefault(); tabNext(); }  // input → agents/control
  });

  document.body.addEventListener('click', (e) => {
    // A tap = eyes on the screen: hold it awake a while. (This was the
    // spike's wake(), left dangling when dc58afa introduced holdWake —
    // the throw silently killed every bare-canvas tap since.)
    holdWake(90000);
    if ($('help').classList.contains('on')) { toggleHelp(); return; }
    if ($('peek').classList.contains('on')) { hidePeek(); return; }  // tap-away closes peek
    if ($('ctl').contains(e.target)) { resetHide(); return; }  // buttons self-handle
    if ($('inp').contains(e.target)) { openInput(); return; }  // tap field → INPUT
    // A bare-canvas tap walks the same ring as Tab (passive→input→agents→
    // control→passive) — so a tap in CONTROL dismisses the controller.
    ringHint();
    tabNext();
  });

  // ---- popup-parity key bindings (for canvases with a keyboard) ------------
  // Focus walks with Tab (passive→input→agents→control) and unwinds with
  // Esc/q. In
  // CONTROL the full tmux-popup (prefix a) hotkey set is live: n channel ·
  // Space play/pause · h/l sentence · H/L paragraph · </> prev/next · -/= vol ·
  // m mute · M keep-muted · v highlight · p clip@cursor · g source · w web UI ·
  // b bookmark · s typed-seek · o open-URL · [/] speed, 0/⌫ reset · r replay · c cc · f sfx ·
  // ? help. Enter → input; Esc/q → passive.
  document.addEventListener('keydown', (e) => {
    if (e.target === $('text')) return;          // the input box owns its keys
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    resetHide();                       // any key = activity; re-arm idle unwind
    const k = e.key;
    // Tree / peek navigation runs BEFORE the mode machinery, so j/k/Enter/p/Esc
    // mean "move the cursor", not "reply / control / toggle playback".
    if ($('peek').classList.contains('on') && peekKey(k)) { e.preventDefault(); return; }
    if (mode === 'agents' && agKey(k)) { e.preventDefault(); return; }
    if (k === 'a' && mode === 'passive' && $('agents').classList.contains('on')
        && !$('peek').classList.contains('on')) {
      e.preventDefault(); setMode('agents'); return;
    }
    if (k === 'Tab') { e.preventDefault(); tabNext(); return; }  // walk / cycle
    // The transcript is a layer over everything, so it takes Escape first —
    // otherwise Escape drops the mode out from under a panel that is still up.
    if (txOpen() && (k === 'Escape' || k === 'z')) {
      e.preventDefault(); openTx(false); return;
    }
    if (k === 'z' && $('sub').classList.contains('on')) {
      e.preventDefault(); openTx(true); return;
    }
    if (k === 'Escape' || (k === 'q' && mode === 'control')) {
      e.preventDefault();
      if ($('help').classList.contains('on')) { toggleHelp(); return; }
      setMode('passive');
      return;
    }
    if (k === 'Enter') { e.preventDefault(); openInput(); return; }
    if (mode !== 'control') return;              // hotkeys are live only in CONTROL
    const fn = CTL_KEYS[k];
    if (!fn) return;
    e.preventDefault();
    fn();
    resetHide();
  });
  // CONTROL-mode hotkeys (popup parity). Named functions are shared with the
  // touch buttons — one code path per verb, whichever surface fired it.
  let lastBookmark = { ch: '', t: 0 };
  function bookmarkAct() {
    const now = Date.now();
    const range = lastBookmark.ch === ch && (now - lastBookmark.t) <= 2000;
    lastBookmark = range ? { ch: '', t: 0 } : { ch, t: now };
    act(range ? 'bookmark-end' : 'bookmark');
  }
  const CTL_KEYS = {
    ' ': () => act('toggle'),
    'h': () => act('skip-'),  'l': () => act('skip+'),
    'H': () => act('para-'),  'L': () => act('para+'),
    '<': prevTrack, '>': nextTrack,
    ',': prevTrack, '.': nextTrack,
    '-': () => act('vol-'),   '=': () => act('vol+'), '+': () => act('vol+'),
    'n': nextChannel,
    'm': () => act('mute'),   'M': () => act('mute-keep'),
    'v': () => act('highlight'), 'p': () => act('clip-cursor', 1),
    'g': () => act('goto'),   'w': openWeb,
    'b': bookmarkAct,
    's': typedSeek,           'o': typedOpen,
    '/': webSearch,
    '[': () => act('speed-'), ']': () => act('speed+'),
    '0': () => act('speed0'), 'Backspace': () => act('speed0'),
    'r': () => act('replay', 1),
    'c': toggleCc,
    'x': toggleSfx,   // sfx — s is typed-seek, f is fit
    'f': cycleFit,
    'e': () => { localStorage.setItem('eink', einkOn() ? '0' : '1');
                 location.reload(); },
    '?': toggleHelp,
  };
  $('xbtn').onclick = (e) => { e.stopPropagation(); setMode('passive'); };
  function drawSfx() {
    $('sfx').innerHTML = icon(sfxOn() ? 'bell' : 'bell-off');
    $('sfx').classList.toggle('lit', sfxOn());
  }
  drawSfx();
  function drawCc() { $('cc').classList.toggle('lit', subsOn()); }
  drawCc();
  function toggleCc() {
    localStorage.setItem('subs', subsOn() ? '0' : '1');
    drawCc();
    if (!subsOn()) setSubtitle(null);
    resetHide();
  }
  $('cc').onclick = (e) => { e.stopPropagation(); toggleCc(); };
  function toggleSfx() {
    localStorage.setItem('sfx', sfxOn() ? '0' : '1');
    drawSfx();
    if (sfxOn()) chime(true);              // audible confirmation + unlocks audio
    resetHide();
  }
  $('sfx').onclick = (e) => { e.stopPropagation(); toggleSfx(); };
  // ---- transcript handlers ---------------------------------------------
  // ?reset=1 — put this screen back to how a fresh one behaves.
  //
  // Every view preference lives in localStorage, per device: captions on or
  // off, fit mode, e-ink, reading size, the split height. That is right for
  // preferences and it is exactly why two screens on the SAME page can behave
  // differently — the app's WebView and a browser tab are two devices, and one
  // of them having captions off is enough to make the words vanish on one and
  // not the other. This is the way back, and it deliberately keeps the pairing
  // token: clearing that would silently cost the ability to control playback,
  // which is not what anybody means by "reset the view".
  if (/(^|[?&])reset=1(&|$)/.test(location.search)) {
    for (const k of ['subs', 'fit', 'eink', 'txscale', 'split', 'sfx'])
      localStorage.removeItem(k);
    location.replace('/');
  }
  // Reading size is a per-device preference and it survives a reload.
  document.documentElement.style.setProperty('--txscale', txScale());
  $('zoom').onclick = (e) => { e.stopPropagation(); openTx(true); resetHide(); };
  // Tap the words — or anywhere in the strip that holds them — to get all of
  // them. The bottom half's taps are its own; they never walk the mode ring
  // that a tap on the picture does.
  const openFromBand = (e) => {
    if (!txLines.length) return;
    e.stopPropagation();
    openTx(true);
    resetHide();
  };
  $('sub').addEventListener('click', openFromBand);
  $('bandhit').addEventListener('click', openFromBand);
  $('txclose').onclick = (e) => { e.stopPropagation(); openTx(false); };
  $('txlive').onclick = (e) => {
    e.stopPropagation();
    txFollow = true; setAhead(false); $('txlive').hidden = true; followTx();
  };
  // Taps inside the panel are the panel's, not the canvas's — without this a
  // tap to dismiss the reveal also reaches the tap-to-control layer beneath.
  // Tapping the masked text is the other way to ask for it — and on a short
  // clip, the only one. Same bargain as the scroll: deliberate, never
  // something the eye does while listening.
  //
  // Tapping any line plays from there, the way read-aloud works.
  //
  // This was a double-tap first, on the worry that a stray touch while
  // scrolling would restart the voice three paragraphs back. That worry does
  // not survive contact with how touch actually works: a scroll is a drag, and
  // a drag does not fire click. The only thing the second tap was protecting
  // against was the first one working.
  $('tx').addEventListener('click', (e) => {
    e.stopPropagation();
    const p = e.target.closest && e.target.closest('#txlines p');
    if (!p) return;
    if (p.classList.contains('ahead') && !document.body.classList.contains('txahead')) {
      setAhead(true);
      return;                       // uncovering is its own act; do not also seek
    }
    playFrom([...$('txlines').children].indexOf(p), p);
  });
  // Play from a sentence. The highlight is moved locally straight away rather
  // than waiting for the next state frame — that is up to a second away, and a
  // tap that appears to do nothing for a second gets tapped again.
  async function playFrom(i, el) {
    if (i < 0) return;
    el.classList.add('seeking');
    setTimeout(() => el.classList.remove('seeking'), 600);
    txIdx = i;
    markTx();
    await act('goto-sentence', 1, String(i));
  }
  $('tx').addEventListener('scroll', txScrolled, { passive: true });
  function drawFit() {
    $('fit').classList.toggle('lit', fitMode() !== 'auto');
    $('fit').style.opacity = fitMode() === 'fill' ? 0.55 : 1;
  }
  drawFit();
  function cycleFit() {
    const next = { auto: 'fit', fit: 'fill', fill: 'auto' }[fitMode()] || 'auto';
    localStorage.setItem('fit', next);
    drawFit();
    // Re-style the image on screen right away, restoring the pan/zoom when
    // the new mode un-fits it.
    const el = layers[front];
    const f = wantFit(lastPurpose);
    applyFit(el, f);
    if (!f) kenBurns(el);
    $('cap').textContent = { auto: 'fit: auto — figures fit, art fills',
                             fit:  'fit: everything fits the screen',
                             fill: 'fill: everything covers the screen' }[next];
    $('cap').classList.remove('hide');
    $('cap').classList.add('on');
    clearTimeout(capTimer);
    capTimer = setTimeout(() => $('cap').classList.remove('on'), 2500);
    resetHide();
  }
  $('fit').onclick = (e) => { e.stopPropagation(); cycleFit(); };
  function nextChannel() {
    ch = ORDER[(ORDER.indexOf(ch) + 1) % ORDER.length];
    histIdx = 1;
    chTouched = Date.now();
    act('select');                 // persist → popup + other canvases follow
    $('title').textContent = '…';
    poll();
    resetHide();
  }
  $('chan').onclick = nextChannel;
  function prevTrack() { act('prev', histIdx); }
  function nextTrack() {
    if (ch !== 'speech') { act('next'); return; }
    if (histIdx > 1) { histIdx -= 1; act('replay', histIdx); }
    else act('jump-end');
  }
  $('pp').onclick  = () => act('toggle');
  $('skb').onclick = () => act('skip-');    // sentence back (±5s music/book)
  $('skf').onclick = () => act('skip+');    // sentence forward
  $('vdn').onclick = () => act('vol-');
  $('vup').onclick = () => act('vol+');
  $('sdn').onclick = () => act('speed-');
  $('sup').onclick = () => act('speed+');
  $('mute').onclick = () => act('mute');
  $('prev').onclick = prevTrack;
  $('next').onclick = nextTrack;

  // ---- agent tree: sessions → their claude panes, with live state ----------
  // Poll /agents (open on the tailnet), group by session into collapsible
  // groups. Each pane shows its state, a peek (output) and a play (its last
  // clip) button; tap a pane label to aim the reply box at it.
  const AG_RANK = { input: 0, approval: 1, working: 2, stopped: 3 };  // needs-you first
  let agOpen = {}, agTop = false;              // session / top-level expanded (persist)
  let agCursor = 0, peekCursor = 0;   // vim-nav cursors (tree focus = mode 'agents')
  const agEsc = (s) => s.replace(/[<>&]/g, (c) => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;' }[c]));
  async function pollAgents() {
    if (document.hidden) return;
    if (mode === 'agents') return;   // frozen while the tree has key focus — don't re-render under the cursor
    let list;
    try {
      const r = await fetch('/agents');
      if (!r.ok) { $('agents').classList.remove('on'); return; }
      list = (await r.json()).agents || [];
    } catch (_) { return; }
    // The transcript's foot: who is still working. Same poll, no second
    // request — the agent strip already asks this question once a beat, and a
    // spinner with its own timer would drift out of step with the tree that
    // explains it.
    setWorking(list.filter((a) => a.state === 'working'));
    const box = $('agents');
    if (!list.length) { box.classList.remove('on'); box.innerHTML = ''; hidePeek(); return; }
    const groups = {};
    for (const a of list) { const s = a.session || a.name; (groups[s] = groups[s] || []).push(a); }
    const best = (ps) => Math.min(...ps.map((p) => AG_RANK[p.state] ?? 9));
    const names = Object.keys(groups).sort((x, y) =>
      best(groups[x]) - best(groups[y]) || x.localeCompare(y));
    const sessHtml = names.map((s) => {
      const ps = groups[s].sort((a, b) =>
        (AG_RANK[a.state] ?? 9) - (AG_RANK[b.state] ?? 9) || a.name.localeCompare(b.name));
      const rows = ps.map((p) =>
        '<div class="pane ' + p.state + '" data-name="' + encodeURIComponent(p.name)
        + '" data-source="' + (p.source === 'tmux' ? 'tmux' : 'amux') + '"'
        + (p.pane ? ' data-pane="' + p.pane + '"' : '') + '>'
        + '<span class="dot"></span><span class="lbl">' + agEsc(p.name) + '</span>'
        + (p.pane ? '<button class="pk" title="peek output">' + icon('cc') + '</button>' : '')
        + '<button class="pl" title="play last clip">' + icon('play') + '</button></div>').join('');
      return '<div class="sess ' + ps[0].state + (agOpen[s] ? ' open' : '')
        + '" data-sess="' + encodeURIComponent(s) + '">'
        + '<div class="shead"><span class="chev">▸</span><span class="dot"></span>'
        + '<span class="sname">' + agEsc(s) + '</span>'
        + '<span class="scount">' + ps.length + '</span></div>'
        + '<div class="panes">' + rows + '</div></div>';
    }).join('');
    // Collapse the whole tree behind one pill; its dot/count give the glance.
    const topState = ['input', 'approval', 'working', 'stopped'][
      Math.min(...list.map((a) => AG_RANK[a.state] ?? 9))] || 'stopped';
    box.innerHTML =
      '<div class="aghead ' + topState + '"><span class="chev">▸</span>'
      + '<span class="dot"></span><span class="atitle">agents</span>'
      + '<span class="scount">' + list.length + '</span></div>'
      + '<div class="aglist">' + sessHtml + '</div>';
    box.classList.add('on');
    box.classList.toggle('expanded', agTop);
  }
  $('agents').addEventListener('click', (e) => {
    e.stopPropagation();
    if (e.target.closest('.aghead')) {          // top pill → show/hide the tree
      agTop = !agTop; $('agents').classList.toggle('expanded', agTop);
      scheduleAgents();                         // expanded → fast poll now (#141)
      return;
    }
    const head = e.target.closest('.shead');
    if (head) {
      const g = head.parentElement, s = decodeURIComponent(g.dataset.sess);
      agOpen[s] = !agOpen[s]; g.classList.toggle('open', agOpen[s]); return;
    }
    const row = e.target.closest('.pane');
    if (!row) return;
    if (e.target.closest('.pl')) { playPane(row.dataset.pane, e.target.closest('.pl')); return; }
    if (e.target.closest('.pk')) { peekPane(row.dataset.pane, decodeURIComponent(row.dataset.name)); return; }
    targetAgent(decodeURIComponent(row.dataset.name), row.dataset.source, row.dataset.pane);
  });
  // ---- play-load spinner: say/replay block for seconds (render + queue) before
  // audio starts, so a tapped play button spins until speech actually begins
  // (a 'state' event with speaking:true clears it) or a fallback timeout fires.
  let saySpinEl = null, saySpinPrev = '', saySpinTimer = null;
  function startSaySpin(btn) {
    stopSaySpin();
    if (!btn) return;
    saySpinEl = btn; saySpinPrev = btn.innerHTML;
    btn.innerHTML = '<svg class="ic spin"><use href="#i-spinner"/></svg>';
    saySpinTimer = setTimeout(stopSaySpin, 25000);   // never spin forever
  }
  function stopSaySpin() {
    clearTimeout(saySpinTimer); saySpinTimer = null;
    if (saySpinEl) { saySpinEl.innerHTML = saySpinPrev; saySpinEl = null; saySpinPrev = ''; }
  }
  async function playPane(pane, btn) {
    if (!pane) return;
    startSaySpin(btn);
    try {
      // /play is an auth-gated state-changing POST (it drives audio) — send the
      // token like /input, else the server 401s and the clip never plays.
      const r = await authed('/play', { method: 'POST',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ pane }) });
      const j = await r.json().catch(() => null);
      if (!r.ok || (j && j.ok === false)) stopSaySpin();   // rejected / nothing to replay → drop the spinner now
    } catch (_) { stopSaySpin(); }
  }
  let peekTurns = [];
  async function peekPane(pane, name) {
    if (!pane) return;
    try { peekTurns = ((await (await fetch('/peek?pane=' + encodeURIComponent(pane))).json()).turns) || []; }
    catch (_) { peekTurns = []; }
    // Chronological like a real transcript: oldest at top, newest at the
    // bottom (open/full); older ones are collapsed snapshots you click to
    // expand. ▶ on each plays that turn. Opens scrolled to the latest.
    const last = peekTurns.length - 1;
    const blocks = peekTurns.map((t, i) =>
      '<div class="turn' + (i === last ? ' open' : '') + '" data-i="' + i + '">'
      + '<button class="tplay" title="play this turn">' + icon('play') + '</button>'
      + '<div class="tbody">' + agEsc(t) + '</div></div>').join('');
    $('peek').innerHTML = '<div class="ph">' + agEsc(name) + '</div>'
      + (blocks || '<div class="tbody" style="max-height:none">(no transcript / output)</div>');
    $('peek').classList.add('on');
    peekCursor = peekTurns.length - 1;      // start on the latest turn (the open one)
    requestAnimationFrame(() => { $('peek').scrollTop = $('peek').scrollHeight; peekPaintCursor(); });
  }
  function hidePeek() { $('peek').classList.remove('on'); }
  $('peek').addEventListener('click', (e) => {
    e.stopPropagation();
    const play = e.target.closest('.tplay');
    if (play) { sayTurn(peekTurns[+play.parentElement.dataset.i], play); return; }
    const turn = e.target.closest('.turn');
    if (turn) { turn.classList.toggle('open'); return; }  // expand/collapse a snapshot
    hidePeek();
  });
  async function sayTurn(text, btn) {
    if (!text) return;
    startSaySpin(btn);
    try {
      // /say is an auth-gated state-changing POST (it speaks) — send the token
      // like /input, else the server 401s and nothing is spoken.
      const r = await authed('/say', { method: 'POST',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }) });
      const j = await r.json().catch(() => null);
      if (!r.ok || (j && j.ok === false)) stopSaySpin();   // rejected / render failed → drop the spinner
    } catch (_) { stopSaySpin(); }
  }
  async function targetAgent(name, source, pane) {
    await openInput();
    // tmux agents are addressed by pane id (a session may hold several); amux
    // agents by name. Remember the friendly label for the target chip.
    const t = source === 'tmux' ? 'tmux:' + (pane || name) : 'amux:' + name;
    targetLabels[t] = name;
    let idx = targets.indexOf(t);
    if (idx < 0) { targets.push(t); idx = targets.length - 1; }
    tIdx = idx; drawTarget();
    $('text').focus();
  }

  // ---- vim-key navigation: the agent tree + peek panel, tmux-chooser style --
  // 'a' focuses the tree; j/k (or arrows) walk the visible heads/panes; l/Enter
  // opens a session or aims the reply box at a pane; h collapses; g/G jump; p
  // peeks; Esc/q leave. In the peek panel j/k walk turns, Enter expands, p plays.
  function agRows() {
    // Visible navigable rows in view order: the top "agents" pill first, then —
    // when the tree is expanded — each session head and its panes (a closed
    // session's panes are display:none, so skipped).
    const head = $('agents').querySelector('.aghead');
    const out = head ? [head] : [];
    if ($('agents').classList.contains('expanded'))
      for (const sess of $('agents').querySelectorAll('.aglist .sess')) {
        out.push(sess.querySelector('.shead'));
        if (sess.classList.contains('open'))
          for (const p of sess.querySelectorAll('.pane')) out.push(p);
      }
    return out;
  }
  function agPaintCursor() {
    for (const el of $('agents').querySelectorAll('.cursor')) el.classList.remove('cursor');
    const rows = agRows();
    if (!rows.length) return;
    agCursor = Math.max(0, Math.min(agCursor, rows.length - 1));
    const cur = rows[agCursor];
    cur.classList.add('cursor');
    cur.scrollIntoView({ block: 'nearest' });
  }
  function agClearCursor() {
    for (const el of $('agents').querySelectorAll('.cursor')) el.classList.remove('cursor');
  }
  function agKey(k) {
    const rows = agRows();
    if (!rows.length) { if (k === 'Escape' || k === 'q') { setMode('passive'); return true; } return false; }
    const cur = rows[agCursor];
    const isTop = cur.classList.contains('aghead'), isPane = cur.classList.contains('pane');
    if (k === 'j' || k === 'ArrowDown') { agCursor = Math.min(agCursor + 1, rows.length - 1); agPaintCursor(); return true; }
    if (k === 'k' || k === 'ArrowUp')   { agCursor = Math.max(agCursor - 1, 0); agPaintCursor(); return true; }
    if (k === 'g') { agCursor = 0; agPaintCursor(); return true; }
    if (k === 'G') { agCursor = rows.length - 1; agPaintCursor(); return true; }
    if (k === 'l' || k === 'Enter' || k === 'ArrowRight') {
      if (isTop) {                          // expand the whole tree from the pill
        agTop = true; $('agents').classList.add('expanded'); agPaintCursor();
      } else if (isPane) {                  // aim the reply box at this pane
        // targetAgent → openInput → setMode('input') collapses the tree.
        targetAgent(decodeURIComponent(cur.dataset.name), cur.dataset.source, cur.dataset.pane);
      } else {                              // expand the session — its panes appear
        const sess = cur.parentElement;
        agOpen[decodeURIComponent(sess.dataset.sess)] = true;
        sess.classList.add('open'); agPaintCursor();
      }
      return true;
    }
    if (k === 'h' || k === 'ArrowLeft') {
      if (isTop) {                          // collapse the whole tree back into the pill
        agTop = false; $('agents').classList.remove('expanded'); agPaintCursor();
      } else if (isPane) {                  // collapse the parent, land on its head
        const sess = cur.closest('.sess');
        agOpen[decodeURIComponent(sess.dataset.sess)] = false;
        sess.classList.remove('open');
        agCursor = agRows().indexOf(sess.querySelector('.shead'));
        agPaintCursor();
      } else if (cur.parentElement.classList.contains('open')) {  // collapse an open session
        agOpen[decodeURIComponent(cur.parentElement.dataset.sess)] = false;
        cur.parentElement.classList.remove('open'); agPaintCursor();
      } else {                              // a closed head: step up to the pill
        agCursor = 0; agPaintCursor();
      }
      return true;
    }
    if (k === 'p') {
      if (isPane && cur.dataset.pane)
        peekPane(cur.dataset.pane, decodeURIComponent(cur.dataset.name));
      return true;
    }
    if (k === 'Escape' || k === 'q') { setMode('passive'); return true; }
    return false;
  }
  function peekRows() { return Array.from($('peek').querySelectorAll('.turn')); }
  function peekPaintCursor() {
    const rows = peekRows();
    for (const el of rows) el.classList.remove('cursor');
    if (!rows.length) return;
    peekCursor = Math.max(0, Math.min(peekCursor, rows.length - 1));
    const cur = rows[peekCursor];
    cur.classList.add('cursor');
    cur.scrollIntoView({ block: 'nearest' });
  }
  function peekKey(k) {
    const rows = peekRows();
    if (!rows.length) { if (k === 'Escape') { hidePeek(); return true; } return false; }
    if (k === 'j' || k === 'ArrowDown') { peekCursor = Math.min(peekCursor + 1, rows.length - 1); peekPaintCursor(); return true; }
    if (k === 'k' || k === 'ArrowUp')   { peekCursor = Math.max(peekCursor - 1, 0); peekPaintCursor(); return true; }
    if (k === 'l' || k === 'ArrowRight') { rows[peekCursor].classList.add('open'); return true; }     // expand the snippet
    if (k === 'h' || k === 'ArrowLeft')  { rows[peekCursor].classList.remove('open'); return true; }  // collapse it
    if (k === 'Enter') { rows[peekCursor].classList.toggle('open'); return true; }
    if (k === 'p') { sayTurn(peekTurns[+rows[peekCursor].dataset.i], rows[peekCursor].querySelector('.tplay')); return true; }
    if (k === 'Escape') { hidePeek(); return true; }
    return false;
  }

  // Adaptive cadence (#141): poll fast only while the tree is expanded and
  // someone's watching states change; when collapsed, drop to a slow heartbeat
  // — enough to keep the "who needs me" pill's dot/count live and to discover
  // new agents, without the every-4s host-side subprocess storm. Idle while
  // backgrounded (pollAgents already no-ops on document.hidden).
  let agTimer = null;
  function scheduleAgents() {
    clearTimeout(agTimer);
    const ms = document.hidden ? 30000 : (agTop ? 4000 : 12000);
    agTimer = setTimeout(() => { pollAgents().then(scheduleAgents); }, ms);
  }
  pollAgents().then(scheduleAgents);
  document.addEventListener('visibilitychange',
    () => { if (!document.hidden) { pollAgents(); scheduleAgents(); } });
