"""伴奏分离（karaoke）的后台任务与后处理测试。

真实 demucs_ft 一首歌要跑 6 分钟，单测里用假管线顶替，只验证状态流转、进度、
版本迁移与后处理参数；真实分离质量由人工验证（见 docs/specs/karaoke/tasks.md）。
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


class FakePipeline:
    """顶替 demucs + 后处理：按真实输出布局造 flac，再“编码”出 mp3。"""

    def __init__(self, release=None, fail=None, gain=3.3):
        self.release = release
        self.fail = fail
        self.gain = gain
        self.started = threading.Event()
        self.runs = 0

    def run_demucs(self, source, tmp_dir, on_progress):
        self.runs += 1
        if self.release is not None:
            on_progress(40)
            self.started.set()
            assert self.release.wait(5), "测试没释放假分离"
        if self.fail:
            raise KaraokeError(self.fail)
        produced = tmp_dir / karaoke_mod.MODEL / source.stem / "no_vocals.flac"
        produced.parent.mkdir(parents=True, exist_ok=True)
        produced.write_bytes(b"flac")
        on_progress(60)
        return produced

    def postprocess(self, source_audio, stem_flac, out_mp3):
        out_mp3.write_bytes(b"mp3")
        return self.gain

def _setup(tmp_path, monkeypatch, pipeline):
    store, recordings, stems = _make(tmp_path)
    sep = StemSeparator(store, recordings, stems, tmp_path / "hf")
    monkeypatch.setattr(StemSeparator, "_run_demucs",
                        lambda self, source, tmp, cb: pipeline.run_demucs(source, tmp, cb))
    monkeypatch.setattr(StemSeparator, "_postprocess",
                        lambda self, src, stem, out: pipeline.postprocess(src, stem, out))
    return store, recordings, stems, sep


def _wait(store, song_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        song = store.get(song_id)
        if song and song.karaoke_status != "running":
            return song
        time.sleep(0.01)
    raise AssertionError("分离任务没有在超时内结束")


def test_start_produces_current_version_with_gain(tmp_path, monkeypatch):
    store, recordings, stems, sep = _setup(tmp_path, monkeypatch, FakePipeline())
    sep.start(store.get("a"))
    song = _wait(store, "a")
    assert song.karaoke_status == "done"
    assert song.karaoke_progress == 100
    assert song.karaoke_version == karaoke_mod.STEM_VERSION
    assert song.karaoke_gain_db == 3.3
    assert (stems / "a" / "no_vocals.mp3").read_bytes() == b"mp3"
    assert not (stems / ".a.tmp").exists(), "临时目录应清掉"


def test_progress_is_reported_while_running(tmp_path, monkeypatch):
    release = threading.Event()
    pipeline = FakePipeline(release=release)
    store, recordings, stems, sep = _setup(tmp_path, monkeypatch, pipeline)
    sep.start(store.get("a"))
    assert pipeline.started.wait(5)
    # 假管线在等待前报过 40%
    deadline = time.time() + 2
    while store.get("a").karaoke_progress != 40 and time.time() < deadline:
        time.sleep(0.01)
    assert store.get("a").karaoke_progress == 40
    release.set()
    _wait(store, "a")


def test_start_is_idempotent_when_current_stem_exists(tmp_path, monkeypatch):
    pipeline = FakePipeline()
    store, recordings, stems, sep = _setup(tmp_path, monkeypatch, pipeline)
    sep.start(store.get("a"))
    _wait(store, "a")
    runs = pipeline.runs
    sep.start(store.get("a"))
    assert pipeline.runs == runs, "已有当前版本成品时不应再跑分离"
    assert store.get("a").karaoke_status == "done"


def test_start_rejects_while_running(tmp_path, monkeypatch):
    release = threading.Event()
    pipeline = FakePipeline(release=release)
    store, recordings, stems, sep = _setup(tmp_path, monkeypatch, pipeline)
    sep.start(store.get("a"))
    assert pipeline.started.wait(5)
    with pytest.raises(KaraokeError):
        sep.start(store.get("a"))
    release.set()
    _wait(store, "a")


def test_failure_records_message(tmp_path, monkeypatch):
    store, recordings, stems, sep = _setup(tmp_path, monkeypatch, FakePipeline(fail="RuntimeError: boom"))
    sep.start(store.get("a"))
    song = _wait(store, "a")
    assert song.karaoke_status == "error"
    assert "boom" in song.karaoke_error
    assert song.karaoke_progress == 0


def test_missing_recording_fails_cleanly(tmp_path, monkeypatch):
    store, recordings, stems, sep = _setup(tmp_path, monkeypatch, FakePipeline())
    (recordings / "a.wav").unlink()
    sep.start(store.get("a"))
    song = _wait(store, "a")
    assert song.karaoke_status == "error"
    assert "录音文件丢失" in song.karaoke_error


def test_stale_running_is_reset_on_init(tmp_path, monkeypatch):
    store, recordings, stems = _make(tmp_path)
    song = store.get("a")
    song.karaoke_status = "running"
    song.karaoke_progress = 55
    store.update(song)
    StemSeparator(store, recordings, stems, tmp_path / "hf")
    assert store.get("a").karaoke_status == "error"
    assert store.get("a").karaoke_progress == 0
    assert "中断" in store.get("a").karaoke_error


def test_old_version_stem_is_invalidated_on_init(tmp_path):
    store, recordings, stems = _make(tmp_path)
    song = store.get("a")
    song.karaoke_status = "done"
    song.karaoke_version = karaoke_mod.STEM_VERSION - 1
    store.update(song)
    old = stems / "a" / "no_vocals.mp3"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"old")
    StemSeparator(store, recordings, stems, tmp_path / "hf")
    assert store.get("a").karaoke_status == "none"
    assert not old.exists(), "旧版成品应清掉，避免被当成当前版本"


def test_delete_while_running_does_not_resurrect_song(tmp_path, monkeypatch):
    release = threading.Event()
    pipeline = FakePipeline(release=release)
    store, recordings, stems, sep = _setup(tmp_path, monkeypatch, pipeline)
    sep.start(store.get("a"))
    assert pipeline.started.wait(5)

    store.remove("a")
    sep.remove("a")
    release.set()
    deadline = time.time() + 5
    while time.time() < deadline and (stems / "a").exists():
        time.sleep(0.01)
    assert store.get("a") is None, "跑完不能把已删除的歌写回来"
    assert not (stems / "a").exists(), "产物应被丢弃"


def test_init_cleans_crashed_temp_dirs(tmp_path):
    store, recordings, stems = _make(tmp_path)
    leftovers = stems / ".a.tmp" / "htdemucs_ft" / "a"
    leftovers.mkdir(parents=True)
    (leftovers / "no_vocals.flac").write_bytes(b"x")
    StemSeparator(store, recordings, stems, tmp_path / "hf")
    assert not (stems / ".a.tmp").exists()


def test_stderr_tail_strips_progress_carriage_returns():
    tail = karaoke_mod._stderr_tail("  0%|     | 0/100\r 50%|██| 50/100\rError: oops\n")
    assert tail == "Error: oops"


def test_postprocess_gain_is_clamped_and_eq_applied(tmp_path, monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        Path(args[-1]).write_bytes(b"mp3")
        return karaoke_mod.subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(karaoke_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(karaoke_mod, "_lufs", lambda p: {"src": -10.0, "stem": -30.0}.get(Path(p).stem))
    sep = StemSeparator(SongStore(tmp_path / "s.json"), tmp_path, tmp_path / "stems", tmp_path / "hf")
    gain = sep._postprocess(Path("src"), Path("stem.flac"), tmp_path / "o.mp3")
    assert gain == karaoke_mod.GAIN_MAX_DB, "20dB 差值应被钳到上限"
    af = captured["args"][captured["args"].index("-af") + 1]
    assert af.startswith(karaoke_mod.EQ_FILTER)
    assert "volume=12.00dB" in af
    assert captured["args"][captured["args"].index("-b:a") + 1] == "320k"


def test_postprocess_without_loudness_falls_back_to_zero_gain(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(karaoke_mod.subprocess, "run",
                        lambda args, **k: captured.setdefault("args", args) or Path(args[-1]).write_bytes(b"x"))
    monkeypatch.setattr(karaoke_mod, "_lufs", lambda p: None)
    sep = StemSeparator(SongStore(tmp_path / "s.json"), tmp_path, tmp_path / "stems", tmp_path / "hf")
    assert sep._postprocess(Path("src"), Path("stem.flac"), tmp_path / "o.mp3") == 0.0
    assert "volume=0.00dB" in captured["args"][captured["args"].index("-af") + 1]


def test_start_safely_calls_start_and_swallows_failure(tmp_path, caplog):
    song = Song(id="a", title="A", audio_file="a.wav", duration=1.0)
    calls = []

    class Fake:
        def start(self, s):
            calls.append(s.id)

    karaoke_mod.start_safely(Fake(), song)
    assert calls == ["a"]

    class Boom:
        def start(self, s):
            raise RuntimeError("boom")

    karaoke_mod.start_safely(Boom(), song)  # 不能把异常抛回保存接口
    assert "自动生成伴奏失败" in caplog.text
    assert "boom" in caplog.text
