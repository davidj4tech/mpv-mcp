# Read-along for the book channel: the ePub as subtitles, and a canvas for books

Status: proposal, nothing built.
Date: 2026-09-09

David's question, verbatim: *"bringing in an ePub file for an audiobook for
text highlighting the spoken words like subtitles... I'm also curious about
having a canvas for audiobooks too which I guess would require the
subtitles?"*

## Recommendation in one line

Yes to both, in that order — and the canvas half is nearly free, because the
canvas already has a subtitle band and the book channel already reports its
position. The whole job is producing **one timings file per book**, and that
is a forced-alignment problem we have not touched before.

## What already exists (this is most of the argument)

- **A subtitle band.** The canvas shows the sentence being spoken, fed by the
  `sentence` field of the speech-state SSE (`canvas.py:speech_state`,
  `canvas.js:setSubtitle`). `?subs=0/1` already switches it per device
  (113a643). It is channel-agnostic on the client; only the server side is
  speech-shaped.
- **A ~1 Hz position feed from the phone's mpv.** The video-sync poller in
  `canvas.py` already reads the phone's mpv IPC once a second and broadcasts
  `{"kind":"video", t, paused, rate}` while screens are connected. The book
  socket is the same protocol one path over — **but only when the book is
  playing through mpv**, which since the Sasonica build landed on p8a
  (2026-09-08) is no longer the usual case. See "Which player is the clock".
- **A book observer.** `book_observer.py` sits on `sink-book.sock`, knows the
  current file and `time-pos`, and already handles "somebody else loaded
  this file" — so the read-along does not care who pressed play.
- **The ePub reader on the phone.** Sasonica inherits Audiobookshelf's
  `components/readers/EpubReader.vue`, and ABS lets an item carry an ebook
  file beside its audio. What ABS does *not* have is any link between the
  reader's position and the player's — "immersion reading" is the one Kindle
  feature nobody open-source has shipped in a library app.

So the missing piece is exactly one artefact: **for this audiobook, which
sentence of the ePub is being read at second *t*.**

## The alignment, which is the actual work

Commercial audiobooks carry no text timing. The ePub has the words, the audio
has the seconds, and something has to marry them once per book. Three ways,
in order of preference:

1. **Storyteller** (`storyteller-platform`, self-hosted, AGPL). It does
   precisely this — takes an ePub and the audiobook, forced-aligns them, and
   emits an EPUB 3 with Media Overlays plus its own read-along Android app.
   Before writing any aligner, run one book through it. Two outcomes are
   both wins: either its app is the answer and we integrate at the
   library level, or its output (SMIL, sentence-level) is the timings file
   and we keep our own players. It uses whisper under the hood, so see the
   cost note below.
2. **aeneas.** Built for ePub-text-plus-audio; CPU only, fast (it
   synthesises the text with espeak and DTWs the MFCCs, no neural model).
   Emits sentence timings as JSON or SMIL. Weakness: it assumes the text and
   the audio say the same words in the same order, so front matter,
   abridgements, and a narrator who skips the epigraphs each need a chapter
   to be trimmed by hand first. For a faithful unabridged read it is the
   cheap, right answer, and it runs comfortably on red5's four cores.
3. **whisperX** (transcribe with word timestamps, then align the transcript
   to the ePub text). Tolerant of mismatch, gives *word*-level timing, and is
   the only route to karaoke-style highlighting. Costs: a model download, and
   on red5's CPU the `small` model runs roughly real-time — a ten-hour book
   is a ten-hour job. If hpo has a usable GPU it is minutes. Transcript-to-text
   matching is a second step (diff the two token streams, anchor on
   agreement, interpolate between anchors) that we would write.

Pick: **try 1, build on 2, keep 3 for word-level later.** Sentence-level is
what the canvas band shows anyway, and sentence-level is what the Pine Note
can refresh.

The output, whichever aligner: `<book>.align.json`, a list of
`{start_s, end_s, chapter, text}` in reading order, stored next to the audio
(or in ABS's item folder, where the app can fetch it). One file per book,
produced once, never touched again.

## Which player is the clock

The first draft of this assumed mpv. That was wrong for the phone. There are
two players today and only one of them is wired in:

- **The book channel is mpv** on `sink-book.sock` — Termux mpv on the phone,
  or red5's feeding the rooms. The observer, `media book`, and the canvas all
  talk to that socket.
- **Sasonica plays Audiobookshelf items in ExoPlayer**, and that is what a
  Bluetooth listen on the phone actually is. The only bridge is
  `media-abs-cast-watcher`, which notices an app session advancing, starts the
  same file on mpv, and closes the app session — the app is treated as a
  remote control for mpv, not as a player.

So when David listens in the app, mpv knows nothing and the once-a-second
socket poll has nothing to read. **ExoPlayer has to be the primary clock and
mpv the fallback**, not the other way round. Two ways to hear it:

1. **Interpolate from Audiobookshelf (no app change).** The app syncs
   progress every few seconds and immediately on pause and seek; the server
   emits `user_item_progress_updated` on its socket.io feed. The canvas
   server subscribes, and between reports runs a local clock:
   `t = last_reported + (now - reported_at) × rate` while playing, frozen
   while paused. Drift is bounded by the sync interval and reset on every
   report, and sentence-level display tolerates a second or two. This is
   the version to build first because it costs nothing on the phone.
2. **Have the app report (exact).** A hook-sized `// Sasonica:` block that
   posts `{item, t, paused, rate}` once a second to the canvas server, the
   way the companion app exposes its state on `127.0.0.1:8770`. This is the
   same seam Path B needs — the reader must ask the player where it is — so
   it is not extra work, just earlier.

Either way the server ends up with one `book position` source that has a
`{uri, t, paused, rate, source: "abs"|"mpv"}` shape, and everything
downstream reads that. mpv stays a source for the rooms and for anything
`media book play` starts.

One thing to decide with it: the cast watcher currently *moves* an app
session to mpv. With the app as a first-class clock that behaviour becomes
optional — a "send to the rooms" action, not a reflex.

## Path A — the canvas (small)

1. In `canvas.py`, a book-position source: the ABS progress feed with
   interpolation (above), falling back to the book socket (`path`,
   `time-pos`, `pause`, `speed`) read in the same batched round-trip the
   video poller already makes.
2. Server-side lookup: load `<book>.align.json` for the current file (keyed
   by normalised URI, cached), bisect on `time-pos`, and broadcast
   `{"kind":"book", sentence, chapter, t, dur, paused}` at the same
   change-only cadence the speech state uses.
3. `canvas.js`: a `book` message drives the same `setSubtitle` and band
   reservation. Speech keeps priority — while a reply is being spoken the
   band shows the reply, and the book's sentence returns when the voice
   stops, the way video already yields to a figure.
4. Tapping a sentence seeks, which the canvas already does for speech;
   the seek goes to the book socket instead.

That is one afternoon once one book is aligned. Nothing new is recorded and
nothing new runs on the phone.

**The Pine Note is the best screen for this.** DU4 draws text crisply and
draws nothing else well. A page of the current paragraph with the live
sentence in bold, refreshed once a sentence, is the e-ink canvas finally
doing the thing it is for — and it needs the `eink` mode to stop hiding the
band and to show a paragraph of context rather than a strip.

## Path B — the phone, in Sasonica (bigger, later)

Same timings file, consumed by `EpubReader.vue`: while the item's audio is
playing, scroll the reader to the current sentence and highlight it;
tapping a sentence seeks the player. This is the immersion-reading feature
ABS lacks, and the fork-side discipline still applies (`sasonica-keep-
upstream-mergeable`: a hook-sized `// Sasonica:` block in the reader that
polls the local player's position, the lookup in a fork-only module). Worth
doing only after Path A proves the alignment is good enough to look at.

## Three screens, one stream

David's picture of the room, 2026-09-09: **a TV as the canvas, an e-ink
tablet for the follow-along text, and a phone playing the audio over
Bluetooth.** Nothing in it needs a new mechanism — each device is a role the
system already has — but it changes what the timings file must carry and
what the e-ink mode is *for*.

- **The phone is the clock.** In practice that is Sasonica's ExoPlayer,
  heard through the ABS progress feed (or the app's own report); mpv only
  when `media book play` started it. Bluetooth adds
  a couple of hundred milliseconds of audio latency, which is invisible at
  sentence granularity; it would matter for word-level highlighting, which
  is one more reason not to start there. Every screen subscribes to the same
  `{"kind":"book"}` stream.
- **The tablet is the reader.** Today the Pine Note is a *mirror* of the wall
  canvas with the video and mid-tones hidden. This makes it a different
  surface reading the same stream: a paragraph view — the current paragraph
  set as text, the live sentence in bold, the band gone — refreshed once per
  sentence, and a page turn at each paragraph boundary. That is the part
  e-ink does better than any other screen in the house, and it is what
  Path A should build first.
- **The TV is the illustrator.** Same page in wall mode, with the subtitle
  band. The canvas already draws a Venice picture per spoken reply; it can
  draw one per *scene* of the book, prompted from the sentences around the
  current position, so the TV becomes ambient illustration that turns with
  the pages. Rendered ahead of time, one per chapter or per N paragraphs,
  into the same spool the speech pictures use — playback never waits on an
  image, and the spool's gc keeps it bounded.

Consequences for the rest of the proposal:

1. **The timings file carries paragraph boundaries**, not just sentences:
   `{start_s, end_s, chapter, paragraph, text}`. The ePub has them; the
   aligner must not flatten them. (aeneas aligns at whatever granularity it
   is given, so this is a matter of how the text is fed in, not a feature.)
2. **The stream carries the paragraph**, so the tablet does not have to
   reconstruct it: `{"kind":"book", sentence, paragraph: [..sentences..],
   sentence_idx, chapter, t, dur, paused}`.
3. **Illustration is a later step**, after the reader view works. It needs a
   scene-detection heuristic (every N paragraphs, or a chapter break) and a
   prompt built from the text, and it is the first place the book channel
   spends money per book rather than per reply.

## What it will not do

- **Word-level highlighting** on any e-ink surface. The refresh cannot keep up.
- **Books read by agent-media's own voice** need none of this — the renderer
  already reports per-clip sentences (see §4 of `2026-08-30-speech-epub-
  export.md`). This proposal is for books somebody else narrated.
- **Imperfect ePubs.** A pirated or OCR'd ePub with a different edition's text
  will align badly. The alignment step should print a confidence (aeneas
  gives one per fragment) and refuse to publish a file below a floor, so a
  bad book shows no subtitles rather than wrong ones.

## Order of work

1. Pick one unabridged book David owns as both ePub and audio. Run it through
   Storyteller in a container on red5; note wall-clock and whether the
   result opens in Thorium with highlighting. **Decision point:** is the
   Storyteller app good enough on its own?
2. Regardless, write `media book align <audio> <epub>` around aeneas,
   producing `<book>.align.json` with per-sentence confidence. Chapter
   mapping by ordinal, with a `--chapters` override for books whose ePub
   spine and audio tracks disagree.
3. The book-position source in `canvas.py`: ABS feed with interpolation,
   mpv fallback. Check it against the app by ear before building any view.
4. Path A: the Note's paragraph view first, then the wall canvas band,
   then scene illustration on the TV.
5. Path B in Sasonica, if 4 gets used — at which point the app reports
   its own position and the interpolation retires.

Cost note: red5 is 4 vCPU and 7 GB. aeneas fits; whisper does not fit
comfortably and should run elsewhere or overnight.
