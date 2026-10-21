"""歌曲元数据的 JSON 落盘存储。

MVP 阶段单机单用户，一个 JSON 文件足够；引入数据库是过度设计。
"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class Song:
    id: str                # 与录音文件名（不含扩展名）一致
    title: str
    audio_file: str        # wav 文件名
    duration: float
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    lyrics: str | None = None            # 用户粘贴的歌词原文（可中英混贴，中文行是上一句的翻译）
    lines: list[dict] | None = None      # 对齐产出的逐句时间区间 [{index, text, zh, start, end, liaison?, swallow?}]
    align_status: str = "none"           # none | running | done | error
    align_error: str | None = None
    annotate_status: str = "none"        # none | done（规则引擎毫秒级同步完成，无 running/error）
    annotate_error: str | None = None


class SongStore:
    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._songs: dict[str, Song] = {}
        if path.exists():
            for item in json.loads(path.read_text(encoding="utf-8")):
                song = Song(**item)
                self._songs[song.id] = song

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps([asdict(s) for s in self._songs.values()], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def add(self, song: Song) -> None:
        with self._lock:
            self._songs[song.id] = song
            self._flush()

    def update(self, song: Song) -> None:
        # 与 add 同一条路，语义上强调"改已有歌曲"
        self.add(song)

    def get(self, song_id: str) -> Song | None:
        return self._songs.get(song_id)

    def list(self) -> list[Song]:
        # 新歌排前面，符合"录完马上要学"的使用顺序
        return sorted(self._songs.values(), key=lambda s: s.created_at, reverse=True)
