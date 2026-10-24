"""伴奏分离：把内录的系统混音拆成人声轨与伴奏轨（karaoke）。

为什么用子进程跑 demucs，而不是在服务进程里 import torch：
- torch + 模型要占几百 MB 内存，分离是一次性重活，跑完应该把内存还给系统；
- demucs CLI 自带模型下载与缓存，崩了也不影响 Web 服务。

为什么串行：8 核机器上 demucs 会吃满大半 CPU，两首并发只会互相拖慢；
后点的歌排队等前一首跑完（状态仍是 running，前端看到的都是"正在做伴奏"）。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from .store import Song, SongStore

MODEL = "htdemucs"            # 官方 4 轨模型；--two-stems 只取 vocals / no_vocals
MP3_BITRATE = "192"
ERROR_TAIL_CHARS = 300


class KaraokeError(RuntimeError):
    pass


class StemSeparator:
    def __init__(
        self,
        store: SongStore,
        recordings_dir: Path,
        stems_dir: Path,
        hf_cache_dir: Path,
    ):
        self._store = store
        self._recordings = recordings_dir
        self._stems = stems_dir
        self._hf_cache = hf_cache_dir
        self._start_lock = threading.Lock()  # 防两次点击同时起两个线程
        self._run_lock = threading.Lock()    # 全局串行，排队跑
        self._stems.mkdir(parents=True, exist_ok=True)
        self._reset_stale()

    def _reset_stale(self) -> None:
        """服务重启时 running 必然是上次崩溃的残留：改成 error，让用户能重试；
        崩溃留下的临时产物目录也一并清掉（一首歌的中间 wav 有几十 MB）。"""
        for tmp_dir in self._stems.glob(".*.tmp"):
            shutil.rmtree(tmp_dir, ignore_errors=True)
        for song in self._store.list():
            if song.karaoke_status == "running":
                song.karaoke_status = "error"
                song.karaoke_error = "上次生成被中断，再点一次重新生成"
                self._store.update(song)

    def stem_path(self, song_id: str) -> Path:
        return self._stems / song_id / "no_vocals.mp3"

    def start(self, song: Song) -> Song:
        """触发分离；已有成品直接返回，正在跑的拒绝重复触发。"""
        with self._start_lock:
            if self.stem_path(song.id).exists():
                if song.karaoke_status != "done":
                    song.karaoke_status = "done"
                    song.karaoke_error = None
                    self._store.update(song)
                return song
            if song.karaoke_status == "running":
                raise KaraokeError("伴奏正在生成")
            song.karaoke_status = "running"
            song.karaoke_error = None
            self._store.update(song)
            threading.Thread(target=self._run, args=(song.id,), daemon=True).start()
        return song

    def remove(self, song_id: str) -> None:
        """删歌时调用；正在跑的任务跑完发现歌没了会自行丢弃产物。"""
        shutil.rmtree(self._stems / song_id, ignore_errors=True)

    def _run(self, song_id: str) -> None:
        song = self._store.get(song_id)
        if song is None:  # 起线程后立刻被删
            return
        tmp_dir = self._stems / f".{song_id}.tmp"
        try:
            with self._run_lock:
                # 排到队时歌可能已经被删了，再确认一次
                song = self._store.get(song_id)
                if song is None:
                    return
                source = self._recordings / song.audio_file
                if not source.exists():
                    raise KaraokeError("录音文件丢失")
                self._run_demucs(source, tmp_dir)
                produced = tmp_dir / MODEL / source.stem / "no_vocals.mp3"
                if not produced.exists():
                    raise KaraokeError("demucs 没有产出伴奏文件")
                dest = self.stem_path(song_id)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(produced), dest)
        except Exception as e:  # noqa: BLE001 —— 任何失败都要落到状态里给前端展示
            song = self._store.get(song_id)
            if song is not None:
                song.karaoke_status = "error"
                song.karaoke_error = f"生成失败：{e}"[:ERROR_TAIL_CHARS]
                self._store.update(song)
            return
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        # 跑到这里说明产物已就位；歌若中途被删，store 里已查不到，不能 update 把它写回来
        song = self._store.get(song_id)
        if song is None:
            shutil.rmtree(self._stems / song_id, ignore_errors=True)
            return
        song.karaoke_status = "done"
        song.karaoke_error = None
        self._store.update(song)

    def _run_demucs(self, source: Path, tmp_dir: Path) -> None:
        cmd = [
            sys.executable, "-m", "demucs",
            "--two-stems=vocals", "-n", MODEL,
            "--mp3", "--mp3-bitrate", MP3_BITRATE,
            "-o", str(tmp_dir), str(source),
        ]
        # 模型走项目内的 HF 缓存（与 faster-whisper 同一个目录），下载一次后离线可用
        env = {**os.environ, "HF_HOME": str(self._hf_cache)}
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if proc.returncode != 0:
            raise KaraokeError(_stderr_tail(proc.stderr) or f"demucs 退出码 {proc.returncode}")


def _stderr_tail(stderr: str) -> str:
    """进度条用 \\r 刷新且带百分比，取最后几行非进度内容当错误摘要。"""
    lines = [ln.strip() for ln in (stderr or "").replace("\r", "\n").splitlines()]
    lines = [ln for ln in lines if ln and not re.match(r"^\d+%\|", ln)]
    return " ".join(lines[-3:])[:ERROR_TAIL_CHARS]
