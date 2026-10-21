"""tinker 后端入口：FastAPI 应用。

路由分四块：录音控制、歌曲/歌词/对齐/标注、跟唱评估、静态文件（前端 + 录音音频）。
"""
import json
import threading
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import aligner, annotate, evaluator, llm
from .audio import decode_audio_16k
from .recorder import Recorder, RecordingError
from .store import Song, SongStore

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
RECORDINGS_DIR = DATA_DIR / "recordings"
ATTEMPTS_DIR = DATA_DIR / "attempts"
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
    # 歌词变了，旧的对齐与标注结果作废
    song.lines = None
    song.align_status = "none"
    song.align_error = None
    song.annotate_status = "none"
    song.annotate_error = None
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
        # 只解码一次：转写与切句边界细化吃同一份 16k 音频
        audio = decode_audio_16k(RECORDINGS_DIR / song.audio_file)
        words = aligner.transcribe_words(audio)
        song.lines = aligner.align_lyrics(song.lyrics, words, audio)
        song.align_status = "done"
        # 重新分句后，旧的翻译标注属于旧的 lines，一并作废
        song.annotate_status = "none"
        song.annotate_error = None
    except Exception as e:  # noqa: BLE001 —— 任何失败都要落到状态里让前端能展示
        song.align_status = "error"
        song.align_error = str(e)
    store.update(song)


@app.post("/api/songs/{song_id}/annotate", status_code=202)
def start_annotate(song_id: str) -> dict:
    song = _get_song(song_id)
    if not song.lines:
        raise HTTPException(status_code=400, detail="先完成分句再标注")
    if song.annotate_status == "running":
        raise HTTPException(status_code=409, detail="正在标注中")
    if not llm.configured():
        raise HTTPException(status_code=400, detail="没有配置 LLM API key（设置环境变量 TINKER_LLM_API_KEY）")
    song.annotate_status = "running"
    song.annotate_error = None
    store.update(song)
    # LLM 调用要几十秒，与分句一样放后台线程
    threading.Thread(target=_run_annotate, args=(song_id,), daemon=True).start()
    return {"status": "running"}


def _run_annotate(song_id: str) -> None:
    song = store.get(song_id)
    try:
        annotate.annotate_lines(song.lines)
        song.annotate_status = "done"
    except Exception as e:  # noqa: BLE001 —— 同上，失败要能让前端展示
        song.annotate_status = "error"
        song.annotate_error = str(e)
    store.update(song)


# ---------- 跟唱评估 ----------

def _attempt_path(attempt_id: str) -> Path:
    if not attempt_id.isalnum():  # 挡路径穿越
        raise HTTPException(status_code=404, detail="评估记录不存在")
    path = ATTEMPTS_DIR / f"{attempt_id}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="评估记录不存在")
    return path


@app.post("/api/songs/{song_id}/attempts", status_code=202)
async def create_attempt(song_id: str, audio: UploadFile, line_index: int = Form(default=None)) -> dict:
    song = _get_song(song_id)
    if not song.lines:
        raise HTTPException(status_code=400, detail="先完成分句再评估")
    lines = song.lines
    user_origin = 0.0
    if line_index is not None:
        # 单句跟唱：只评估这一句；录音从这句开头开始，时间原点对齐到句首
        lines = [l for l in song.lines if l["index"] == line_index]
        if not lines:
            raise HTTPException(status_code=400, detail="没有这一句")
        user_origin = lines[0]["start"]
    attempt_id = uuid.uuid4().hex
    ATTEMPTS_DIR.mkdir(parents=True, exist_ok=True)
    audio_path = ATTEMPTS_DIR / f"{attempt_id}.webm"
    audio_path.write_bytes(await audio.read())
    record = {"id": attempt_id, "song_id": song_id, "status": "running",
              "created_at": datetime.now().isoformat(timespec="seconds")}
    (ATTEMPTS_DIR / f"{attempt_id}.json").write_text(
        json.dumps(record, ensure_ascii=False), encoding="utf-8")
    threading.Thread(target=_run_evaluate, args=(attempt_id, song_id, audio_path, lines, user_origin),
                     daemon=True).start()
    return {"attempt_id": attempt_id}


def _run_evaluate(attempt_id: str, song_id: str, audio_path: Path, lines: list[dict],
                  user_origin: float) -> None:
    record_path = ATTEMPTS_DIR / f"{attempt_id}.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    try:
        song = store.get(song_id)
        result = evaluator.evaluate(lines, RECORDINGS_DIR / song.audio_file, audio_path, user_origin)
        record.update(result)
        record["status"] = "done"
    except Exception as e:  # noqa: BLE001 —— 同上，失败要能让前端展示
        record["status"] = "error"
        record["error"] = str(e)
    record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")


@app.get("/api/attempts/{attempt_id}")
def get_attempt(attempt_id: str) -> dict:
    return json.loads(_attempt_path(attempt_id).read_text(encoding="utf-8"))


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
