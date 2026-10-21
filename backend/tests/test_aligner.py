"""aligner 纯函数测试：歌词解析与逐句对齐（不触碰 whisper 模型）。"""
import pytest

from tinker.aligner import AlignError, Word, align_lyrics, parse_lyrics


def make_words(text: str, start: float = 0.0, step: float = 0.5) -> list[Word]:
    """把一段文本变成均匀分布的词级时间戳，模拟 whisper 输出。"""
    return [Word(w, round(start + i * step, 2), round(start + i * step + 0.4, 2))
            for i, w in enumerate(text.lower().split())]


LYRICS = "Twinkle, twinkle, little star\nHow I wonder what you are"


def test_parse_lyrics_drops_blank_lines_and_normalizes():
    parsed = parse_lyrics("Twinkle, twinkle!\n\n  \nHow's it")
    assert parsed == [("Twinkle, twinkle!", ["twinkle", "twinkle"]),
                      ("How's it", ["hows", "it"])]


def test_align_exact_match():
    words = make_words("twinkle twinkle little star how i wonder what you are")
    lines = align_lyrics(LYRICS, words)
    assert [l["index"] for l in lines] == [1, 2]
    assert lines[0]["text"] == "Twinkle, twinkle, little star"
    assert lines[0]["start"] == pytest.approx(0.0)
    # 行 1 余量 1.9+0.15 与行 2 余量 2.0-0.1 冲突，取中点 1.975 相接（输出保留两位小数）
    assert lines[0]["end"] == pytest.approx(1.97, abs=1e-6)
    assert lines[1]["start"] == pytest.approx(1.97, abs=1e-6)
    assert lines[1]["end"] == pytest.approx(4.9 + 0.15, abs=1e-6)


def test_align_tolerates_transcription_noise():
    # whisper 把 little 听成了 liddle、漏听了 i：其余词仍应匹配上
    words = make_words("twinkle twinkle liddle star how wonder what you are")
    lines = align_lyrics(LYRICS, words)
    assert lines[0]["start"] == pytest.approx(0.0)
    assert lines[1]["end"] == pytest.approx(4.4 + 0.15, abs=1e-6)


def test_align_interpolates_unmatched_line():
    lyrics = "la la la\nTwinkle twinkle little star\nHow I wonder what you are"
    words = make_words("twinkle twinkle little star how i wonder what you are", start=3.0)
    lines = align_lyrics(lyrics, words)
    # 第 1 行没匹配上：插在 [0, 第2行start) 之间
    assert lines[0]["start"] == 0.0
    assert lines[0]["end"] < lines[2]["start"]
    assert lines[1]["start"] > 2.9  # 3.0 - 0.1 余量，再与插值行中点相接
    assert lines[1]["end"] <= lines[2]["start"]  # 区间相接不重叠


def test_align_no_overlap_raises():
    words = make_words("completely different words here")
    with pytest.raises(AlignError):
        align_lyrics(LYRICS, words)


def test_align_empty_audio_raises():
    with pytest.raises(AlignError):
        align_lyrics(LYRICS, [])


def test_intervals_never_overlap_and_are_playable():
    lyrics = "\n".join(["one two", "three four", "five six", "seven eight"])
    words = make_words("one two three four five six seven eight")
    lines = align_lyrics(lyrics, words)
    for prev, cur in zip(lines, lines[1:]):
        assert prev["end"] <= cur["start"]
    for line in lines:
        assert line["end"] - line["start"] >= 0.3
