# 从渲染服务到创作工作台 — 重构设计

状态：阶段一已实现，其余阶段是提案。

参照对象是 MiniMax Design（本地桌面 AI Agent 创作平台）的产品形态：全链路生产、
Agent 编排、Skill 技能库、Memory 记忆、本地工作流打通。本文档把这五个方向映射到
本项目的现状，定出目标架构和分阶段落地顺序。

## 1. 现状与差距

| MiniMax Design 的能力 | 本项目现状 | 差距 |
|---|---|---|
| 全链路生产（调研→脚本→分镜→生产→交付），多项目并行 | 两个孤立单点：`POST /v1/scripts`（网址→分镜，结果不落盘）和 `POST /v1/renders`（渲染） | 没有"项目"概念，脚本一刷新就丢，流程靠人肉在表单间搬运 |
| Agent 编排：自动规划、调度模型、审阅点等人 | 全手动；`story` 模式算半自动 | 没有流水线运行器；`docs/visual-director-contract-v1.md` 已把编排的权力边界设计好了，但没有代码 |
| Skill 库：SKILL.md + meta.yaml 的可复用工作流/风格 | 风格是 config 里一个字符串（`script.style`），guidance 每次手填 | 没有具名、可分享、可切换的预设 |
| Memory：记住偏好、风格标准、历史选择 | 无 | 每次请求从零开始 |
| 本地工作流：读 PDF/Word 照计划执行、整理素材、导出剪映/DaVinci | 调研只吃 YouTube 字幕，产物只有单个 MP4 | 缺文档摄取和成片打包导出 |
| 资产中心：角色/场景/风格包跨项目复用 | 参考帧是一次性的，跟着 render 删除 | 素材没有身份，不可复用 |
| 无限画布 | 轻量控制台页面 | 完全不同的前端量级，刻意不追 |

## 2. 不变的约束

0. **一切在本机完成，不依赖云端 API。** 生产链路只有三个本地组件：yt-dlp
   （字幕）、Ollama（文本）、ComfyUI/H3（画面）。后续所有阶段——编排、资产、
   Memory、导入导出——都只编排这三样和本地文件，不引入任何云服务调用。
1. **`/health`、`/v1/validate`、`/v1/renders` 系列是冻结契约。** `videotube` 用
   `extra="forbid"` 解析这些响应，字段一个都不能加。所有新能力走新路由，响应模型
   （`RenderView`、health 字典）保持原样。
2. **GPU 一次只进一个任务、状态落盘可恢复**的执行内核不重写，新概念挂在它外面。
3. **Agent 编排必须继承 visual-director 契约的权力分离**：提案者不能批准，
   执行器只消费已批准计划，人工审阅是显式关卡。MiniMax Design 的"全自动"哲学
   在这一点上刻意不抄。

## 3. 目标概念模型

新增三个聚合，全部落盘为版本化 JSON，和 render 记录同一套原子写风格：

- **Project（项目）**：一次创作的容器。持有调研来源、脚本草稿（版本化）、关联的
  render_id 列表和备注。目录 `work/.projects/<project_id>/project.json`——
  `.projects` 不是合法 render_id（含点号），永远不会和渲染目录相撞。
- **Skill（技能）**：`skills/<id>/` 下两个文件，照抄 MiniMax Design 的结构：
  - `SKILL.md`：给模型读的"说明书"，Markdown，写创作规范、审美标准、流程要求，
    整体注入总结 Prompt；
  - `meta.yaml`：身份证——名称、描述、分类，以及可选的默认参数
    （style、target_seconds、shot_seconds、max_shots、output_language）。
  优先级：请求显式字段 > Skill 默认 > config 默认。
- **Memory（偏好，阶段四）**：`work/memory.json`，键值化的偏好条目（常用引导词、
  风格纠正），可查看可删除，注入总结 Prompt 的"额外要求"段。

## 4. 分阶段计划

### 阶段一（本次实现）：项目 + Skill 骨架

- `videogen_service/skills.py`：SkillLibrary，启动时扫描 `skills/`，坏的 Skill
  只警告不拦启动；`GET /v1/skills` 列出。
- `videogen_service/projects.py`：ProjectStore；`/v1/projects` 增删查，
  `POST /v1/projects/{id}/renders` 关联渲染。
- `ScriptRequest` 增加可选 `skill` 与 `project_id`：带 skill 就套用其默认值并注入
  SKILL.md；带 project_id 就把生成结果自动存成该项目的一版草稿。
- 生成台页面：项目选择/新建、Skill 下拉、草稿列表回填、提交渲染自动挂进项目。

### 阶段二（已实现）：Agent 编排

流水线运行器把项目推着走：取字幕 → 出脚本 →（人工审阅关卡）→ 提交渲染 →
收产物。`videogen_service/pipeline.py`：QUEUED → SCRIPTING → AWAITING_REVIEW
→ RENDERING → DONE/REJECTED/FAILED，落盘 `work/.projects/<id>/pipeline.json`，
每项目一条活动流水线；脚本阶段在工作线程执行，渲染阶段委托渲染队列并在读取时
懒同步终态，重启恢复与渲染记录同风格。审阅批准可携带改过的分镜（记
`prompt_overridden`），驳回记原因。visual-director 契约的 Director Brief /
Visual Proposal / 裁决对象是后续增强：确定性 Proposal Builder 先行，模型排序
走 shadow 期。

### 阶段三（已实现）：资产中心

参考帧、角色/场景/风格包成为一等公民：`videogen_service/assets.py`，
`work/.assets/<asset_id>/{asset.json,media.*}`，具名、分类、跨项目引用。
render 的参考帧可以"存为资产"（`POST /v1/assets/from-render`），新任务从资产
选图而不是每次上传（`/v1/renders` 的可选 `first_frame_asset`/`last_frame_asset`
表单字段，上传文件优先）。提交时资产字节拷进渲染目录，渲染自包含，删资产
不碰历史。

### 阶段四（已实现）：Memory

`videogen_service/memory.py`：`work/memory.json` 的显式偏好存储，
`/v1/memory` 增删查；起稿时注入总结 Prompt 的"长期偏好"段（只取最近 20 条），
优先级低于 Skill 与单次 guidance。不做静默学习，条目全部可见可删。

### 阶段五：本地工作流

- 摄取：PDF/Word/纯文本作为调研来源（`/v1/scripts` 的输入从"仅 YouTube URL"
  扩展为可上传文档）；
- 导出：项目打包（成片 MP4 + narration 生成的 SRT + 分镜元数据 JSON），
  之后再评估剪映草稿 / EDL 格式。

### 刻意不做

- 无限画布、多模型商店式接入（本项目只有 ComfyUI/H3 一条生产路径 + Ollama）；
- 绕过人工审阅的全自动发布——与 visual-director 契约冲突；
- 往冻结契约里加字段。

## 5. 阶段一 API 摘要

| 路由 | 作用 |
|---|---|
| `GET /v1/skills` | 列出可用 Skill（id、名称、描述、分类、默认参数） |
| `GET /v1/projects` | 项目摘要列表（新→旧） |
| `POST /v1/projects` | 创建项目（可自带 project_id，缺省服务端生成） |
| `GET /v1/projects/{id}` | 项目详情：草稿全文 + 关联渲染 |
| `DELETE /v1/projects/{id}` | 删除项目（不动已关联的渲染） |
| `POST /v1/projects/{id}/renders` | 把一条 render_id 挂进项目 |
| `POST /v1/scripts`（扩展） | 新增可选 `skill`、`project_id` 字段 |

`/v1/scripts` 不属于冻结契约（`videotube` 不调它），扩展是安全的。
