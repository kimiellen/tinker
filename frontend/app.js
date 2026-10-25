// tinker 前端逻辑（M1：歌曲库 + 内录；M2：歌词导入 + 逐句学习）
"use strict";

const $ = (id) => document.getElementById(id);

const screens = {
  library: $("s1"), record: $("s2"), lyrics: $("s3"), learn: $("s4"), takes: $("s5"),
};
function show(name) {
  if (name !== "learn") {
    stopLearning();      // 离开学习页就停声
    stopKaraokePolling(); // 伴奏生成交给后端继续跑，前端不用再轮询
    abortTake();          // 录制中离开：这条作废，不留在列表里
    stopTakePlayback();
  }
  if (name !== "takes") stopLibraryPlayback(); // 离开唱录页就停回放
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

let deletingSong = null; // 待二次确认删除的歌

async function loadSongs() {
  const res = await fetch("/api/songs");
  const songs = await res.json();
  const list = $("song-list");
  list.innerHTML = "";
  $("empty-hint").hidden = songs.length > 0;
  // 唱录总数直接来自歌曲详情里的 takes，不用额外请求；0 时隐藏徽标
  const takeCount = songs.reduce((n, song) => n + (song.takes || []).length, 0);
  $("takes-count").textContent = takeCount;
  $("takes-count").hidden = takeCount === 0;

  songs.forEach((song, i) => {
    const [emoji, bg] = COVERS[i % COVERS.length];
    const item = document.createElement("div");
    item.className = "song-item";
    item.draggable = true;
    item.dataset.songId = song.id;
    item.innerHTML = `
      <div class="song-cover" style="background:${bg}">${emoji}</div>
      <div class="song-meta">
        <div class="song-name"></div>
        <div class="song-info">${songStatus(song)}</div>
      </div>
      <button class="song-del" title="删除">🗑</button>`;
    item.querySelector(".song-name").textContent = song.title; // textContent 防注入

    item.querySelector(".song-del").onclick = (e) => {
      e.stopPropagation(); // 不触发卡片的学习页跳转
      deletingSong = song;
      $("delete-song-title").textContent = song.title;
      $("delete-dialog").showModal();
    };
    // 点卡片：分好句的去学习页，否则去贴歌词页（拖拽后 mouseup 不触发 click，互不冲突）
    item.onclick = () => openSong(song.id);
    list.appendChild(item);
  });
}

$("btn-delete-cancel").onclick = () => $("delete-dialog").close();

$("btn-delete-ok").onclick = async () => {
  $("delete-dialog").close();
  const res = await fetch(`/api/songs/${deletingSong.id}`, { method: "DELETE" });
  if (res.ok) {
    // 就地移除 DOM，不整页重渲染，避免闪烁
    const item = document.querySelector(`.song-item[data-song-id="${deletingSong.id}"]`);
    if (item) item.remove();
    if (!$("song-list").children.length) $("empty-hint").hidden = false;
  }
  deletingSong = null;
};

// ---------- 歌单拖动排序 ----------
// HTML5 拖拽：dragover 中实时移动 DOM 给出落点反馈，drop 后把新顺序整体提交后端
const songList = $("song-list");
let dragItem = null;

songList.addEventListener("dragstart", (e) => {
  dragItem = e.target.closest(".song-item");
  if (dragItem) dragItem.classList.add("dragging");
});

songList.addEventListener("dragover", (e) => {
  e.preventDefault(); // 必须拦默认行为才允许 drop
  if (!dragItem) return;
  const after = [...songList.querySelectorAll(".song-item:not(.dragging)")]
    .find((el) => e.clientY < el.getBoundingClientRect().top + el.offsetHeight / 2);
  if (after) songList.insertBefore(dragItem, after);
  else songList.appendChild(dragItem);
});

songList.addEventListener("drop", (e) => e.preventDefault());

songList.addEventListener("dragend", async () => {
  if (!dragItem) return;
  dragItem.classList.remove("dragging");
  dragItem = null;
  const ids = [...songList.querySelectorAll(".song-item")].map((el) => el.dataset.songId);
  const res = await fetch("/api/songs/order", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ids }),
  });
  if (!res.ok) loadSongs(); // 列表已变化（409 等）：拉最新数据恢复一致
});

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

function formatDuration(sec) {
  const m = Math.floor(sec / 60), s = Math.round(sec % 60);
  return `${m} 分 ${s} 秒`;
}
// ---------- 录音 ----------
let recording = false;
let timer = null;
let seconds = 0;

$("btn-new").onclick = () => show("record");
$("btn-takes").onclick = () => openTakeLibrary();

// 录到一半就返回：丢弃这段录音，不把半截文件留在后端（与保存对话框的「不要了」一致）
$("btn-rec-back").onclick = async () => {
  if (recording) {
    setRecordingUI(false);
    await fetch("/api/recordings/stop", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ discard: true }),
    });
  }
  show("library");
};

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
  $("align-status").textContent = "正在一句一句听歌……🎵 可能要等一两分钟，别走开";
  $("align-status").hidden = false;
  alignTimer = setInterval(async () => {
    const song = await (await fetch(`/api/songs/${lyricsSong.id}`)).json();
    if (song.align_status === "done") {
      stopAlignPolling();
      // 分句完成，同步跑发音标注（本地规则引擎，毫秒级）；失败不挡路，没有标注也能学
      await fetch(`/api/songs/${song.id}/annotate`, { method: "POST" });
      openLearn(await (await fetch(`/api/songs/${song.id}`)).json());
    } else if (song.align_status === "error") {
      stopAlignPolling();
      showLyricsError("分句失败了：" + (song.align_error || "未知原因"));
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

// ---------- 原唱 / 伴奏（karaoke） ----------
// 伴奏由后端 demucs 预生成（约 1 分钟），前端只负责：没生成时先播原唱并等，
// 生成好后切源。demucs 输出与源等长且时间对齐，所以切源按 currentTime 直接还原
let currentStem = "original";   // original | instrumental
let karaokeStatus = "none";     // 后端 song.karaoke_status：none|running|done|error
let wantInstrumental = false;   // 用户点了伴奏但还在等生成
let karaokeTimer = null;
let stemSwitchToken = 0;        // 切源代次：音源元数据回调晚到时，丢弃过期的恢复动作

// ---------- 跟唱录音（karaoke M3） ----------
// 后端录麦克风，停止时按"起播墙钟 - 起录墙钟"的偏移与伴奏混成一段 mp3；
// 录音期间锁住播放条与歌词点击，避免 seek/暂停/变速把音乐时间轴弄乱
const takePlayer = new Audio();
let takeRec = null;        // {id, start, prevSpeedIdx}
let takeTimer = null;
let takePreview = null;    // 回放弹窗里的成品（丢弃时要删）
let playingTakeId = null;  // "我的演唱"列表里正在回放的那条
let startingTake = false;  // 正在等麦克风唤醒，防止连点起两条录音
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

// 把一句歌词渲染成带连读/吞音高亮的 HTML。
// 数据形态（2026-10-23 spec-change）：每句只存 marked 标记文本——两词间的 -
// 是连读（But-I），^ 包住的字母是吞音（wan^t^）；渲染时现场解析，原文先转义
const _esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
// 吞音：转义后的片段里把 ^...^ 包成划线灰字（^ 不属于转义字符，配对在转义后不变）
const _renderWord = (w) => _esc(w).replace(/\^([^^]*)\^/g, '<span class="swallow">$1</span>');

// 连读单元按 - 拆成非空词片段；空片段（孤立或连续的 -）丢弃
const _splitTok = (tok) => tok.split("-").filter((w) => w !== "");

function renderLineHtml(line) {
  const marked = line.marked || line.text;
  return marked.split(/\s+/).filter(Boolean).map((tok) => {
    // 连读单元：- 连接的几个词一起包下划线，展示时还原成空格（与 UI 定稿一致）
    const words = _splitTok(tok).map(_renderWord);
    if (words.length > 1) return '<span class="liaison">' + words.join(" ") + "</span>";
    return words[0] || ""; // 孤立的 - 没有可渲染内容
  }).join(" ");
}

// 提示标签从标记文本推导：有 - 连词即"连读"，^...^ 的内容即被吞的字母
function lineTips(line) {
  const marked = line.marked || "";
  const tips = [];
  if (marked.split(/\s+/).some((t) => _splitTok(t).length > 1)) tips.push("︶ 连读");
  const swallowed = [...marked.matchAll(/\^([^^]+)\^/g)].map((m) => m[1]);
  if (swallowed.length) tips.push("✂ 吞音 " + swallowed.join(" "));
  return tips;
}

// 填充一句歌词卡片的内容（打开学习页与保存标注后就地重渲染共用）
function fillLineEl(el, line) {
  el.innerHTML = "";
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
  // 底部一行：左侧标注提示标签，右侧"修改标注"按钮（无标注的句也可从零手写）
  const foot = document.createElement("div");
  foot.className = "line-foot";
  const tip = document.createElement("div");
  tip.className = "tip-icons";
  for (const t of lineTips(line)) {
    const s = document.createElement("span");
    s.textContent = t;
    tip.appendChild(s);
  }
  foot.appendChild(tip);
  const editBtn = document.createElement("button");
  editBtn.className = "btn-edit-markup";
  editBtn.textContent = "✏️ 修改标注";
  editBtn.onclick = (e) => {
    e.stopPropagation(); // 不触发卡片的点句播放
    openMarkupDialog(line);
  };
  foot.appendChild(editBtn);
  // 句界微调（line-tweak）：上=句首往前扩、下=句尾往后扩，吸附到换气低谷，
  // 邻句自动同步收缩；反向收回用邻句的同向按钮
  const tweak = document.createElement("div");
  tweak.className = "line-tweak";
  for (const [icon, side, hint] of [["◀", "start", "句首往前扩一点（上一句变短）"],
                                    ["▶", "end", "句尾往后扩一点（下一句变短）"]]) {
    const b = document.createElement("button");
    b.className = "btn-tweak";
    b.textContent = icon;
    b.title = hint;
    b.onclick = (e) => { e.stopPropagation(); tweakLine(line, side, b); }; // 不触发点句播放
    tweak.appendChild(b);
  }
  foot.appendChild(tweak);
  el.appendChild(foot);
}

async function tweakLine(line, side, btn) {
  const res = await fetch(`/api/songs/${learnSong.id}/lines/${line.index}/expand`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ side }),
  });
  if (!res.ok) {
    // 到头了等 400：按钮短暂打叉提示，不改数据
    btn.textContent = "✕";
    setTimeout(() => { btn.textContent = side === "start" ? "◀" : "▶"; }, 800);
    return;
  }
  // 就地更新时间戳（保持对象引用，弹窗/播放逻辑读的都是同一批对象）
  const fresh = (await res.json()).lines;
  fresh.forEach((l, i) => { learnSong.lines[i].start = l.start; learnSong.lines[i].end = l.end; });
}

function openLearn(song) {
  learnSong = song;
  $("learn-song-title").textContent = song.title;
  const list = $("line-list");
  list.innerHTML = "";
  lineEls = song.lines.map((line) => {
    const el = document.createElement("div");
    el.className = "line";
    fillLineEl(el, line);
    el.onclick = () => jumpToLine(line.index - 1);
    list.appendChild(el);
    return el;
  });
  player.src = `/api/songs/${song.id}/audio`;
  // 伴奏状态随歌曲详情一起拿到：已生成切源零等待，正在生成则接着轮询
  currentStem = "original";
  stemSwitchToken++; // 让上一首歌还没落地的切源回调失效
  wantInstrumental = false;
  karaokeStatus = song.karaoke_status || "none";
  stopKaraokePolling();
  hideKaraokeHint();
  if (karaokeStatus === "running") {
    showKaraokeHint("正在做伴奏，大概要 6~7 分钟…先听原唱吧 🎵");
    startKaraokePolling();
  }
  updateStemUI();
  stopTakePlayback();
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
  if (takeRec) return;
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
  if (!learnSong || takeRec) return;
  const lines = learnSong.lines;
  if (d < 0 && currentLine === 0) {
    // 第一句再往前是前奏：回到 0，不高亮任何句
    activeLine = -1;
    player.currentTime = 0;
  } else {
    currentLine = Math.max(0, Math.min(currentLine + d, lines.length - 1));
    activeLine = currentLine;
    player.currentTime = lines[currentLine].start;
  }
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

// ---------- 原唱 / 伴奏切换（karaoke） ----------
function stemUrl(stem) {
  const base = `/api/songs/${learnSong.id}/audio`;
  return stem === "instrumental" ? base + "?stem=instrumental" : base;
}

function showKaraokeHint(text) {
  $("karaoke-hint").textContent = text;
  $("karaoke-hint").hidden = false;
}

function hideKaraokeHint() {
  $("karaoke-hint").hidden = true;
}

function updateStemUI() {
  $("btn-stem-original").classList.toggle("on", currentStem === "original");
  $("btn-stem-instrumental").classList.toggle("on", currentStem === "instrumental");
  // 生成中给"伴奏"按钮一个呼吸效果：孩子知道点了有用，只是要等
  $("btn-stem-instrumental").classList.toggle(
    "busy", karaokeStatus === "running" && currentStem !== "instrumental");
}

// 换音轨：记住位置/变速/是否在播，新音源元数据到了再恢复，避免回到 0
function switchStem(next) {
  if (next === currentStem) return Promise.resolve();
  const t = player.currentTime;
  const rate = player.playbackRate;
  const wasPlaying = linePlaying;
  currentStem = next;
  const token = ++stemSwitchToken;
  player.src = stemUrl(next);
  const restore = () => {
    if (token !== stemSwitchToken) return; // 期间又切了源/换了歌，这次恢复已过期
    // 两条音轨等长，但跳到结尾附近时留一点余量，免得一换源就 ended
    const end = player.duration || t;
    player.currentTime = Math.min(t, Math.max(0, end - 0.05));
    player.playbackRate = rate;
    // 换源期间如果已经离开学习页，不能在这里把声音重新放出来
    if (wasPlaying && screens.learn.classList.contains("active")) player.play().catch(() => {});
  };
  const ready = new Promise((resolve) => {
    if (player.readyState >= 1) return resolve();
    player.addEventListener("loadedmetadata", () => resolve(), { once: true });
  });
  if (player.readyState >= 1) restore();
  else player.addEventListener("loadedmetadata", restore, { once: true });
  updateStemUI();
  return ready;
}

function selectStem(stem) {
  if (!learnSong || takeRec) return; // 录音中切源会打乱音乐时间轴
  if (stem === "original") {
    wantInstrumental = false;
    hideKaraokeHint();
    switchStem("original");
    return;
  }
  if (karaokeStatus === "done") {
    wantInstrumental = false;
    hideKaraokeHint();
    switchStem("instrumental");
    return;
  }
  // 还没生成：先播着原唱，后台开始做，做完自动切过去
  wantInstrumental = true;
  if (karaokeStatus === "running") {
    showKaraokeHint("正在做伴奏，大概要 6~7 分钟…先听原唱吧 🎵");
    startKaraokePolling();
    return;
  }
  requestKaraoke();
}

async function requestKaraoke() {
  showKaraokeHint("正在做伴奏，大概要 6~7 分钟…先听原唱吧 🎵");
  karaokeStatus = "running";
  updateStemUI();
  const res = await fetch(`/api/songs/${learnSong.id}/separate`, { method: "POST" });
  // 202=刚开始；409=已经在做（比如本页之前点过）。其余算失败，允许再点重试
  if (!res.ok && res.status !== 409) {
    karaokeStatus = "error";
    showKaraokeHint("伴奏没做成，再点一次「伴奏」试试");
    updateStemUI();
    return;
  }
  startKaraokePolling();
}

function startKaraokePolling() {
  if (karaokeTimer) return;
  karaokeTimer = setInterval(pollKaraoke, 2000);
  pollKaraoke();
}

function stopKaraokePolling() {
  clearInterval(karaokeTimer);
  karaokeTimer = null;
}

async function pollKaraoke() {
  if (!learnSong) {
    stopKaraokePolling();
    return;
  }
  let song;
  try {
    song = await (await fetch(`/api/songs/${learnSong.id}`)).json();
  } catch {
    return; // 网络抖一下不致命，下一轮再试
  }
  karaokeStatus = song.karaoke_status || "none";
  if (karaokeStatus === "running") {
    // 微调模型一首要 6 分钟上下，没有百分比孩子会以为卡死了
    showKaraokeHint(`正在做伴奏 ${song.karaoke_progress || 0}%…先听原唱吧 🎵`);
    return;
  }
  stopKaraokePolling();
  updateStemUI();
  if (karaokeStatus === "done") {
    hideKaraokeHint();
    if (wantInstrumental) {
      wantInstrumental = false;
      switchStem("instrumental");
    }
  } else if (karaokeStatus === "error") {
    wantInstrumental = false;
    showKaraokeHint("伴奏没做成，再点一次「伴奏」试试");
  }
}

$("btn-stem-original").onclick = () => selectStem("original");
$("btn-stem-instrumental").onclick = () => selectStem("instrumental");

// ---------- 跟唱录音与"我的演唱" ----------
function fmtClock(sec) {
  const m = Math.floor(sec / 60), s = Math.floor(sec % 60);
  return m + ":" + String(s).padStart(2, "0");
}

function setRecordingUI(on) {
  $("btn-take").textContent = on ? "⏹ 停止 0:00" : "🎤 录歌";
  $("btn-take").classList.toggle("recording", on);
  // 录音期间锁住播放条与歌词点击：seek/暂停/变速都会破坏音乐时间轴
  document.querySelector(".player-bar").classList.toggle("locked", on);
  $("line-list").classList.toggle("locked", on);
}

function tickTake() {
  if (!takeRec) return;
  // 计时用播放器音频时钟，不用墙钟：更贴近真正唱了多久
  $("btn-take").textContent = "⏹ 停止 " + fmtClock(Math.max(0, player.currentTime - takeRec.start));
}

async function toggleTake() {
  if (startingTake) return;
  if (takeRec) {
    await stopTake();
    return;
  }
  startingTake = true;
  try {
    await startTake();
  } finally {
    startingTake = false;
  }
}

async function startTake() {
  if (!learnSong) return;
  if (karaokeStatus !== "done") {
    // 没伴奏就没法跟唱：顺手触发生成，做好后再点一次
    selectStem("instrumental");
    showKaraokeHint("等伴奏做好，再点「🎤 录歌」");
    return;
  }
  await switchStem("instrumental"); // 跟唱必须踩着伴奏；切换是异步的，等它落定
  if (takeRec) return;
  const start = Math.min(player.currentTime, Math.max(0, (player.duration || 0) - 3));
  const btnTake = $("btn-take");
  btnTake.textContent = "🎤 准备麦克风…";
  const res = await fetch(`/api/songs/${learnSong.id}/takes/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ start }),
  });
  if (!res.ok) {
    btnTake.textContent = "🎤 录歌";
    showKaraokeHint((await res.json()).detail || "录音没启动，再试一次");
    return;
  }
  const data = await res.json();
  player.pause();
  loopOne = false;
  $("btn-loop").classList.remove("on");
  player.currentTime = start;
  player.playbackRate = 1; // 混音按原速对齐，录音期间强制 1x
  player.play();
  linePlaying = true;
  takeRec = { id: data.take_id, start, prevSpeedIdx: speedIdx };
  setRecordingUI(true);
  takeTimer = setInterval(tickTake, 500);
  tickTake();
  updateLearnUI();
}

async function stopTake(silent = false) {
  if (!takeRec) return;
  const rec = takeRec;
  const songId = learnSong.id;
  takeRec = null;
  clearInterval(takeTimer);
  takeTimer = null;
  player.pause();
  // 用播放器音频时钟回推"音乐真正开始"的墙钟，而不是 play() 调用时刻：
  // seek + 解码 + 输出缓冲会让真正出声晚几百毫秒；四舍五入成整数毫秒（后端按 int 收）
  const playbackStartedMs = rec.playbackStartedMs
    ?? Math.round(Date.now() - Math.max(0, player.currentTime - rec.start) * 1000);
  rec.playbackStartedMs = playbackStartedMs; // 失败重试沿用同一个起播时刻
  linePlaying = false;
  player.playbackRate = SPEEDS[speedIdx];
  setRecordingUI(false);
  updateLearnUI();
  const res = await fetch(`/api/songs/${songId}/takes/stop`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ take_id: rec.id, playback_started_ms: playbackStartedMs }),
  });
  if (!res.ok) {
    if (!silent) {
      // 后端可能还在录：把状态放回去，让孩子再点一次「停止」重试
      takeRec = rec;
      setRecordingUI(true);
      $("btn-take").textContent = "⏹ 再点一次停止";
      showKaraokeHint((await res.json()).detail || "成品没做成，再点一次停止");
    }
    return;
  }
  const take = await res.json();
  // 录制中离开/换了歌：这条作废，别把成品挂到别的歌上
  if (silent || !learnSong || learnSong.id !== songId) {
    await fetch(`/api/songs/${songId}/takes/${take.id}`, { method: "DELETE" });
    return;
  }
  learnSong.takes = (learnSong.takes || []).concat(take);
  renderTakes();
  openTakeDialog(take);
}

function abortTake() {
  if (takeRec) stopTake(true);
}

// 学习页「我的演唱」与首页唱录页共用同一份名称与全局顺序，避免同一个唱录两处叫法不同
function renderTakes() {
  const takes = (learnSong && learnSong.takes) || [];
  $("take-list").hidden = takes.length === 0;
  const items = $("take-items");
  items.innerHTML = "";
  takes.slice().sort((a, b) =>
    (a.position ?? 0) - (b.position ?? 0) || String(a.created_at).localeCompare(String(b.created_at)),
  ).forEach((take) => {
    const row = document.createElement("div");
    row.className = "take-item";
    const label = document.createElement("div");
    label.className = "take-label";
    const name = document.createElement("div");
    name.className = "take-name";
    name.textContent = take.name || `跟唱 ${fmtClock(take.duration)}`; // textContent 防注入
    const meta = document.createElement("div");
    meta.className = "take-meta";
    const from = take.start > 0.5 ? `从 ${fmtClock(take.start)} 开始` : "从头唱";
    meta.textContent = `时长 ${fmtClock(take.duration)} · ${from}`;
    label.append(name, meta);
    const play = document.createElement("button");
    play.className = "take-btn";
    play.textContent = playingTakeId === take.id ? "⏸" : "▶";
    play.title = "回放这次演唱";
    play.onclick = () => toggleTakePlay(take);
    const del = document.createElement("button");
    del.className = "take-btn del";
    del.textContent = "🗑";
    del.title = "删掉这次演唱";
    del.onclick = () => deleteTake(take);
    row.append(label, play, del);
    items.appendChild(row);
  });
}

function toggleTakePlay(take) {
  if (playingTakeId === take.id) {
    stopTakePlayback();
    return;
  }
  player.pause();
  linePlaying = false;
  updateLearnUI();
  takePlayer.src = `/api/songs/${learnSong.id}/takes/${take.id}/audio`;
  takePlayer.play().catch(() => {});
  playingTakeId = take.id;
  renderTakes();
}

function stopTakePlayback() {
  takePlayer.pause();
  // 清掉 src 才会中止还没下载完的 Range 请求：否则紧接着删文件，
  // 服务端可能正在读一个已经被删掉的成品（日志里一个 500）
  takePlayer.removeAttribute("src");
  takePlayer.load();
  playingTakeId = null;
  if (learnSong) renderTakes();
}

async function deleteTake(take) {
  const songId = learnSong.id;
  if (playingTakeId === take.id) stopTakePlayback();
  const res = await fetch(`/api/songs/${songId}/takes/${take.id}`, { method: "DELETE" });
  if (!res.ok) {
    showKaraokeHint("删除失败，再试一次");
    return;
  }
  if (learnSong && learnSong.id === songId) {
    learnSong.takes = (learnSong.takes || []).filter((t) => t.id !== take.id);
    renderTakes();
  }
}

function openTakeDialog(take) {
  takePreview = take;
  $("take-audio").src = `/api/songs/${learnSong.id}/takes/${take.id}/audio`;
  $("take-dialog").showModal();
  $("take-audio").play().catch(() => {});
}

$("btn-take").onclick = () => toggleTake();
$("btn-take-keep").onclick = () => {
  $("take-audio").pause();
  $("take-dialog").close();
  takePreview = null;
};
$("btn-take-discard").onclick = async () => {
  const take = takePreview;
  takePreview = null;
  $("take-audio").pause();
  $("take-dialog").close();
  if (take) await deleteTake(take); // 已入库的成品，丢弃=删掉
};
$("take-dialog").addEventListener("close", () => $("take-audio").pause());
// 关页/刷新不会走正常停止：兜底通知后端丢掉进行中的录音（后端还有超时 watchdog 兜底）
window.addEventListener("pagehide", () => {
  if (!takeRec || !learnSong) return;
  const body = new Blob([JSON.stringify({ take_id: takeRec.id })], { type: "application/json" });
  navigator.sendBeacon(`/api/songs/${learnSong.id}/takes/cancel`, body);
});
takePlayer.addEventListener("ended", () => {
  playingTakeId = null;
  renderTakes();
});

// ---------- 我的唱录（takes-library） ----------
// 首页入口进来的跨歌列表。同时只放一条：切换时先停上一条；列表项用细进度条
// 显示当前位置。改名/删除/排序都直接落后端，学习页读的是同一份名称与顺序。
const libraryPlayer = new Audio();
let libraryTakes = [];
let playingLibraryKey = null; // `${song_id}/${take_id}`
let renamingTake = null;
let deletingTake = null;
let dragTakeCard = null;

const takeKey = (take) => `${take.song_id}/${take.id}`;

async function openTakeLibrary() {
  show("takes");
  await loadTakeLibrary();
}

async function loadTakeLibrary() {
  $("takes-error").hidden = true;
  try {
    const res = await fetch("/api/takes");
    if (!res.ok) throw new Error();
    libraryTakes = await res.json();
  } catch {
    libraryTakes = [];
    $("take-library").innerHTML = "";
    $("takes-empty").hidden = true; // 拉取失败不等于没有唱录，别给错误暗示
    showTakesError("唱录列表没加载出来，刷新页面试试");
    return;
  }
  renderTakeLibrary();
}

function showTakesError(msg) {
  $("takes-error").textContent = "😥 " + msg;
  $("takes-error").hidden = false;
}

function renderTakeLibrary() {
  $("takes-empty").hidden = libraryTakes.length > 0;
  const list = $("take-library");
  list.innerHTML = "";
  libraryTakes.forEach((take) => {
    const key = takeKey(take);
    const card = document.createElement("div");
    card.className = "take-card";
    card.dataset.key = key;
    card.dataset.songId = take.song_id;
    card.dataset.takeId = take.id;
    if (key === playingLibraryKey) card.classList.add("playing");

    const grip = document.createElement("span");
    grip.className = "drag-grip";
    grip.draggable = true;
    grip.title = "拖动排序";
    grip.textContent = "⠿";
    grip.onclick = (e) => e.stopPropagation(); // 只是把手，点了不触发播放

    const art = document.createElement("div");
    art.className = "take-art";
    art.textContent = "🎤";

    const main = document.createElement("div");
    main.className = "take-main";
    const name = document.createElement("div");
    name.className = "take-name";
    name.textContent = take.name; // textContent 防注入
    const info = document.createElement("div");
    info.className = "take-info";
    info.textContent = `来自《${take.song_title}》· ${formatDuration(take.duration)}`;
    const progress = document.createElement("div");
    progress.className = "take-progress";
    progress.appendChild(document.createElement("i"));
    main.append(name, info, progress);

    const play = document.createElement("button");
    play.className = "take-action play";
    play.title = "回放这条唱录";
    setTakePlayIcon(play, key === playingLibraryKey);
    play.onclick = (e) => { e.stopPropagation(); toggleLibraryPlay(take); };

    const rename = document.createElement("button");
    rename.className = "take-action rename";
    rename.title = "重命名";
    rename.textContent = "✏️";
    rename.onclick = (e) => { e.stopPropagation(); openTakeRename(take); };

    const del = document.createElement("button");
    del.className = "take-action del";
    del.title = "删掉这条唱录";
    del.textContent = "🗑";
    del.onclick = (e) => { e.stopPropagation(); openTakeDelete(take); };

    // 点卡片本体也能播放，按钮各自 stopPropagation 拦掉
    card.onclick = () => toggleLibraryPlay(take);
    card.append(grip, art, main, play, rename, del);
    list.appendChild(card);
  });
  updateLibraryPlaying();
}

function updateLibraryPlaying() {
  document.querySelectorAll(".take-card").forEach((card) => {
    const on = card.dataset.key === playingLibraryKey && !libraryPlayer.paused;
    card.classList.toggle("playing", on);
    const btn = card.querySelector(".take-action.play");
    if (btn) setTakePlayIcon(btn, on);
    if (!on) {
      const bar = card.querySelector(".take-progress i");
      if (bar) bar.style.width = "0%";
    }
  });
}

// 暂停用与学习页播放条同一枚 SVG：⏸ 字符会按 emoji 渲染成橙色方块，和卡片风格不搭
function setTakePlayIcon(btn, playing) {
  if (playing) btn.innerHTML = ICON_PAUSE;
  else btn.textContent = "▶";
}

function toggleLibraryPlay(take) {
  $("takes-error").hidden = true; // 上次播放失败的提示不该赖在页面上
  const key = takeKey(take);
  if (key === playingLibraryKey) {
    // 同一条：暂停后从原位置继续，不重新加载
    if (libraryPlayer.paused) {
      libraryPlayer.play().catch(() => playLibraryError());
    } else {
      libraryPlayer.pause();
    }
    updateLibraryPlaying();
    return;
  }
  stopLibraryPlayback();
  libraryPlayer.src = `/api/songs/${take.song_id}/takes/${take.id}/audio`;
  playingLibraryKey = key;
  libraryPlayer.play().catch(playLibraryError);
  updateLibraryPlaying();
}

function playLibraryError() {
  showTakesError("这条唱录放不出来，文件可能已经不在了");
  stopLibraryPlayback();
}

function stopLibraryPlayback() {
  libraryPlayer.pause();
  // 清掉 src 才会中止未完成的 Range 请求：否则紧接着删文件，服务端可能正在读
  libraryPlayer.removeAttribute("src");
  libraryPlayer.load();
  playingLibraryKey = null;
  updateLibraryPlaying();
}

libraryPlayer.addEventListener("play", updateLibraryPlaying);
libraryPlayer.addEventListener("pause", updateLibraryPlaying);
libraryPlayer.addEventListener("ended", () => {
  playingLibraryKey = null;
  updateLibraryPlaying();
});
libraryPlayer.addEventListener("timeupdate", () => {
  if (!playingLibraryKey) return;
  const card = [...document.querySelectorAll(".take-card")]
    .find((el) => el.dataset.key === playingLibraryKey);
  const bar = card && card.querySelector(".take-progress i");
  if (!bar) return;
  const total = libraryPlayer.duration || 0;
  bar.style.width = total ? `${Math.min(100, (libraryPlayer.currentTime / total) * 100)}%` : "0%";
});

// ---- 重命名 ----
function openTakeRename(take) {
  renamingTake = take;
  $("take-rename-input").value = take.name;
  $("take-rename-error").hidden = true;
  $("take-rename-dialog").showModal();
  $("take-rename-input").focus();
  $("take-rename-input").select();
}

$("btn-take-rename-cancel").onclick = () => $("take-rename-dialog").close();
$("take-rename-dialog").addEventListener("close", () => { renamingTake = null; });

$("btn-take-rename-save").onclick = async () => {
  const take = renamingTake;
  if (!take) return;
  const name = $("take-rename-input").value.trim();
  if (!name) {
    $("take-rename-error").textContent = "😥 名字不能为空";
    $("take-rename-error").hidden = false;
    return;
  }
  const res = await fetch(`/api/songs/${take.song_id}/takes/${take.id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  if (!res.ok) {
    $("take-rename-error").textContent = "😥 " + ((await res.json()).detail || "改名失败");
    $("take-rename-error").hidden = false;
    return;
  }
  take.name = (await res.json()).name;
  $("take-rename-dialog").close();
  // 就地更新卡片标题，不整页重渲染，避免滚动位置和播放状态跳动
  const card = [...document.querySelectorAll(".take-card")]
    .find((el) => el.dataset.key === takeKey(take));
  const label = card && card.querySelector(".take-name");
  if (label) label.textContent = take.name;
};

// ---- 删除（二次确认） ----
function openTakeDelete(take) {
  deletingTake = take;
  $("take-delete-name").textContent = take.name;
  $("take-delete-dialog").showModal();
}

$("btn-take-delete-cancel").onclick = () => $("take-delete-dialog").close();
$("take-delete-dialog").addEventListener("close", () => { deletingTake = null; });

$("btn-take-delete-ok").onclick = async () => {
  const take = deletingTake;
  $("take-delete-dialog").close();
  if (!take) return;
  if (takeKey(take) === playingLibraryKey) stopLibraryPlayback();
  const res = await fetch(`/api/songs/${take.song_id}/takes/${take.id}`, { method: "DELETE" });
  if (!res.ok) {
    showTakesError("删除失败，再试一次");
    return;
  }
  const key = takeKey(take);
  libraryTakes = libraryTakes.filter((t) => takeKey(t) !== key);
  renderTakeLibrary();
};

// ---- 拖动排序 ----
// 只有 ⠿ 把手能拖：点卡片是播放，整卡可拖会让单击变得别扭
const takeLibraryEl = $("take-library");
takeLibraryEl.addEventListener("dragstart", (e) => {
  const grip = e.target.closest(".drag-grip");
  if (!grip) return;
  dragTakeCard = grip.closest(".take-card");
  dragTakeCard.classList.add("dragging");
  if (e.dataTransfer) {
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setDragImage(dragTakeCard, 24, 24);
  }
});

takeLibraryEl.addEventListener("dragover", (e) => {
  e.preventDefault();
  if (!dragTakeCard) return;
  const after = [...takeLibraryEl.querySelectorAll(".take-card:not(.dragging)")]
    .find((el) => e.clientY < el.getBoundingClientRect().top + el.offsetHeight / 2);
  if (after) takeLibraryEl.insertBefore(dragTakeCard, after);
  else takeLibraryEl.appendChild(dragTakeCard);
});

takeLibraryEl.addEventListener("drop", (e) => e.preventDefault());

takeLibraryEl.addEventListener("dragend", async () => {
  if (!dragTakeCard) return;
  dragTakeCard.classList.remove("dragging");
  dragTakeCard = null;
  const cards = [...takeLibraryEl.querySelectorAll(".take-card")];
  const items = cards.map((el) => ({ song_id: el.dataset.songId, take_id: el.dataset.takeId }));
  try {
    const res = await fetch("/api/takes/order", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items }),
    });
    if (res.ok) {
      const byKey = new Map(libraryTakes.map((t) => [takeKey(t), t]));
      libraryTakes = items.map((it) => byKey.get(`${it.song_id}/${it.take_id}`)).filter(Boolean);
      return;
    }
    await loadTakeLibrary(); // 409（期间又录了一条）或过期：重拉服务端顺序
    if (res.status === 409) showTakesError("唱录列表刚刚有变化，已经帮你刷新了");
  } catch {
    await loadTakeLibrary();
  }
});

$("btn-takes-back").onclick = () => { show("library"); loadSongs(); };

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

// ---------- 修改标注弹窗 ----------
let editingLine = null; // 正在改标注的那句（引用 learnSong.lines 里的对象，保存后就地生效）

function openMarkupDialog(line) {
  editingLine = line;
  $("markup-title").textContent = `✏️ 修改标注 · 第 ${line.index} 句`;
  $("markup-original").textContent = line.text; // 原句对照，textContent 防注入
  $("markup-text").value = line.marked || line.text; // 没标过的句从原句起手
  $("markup-error").hidden = true;
  $("markup-dialog").showModal();
  $("markup-text").focus();
}

$("btn-markup-cancel").onclick = () => $("markup-dialog").close();

$("btn-markup-save").onclick = async () => {
  const res = await fetch(`/api/songs/${learnSong.id}/lines/${editingLine.index}/markup`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: $("markup-text").value }),
  });
  if (!res.ok) {
    // 校验失败（误改了词等）：弹窗内红字提示，不关闭，让用户接着改
    $("markup-error").textContent = "😥 " + ((await res.json()).detail || "保存失败");
    $("markup-error").hidden = false;
    return;
  }
  editingLine.marked = (await res.json()).marked;
  $("markup-dialog").close();
  fillLineEl(lineEls[editingLine.index - 1], editingLine); // 就地重渲染这一句
};

$("btn-markup-reset").onclick = async () => {
  // 后端重算规则引擎结果填入文本框（不落盘），由用户再点保存确认
  const res = await fetch(`/api/songs/${learnSong.id}/lines/${editingLine.index}/markup/reset`,
    { method: "POST" });
  if (res.ok) {
    $("markup-text").value = (await res.json()).text;
    $("markup-error").hidden = true;
  }
};

// 学习页的播放/暂停
function toggleLearnPlay() {
  if (takeRec) return;
  if (linePlaying) {
    player.pause();
    linePlaying = false;
    updateLearnUI();
    return;
  }
  // 从当前位置继续：刚打开是 0（前奏一起播出来），暂停后是暂停点；
  // 整首播完了则从头再来（同样含前奏）
  if (player.ended) player.currentTime = 0;
  player.playbackRate = SPEEDS[speedIdx];
  player.play();
  linePlaying = true;
  updateLearnUI();
}
function toggleLoop() {
  if (takeRec) return;
  loopOne = !loopOne;
  $("btn-loop").classList.toggle("on", loopOne);
}
function cycleSpeed() {
  if (takeRec) return;
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
  if ($("markup-dialog").open) return; // 弹窗输入时把按键留给文本框
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

player.addEventListener("ended", () => {
  if (takeRec) { // 唱到曲子结尾：自动停止并生成成品
    stopTake();
    return;
  }
  if (linePlaying) { // 连播到曲末自然结束：停在最末句，图标回到播放
    linePlaying = false;
    activeLine = -1;
    updateLearnUI();
  }
});

loadSongs();
