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
    lines: list[dict] | None = None      # 对齐产出的逐句时间区间 [{index, text, zh, start, end, marked?}]
    align_status: str = "none"           # none | running | done | error
    align_error: str | None = None
    annotate_status: str = "none"        # none | done（规则引擎毫秒级同步完成，无 running/error）
    annotate_error: str | None = None
    position: int = 0                    # 歌单顺序，越小越靠前（新歌排最前）
    karaoke_status: str = "none"         # none | running | done | error（伴奏分离）
    karaoke_error: str | None = None
    takes: list[dict] = field(default_factory=list)  # 跟唱成品 [{id, file, start, offset_ms, duration, created_at}]


class SongStore:
    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._songs: dict[str, Song] = {}
        if path.exists():
            for item in json.loads(path.read_text(encoding="utf-8")):
                song = Song(**item)
                self._songs[song.id] = song
        # 旧数据没有 position 字段，默认 0 会全部撞车：按录入时间倒序补出初始顺序
        # （与 M9 之前的展示顺序一致，升级后用户看到的样子不变）
        positions = [s.position for s in self._songs.values()]
        if len(positions) != len(set(positions)):
            with self._lock:
                for i, song in enumerate(
                    sorted(self._songs.values(), key=lambda s: (s.created_at, s.id), reverse=True)
                ):
                    song.position = i
                self._flush()

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps([asdict(s) for s in self._songs.values()], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def add(self, song: Song) -> None:
        with self._lock:
            # 新歌排最前：position 比现有的最小值还小
            if self._songs:
                song.position = min(s.position for s in self._songs.values()) - 1
            self._songs[song.id] = song
            self._flush()

    def update(self, song: Song) -> None:
        with self._lock:
            self._songs[song.id] = song
            self._flush()

    def remove(self, song_id: str) -> Song | None:
        with self._lock:
            song = self._songs.pop(song_id, None)
            if song is not None:
                self._flush()
            return song

    def reorder(self, ids: list[str]) -> bool:
        # 必须恰好覆盖全部歌曲，否则说明前端拿到的是过期列表，拒绝部分生效
        with self._lock:
            if set(ids) != set(self._songs) or len(ids) != len(self._songs):
                return False
            for i, song_id in enumerate(ids):
                self._songs[song_id].position = i
            self._flush()
            return True

    def get(self, song_id: str) -> Song | None:
        return self._songs.get(song_id)

    def list(self) -> list[Song]:
        # 按歌单顺序展示；position 由用户拖拽或录入先后决定
        return sorted(self._songs.values(), key=lambda s: s.position)
