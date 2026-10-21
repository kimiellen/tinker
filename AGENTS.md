# AGENTS.md —— tinker 项目长期约定

## 项目是什么

tinker：跑在 Arch Linux 单机上的英文歌学唱应用（首要用户是小学生）。事实源见 `docs/specs/mvp/spec.md`，方向性变更必须走 spec-change 协议先改文档。

## 技术栈（已选定，勿擅自更换）

- **后端**：Python 3.11+，FastAPI，依赖管理用 **uv**（`uv add` / `uv run`），不用 pip/poetry
- **前端**：无构建步骤的原生 HTML/JS/CSS，由 FastAPI 托管静态文件。**不引入 npm/打包器/前端框架**
- **录音**：PipeWire/PulseAudio monitor source（`pw-record` / `parec`），仅 Linux
- **音频分析**：faster-whisper（歌词对齐）、librosa（音高提取）
- **LLM**：可配置 API（默认 DeepSeek），结果必须本地缓存，重复请求不得重复调 API
- **运行环境**：Arch Linux 单机，无需考虑跨平台

## 代码红线

- YAGNI：不做 spec 之外的功能，不为"将来可能"留抽象
- DRY：重复出现两次以上的逻辑必须收敛
- 重依赖（torch、tensorflow 等）引入前必须在 spec 中论证；默认用轻量替代（如 librosa 而非 CREPE）
- 前端保持零构建：只允许 `<script src>` 引入的本地 vendored 库，新增前说明理由
- 代码会被开源阅读：命名清晰、注释解释"为什么"而非"是什么"

## 目录结构

```
tinker/
├── docs/specs/<feature>/   # spec.md + tasks.md，跨会话事实源
├── backend/                # FastAPI 应用（uv 项目）
│   └── tinker/             # Python 包
├── frontend/               # 静态文件（由后端托管）
└── data/                   # 运行期数据：录音、缓存、歌曲库（不入库）
```

## 开发纪律

- 直接在 main 开发，里程碑即 commit，一个 commit 只做一件事
- 宣布完成前必须有实际运行证据（测试/构建/真实运行输出）
