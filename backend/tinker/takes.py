"""跟唱录音：伴奏播放时录系统默认麦克风，停止后按启动偏移与伴奏混成成品 mp3。

为什么录麦克风而不是回采系统输出：孩子的声音只在麦克风里；伴奏是现成文件，
混音时按偏移叠加即可。回采系统输出会把伴奏录成两份（一份来自播放一份来自麦克风）。
偏移 = 起播墙钟（前端用播放器音频时钟回推）- 首帧采集墙钟（后端实测）。
前后端同机运行，Date.now() 与 time.time() 同源；默认麦克风挂起唤醒要 1~2s，
所以起录接口会等首帧真的到了再返回，避免第一次录音整段错位。
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .store import Song, SongStore

MIC_GAIN = 2.0          # 孩子对着麦克风唱通常偏轻，混音时给人声 +6dB
MP3_BITRATE = "192k"
MIN_TAKE_SECONDS = 0.5  # 比这还短的录音当作误触
OVERRUN_GRACE_SECONDS = 15  # 歌播完后还录这么久就说明前端已经跑掉（关页/刷新技术上无法通知）
MIC_RATE = 44100        # 与系统内录一致的采样率/声道，混音时不用重采样
MIC_CHANNELS = 2
BYTES_PER_SEC = MIC_RATE * MIC_CHANNELS * 2


class TakeError(RuntimeError):
    pass


@dataclass
class _ActiveTake:
    take_id: str
    song_id: str
    start: float        # 跟唱的歌曲起始位置（秒）
    raw: Path           # parec 直接写的 s16le 裸流
    wav: Path           # 停止后转成的标准 wav（混音用）
    proc: subprocess.Popen
    handle: object      # 裸流文件句柄，停止时要关闭
    started_at_ms: int  # 发起采集的墙钟（毫秒）
    # 真正采到第一帧的墙钟：默认麦克风可能处于挂起态，唤醒要 1~2 秒，
    # 用 started_at_ms 算偏移会让第一次录音整体错位（实测吃了 1.9s）。
    capture_started_ms: int | None = None


class TakeRecorder:
    """同一时刻只允许一条跟唱录音：孩子不会一边唱两首，也省掉并发混音。"""

    def __init__(self, store: SongStore, stems_dir: Path, takes_dir: Path):
        self._store = store
        self._stems = stems_dir
        self._takes = takes_dir
        self._takes.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._active: _ActiveTake | None = None
        self._clean_stale()

    def _clean_stale(self) -> None:
        """上次进程被杀时留下的中间文件没有成品价值，启动时清掉。"""
        for leftover in list(self._takes.glob("*/*.wav")) + list(self._takes.glob("*/*.pcm")):
            leftover.unlink(missing_ok=True)

    def start(self, song: Song, start: float) -> dict:
        with self._lock:
            if self._active is not None:
                raise TakeError("正在录着呢")
            stem = self._stems / song.id / "no_vocals.mp3"
            if not stem.exists():
                raise TakeError("伴奏还没做好")
            if not (0 <= start < max(song.duration - 1, 0)):
                raise TakeError("起唱位置超出歌曲范围")
            take_id = uuid.uuid4().hex[:8]
            folder = self._takes / song.id
            folder.mkdir(parents=True, exist_ok=True)
            raw, wav = folder / f"{take_id}.pcm", folder / f"{take_id}.wav"
            # 为什么直接录裸 PCM 而不是 parec | ffmpeg 管道：停止时要给 ffmpeg 发信号，
            # bash 会先退出、ffmpeg 还在收尾写 wav 头，紧接着读文件会 EOF。parec 写裸流
            # 没有文件头，进程退出即完整；停止后再用 ffmpeg 转 wav。
            handle = open(raw, "wb")
            try:
                proc = subprocess.Popen(
                    ["parec", "--device=@DEFAULT_SOURCE@", "--format=s16le",
                     "--rate", str(MIC_RATE), "--channels", str(MIC_CHANNELS)],
                    stdout=handle, stderr=subprocess.DEVNULL, start_new_session=True,
                )
            except OSError:
                handle.close()
                raw.unlink(missing_ok=True)
                raise TakeError("麦克风打不开")
            active = _ActiveTake(take_id, song.id, start, raw, wav, proc, handle, _now_ms())
            self._active = active
            threading.Thread(target=self._watch_capture, args=(active,), daemon=True).start()
            # 关标签页/刷新不会调用 stop：不设上限的话麦克风会一直录到服务重启
            max_seconds = max(song.duration - start, 1) + OVERRUN_GRACE_SECONDS
            threading.Thread(target=self._watch_overrun, args=(active, max_seconds), daemon=True).start()
        # 等麦克风真的开始采集再返回，前端随后才起播——这样不会漏掉开头的人声。
        # 默认输入源从挂起到唤醒实测要 1~2 秒，最多等 3 秒（超时就照常开始，偏移兜底）。
        deadline = time.time() + 3
        while active.capture_started_ms is None and time.time() < deadline:
            time.sleep(0.02)
        return {"take_id": take_id, "capture_started_ms": active.capture_started_ms}

    def stop(self, song: Song, take_id: str, playback_started_ms: int) -> dict:
        active = self._take_active(song.id, take_id)
        # 偏移 = 音乐起播的墙钟 - 麦克风真正开始采集的墙钟；
        # 采集比播放晚（麦克风刚唤醒）就是负值，混音时给麦克风补静音。
        offset_ms = int(playback_started_ms) - (active.capture_started_ms or active.started_at_ms)
        self._stop_process(active)
        try:
            _pcm_to_wav(active.raw, active.wav)
        except subprocess.CalledProcessError as e:
            active.raw.unlink(missing_ok=True)
            active.wav.unlink(missing_ok=True)
            raise TakeError("录音文件转换失败") from e
        finally:
            active.raw.unlink(missing_ok=True)
        mic_seconds = _wav_duration(active.wav)
        if mic_seconds < MIN_TAKE_SECONDS:
            active.wav.unlink(missing_ok=True)
            raise TakeError("录得太短了，再唱久一点吧")
        # 偏移超出录音时长说明前端时间戳乱了：钳到合法范围，至少留 0.2s 成品
        offset_ms = max(-5000, min(int(offset_ms), int(mic_seconds * 1000) - 200))
        stem = self._stems / song.id / "no_vocals.mp3"
        out = self._takes / song.id / f"{take_id}.mp3"
        try:
            subprocess.run(mix_args(stem, active.wav, active.start, offset_ms, out),
                           capture_output=True, check=True)
        except subprocess.CalledProcessError as e:
            out.unlink(missing_ok=True)
            active.wav.unlink(missing_ok=True)
            raise TakeError("混音失败，再录一次吧") from e
        active.wav.unlink(missing_ok=True)
        duration = round(max(0.1, mic_seconds - max(offset_ms, 0) / 1000), 2)
        take = {
            "id": take_id,
            "file": out.name,
            "start": round(active.start, 2),
            "offset_ms": offset_ms,
            "duration": duration,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        # 停止期间歌可能被删了：成品直接丢弃，不能把已删除的歌写回来
        fresh = self._store.get(song.id)
        if fresh is None:
            out.unlink(missing_ok=True)
            raise TakeError("歌曲已经被删除了")
        fresh.takes = (fresh.takes or []) + [take]
        self._store.update(fresh)
        return take

    def delete(self, song: Song, take_id: str) -> None:
        with self._lock:
            if self._active is not None and self._active.take_id == take_id:
                raise TakeError("正在录这条，先停止再删")
            keep = [t for t in (song.takes or []) if t["id"] != take_id]
            if len(keep) == len(song.takes or []):
                raise TakeError("没有这条演唱")
            (self._takes / song.id / f"{take_id}.mp3").unlink(missing_ok=True)
            song.takes = keep
            self._store.update(song)

    def take_path(self, song: Song, take_id: str) -> Path:
        # 只认 songs.json 里登记过的 take_id，避免路径参数直接拼文件系统
        for take in song.takes or []:
            if take["id"] == take_id:
                path = self._takes / song.id / take["file"]
                if path.exists():
                    return path
        raise TakeError("没有这条演唱")

    def cancel(self, song_id: str, take_id: str) -> None:
        """前端关页/刷前发来的兜底：丢掉进行中的这条录音，不留半成品。"""
        with self._lock:
            active = self._active
            if active is None or active.song_id != song_id or active.take_id != take_id:
                raise TakeError("没有进行中的录音")
            self._active = None
        self._stop_process(active)
        active.raw.unlink(missing_ok=True)
        active.wav.unlink(missing_ok=True)

    def remove_all(self, song_id: str) -> None:
        """删歌时调用：中断进行中的录音并清空目录。"""
        with self._lock:
            if self._active is not None and self._active.song_id == song_id:
                self._stop_process(self._active)
                self._active = None
        shutil.rmtree(self._takes / song_id, ignore_errors=True)

    # ---- 内部 ----

    def _watch_capture(self, active: _ActiveTake) -> None:
        """盯住裸流文件第一次出现字节的时刻。

        不用"进程起来"当采集起点：默认麦克风挂起时 parec 要 1~2s 才出第一帧
        （实测第一次录音 3s 只录到 1.1s）。首字节文件时间戳再按已写入字节数
        回推，得到第一帧被采集的墙钟。
        """
        while active.capture_started_ms is None and self._active is active:
            try:
                size = active.raw.stat().st_size
            except FileNotFoundError:
                return
            if size > 0:
                started = _now_ms() - round(size / BYTES_PER_SEC * 1000)
                # 文件落盘有缓冲，回推值最多只能早于发起时刻，钳一下
                active.capture_started_ms = max(started, active.started_at_ms)
                return
            time.sleep(0.01)

    def _watch_overrun(self, active: _ActiveTake, max_seconds: float) -> None:
        """录音超过"剩余歌曲长度 + 宽限"就强制收尾丢弃：前端已跑掉，没人来 stop。"""
        while self._active is active:
            started = active.capture_started_ms or active.started_at_ms
            if _now_ms() - started > max_seconds * 1000:
                with self._lock:
                    if self._active is not active:
                        return
                    self._active = None
                self._stop_process(active)
                active.raw.unlink(missing_ok=True)
                active.wav.unlink(missing_ok=True)
                return
            time.sleep(0.5)

    def _take_active(self, song_id: str, take_id: str) -> _ActiveTake:
        with self._lock:
            active = self._active
            if active is None or active.song_id != song_id or active.take_id != take_id:
                raise TakeError("没有进行中的录音")
            self._active = None
            return active

    @staticmethod
    def _stop_process(active: _ActiveTake) -> None:
        # start_new_session 让 parec 独占一个进程组，停的时候整组收信号
        try:
            os.killpg(os.getpgid(active.proc.pid), signal.SIGTERM)
            active.proc.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(os.getpgid(active.proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            active.proc.wait()
        finally:
            if active.handle is not None and not active.handle.closed:
                active.handle.close()


def mix_args(stem: Path, mic: Path, start: float, offset_ms: int, out: Path) -> list[str]:
    """伴奏从 start 起播；麦克风录音按偏移对齐后混音。

    offset_ms > 0：录音比音乐早开始，丢掉开头多录的那段；
    offset_ms < 0：录音比音乐晚开始，用 adelay 在开头补静音。
    """
    trim = max(0, offset_ms) / 1000.0
    delay = max(0, -offset_ms)
    args = ["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.3f}", "-i", str(stem)]
    if trim > 0:
        args += ["-ss", f"{trim:.3f}"]
    args += ["-i", str(mic)]
    voice = f"[1:a]volume={MIC_GAIN}"
    if delay:
        voice = f"[1:a]adelay={delay}:all=1,volume={MIC_GAIN}"
    args += [
        "-filter_complex",
        f"[0:a]volume=1.0[music];{voice}[voice];"
        "[music][voice]amix=inputs=2:duration=shortest:normalize=0",
        "-codec:a", "libmp3lame", "-b:a", MP3_BITRATE,
        str(out),
    ]
    return args


def _now_ms() -> int:
    return round(time.time() * 1000)


def _pcm_to_wav(raw: Path, wav: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "s16le", "-ar", str(MIC_RATE),
         "-ac", str(MIC_CHANNELS), "-i", str(raw), str(wav)],
        check=True,
    )


def _wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()
