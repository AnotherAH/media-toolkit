"""Caption parsing and transcript formatting.

Parsers turn whatever a site hands us (YouTube json3, WebVTT, SRT, TTML/srv3)
into a flat list of segments. Formatters turn segments into the shapes people
actually want: clean prose for pasting into a chatbot, SRT/VTT for video players,
timestamped Markdown for notes, JSON for scripts.
"""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, asdict


@dataclass
class Segment:
    start: float
    end: float
    text: str

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- parse

_TAG = re.compile(r"<[^>]+>")
_TS = re.compile(r"(\d{1,3}):(\d{2}):(\d{2})[.,](\d{1,3})")
_TS_SHORT = re.compile(r"^(\d{1,3}):(\d{2})[.,](\d{1,3})$")


def _clean(text: str) -> str:
    text = _TAG.sub("", text)
    text = html.unescape(text)
    return re.sub(r"[ \t ]+", " ", text).strip()


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
            if text.startswith(prev + " "):          # cue extends the previous one
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
        t = re.search(r'\bt="(\d+)"', attrs) or re.search(r'begin="([^"]+)"', attrs)
        d = re.search(r'\bd="(\d+)"', attrs)
        if t:
            raw_t = t.group(1)
            start = int(raw_t) / 1000 if raw_t.isdigit() else _secs(raw_t)
        else:
            start = 0.0
        dur = int(d.group(1)) / 1000 if d else 4.0
        out.append(Segment(start, start + dur, body))
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


def _merge_adjacent(segs: list[Segment]) -> list[Segment]:
    """Glue caption fragments that are really one sentence split across cues."""
    out: list[Segment] = []
    for seg in segs:
        if out and seg.start - out[-1].end < 0.06 and len(out[-1].text) < 90 \
                and not out[-1].text.endswith((".", "!", "?", ":")):
            out[-1] = Segment(out[-1].start, seg.end, f"{out[-1].text} {seg.text}".strip())
        else:
            out.append(seg)
    return out


# ---------------------------------------------------------------------- format

def hhmmss(t: float, comma: bool = False) -> str:
    t = max(t, 0.0)
    ms = int(round((t - int(t)) * 1000))
    s = int(t)
    sep = "," if comma else "."
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}{sep}{ms:03d}"


def short_ts(t: float) -> str:
    s = int(max(t, 0.0))
    if s >= 3600:
        return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"
    return f"{s // 60}:{s % 60:02d}"


def to_txt(segs: list[Segment], paragraphs: bool = True) -> str:
    """Clean prose. This is the format you paste into a chatbot."""
    if not paragraphs:
        return " ".join(s.text for s in segs).strip()
    out: list[str] = []
    buf: list[str] = []
    last_end = None
    for seg in segs:
        gap = seg.start - last_end if last_end is not None else 0.0
        joined = " ".join(buf)
        if buf and (gap > 2.0 or (len(joined) > 420 and joined.endswith((".", "!", "?")))):
            out.append(joined)
            buf = []
        buf.append(seg.text)
        last_end = seg.end
    if buf:
        out.append(" ".join(buf))
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
        body.append(f"**{link}** {' '.join(bucket)}")

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


def stats(segs: list[Segment]) -> dict:
    text = " ".join(s.text for s in segs)
    words = len(text.split())
    return {
        "segments": len(segs),
        "words": words,
        "characters": len(text),
        "est_tokens": int(words * 1.35),
        "duration": segs[-1].end if segs else 0.0,
    }


def chunk(text: str, size: int = 12000) -> list[str]:
    """Split a long transcript on paragraph/sentence limits so each piece fits
    inside one chatbot message."""
    if len(text) <= size:
        return [text]
    out: list[str] = []
    buf = ""
    for para in text.split("\n\n"):
        if len(buf) + len(para) + 2 > size and buf:
            out.append(buf.strip())
            buf = ""
        if len(para) > size:
            for sent in re.split(r"(?<=[.!?])\s+", para):
                if len(buf) + len(sent) + 1 > size and buf:
                    out.append(buf.strip())
                    buf = ""
                buf += sent + " "
        else:
            buf += para + "\n\n"
    if buf.strip():
        out.append(buf.strip())
    return out
