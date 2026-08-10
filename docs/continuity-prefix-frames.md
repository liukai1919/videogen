# 跨段连续性与"前缀帧"——机制核查与业界做法(2026-08-10)

起因:R1 评测夜 story 渲染 7/11 崩溃(ComfyUI 进程死亡),当时头号嫌疑是
2026-08-09 试开的 `renderer.continuous_reference`(前缀帧)。本次对照实验前
先读了节点源码,结论反转,记录如下。

## 一、核查结论:我们的"前缀帧试开"是双重空转

本机节点 `ComfyUI_MiniMaxH3_Director` 源码(WSL `~/ComfyUI/custom_nodes/`):

1. **写错了键。** 我们的 renderer 把 `config.renderer.continuous_reference`
   写进 timeline 的 `global.continuousReference`(renderer.py build_timeline);
   而节点里这个键的唯一消费点是 `director/plan.py:582→606`——只用于
   **ads2v(广告植入)段的参考视频起始帧**,与跨段接力无关。我们从不用 ads2v。
2. **真开关我们没写,写了也过不了门槛。** 节点真正的段间接力开关是
   `output.continuityEnabled` + `output.continuityOverlapFrames`
   (`director/segment_continuity.py:127 resolve_continuity_settings`),且
   `is_continuity_segment` 要求段的 task_key ∈ {i2v, fl2v} 且段数 ≥2;
   我们的 story 块是 gen_blank + t2v,一票否决。整套机制还建立在
   有源视频画布的 WanSCAIL 编辑栈上,和我们的空白分镜生成不同源。

**推论:** 前缀帧从未被垫进任何一块渲染 → 崩溃与它无关,真凶另查
(崩溃签名 connection refused/reset = ComfyUI 进程死亡后被拉起,像宿主侧
OOM/进程级问题);config 注释宣称的"Director 把上一段尾帧+外观参考带进
下一段"没有发生过。跨块一致性一直靠我们自己的两件套(见下)。

## 二、节点真实的接力机制(将来若要接入,按这个理解)

`segment_continuity.py` 头注释即设计说明(WanSCAIL 式交接,一次提交内的
段与段之间):

- 把上一段尾部 **默认 9 帧**(`continuityOverlapFrames`,可调 1..81,
  Wan 4n+1 对齐)以像素垫进下一段画布,生成 `prefix+segment`,解码后裁掉
  前缀;SCAIL 用 noise_mask 锁死前缀不重采样。
- 另取上一段尾部 1-2 帧作外观参考进 context_latents(发型/服装跳变防抖)。
- 附加开销:lookahead 8 帧 + 落定烧机 12 帧(导出丢弃)+ 开场 RGB 桥 8 帧
  ——每段画布净增约 20~29 帧(12s 段 288 帧 → ~10% 画布膨胀)。
- 代码注释里满是血泪:羽化/软解锁会"画面花",双运动流会接缝跳变——
  这是个精细但脆弱的路径,接入需按其 15 条注意事项逐条对照。
- **适用范围:一次提交内部的段间**。我们的拆链是多次提交,块与块之间
  它管不着——那是我们自己帧接力的战场。

## 三、官方与业界怎么做长视频连续性

- **MiniMax H3 官方**(github.com/MiniMax-AI/MiniMax-H3):只定义单发
  4-15 秒,**未提任何多段/前缀帧/长视频机制**——跨段一致性是社区
  工程层的事,官方不背书。
- **FramePack**(lllyasviel,arXiv 2504.12626):把历史帧上下文**压缩成
  定长表示**,变换器上下文长度与视频总长无关 → 显存 O(1),13B 模型
  6GB 卡能跑分钟级视频;反漂移用**倒序采样锚定高质量端点**。教训:
  朴素前缀帧线性膨胀画布,成熟做法是压缩历史而不是携带历史。
- **SkyReels-V2**(arXiv 2504.13074):Diffusion Forcing——每帧独立噪声
  等级,新帧以已去噪的干净帧为条件自回归延长;实操按 **97 帧一段,
  段间 overlap + 噪声条件**衔接,无限时长。
- **共性:** 三条路线殊途同归——要么压缩历史(FramePack),要么
  重叠+条件化(SkyReels/本节点的 9 帧 SCAIL),要么锚点帧(i2v 接力,
  我们在用)。全都在对抗同一对矛盾:上下文越多越连贯,显存越爆。

## 四、我们的现状与建议

已在工作且验证过的跨块两件套(5ff9625):
- **帧接力**:`[Xs-Ys续]` 标记 → 块边界取上一块尾帧作 i2v 锚点
  (test_handoff_18s 验证)。
- **r2v 定妆**:资产定妆照经 ref_assets 贯穿每块锁身份
  (test_r2v_identity 验证)。

建议:
1. **短期**:`continuous_reference` 置 false 并改写注释还原真相(本次
   已做);ComfyUI 崩溃根因单独排查(宿主侧日志/事件,已挂任务卡)。
2. **中期(若段内接缝要更顺)**:接入节点真开关是一个工程——需要把
   非首段改造成 i2v 语义并评估 gen_blank 兼容性,按第二节 15 条
   注意事项验证,且 overlap 从小档(3~5 帧)起步控显存。
3. **远期参考**:FramePack 的"定长历史"思路若进入 ComfyUI 生态的
   H3/Wan 节点,优先于自研。

## 五、原定的 A/B 对照:已取消

原计划固定负载(《灯塔守望者》24s 四镜双块,seed=808)各 3 发对比
开关两态。源码核查(第一节)已直接回答了问题——开关根本不进渲染路径,
两个臂在物理上是同一件事,对照失去意义,跑到一半叫停(首发 ab-on-1
已排入无法取消,烧完即止,不作对照数据)。

ComfyUI 崩溃根因的排查(独立任务)不再需要这组对照;固定负载复测
脚本保留在会话 scratchpad(ab-arm.sh),排查时可直接复用作基线工具。
