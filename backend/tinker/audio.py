"""音频解码公共工具：统一用系统 ffmpeg 解码为 16kHz 单声道 float32。

对齐（whisper）与句界微调吃的是同一种输入，收敛到这里。

为什么不用 faster-whisper 自带的 PyAV 解码：faster-whisper 1.2.x 的
av.open(metadata_errors=...) 与 av>=15 不兼容，而旧版 av 在 Python 3.14
上没有预编译 wheel；系统 ffmpeg 是项目已有的依赖工具，最省事。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000

# 短时能量包络：25ms 窗、10ms 步长。人声换气与首尾静音在 100ms 尺度上都够清晰。
ENVELOPE_FRAME_S = 0.025
ENVELOPE_HOP_S = 0.010


def decode_audio_16k(audio_path: Path) -> np.ndarray:
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(audio_path),
         "-f", "f32le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"],
        capture_output=True, check=True,
    )
    return np.frombuffer(out.stdout, dtype=np.float32)


def rms_envelope(audio: np.ndarray) -> tuple[np.ndarray, float]:
    """短时 RMS 能量包络；返回 (rms, 步长秒数)。

    对齐切边（换气口低谷）与内录首尾静音检测共用同一套包络，口径一致。
    """
    frame, hop = int(ENVELOPE_FRAME_S * SAMPLE_RATE), int(ENVELOPE_HOP_S * SAMPLE_RATE)
    if len(audio) < frame:
        return np.zeros(0), ENVELOPE_HOP_S
    frames = np.lib.stride_tricks.sliding_window_view(audio, frame)[::hop]
    return np.sqrt((frames ** 2).mean(axis=1)), ENVELOPE_HOP_S
