# VideoTube 视觉生产

这个限界上下文把有证据支撑的旁白转化为可审计的连续画面，并明确分开创意建议、策略控制、媒体执行和发布批准。

## 统一语言

**脚本 Beat（Script Beat）**：
具有单一叙事角色，并与 Claim 和 Evidence 显式关联的旁白语义单元。
_避免使用_：段落、场景

**镜头（Shot）**：
与真实旁白时间轴对齐的一段连续视觉区间。一个 Shot 只属于一个 Script Beat，一个 Script Beat 可以包含多个 Shot。
_避免使用_：Clip、Segment

**导演简报（Director Brief）**：
针对一个 Shot 提供给视觉导演的可信上下文，包括旁白、证据边界、时间、相邻画面和生产能力；它刻意不包含使用来源媒体的授权权力。
_避免使用_：Prompt、Request

**视觉导演（Visual Director）**：
为 Shot 提议表达方式的顾问系统。它只有提案权，不能授权、执行渲染或批准发布。
_避免使用_：Router、Executor

**视觉提案（Visual Proposal）**：
针对一个 Shot 的不可信创意建议，包含一组有序的 Media Candidate，以及认识论表达和连续性意图。
_避免使用_：Decision、Render Job

**媒体候选（Media Candidate）**：
实现一个 Visual Proposal 的某种备选方式，并与同一提案中的其他候选一起排序。候选只描述意图和可信引用，不可直接执行。
_避免使用_：Asset、Output

**视觉裁决（Visual Adjudication）**：
规则引擎依据权利、证据、时长、能力、预算和审核策略，对每个 Media Candidate 形成的可审计评估。
_避免使用_：AI Decision、Moderation

**已批准视觉计划（Approved Visual Plan）**：
从 Visual Adjudication 产生的唯一策略批准指令。只有 Approved Visual Plan 才能编译成适配器任务。
_避免使用_：Visual Proposal、Prompt

**媒体任务（Media Task）**：
由 Approved Visual Plan 生成、面向具体适配器的执行单元，例如选择来源片段、生成图片、渲染程序化图形或生成视频。
_避免使用_：Visual Proposal、Shot

**策略审核（Policy Review）**：
候选没有明确的不安全因素，但确定性规则无法有把握批准时，在执行前进行的人工判断。
_避免使用_：Release Review

**发布审核（Release Review）**：
对合成视频的审美和发布资格进行最终人工批准。即使所有 Shot 都已通过自动裁决，这一步仍然必需。
_避免使用_：Policy Review

**证据绑定（Evidence Binding）**：
把提议中的视觉断言引用到可信 Claim、Evidence 或具名数据项的关系。它携带来源链，而不是复制或虚构数值。
_避免使用_：Citation Text

**认识论表达（Epistemic Treatment）**：
画面保持 Claim 已知确定性的方式，包括采用写实、示意或隐喻表达，以及画面不得暗示的内容。
_避免使用_：Art Style

**连续性意图（Continuity Intent）**：
一个 Shot 通过主体、构图、景别、色板、运动或显式连续性键，与相邻 Shot 保持的视觉关系。
_避免使用_：Transition Effect
