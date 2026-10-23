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
    assert parsed == [("Twinkle, twinkle!", "", ["twinkle", "twinkle"]),
                      ("How's it", "", ["hows", "it"])]


def test_parse_bilingual_attaches_zh_to_previous_en_line():
    parsed = parse_lyrics(
        "Twinkle, twinkle, little star\n"
        "一闪一闪小星星\n"
        "How I wonder what you are\n"
        "我多想知道你是什么\n"
        "Up above the world so high"  # 没贴翻译：留空
    )
    assert parsed[0][:2] == ("Twinkle, twinkle, little star", "一闪一闪小星星")
    assert parsed[1][:2] == ("How I wonder what you are", "我多想知道你是什么")
    assert parsed[2][:2] == ("Up above the world so high", "")


def test_parse_bilingual_orphan_and_consecutive_zh():
    # 开头孤立的中文行没有归属，忽略；连续中文行拼进同一句
    parsed = parse_lyrics("主题曲\nTwinkle twinkle\n小星星\n亮晶晶")
    assert parsed == [("Twinkle twinkle", "小星星 亮晶晶", ["twinkle", "twinkle"])]


def test_align_carries_zh_into_lines():
    words = make_words("twinkle twinkle little star how i wonder what you are")
    lines = align_lyrics("Twinkle, twinkle, little star\n一闪一闪小星星\nHow I wonder what you are", words)
    assert lines[0]["zh"] == "一闪一闪小星星"
    assert lines[1]["zh"] == ""


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
    words = make_words("twinkle twinkle little star how i wonder what you are", start=6.0)
    lines = align_lyrics(lyrics, words)
    # 第 1 行没匹配上：从第 2 行起点向前倒推一个典型句长（匹配行时长中位数 2.9s）。
    # 前奏 6s 比典型句长长，第 1 行不得从 0 摊满整个前奏
    assert lines[0]["start"] == pytest.approx(6.0 - 2.9 - 0.1, abs=1e-6)
    assert lines[0]["end"] <= lines[1]["start"]  # 终点贴着第 2 行
    assert lines[1]["start"] > 5.9  # 6.0 - 0.1 余量，再与插值行中点相接
    assert lines[1]["end"] <= lines[2]["start"]  # 区间相接不重叠


def test_align_leading_gap_does_not_swallow_intro():
    # 回归：20s 前奏里 whisper 一个歌词都没听到（或只听到幻觉词）时，
    # 头部未匹配行不得从 0 均摊——前奏不是歌词（2026-10-21 Young for You 事故）
    lyrics = "hmm hmm hmm\none two\nthree four"
    words = make_words("one two three four", start=20.0)
    lines = align_lyrics(lyrics, words)
    # 匹配行典型句长约 0.9s：第 1 行应倒推为 [19.0-0.1, 20.0] 附近，而不是 [0, 20]
    assert lines[0]["end"] <= lines[1]["start"]
    assert lines[0]["start"] > 18.0
    assert lines[0]["end"] - lines[0]["start"] < 2.0


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


def test_unclaimed_word_goes_to_line_missing_head():
    """歌词首词被转写变形（Forecaster→forecasters）匹配失败时，这个词应归
    下句而不是留在两句之间的无人区——否则换气口切边会把它劈给上句，
    用户听到"下句的词出现在上句"（2026-10-23 Young for You 行2|3 事故）。"""
    # star[1.0-1.4] forecasters[1.5-1.9] said[2.0-2.4] the[2.5-2.9] sun[3.0-3.4]
    words = make_words("to your star forecasters said the sun", start=0.0)
    lines = align_lyrics("To your star\nForecaster said the sun", words)
    assert lines[0]["end"] == lines[1]["start"]
    # 边界钳在 star 与 forecasters 的词缝 [1.4, 1.5] 内，第 2 句从 forecaster 起
    assert 1.4 <= lines[0]["end"] <= 1.5


def test_unclaimed_words_split_when_both_sides_missing():
    """上句缺尾词、下句缺首词时，中间的无人认领词按比例分（1:1 各得一个）。"""
    # one[0-.4] two[.5-.9] wa[1.0-1.4] wb[1.5-1.9] five[2.0-2.4] six[2.5-2.9]
    words = make_words("one two wa wb five six")
    lines = align_lyrics("one two three\nfour five six", words)
    # 边界落在 wa 与 wb 的词缝 [1.4, 1.5]：wa 归上句、wb 归下句
    assert lines[0]["end"] == lines[1]["start"]
    assert 1.4 <= lines[0]["end"] <= 1.5


def test_unclaimed_word_in_interlude_not_assigned():
    """锚词间隔 >3s 是间奏：即使下句缺首词，无人认领的词也不参与分配，
    两句各自留余量、谁都不占间奏。"""
    words = [Word("one", 1.0, 1.4), Word("two", 1.5, 1.9),
             Word("humming", 3.0, 3.4), Word("three", 5.0, 5.4),
             Word("four", 12.5, 12.9)]
    lines = align_lyrics("one two\ntres four", words)
    assert lines[0]["end"] == pytest.approx(2.05, abs=0.01)   # two.end + 0.15
    assert lines[1]["start"] == pytest.approx(12.4, abs=0.01)  # four.start - 0.1


def test_boundary_moves_to_energy_valley():
    """切句边界应落在两句间换气的能量低谷，而不是词锚点的中点。

    合成音频：第 1 句的尾音拖到 2.6s（whisper 词 end 只到 1.9s），
    2.6~2.9s 是换气静音，之后第 2 句起音。低谷切分让上句保住完整尾音；
    中点切分（约 2.48s）会从尾音中间劈开。
    """
    import numpy as np

    from tinker.audio import SAMPLE_RATE

    rng = np.random.default_rng(0)
    audio = rng.standard_normal(6 * SAMPLE_RATE).astype(np.float32)
    envelope = np.full(len(audio), 0.01, dtype=np.float32)  # 底噪
    for t0, t1 in [(0.8, 2.6), (2.9, 4.4)]:
        envelope[int(t0 * SAMPLE_RATE):int(t1 * SAMPLE_RATE)] = 0.5
    audio *= envelope

    lyrics = "one two\nthree four"
    words = [Word("one", 1.0, 1.4), Word("two", 1.5, 1.9),
             Word("three", 3.0, 3.4), Word("four", 3.5, 3.9)]

    with_audio = align_lyrics(lyrics, words, audio)
    without_audio = align_lyrics(lyrics, words)

    # 无音频兜底：词 end 加 0.15s 余量就到 2.05 为止，尾音（至 2.6s）被切掉
    assert without_audio[0]["end"] == pytest.approx(2.05, abs=0.01)
    # 有音频：边界落在换气低谷（2.6~2.9），上句尾音完整、下句从安静处开始
    assert with_audio[0]["end"] == with_audio[1]["start"]
    assert 2.55 < with_audio[0]["end"] < 2.95


def test_interlude_is_not_absorbed_by_either_line():
    """两句词锚点间隔 >3s 是间奏而非换气：不共享边界，谁都不占间奏，
    否则单句循环会把整段间奏播出来。"""
    import numpy as np

    from tinker.audio import SAMPLE_RATE

    rng = np.random.default_rng(0)
    audio = (rng.standard_normal(20 * SAMPLE_RATE) * 0.01).astype(np.float32)

    lyrics = "one two\nthree four"
    words = [Word("one", 1.0, 1.4), Word("two", 1.5, 1.9),
             Word("three", 12.0, 12.4), Word("four", 12.5, 12.9)]

    lines = align_lyrics(lyrics, words, audio)
    # 各自留余量：第 1 句在词尾 1.9+0.15 收，第 2 句从词头 12.0-0.1 起
    assert lines[0]["end"] == pytest.approx(2.05, abs=0.01)
    assert lines[1]["start"] == pytest.approx(11.9, abs=0.01)
