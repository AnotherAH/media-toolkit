"""Captions a site already has: which track to use, and fetching it.

The rule that matters: never hand back a translation when the user asked for
the video's own words. YouTube lists over a hundred machine translations among
its "automatic captions", including a plain 'en' for a Spanish video; picking
that by code alone returned English text labelled as the transcript, and those
translated requests are also the ones YouTube rate-limits first. A track counts
as a translation when its key does not end in '-orig' and its URL carries
'tlang='.

Ranking, with T the chosen language (or the video's own language L when none
is chosen):
  1. the uploader's captions in T
  2. the automatic '{T}-orig' track
  3. an automatic T track that is not a translation
  4. only when T is unknown: the single uploader track, or else the '-orig' one
  5. nothing, so Whisper transcribes the speech (in T when it is known)
With "Translate to English" the order is the uploader's English captions, the
automatic English original, then a machine translation to English, which is
allowed and flagged.

Every candidate is tried in turn, so a rate-limited or empty track falls
through to the next one instead of ending the caption path.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import subs

FORMAT_PRIORITY = ("json3", "srv3", "vtt", "ttml", "srv1", "srv2")
# Keys yt-dlp files under subtitles that are not captions at all.
NOT_CAPTIONS = {"live_chat", "rechat", "danmaku", "comments"}
_OLD_CODES = {"iw": "he", "in": "id", "ji": "yi", "jw": "jv", "mo": "ro"}

# English names for the languages Whisper knows plus a few caption-only ones,
# for plain-language step notes ("automatic Spanish").
LANGUAGE_NAMES = {
    "af": "Afrikaans", "am": "Amharic", "ar": "Arabic", "as": "Assamese",
    "az": "Azerbaijani", "ba": "Bashkir", "be": "Belarusian", "bg": "Bulgarian",
    "bn": "Bengali", "bo": "Tibetan", "br": "Breton", "bs": "Bosnian", "ca": "Catalan",
    "cs": "Czech", "cy": "Welsh", "da": "Danish", "de": "German", "el": "Greek",
    "en": "English", "es": "Spanish", "et": "Estonian", "eu": "Basque", "fa": "Persian",
    "fi": "Finnish", "fil": "Filipino", "fo": "Faroese", "fr": "French", "ga": "Irish",
    "gl": "Galician", "gu": "Gujarati", "ha": "Hausa", "haw": "Hawaiian", "he": "Hebrew",
    "hi": "Hindi", "hr": "Croatian", "ht": "Haitian Creole", "hu": "Hungarian",
    "hy": "Armenian", "id": "Indonesian", "is": "Icelandic", "it": "Italian",
    "ja": "Japanese", "jv": "Javanese", "ka": "Georgian", "kk": "Kazakh", "km": "Khmer",
    "kn": "Kannada", "ko": "Korean", "ku": "Kurdish", "ky": "Kyrgyz", "la": "Latin",
    "lb": "Luxembourgish", "ln": "Lingala", "lo": "Lao", "lt": "Lithuanian",
    "lv": "Latvian", "mg": "Malagasy", "mi": "Maori", "mk": "Macedonian",
    "ml": "Malayalam", "mn": "Mongolian", "mr": "Marathi", "ms": "Malay", "mt": "Maltese",
    "my": "Burmese", "ne": "Nepali", "nl": "Dutch", "nn": "Norwegian Nynorsk",
    "no": "Norwegian", "nb": "Norwegian", "oc": "Occitan", "pa": "Punjabi", "pl": "Polish",
    "ps": "Pashto", "pt": "Portuguese", "ro": "Romanian", "ru": "Russian",
    "sa": "Sanskrit", "sd": "Sindhi", "si": "Sinhala", "sk": "Slovak", "sl": "Slovenian",
    "sn": "Shona", "so": "Somali", "sq": "Albanian", "sr": "Serbian", "su": "Sundanese",
    "sv": "Swedish", "sw": "Swahili", "ta": "Tamil", "te": "Telugu", "tg": "Tajik",
    "th": "Thai", "tk": "Turkmen", "tl": "Tagalog", "tr": "Turkish", "tt": "Tatar",
    "uk": "Ukrainian", "ur": "Urdu", "uz": "Uzbek", "vi": "Vietnamese", "yi": "Yiddish",
    "yo": "Yoruba", "yue": "Cantonese", "zh": "Chinese", "zu": "Zulu",
}


def primary(code: str | None) -> str:
    """'pt-BR' -> 'pt', 'zh-Hans' -> 'zh', 'en-orig' -> 'en', 'iw' -> 'he'."""
    head = (code or "").strip().lower().replace("_", "-").split("-")[0]
    return _OLD_CODES.get(head, head)


def language_name(code: str | None) -> str:
    p = primary(code)
    return LANGUAGE_NAMES.get(p, "")


def _strip_orig(key: str) -> str:
    return key[:-5] if key.endswith("-orig") else key


def is_translation(key: str, tracks: list[dict]) -> bool:
    if key.endswith("-orig"):
        return False
    return any("tlang=" in str(t.get("url") or "") for t in tracks or [])


@dataclass
class Choice:
    key: str
    source: str               # official | auto | auto_translated
    tracks: list[dict] = field(repr=False)
    caption_lang: str         # language of the text, e.g. 'es' or 'pt-BR'
    source_lang: str          # language it came from (differs for translations)

    @property
    def label(self) -> str:
        return {"official": "the uploader's captions",
                "auto": "automatic captions",
                "auto_translated": "automatic translation"}[self.source]


def _pools(info: dict) -> tuple[dict, dict]:
    manual = {k: v for k, v in (info.get("subtitles") or {}).items()
              if v and k not in NOT_CAPTIONS}
    auto = {k: v for k, v in (info.get("automatic_captions") or {}).items()
            if v and k not in NOT_CAPTIONS}
    return manual, auto


def _matching(pool: dict, lang: str) -> list[str]:
    """Keys in pool for this language, the bare code first ('en' before 'en-GB')."""
    if not lang:
        return []
    hits = [k for k in pool if primary(k) == lang]
    return sorted(hits, key=lambda k: (k.lower() != lang, k))


def candidates(info: dict, language: str = "", translate: bool = False) -> list[Choice]:
    """Caption tracks worth trying, best first. Empty means use Whisper."""
    manual, auto = _pools(info)
    own = primary(info.get("language"))
    want = primary(language) or own
    out: list[Choice] = []
    seen: set[tuple[str, str]] = set()

    def add(pool: dict, key: str, source: str, caption_lang: str = "", source_lang: str = ""):
        tag = ("m" if pool is manual else "a", key)
        if tag in seen or not pool.get(key):
            return
        seen.add(tag)
        cap = caption_lang or _strip_orig(key)
        out.append(Choice(key, source, pool[key], cap, source_lang or cap))

    if not translate:
        for k in _matching(manual, want):
            add(manual, k, "official")
        for k in sorted(auto):
            if k.endswith("-orig") and primary(k) == want:
                add(auto, k, "auto")
        for k in _matching(auto, want):
            if not k.endswith("-orig") and not is_translation(k, auto[k]):
                add(auto, k, "auto")
        if not want:
            if len(manual) == 1:
                add(manual, next(iter(manual)), "official")
            else:
                for k in sorted(auto):
                    if k.endswith("-orig"):
                        add(auto, k, "auto")
                        break
        return out

    # Translate to English.
    for k in _matching(manual, "en"):
        add(manual, k, "official")
    for k in sorted(auto):
        if k.endswith("-orig") and primary(k) == "en":
            add(auto, k, "auto")
    translations = []
    for k in _matching(auto, "en"):
        if k.endswith("-orig"):
            continue
        if not is_translation(k, auto[k]):
            add(auto, k, "auto")
            continue
        # 'en-es' is YouTube's translation of the uploader's Spanish captions,
        # a better starting point than a translation of speech recognition.
        rest = k.split("-", 1)[1] if "-" in k else ""
        from_manual = bool(rest) and rest in manual
        translations.append((0 if from_manual else 1, k, rest if from_manual else own))
    for _, k, src in sorted(translations):
        add(auto, k, "auto_translated", caption_lang="en", source_lang=src)
    return out


def note_for(choice: Choice | None, language: str = "", info: dict | None = None,
             translate: bool = False) -> str:
    """The plain step note under 'Looking for captions'."""
    if choice is None:
        want = primary(language) or primary((info or {}).get("language"))
        if translate:
            return "none in English"
        name = language_name(want)
        return f"none in {name}" if name else "none"
    name = language_name(choice.caption_lang)
    if choice.source == "official":
        return f"{name}, from the uploader" if name else "from the uploader"
    if choice.source == "auto":
        return f"automatic {name}" if name else "automatic"
    src = language_name(choice.source_lang)
    return f"automatic translation from {src}" if src else "automatic translation"


class RateLimited(Exception):
    """The site answered 429 for a caption track."""


def fetch(ydl, choice: Choice) -> list[subs.Segment] | None:
    """Download and parse one caption track, trying its formats best first.

    A 429 on one format means every format of that track will get one too, so
    it moves straight on to the next candidate instead.
    """
    from yt_dlp.networking import Request
    from yt_dlp.networking.exceptions import HTTPError

    ranked = sorted(choice.tracks, key=lambda t: FORMAT_PRIORITY.index(t.get("ext", ""))
                    if t.get("ext") in FORMAT_PRIORITY else 99)
    for track in ranked:
        ext = track.get("ext", "")
        if track.get("data") is not None:
            raw = str(track["data"])
        elif track.get("url"):
            try:
                raw = _open(ydl, track, Request, HTTPError)
            except RateLimited:
                raise
            except Exception:
                continue
        else:
            continue
        segments = subs.parse_auto(raw, ext)
        if segments:
            return segments
    return None


def _open(ydl, track: dict, Request, HTTPError) -> str:
    extensions = {}
    if track.get("impersonate") is not None:
        try:
            target, _ = ydl._parse_impersonate_targets(track["impersonate"])
            if target is not None:
                extensions["impersonate"] = target
        except Exception:
            pass
    req = Request(track["url"], headers=track.get("http_headers") or {},
                  extensions=extensions or None)
    try:
        with ydl.urlopen(req) as resp:
            return resp.read().decode("utf-8", "replace")
    except HTTPError as exc:
        if getattr(exc, "status", None) == 429:
            raise RateLimited(str(exc)) from exc
        raise


def meta_from_info(info: dict, url: str = "") -> dict:
    """What the transcript files and the reader header need about the video."""
    duration = info.get("duration") or 0
    raw_date = info.get("upload_date") or ""
    date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}" if len(raw_date) == 8 else raw_date
    return {
        "id": info.get("id", ""),
        "title": info.get("title") or "Untitled",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": duration,
        "duration_string": info.get("duration_string") or _dur(duration),
        "thumbnail": info.get("thumbnail") or "",
        "url": info.get("webpage_url") or url,
        "upload_date": date,
        "language": info.get("language") or "",
        "extractor": info.get("extractor_key") or "",
    }


def _dur(seconds: float) -> str:
    s = int(seconds or 0)
    if not s:
        return ""
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"
