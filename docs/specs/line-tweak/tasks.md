# line-tweak 任务清单

## M1：逐换气口手动微调句界

- [x] aligner：提取 RMS 包络/低谷查找 helper（_valley_boundaries 复用收敛），
  新增 `expand_boundary(lines, idx, side, audio)`——方向窗口内低谷吸附 +
  邻句钳制/同步规则
  - 验证：pytest 合成音频（句首前扩吸附低谷、句尾后扩、共享句界同步、
    间奏不吞噬、到头报错）
- [x] 接口：`POST /api/songs/{id}/lines/{n}/expand`，body `{side: start|end}`，
  落盘并返回更新后的 lines
  - 验证：curl 实测《Young for you》行 3 句首前扩 26.90→26.61 附近；
    错误路径（未分句/句不存在/到头）400
- [x] 前端：句卡右下竖排 ◀/▶ 按钮，点击调接口并就地更新 lines 数据，
  不触发点句播放
  - 验证：起服务浏览器实开学习页，按钮渲染、点击后单句循环区间变化可听
