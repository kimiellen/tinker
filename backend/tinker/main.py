"""tinker 后端入口：FastAPI 应用。

路由分三块：录音控制、歌曲/歌词/对齐/标注、静态文件（前端 + 录音音频）。
"""
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import aligner, annotate, karaoke, markup, takes
from .audio import decode_audio_16k
from .recorder import Recorder, RecordingError
from .store import Song, SongStore

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
RECORDINGS_DIR = DATA_DIR / "recordings"
STEMS_DIR = DATA_DIR / "stems"
TAKES_DIR = DATA_DIR / "takes"
FRONTEND_DIR = BASE_DIR / "frontend"

app = FastAPI(title="tinker")
recorder = Recorder(RECORDINGS_DIR)
store = SongStore(DATA_DIR / "songs.json")
separator = karaoke.StemSeparator(store, RECORDINGS_DIR, STEMS_DIR, DATA_DIR / "hf-cache")
take_recorder = takes.TakeRecorder(store, STEMS_DIR, TAKES_DIR)


def _migrate_marked_annotations() -> None:
    """2026-10-23 spec-change：标注落盘形态从 liaison/swallow 结构改为 marked
    标记文本。迁移前不存在手动标注，旧结构直接重跑规则引擎换算，无数据可丢。"""
    for song in store.list():
        if not song.lines:
            continue
        dirty = False
        for line in song.lines:
            line.pop("liaison", None)
            line.pop("swallow", None)
            if "marked" not in line:
                annotate.annotate_line(line)
                dirty = True
        if dirty:
            store.update(song)


_migrate_marked_annotations()


class StopRequest(BaseModel):
    title: str = Field(default="", max_length=100)
    discard: bool = False


class LyricsRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20000)


class OrderRequest(BaseModel):
    ids: list[str]


class MarkupRequest(BaseModel):
    text: str = Field(max_length=2000)


class TakeStartRequest(BaseModel):
    start: float = Field(ge=0)


class TakeCancelRequest(BaseModel):
    take_id: str


class TakeStopRequest(BaseModel):
    take_id: str
    playback_started_ms: int




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


@app.delete("/api/songs/{song_id}")
def delete_song(song_id: str) -> dict:
    song = store.remove(song_id)
    if song is None:
        raise HTTPException(status_code=404, detail="歌曲不存在")
    # 彻底删除：录音 wav 与伴奏一起清掉，不留孤儿文件
    (RECORDINGS_DIR / song.audio_file).unlink(missing_ok=True)
    separator.remove(song.id)
    take_recorder.remove_all(song.id)
    return {"status": "deleted"}


@app.put("/api/songs/order")
def reorder_songs(req: OrderRequest) -> dict:
    # 必须提交歌单全量 id，防止拿着过期列表把别人刚录的歌排丢
    if not store.reorder(req.ids):
        raise HTTPException(status_code=409, detail="歌单已变化，请刷新后重试")
    return {"status": "ok"}


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


@app.post("/api/songs/{song_id}/annotate")
def annotate_song(song_id: str) -> dict:
    song = _get_song(song_id)
    if not song.lines:
        raise HTTPException(status_code=400, detail="先完成分句再标注")
    # 连读/吞音是本地规则引擎，毫秒级完成，同步执行
    annotate.annotate_lines(song.lines)
    song.annotate_status = "done"
    song.annotate_error = None
    store.update(song)
    return {"status": "done"}


# ---------- 逐句手动修改标注（edit-markup） ----------

def _get_line(song: Song, line_index: int) -> dict:
    for line in song.lines or []:
        if line["index"] == line_index:
            return line
    raise HTTPException(status_code=404, detail="没有这一句")


@app.put("/api/songs/{song_id}/lines/{line_index}/markup")
def set_line_markup(song_id: str, line_index: int, req: MarkupRequest) -> dict:
    song = _get_song(song_id)
    line = _get_line(song, line_index)
    marked = req.text.strip()
    err = markup.validate_markup(line["text"], marked)
    if err:
        raise HTTPException(status_code=400, detail=err)
    line["marked"] = marked
    store.update(song)
    return line


@app.post("/api/songs/{song_id}/lines/{line_index}/markup/reset")
def reset_line_markup(song_id: str, line_index: int) -> dict:
    # 只重算返回、不落盘：由用户在弹窗里再点保存确认，避免误触覆盖手动成果
    line = _get_line(_get_song(song_id), line_index)
    fresh = dict(line)
    annotate.annotate_line(fresh)
    return {"text": fresh["marked"]}


# ---------- 逐句手动微调句界（line-tweak） ----------

class ExpandRequest(BaseModel):
    side: str = Field(pattern="^(start|end)$")  # start=句首往前扩，end=句尾往后扩


@app.post("/api/songs/{song_id}/lines/{line_index}/expand")
def expand_line(song_id: str, line_index: int, req: ExpandRequest) -> dict:
    song = _get_song(song_id)
    if not song.lines:
        raise HTTPException(status_code=400, detail="先完成分句再微调")
    _get_line(song, line_index)  # 句不存在走统一 404
    try:
        audio = decode_audio_16k(RECORDINGS_DIR / song.audio_file)
        aligner.expand_boundary(song.lines, line_index, req.side, audio)
    except aligner.AlignError as e:
        raise HTTPException(status_code=400, detail=str(e))
    store.update(song)
    return {"lines": song.lines}


def _get_song(song_id: str) -> Song:
    song = store.get(song_id)
    if song is None:
        raise HTTPException(status_code=404, detail="歌曲不存在")
    return song


@app.post("/api/songs/{song_id}/takes/start", status_code=201)
def start_take(song_id: str, req: TakeStartRequest) -> dict:
    """开始录跟唱：等麦克风真正出第一帧再返回，前端随后起播。"""
    song = _get_song(song_id)
    try:
        return take_recorder.start(song, req.start)
    except takes.TakeError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/api/songs/{song_id}/takes/stop")
def stop_take(song_id: str, req: TakeStopRequest) -> dict:
    """停止录音并与伴奏混音。playback_started_ms 是前端记录的起播墙钟。"""
    song = _get_song(song_id)
    try:
        return take_recorder.stop(song, req.take_id, req.playback_started_ms)
    except takes.TakeError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/api/songs/{song_id}/takes/cancel")
def cancel_take(song_id: str, req: TakeCancelRequest) -> dict:
    """页面关闭/刷新时的兜底取消（sendBeacon），丢掉进行中的录音。"""
    song = _get_song(song_id)
    try:
        take_recorder.cancel(song.id, req.take_id)
    except takes.TakeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"status": "cancelled"}


@app.get("/api/songs/{song_id}/takes/{take_id}/audio")
def take_audio(song_id: str, take_id: str) -> FileResponse:
    song = _get_song(song_id)
    try:
        path = take_recorder.take_path(song, take_id)
    except takes.TakeError:
        raise HTTPException(status_code=404, detail="没有这条演唱")
    return FileResponse(path, media_type="audio/mpeg")


@app.delete("/api/songs/{song_id}/takes/{take_id}")
def delete_take(song_id: str, take_id: str) -> dict:
    song = _get_song(song_id)
    try:
        take_recorder.delete(song, take_id)
    except takes.TakeError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"status": "deleted"}


@app.get("/api/songs/{song_id}/audio")
def song_audio(song_id: str, stem: str = "original") -> FileResponse:
    # 只服务曲库中登记的歌曲，避免路径参数直接映射到文件系统
    song = _get_song(song_id)
    if stem == "instrumental":
        path = separator.stem_path(song.id)
        if not path.exists():
            raise HTTPException(status_code=404, detail="伴奏还没生成")
        return FileResponse(path, media_type="audio/mpeg")
    if stem != "original":
        raise HTTPException(status_code=404, detail="没有这种音轨")
    path = RECORDINGS_DIR / song.audio_file
    if not path.exists():
        raise HTTPException(status_code=404, detail="录音文件丢失")
    return FileResponse(path, media_type="audio/wav")


@app.post("/api/songs/{song_id}/separate", status_code=202)
def separate_song(song_id: str) -> Song:
    """触发伴奏生成。一首歌约 1 分钟，后台跑，前端轮询歌曲详情里的 karaoke_status。"""
    song = _get_song(song_id)
    try:
        return separator.start(song)
    except karaoke.KaraokeError as e:
        raise HTTPException(status_code=409, detail=str(e))


# 前端静态文件挂在最后，避免抢占 /api 路由。
# 浏览器对无 Cache-Control 的静态文件会做启发式缓存，改了前端不生效（M9 用户踩到），
# 强制 no-cache：每次都回源校验，命中 ETag 时 304，本地开销可忽略
class NoCacheStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/", NoCacheStaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
