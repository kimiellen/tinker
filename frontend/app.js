// tinker 前端逻辑（M1：歌曲库 + 内录；M2：歌词导入 + 逐句学习）
"use strict";

const $ = (id) => document.getElementById(id);

const screens = { library: $("s1"), record: $("s2"), lyrics: $("s3"), learn: $("s4") };
function show(name) {
  if (name !== "learn") stopLearning(); // 离开学习页就停声
  if (name !== "lyrics") stopAlignPolling();
  for (const [key, el] of Object.entries(screens)) el.classList.toggle("active", key === name);
}

// ---------- 歌曲库 ----------
const COVERS = [
  ["🌟", "var(--yellow-soft)"],
  ["🚌", "var(--teal-soft)"],
  ["🐻", "var(--coral-soft)"],
  ["🦆", "var(--yellow-soft)"],
  ["🌈", "var(--teal-soft)"],
];

let currentAudio = null; // 同一时刻只放一首歌

async function loadSongs() {
  const res = await fetch("/api/songs");
  const songs = await res.json();
  const list = $("song-list");
  list.innerHTML = "";
  $("empty-hint").hidden = songs.length > 0;

  songs.forEach((song, i) => {
    const [emoji, bg] = COVERS[i % COVERS.length];
    const item = document.createElement("div");
    item.className = "song-item";
    item.innerHTML = `
      <div class="song-cover" style="background:${bg}">${emoji}</div>
      <div class="song-meta">
        <div class="song-name"></div>
        <div class="song-info">${songStatus(song)}</div>
      </div>
      <button class="song-play" title="播放">▶</button>`;
    item.querySelector(".song-name").textContent = song.title; // textContent 防注入

    const btn = item.querySelector(".song-play");
    btn.onclick = (e) => {
      e.stopPropagation();
      togglePlay(song, item, btn);
    };
    // 点卡片：分好句的去学习页，否则去贴歌词页
    item.onclick = () => openSong(song.id);
    list.appendChild(item);
  });
}

function songStatus(song) {
  const base = formatDuration(song.duration);
  if (song.align_status === "done" && song.lines) return `${base} · ${song.lines.length} 句，可以学啦 ✨`;
  if (song.align_status === "running") return `${base} · 正在分句……`;
  if (song.align_status === "error") return `${base} · 分句失败，点我重试`;
  if (song.lyrics) return `${base} · 已有歌词，点我开始分句`;
  return `${base} · 还没有歌词，点我贴歌词`;
}

async function openSong(songId) {
  const song = await (await fetch(`/api/songs/${songId}`)).json();
  if (song.align_status === "done" && song.lines) {
    openLearn(song);
  } else {
    openLyrics(song);
  }
}

function togglePlay(song, item, btn) {
  if (currentAudio && currentAudio.btn === btn) {
    currentAudio.audio.pause();
    return;
  }
  if (currentAudio) currentAudio.audio.pause();
  const audio = new Audio(`/api/songs/${song.id}/audio`);
  audio.onended = audio.onpause = () => {
    btn.textContent = "▶";
    item.classList.remove("playing");
    currentAudio = null;
  };
  audio.play();
  btn.textContent = "⏸";
  item.classList.add("playing");
  currentAudio = { audio, btn };
}

function formatDuration(sec) {
  const m = Math.floor(sec / 60), s = Math.round(sec % 60);
  return `${m} 分 ${s} 秒`;
}
// ---------- 录音 ----------
let recording = false;
let timer = null;
let seconds = 0;

$("btn-new").onclick = () => show("record");

$("btn-rec-toggle").onclick = async () => {
  $("rec-error").hidden = true;
  if (!recording) {
    const res = await fetch("/api/recordings/start", { method: "POST" });
    if (!res.ok) {
      showError((await res.json()).detail || "启动录音失败");
      return;
    }
    setRecordingUI(true);
  } else {
    // 先弹命名对话框，保存时才真正 stop，给孩子留出想名字的时间
    $("song-title").value = "";
    $("save-dialog").showModal();
    $("song-title").focus();
  }
};

$("btn-save-ok").onclick = async () => {
  const title = $("song-title").value.trim();
  if (!title) {
    $("song-title").focus();
    return;
  }
  $("save-dialog").close();
  const res = await fetch("/api/recordings/stop", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
  if (!res.ok) {
    setRecordingUI(false);
    showError((await res.json()).detail || "保存失败");
    return;
  }
  setRecordingUI(false);
  show("library");
  loadSongs();
};

$("btn-save-cancel").onclick = async () => {
  $("save-dialog").close();
  setRecordingUI(false);
  await fetch("/api/recordings/stop", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ discard: true }),
  });
  show("library");
};

function setRecordingUI(on) {
  recording = on;
  $("rec-dot").classList.toggle("recording", on);
  $("rec-wave").hidden = !on;
  $("btn-rec-toggle").textContent = on ? "⏹ 完成录音" : "⏺ 开始录音";
  $("btn-rec-toggle").classList.toggle("btn-coral", !on);
  $("btn-rec-toggle").classList.toggle("btn-teal", on);
  $("rec-status").innerHTML = on
    ? "正在录电脑播放的声音……🔊 请保持歌曲播放"
    : "准备好了就开始";

  clearInterval(timer);
  if (on) {
    seconds = 0;
    tick();
    timer = setInterval(() => { seconds++; tick(); }, 1000);
  } else {
    $("rec-time").textContent = "00:00";
  }
}

function tick() {
  const m = String(Math.floor(seconds / 60)).padStart(2, "0");
  const s = String(seconds % 60).padStart(2, "0");
  $("rec-time").textContent = `${m}:${s}`;
}

function showError(msg) {
  $("rec-error").textContent = "😥 " + msg;
  $("rec-error").hidden = false;
}

// ---------- 贴歌词 + 分句 ----------
let lyricsSong = null;
let alignTimer = null;

function openLyrics(song) {
  lyricsSong = song;
  $("lyrics-song-title").textContent = song.title;
  $("lyrics-text").value = song.lyrics || "";
  $("lyrics-error").hidden = true;
  show("lyrics");
  if (song.align_status === "running") startAlignPolling();
}

$("btn-lyrics-back").onclick = () => show("library");

$("btn-align").onclick = async () => {
  const text = $("lyrics-text").value.trim();
  $("lyrics-error").hidden = true;
  if (!text) {
    $("lyrics-error").textContent = "😥 歌词还是空的，先贴歌词哦";
    $("lyrics-error").hidden = false;
    return;
  }
  const put = await fetch(`/api/songs/${lyricsSong.id}/lyrics`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  if (!put.ok) {
    $("lyrics-error").textContent = "😥 " + ((await put.json()).detail || "保存歌词失败");
    $("lyrics-error").hidden = false;
    return;
  }
  const res = await fetch(`/api/songs/${lyricsSong.id}/align`, { method: "POST" });
  if (!res.ok && res.status !== 409) {
    $("lyrics-error").textContent = "😥 " + ((await res.json()).detail || "启动分句失败");
    $("lyrics-error").hidden = false;
    return;
  }
  startAlignPolling();
};

function startAlignPolling() {
  stopAlignPolling();
  $("align-status").hidden = false;
  $("btn-align").disabled = true;
  alignTimer = setInterval(async () => {
    const song = await (await fetch(`/api/songs/${lyricsSong.id}`)).json();
    if (song.align_status === "done") {
      stopAlignPolling();
      openLearn(song);
    } else if (song.align_status === "error") {
      stopAlignPolling();
      $("lyrics-error").textContent = "😥 分句失败了：" + (song.align_error || "未知原因");
      $("lyrics-error").hidden = false;
    }
  }, 2000);
}

function stopAlignPolling() {
  clearInterval(alignTimer);
  alignTimer = null;
  $("align-status").hidden = true;
  $("btn-align").disabled = false;
}

// ---------- 逐句学习 ----------
const SPEEDS = [1, 0.75, 0.5];
const player = new Audio();
let learnSong = null;
let lineEls = [];
let currentLine = 0;
let loopOne = false;
let speedIdx = 0;
let linePlaying = false;

function openLearn(song) {
  learnSong = song;
  $("learn-song-title").textContent = song.title;
  const list = $("line-list");
  list.innerHTML = "";
  lineEls = song.lines.map((line) => {
    const el = document.createElement("div");
    el.className = "line";
    el.innerHTML = `<div class="line-en"><span class="line-num">${line.index}</span></div>`;
    el.querySelector(".line-en").appendChild(document.createTextNode(line.text));
    el.onclick = () => playLine(line.index - 1);
    list.appendChild(el);
    return el;
  });
  player.src = `/api/songs/${song.id}/audio`;
  currentLine = 0;
  linePlaying = false;
  show("learn");
}

function playLine(i) {
  const lines = learnSong.lines;
  currentLine = Math.max(0, Math.min(i, lines.length - 1));
  player.playbackRate = SPEEDS[speedIdx];
  player.currentTime = lines[currentLine].start;
  player.play();
  linePlaying = true;
  updateLearnUI();
}

player.addEventListener("timeupdate", () => {
  if (!linePlaying || !learnSong) return;
  const line = learnSong.lines[currentLine];
  if (player.currentTime >= line.end) {
    if (loopOne) {
      player.currentTime = line.start; // 回到句首，实现单句循环
    } else {
      player.pause();
      linePlaying = false;
      updateLearnUI();
    }
  }
});

function updateLearnUI() {
  lineEls.forEach((el, i) => el.classList.toggle("playing", i === currentLine && linePlaying));
  $("btn-play").textContent = linePlaying ? "⏸" : "▶";
}

function stopLearning() {
  player.pause();
  linePlaying = false;
  if (learnSong) updateLearnUI();
}

$("btn-learn-back").onclick = () => { show("library"); loadSongs(); };

$("btn-play").onclick = () => {
  if (linePlaying) {
    player.pause();
    linePlaying = false;
    updateLearnUI();
  } else {
    playLine(currentLine);
  }
};
$("btn-prev").onclick = () => playLine(currentLine - 1);
$("btn-next").onclick = () => playLine(currentLine + 1);
$("btn-loop").onclick = () => {
  loopOne = !loopOne;
  $("btn-loop").classList.toggle("on", loopOne);
};
$("btn-speed").onclick = () => {
  speedIdx = (speedIdx + 1) % SPEEDS.length;
  player.playbackRate = SPEEDS[speedIdx];
  $("speed-label").textContent = SPEEDS[speedIdx] + "x";
};

loadSongs();
