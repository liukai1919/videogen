# 对标 Higgsfield 的平台硬化计划

来源：对 [higgsfield-ai/skills](https://github.com/higgsfield-ai/skills)（v0.12.0）及其组织仓库的分析。
它们的核心经验——工具面收窄、SKILL.md 300 行纪律、活数据与静态知识分离、评测钉默认值、
style key 锁风格、成片评分闭环——按对本项目的价值和依赖关系排成四个里程碑。

## 不变前提

- `/health`、`/v1/validate`、`/v1/renders` 冻结契约不动（videotube 在用）。
- 一切本地：Ollama / ComfyUI / H3 / 本地 TTS / FFmpeg，不引入云端 API。
- 对话内生成工具保持"排队即返回"的异步设计——那是为对话响应性刻意选的，
  不照抄 Higgsfield 的 `--wait`（它们的 agent 就是终端，我们的对话 agent 不是）。
- 画布进阶（refactor-design.md 阶段 E）与本计划正交，不在此处。

---

## M1 · 评测防护网 + 决策台账（约 0.5 人日，纯文档，先做）

后面每个里程碑都要改提示词和工具行为，先立回归标尺，否则每次重构都是碰运气。

| # | 任务 | 产物 |
|---|---|---|
| 1.1 | 写 `evals/scenarios.md`：8–10 个场景，每个 = 用户说什么 → agent 应该做什么 → pass/partial/fail 判据。首批场景：①分镜→渲染→配音→合成四工具成片链；②长片跨段一致性（帧接力生效）；③r2v 定妆锁脸被正确使用；④单段时长红线被尊重（不排 >15s 渲染）；⑤技能路由正确（预告片需求进 concept-trailer 而不是 science-doc）；⑥Skill 缺省值正确合并进请求 | `evals/scenarios.md` |
| 1.2 | 写 `evals/README.md`：轮次协议——记录 commit SHA、日期、每场景得分、出片耗时；回归阈值：总分降 >15% 或耗时 >2× → 回滚重查 | `evals/README.md` |
| 1.3 | 建"Key Decisions (Do Not Revisit Without Data)"台账，首批写入已经用代价换来的结论：单渲染每段 ≤15 秒；1080p 不进渲染队列；H3 提示词遵循官方规范；长片一致性 = 拆链渲染 + 帧接力 + r2v 定妆。以后任何人（包括 agent 自己）想改默认值，必须带新一轮 eval 数据 | `evals/DECISIONS.md` |

验收：新人（或外部 worker）只读 evals/ 就能跑一轮回归并给出可比分数。
后续可选：让外部 agent worker 自动跑场景，人只看分数——先手动，跑通协议再自动化。

## M2 · 技能体系升级：路由三件套 + references 分层（约 1–2 人日）

现状：`meta.yaml` 只有 name/description/category/defaults，SKILL.md 全文（≤8000 字符）
整体注入提示词。技能多起来后既费 token 又没法自动路由。

| # | 任务 | 涉及文件 |
|---|---|---|
| 2.1 | `SkillMeta` 增加 `use_when: list[str]`（触发短语）、`not_for: list[str]`（边界+转介）、`chain: list[str]`、`version: str`，全部可选、旧 meta.yaml 保持合法 | `videogen_service/skills.py`、`tests/test_skills.py` |
| 2.2 | 支持 `skills/<id>/references/*.md`：`SkillLibrary` 发现并列出；新增 agent 工具 `read_skill_reference(skill_id, name)` 按需读取。SKILL.md 主文件只留决策性内容（判据学 Higgsfield：删掉这段会不会破坏下一步决策？不会 → 进 references/） | `videogen_service/skills.py`、`videogen_service/agent.py` |
| 2.3 | 迁移现有两个技能：concept-trailer、science-doc 补 use_when/not_for；把细节规则（如逐条摄影规范）移入 references/，主文件压到"路由 + 决策树" | `skills/concept-trailer/`、`skills/science-doc/` |
| 2.4 | 提示词注入改为两级：系统提示只带路由表（id + description + use_when/not_for，不带全文），选中技能后才注入其 SKILL.md 主文件 | `videogen_service/scripting.py`、`agent.py` |
| 2.5 | 技能 lint 进测试（对标它们的 validate-skills.yml）：meta 合法、目录名=id、references 都被 SKILL.md 引用（无孤儿）、SKILL.md 超行数上限报警 | `tests/test_skills.py` 或新 `tests/test_skill_lint.py` |

验收：M1 场景⑤⑥通过；同等请求下注入提示词的技能相关 token 明显下降；`pytest tests/test_skills.py` 全绿。

## M3 · 能力活数据化 + 红线机制化（约 2–4 人日，可拆单项上线）

现状：H3 各 workflow 的参数边界靠提示词和人脑记忆；OOM 红线只存在于记忆里；
negative_prompt / 每段 refs / prompt_enhance 三个官方能力没接线。

| # | 任务 | 涉及文件 |
|---|---|---|
| 3.1 | H3 能力元数据进 `/v1/capabilities`：每个 workflow（t2v/i2v/flf2v/r2v/director_story）声明参数 schema——时长上限、分辨率枚举、支持哪些可选参数。agent 用 `list_capabilities` 现查，提示词里不再硬编码参数表（"活数据不进技能"） | `videogen_service/config.py`、`jobs.py`、`workflows/*.json` |
| 3.2 | 服务端 adjustments 纠偏：生成工具收到越界参数（段长 >15s、1080p 排队）时不报错，吸附到红线内并在返回 JSON 里带 `adjustments: [...]` 说明改了什么。红线值从 config 读，与 DECISIONS.md 呼应 | `videogen_service/jobs.py`、`renderer.py`、`tests/test_jobs.py` |
| 3.3 | 接线三个未用的 H3 能力（每个独立成 PR，各配一个新 eval 场景）：a. `negative_prompt` 从工具 schema 通到 workflow 注入；b. 每段 refs（分段参考图）；c. `prompt_enhance` 开关 | `videogen_service/agent.py`、`comfyui.py`、`renderer.py` |
| 3.4 | 外部 worker 的等待语义：补 `wait_job(job_id, timeout)` 阻塞式工具（或轮询工具的 `--wait` 语义），worker 一步拿终态；对话内工具维持异步不变 | `videogen_service/agent.py`、`agents/` |

验收：把提示词里的 H3 参数描述删掉后，agent 仍能通过 `list_capabilities` 排出合法任务；
故意传 20s/1080p，任务被纠偏执行且返回里可见 adjustments；场景④由"提示词求它守规矩"变成"服务端保证"。

**M3 落地记（2026-08-10）：**
- 3.1 ✓ `mode_capability_schema`（renderer.py）作为唯一事实源，/v1/capabilities 与
  agent 的 list_capabilities 共用，像素红线常量同源收编。
- 3.2 缓：M2 把段长规则前移进工具描述后 R2 全轮零失败提交，adjustments 从
  "必需"降为"保险带"，暂不做。
- 3.3a/c（negative_prompt、prompt_enhance）缓：节点 story 通路的消费点未证实
  （只有 fl2v 路径明确读 negativePrompt）——continuous_reference 空转的教训在前，
  拒绝再接可能空转的线；待单独实验证实后再接。
- 3.3b ✓ segment_ref_assets 全链（agent/HTTP/存储/指纹/timeline 每段注入），
  真机探针验证节点接受;这是 M4 风格钥匙的通道。
  **勘误(2026-08-10 晚)**:"节点接受"≠"节点消费"——t2v 段丢弃一切
  refs,通道对 story 无效,仅 r2v 分镜可用;见 continuity 文档第六节。
- 3.4 ✓ wait_render / wait_job 阻塞工具（默认 900s,上限 1800s,超时带标记返回）。
- 计划外并入：编剧裁剪器就近适配（修"片长偏短八成"）、continues 判据重写、
  默认风格中性化 + write_storyboard style 参数。

## M4 · 生产新能力：style key + 成片评分闭环（实验性，各 2–3 人日）

| # | 任务 | 说明 |
|---|---|---|
| 4.1 | 全片 style key：项目级"风格钥匙图"——ComfyUI 生成一张风格基准图存入资产中心；分镜的每段渲染把它作为风格参考输入（r2v 已有定妆锁脸通道，style key 走同机制的另一个参考槽位，依赖 3.3b 的每段 refs）。用 eval 做 A/B：帧接力 vs 帧接力+style key 的跨段色调一致性 | Higgsfield video-explainer 已验证"一张风格图挂全片每个 clip"可行 |
| 4.2 | 成片评分闭环：新增 `review_video` 能力——本地多模态模型（Ollama 拉一个 VLM）抽帧打分：开场 hook 强度、跨段一致性、画面-旁白对齐；分数写进成片资产元数据，工作台成片卡片展示，作为 Release Review 的参考输入（不替代人审） | 对标 Virality Predictor，但全本地。走通用任务队列，抽帧用 FFmpeg（CPU，不占 GPU 闸门；VLM 推理走 GPU 闸门排队） |

验收：4.1——同一脚本两种配置各出一片，eval 打分可见一致性差异；4.2——每个成片自动带一份评分卡，Release Review 页可见。

**M4 落地记（2026-08-10）：**
- 4.2 ✓ **review 内建能力全线打通**：FFmpeg 抽 6 帧(CPU) + qwen3.6 原生多模态
  打分(实测有视觉能力,零新模型),hook/consistency/visual_quality/alignment
  各 1-10 + 中文评语;真机 45 秒/次,评语能独立发现"首镜俯视 vs 分镜仰摇"
  级别的真实意图偏差。agent 工具 review_video + check_job 直接带回分数;
  工作台评分卡 UI 留待后续(评分任务已在任务网格可见)。
- 4.1 通道与玩法 ✓：save_render_frame_as_asset(frame_path 增成片抽帧回退,
  定妆照/风格钥匙的生产入口)+ segment_ref_assets + 系统提示玩法注入。
  **A/B 结果(24s 双块,同 seed)：无显著差异**——consistency 9 vs 9,现有
  一致性机制在此工况已到 9 分天花板。通道保留,待 60-120s 多块长片复测;
  不进 DECISIONS。
  **勘误(2026-08-10 晚)**:该 A/B 的两臂物理相同——节点对 t2v 段丢弃
  refs,style key 从未进采样,"9v9"是同一条视频和自己比,结论作废。
  同日核查发现 r2v 全局定妆同为死通道(editMode=segment 不继承
  global.refs,ComfyUI 日志有 fallback 警告),已修复为 refs 下沉每段;
  全案见 continuity 文档第六节。

---

## 顺序与依赖

```text
M1（防护网）→ M2（技能分层）→ M3（活数据+红线）→ M4（style key / 评分）
  │                                    │ 3.3b 每段 refs ──→ 4.1 style key
  └── 每个后续里程碑的验收都引用 M1 的场景
```

M1 半天就能做完且零风险，任何时候都值得先做。M2/M3 内部任务可并行拆 PR。
M4 两项相互独立，4.1 依赖 3.3b。

---

## Comfy 官方 Day-0 博客对标(2026-08-11,任务源:blog.comfy.org/p/minimax-h3-day-0-support-in-comfyui)

官方本地模板(workflow_templates/video_minimax_h3_{t2v,i2v,r2v}.json)与本仓对照结论:

1. **采样参数与官方完全一致**:res_multistep + simple 调度 + 20 步 + denoise 1,
   同一套权重文件名(fl2va/ref2va pruned_int8_convrot + qwen3vl nvfp4 + 双 VAE)。
   我们的 t2v 工作流本就是官方核心节点栈(MiniMaxH3ImageToVideo +
   SamplerCustomAdvanced);无需调参。
2. **官方本地默认分辨率 = 0.4MP = 864x480**,与我们的 OOM 红线相同——2K 是模型
   上限不是本地推荐。官方模板 MarkdownNote 附完整分辨率阶梯(0.2MP=608x352 …
   0.98MP=1344x768 … 2.0MP=1920x1088),已抄录为活数据来源;越阶试渲走
   offload 探针,过了再动 MAX_RENDER_PIXELS。
3. **官方 r2v 走核心节点 MiniMaxH3ReferenceToVideo**(LoadImage 直连,模板给 2 个
   参考位),与我们的 Director timeline 路线不同源。我们的 Director refs 已修通
   (66ec33e)且支持分镜+9 图,维持现路线;核心节点留作单段 r2v 的备选路
   (若 Director 再出兼容问题)。ComfyUI 核心支持见 ComfyUI#15224。
4. **官方 t2v 示例提示词是"单发散文时间轴"**:一条 prompt 内写 [0s-1s][1s-2.5s]…
   硬切多镜头(亚 5 秒镜头合法,画面内文字大方使用,负面约束以散文句式附尾),
   一次采样保证镜头间连贯——不依赖任何 Director 分段。这是"≤15s 单发 vs story
   分段"A/B(见下一条落地记)的直接依据。

**散文时间轴 A/B 落地记(2026-08-11):**
同一份 3×5s 三镜头脚本、同种子:(a) t2v 单发散文时间轴 vs (b) story
Director 分段(实拆 2 块)。VLM 评分 9v9 再次触天花板(6 帧抽样只盯
人物,抓不到场景漂移),**帧证据分胜负**:(a) 三镜头同一条街、同一
人物、同一帆布包,光线从晨雾连续演进到日出;(b) 三段各长一条街,
道具丢失,跨块段连建筑风格都换了(红灯笼街)。已落地片长路由:
`_script_mode` 按原始总秒数选模式——≤max_seconds 走 t2v 单发
(seconds 用原始值,SRT 不补帧网格),超了才 story 拆块;agent 系统
提示与工具描述同步。教训并入 DECISIONS 候选:VLM 评分对"场景级"
一致性不敏感,一致性 A/B 必须附帧证据。

**VRAM offload 探针落地记(2026-08-11):**
ComfyUI 已带动态 offload(--reserve-vram 2.0 等),逐档探针:0.5MP
(960x544)242s ✓、0.7MP(1152x640)184s ✓、0.98MP(1344x768 原生档)
283s ✓,全程零 OOM;满包络反例 1344x768×15s 在 1800s 超时线被掐、
宿主内存见底。结论:内存压力 ≈ 像素×帧数,红线从单一分辨率改为乘积
预算(renderer.max_pixels_for_seconds,agent 守卫/能力表同源),短条
解锁原生 768 档(定妆照直接受益),15s 级与 story 维持 864x480。
D1 已修订。
