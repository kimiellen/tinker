"""系统输出内录服务：通过 PulseAudio 兼容层（pipewire-pulse）的 monitor 源录制电脑正在播放的声音。

为什么用 parec 而不是 pw-record：pipewire-pulse 提供 `@DEFAULT_MONITOR@` 这个稳定别名，
直接指向默认输出设备的 monitor 源，免去枚举 PipeWire 节点的麻烦。

录新歌的真实操作顺序是"先点录制、再起播"和"先停音乐、再点停止"，首尾各有一段
静音（存量实测开头 3.3s、结尾 19s）。保存时用 RMS 包络找出首末有声帧并裁掉空白，
只留 0.2s/0.5s 余量保护起音与收尾。
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .audio import ENVELOPE_FRAME_S, SAMPLE_RATE, decode_audio_16k, rms_envelope

logger = logging.getLogger(__name__)

# 裁切余量：2026-10-25 用户确认的听感取舍（见 docs/specs/record-trim/spec.md）
HEAD_MARGIN_S = 0.2
TAIL_MARGIN_S = 0.5
# 有声判定：最响帧以下 45dB，且不低于 -60dBFS。monitor 无声时是数字零，
# 这个动态范围既滤得掉底噪，又不至于漏掉很轻的演唱/前奏。
SILENCE_DYNAMIC_RANGE_DB = 45.0
SILENCE_FLOOR_DB = -60.0
# 起点要求连续有声 ≥50ms，避免把一次点击声当作歌曲开始
MIN_ONSET_S = 0.05
_DB_FLOOR = 1e-9


class RecordingError(RuntimeError):
    pass


@dataclass
class Recording:
    id: str
    file: str          # 相对 recordings 目录的文件名


def find_sound_bounds(audio: np.ndarray) -> tuple[float, float] | None:
    """返回首末有声时刻（秒，end 为最后一帧的结束时刻）；整段静音返回 None。"""
    rms, hop_s = rms_envelope(audio)
    if len(rms) == 0:
        return None
    db = 20 * np.log10(np.maximum(rms, _DB_FLOOR))
    threshold = max(float(db.max()) - SILENCE_DYNAMIC_RANGE_DB, SILENCE_FLOOR_DB)
    above = db > threshold
    if not above.any():
        return None

    # 起点取第一段连续有声帧；整段只有短促响声时退回第一个有声帧，
    # 好歹别把整首歌判成静音。
    need = max(1, round(MIN_ONSET_S / hop_s))
    start_idx, run = None, 0
    for i, flag in enumerate(above):
        run = run + 1 if flag else 0
        if run >= need:
            start_idx = i - need + 1
            break
    if start_idx is None:
        start_idx = int(np.argmax(above))

    # 尾端同样按"连续有声 ≥50ms"取最后一段：音乐结束后偶尔录到的短点击声
    # 不能被当成结尾，否则中间那段静音会全部留下。整段只有短促响声时退回
    # 最后一个有声帧，短录音至少不会被判成静音。
    tail_end = None
    run = 0
    for i, flag in enumerate(above):
        run = run + 1 if flag else 0
        if run >= need:
            tail_end = i
    if tail_end is None:
        tail_end = len(above) - 1 - int(np.argmax(above[::-1]))
    end = min(len(audio) / SAMPLE_RATE, tail_end * hop_s + ENVELOPE_FRAME_S)
    return start_idx * hop_s, end


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as f:
        frames, rate = f.getnframes(), f.getframerate()
        block = f.getnchannels() * f.getsampwidth()
    # ffmpeg 被强杀时 wav 头的 data size 还是 0xFFFFFFFF 占位，按文件大小兜底，
    # 免得把一首 4 分钟的歌报成 6 小时
    if block and frames * block > path.stat().st_size:
        frames = path.stat().st_size // block
    return frames / rate


def trim_silence(path: Path) -> float:
    """原地裁掉 path 的首尾静音，返回成品实际时长（秒）。

    裁切失败保留原始录音并返回原时长——录音本身比"功能生效"更重要。
    整段没有声音时抛 RecordingError，由调用方决定删除文件。
    """
    duration = wav_duration(path)
    try:
        bounds = find_sound_bounds(decode_audio_16k(path))
    except Exception:  # noqa: BLE001 —— 解码失败也不该把整段录音弄丢
        logger.exception("分析录音 %s 失败，保留原始文件", path.name)
        return duration
    if bounds is None:
        raise RecordingError("没有录到声音，请确认录制时歌曲正在播放")

    start = max(bounds[0] - HEAD_MARGIN_S, 0.0)
    end = min(bounds[1] + TAIL_MARGIN_S, duration)
    if start <= 0.01 and end >= duration - 0.01:
        return duration  # 本来就没有多余空白，不重编码

    tmp = path.with_name(f"{path.stem}.trim.wav")
    try:
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.3f}", "-i", str(path),
             "-t", f"{end - start:.3f}", "-c:a", "pcm_s16le", str(tmp)],
            capture_output=True, check=True,
        )
        os.replace(tmp, path)  # 原子替换：中途失败时原文件仍然完整
        return wav_duration(path)
    except Exception:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        logger.exception("裁切录音 %s 失败，保留原始文件", path.name)
        return duration


def _wait_for_group(pgid: int, timeout: float) -> bool:
    """等进程组里的 parec/ffmpeg 全部退出；bash 先退不代表 wav 已经写完。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


class Recorder:
    """管理一次进行中的内录。同一时刻只允许录一条，避免孩子误操作叠录。"""

    def __init__(self, recordings_dir: Path):
        self._dir = recordings_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        # 上次裁切中途被杀留下的临时文件没有保留价值
        for leftover in self._dir.glob("*.trim.wav"):
            leftover.unlink(missing_ok=True)
        self._proc: subprocess.Popen | None = None
        self._current: Path | None = None
        self._lock = threading.Lock()

    @property
    def is_recording(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        with self._lock:
            if self.is_recording:
                raise RecordingError("已经在录音了")
            rec_id = uuid.uuid4().hex[:8]
            path = self._dir / f"{rec_id}.wav"
            # parec 写原始流，套 ffmpeg 转成标准 wav，避免手写 wav 头逻辑
            # start_new_session：让整条管道独立成进程组，停止时能一起收信号。
            # 只对 bash 发信号不会传递给 parec/ffmpeg，会导致停止后仍在继续写文件。
            self._proc = subprocess.Popen(
                [
                    "bash", "-c",
                    f'parec --device=@DEFAULT_MONITOR@ --format=s16le --rate=44100 --channels=2 '
                    f'| ffmpeg -loglevel error -f s16le -ar 44100 -ac 2 -i - -y "{path}"',
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self._current = path

    def stop(self) -> Recording:
        """停止录音并返回录音元信息；是否入库由调用方决定（支持丢弃场景）。"""
        with self._lock:
            if not self.is_recording:
                raise RecordingError("当前没有进行中的录音")
            proc, path = self._proc, self._current
            self._proc = self._current = None

        # 给整个进程组发 SIGTERM：parec 退出 → 管道闭合 → ffmpeg 收尾写全 wav 头。
        # 只等 bash 不够——bash 会先退，ffmpeg 还要一两百毫秒才把缓冲和文件头落盘，
        # 此时立刻裁切会按半截文件算边界、把结尾静音和尾音一起丢掉。
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
        if not _wait_for_group(pgid, timeout=5):
            os.killpg(pgid, signal.SIGKILL)
            _wait_for_group(pgid, timeout=2)
        proc.wait()

        if not path.exists() or path.stat().st_size < 1024:
            path.unlink(missing_ok=True)
            raise RecordingError("没有录到声音，请确认录制时歌曲正在播放")

        return Recording(id=path.stem, file=path.name)
