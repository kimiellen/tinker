"""系统输出内录服务：通过 PulseAudio 兼容层（pipewire-pulse）的 monitor 源录制电脑正在播放的声音。

为什么用 parec 而不是 pw-record：pipewire-pulse 提供 `@DEFAULT_MONITOR@` 这个稳定别名，
直接指向默认输出设备的 monitor 源，免去枚举 PipeWire 节点的麻烦。
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


class RecordingError(RuntimeError):
    pass


@dataclass
class Recording:
    id: str
    file: str          # 相对 recordings 目录的文件名
    duration: float    # 秒


class Recorder:
    """管理一次进行中的内录。同一时刻只允许录一条，避免孩子误操作叠录。"""

    def __init__(self, recordings_dir: Path):
        self._dir = recordings_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._proc: subprocess.Popen | None = None
        self._current: Path | None = None
        self._started_at: datetime | None = None
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
            self._started_at = datetime.now()

    def stop(self) -> Recording:
        """停止录音并返回录音元信息；是否入库由调用方决定（支持丢弃场景）。"""
        with self._lock:
            if not self.is_recording:
                raise RecordingError("当前没有进行中的录音")
            proc, path, started = self._proc, self._current, self._started_at
            self._proc = self._current = self._started_at = None

        # 给整个进程组发 SIGTERM：parec 退出 → 管道闭合 → ffmpeg 收尾写全 wav 头
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait()

        if not path.exists() or path.stat().st_size < 1024:
            path.unlink(missing_ok=True)
            raise RecordingError("没有录到声音，请确认录制时歌曲正在播放")

        duration = (datetime.now() - started).total_seconds()
        return Recording(id=path.stem, file=path.name, duration=round(duration, 1))
