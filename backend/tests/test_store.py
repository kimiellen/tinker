"""歌单存储的删除 / 拖动排序 / 旧数据迁移测试。"""
import json

from tinker.store import Song, SongStore


def _song(song_id, title, created_at):
    return Song(id=song_id, title=title, audio_file=f"{song_id}.wav",
                duration=1.0, created_at=created_at)


def _store(tmp_path, songs):
    path = tmp_path / "songs.json"
    path.write_text(json.dumps([s.__dict__ for s in songs], ensure_ascii=False),
                    encoding="utf-8")
    return SongStore(path)


def test_legacy_data_without_position_keeps_newest_first(tmp_path):
    # M9 之前的 songs.json 没有 position 字段，迁移后展示顺序应与旧行为一致（新的在前）
    legacy = [
        {"id": "a", "title": "老歌", "audio_file": "a.wav", "duration": 1.0,
         "created_at": "2026-10-20T10:00:00"},
        {"id": "b", "title": "新歌", "audio_file": "b.wav", "duration": 1.0,
         "created_at": "2026-10-21T10:00:00"},
    ]
    path = tmp_path / "songs.json"
    path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    store = SongStore(path)
    assert [s.id for s in store.list()] == ["b", "a"]
    # 迁移结果要落盘，重开不再靠撞车检测
    assert [s.id for s in SongStore(path).list()] == ["b", "a"]


def test_add_puts_new_song_first(tmp_path):
    store = _store(tmp_path, [_song("a", "A", "2026-10-20T10:00:00"),
                              _song("b", "B", "2026-10-21T10:00:00")])
    store.add(_song("c", "C", "2026-10-22T10:00:00"))
    assert [s.id for s in store.list()] == ["c", "b", "a"]


def test_remove_deletes_and_keeps_order(tmp_path):
    store = _store(tmp_path, [_song("a", "A", "2026-10-20T10:00:00"),
                              _song("b", "B", "2026-10-21T10:00:00")])
    assert store.remove("b").id == "b"
    assert [s.id for s in store.list()] == ["a"]
    assert store.remove("missing") is None


def test_reorder_persists(tmp_path):
    store = _store(tmp_path, [_song("a", "A", "2026-10-20T10:00:00"),
                              _song("b", "B", "2026-10-21T10:00:00"),
                              _song("c", "C", "2026-10-22T10:00:00")])
    assert store.reorder(["a", "c", "b"]) is True
    assert [s.id for s in store.list()] == ["a", "c", "b"]
    # 重开存储顺序不变（持久化生效）
    assert [s.id for s in SongStore(store._path).list()] == ["a", "c", "b"]


def test_reorder_rejects_partial_or_stale_id_set(tmp_path):
    store = _store(tmp_path, [_song("a", "A", "2026-10-20T10:00:00"),
                              _song("b", "B", "2026-10-21T10:00:00")])
    # 少一个 / 多一个 / 有重复都必须拒绝，且顺序不被改坏
    assert store.reorder(["a"]) is False
    assert store.reorder(["a", "b", "c"]) is False
    assert store.reorder(["a", "a"]) is False
    assert [s.id for s in store.list()] == ["b", "a"]
