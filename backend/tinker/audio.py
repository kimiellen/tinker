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


def decode_audio_16k(audio_path: Path) -> np.ndarray:
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(audio_path),
         "-f", "f32le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"],
        capture_output=True, check=True,
    )
    return np.frombuffer(out.stdout, dtype=np.float32)
