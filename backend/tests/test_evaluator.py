"""evaluator 纯函数测试：时间偏移估计与逐句评判（不跑 pyin）。"""
import numpy as np
import pytest

from tinker.audio import SAMPLE_RATE
from tinker.evaluator import HOP, EvaluateError, global_offset, judge_line, line_time_offset

FPS = SAMPLE_RATE / HOP  # 帧率 31.25


def make_track(segments: list[tuple[float, float, float]], duration: float):
    """造一条合成音高轨：segments 为 (起始秒, 结束秒, MIDI)，其余无声。"""
    n = int(duration * FPS) + 1
    midi = np.full(n, np.nan)
    for start, end, m in segments:
        midi[int(start * FPS):int(end * FPS)] = m
    times = np.arange(n) / FPS
    return times, midi


LINE = {"index": 1, "text": "Twinkle, twinkle, little star", "start": 1.0, "end": 4.0}


def judge(user_segments, line_lag=0.0, base_lag=0.0, ref_segments=None):
    ref_segments = ref_segments if ref_segments is not None else [(1.0, 4.0, 60.0)]
    ref_times, ref = make_track(ref_segments, 6.0)
    user_times, user = make_track(user_segments, 6.0)
    return judge_line(LINE, ref_times, ref, user_times, user, line_lag, base_lag)


def test_good_when_pitch_and_timing_match():
    assert judge([(1.0, 4.0, 60.3)])["verdict"] == "good"


def test_pitch_too_high():
    card = judge([(1.0, 4.0, 62.5)])  # 高 2.5 个半音
    assert card["verdict"] == "bad"
    assert "偏高" in card["detail"]


def test_pitch_too_low():
    card = judge([(1.0, 4.0, 57.5)])
    assert card["verdict"] == "bad"
    assert "偏低" in card["detail"]


def test_rushing():
    card = judge([(0.4, 3.4, 60.0)], line_lag=-0.6)  # 比全局早 0.6s
    assert card["verdict"] == "warn"
    assert "抢拍" in card["detail"]


def test_dragging():
    card = judge([(1.6, 4.6, 60.0)], line_lag=0.6)
    assert card["verdict"] == "warn"
    assert "拖拍" in card["detail"]


def test_lag_matching_global_is_not_rushing():
    # 整首都晚 0.6s（录音按晚了一点）：单句 lag 与全局一致，不算拖拍
    card = judge([(1.6, 4.6, 60.0)], line_lag=0.6, base_lag=0.6)
    assert card["verdict"] == "good"


def test_silent_user():
    assert judge([])["verdict"] == "silent"


def test_line_time_offset_finds_delay():
    ref_times, ref = make_track([(1.0, 4.0, 60.0)], 6.0)
    user_times, user = make_track([(1.8, 4.8, 60.0)], 6.0)  # 这句晚 0.8s
    lag = line_time_offset(LINE, ref_times, ref, user_times, user)
    assert lag == pytest.approx(0.8, abs=0.1)


def test_line_time_offset_none_when_silent():
    ref_times, ref = make_track([(1.0, 4.0, 60.0)], 6.0)
    user_times, user = make_track([], 6.0)
    assert line_time_offset(LINE, ref_times, ref, user_times, user) is None


def test_global_offset_is_robust_median():
    # 一句抢拍（-0.6）不影响全局对齐
    assert global_offset([0.1, 0.12, -0.6, 0.11, 0.1, None]) == pytest.approx(0.1, abs=1e-6)


def test_global_offset_raises_when_all_none():
    with pytest.raises(EvaluateError):
        global_offset([None, None])
