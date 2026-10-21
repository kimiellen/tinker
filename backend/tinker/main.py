"""tinker 后端入口：FastAPI 应用。

路由分三块：录音控制、歌曲列表、静态文件（前端 + 录音音频）。
"""
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

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


@app.get("/api/songs/{song_id}/audio")
def song_audio(song_id: str) -> FileResponse:
    # 只服务曲库中登记的歌曲，避免路径参数直接映射到文件系统
    song = store.get(song_id)
    if song is None:
        raise HTTPException(status_code=404, detail="歌曲不存在")
    path = RECORDINGS_DIR / song.audio_file
    if not path.exists():
        raise HTTPException(status_code=404, detail="录音文件丢失")
    return FileResponse(path, media_type="audio/wav")


# 前端静态文件挂在最后，避免抢占 /api 路由
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
