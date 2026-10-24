"""跟唱录音（takes）的混音参数、偏移对齐与元数据测试。

真实麦克风录音与成品听感由人工验证；这里用合成正弦波验证"人声相对伴奏的位置"
确实按偏移对齐，避免参数写反了却没人发现。
"""
import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest

from tinker import takes as takes_mod
from tinker.store import Song, SongStore
from tinker.takes import (TakeError, TakeRecorder, default_take_name, migrate_takes,
                          mix_args)

SR = 44100


def _song(song_id="s1", duration=10.0, title="S"):
    return Song(id=song_id, title=title, audio_file=f"{song_id}.wav", duration=duration)


def _take(take_id, created_at, **extra):
    return {"id": take_id, "file": f"{take_id}.mp3", "start": 0, "offset_ms": 0,
            "duration": 1.0, "created_at": created_at, **extra}


def _tone(path: Path, freq: float, seconds: float):
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", f"sine=frequency={freq}:duration={seconds}:sample_rate={SR}",
         "-ac", "2", str(path)],
        check=True,
    )


def _write_raw(path: Path, samples: np.ndarray):
    data = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    stereo = np.repeat(data[:, None], 2, axis=1)
    path.write_bytes(stereo.tobytes())


def _write_wav(path: Path, samples: np.ndarray):
    data = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    stereo = np.repeat(data[:, None], 2, axis=1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(stereo.tobytes())


def _decode(path: Path) -> np.ndarray:
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", str(SR), "-"],
        capture_output=True, check=True,
    )
    return np.frombuffer(out.stdout, dtype=np.float32)


def _peak_freq(samples: np.ndarray, start_s: float, length_s: float = 0.2) -> float:
    chunk = samples[int(start_s * SR): int((start_s + length_s) * SR)]
    spectrum = np.abs(np.fft.rfft(chunk * np.hanning(len(chunk))))
    return np.fft.rfftfreq(len(chunk), 1 / SR)[np.argmax(spectrum)]


def _setup(tmp_path):
    store = SongStore(tmp_path / "songs.json")
    song = _song()
    store.add(song)
    stems, takes = tmp_path / "stems", tmp_path / "takes"
    (stems / song.id).mkdir(parents=True)
    _tone(stems / song.id / "no_vocals.mp3", 440, 6)
    rec = TakeRecorder(store, stems, takes)
    return store, song, rec, stems, takes


def test_mix_args_trim_mic_when_recording_started_early(tmp_path):
    args = mix_args(Path("stem.mp3"), Path("mic.wav"), start=12.5, offset_ms=500, out=Path("o.mp3"))
    # 伴奏从 12.5s 起；麦克风掐掉开头 0.5s；人声不需要补静音
    assert args[args.index("-ss") + 1] == "12.500"
    assert args.count("-ss") == 2
    assert args[args.index("-filter_complex") + 1].find("adelay") == -1
    assert "volume=6.0dB" in args[args.index("-filter_complex") + 1]


def test_mix_args_pad_mic_when_recording_started_late(tmp_path):
    args = mix_args(Path("stem.mp3"), Path("mic.wav"), start=0, offset_ms=-250, out=Path("o.mp3"))
    assert args.count("-ss") == 1  # 麦克风不掐头
    assert "adelay=250:all=1" in args[args.index("-filter_complex") + 1]


def test_mix_args_voice_gain_follows_backing_gain():
    # 伴奏做过响度提升时，人声也要加同样的量，否则一开唱人声就变小
    args = mix_args(Path("s.mp3"), Path("m.wav"), start=0, offset_ms=0,
                    out=Path("o.mp3"), voice_gain_db=9.3)
    assert "volume=9.3dB" in args[args.index("-filter_complex") + 1]


def test_mix_aligns_voice_by_offset(tmp_path):
    stems = tmp_path / "stems"
    stems.mkdir()
    backing, mic, out = stems / "b.wav", stems / "m.wav", stems / "out.mp3"
    _tone(backing, 440, 5)  # 伴奏：440Hz 从音乐 0s 开始
    # 麦克风比音乐早开始 0.5s（offset=500），人声在音乐 1.0s 处才出现：
    # 所以文件前 1.5s 是静音，接着 1s 的 880Hz。
    t = np.arange(int(2.5 * SR)) / SR
    voice = np.zeros_like(t)
    mask = t >= 1.5
    voice[mask] = 0.5 * np.sin(2 * np.pi * 880 * t[mask])
    _write_wav(mic, voice)

    subprocess.run(mix_args(backing, mic, start=0, offset_ms=500, out=out), check=True)
    samples = _decode(out)
    assert abs(len(samples) / SR - 2.0) < 0.15, "成品应≈麦克风长度减去前导 0.5s"
    # 人声只在 1.0s 之后出现（1.5 - 0.5 的偏移对齐），之前只有伴奏
    assert abs(_peak_freq(samples, 0.5) - 440) < 20
    assert abs(_peak_freq(samples, 1.4) - 880) < 20


def test_stop_records_take_metadata(tmp_path, monkeypatch):
    store, song, rec, stems, takes = _setup(tmp_path)
    folder = takes / song.id
    folder.mkdir(parents=True)
    raw, wav = folder / "abc123.pcm", folder / "abc123.wav"
    t = np.arange(int(2.0 * SR)) / SR
    _write_raw(raw, 0.4 * np.sin(2 * np.pi * 660 * t))
    monkeypatch.setattr(TakeRecorder, "_stop_process", staticmethod(lambda active: None))
    rec._active = takes_mod._ActiveTake("abc123", song.id, 3.0, raw, wav, None, None, 1000, 1000)
    take = rec.stop(song, "abc123", playback_started_ms=1300)
    assert take["duration"] == pytest.approx(1.7, abs=0.01)
    assert take["offset_ms"] == 300
    assert take["start"] == 3.0
    assert (folder / "abc123.mp3").exists()
    assert not raw.exists() and not wav.exists(), "中间文件用完即删"
    assert store.get(song.id).takes[0]["id"] == "abc123"


def test_stop_rejects_too_short_and_cleans_files(tmp_path, monkeypatch):
    store, song, rec, stems, takes = _setup(tmp_path)
    folder = takes / song.id
    folder.mkdir(parents=True)
    raw, wav = folder / "abc123.pcm", folder / "abc123.wav"
    _write_raw(raw, np.zeros(int(0.2 * SR)))
    monkeypatch.setattr(TakeRecorder, "_stop_process", staticmethod(lambda active: None))
    rec._active = takes_mod._ActiveTake("abc123", song.id, 0.0, raw, wav, None, None, 0, 0)
    with pytest.raises(TakeError):
        rec.stop(song, "abc123", playback_started_ms=0)
    assert not raw.exists() and not wav.exists()
    assert store.get(song.id).takes == []


def test_offset_larger_than_recording_is_clamped(tmp_path, monkeypatch):
    store, song, rec, stems, takes = _setup(tmp_path)
    folder = takes / song.id
    folder.mkdir(parents=True)
    raw, wav = folder / "abc123.pcm", folder / "abc123.wav"
    _write_raw(raw, np.zeros(SR))
    monkeypatch.setattr(TakeRecorder, "_stop_process", staticmethod(lambda active: None))
    rec._active = takes_mod._ActiveTake("abc123", song.id, 0.0, raw, wav, None, None, 0, 0)
    take = rec.stop(song, "abc123", playback_started_ms=999999)
    assert take["offset_ms"] == 800, "留 0.2s 成品，不能钳成整段静音"
    assert take["duration"] == pytest.approx(0.2, abs=0.01)


def test_start_requires_instrumental(tmp_path):
    store, song, rec, stems, takes = _setup(tmp_path)
    (stems / song.id / "no_vocals.mp3").unlink()
    with pytest.raises(TakeError, match="伴奏"):
        rec.start(song, 0.0)


def test_delete_removes_file_and_metadata(tmp_path):
    store, song, rec, stems, takes = _setup(tmp_path)
    song = store.get(song.id)
    song.takes = [{"id": "t1", "file": "t1.mp3", "start": 0, "offset_ms": 0, "duration": 1, "created_at": "x"}]
    store.update(song)
    (takes / song.id).mkdir(parents=True)
    (takes / song.id / "t1.mp3").write_bytes(b"x")
    rec.delete(song, "t1")
    assert not (takes / song.id / "t1.mp3").exists()
    assert store.get(song.id).takes == []
    with pytest.raises(TakeError):
        rec.delete(store.get(song.id), "t1")


def test_watch_capture_estimates_first_frame_wall_clock(tmp_path):
    # 麦克风唤醒慢：发起后 1.9s 才出第一帧；监测线程应按已写字节回推出这个时刻
    store, song, rec, stems, takes = _setup(tmp_path)
    raw = takes / song.id / "w.bin"
    raw.parent.mkdir(parents=True)
    rec._active = takes_mod._ActiveTake("w", song.id, 0.0, raw, raw.with_suffix(".wav"),
                                        None, None, 1000)
    raw.write_bytes(b"\0" * int(takes_mod.BYTES_PER_SEC * 3))
    rec._watch_capture(rec._active)
    started = rec._active.capture_started_ms
    assert started is not None
    # 3 秒音频回推约 3 秒前（允许 200ms 误差）
    assert abs((takes_mod._now_ms() - started) - 3000) < 200


def test_watchdog_discards_runaway_recording(tmp_path, monkeypatch):
    store, song, rec, stems, takes = _setup(tmp_path)
    raw = takes / song.id / "w.pcm"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"x" * 100)
    wav = raw.with_suffix(".wav")
    monkeypatch.setattr(TakeRecorder, "_stop_process", staticmethod(lambda active: None))
    active = takes_mod._ActiveTake("w", song.id, 0.0, raw, wav, None, None, 0, 0)
    rec._active = active
    rec._watch_overrun(active, max_seconds=0.05)

    assert rec._active is None
    assert not raw.exists()
    with pytest.raises(TakeError):
        rec.stop(song, "w", playback_started_ms=0)


def test_cancel_discards_active_recording(tmp_path, monkeypatch):
    store, song, rec, stems, takes = _setup(tmp_path)
    raw = takes / song.id / "c.pcm"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"x")
    monkeypatch.setattr(TakeRecorder, "_stop_process", staticmethod(lambda active: None))
    rec._active = takes_mod._ActiveTake("c", song.id, 0.0, raw, raw.with_suffix(".wav"), None, None, 0, 0)
    rec.cancel(song.id, "c")
    assert rec._active is None
    assert not raw.exists()
    with pytest.raises(TakeError):
        rec.cancel(song.id, "c")


def test_remove_all_clears_song_takes(tmp_path):
    store, song, rec, stems, takes = _setup(tmp_path)
    (takes / song.id).mkdir(parents=True)
    (takes / song.id / "t1.mp3").write_bytes(b"x")
    rec.remove_all(song.id)
    assert not (takes / song.id).exists()


def test_startup_cleans_intermediate_files_only(tmp_path):
    store, song, rec, stems, takes = _setup(tmp_path)
    (takes / song.id).mkdir(parents=True)
    (takes / song.id / "x.wav").write_bytes(b"x")
    (takes / song.id / "x.pcm").write_bytes(b"x")
    (takes / song.id / "keep.mp3").write_bytes(b"x")
    TakeRecorder(store, stems, takes)
    assert not (takes / song.id / "x.wav").exists()
    assert not (takes / song.id / "x.pcm").exists()
    assert (takes / song.id / "keep.mp3").exists()


# ---------- 我的唱录（takes-library） ----------

def test_default_take_name_is_song_plus_local_time():
    assert default_take_name("My Song", "2026-10-24T23:25:00") == "My Song-10月24日 23:25"
    assert default_take_name("My Song", "2026-01-05T09:07:00") == "My Song-1月5日 09:07"
    # 脏数据不能让列表打不开：退回原名或纯歌名
    assert default_take_name("My Song", "") == "My Song"
    assert default_take_name("My Song", "garbage") == "My Song-garbage"


def test_stop_names_take_and_puts_it_first(tmp_path, monkeypatch):
    store, song, rec, stems, takes = _setup(tmp_path)
    song = store.get(song.id)
    song.takes = [_take("old", "2026-10-21T10:00:00", name="旧唱录", position=5)]
    store.update(song)

    folder = takes / song.id
    folder.mkdir(parents=True)
    raw, wav = folder / "abc123.pcm", folder / "abc123.wav"
    t = np.arange(int(1.0 * SR)) / SR
    _write_raw(raw, 0.4 * np.sin(2 * np.pi * 660 * t))
    monkeypatch.setattr(TakeRecorder, "_stop_process", staticmethod(lambda active: None))
    rec._active = takes_mod._ActiveTake("abc123", song.id, 0.0, raw, wav, None, None, 0, 0)
    take = rec.stop(store.get(song.id), "abc123", playback_started_ms=0)

    assert take["name"] == default_take_name("S", take["created_at"])
    assert take["position"] == 4, "新唱录排在已有唱录前面"
    assert store.get(song.id).takes[-1]["name"] == take["name"]


def test_migrate_takes_fills_names_and_order(tmp_path):
    store = SongStore(tmp_path / "songs.json")
    store.add(Song(id="s1", title="My Song", audio_file="s1.wav", duration=10.0,
                   takes=[_take("old", "2026-10-21T10:00:00"),
                          _take("new", "2026-10-24T23:25:00")]))
    migrate_takes(store)

    takes = {t["id"]: t for t in store.get("s1").takes}
    assert takes["new"]["name"] == "My Song-10月24日 23:25"
    assert takes["old"]["name"] == "My Song-10月21日 10:00"
    assert takes["new"]["position"] == 0
    assert takes["old"]["position"] == 1
    # 幂等：第二次不再改写 songs.json
    snapshot = (tmp_path / "songs.json").read_text(encoding="utf-8")
    migrate_takes(store)
    assert (tmp_path / "songs.json").read_text(encoding="utf-8") == snapshot


def test_migrate_takes_keeps_already_ordered_takes_in_place(tmp_path):
    store = SongStore(tmp_path / "songs.json")
    store.add(Song(id="s1", title="S", audio_file="s1.wav", duration=10.0,
                   takes=[_take("b", "2026-10-20T10:00:00", name="B", position=3),
                          _take("a", "2026-10-21T10:00:00", name="A", position=7),
                          _take("new", "2026-10-24T10:00:00")]))
    migrate_takes(store)

    takes = {t["id"]: t for t in store.get("s1").takes}
    assert takes["b"]["position"] < takes["a"]["position"], "已有排序的相对顺序不变"
    assert takes["new"]["position"] > takes["a"]["position"], "没排过的旧唱录接在后面"
    assert takes["new"]["name"] == "S-10月24日 10:00"


def test_rename_persists_and_rejects_unknown_take(tmp_path):
    store, song, rec, stems, takes = _setup(tmp_path)
    song = store.get(song.id)
    song.takes = [_take("t1", "2026-10-24T10:00:00", name="旧名字", position=0)]
    store.update(song)

    rec.rename(store.get(song.id), "t1", "新名字")
    assert store.get(song.id).takes[0]["name"] == "新名字"
    with pytest.raises(TakeError):
        rec.rename(store.get(song.id), "nope", "x")


def test_list_all_and_reorder_across_songs(tmp_path):
    store = SongStore(tmp_path / "songs.json")
    store.add(Song(id="s1", title="First", audio_file="s1.wav", duration=10.0,
                   takes=[_take("a", "2026-10-20T10:00:00", name="A", position=0),
                          _take("b", "2026-10-22T10:00:00", name="B", position=2)]))
    store.add(Song(id="s2", title="Second", audio_file="s2.wav", duration=10.0,
                   takes=[_take("c", "2026-10-21T10:00:00", name="C", position=1)]))
    rec = TakeRecorder(store, tmp_path / "stems", tmp_path / "takes")

    rows = rec.list_all()
    assert [(r["song_id"], r["id"]) for r in rows] == [("s1", "a"), ("s2", "c"), ("s1", "b")]
    assert rows[1]["song_title"] == "Second"

    assert rec.reorder([("s2", "c"), ("s1", "b"), ("s1", "a")])
    assert [r["id"] for r in rec.list_all()] == ["c", "b", "a"]
    assert [(t["id"], t["position"]) for t in store.get("s1").takes] == [("a", 2), ("b", 1)]

    # 少提交/多提交/重复都算清单过期，不能部分生效
    assert not rec.reorder([("s1", "a")])
    assert not rec.reorder([("s1", "a"), ("s1", "a"), ("s2", "c")])
    assert [r["id"] for r in rec.list_all()] == ["c", "b", "a"]
