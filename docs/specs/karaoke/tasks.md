# tinker karaoke 任务清单

事实源：[spec.md](spec.md)（2026-10-24 用户确认：Demucs htdemucs；跟唱录音回放恢复，评分不做）

## M1：伴奏分离后端

- [x] pyproject 引入 demucs / torch / torchaudio / numpy，uv 配置 PyTorch CPU 索引（避免默认拉 CUDA 轮子），更新 lock
  - 证据：uv lock/sync 在 Python 3.14.8 装出 torch 2.14.1+cpu / torchaudio 2.11.0+cpu / demucs 4.1.0；`tool.uv.environments` 限定 linux 后才解得开（demucs 的 macOS marker 要求 numpy<2）
- [x] karaoke.py：子进程跑 `demucs --two-stems=vocals --mp3`，产物落 `data/stems/{song_id}/no_vocals.mp3`；单任务队列、状态机（none/running/done/error）、失败保留错误摘要、启动时清理崩溃残留的 running
- [x] API：`POST /api/songs/{id}/separate`（202 启动 / 409 已在跑 / 200 已有）、歌曲详情带 karaoke 状态、`GET /api/songs/{id}/audio?stem=instrumental`；删除歌曲时清理 stems 目录
- [x] 单测：mock 子进程覆盖触发/重复触发/成功/失败/删除清理
- [x] **验证**：真实跑《Young for you》与 Twinkle 两首，产物时长与源一致、非静音；伴奏轨 whisper 无法转出歌词而人声轨可以；API 状态流转与 404/409 路径；全部单测通过
  - 证据：《Young for you》真实服务（8765）202→running→done，200.6s 的歌 73s 跑完；产物 4.8MB / 192kbps / 200.65s（源 200.62s），音量非静音；伴奏接口 200 `audio/mpeg` 且 `accept-ranges: bytes`；重复触发 409、未生成时伴奏 404、未知 stem 404；Twinkle（录音文件缺失）落 error「生成失败：录音文件丢失」；实际产物经 faster-whisper 只能转出 "You You You…"，而人声轨可完整转写全部歌词；全量 59 项测试通过（karaoke 8 项）

## M2：学习页原唱/伴奏切换

- [x] 学习页加"原唱 / 伴奏"切换；切换保留 currentTime、播放/暂停状态与变速
- [x] 未生成时先播原唱，触发后台生成并轮询状态、显示进度提示；失败可再点重试
- [x] 打开学习页即预取歌曲详情里的伴奏状态，已生成则切源零等待
- [x] **验证**：真实浏览器（headless Chrome 驱动）实操——播放中切换位置不跳、暂停切换状态不丢、未生成时等待流程与失败重试
  - 证据：headless Chrome 驱动真实页面 15 项全过——未生成时点伴奏：提示"正在做伴奏…"、继续播原唱、按钮呼吸态；约 73s 后自动切伴奏且位置继续前进（不回 0）；暂停中切回原唱/再切伴奏均保持暂停、位置 30.0s 不丢；Twinkle（录音缺失）轮询后提示"伴奏没做成…"且忙碌态解除；补充脚本验证 0.75x 变速切源后仍为 0.75x、位置 12.0s 保留
  - 说明：切源回调加了代次令牌——换歌/离开学习页后迟到的 loadedmetadata 不再恢复旧位置或重新出声

## M3：跟唱录音回放（spec-change 恢复）

- [ ] MicRecorder：`parec @DEFAULT_SOURCE@` 录默认输入源到 wav；start 返回后端时间戳，stop 收前端起播时间戳算出偏移
- [ ] API：开始/停止跟唱录音（停止时用 ffmpeg 按偏移与伴奏混成单文件 mp3）、take 列表/删除；删除歌曲时清理 takes
- [ ] 前端：🎤 跟唱入口 + 戴耳机提示 + 录音计时；停止后成品回放弹窗（保存/丢弃）；学习页展示"我的演唱"列表，可回放/删除
- [ ] **验证**：戴耳机真实跟唱一段，成品回放人声与伴奏对拍（偏移在 100ms 内）、保存后重启服务仍在、删除歌曲连带清理；混音参数量纲单测
