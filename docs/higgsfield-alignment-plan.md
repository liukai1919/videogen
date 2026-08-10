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

## M4 · 生产新能力：style key + 成片评分闭环（实验性，各 2–3 人日）

| # | 任务 | 说明 |
|---|---|---|
| 4.1 | 全片 style key：项目级"风格钥匙图"——ComfyUI 生成一张风格基准图存入资产中心；分镜的每段渲染把它作为风格参考输入（r2v 已有定妆锁脸通道，style key 走同机制的另一个参考槽位，依赖 3.3b 的每段 refs）。用 eval 做 A/B：帧接力 vs 帧接力+style key 的跨段色调一致性 | Higgsfield video-explainer 已验证"一张风格图挂全片每个 clip"可行 |
| 4.2 | 成片评分闭环：新增 `review_video` 能力——本地多模态模型（Ollama 拉一个 VLM）抽帧打分：开场 hook 强度、跨段一致性、画面-旁白对齐；分数写进成片资产元数据，工作台成片卡片展示，作为 Release Review 的参考输入（不替代人审） | 对标 Virality Predictor，但全本地。走通用任务队列，抽帧用 FFmpeg（CPU，不占 GPU 闸门；VLM 推理走 GPU 闸门排队） |

验收：4.1——同一脚本两种配置各出一片，eval 打分可见一致性差异；4.2——每个成片自动带一份评分卡，Release Review 页可见。

---

## 顺序与依赖

```text
M1（防护网）→ M2（技能分层）→ M3（活数据+红线）→ M4（style key / 评分）
  │                                    │ 3.3b 每段 refs ──→ 4.1 style key
  └── 每个后续里程碑的验收都引用 M1 的场景
```

M1 半天就能做完且零风险，任何时候都值得先做。M2/M3 内部任务可并行拆 PR。
M4 两项相互独立，4.1 依赖 3.3b。
