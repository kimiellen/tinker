"""跟唱评估：原唱 vs 跟唱的音高/节奏逐句对比。

跟唱时原唱正在播放（UI 引导戴耳机），两条音轨大体同时间轴。
节奏判断不靠"开口时刻"（窗口会切掉抢拍、还会吃到上一句的尾巴），
而是对每句窗口内的有声/无声序列做互相关，得到该句的整体时间偏移；
全部句子的偏移取中位数作为全局偏移（录音与播放启动的先后差），
单句偏移与全局偏移之差才是这句真正的抢拍/拖拍。

每句产出一张反馈卡（与 UI 定稿 ④ 对应）：
- 音高：按该句时间偏移对齐后，双方同时有声帧的 MIDI 差值中位数，
  超 1 个半音判偏高/偏低
- 节奏：单句偏移 - 全局偏移，超 0.35s 判抢拍/拖拍
- 都没问题 → 表扬卡
反馈文案面向一年级小朋友：明确说哪句、什么问题、怎么练。
"""
from __future__ import annotations

import numpy as np

from .audio import SAMPLE_RATE, decode_audio_16k

FMIN, FMAX = 80.0, 800.0   # 覆盖童声/女声歌唱音域
HOP = 512                  # 16kHz 下约 32fps

PITCH_TOLERANCE = 1.0      # 半音
TIMING_TOLERANCE = 0.35    # 秒
MIN_BOTH_VOICED = 10       # 双方都出声的最少帧数，不足则不做音高判断
MAX_LINE_LAG = 1.5         # 单句互相关只信 ±1.5s 内的对齐


class EvaluateError(Exception):
    """评估失败（录音里听不到人声等），message 面向用户。"""


def pitch_track(y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """pyin 基频提取，返回 (帧时间, MIDI 音高)；无声帧音高为 nan。"""
    import librosa  # 导入慢（秒级），只在评估路径加载

    f0, _, _ = librosa.pyin(y, sr=SAMPLE_RATE, fmin=FMIN, fmax=FMAX,
                            hop_length=HOP, frame_length=2048)
    times = librosa.times_like(f0, sr=SAMPLE_RATE, hop_length=HOP)
    return times, librosa.hz_to_midi(f0)


def line_time_offset(line: dict, ref_times: np.ndarray, ref_midi: np.ndarray,
                     user_times: np.ndarray, user_midi: np.ndarray) -> float | None:
    """一句的时间偏移（秒）：用户有声序列相对原唱有声序列的互相关峰值位置。

    > 0 表示这句唱晚了。双方任一侧在该句几乎无声时返回 None（无法判断）。
    """
    frame_s = HOP / SAMPLE_RATE
    max_lag = int(MAX_LINE_LAG / frame_s)
    ref_in = (ref_times >= line["start"]) & (ref_times < line["end"])
    user_in = (user_times >= line["start"] - MAX_LINE_LAG) & (user_times < line["end"] + MAX_LINE_LAG)
    ref_v = np.isfinite(ref_midi[ref_in]).astype(float)
    user_v = np.isfinite(user_midi[user_in]).astype(float)
    if ref_v.sum() < MIN_BOTH_VOICED or user_v.sum() < MIN_BOTH_VOICED:
        return None
    # 用户窗口的实际起点（可能被歌曲 0 点截断），互相关 lag 要以它为基准换算
    user_start = float(user_times[user_in][0])
    center = (line["start"] - user_start) / frame_s
    corr = np.correlate(user_v, ref_v, mode="full")
    lags = np.arange(-(len(ref_v) - 1), len(user_v))
    window = (lags >= center - max_lag) & (lags <= center + max_lag)
    lag = lags[window][np.argmax(corr[window])]
    return lag * frame_s - (line["start"] - user_start)


def global_offset(offsets: list[float | None]) -> float:
    """全局偏移 = 各句偏移的中位数；个别唱错节奏的句不影响整体对齐。"""
    valid = [o for o in offsets if o is not None]
    if not valid:
        raise EvaluateError("录音里听不到歌声，换个安静的地方、戴上耳机再唱一次")
    return float(np.median(valid))


def judge_line(line: dict, ref_times: np.ndarray, ref_midi: np.ndarray,
               user_times: np.ndarray, user_midi: np.ndarray,
               line_lag: float | None, base_lag: float) -> dict:
    """评估一句，返回 {index, text, verdict, detail}。verdict: good | bad | warn | silent。"""
    idx, text = line["index"], line["text"]
    ref_in = (ref_times >= line["start"]) & (ref_times < line["end"])
    lag = line_lag if line_lag is not None else base_lag
    user_in = (user_times - lag >= line["start"]) & (user_times - lag < line["end"])

    ref_seg, user_seg = ref_midi[ref_in], user_midi[user_in]
    # 两条轨的帧网格不完全重合，窗口帧数可能差 1 帧，对齐到较短者
    n = min(len(ref_seg), len(user_seg))
    ref_seg, user_seg = ref_seg[:n], user_seg[:n]
    ref_v = np.isfinite(ref_seg)
    user_v = np.isfinite(user_seg)
    both = ref_v & user_v

    if ref_v.sum() < MIN_BOTH_VOICED:
        return _card(idx, text, "warn", "这句原唱声音太轻，没办法判断，跳过啦")
    if user_v.sum() < MIN_BOTH_VOICED:
        return _card(idx, text, "silent", "这句没听到你唱，大声一点试试！")

    if both.sum() >= MIN_BOTH_VOICED:
        dev = float(np.median(user_seg[both] - ref_seg[both]))
        if abs(dev) > PITCH_TOLERANCE:
            direction = "偏高" if dev > 0 else "偏低"
            hint = "把声音压低一点" if dev > 0 else "把声音抬高一点"
            return _card(idx, text, "bad",
                         f"这句的音唱{direction}了（差了约 {abs(dev):.1f} 个半音），{hint}，点这句再听几遍原唱～")

    if line_lag is not None:
        dt = line_lag - base_lag
        if dt < -TIMING_TOLERANCE:
            return _card(idx, text, "warn", "这句开头唱早了（抢拍），跟着 0.75x 慢速再练一次")
        if dt > TIMING_TOLERANCE:
            return _card(idx, text, "warn", "这句开头唱晚了（拖拍），跟着 0.75x 慢速再练一次")
    return _card(idx, text, "good", "音高和节奏都很准，唱得真棒！")


def _card(index: int, text: str, verdict: str, detail: str) -> dict:
    return {"index": index, "text": text, "verdict": verdict, "detail": detail}


def _export_curve(ref_times: np.ndarray, ref_midi: np.ndarray,
                  user_times: np.ndarray, user_midi: np.ndarray,
                  base_lag: float, judgments: list[dict]) -> dict:
    """曲线数据抽稀到 ~8fps 供前端 canvas 绘制；无声帧为 null。"""
    step = 4  # 32fps → 8fps，125 秒约 1000 个点，够平滑也够小
    verdicts = {j["index"]: j["verdict"] for j in judgments}

    def clean(arr):
        return [None if not np.isfinite(v) else round(float(v), 1) for v in arr[::step]]

    return {
        "ref_times": [round(float(t), 2) for t in ref_times[::step]],
        "user_times": [round(float(t - base_lag), 2) for t in user_times[::step]],
        "ref": clean(ref_midi),
        "user": clean(user_midi),
        "line_verdicts": verdicts,
    }


def evaluate(song_lines: list[dict], ref_audio, user_audio, user_origin: float = 0.0) -> dict:
    """整首评估：输入逐句区间与两段音频路径，返回 {judgments, curve}。

    user_origin：跟唱录音的时间原点在原唱时间轴上的位置。整首跟唱为 0；
    单句跟唱时前端只放这一句，录音从句首开始，origin = 该句 start。
    """
    ref_times, ref_midi = pitch_track(decode_audio_16k(ref_audio))
    user_times, user_midi = pitch_track(decode_audio_16k(user_audio))
    user_times = user_times + user_origin
    lags = [line_time_offset(l, ref_times, ref_midi, user_times, user_midi) for l in song_lines]
    base = global_offset(lags)
    judgments = [judge_line(l, ref_times, ref_midi, user_times, user_midi, lag, base)
                 for l, lag in zip(song_lines, lags)]
    return {"judgments": judgments,
            "curve": _export_curve(ref_times, ref_midi, user_times, user_midi, base, judgments)}
