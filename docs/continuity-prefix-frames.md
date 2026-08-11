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

## 六、落地记(2026-08-10 晚):跨块风格锚默认化

**事故:** `ag_20260810214110_7291b8`(钢铁侠 vs 二郎神,4×5s story)
连贯性崩坏。根因链:min_seconds=5 + 单块 362 帧预算 → 每块最多 2 段,
4 段必拆双块;分镜无 续 标记、无 refs → 两块是不同种子的完全独立渲染,
10.33s 边界处场景/光线/配乐全部硬切,二郎神跨块换装、胸口还串味长出
反应堆。上游:codex 首轮方案本带 [10s-15s续],换本地大脑执行时把方案
压缩成一句 idea 传给 write_storyboard,续标记丢失;编剧模板的 continues
判据(同一动作才标)对剪辑式分镜永远不触发;三层里没有一层知道块边界
落在哪、边界意味着什么。

**第一版修复(跨块风格锚)已被同日复测证伪,撤销。** 复测设计:同
提示词 + 同有效种子(job_seed 显式回传)重渲 `ab-chunkanchor-on-1`,
块 2 每段确实带上了尾帧参考(ComfyUI history 可见 per-seg refs
[1,1])——但成片与原片**逐像素一致**(三组采样帧 PSNR=inf)。顺藤摸到
节点源码,真相比预想更大:

- `director/plan.py` / `gen_timeline.py` 的
  `CONTEXT_REFERENCE_EXCLUDED_KEYS = {i2v, fl2v, t2v, v2v}`:**t2v 段
  丢弃一切段级 refs**。风格钥匙对 story 分镜从来无效;3.3b 的"节点
  接受"只是不报错,不等于进采样。M4 的 style key A/B(9v9"无显著
  差异")实为两条物理相同的视频在对比,该结论作废。
- editMode=segment 下段**从不继承 global.refs**——而我们的 r2v 定妆
  恰恰只写在 global 上。ComfyUI 日志实锤:每次 r2v 渲染都在警告
  "gen segment task=r2v has no reference media — will behave like
  t2v"。**test_r2v_identity 的"锁脸验证"是假阳性**(纯提示词相似)。
  两件套里只有帧接力(续标记 i2v 锚)是真的在工作。
- story 与 r2v 的工作流权重不同(fl2va vs ref2va),story 段改挂 r2v
  任务此路不通:story 模式下不存在任何可用的参考图通道。

**第二版修复(本次落地,全量测试 233 过):**
1. **修通 r2v 定妆**:build_timeline 把 refs 下沉到每一段的 refs
   (段任务 r2v 时节点才消费);定妆图占 <Picture 1..N>,段级风格
   钥匙接在其后编号。global.refs 保留仅作对账。
2. **拒收无效挂载**:validate_render 只允许 r2v 分镜带 segment_refs,
   story 挂了直接报错(此前是静默无效);定妆+段级合计 ≤9。
3. 能力表 `chunk` 事实块(每块段数上限、边界硬切、无自动跨块锚)+
   `accepts_segment_refs` 收敛为 r2v 专属;agent 系统提示同步:跨块
   锁角色/风格唯一通道是 r2v 定妆,≥3 段高连贯先讲清风险;方案获
   确认后原文一字不落传给 write_storyboard(计划-执行断链的封堵)。
4. 编剧模板第 7 条加码:每镜 prompt 自含完整场景,禁写依赖上一镜的
   指代。

**方法论教训:** "节点接受"≠"节点消费"。任何参考图通道的验收标准
从此固定为:同种子 A/B 出现像素级差异 + ComfyUI 日志无 fallback 警告,
二者缺一不可。
