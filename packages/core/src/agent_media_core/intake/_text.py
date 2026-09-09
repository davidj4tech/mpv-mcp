"""Small text-cleanup helpers shared across intake adapters."""

from __future__ import annotations

import os
import re


#: Blocks the harness writes into a user prompt that the listener never said:
#: a finished background task, a system reminder, the echo of a slash command.
#: They arrived in the transcript as listener turns and were read aloud — one
#: task notification rendered 230KB of somebody spelling out a tool-use id.
_SYSTEM_BLOCKS = re.compile(
    r"<(task-notification|system-reminder|local-command-caveat|local-command-stdout"
    r"|command-name|command-message|command-args)\b.*?</\1>",
    re.DOTALL | re.IGNORECASE)
_SYSTEM_BANNER = re.compile(
    r"\[SYSTEM NOTIFICATION - NOT USER INPUT\].*?(?=\n\s*\n|\Z)",
    re.DOTALL | re.IGNORECASE)


def strip_system_blocks(text: str) -> str:
    """The listener's own words, with the harness's asides removed.

    Stripped rather than skipped: a real message often arrives with a system
    block stapled to it (a slash command's output, then what the person
    actually typed), and dropping the whole prompt would lose the sentence
    that matters. Nothing left over means nothing was said.
    """
    out = _SYSTEM_BANNER.sub(" ", _SYSTEM_BLOCKS.sub(" ", text or ""))
    # An unterminated block (the harness truncates long ones) leaves a bare
    # opening tag and everything after it; there is no listener text in that.
    out = re.split(r"<(?:task-notification|system-reminder|local-command-caveat)\b",
                   out, maxsplit=1)[0]
    return " ".join(out.split())


_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*")
# Bold spans may cross lines and contain single-* emphasis (`**a *b* c**`) —
# hence DOTALL + a lazy any-char body, not [^*]+ (which broke the pairing and
# left literal ** markers to be read aloud).
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", flags=re.DOTALL)
_ITAL_RE = re.compile(r"(?<!\*)\*([^*]+)\*")
_CODE_RE = re.compile(r"`([^`]+)`")
_HEAD_RE = re.compile(r"^#{1,6}\s+", flags=re.MULTILINE)
_BULLET_RE = re.compile(r"^\s*[-*]\s+", flags=re.MULTILINE)
_QUOTE_RE = re.compile(r"^\s*>\s?", flags=re.MULTILINE)
_BLANK_RE = re.compile(r"\n[ \t]*\n+")


_REGEX_FENCE_BLOCK = re.compile(r"(`{3,}|~{3,})([^\n]*)\n(.*?)\n[ \t]*\1", re.DOTALL)


# Markdown link / image: [text](url) or ![alt](url) -> keep only the human text.
_MD_LINK_RE = re.compile(r"!?\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+[^)]*)?\)")
# Autolink <https://...> -> spoken host.
_AUTOLINK_RE = re.compile(r"<(https?://[^>\s]+)>")
# Bare URL with an explicit scheme (safe: won't catch "e.g." / "example.com").
_BARE_URL_RE = re.compile(r"\bhttps?://[^\s)>\]`]+", re.IGNORECASE)


def _url_host(url: str) -> str:
    """Reduce a URL to a spoken 'host link' placeholder, dropping the path/query
    so TTS says "github.com link" instead of reading the whole thing."""
    host = re.sub(r"^https?://", "", url, flags=re.IGNORECASE)
    host = re.sub(r"^www\.", "", host, flags=re.IGNORECASE)
    host = host.split("/")[0].split("?")[0].split("#")[0].strip()
    host = host.rstrip(".,;:!?")
    return f"{host} link" if host else "link"


def suppress_urls(text: str) -> str:
    """Keep markdown link *text* but drop the URL, and reduce bare URLs to a
    spoken "<host> link" placeholder, so TTS doesn't read long URLs / query
    strings aloud."""
    if not text:
        return text
    out = _MD_LINK_RE.sub(lambda m: m.group(1).strip() or _url_host(m.group(2)), text)
    out = _AUTOLINK_RE.sub(lambda m: _url_host(m.group(1)), out)

    def _bare(m: "re.Match[str]") -> str:
        url = m.group(0)
        trail = ""
        while url and url[-1] in ".,;:!?":  # keep sentence punctuation outside the URL
            trail = url[-1] + trail
            url = url[:-1]
        return _url_host(url) + trail

    out = _BARE_URL_RE.sub(_bare, out)
    return out


def _int_env(name: str, default: int) -> int:
    try:
        return int((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


def _describe_code_or(body: str, placeholder: str, describe: bool) -> str:
    """When ``describe`` is set, replace an un-readable code block with a
    one-sentence spoken description via the (local) summary model; otherwise —
    or on any failure — return the mechanical placeholder. ``describe`` is passed
    explicitly (not read from the env here) so the caller controls *where* the
    model call happens: the hook enables it only in the detached playback child,
    never on the hot path. Lazy-imported so the default path stays network-free."""
    if not describe:
        return placeholder
    try:
        from ._summary import describe_code
        return describe_code(body) or placeholder
    except Exception:  # noqa: BLE001 — never let describe break the pipeline
        return placeholder


def _table_source(headers: "list[str]", rows: "list[list[str]]") -> str:
    parts = [" | ".join(headers)] if headers else []
    parts += [" | ".join(r) for r in rows]
    return "\n".join(parts)


def _describe_table_or(headers: "list[str]", rows: "list[list[str]]",
                       placeholder: str, describe: bool) -> str:
    if not describe:
        return placeholder
    try:
        from ._summary import describe_table
        return describe_table(_table_source(headers, rows)) or placeholder
    except Exception:  # noqa: BLE001
        return placeholder


def _code_reads_well(body: str) -> bool:
    """True if a code block is small and low-symbol enough to speak acceptably
    (e.g. a one-line shell command) instead of being replaced with a spoken
    placeholder. Tuned by MEDIA_SPEECH_CODE_MAX_LINES (default 2) and
    MEDIA_SPEECH_CODE_MAX_SYMBOL_RATIO (default 0.28); set max lines to 0 to
    always suppress."""
    text = (body or "").strip("\n")
    nonblank = [ln for ln in text.split("\n") if ln.strip()]
    if not nonblank or len(nonblank) > _int_env("MEDIA_SPEECH_CODE_MAX_LINES", 2):
        return False
    compact = text.strip()
    if not compact:
        return False
    symbols = sum(1 for c in compact if not (c.isalnum() or c.isspace() or c in "._,:-/'\""))
    return symbols / len(compact) <= _float_env("MEDIA_SPEECH_CODE_MAX_SYMBOL_RATIO", 0.28)


def _code_placeholder(n_lines: int, lang: str = "") -> str:
    n = max(1, n_lines)
    lang_word = f"{lang} " if lang else ""
    return f"{lang_word}code block, {n} line{'s' if n != 1 else ''}, omitted."


def _regex_suppress_fences(text: str, describe: bool = False) -> str:
    def repl(m: "re.Match[str]") -> str:
        lang = (m.group(2) or "").strip().split(" ")[0]
        body = m.group(3)
        if _code_reads_well(body):
            return body  # small & readable → keep the code, drop the fences
        n = body.count("\n") + 1
        return _describe_code_or(body, _code_placeholder(n, lang), describe)
    return _REGEX_FENCE_BLOCK.sub(repl, text)


def suppress_code_blocks(text: str, describe: bool = False) -> str:
    """Replace fenced / indented code blocks with a short *spoken* placeholder
    ("python code block, 12 lines, omitted.") so TTS describes code instead of
    reading it line by line. Uses markdown-it-py for robust block detection when
    available; falls back to a fenced-code regex. Must run on the FULL text
    (a block spans multiple sentences), so it's applied at the top of
    ``strip_markdown`` before sentence-level cleanup.
    """
    if not text or ("```" not in text and "~~~" not in text and "\n    " not in text):
        return text
    try:
        from markdown_it import MarkdownIt
        tokens = MarkdownIt("commonmark").parse(text)
    except Exception:  # noqa: BLE001 — any import/parse issue → regex fallback
        return _regex_suppress_fences(text, describe)

    spans: list[tuple[int, int, str]] = []
    for tok in tokens:
        rng = getattr(tok, "map", None)
        if not rng or tok.type not in ("fence", "code_block"):
            continue
        start, end = rng
        if _code_reads_well(tok.content or ""):
            continue  # small & readable → leave it in; fences stripped later
        if tok.type == "fence":
            lang = (tok.info or "").strip().split(" ")[0]
            placeholder = _code_placeholder(end - start - 2, lang)
        else:  # indented code_block
            placeholder = _code_placeholder(end - start)
        spans.append((start, end, _describe_code_or(tok.content or "", placeholder, describe)))
    if not spans:
        return text
    return _splice_spans(text, spans)


def _splice_spans(text: str, spans: "list[tuple[int, int, str]]") -> str:
    """Replace half-open ``[start, end)`` source-line ranges with their spoken
    placeholder. Shared by the code-block and table suppressors."""
    spans = sorted(spans)
    lines = text.split("\n")
    out_lines: list[str] = []
    i = 0
    si = 0
    while i < len(lines):
        if si < len(spans) and i == spans[si][0]:
            out_lines.append(spans[si][2])
            i = spans[si][1]
            si += 1
        else:
            out_lines.append(lines[i])
            i += 1
    return "\n".join(out_lines)


def _table_placeholder(headers: "list[str]", n_rows: int) -> str:
    n = max(1, n_rows)
    cols = ", ".join(h for h in headers if h)
    head = f"columns {cols}, " if cols else ""
    return f"table, {head}{n} row{'s' if n != 1 else ''}, omitted."


def _table_reads_well(rows: "list[list[str]]") -> bool:
    """True if a table is small enough (and its cells short enough) to speak as
    prose instead of a placeholder. Tuned by MEDIA_SPEECH_TABLE_MAX_ROWS
    (default 2) and MEDIA_SPEECH_TABLE_MAX_CELL (default 40); set max rows to 0
    to always suppress."""
    if not rows or len(rows) > _int_env("MEDIA_SPEECH_TABLE_MAX_ROWS", 2):
        return False
    max_cell = _int_env("MEDIA_SPEECH_TABLE_MAX_CELL", 40)
    return all(
        len(c) <= max_cell and "http://" not in c and "https://" not in c
        for row in rows for c in row
    )


def _table_to_prose(headers: "list[str]", rows: "list[list[str]]") -> str:
    """Render a small table as spoken prose: each row as "header: cell, ..."."""
    out: list[str] = []
    for row in rows:
        if headers and len(headers) == len(row):
            pairs = [f"{h}: {c}" for h, c in zip(headers, row) if c.strip()]
        else:
            pairs = [c for c in row if c.strip()]
        if pairs:
            out.append(", ".join(pairs))
    return ". ".join(out) + "." if out else ""


# Separator row of a GFM table, e.g. "| --- | :--: |". Must contain a dash.
_TABLE_SEP_RE = re.compile(
    r"^[ \t]*\|?[ \t]*:?-{1,}:?[ \t]*(?:\|[ \t]*:?-{1,}:?[ \t]*)*\|?[ \t]*$"
)


def _regex_suppress_tables(text: str, describe: bool = False) -> str:
    """Fallback table detector: a pipe-bearing header line immediately followed
    by a separator line, then consecutive pipe-bearing data lines."""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        if "|" in lines[i] and i + 1 < n and _TABLE_SEP_RE.match(lines[i + 1]):
            headers = [c.strip() for c in lines[i].strip().strip("|").split("|")]
            j = i + 2
            rows: list[list[str]] = []
            while j < n and lines[j].strip() and "|" in lines[j]:
                rows.append([c.strip() for c in lines[j].strip().strip("|").split("|")])
                j += 1
            if _table_reads_well(rows):
                out.append(_table_to_prose(headers, rows) or _table_placeholder(headers, len(rows)))
            else:
                out.append(_describe_table_or(headers, rows, _table_placeholder(headers, len(rows)), describe))
            i = j
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


def suppress_tables(text: str, describe: bool = False) -> str:
    """Replace markdown tables with a short spoken placeholder
    ("table, columns Name, Port, Notes, 2 rows, omitted.") so TTS describes the
    table instead of reading every cell and the ``|---|`` separator aloud. Runs
    on the FULL text (a table spans many lines), after code-block suppression so
    a fenced block containing ``|`` lines isn't mistaken for a table."""
    if not text or "|" not in text:
        return text
    try:
        from markdown_it import MarkdownIt
        md = MarkdownIt("commonmark")
        md.enable("table")
        tokens = md.parse(text)
    except Exception:  # noqa: BLE001 — any import/parse issue → regex fallback
        return _regex_suppress_tables(text, describe)

    lines = text.split("\n")
    spans: list[tuple[int, int, str]] = []
    n = len(tokens)
    i = 0
    while i < n:
        tok = tokens[i]
        if tok.type == "table_open" and getattr(tok, "map", None):
            start, end = tok.map
            headers: list[str] = []
            rows: list[list[str]] = []
            cur_row: list[str] | None = None
            section: str | None = None
            j = i + 1
            while j < n and tokens[j].type != "table_close":
                tj = tokens[j]
                if tj.type in ("thead_open", "tbody_open"):
                    section = tj.type[:5]  # "thead" / "tbody"
                elif tj.type in ("thead_close", "tbody_close"):
                    section = None
                elif tj.type == "tr_open" and section == "tbody":
                    # GFM absorbs a following non-blank prose line as a bare
                    # single-cell row; a real table row has a pipe in its source
                    # line, so only count/keep those.
                    rng = getattr(tj, "map", None)
                    cur_row = [] if (rng and "|" in lines[rng[0]]) else None
                elif tj.type == "tr_close" and section == "tbody":
                    if cur_row is not None:
                        rows.append(cur_row)
                    cur_row = None
                elif tj.type == "inline":
                    if section == "thead":
                        headers.append(tj.content.strip())
                    elif section == "tbody" and cur_row is not None:
                        cur_row.append(tj.content.strip())
                j += 1
            # Trim trailing non-pipe lines that GFM pulled into the table.
            while end > start + 1 and "|" not in lines[end - 1]:
                end -= 1
            if _table_reads_well(rows):
                repl = _table_to_prose(headers, rows) or _table_placeholder(headers, len(rows))
            else:
                repl = _describe_table_or(headers, rows, _table_placeholder(headers, len(rows)), describe)
            spans.append((start, end, repl))
            i = j
        i += 1

    if not spans:
        return text
    return _splice_spans(text, spans)


def strip_markdown(text: str, describe: bool = False) -> str:
    """Strip enough markdown that TTS doesn't read backticks / asterisks /
    fence markers aloud, and replace fenced code blocks with a spoken
    placeholder. Loose by design: callers can submit anything.

    ``describe=True`` turns the "…omitted." placeholders for un-readable code
    blocks / tables into one-sentence LLM descriptions. It makes network calls,
    so callers pass it ONLY off the hot path (the hook's detached playback
    child); the default keeps strip_markdown deterministic and network-free.
    """
    if not text:
        return ""
    out = suppress_code_blocks(text, describe)
    out = suppress_tables(out, describe)
    out = suppress_urls(out)
    out = _FENCE_RE.sub("", out)
    out = _HEAD_RE.sub("", out)
    out = _BOLD_RE.sub(r"\1", out)
    out = _ITAL_RE.sub(r"\1", out)
    out = _CODE_RE.sub(r"\1", out)
    out = _BULLET_RE.sub("", out)
    out = _QUOTE_RE.sub("", out)
    # Residual emphasis markers (an UNPAIRED ** / __ survives the pairing
    # passes above) are never worth hearing — drop them outright.
    out = out.replace("**", "").replace("__", "")
    out = _BLANK_RE.sub("\n", out).strip()
    return out


# Sentence-ending punctuation, optional closing quote/bracket, then whitespace.
# The trailing whitespace is what tells us the sentence is *complete* — we only
# split once the following character has arrived in the stream.
_SENT_BOUNDARY = re.compile(r'[.!?]+["\'”’)\]]*\s')

# Abbreviations / initials whose trailing period is NOT a sentence end. Matched
# against the text up to and including the boundary's first punctuation char.
_ABBREV_TAIL = re.compile(
    r'(?:\b(?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr|St|vs|etc|e\.g|i\.e|'
    r'Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)|\b[A-Za-z])\.$'
)

_WS_RE = re.compile(r'\s+')

# A line that opens/closes a fenced code block. The streaming sentencer must NOT
# split a sentence boundary that falls inside an open fence (or on a table row):
# the whole block has to reach strip_markdown intact to be suppressed as a unit —
# a half-a-fence fragment can't be matched.
_FENCE_LINE_RE = re.compile(r'(?m)^[ \t]*(?:`{3,}|~{3,})')


def _in_open_fence(prefix: str) -> bool:
    """True if `prefix` ends inside an unclosed ``` / ~~~ code fence."""
    return len(_FENCE_LINE_RE.findall(prefix)) % 2 == 1


def _on_table_row(buf: str, pos: int) -> bool:
    """True if the line containing `pos` looks like a markdown table row
    (starts with an optional-indent ``|``)."""
    line_start = buf.rfind("\n", 0, pos) + 1
    return buf[line_start:pos + 1].lstrip().startswith("|")


class IncrementalSentencer:
    """Segment a *streamed* text into complete sentences as they arrive.

    Used by the streaming pi intake: token deltas are `feed()` in as they
    arrive, and each call returns any sentences that have just completed (i.e.
    whose terminating punctuation is now followed by whitespace). Trailing
    partial text is buffered until more arrives. `close()` flushes whatever
    remains as a final sentence.

    Each emitted sentence is run through `strip_markdown` and whitespace-
    collapsed so it's ready to render. Abbreviations and single-letter
    initials ("Dr.", "e.g.", "J.") don't trigger a split.

    Fenced code blocks and tables are held together across the stream (a
    boundary inside an open fence or on a table row doesn't split), so the
    complete block reaches strip_markdown and is suppressed as a unit rather
    than spoken as a half-fence / raw pipes.
    """

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, chunk: str) -> list[str]:
        if not chunk:
            return []
        self._buf += chunk
        return self._drain()

    def close(self) -> list[str]:
        out = self._drain()
        tail = self._clean(self._buf)
        self._buf = ""
        if tail:
            out.append(tail)
        return out

    def _drain(self) -> list[str]:
        out: list[str] = []
        while True:
            split_end = -1
            for m in _SENT_BOUNDARY.finditer(self._buf):
                # Text up to and including the first punctuation char of the
                # boundary; skip if it ends in an abbreviation/initial.
                head = self._buf[: m.start() + 1]
                if _ABBREV_TAIL.search(head):
                    continue
                # Don't split inside a code fence or on a table row: let the
                # whole block accumulate so strip_markdown suppresses it as a
                # unit instead of speaking a half-fence / raw pipes. If the
                # block is still open (no qualifying boundary yet), we simply
                # buffer until it closes — or until close() flushes it.
                if (_in_open_fence(self._buf[: m.end()])
                        or _on_table_row(self._buf, m.start())):
                    continue
                split_end = m.end()  # includes the trailing whitespace
                break
            if split_end < 0:
                break
            raw, self._buf = self._buf[:split_end], self._buf[split_end:]
            cleaned = self._clean(raw)
            if cleaned:
                out.append(cleaned)
        return out

    @staticmethod
    def _clean(raw: str) -> str:
        return _WS_RE.sub(" ", strip_markdown(raw)).strip()
