"""歌词逐句对齐：faster-whisper 词级时间戳 + 歌词文本匹配。

模型是进程内单例、懒加载——加载要几秒且占内存，不能每次对齐都新建。

对齐策略：把歌词各行与 whisper 转写结果都拆成规范化词序列（小写、仅字母
数字），用 difflib.SequenceMatcher 做整体匹配；每行取首个/末个匹配词的
时间戳作为该句时间区间。没匹配上的行（转写漏词、纯哼唱等）按相邻已匹配
行的时间线性插值，保证每一句都可点播。

切边前先做词归属：相邻两句锚词之间常夹着"无人认领"的转写词（歌词首/尾词
被转写变形导致匹配失败，如 Forecaster→forecasters），按两侧歌词缺失词数
把它们分给上句或下句，换气口切边才不会从这些词中间劈开（上句带出下句的
词）。
"""
from __future__ import annotations

import difflib
import re
import threading
from dataclasses import dataclass

import numpy as np

from .audio import SAMPLE_RATE

# small.en 对口音重、伴奏吵的演唱会产生大面积幻觉（把前奏听成
# "thank you so much for watching"），实测 medium.en 才能稳定转写真实人声；
# CPU int8 下一首 3 分半的歌约 2~4 分钟，可接受。
MODEL_NAME = "medium.en"

_model = None
_model_lock = threading.Lock()


class AlignError(Exception):
    """对齐失败（音频里听不出人声等），message 面向用户。"""


@dataclass
class Word:
    text: str  # 规范化后的词
    start: float
    end: float


def _norm(text: str) -> str:
    """小写并去掉一切非字母数字，让 Twinkle,/twinkle 与 don't/dont 都能对上。"""
    return re.sub(r"[^a-z0-9]+", "", text.lower())


# 汉字（含扩展 A 区）：判定"这一行是中文翻译"用
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


def _get_model():
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel

            _model = WhisperModel(MODEL_NAME, device="cpu", compute_type="int8")
    return _model


def transcribe_words(audio: np.ndarray) -> list[Word]:
    """转写 16k 单声道音频，返回规范化后的词级时间戳序列。"""
    model = _get_model()
    segments, _ = model.transcribe(
        audio,
        language="en",
        word_timestamps=True,
        # 反幻觉：不带入上一段上下文（幻觉句会自我复制扩散），
        # 超过 2s 的静音段重新开始（前奏/间奏里不再脑补歌词）。
        # 注意不能用 vad_filter——它会把带伴奏的歌声误判为非语音而整段丢弃。
        condition_on_previous_text=False,
        hallucination_silence_threshold=2.0,
    )
    words = []
    for seg in segments:
        for w in seg.words or []:
            norm = _norm(w.word)
            if norm:
                words.append(Word(norm, round(w.start, 2), round(w.end, 2)))
    return words


def parse_lyrics(lyrics: str) -> list[tuple[str, str, list[str]]]:
    """歌词文本 -> [(英文行, 中文翻译, 规范化词序列)]，丢弃空行。

    中英混贴（网易云等平台复制歌词的格式）：含汉字的行是它上面那句英文
    的翻译；连续多行中文拼进同一句；开头孤立的中文行没有归属，忽略。
    """
    parsed: list[tuple[str, str, list[str]]] = []
    for raw in lyrics.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        if _CJK.search(raw):
            if parsed:
                en, zh, tokens = parsed[-1]
                parsed[-1] = (en, f"{zh} {raw}".strip(), tokens)
            continue
        tokens = [t for t in (_norm(w) for w in raw.split()) if t]
        if tokens:
            parsed.append((raw, "", tokens))
    return parsed


def align_lyrics(lyrics: str, words: list[Word], audio: np.ndarray | None = None) -> list[dict]:
    """产出逐句时间区间 [{index, text, zh, start, end}]，按行号顺序、区间互不重叠。

    audio（16k 单声道）用于把相邻句的边界移到换气口的能量最低点；
    不传则退化为纯文本匹配 + 中点切分（测试与离线推演用）。
    """
    parsed = parse_lyrics(lyrics)
    if not parsed:
        raise AlignError("歌词是空的")
    if not words:
        raise AlignError("没有在录音里听到歌声，换一段有歌声的录音试试")

    lyric_tokens: list[str] = []
    token_line: list[int] = []  # 每个歌词词属于第几行
    for i, (_, _, tokens) in enumerate(parsed):
        lyric_tokens.extend(tokens)
        token_line.extend([i] * len(tokens))

    # 整体匹配：matching_blocks 在两个序列上都是单调的，所以区间天然有序
    matcher = difflib.SequenceMatcher(None, lyric_tokens, [w.text for w in words], autojunk=False)
    matched: list[list[int]] = [[] for _ in parsed]      # 每行匹配到的 whisper 词下标
    matched_tok: list[list[int]] = [[] for _ in parsed]  # 每行匹配到的歌词词下标（全局序号）
    for block in matcher.get_matching_blocks():
        for k in range(block.size):
            line = token_line[block.a + k]
            matched[line].append(block.b + k)
            matched_tok[line].append(block.a + k)

    spans = _assign_unclaimed(parsed, matched, matched_tok, words)

    intervals: list[list[float] | None] = []
    for span in spans:
        if span is not None:
            intervals.append([words[span[0]].start, words[span[1]].end])
        else:
            intervals.append(None)
    if not any(intervals):
        raise AlignError("歌词和录音对不上，检查一下贴的是不是这首歌的歌词")

    _interpolate_gaps(intervals, words[-1].end)
    boundaries = _valley_boundaries(intervals, audio) if audio is not None else None
    _pad_and_clamp(intervals, boundaries)

    return [
        {"index": i + 1, "text": en, "zh": zh, "start": round(iv[0], 2), "end": round(iv[1], 2)}
        for i, ((en, zh, _), iv) in enumerate(zip(parsed, intervals))
    ]


def _assign_unclaimed(parsed: list[tuple[str, str, list[str]]],
                      matched: list[list[int]],
                      matched_tok: list[list[int]],
                      words: list[Word]) -> list[list[int] | None]:
    """定每行实际占有的 whisper 词区间 [首词下标, 末词下标]（含分配来的词）。

    相邻两句的锚词之间夹着"无人认领"的转写词时（歌词首/尾词匹配失败所致），
    按两侧歌词的缺失词数分配归属：一侧不缺则全归另一侧，都缺则按比例分。
    锚词间隔 >3s 是间奏而非词间隙，不参与分配（间奏谁都不占，与
    _valley_boundaries 的口径一致）。
    """
    offsets: list[int] = []  # 每行首个词在全局歌词词序列里的下标
    o = 0
    for _, _, tokens in parsed:
        offsets.append(o)
        o += len(tokens)

    spans: list[list[int] | None] = [[m[0], m[-1]] if m else None for m in matched]
    for i in range(len(parsed) - 1):
        if spans[i] is None or spans[i + 1] is None:
            continue
        a_end, b_start = spans[i][1], spans[i + 1][0]
        if words[b_start].start - words[a_end].end > 3.0:
            continue
        gap_n = b_start - a_end - 1  # 两锚词之间的无人认领词数
        if gap_n <= 0:
            continue
        tail_miss = (offsets[i] + len(parsed[i][2]) - 1) - matched_tok[i][-1]
        head_miss = matched_tok[i + 1][0] - offsets[i + 1]
        if tail_miss + head_miss == 0:
            continue
        if head_miss == 0:
            take = gap_n
        elif tail_miss == 0:
            take = 0
        else:
            take = round(gap_n * tail_miss / (tail_miss + head_miss))
        spans[i][1] = a_end + take
        spans[i + 1][0] = b_start - (gap_n - take)
    return spans


def _interpolate_gaps(intervals: list[list[float] | None], last_word_end: float) -> None:
    """未匹配的连续行段插值。就地修改。

    - 中段：在前后已匹配行之间均匀分配时间
    - 头部（前奏没歌词）：按已匹配行的典型句长从下一句起点向前倒推，
      而不是从 0 均摊——否则整段前奏都会被算进前几行歌词
    - 尾部：接在前一行后，每句给 3 秒的典型时长
    """
    matched_durations = sorted(iv[1] - iv[0] for iv in intervals if iv)
    typical = matched_durations[len(matched_durations) // 2] if matched_durations else 3.0
    n = len(intervals)
    i = 0
    while i < n:
        if intervals[i] is not None:
            i += 1
            continue
        j = i
        while j < n and intervals[j] is None:
            j += 1
        prev_end = intervals[i - 1][1] if i > 0 else 0.0
        if j < n:
            next_start = intervals[j][0]
        else:
            next_start = max(prev_end + 3.0 * (j - i), last_word_end)
        if i == 0:
            # 向前倒推：第 1 行终点贴着首个匹配行的起点，而不是从 0 开始
            step = min(typical, next_start / (j - i)) if next_start > 0 else 0.0
            for k in range(j - i):
                start = max(next_start - step * (j - i - k), 0.0)
                intervals[i + k] = [start, max(next_start - step * (j - i - k - 1), start)]
        else:
            step = max(next_start - prev_end, 0.0) / (j - i)
            for k in range(j - i):
                intervals[i + k] = [prev_end + step * k, prev_end + step * (k + 1)]
        i = j


def _valley_boundaries(intervals: list[list[float]], audio: np.ndarray) -> list[float | None]:
    """对每对相邻句，在两句归属分界词之间的空隙里找 RMS 能量最低点作为边界。

    唱歌的尾音会拖过 whisper 给的词 end，中点切分会从尾音中间劈开
    （上句被截断、下句开头带上句尾音）；两句之间几乎总有换气的音量
    低谷，切在那里两头都干净。空隙太窄（真连唱）或太宽（>3s，是间奏
    而非换气）时返回 None，走中点/各自留余量兜底。
    """
    # 25ms 窗、10ms 步长的短时能量包络；人声换气低谷在 100ms 尺度上足够清晰
    frame, hop = int(0.025 * SAMPLE_RATE), int(0.010 * SAMPLE_RATE)
    if len(audio) < frame:
        return [None] * (len(intervals) - 1)
    frames = np.lib.stride_tricks.sliding_window_view(audio, frame)[::hop]
    rms = np.sqrt((frames ** 2).mean(axis=1))
    hop_s = hop / SAMPLE_RATE

    boundaries: list[float | None] = []
    for cur, nxt in zip(intervals, intervals[1:]):
        gap = nxt[0] - cur[1]
        # 空隙超过 3s 是间奏而非换气：两句各自留余量、谁都不占间奏，
        # 否则单句循环会把整段间奏播出来
        if gap > 3.0:
            boundaries.append(None)
            continue
        lo, hi = cur[1] + 0.05, nxt[0] - 0.05  # 两端留边，不切进词本体
        if hi - lo < 0.1:  # 空隙太小，没有换气口可找
            boundaries.append(None)
            continue
        a = min(int(lo / hop_s), len(rms) - 1)
        b = min(int(hi / hop_s) + 1, len(rms))
        if b <= a:
            boundaries.append(None)
            continue
        boundaries.append(round((a + int(np.argmin(rms[a:b]))) * hop_s + 0.0125, 2))
    return boundaries


def _pad_and_clamp(intervals: list[list[float]],
                   boundaries: list[float | None] | None = None) -> None:
    """定每句的最终起止。就地修改。

    首尾外扩小余量；相邻句有换气口低谷边界就直接共用该点，
    否则余量冲突时取中点平分，保证区间不重叠。
    """
    padded = [[max(s - 0.1, 0.0), max(e + 0.15, s + 0.3)] for s, e in intervals]
    for i in range(len(padded) - 1):
        b = boundaries[i] if boundaries else None
        if b is not None:
            padded[i][1] = padded[i + 1][0] = b
        elif padded[i][1] > padded[i + 1][0]:
            mid = (padded[i][1] + padded[i + 1][0]) / 2
            # 归属后的两词紧贴时，余量中点会越界 25ms 切进下词本体：
            # 钳回词缝（两词各自的原始起止之间）内
            lo, hi = intervals[i][1], intervals[i + 1][0]
            if lo <= hi:
                mid = min(max(mid, lo), hi)
            padded[i][1] = padded[i + 1][0] = mid
    for i, iv in enumerate(padded):
        intervals[i] = iv
