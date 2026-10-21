"""tinker 后端入口：FastAPI 应用。

路由分三块：录音控制、歌曲/歌词/对齐、静态文件（前端 + 录音音频）。
"""
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import aligner
from .recorder import Recorder, RecordingError
from .store import Song, SongStore

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
RECORDINGS_DIR = DATA_DIR / "recordings"
FRONTEND_DIR = BASE_DIR / "frontend"

app = FastAPI(title="tinker")
recorder = Recorder(RECORDINGS_DIR)
store = SongStore(DATA_DIR / "songs.json")


class StopRequest(BaseModel):
    title: str = Field(default="", max_length=100)
    discard: bool = False


class LyricsRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20000)


@app.post("/api/recordings/start", status_code=201)
def start_recording() -> dict:
    try:
        recorder.start()
    except RecordingError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"status": "recording"}


@app.post("/api/recordings/stop")
def stop_recording(req: StopRequest):
    try:
        rec = recorder.stop()
    except RecordingError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if req.discard:
        (RECORDINGS_DIR / rec.file).unlink(missing_ok=True)
        return {"status": "discarded"}
    title = req.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="歌名不能为空")
    song = Song(id=rec.id, title=title, audio_file=rec.file, duration=rec.duration)
    store.add(song)
    return song


@app.get("/api/songs")
def list_songs() -> list[Song]:
    return store.list()


@app.get("/api/songs/{song_id}")
def song_detail(song_id: str) -> Song:
    return _get_song(song_id)


@app.put("/api/songs/{song_id}/lyrics")
def set_lyrics(song_id: str, req: LyricsRequest) -> Song:
    song = _get_song(song_id)
    song.lyrics = req.text.strip()
    # 歌词变了，旧的对齐结果作废
    song.lines = None
    song.align_status = "none"
    song.align_error = None
    store.update(song)
    return song


@app.post("/api/songs/{song_id}/align", status_code=202)
def start_align(song_id: str) -> dict:
    song = _get_song(song_id)
    if not song.lyrics:
        raise HTTPException(status_code=400, detail="先贴歌词再分句")
    if song.align_status == "running":
        raise HTTPException(status_code=409, detail="正在分句中")
    song.align_status = "running"
    song.align_error = None
    store.update(song)
    # 转写要几十秒，放后台线程跑，前端轮询歌曲详情拿结果
    threading.Thread(target=_run_align, args=(song_id,), daemon=True).start()
    return {"status": "running"}


def _run_align(song_id: str) -> None:
    song = store.get(song_id)
    try:
        words = aligner.transcribe_words(RECORDINGS_DIR / song.audio_file)
        song.lines = aligner.align_lyrics(song.lyrics, words)
        song.align_status = "done"
    except Exception as e:  # noqa: BLE001 —— 任何失败都要落到状态里让前端能展示
        song.align_status = "error"
        song.align_error = str(e)
    store.update(song)


def _get_song(song_id: str) -> Song:
    song = store.get(song_id)
    if song is None:
        raise HTTPException(status_code=404, detail="歌曲不存在")
    return song


@app.get("/api/songs/{song_id}/audio")
def song_audio(song_id: str) -> FileResponse:
    # 只服务曲库中登记的歌曲，避免路径参数直接映射到文件系统
    song = _get_song(song_id)
    path = RECORDINGS_DIR / song.audio_file
    if not path.exists():
        raise HTTPException(status_code=404, detail="录音文件丢失")
    return FileResponse(path, media_type="audio/wav")


# 前端静态文件挂在最后，避免抢占 /api 路由
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
