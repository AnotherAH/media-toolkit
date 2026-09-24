"""Caption parsers, transcript formats, statistics and chunking."""
import json

import pytest

from app import subs
from app.subs import Segment as S


# ------------------------------------------------------------------ parsers

def test_json3_line_wraps_become_spaces():
    raw = json.dumps({"events": [
        {"tStartMs": 0, "dDurationMs": 2000,
         "segs": [{"utf8": "All right, so here we are, in front of the\nelephants"}]},
        {"tStartMs": 2000, "dDurationMs": 2000, "segs": [{"utf8": "the cool thing."}]},
    ]})
    segs = subs.parse_json3(raw)
    assert all("\n" not in s.text for s in segs)
    assert "front of the elephants" in subs.to_txt(segs)


def test_vtt_rolling_captions_are_deduplicated():
    raw = ("WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nhello there\n\n"
           "00:00:02.000 --> 00:00:04.000\nhello there\ngeneral kenobi\n\n"
           "00:00:04.000 --> 00:00:06.000\ngeneral kenobi\nyou are bold\n")
    text = subs.to_txt(subs.parse_vtt(raw))
    assert text.split() == "hello there general kenobi you are bold".split()


def test_ttml_parses_times_and_breaks():
    raw = '<tt><body><p t="1000" d="1500">one<br/>two</p><p t="3000" d="1000">three</p></body></tt>'
    segs = subs.parse_ttml(raw)
    assert segs[0].start == 1.0 and "one two" in segs[0].text
    assert subs.parse_auto(raw, "srv3")[0].text.startswith("one two")


def test_ttml_end_times_are_used():
    raw = ('<tt><body><p begin="00:00:01.000" end="00:00:02.500">one</p>'
           '<p begin="00:00:05.000" end="00:00:06.000">two.</p></body></tt>')
    segs = subs.parse_ttml(raw)
    assert (segs[0].start, segs[0].end) == (1.0, 2.5)
    assert (segs[1].start, segs[1].end) == (5.0, 6.0)


def test_cjk_rolling_captions_are_deduplicated():
    raw = ("WEBVTT\n\n00:00:00.000 --> 00:00:02.000\n今天我们\n\n"
           "00:00:02.000 --> 00:00:04.000\n今天我们来讨论\n")
    segs = subs.parse_vtt(raw)
    assert [s.text for s in segs] == ["今天我们来讨论"]


def test_clean_collapses_doubled_continuation_dots():
    assert subs._clean("going to... ...say it") == "going to say it"
    assert subs._clean("a … … b") == "a b"
    assert subs._clean("<i>x</i>&amp;\ty") == "x& y"


def test_join_drops_continuation_dots_across_cues():
    assert subs.join_texts(["I was going to...", "...say this"]) == "I was going to say this"


def test_cjk_is_joined_without_spaces():
    assert subs.join_texts(["今天我们", "来讨论。"]) == "今天我们来讨论。"
    assert subs.join_texts(["hello", "世界"]) == "hello 世界"


# ------------------------------------------------------------------ to_txt

def _para_lengths(text):
    return [len(p) for p in text.split("\n\n")]


def test_pause_breaks_only_substantial_paragraphs():
    segs = [S(i * 4.0, i * 4.0 + 1.0, "Short sentence here.") for i in range(40)]
    lengths = _para_lengths(subs.to_txt(segs))
    assert all(n >= 250 for n in lengths[:-1])


def test_speaker_markers_always_break():
    segs = [S(0, 1, "Hello there."), S(1, 2, ">> Who is it?"), S(2, 3, "- Me.")]
    paras = subs.to_txt(segs).split("\n\n")
    assert paras == ["Hello there.", ">> Who is it?", "- Me."]


def test_unpunctuated_speech_still_gets_paragraphs():
    segs = [S(i * 2.0, i * 2.0 + 1.9, "this is some words without any punctuation at all here")
            for i in range(600)]
    lengths = _para_lengths(subs.to_txt(segs))
    assert len(lengths) > 20
    assert max(lengths) <= subs.PARA_MAX + 80


def test_flat_has_no_breaks():
    segs = [S(0, 1, "one."), S(5, 6, "two.")]
    assert subs.render(segs, "flat") == "one. two."


# ------------------------------------------------------------------ chunk

LONG_TEXTS = {
    "unpunctuated": lambda: " ".join(["word"] * 20000),
    "cjk": lambda: "今天我们来讨论一下这个问题的解决方案。" * 2000,
    "persian": lambda: "آیا این یک جمله فارسی است؟ " * 1500,
    "no_breaks": lambda: "x" * 30000,
    "lines": lambda: "Sentence one. Sentence two!\n" * 3000,
}


@pytest.mark.parametrize("kind", list(LONG_TEXTS))
def test_chunk_never_exceeds_size_and_loses_nothing(kind):
    text = LONG_TEXTS[kind]()
    parts = subs.chunk(text, 12000)
    assert len(parts) >= 2
    assert all(len(p) <= 12000 for p in parts)
    assert "".join(parts).replace(" ", "").replace("\n", "") == \
        text.replace(" ", "").replace("\n", "")


def test_chunk_prefers_sentence_ends_for_cjk():
    text = "今天我们来讨论一下这个问题的解决方案。" * 2000
    parts = subs.chunk(text, 12000)
    assert all(p.endswith("。") for p in parts)


def test_chunk_short_text_is_one_piece():
    assert subs.chunk("short", 12000) == ["short"]


def test_chunk_of_rendered_unpunctuated_transcript_splits():
    segs = [S(i * 2.0, i * 2.0 + 1.9, "this is some words without any punctuation at all here")
            for i in range(600)]
    txt = subs.render(segs, "txt")
    assert len(subs.chunk(txt, 12000)) >= 3


def test_chunk_does_not_split_combining_marks():
    text = ("é" * 7000)            # 'é' as e + combining accent, no breaks
    for part in subs.chunk(text, 1001):
        assert not part[0] == "́"


# ------------------------------------------------------------------ timestamps

@pytest.mark.parametrize("t,comma,want", [
    (1.9996, False, "00:00:02.000"),
    (0.7 + 0.2 + 0.1, False, "00:00:01.000"),
    (3599.9999, True, "01:00:00,000"),
    (61.5, True, "00:01:01,500"),
    (-3, False, "00:00:00.000"),
])
def test_hhmmss_carries_milliseconds(t, comma, want):
    assert subs.hhmmss(t, comma) == want


def test_srt_and_vtt_shapes():
    segs = [S(0, 1.5, "one"), S(1.5, 3, "two")]
    srt = subs.render(segs, "srt")
    assert srt.startswith("1\n00:00:00,000 --> 00:00:01,500\none\n")
    vtt = subs.render(segs, "vtt")
    assert vtt.startswith("WEBVTT\n\n00:00:00.000 --> 00:00:01.500\none\n")


def test_markdown_links_youtube_times():
    segs = [S(0, 1, "a"), S(40, 41, "b")]
    md = subs.render(segs, "md", {"title": "T", "url": "https://www.youtube.com/watch?v=x"})
    assert "[0:40](https://www.youtube.com/watch?v=x&t=40s)" in md


# ------------------------------------------------------------------ stats

def test_stats_latin():
    st = subs.stats([S(0, 1, "Hello there my friend."), S(1, 2, ">> Yes -")])
    assert st["words"] == 5
    assert st["script"] == "latin"
    assert 5 <= st["est_tokens"] <= 10


def test_stats_counts_cjk_characters_as_words():
    segs = [S(i, i + 1, "今日は良い天気です。") for i in range(2000)]
    st = subs.stats(segs)
    assert st["script"] == "cjk"
    assert st["words"] == 9 * 2000
    assert st["est_tokens"] >= 9 * 2000


def test_stats_persian_tokens_are_not_undercounted():
    segs = [S(i, i + 1, "آیا این یک جمله فارسی است؟") for i in range(1200)]
    st = subs.stats(segs)
    assert st["script"] == "arabic"
    assert st["words"] == 6 * 1200
    # About two and a half tokens per Persian word in common chat tokenizers.
    assert st["est_tokens"] >= 2.5 * st["words"]


def test_stats_empty():
    st = subs.stats([])
    assert st["words"] == 0 and st["est_tokens"] == 0 and st["duration"] == 0.0
    assert subs.stats([S(0, 1, "one two")])["tokens"] == subs.stats([S(0, 1, "one two")])["est_tokens"]
