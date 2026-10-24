"""伴奏分离：把内录的系统混音拆成人声轨与伴奏轨（karaoke）。

为什么用子进程跑 demucs，而不是在服务进程里 import torch：
- torch + 模型要占几百 MB 内存，分离是一次性重活，跑完应该把内存还给系统；
- demucs CLI 自带模型下载与缓存，崩了也不影响 Web 服务。

为什么串行：8 核机器上 demucs 会吃满大半 CPU，两首并发只会互相拖慢；
后点的歌排队等前一首跑完（状态仍是 running，前端看到的都是"正在做伴奏"）。

成品为什么不直接要 demucs 的 mp3：分离后还要做响度对齐和临场感 EQ
（见 docs/specs/karaoke/spec.md），统一用 flac 中间产物再走一遍 ffmpeg 编码。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from .store import Song, SongStore

MODEL = "htdemucs_ft"        # 4 个微调模型的 bag；人声残留比 htdemucs 少，代价是 4 倍时间
DEMUCS_BAGS = 4              # 进度条每跑完一个 bag 会从 0 重新开始，用它折算总进度
STEM_VERSION = 2             # 2 = htdemucs_ft + 响度对齐 + EQ + 320k；旧版本成品按需重做
MP3_BITRATE = "320k"
EQ_FILTER = "highshelf=f=3000:g=2,highshelf=f=10000:g=2"
GAIN_MIN_DB, GAIN_MAX_DB = -6.0, 12.0
ERROR_TAIL_CHARS = 300
_PCT_RE = re.compile(rb"(\d{1,3})%")


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
        """启动时收拾上次崩溃/旧版本留下的状态。

        - running 必然是进程被杀时的残留（改成 error，让用户能重试）；
        - 崩溃留下的临时目录清掉；
        - 旧版本成品（模型或后处理变了）直接作废，下次点伴奏时按需重做。
        """
        for tmp_dir in self._stems.glob(".*.tmp"):
            shutil.rmtree(tmp_dir, ignore_errors=True)
        for song in self._store.list():
            if song.karaoke_status == "running":
                song.karaoke_status = "error"
                song.karaoke_error = "上次生成被中断，再点一次重新生成"
                song.karaoke_progress = 0
                self._store.update(song)
            elif song.karaoke_status == "done" and song.karaoke_version != STEM_VERSION:
                song.karaoke_status = "none"
                song.karaoke_progress = 0
                song.karaoke_error = None
                self._store.update(song)
                shutil.rmtree(self._stems / song.id, ignore_errors=True)

    def stem_path(self, song_id: str) -> Path:
        return self._stems / song_id / "no_vocals.mp3"

    def is_current(self, song: Song) -> bool:
        return song.karaoke_version == STEM_VERSION and self.stem_path(song.id).exists()

    def start(self, song: Song) -> Song:
        """触发分离；已有当前版本成品直接返回，正在跑的拒绝重复触发。"""
        with self._start_lock:
            if self.is_current(song):
                if song.karaoke_status != "done":
                    song.karaoke_status = "done"
                    song.karaoke_error = None
                    song.karaoke_progress = 100
                    self._store.update(song)
                return song
            if song.karaoke_status == "running":
                raise KaraokeError("伴奏正在生成")
            song.karaoke_status = "running"
            song.karaoke_error = None
            song.karaoke_progress = 0
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
                shutil.rmtree(tmp_dir, ignore_errors=True)
                stem_flac = self._run_demucs(
                    source, tmp_dir, lambda pct: self._set_progress(song_id, pct))
                out_tmp = tmp_dir / "no_vocals.mp3"
                gain = self._postprocess(source, stem_flac, out_tmp)
                dest = self.stem_path(song_id)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(out_tmp), dest)
        except Exception as e:  # noqa: BLE001 —— 任何失败都要落到状态里给前端展示
            song = self._store.get(song_id)
            if song is not None:
                song.karaoke_status = "error"
                song.karaoke_error = f"生成失败：{e}"[:ERROR_TAIL_CHARS]
                song.karaoke_progress = 0
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
        song.karaoke_progress = 100
        song.karaoke_version = STEM_VERSION
        song.karaoke_gain_db = gain
        self._store.update(song)

    def _set_progress(self, song_id: str, pct: int) -> None:
        song = self._store.get(song_id)
        if song is None or song.karaoke_status != "running":
            return
        song.karaoke_progress = pct
        self._store.update(song)

    def _run_demucs(self, source: Path, tmp_dir: Path, on_progress) -> Path:
        """跑 demucs，边跑边把百分比报给 on_progress；返回 no_vocals.flac。"""
        cmd = [
            sys.executable, "-m", "demucs",
            "--two-stems=vocals", "-n", MODEL, "--flac",
            "-o", str(tmp_dir), str(source),
        ]
        # 模型走项目内的 HF 缓存（与 faster-whisper 同一目录），下载一次后离线可用；
        # 关掉 Xet 传输：实测 ft 模型走 Xet 会卡在 0 字节，普通 HTTP 正常。
        env = {
            **os.environ,
            "HF_HOME": str(self._hf_cache),
            "HF_HUB_DISABLE_XET": "1",
        }
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env,
        )
        buf = b""
        bag = 0
        last_pct = -1
        last_overall = -1
        error_lines: list[str] = []  # 保留非进度行，失败时当错误摘要
        while True:
            chunk = proc.stderr.read(1024)
            if not chunk:
                break
            buf += chunk
            *lines, buf = re.split(rb"[\r\n]", buf)
            for line in lines:
                match = _PCT_RE.search(line)
                if not match:
                    text = line.decode("utf-8", "replace").strip()
                    if text:
                        error_lines = (error_lines + [text])[-10:]
                    continue
                pct = int(match.group(1))
                if pct + 5 < last_pct:  # bag 跑完，进度条从 0 重来
                    bag += 1
                last_pct = pct
                overall = min(99, (bag * 100 + pct) // DEMUCS_BAGS)
                if overall - last_overall >= 2:
                    last_overall = overall
                    on_progress(overall)
        if proc.wait() != 0:
            tail = "\n".join(error_lines) + "\n" + buf.decode("utf-8", "replace")
            raise KaraokeError(_stderr_tail(tail) or "demucs 失败")
        produced = tmp_dir / MODEL / source.stem / "no_vocals.flac"
        if not produced.exists():
            raise KaraokeError("demucs 没有产出伴奏文件")
        return produced

    def _postprocess(self, source_audio: Path, stem_flac: Path, out_mp3: Path) -> float:
        """响度对齐原曲 + 临场感 EQ + 320k 编码；返回实际应用的增益（dB）。"""
        target, current = _lufs(source_audio), _lufs(stem_flac)
        gain = 0.0
        if target is not None and current is not None:
            gain = max(GAIN_MIN_DB, min(GAIN_MAX_DB, target - current))
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(stem_flac),
             "-af", f"{EQ_FILTER},volume={gain:.2f}dB",
             "-codec:a", "libmp3lame", "-b:a", MP3_BITRATE, str(out_mp3)],
            capture_output=True, check=True,
        )
        return round(gain, 2)


def _lufs(path: Path) -> float | None:
    """用 ffmpeg loudnorm 读整首的积分响度；解析失败返回 None（后处理退化为不加增益）。"""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
         "-af", "loudnorm=print_format=json", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    match = re.search(r'\{[^{}]*"input_i"[^{}]*\}', proc.stderr, re.S)
    if not match:
        return None
    try:
        value = float(json.loads(match.group(0))["input_i"])
    except (ValueError, KeyError):
        return None
    return value if value > -70 else None


def _stderr_tail(stderr: str) -> str:
    """进度条用 \\r 刷新且带百分比，取最后几行非进度内容当错误摘要。"""
    lines = [ln.strip() for ln in (stderr or "").replace("\r", "\n").splitlines()]
    lines = [ln for ln in lines if ln and not re.match(r"^\d+%\|", ln)]
    return " ".join(lines[-3:])[:ERROR_TAIL_CHARS]
