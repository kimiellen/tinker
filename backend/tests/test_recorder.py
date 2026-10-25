"""内录首尾静音裁切：边界判定与真实 wav 裁切的回归测试。

真实录音（parec 的 monitor 源）静音段是数字零，这里用合成音频覆盖首尾边界、
短促点击、安静录音、整段静音和本来没有多余空白等边界情况。
"""
import os
import subprocess
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from tinker import recorder as recorder_mod
from tinker.audio import SAMPLE_RATE as DECODE_RATE
from tinker.recorder import (HEAD_MARGIN_S, TAIL_MARGIN_S, RecordingError, Recorder,
                             find_sound_bounds, trim_silence, wav_duration)

SR = 44100


def _tone(seconds: float, freq: float = 440.0, amp: float = 0.3,
          sr: int = SR) -> np.ndarray:
    t = np.arange(int(seconds * sr)) / sr
    return amp * np.sin(2 * np.pi * freq * t)


def _cat(*parts: np.ndarray) -> np.ndarray:
    return np.concatenate([p.astype(np.float32) for p in parts])


def _write_wav(path: Path, samples: np.ndarray) -> None:
    data = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    stereo = np.repeat(data[:, None], 2, axis=1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(stereo.tobytes())


def _decode(path: Path) -> np.ndarray:
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le",
         "-ac", "1", "-ar", str(DECODE_RATE), "-"],
        capture_output=True, check=True,
    )
    return np.frombuffer(out.stdout, dtype=np.float32)


def _pattern(head: float, sound: float, tail: float, amp: float = 0.3,
             sr: int = SR) -> np.ndarray:
    return _cat(np.zeros(int(head * sr), np.float32), _tone(sound, amp=amp, sr=sr),
                np.zeros(int(tail * sr), np.float32))


def test_find_sound_bounds_locates_first_and_last_sound():
    audio = _pattern(1.0, 2.0, 3.0, sr=DECODE_RATE)
    start, end = find_sound_bounds(audio)
    assert abs(start - 1.0) < 0.05
    assert abs(end - 3.0) < 0.05


def test_find_sound_bounds_returns_none_for_digital_silence():
    assert find_sound_bounds(np.zeros(DECODE_RATE * 2, np.float32)) is None


def test_find_sound_bounds_ignores_short_click_before_music():
    # 起播前的一次短点击声不能当作歌曲起点，否则中间那段空白会被保留
    click = np.zeros(int(0.01 * DECODE_RATE), np.float32)
    click[:] = 0.5
    audio = _cat(np.zeros(int(0.5 * DECODE_RATE), np.float32), click,
                 np.zeros(int(1.0 * DECODE_RATE), np.float32),
                 _tone(1.0, sr=DECODE_RATE))
    start, _ = find_sound_bounds(audio)
    assert abs(start - 1.5) < 0.05


def test_find_sound_bounds_ignores_short_click_after_music():
    # 停播后的一次短点击声不能当成结尾，否则中间的空隙会全留下
    click = np.zeros(int(0.01 * DECODE_RATE), np.float32)
    click[:] = 0.5
    audio = _cat(_tone(1.0, sr=DECODE_RATE),
                 np.zeros(int(2.0 * DECODE_RATE), np.float32), click)
    _, end = find_sound_bounds(audio)
    assert abs(end - 1.0) < 0.05


def test_find_sound_bounds_detects_quiet_recording():
    # 系统音量很小（峰值约 -40dB）时仍要能判定有声
    audio = _pattern(1.0, 1.5, 1.0, amp=0.01, sr=DECODE_RATE)
    start, end = find_sound_bounds(audio)
    assert abs(start - 1.0) < 0.05
    assert abs(end - 2.5) < 0.05


def test_trim_silence_keeps_margins_and_actual_duration(tmp_path):
    path = tmp_path / "rec.wav"
    _write_wav(path, _pattern(1.0, 2.0, 3.0))

    duration = trim_silence(path)

    expected = 2.0 + HEAD_MARGIN_S + TAIL_MARGIN_S
    assert abs(duration - expected) < 0.08
    assert abs(wav_duration(path) - expected) < 0.08

    samples = _decode(path)
    # 开头留 0.2s 余量：前 0.15s 仍然安静，0.2s 后进入测试音
    assert np.abs(samples[: int(0.15 * DECODE_RATE)]).max() < 1e-3
    assert np.abs(samples[int(0.25 * DECODE_RATE): int(0.4 * DECODE_RATE)]).max() > 0.1
    # 结尾留 0.5s 余量：最后 0.45s 安静
    assert np.abs(samples[-int(0.45 * DECODE_RATE):]).max() < 1e-3


def test_trim_silence_rejects_recording_without_sound(tmp_path):
    path = tmp_path / "silent.wav"
    _write_wav(path, np.zeros(SR * 2, np.float32))
    with pytest.raises(RecordingError, match="没有录到声音"):
        trim_silence(path)


def test_trim_silence_leaves_clean_recording_untouched(tmp_path):
    path = tmp_path / "clean.wav"
    _write_wav(path, _tone(2.0))
    before = path.read_bytes()

    duration = trim_silence(path)

    assert abs(duration - 2.0) < 0.01
    assert path.read_bytes() == before  # 没有空白可裁时不做无谓的重编码


def test_wav_duration_falls_back_when_header_is_not_finalized(tmp_path):
    # 采集管道被强杀时 data size 是 0xFFFFFFFF 占位，不能把 2s 报成 6 小时
    path = tmp_path / "streaming.wav"
    _write_wav(path, _tone(2.0))
    data = bytearray(path.read_bytes())
    pos = data.find(b"data")
    data[pos + 4:pos + 8] = b"\xff\xff\xff\xff"
    path.write_bytes(bytes(data))
    assert abs(wav_duration(path) - 2.0) < 0.01


# ---------- 采集进程生命周期（2026-10-25 孤儿进程泄漏修复） ----------

def _sleep_group() -> subprocess.Popen:
    # 用独立进程组模拟 parec|ffmpeg：测试只关心有没有被杀掉
    return subprocess.Popen(["sleep", "30"], start_new_session=True)


def test_looks_like_recorder_only_matches_this_app(tmp_path):
    wav = tmp_path / "deadbeef.wav"
    bash = f'bash\x00-c\x00parec --device=@DEFAULT_MONITOR@ | ffmpeg -f s16le -i - "{wav}"\x00'.encode()
    ffmpeg = f"ffmpeg\x00-f\x00s16le\x00-i\x00-\x00-y\x00{wav}\x00".encode()
    assert recorder_mod._looks_like_recorder(bash, tmp_path)
    assert recorder_mod._looks_like_recorder(ffmpeg, tmp_path)
    # 别的 app 的 monitor 采集，或正常的裁切 ffmpeg，都不能误杀
    assert not recorder_mod._looks_like_recorder(b"parec\x00--device=@DEFAULT_MONITOR@\x00", tmp_path)
    trim = f"ffmpeg\x00-v\x00error\x00-i\x00{wav}\x00-c:a\x00pcm_s16le\x00".encode()
    assert not recorder_mod._looks_like_recorder(trim, tmp_path)
    assert not recorder_mod._looks_like_recorder(bash, tmp_path / "other")


def test_recorded_wav_from_bash_and_ffmpeg_cmdlines(tmp_path):
    wav = tmp_path / "deadbeef.wav"
    bash = f'bash\x00-c\x00parec --device=@DEFAULT_MONITOR@ | ffmpeg -f s16le -i - "{wav}"\x00'.encode()
    ffmpeg = f"ffmpeg\x00-f\x00s16le\x00-i\x00-\x00-y\x00{wav}\x00".encode()
    assert recorder_mod._recorded_wav(bash, tmp_path) == wav
    assert recorder_mod._recorded_wav(ffmpeg, tmp_path) == wav
    assert recorder_mod._recorded_wav(ffmpeg, tmp_path / "other") is None


def test_find_orphan_groups_scans_proc(tmp_path):
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    wav = recordings / "deadbeef.wav"
    fake_proc = tmp_path / "proc"
    pid_dir = fake_proc / str(os.getpid())
    pid_dir.mkdir(parents=True)
    cmd = f'bash\x00-c\x00parec --device=@DEFAULT_MONITOR@ | ffmpeg -f s16le -i - "{wav}"\x00'.encode()
    (pid_dir / "cmdline").write_bytes(cmd)

    groups = recorder_mod._find_orphan_groups(recordings, fake_proc)

    pgid = os.getpgid(os.getpid())
    assert groups == {pgid: {wav}}


def test_reap_orphans_kills_leftover_group_and_deletes_file(tmp_path, monkeypatch):
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    wav = recordings / "deadbeef.wav"
    wav.write_bytes(b"x" * 2048)
    proc = _sleep_group()
    try:
        pgid = os.getpgid(proc.pid)
        monkeypatch.setattr(
            recorder_mod, "_find_orphan_groups",
            lambda directory: {pgid: {wav}},
        )
        rec = Recorder(recordings)
        rec.reap_orphans()

        assert not wav.exists()
        proc.wait(timeout=3)
        assert proc.returncode is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_shutdown_kills_active_recording_and_removes_partial(tmp_path):
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    wav = recordings / "deadbeef.wav"
    wav.write_bytes(b"x" * 2048)
    proc = _sleep_group()
    try:
        rec = Recorder(recordings)
        rec._proc = proc
        rec._current = wav
        rec._token = 1

        rec.shutdown()

        assert not rec.is_recording
        assert not wav.exists()
        proc.wait(timeout=3)
        assert proc.returncode is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_watchdog_aborts_timeout_and_stop_reports_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(recorder_mod, "MAX_RECORDING_S", 10.0)
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    wav = recordings / "deadbeef.wav"
    wav.write_bytes(b"x" * 2048)
    proc = _sleep_group()
    try:
        rec = Recorder(recordings)
        rec._proc = proc
        rec._current = wav
        rec._token = 1

        rec._watchdog(proc, wav, 1, time.monotonic() - 60)

        assert not rec.is_recording
        assert not wav.exists()
        proc.wait(timeout=3)
        with pytest.raises(RecordingError, match="20 分钟"):
            rec.stop()
        with pytest.raises(RecordingError, match="当前没有"):
            rec.stop()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
