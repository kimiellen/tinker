// tinker 前端逻辑（M1：歌曲库 + 内录；M2：歌词导入 + 逐句学习）
"use strict";

const $ = (id) => document.getElementById(id);

const screens = { library: $("s1"), record: $("s2"), lyrics: $("s3"), learn: $("s4"), eval: $("s5") };
function show(name) {
  if (name !== "learn") stopLearning(); // 离开学习页就停声
  if (name !== "learn" && singing) stopSinging();
  if (name !== "lyrics") stopAlignPolling();
  if (name !== "eval") clearInterval(evalTimer);
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
      toggleLibraryPlay(song, item, btn);
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

function toggleLibraryPlay(song, item, btn) {
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
  $("btn-align").disabled = true;
  let phase = "align";
  $("align-status").textContent = "正在一句一句听歌……🎵 可能要等一两分钟，别走开";
  $("align-status").hidden = false;
  alignTimer = setInterval(async () => {
    const song = await (await fetch(`/api/songs/${lyricsSong.id}`)).json();
    if (phase === "align") {
      if (song.align_status === "done") {
        phase = "annotate";
        // 分句完成，自动翻译+标注；标注失败不挡路，没有翻译也能学
        const res = await fetch(`/api/songs/${song.id}/annotate`, { method: "POST" });
        if (!res.ok) { stopAlignPolling(); openLearn(song); return; }
        $("align-status").textContent = "分好句啦！正在翻译和标注发音技巧……✍️";
      } else if (song.align_status === "error") {
        stopAlignPolling();
        showLyricsError("分句失败了：" + (song.align_error || "未知原因"));
      }
    } else {
      if (song.annotate_status === "done" || song.annotate_status === "error") {
        stopAlignPolling();
        openLearn(song);
      }
    }
  }, 2000);
}

function showLyricsError(msg) {
  $("lyrics-error").textContent = "😥 " + msg;
  $("lyrics-error").hidden = false;
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
let activeLine = -1; // currentTime 实际落在的句；间奏段为 -1（播放中不高亮任何句）

// t 落在哪句返回哪句下标；句间间隙（间奏）返回 -1。lines 按 start 有序。
// 整文件连播不切片：句界即前句 end = 后句 start，高亮按时间戳归属，天然无重复
function lineAtTime(lines, t) {
  for (let i = 0; i < lines.length; i++) {
    if (t < lines[i].start) return -1;
    if (t < lines[i].end) return i;
  }
  return -1;
}

// 把一句歌词渲染成带连读/吞音高亮的 HTML（所有原文先转义，再包高亮 span）
const _normWord = (w) => w.toLowerCase().replace(/[^a-z0-9']+/g, "");
const _esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

function renderLineHtml(line) {
  const words = line.text.split(/\s+/);

  // 吞音：按词匹配（取第一个未占用的同名词），记录要划掉的字母
  const swallowAt = {};
  for (const s of line.swallow || []) {
    const i = words.findIndex((w, j) => !(j in swallowAt) && _normWord(w) === _normWord(s.word));
    if (i >= 0) swallowAt[i] = s.letters;
  }
  // 连读：相邻词对匹配，记下左词下标
  const liaisonFrom = new Set();
  for (const pair of line.liaison || []) {
    const [a, b] = pair;
    for (let i = 0; i < words.length - 1; i++) {
      if (!liaisonFrom.has(i) && _normWord(words[i]) === _normWord(a) && _normWord(words[i + 1]) === _normWord(b)) {
        liaisonFrom.add(i);
        break;
      }
    }
  }

  const wordHtml = words.map((w, i) => {
    if (!(i in swallowAt)) return _esc(w);
    const letters = swallowAt[i];
    const pos = w.toLowerCase().indexOf(letters.toLowerCase());
    if (pos < 0) return _esc(w);
    return _esc(w.slice(0, pos))
      + '<span class="swallow">' + _esc(w.slice(pos, pos + letters.length)) + "</span>"
      + _esc(w.slice(pos + letters.length));
  });

  let out = "";
  for (let i = 0; i < words.length; i++) {
    if (liaisonFrom.has(i) && i + 1 < words.length) {
      out += '<span class="liaison">' + wordHtml[i] + " " + wordHtml[i + 1] + "</span> ";
      i++; // 右词已并入连读 span
    } else {
      out += wordHtml[i] + " ";
    }
  }
  return out.trim();
}

function openLearn(song) {
  learnSong = song;
  $("learn-song-title").textContent = song.title;
  const list = $("line-list");
  list.innerHTML = "";
  lineEls = song.lines.map((line) => {
    const el = document.createElement("div");
    el.className = "line";
    const en = document.createElement("div");
    en.className = "line-en";
    const num = document.createElement("span");
    num.className = "line-num";
    num.textContent = line.index;
    en.appendChild(num);
    en.insertAdjacentHTML("beforeend", renderLineHtml(line)); // 内部已转义
    el.appendChild(en);
    if (line.zh) {
      const zh = document.createElement("div");
      zh.className = "line-zh";
      zh.textContent = line.zh; // textContent 防注入
      el.appendChild(zh);
    }
    const tips = [];
    if (line.liaison && line.liaison.length) tips.push("︶ 连读");
    if (line.swallow && line.swallow.length) tips.push("✂ 吞音 " + line.swallow.map(s => s.letters).join(" "));
    if (tips.length) {
      const tip = document.createElement("div");
      tip.className = "tip-icons";
      for (const t of tips) {
        const s = document.createElement("span");
        s.textContent = t;
        tip.appendChild(s);
      }
      el.appendChild(tip);
    }
    el.onclick = () => jumpToLine(line.index - 1);
    list.appendChild(el);
    return el;
  });
  player.src = `/api/songs/${song.id}/audio`;
  currentLine = 0;
  activeLine = -1;
  linePlaying = false;
  updateLearnUI(); // 初始化播放按钮图标
  show("learn");
}

// 跳到某句起播；之后不再干预播放头，音频自然连播，高亮靠 timeupdate 跟随。
// 连播全程零 seek——旧方案每句结尾回退 seek 接下一句，timeupdate 约 0.25s
// 延迟会把下一句开头重播一遍（用户反馈的"句首重复发音"），这里从根上消除
function jumpToLine(i) {
  const lines = learnSong.lines;
  currentLine = Math.max(0, Math.min(i, lines.length - 1));
  activeLine = currentLine;
  player.playbackRate = SPEEDS[speedIdx];
  player.currentTime = lines[currentLine].start;
  player.play();
  linePlaying = true;
  updateLearnUI();
  scrollLineIntoView();
}

// 上/下一句：seek 到目标句首。播放中继续连播；暂停中只移动高亮与进度
function stepLine(d) {
  if (!learnSong) return;
  const lines = learnSong.lines;
  currentLine = Math.max(0, Math.min(currentLine + d, lines.length - 1));
  activeLine = currentLine;
  player.currentTime = lines[currentLine].start;
  updateLearnUI();
  scrollLineIntoView();
}

// 把当前句滚到屏幕中部；开头几句居中所需滚动量为负，浏览器自动钳制为不滚，
// 已经在屏幕中段时也不滚，避免连播/点句时页面小幅抖动
function scrollLineIntoView() {
  const el = lineEls[currentLine];
  if (!el) return;
  const rect = el.getBoundingClientRect();
  const offCenter = Math.abs(rect.top + rect.height / 2 - window.innerHeight / 2);
  if (offCenter > window.innerHeight * 0.2) {
    el.scrollIntoView({ behavior: "smooth", block: "center" });
  }
}

// 播放/暂停图标（SVG，和内嵌的其他按钮图标配套）
const ICON_PLAY = '<svg viewBox="0 0 24 24"><path d="M7 4.5v15L20 12z" fill="currentColor"/></svg>';
const ICON_PAUSE = '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="4.5" width="4" height="15" rx="1.2"/><rect x="14" y="4.5" width="4" height="15" rx="1.2"/></svg>';

player.addEventListener("timeupdate", () => {
  if (!linePlaying || !learnSong) return;
  const lines = learnSong.lines;
  // 单句循环：越过句尾跳回句首——这是唯一的 seek 场景
  if (loopOne && player.currentTime >= lines[currentLine].end) {
    player.currentTime = lines[currentLine].start;
    return;
  }
  // 连播：只移动高亮，不碰播放头
  const i = lineAtTime(lines, player.currentTime);
  if (i === activeLine) return;
  activeLine = i;
  if (i >= 0) {
    currentLine = i; // 上/下一句与循环以正在唱的句为基准
    updateLearnUI();
    scrollLineIntoView();
  } else {
    updateLearnUI(); // 间奏段：不高亮任何句
  }
});

function updateLearnUI() {
  // 播放中亮 activeLine（间奏 -1 则全灭）；暂停时亮选中的 currentLine
  const hl = linePlaying ? activeLine : currentLine;
  lineEls.forEach((el, i) => el.classList.toggle("playing", i === hl));
  $("btn-play").innerHTML = linePlaying ? ICON_PAUSE : ICON_PLAY;
}

function stopLearning() {
  player.pause();
  linePlaying = false;
  if (learnSong) updateLearnUI();
}

$("btn-learn-back").onclick = () => { show("library"); loadSongs(); };

// 学习页的播放/暂停。叫 toggleLearnPlay 而不叫 togglePlay——
// 歌曲库的 toggleLibraryPlay 曾与它同名撞车，后者被覆盖失效
function toggleLearnPlay() {
  if (linePlaying) {
    player.pause();
    linePlaying = false;
    updateLearnUI();
  } else if (player.currentTime > 0 && !player.ended) {
    // 从暂停点继续连播，不跳回句首
    player.playbackRate = SPEEDS[speedIdx];
    player.play();
    linePlaying = true;
    updateLearnUI();
  } else {
    jumpToLine(currentLine); // 还没开播或已播完：从当前句起播
  }
}
function toggleLoop() {
  loopOne = !loopOne;
  $("btn-loop").classList.toggle("on", loopOne);
}
function cycleSpeed() {
  speedIdx = (speedIdx + 1) % SPEEDS.length;
  player.playbackRate = SPEEDS[speedIdx];
  $("speed-label").textContent = SPEEDS[speedIdx] + "x";
}

$("btn-play").onclick = toggleLearnPlay;
$("btn-prev").onclick = () => stepLine(-1);
$("btn-next").onclick = () => stepLine(1);
$("btn-loop").onclick = toggleLoop;
$("btn-speed").onclick = cycleSpeed;

// 学习页快捷键：空格播放/暂停，←→切句，↑单句循环，↓变速
document.addEventListener("keydown", (e) => {
  if (!screens.learn.classList.contains("active") || !learnSong) return;
  const actions = {
    " ": toggleLearnPlay,
    ArrowLeft: () => stepLine(-1),
    ArrowRight: () => stepLine(1),
    ArrowUp: toggleLoop,
    ArrowDown: cycleSpeed,
  };
  const action = actions[e.key];
  if (action) {
    e.preventDefault(); // 挡住空格触发聚焦按钮、方向键滚动页面
    action();
  }
});

// ---------- 跟唱评估 ----------
let mediaRecorder = null;
let singChunks = [];
let singing = false;
let evalSong = null;
let evalTimer = null;

$("btn-sing").onclick = () => (singing ? stopSinging() : startSinging());

async function startSinging() {
  $("sing-error").hidden = true;
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    $("sing-error").textContent = "😥 用不了麦克风……请在浏览器里允许麦克风权限";
    $("sing-error").hidden = false;
    return;
  }
  singChunks = [];
  mediaRecorder = new MediaRecorder(stream);
  mediaRecorder.ondataavailable = (e) => singChunks.push(e.data);
  mediaRecorder.start();
  singing = true;
  // 从头播放原唱，跟着唱；linePlaying 保持 false，整首播放不做句末暂停
  stopLearning();
  player.playbackRate = 1;
  player.currentTime = 0;
  player.play();
  $("btn-sing").textContent = "⏹ 唱完了";
  $("sing-hint").hidden = false;
}

player.addEventListener("ended", () => {
  if (singing) stopSinging();
  if (linePlaying) { // 连播到曲末自然结束：停在最末句，图标回到播放
    linePlaying = false;
    activeLine = -1;
    updateLearnUI();
  }
});

async function stopSinging() {
  if (!singing) return;
  singing = false;
  player.pause();
  $("btn-sing").textContent = "🎤 我唱一遍";
  $("sing-hint").hidden = true;
  const rec = mediaRecorder;
  const blob = await new Promise((resolve) => {
    rec.onstop = () => resolve(new Blob(singChunks, { type: rec.mimeType }));
    rec.stop();
  });
  rec.stream.getTracks().forEach((t) => t.stop());
  uploadAttempt(blob);
}

async function uploadAttempt(blob) {
  const fd = new FormData();
  fd.append("audio", blob, "sing.webm");
  const res = await fetch(`/api/songs/${learnSong.id}/attempts`, { method: "POST", body: fd });
  if (!res.ok) {
    $("sing-error").textContent = "😥 " + ((await res.json()).detail || "上传失败");
    $("sing-error").hidden = false;
    return;
  }
  const { attempt_id } = await res.json();
  openEval(learnSong, attempt_id);
}

function openEval(song, attemptId) {
  evalSong = song;
  $("eval-song-title").textContent = song.title + " · 整首跟唱评估";
  $("eval-summary").textContent = "正在听你唱的……🎵";
  show("eval");
  evalTimer = setInterval(async () => {
    const a = await (await fetch(`/api/attempts/${attemptId}`)).json();
    if (a.status === "done") {
      clearInterval(evalTimer);
      renderEval(a);
    } else if (a.status === "error") {
      clearInterval(evalTimer);
      $("eval-emoji").textContent = "😥";
      $("eval-summary").textContent = a.error || "评估失败了";
    }
  }, 2000);
}

const VERDICT_ICON = { good: "✅", bad: "🎯", warn: "⏱️", silent: "🔇" };

function renderEval(a) {
  const problems = a.judgments.filter((j) => j.verdict === "bad" || j.verdict === "warn").length;
  $("eval-emoji").textContent = problems === 0 ? "🌈" : problems <= 2 ? "🌟" : "💪";
  $("eval-summary").textContent = problems === 0
    ? "太棒了！每一句都唱得很好！"
    : `唱得不错！有 ${problems} 个地方可以再练练`;

  const list = $("judge-list");
  list.innerHTML = "";
  for (const j of a.judgments) {
    const card = document.createElement("div");
    card.className = "judge " + j.verdict;
    const title = document.createElement("div");
    title.className = "t";
    title.textContent = `第 ${j.index} 句「${j.text}」`;
    const detail = document.createElement("div");
    detail.className = "d";
    detail.textContent = j.detail;
    const icon = document.createElement("div");
    icon.className = "icon";
    icon.textContent = VERDICT_ICON[j.verdict] || "🎵";
    const body = document.createElement("div");
    body.appendChild(title);
    body.appendChild(detail);
    card.appendChild(icon);
    card.appendChild(body);
    card.style.cursor = "pointer";
    card.onclick = () => { show("learn"); jumpToLine(j.index - 1); }; // 点卡片回去听这句
    list.appendChild(card);
  }
  drawPitch(a.curve, evalSong.lines);
}

function drawPitch(curve, lines) {
  const cv = $("pitch-canvas");
  const ctx = cv.getContext("2d");
  const W = cv.width, H = cv.height;
  ctx.clearRect(0, 0, W, H);
  const t1 = Math.max(curve.ref_times.at(-1) || 0, evalSong.duration);
  const allMidi = [...curve.ref, ...curve.user].filter((v) => v !== null);
  if (!allMidi.length) return;
  const lo = Math.min(...allMidi) - 2, hi = Math.max(...allMidi) + 2;
  const X = (t) => (t / t1) * (W - 20) + 10;
  const Y = (m) => H - 20 - ((m - lo) / (hi - lo)) * (H - 40);

  // 问题句黄色高亮 + 句界分隔线与句号
  for (const l of lines) {
    const v = curve.line_verdicts[l.index];
    if (v === "bad" || v === "warn") {
      ctx.fillStyle = "rgba(255,197,61,.28)";
      ctx.fillRect(X(l.start), 8, X(l.end) - X(l.start), H - 26);
    }
  }
  ctx.strokeStyle = "#EFE6DA";
  ctx.fillStyle = "#B4A99E";
  ctx.font = "12px sans-serif";
  ctx.textAlign = "center";
  for (const l of lines) {
    ctx.beginPath(); ctx.moveTo(X(l.start), 8); ctx.lineTo(X(l.start), H - 18); ctx.stroke();
    ctx.fillText(l.index, X((l.start + l.end) / 2), 18);
  }

  const draw = (times, vals, color) => {
    ctx.strokeStyle = color;
    ctx.lineWidth = 2.5;
    ctx.lineCap = "round";
    ctx.beginPath();
    let pen = false;
    times.forEach((t, i) => {
      const v = vals[i];
      if (v === null || t < 0 || t > t1) { pen = false; return; } // 无声处断笔
      const x = X(t), y = Y(v);
      if (pen) ctx.lineTo(x, y); else ctx.moveTo(x, y);
      pen = true;
    });
    ctx.stroke();
  };
  draw(curve.ref_times, curve.ref, "#3BBFB5");
  draw(curve.user_times, curve.user, "#FF7F6E");
}

$("btn-sing-again").onclick = () => { show("learn"); startSinging(); };
$("btn-back-learn").onclick = () => show("learn");

loadSongs();
