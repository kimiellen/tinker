"""伴奏分离（karaoke）的后台任务与 API 状态机测试。

真实 demucs 一首歌要跑 1 分钟，单测里用假 subprocess 顶替，只验证状态流转与文件落位；
真实分离效果由人工验证（见 docs/specs/karaoke/tasks.md）。
"""
import threading
import time
from pathlib import Path

import pytest

from tinker import karaoke as karaoke_mod
from tinker.karaoke import KaraokeError, StemSeparator
from tinker.store import Song, SongStore


def _make(tmp_path):
    recordings = tmp_path / "recordings"
    stems = tmp_path / "stems"
    recordings.mkdir()
    (recordings / "a.wav").write_bytes(b"fake")
    store = SongStore(tmp_path / "songs.json")
    store.add(Song(id="a", title="A", audio_file="a.wav", duration=10.0))
    return store, recordings, stems


class FakeDemucs:
    """按 demucs 的输出布局造一个 no_vocals.mp3，行为可用 event 控制。"""

    def __init__(self, returncode=0, stderr="", release=None):
        self.returncode = returncode
        self.stderr = stderr
        self.started = threading.Event()
        self._release = release

    def __call__(self, cmd, capture_output, text, env):
        self.started.set()
        if self._release is not None:
            assert self._release.wait(5), "测试没释放假 demucs"
        if self.returncode == 0:
            out_dir = Path(cmd[cmd.index("-o") + 1])
            source = Path(cmd[-1])
            produced = out_dir / "htdemucs" / source.stem / "no_vocals.mp3"
            produced.parent.mkdir(parents=True)
            produced.write_bytes(b"mp3")
        return karaoke_mod.subprocess.CompletedProcess(cmd, self.returncode, "", self.stderr)


def _wait(store, song_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        song = store.get(song_id)
        if song and song.karaoke_status != "running":
            return song
        time.sleep(0.01)
    raise AssertionError("分离任务没有在超时内结束")


def test_start_separates_and_caches(tmp_path, monkeypatch):
    store, recordings, stems = _make(tmp_path)
    fake = FakeDemucs()
    monkeypatch.setattr(karaoke_mod.subprocess, "run", fake)
    sep = StemSeparator(store, recordings, stems, tmp_path / "hf")

    sep.start(store.get("a"))
    song = _wait(store, "a")
    assert song.karaoke_status == "done"
    assert (stems / "a" / "no_vocals.mp3").read_bytes() == b"mp3"
    assert not (stems / ".a.tmp").exists(), "临时目录应清掉"


def test_start_is_idempotent_when_stem_exists(tmp_path, monkeypatch):
    store, recordings, stems = _make(tmp_path)
    fake = FakeDemucs()
    monkeypatch.setattr(karaoke_mod.subprocess, "run", fake)
    sep = StemSeparator(store, recordings, stems, tmp_path / "hf")
    sep.start(store.get("a"))
    _wait(store, "a")

    # 第二次触发不应再跑 demucs（假 run 只被调用一次）
    calls = []
    monkeypatch.setattr(karaoke_mod.subprocess, "run",
                        lambda *a, **k: calls.append(1) or fake(*a, **k))
    sep.start(store.get("a"))
    assert calls == []


def test_start_rejects_while_running(tmp_path, monkeypatch):
    store, recordings, stems = _make(tmp_path)
    release = threading.Event()
    fake = FakeDemucs(release=release)
    monkeypatch.setattr(karaoke_mod.subprocess, "run", fake)
    sep = StemSeparator(store, recordings, stems, tmp_path / "hf")
    sep.start(store.get("a"))
    assert fake.started.wait(5)
    with pytest.raises(KaraokeError):
        sep.start(store.get("a"))
    release.set()
    _wait(store, "a")


def test_failure_records_stderr_summary(tmp_path, monkeypatch):
    store, recordings, stems = _make(tmp_path)
    monkeypatch.setattr(karaoke_mod.subprocess, "run",
                        FakeDemucs(returncode=1, stderr="RuntimeError: boom\n"))
    sep = StemSeparator(store, recordings, stems, tmp_path / "hf")
    sep.start(store.get("a"))
    song = _wait(store, "a")
    assert song.karaoke_status == "error"
    assert "boom" in song.karaoke_error


def test_missing_recording_fails_cleanly(tmp_path, monkeypatch):
    store, recordings, stems = _make(tmp_path)
    (recordings / "a.wav").unlink()
    monkeypatch.setattr(karaoke_mod.subprocess, "run", FakeDemucs())
    sep = StemSeparator(store, recordings, stems, tmp_path / "hf")
    sep.start(store.get("a"))
    song = _wait(store, "a")
    assert song.karaoke_status == "error"
    assert "录音文件丢失" in song.karaoke_error


def test_stale_running_is_reset_on_init(tmp_path, monkeypatch):
    store, recordings, stems = _make(tmp_path)
    song = store.get("a")
    song.karaoke_status = "running"
    store.update(song)
    sep = StemSeparator(store, recordings, stems, tmp_path / "hf")
    assert store.get("a").karaoke_status == "error"
    assert "中断" in store.get("a").karaoke_error


def test_delete_while_running_does_not_resurrect_song(tmp_path, monkeypatch):
    store, recordings, stems = _make(tmp_path)
    release = threading.Event()
    fake = FakeDemucs(release=release)
    monkeypatch.setattr(karaoke_mod.subprocess, "run", fake)
    sep = StemSeparator(store, recordings, stems, tmp_path / "hf")
    sep.start(store.get("a"))
    assert fake.started.wait(5)

    # 模拟删除歌曲：store 里消失 + 清 stems
    store.remove("a")
    sep.remove("a")
    release.set()
    deadline = time.time() + 5
    while time.time() < deadline and (stems / "a").exists():
        time.sleep(0.01)
    assert store.get("a") is None, "跑完不能把已删除的歌写回来"
    assert not (stems / "a").exists(), "产物应被丢弃"


def test_init_cleans_crashed_temp_dirs(tmp_path, monkeypatch):
    store, recordings, stems = _make(tmp_path)
    leftovers = stems / ".a.tmp" / "htdemucs" / "a"
    leftovers.mkdir(parents=True)
    (leftovers / "no_vocals.wav").write_bytes(b"x")
    StemSeparator(store, recordings, stems, tmp_path / "hf")
    assert not (stems / ".a.tmp").exists()


def test_stderr_tail_strips_progress_carriage_returns():
    tail = karaoke_mod._stderr_tail("  0%|     | 0/100\r 50%|██| 50/100\rError: oops\n")
    assert tail == "Error: oops"
