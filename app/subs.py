"""Caption parsing and transcript formatting.

Parsers turn whatever a site hands us (YouTube json3, WebVTT, SRT, TTML/srv3)
into a flat list of segments. Formatters turn segments into the shapes people
actually want: clean prose for pasting into a chatbot, SRT/VTT for video players,
timestamped Markdown for notes, JSON for scripts.

Transcripts are not only English. Chinese and Japanese put no spaces between
words, Persian and Arabic end questions with '؟', and plenty of auto-captions
have no punctuation at all, so joining, counting and splitting all have to work
without relying on ASCII spaces and full stops.
"""
from __future__ import annotations

import html
import json
import math
import re
import unicodedata
from dataclasses import dataclass, asdict


@dataclass
class Segment:
    start: float
    end: float
    text: str

    def to_dict(self) -> dict:
        return asdict(self)


# ------------------------------------------------------------------ scripts

def _is_cjk(ch: str) -> bool:
    """Han, kana and the CJK punctuation block: scripts written without spaces.

    Hangul is deliberately excluded: Korean separates words with spaces, so
    whitespace joining and word counting already work for it.
    """
    o = ord(ch)
    return (0x3000 <= o <= 0x30FF          # CJK punctuation, hiragana, katakana
            or 0x3400 <= o <= 0x4DBF       # CJK extension A
            or 0x4E00 <= o <= 0x9FFF       # CJK unified ideographs
            or 0xF900 <= o <= 0xFAFF       # compatibility ideographs
            or 0xFF00 <= o <= 0xFF60       # full-width forms (！？，)
            or 0x31F0 <= o <= 0x31FF       # katakana extensions
            or 0x20000 <= o <= 0x2FA1F)    # CJK extensions B and later


def _is_hangul(ch: str) -> bool:
    o = ord(ch)
    return 0xAC00 <= o <= 0xD7AF or 0x1100 <= o <= 0x11FF or 0x3130 <= o <= 0x318F


def _is_arabic(ch: str) -> bool:
    """Arabic script (Arabic, Persian, Urdu) and Hebrew: written right to left
    and split into many more tokens per word than Latin text."""
    o = ord(ch)
    return (0x0590 <= o <= 0x08FF or 0xFB1D <= o <= 0xFDFF or 0xFE70 <= o <= 0xFEFF)


def _join(a: str, b: str) -> str:
    """Join two caption fragments the way the language itself would.

    No space between two CJK characters. A '...' that ends one cue and a '...'
    that starts the next mark the same continuation, so both are dropped.
    """
    if not a:
        return b
    if not b:
        return a
    if a.endswith(("...", "…")) and b.startswith(("...", "…")):
        a = a.rstrip(".…").rstrip()
        b = b.lstrip(".…").lstrip()
        if not a or not b:
            return a or b
    if _is_cjk(a[-1]) and _is_cjk(b[0]):
        return a + b
    return f"{a} {b}"


def join_texts(parts) -> str:
    out = ""
    for p in parts:
        out = _join(out, p)
    return out


# --------------------------------------------------------------------------- parse

_TAG = re.compile(r"<[^>]+>")
_TS = re.compile(r"(\d{1,3}):(\d{2}):(\d{2})[.,](\d{1,3})")
_TS_SHORT = re.compile(r"^(\d{1,3}):(\d{2})[.,](\d{1,3})$")
# Two continuation marks in a row inside one cue ("going to... ...say").
_DOUBLE_DOTS = re.compile(r"(?:\.\.\.|…)\s+(?:\.\.\.|…)")


def _clean(text: str) -> str:
    """Strip markup and fold every run of whitespace, line breaks included.

    YouTube json3 cues carry a '\\n' wherever the on-screen caption wrapped; kept,
    it lands mid-sentence in the clean text that people paste into a chat.
    """
    text = _TAG.sub("", text)
    text = html.unescape(text)
    text = _DOUBLE_DOTS.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _secs(stamp: str) -> float:
    stamp = stamp.strip()
    if (m := _TS.match(stamp)):
        h, mnt, s, ms = m.groups()
        return int(h) * 3600 + int(mnt) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000
    if (m := _TS_SHORT.match(stamp)):
        mnt, s, ms = m.groups()
        return int(mnt) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000
    return 0.0


def parse_json3(raw: str) -> list[Segment]:
    """YouTube json3 captions. Already de-overlapped, so this is the best source."""
    data = json.loads(raw)
    out: list[Segment] = []
    for ev in data.get("events", []):
        segs = ev.get("segs")
        if not segs:
            continue
        text = _clean("".join(s.get("utf8", "") for s in segs))
        if not text:
            continue
        start = ev.get("tStartMs", 0) / 1000
        dur = ev.get("dDurationMs") or 0
        out.append(Segment(start, start + dur / 1000, text))
    return _merge_adjacent(out)


def parse_vtt(raw: str) -> list[Segment]:
    """WebVTT/SRT. Handles the rolling auto-captions YouTube serves, where every
    cue repeats the previous line. Naive parsing roughly triples the word count.
    """
    out: list[Segment] = []
    for block in re.split(r"\r?\n\r?\n+", raw.strip()):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if idx is None:
            continue
        left, _, right = lines[idx].partition("-->")
        tail = right.split()
        start, end = _secs(left), _secs(tail[0] if tail else "0")
        body = _clean(" ".join(lines[idx + 1:]))
        if not body or body.upper().startswith(("WEBVTT", "KIND:", "LANGUAGE:")):
            continue
        out.append(Segment(start, end, body))
    return _merge_adjacent(_dedupe_rolling(out))


def _dedupe_rolling(segs: list[Segment]) -> list[Segment]:
    """Drop text a previous cue already emitted (auto-caption scroll effect)."""
    out: list[Segment] = []
    for seg in segs:
        text = seg.text
        if out:
            prev = out[-1].text
            if text == prev:
                out[-1].end = max(out[-1].end, seg.end)
                continue
            # The cue extends the previous one (no space between CJK characters).
            if text.startswith(prev + " ") or (_is_cjk(prev[-1]) and text.startswith(prev)):
                out[-1] = Segment(out[-1].start, seg.end, text)
                continue
            # trim any leading overlap with the tail of the previous cue
            words, pwords = text.split(), prev.split()
            for n in range(min(len(words), len(pwords)), 0, -1):
                if pwords[-n:] == words[:n]:
                    text = " ".join(words[n:])
                    break
        if text:
            out.append(Segment(seg.start, seg.end, text))
    return out


def parse_ttml(raw: str) -> list[Segment]:
    """srv3 / TTML fallback."""
    out: list[Segment] = []
    for m in re.finditer(r"<p\b([^>]*)>(.*?)</p>", raw, re.S):
        attrs, body = m.group(1), m.group(2)
        body = _clean(re.sub(r"<br\s*/?>", " ", body))
        if not body:
            continue
        t = re.search(r'\bt="(\d+)"', attrs) or re.search(r'\bbegin="([^"]+)"', attrs)
        d = re.search(r'\bd="(\d+)"', attrs)
        e = re.search(r'\bend="([^"]+)"', attrs)
        if t:
            raw_t = t.group(1)
            start = int(raw_t) / 1000 if raw_t.isdigit() else _secs(raw_t)
        else:
            start = 0.0
        if d:
            end = start + int(d.group(1)) / 1000
        elif e and _secs(e.group(1)) > start:
            # TTML proper gives an end time; without it every cue ran four
            # seconds and overlapped the next ones in the SRT and VTT.
            end = _secs(e.group(1))
        else:
            end = start + 4.0
        out.append(Segment(start, end, body))
    return _merge_adjacent(out)


def parse_auto(raw: str, ext: str = "") -> list[Segment]:
    """Pick a parser from the extension, falling back to sniffing the content."""
    ext = (ext or "").lower().lstrip(".")
    try:
        if ext == "json3" or raw.lstrip().startswith("{"):
            return parse_json3(raw)
        if ext in ("srv3", "ttml", "xml") or raw.lstrip().startswith("<"):
            return parse_ttml(raw)
        return parse_vtt(raw)
    except Exception:
        return parse_vtt(raw)


# Where a sentence ends, in the scripts people actually transcribe: Latin,
# CJK full-width marks, the Arabic question mark, the Urdu full stop and the
# Devanagari danda.
_SENTENCE_END = (".", "!", "?", "…", "。", "！", "？", "؟", "۔", "।")


def _merge_adjacent(segs: list[Segment]) -> list[Segment]:
    """Glue caption fragments that are really one sentence split across cues."""
    out: list[Segment] = []
    for seg in segs:
        if out and seg.start - out[-1].end < 0.06 and len(out[-1].text) < 90 \
                and not out[-1].text.endswith(_SENTENCE_END + (":",)):
            out[-1] = Segment(out[-1].start, seg.end, _join(out[-1].text, seg.text))
        else:
            out.append(seg)
    return out


# ---------------------------------------------------------------------- format

def hhmmss(t: float, comma: bool = False) -> str:
    # Round once to whole milliseconds and carry, so 1.9996 s becomes 00:00:02.000
    # instead of the invalid 00:00:01.1000.
    total = int(round(max(t, 0.0) * 1000))
    s, ms = divmod(total, 1000)
    sep = "," if comma else "."
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}{sep}{ms:03d}"


def short_ts(t: float) -> str:
    s = int(max(t, 0.0))
    if s >= 3600:
        return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"
    return f"{s // 60}:{s % 60:02d}"


# Paragraph rules for the clean text. A pause only starts a paragraph once the
# current one has some substance; without that, every breath in a lecture
# became its own one-line paragraph.
PARA_MIN = 250          # a pause breaks a paragraph only past this length
PARA_SENTENCE = 420     # past this, break at the next sentence end
PARA_UNPUNCTUATED = 600  # no sentence end seen at all: break at the next cue
PARA_MAX = 1200         # hard ceiling, so chunking always has somewhere to cut
_SPEAKER = re.compile(r"^(>>|-\s)")


def to_txt(segs: list[Segment], paragraphs: bool = True) -> str:
    """Clean prose. This is the format you paste into a chatbot."""
    if not paragraphs:
        return join_texts(s.text for s in segs).strip()
    out: list[str] = []
    buf = ""
    punctuated = False
    last_end = None
    for seg in segs:
        gap = seg.start - last_end if last_end is not None else 0.0
        if buf and (
                _SPEAKER.match(seg.text)
                or (gap > 2.0 and len(buf) >= PARA_MIN)
                or (len(buf) >= PARA_SENTENCE and buf.endswith(_SENTENCE_END))
                or (len(buf) >= PARA_UNPUNCTUATED and not punctuated)
                or len(buf) >= PARA_MAX):
            out.append(buf)
            buf, punctuated = "", False
        buf = _join(buf, seg.text)
        punctuated = punctuated or any(c in _SENTENCE_END for c in seg.text)
        last_end = seg.end
    if buf:
        out.append(buf)
    return "\n\n".join(p.strip() for p in out if p.strip())


def to_srt(segs: list[Segment]) -> str:
    return "\n".join(
        f"{i}\n{hhmmss(s.start, True)} --> {hhmmss(max(s.end, s.start + 0.1), True)}\n{s.text}\n"
        for i, s in enumerate(segs, 1)
    )


def to_vtt(segs: list[Segment]) -> str:
    body = "\n".join(
        f"{hhmmss(s.start)} --> {hhmmss(max(s.end, s.start + 0.1))}\n{s.text}\n" for s in segs
    )
    return "WEBVTT\n\n" + body


def to_markdown(segs: list[Segment], meta: dict | None = None, block: float = 30.0) -> str:
    """Timestamped Markdown. YouTube timestamps become clickable deep links."""
    meta = meta or {}
    url = meta.get("url", "")
    head: list[str] = []
    if meta.get("title"):
        head.append(f"# {meta['title']}\n")
    for label, key in (("Channel", "uploader"), ("Duration", "duration_string"),
                       ("Published", "upload_date"), ("Source", "url")):
        if meta.get(key):
            head.append(f"**{label}:** {meta[key]}")
    if head:
        head.append("\n---\n")

    yt = "youtube.com" in url or "youtu.be" in url
    body: list[str] = []
    bucket: list[str] = []
    bstart = None

    def flush():
        if not bucket or bstart is None:
            return
        stamp = short_ts(bstart)
        if yt and url:
            joiner = "&" if "?" in url else "?"
            link = f"[{stamp}]({url}{joiner}t={int(bstart)}s)"
        else:
            link = f"`{stamp}`"
        body.append(f"**{link}** {join_texts(bucket)}")

    for seg in segs:
        if bstart is None:
            bstart = seg.start
        if seg.start - bstart >= block and bucket:
            flush()
            bucket, bstart = [], seg.start
        bucket.append(seg.text)
    flush()
    return "\n".join(head) + "\n\n".join(body) + "\n"


def to_json(segs: list[Segment], meta: dict | None = None) -> str:
    return json.dumps({"meta": meta or {}, "segments": [s.to_dict() for s in segs]},
                      indent=2, ensure_ascii=False)


FORMATTERS = {
    "txt": lambda s, m: to_txt(s),
    "flat": lambda s, m: to_txt(s, paragraphs=False),
    "srt": lambda s, m: to_srt(s),
    "vtt": lambda s, m: to_vtt(s),
    "md": to_markdown,
    "json": to_json,
}

EXTENSIONS = {"txt": "txt", "flat": "txt", "srt": "srt", "vtt": "vtt", "md": "md", "json": "json"}


def render(segs: list[Segment], fmt: str, meta: dict | None = None) -> str:
    return FORMATTERS.get(fmt, FORMATTERS["txt"])(segs, meta or {})


# ----------------------------------------------------------------------- stats

def stats(segs: list[Segment]) -> dict:
    """Word and token estimates that hold up outside English.

    Whitespace splitting counts a whole Chinese or Japanese cue as one word, so
    each Han or kana character counts as a word here (the convention word
    processors use), and roughly one token. Arabic-script words (Persian,
    Arabic, Urdu) cost about two and a half tokens each in common chat
    tokenizers, other non-Latin words about two, Latin words about 1.35.
    """
    text = join_texts(s.text for s in segs)
    cjk = hangul = arabic = latin_letters = other_letters = 0
    words = 0
    latin_words = other_words = arabic_words = 0
    for token in text.split():
        t_cjk = sum(1 for c in token if _is_cjk(c) and c.isalnum())
        cjk += t_cjk
        words += t_cjk
        rest = [c for c in token if not _is_cjk(c)]
        if not any(c.isalnum() for c in rest):
            continue                  # punctuation such as '>>' or '-' is not a word
        words += 1
        letters = [c for c in rest if c.isalpha()]
        if not letters:
            latin_words += 1          # numbers tokenize like Latin words
            continue
        if any(_is_hangul(c) for c in letters):
            hangul += sum(1 for c in letters if _is_hangul(c))
            other_words += 1
        elif any(_is_arabic(c) for c in letters):
            arabic += len(letters)
            arabic_words += 1
        elif all(ord(c) < 0x250 for c in letters):
            latin_letters += len(letters)
            latin_words += 1
        else:
            other_letters += len(letters)
            other_words += 1

    tokens = (latin_words * 1.35
              + cjk * 1.0
              + hangul * 1.0
              + max(arabic_words * 2.5, arabic / 2.5)
              + max(other_words * 2.0, other_letters / 2.5))
    counts = {"cjk": cjk, "arabic": arabic, "latin": latin_letters,
              "other": other_letters + hangul}
    script = max(counts, key=counts.get) if any(counts.values()) else "latin"
    estimate = int(math.ceil(tokens))
    return {
        "segments": len(segs),
        "words": words,
        "characters": len(text),
        "est_tokens": estimate,
        "tokens": estimate,              # the same estimate, under the shorter name
        "duration": segs[-1].end if segs else 0.0,
        # "cjk": show a character count instead of words for these scripts.
        "script": script,
    }


# ----------------------------------------------------------------------- chunk

# Split levels, coarsest first. Each pattern captures the separator so nothing
# is lost when pieces are glued back together.
_LEVELS = (
    re.compile(r"(\n[ \t]*\n\s*)"),                          # paragraphs
    re.compile(r"(\n)"),                                     # lines
    # Sentence ends: Latin punctuation needs following whitespace; CJK, Arabic,
    # Urdu and Devanagari full stops do not.
    re.compile(r"((?<=[.!?…])\s+|(?<=[。！？؟۔।])\s*)"),
    re.compile(r"(\s+)"),                                    # words
)


def _split_keep(text: str, pattern: re.Pattern) -> list[str]:
    parts = pattern.split(text)
    out: list[str] = []
    for i in range(0, len(parts), 2):
        piece = parts[i] + (parts[i + 1] if i + 1 < len(parts) else "")
        if piece:
            out.append(piece)
    return out


def _hard_split(text: str, size: int) -> list[str]:
    """Last resort for a run with no break at all: cut at a character boundary,
    never between a letter and the accent that combines with it."""
    out: list[str] = []
    while len(text) > size:
        cut = size
        while cut > 1 and unicodedata.combining(text[cut]):
            cut -= 1
        out.append(text[:cut])
        text = text[cut:]
    if text:
        out.append(text)
    return out


def _units(text: str, size: int, level: int = 0) -> list[str]:
    if len(text) <= size:
        return [text]
    if level >= len(_LEVELS):
        return _hard_split(text, size)
    parts = _split_keep(text, _LEVELS[level])
    if len(parts) <= 1:
        return _units(text, size, level + 1)
    out: list[str] = []
    for part in parts:
        out.extend(_units(part, size, level + 1) if len(part) > size else [part])
    return out


def chunk(text: str, size: int = 12000) -> list[str]:
    """Split a long transcript so each piece fits inside one chatbot message.

    Prefers paragraph, then line, then sentence, then word boundaries, and only
    cuts inside a word when a single run has no break at all. No piece is ever
    longer than size, whatever the language or punctuation.
    """
    size = max(int(size or 0), 1)
    if len(text) <= size:
        return [text]
    out: list[str] = []
    buf = ""
    for unit in _units(text, size):
        if buf and len(buf) + len(unit) > size:
            if buf.strip():
                out.append(buf.strip())
            buf = ""
        buf += unit
    if buf.strip():
        out.append(buf.strip())
    return out
