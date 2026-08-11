# VideoTube Videogen

本地通用视频生成平台：Agent 对话编排本机模型出完整成片——Ollama 写脚本、ComfyUI 出图、MiniMax H3 出视频、本地 TTS 配音、FFmpeg 合成，全程不依赖云端 API（云端 CLI 可选接入，但只当推理大脑，执行权永远留在本机）。v2 路线（`docs/refactor-design.md`）的通用任务底座、Agent 对话核心、三区工作台、配音与成片四个阶段均已实现；画布进阶（节点/连线）是唯一未完成的阶段。

它同时仍是 VideoTube Studio 的独立本地渲染服务。它拥有 MiniMax H3、ComfyUI workflow、参考帧副本、渲染进度和渲染产物；原来的 `videotube` 项目继续拥有自动化流水线、用户任务、预览副本和 B 站投稿；视频生成台这一页已经搬到本项目。

```text
浏览器 → 本服务:8020 → ComfyUI:8188
              ↑
   videotube:8000 ─┘（流水线的渲染，仍走同一条 /v1 seam）
              └────→ B 站投稿
```

浏览器现在直接对着本服务：手工生成的那条路不再经过 `videotube`。`videotube` 的自动化流水线还是老样子——提交一个远端 render，轮询状态，完成后把 MP4 下载回自己的 `work/videogen/`，所以它的任务记录和发布流程都不用改。`/health`、`/v1/validate`、`/v1/renders` 这几个它在用的字段因此是冻结的契约。

## 启动

一条命令就够,虚拟环境、依赖和 `config.yaml` 都是缺什么补什么,已经就位就直接启动:

Windows PowerShell:

```powershell
cd D:\vediotube-videogen
.\start.ps1
```

在 WSL 里跑(显卡在 Windows 上也可以,见下面的网络那段):

```powershell
cd D:\vediotube-videogen
.\start-wsl.ps1
```

双击 `start-wsl.cmd` 效果一样,它只是替你绕开 PowerShell 的执行策略。已经在 WSL 终端里的话就是 `./start.sh`。

`start-wsl.ps1` 替你处理了两件事:把仓库的 Windows 路径翻成 WSL 路径(用 `wslpath`,不用手写 `/mnt/d/...`),以及把 `start.sh` 去掉 CR 再喂给 bash —— 这个仓库被两个平台共用,Windows 上的 checkout 很可能是 CRLF,而 CRLF 的脚本在 bash 里只会报一句看不出病因的 `$'\r': command not found`。它不改动文件,也不要求文件有可执行位。

虚拟环境在 WSL 下会建成 `.wsl-venv`,和 Windows 那份 `.venv` 并存,不会互相覆盖成对方平台的解释器。

WSL 里还有一件事要注意:**默认的 NAT 网络下,WSL 里的 `127.0.0.1` 是 WSL 自己**,不是 Windows。ComfyUI 和 Ollama 通常跑在 Windows 上(显卡在那边),所以 `config.yaml` 里的 `comfyui.base_url`、`ollama.base_url` 要改成 Windows 主机的 IP;自检连不上时会把这个 IP 直接算给你。或者在 `%UserProfile%\.wslconfig` 里开镜像网络,`127.0.0.1` 两边就通了:

```ini
[wsl2]
networkingMode=mirrored
```

反过来不用管:WSL 里监听 `127.0.0.1:8020` 的服务,Windows 的浏览器直接就能打开。ComfyUI 若也跑在 WSL 内,给它留足内存——H3 单次渲染的模型腾挪窗口实测峰值超过 30GB,`.wslconfig` 的 `memory` 上限太紧会被内核 OOM 杀掉(排查过程见 `docs/comfyui-oom-rootcause.md`)。

启动前会先自检,把这台机器上真正会出问题的地方一次说清楚:

```text
✓ 服务地址        http://127.0.0.1:8020/  ← 生成台就在这个地址
✓ 工作目录        D:\vediotube-videogen\work
✓ t2v workflow    minimax_h3_t2v.json
! ComfyUI         http://127.0.0.1:8188 连不上；先启动它，否则渲染会立刻失败
✓ Ollama          http://127.0.0.1:11434 · qwen3.6:27b
✓ yt-dlp          2026.07.04
```

`✗` 是启动不了的问题(workflow 存成了界面格式、节点指针对不上、工作目录不可写),服务会停下并退出码 1。`!` 只是提醒:ComfyUI 和 Ollama 通常是后启动的,不挡着服务先跑起来。

常用参数:

| | PowerShell | bash |
| --- | --- | --- |
| 只自检不启动 | `.\start.ps1 -Check` | `./start.sh --check` |
| 换端口 | `.\start.ps1 -Port 8030` | `./start.sh --port 8030` |
| 换配置 | `.\start.ps1 -Config other.yaml` | `./start.sh --config other.yaml` |
| 强制重装依赖 | `.\start.ps1 -Reinstall` | `./start.sh --reinstall` |

改过 `pyproject.toml` 后脚本会自己重装依赖,平时启动不会为此变慢。

想手工来也可以,脚本做的就是这几步:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item config.example.yaml config.yaml
.\.venv\Scripts\videogen-service.exe --config config.yaml
```

先启动 ComfyUI,再启动本服务。服务起来后直接打开 `http://127.0.0.1:8020/` 就是视频生成台,`http://127.0.0.1:8020/workspace` 是 Agent 工作台。

## 配置归属

- `config.yaml` 的 `comfyui`、`renderer`、`ollama`、`script`、`modes` 只属于这个项目。
- 四份 H3 API workflow 位于 `workflows/`，节点指针也只在这个项目配置。
- 原项目只配置 `videogen.service_url`、等待时间、本地预览目录和 B 站投稿声明。
- `story` 依赖 `ComfyUI_MiniMaxH3_Director`；不使用时可从本项目配置中删掉该模式。

## 工作台

`http://127.0.0.1:8020/workspace` 是平台的主界面，MiniMax Design 式的三区布局：左侧项目/Skill/资产中心，中间产物区（Agent 排的图片、配音、视频任务和手动渲染合成一条时间流，图片可直接存资产），右侧 Agent 对话（新建会话、发消息，工具调用和结果内联展示，长渲染不卡对话——Agent 只排队并报任务 id，产物区自动刷新）。原来的手动生成台还在 `/`，两边互通同一批数据。

## Agent

`POST /v1/chats/{id}/messages` 驱动一个 tool-calling 循环，工具即平台能力：

| 工具 | 做什么 |
|---|---|
| `write_storyboard` | 想法/资料 → 带 `[0s-6s]` 时间轴的分镜脚本；可选 `skill`、`style` |
| `generate_video` | 排一条 H3 渲染（t2v/i2v/flf2v/r2v/story），立即返回 `render_id` |
| `generate_image` / `generate_speech` | 排图片/配音任务，立即返回 `job_id` |
| `compose_video` | 渲染 + 配音 + 字幕 → FFmpeg 出成片，可放大到 1080p |
| `review_video` | 本地 VLM 给成片打质量分（见下） |
| `save_render_frame_as_asset` | 渲染首/尾帧存进资产中心（定妆照、风格钥匙的生产入口） |
| `check_render` / `check_job` | 查状态 |
| `cancel_render` | 取消排队中或渲染中的任务 |
| `wait_render` / `wait_job` | 阻塞等到终态；只给批处理/外部 worker 用，对话内用 `check_*` |
| `inspect_failure` | 失败诊断：报错原文 + 提交参数 + 工作流配置 + ComfyUI 连通性 |
| `list_capabilities` / `list_assets` | 查看本机能力表 / 资产中心 |
| `read_skill_reference` | 按需读某个 Skill 的参考文档 |
| `save_memory` | 用户明确要求时记住长期偏好 |

默认大脑是本地 Ollama（原生 tool calls）；请求带 `agent: claude/codex` 时**同一套循环由云端 CLI 驱动**——工具调用走 fenced-JSON 文本协议，云端只出决策，执行永远在本机（见下文「外部 Agent worker」）。生成类工具只排队并立即返回任务 id，长渲染不会卡住对话；渲染启动前若本地大脑正在生成，会先等它说完这句话再卸载模型让出显存（`BrainGate`）。Memory 偏好自动注入系统提示；会话落盘 `work/.chats/`。

## HTTP seam

- `GET /`、`GET /workspace`、`GET /static/*`：手动生成台、Agent 工作台和它们的静态文件。
- `GET /health`：模式、分镜模式和限制。字段是和 `videotube` 之间的固定契约（那边用 `extra="forbid"` 解析），新东西一律不要往里加。
- `GET /v1/scripts/config`：字幕总结的配置，页面用它决定默认时长和分镜数。
- `POST /v1/scripts`：传一个 YouTube 网址，取字幕、让 Ollama 总结成分镜脚本。可选 `skill` 套用一份具名创作规范，可选 `style` 表达气质（无 Skill 命中时兜底为中性电影感），可选 `project_id` 把结果自动存成项目草稿。
- `GET /v1/skills`：列出 `skills/` 里的创作预设，含路由三件套（`use_when`/`not_for`/`chain`）与 `references` 文件名列表。
- `GET/POST /v1/projects`、`GET/DELETE /v1/projects/{id}`、`POST /v1/projects/{id}/renders`：项目工作区——脚本草稿和渲染的归档容器，见下文。
- `POST/GET/DELETE /v1/projects/{id}/pipeline` 及 `.../pipeline/{approve,reject,retry}`：自动流水线，见下文。
- `GET/POST /v1/assets`、`POST /v1/assets/from-render`、`GET /v1/assets/{id}/media`、`DELETE /v1/assets/{id}`：资产中心，见下文。
- `GET/POST /v1/memory`、`DELETE /v1/memory/{entry_id}`：长期创作偏好，每次起稿自动注入总结 Prompt。
- `POST/GET /v1/chats`、`GET/DELETE /v1/chats/{id}`、`POST /v1/chats/{id}/messages`：Agent 对话，见上文。
- `GET /v1/capabilities`：平台能力目录——H3 视频各模式（提交走 `/v1/renders`，附参数事实表：分段/总长界限、像素红线、是否接参考图/段级参考图，与实际校验逻辑同源）加上 config `capabilities` 里声明的本地能力（文生图、TTS，提交走 `/v1/jobs`），以及内建的 `compose`（合成）、`review`（评分）、`whiteboard`（白板手绘动画）三个能力。
- `POST/GET /v1/jobs`、`GET /v1/jobs/{id}`、`GET /v1/jobs/{id}/media`、`POST /v1/jobs/{id}/retry`、`DELETE /v1/jobs/{id}`、`POST /v1/jobs/{id}/save-asset`：通用生成任务队列。t2i 走 ComfyUI workflow（和渲染同一套节点指针机制），tts 走 config 里的本地命令模板，内建 `compose` 能力用 FFmpeg 合成成片，内建 `review` 能力抽帧送本地 VLM 打分（见下），内建 `whiteboard` 能力把线稿资产按标注逐笔绘制成手绘动画（纯 CPU，见 `docs/whiteboard-animation.md`）；`needs_gpu` 的任务和 H3 渲染共享一把 GPU 锁轮流上卡，图片产物可一键存进资产中心。
- `POST /v1/scripts/document`：上传本地 PDF/Word/文本，本机抽文本后走同一条 Ollama 总结路径生成分镜。
- `GET /v1/projects/{id}/export`：项目打包下载——成片 MP4、每稿分镜文本与解说词、按渲染时间轴对齐的 SRT、元数据清单。
- `POST /v1/renders` 新增可选表单字段 `first_frame_asset`/`last_frame_asset`/`segment_ref_assets`：用资产代替上传参考图（后者是段级参考，见「风格钥匙」）；`videotube` 不发这几个字段，冻结契约不受影响，上传文件优先于资产。
- `GET /v1/renders`：列出全部渲染，带上当初提交的参数，页面的任务列表用它。
- `POST /v1/validate`：在创建用户任务前校验模式、参考图和分镜，并返回对齐后的真实时长。
- `POST /v1/renders`：以 `render_id` 幂等提交渲染。
- `GET /v1/renders/{render_id}`：读取队列、进度或失败原因。
- `GET /v1/renders/{render_id}/media`：下载完成的 MP4。
- `POST /v1/renders/{render_id}/retry`：用磁盘上的原请求重排一条失败的渲染。
- `POST /v1/renders/{render_id}/cancel`：取消排队中或渲染中的渲染（终态沿用 `FAILED` + 可读文案，不给冻结契约引入新状态值）。
- `DELETE /v1/renders/{render_id}`：删除终态渲染及其参考帧。

## 视频生成台

生成台的前端从 `videotube` 搬了过来，现在住在 `videogen_service/static/`：页面、样式和一份精简过的 `ui.js`，本服务直接把它们发出去。搬过来的只有跟视频生成有关的那一页——仪表盘、发布审核、B 站投稿的模板和样式都留在原项目，投稿仍旧是那边的事。

前端因此直接说本服务的 `/v1` 语言，不再经过控制台转发：任务 id 由页面自己生成，任务列表读 `GET /v1/renders`，失败重试打 `POST /v1/renders/{id}/retry`，视频从 `/v1/renders/{id}/media` 播。`videotube` 那份 `/videogen` 页面和它的 `/api/videogen/*` 路由现在是重复的，要不要下掉由那个项目自己决定；两边同时开着也不会打架，任务状态只有本服务这一份。

## 原创科普：从 YouTube 网址到分镜

`POST /v1/scripts` 把一条 YouTube 链接变成可以直接提交渲染的分镜脚本，分两步走，中间留出人工审阅。页面左上角的"从 YouTube 网址起稿"就是它，命令行等价物：

```bash
curl -s http://127.0.0.1:8020/v1/scripts \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://youtu.be/dQw4w9WgXcQ", "target_seconds": 60, "guidance": "面向初中生"}'
```

服务依次做三件事：yt-dlp 按 `script.subtitle_languages` 的顺序取字幕（人工字幕优先于自动字幕），把字幕截到 `script.max_transcript_chars` 后交给 Ollama，再把模型返回的 JSON 收敛成本服务自己的分镜格式。返回里 `prompt` 是拼好的 `[0s-6s] …` 分镜文本，`mode` 是 `story`，`seconds` 是按 H3 长度步长对齐后的真实总时长——三个字段原样填进 `POST /v1/renders` 即可开始生成，`shots[].narration` 是给配音留的解说词，不参与画面。

每段分镜的时长会被夹到 `renderer.min_seconds`/`max_seconds` 之间，段数按 `script`（或请求里的 `max_shots`）截断，总时长在预算内做就近适配（超一点比缺一镜更接近目标时收下最后一镜，硬上限 `renderer.max_total_seconds` 永不越），所以模型跑偏也不会提交一份渲染器不接受的脚本，也不会因为量化取整系统性偏短。

这一步只花 CPU 和 Ollama，不碰 GPU；要不要渲染、渲染几次仍然由原项目决定。取不到字幕、Ollama 没启动这类上游问题返回 `503`，链接不是 YouTube、模型没给出分镜这类返回 `400`。需要登录态或代理才能取字幕时，填 `script.cookies_file` 和 `script.proxy`。

服务一次只让一个任务进入 GPU。状态以版本明确的 JSON 落在 `work/<render_id>/`；进程重启后，未完成任务会转为可重试的失败态，不会伪装成仍在运行。长分镜（超过单条渲染安全包络）自动拆成多块串行渲染再无缝拼接；时间轴里 `[Xs-Ys续]` 标记的镜头（上一镜同一动作的直接延续）落在块边界时会用上一块最后一帧做 i2v 锚点接力，动作不断线。

## 自动流水线

选中项目后，生成台的"自动流水线"面板可以一键跑完起稿到渲染，编排的全是本机组件，不依赖任何云端 API：

```text
QUEUED → SCRIPTING（yt-dlp 取字幕 + Ollama 出脚本,自动存为项目草稿）
       → AWAITING_REVIEW（硬关卡:草稿填进表单,等人批准或驳回）
       → RENDERING（批准后进 ComfyUI 渲染队列,渲染挂进项目）
       → DONE / REJECTED / FAILED（失败可重试:脚本阶段重跑,渲染阶段重排原渲染）
```

审阅是刻意保留的人工关卡：流水线只有提案权，批准才花显卡——和 `docs/visual-director-contract-v1.md` 的权力分离一致。批准前可以直接在提示词框里改分镜，改过的版本进渲染，存档草稿保持原样并记下 `prompt_overridden`。状态落在 `work/.projects/<id>/pipeline.json`，每个项目同时只有一条；服务重启后，脚本阶段被打断的流水线转成可重试的失败态，渲染阶段的靠渲染队列自己的恢复结果收敛。

## 本地文档与导出

调研入口不再只有 YouTube：`POST /v1/scripts/document` 接本地 PDF、Word（.docx）、纯文本和 Markdown，pypdf / python-docx 在本机抽文本，之后与字幕走完全相同的总结路径（Skill、Memory、guidance 都生效），结果同样可存为项目草稿。扫描件 PDF 提取不到文字会明确报错。

出口是项目打包：`GET /v1/projects/{id}/export` 下载一个 zip——`renders/` 里是完成的 MP4，`drafts/draft-NN/` 里是分镜文本、解说词和 `narration.srt`（字幕时间按 H3 帧对齐后的真实渲染时长计算），`project.json` 是清单。MP4+SRT 是剪映 / DaVinci / Premiere 都直接吃的通用格式；未完成的渲染记为 skipped，不挡导出。

## Memory 偏好

`work/memory.json` 存用户显式要求记住的长期偏好（"以后旁白都用口语"），手动或流水线起稿时作为"长期偏好"段注入 Ollama 的总结 Prompt，排在 Skill 规范和单次"额外要求"之前——所以预设和临时指示仍能压过它。只记明确要求的，条目全部可见、随时可删，没有静默学习；注入只取最近 20 条，防止挤占字幕上下文。

## 资产中心

参考图从一次性的上传变成有身份的素材：`work/.assets/<asset_id>/` 存一张图和它的名字、分类（角色、场景、风格包、道具……）。生成台的首尾帧字段可以直接从资产下拉里选；任务卡的「⋯」菜单能把当初提交的参考帧存为资产，Agent 的 `save_render_frame_as_asset` 工具则能把任意一条**已完成渲染**的首帧或尾帧存成资产（没有原始上传帧时会从成片现抽一帧）——这是定妆照、风格钥匙的标准生产路径。提交渲染时服务端把资产字节拷进渲染目录——渲染保持自包含，之后删资产不影响任何历史任务。

## 项目与 Skill

这两个概念把孤立的"出脚本"和"出视频"串成一条可回溯的流程（设计与后续阶段见 `docs/refactor-design.md`）：

- **项目**：一次创作的容器，落盘在 `work/.projects/<project_id>/project.json`。页面顶部选中一个项目后，生成的分镜脚本自动存成一版草稿（可随时回填表单重渲），提交的渲染自动挂进项目。删除项目不动已关联的渲染。
- **Skill**：`skills/` 下每个子目录一份具名创作规范，结构是 `SKILL.md`（给模型读的说明书）、`meta.yaml`（名称、描述、路由信息、默认参数）和可选的 `references/*.md`（细则文档，按需读取，不常驻提示词）。`meta.yaml` 除 `name`/`description`/`category`/`defaults`（style/target_seconds/shot_seconds/max_shots/output_language）外，还带路由三件套：`use_when`（触发场景列表）、`not_for`（边界，写明该转介去哪）、`chain`（与哪些 Skill 配合）。Agent 系统提示只注入这份路由表（id + 描述 + 适用/不适用 + 参考文档名），选中某个 Skill 后 `SKILL.md` 全文才进编剧提示词，需要细则时用 `read_skill_reference` 按需读——两级注入让技能库能长而不臃肿。请求里显式给的字段永远压过 Skill 默认值，Skill 默认值压过 config 默认值；`额外要求`/`style` 排在 Skill 规范之后，单次覆盖仍然可行。改动 Skill 文件立即生效，不用重启服务；写坏的 Skill 只会自己下线并留一条警告。仓库自带 `skills/concept-trailer`、`skills/science-doc` 两份样例。

## 风格钥匙与成片评分

两个建在通用能力之上的玩法，帮 Agent 把"能出片"往"出得稳、出得好"再推一步：

- **风格钥匙**：从任意渲染存一张好画面成资产（`save_render_frame_as_asset`），再把它的 `asset_id` 传给下一次 `POST /v1/renders`（或 `generate_video` 工具）的 `segment_ref_assets`——它会挂到 story 分镜**每一段**的参考图上，锁定全片色调质感（和 `ref_assets` 的 r2v 身份锁脸是同一机制的不同用法：一个锁角色，一个锁风格）。段级参考只在分镜模式生效，最多 9 张。
- **成片评分**：`review_video` 工具（对内是内建能力 `review`）用 FFmpeg 从成片均匀抽 6 帧（CPU，不占 GPU 闸门），连同分镜原文一起送本地 VLM（`ollama.model`，本机实测 qwen3.6:27b 具备原生视觉能力），打 `hook`（开场吸引力）、`consistency`（跨段一致性）、`visual_quality`（画面质量）、`alignment`（与分镜意图吻合度）四项 1-10 分，附一句话评语。排队即返回 `job_id`，`check_job` 在任务 `DONE` 时直接把分数带回来。给 Release Review 当参考输入，不替代人审。

## 外部 Agent worker(可选)

`videogen_service/agents/` 把云端编码/推理 CLI(目前 Claude Code、Codex CLI)接成统一的 worker 层:云端 Agent 是**推理层**,本服务是**编排层**,显卡 + ComfyUI + FFmpeg 是**执行层**——显卡只留给视频/图片,不再被通用 LLM 占着。工作台对话里选中某个云端 Agent 后,它就是这一轮的大脑:走和本地 Ollama 完全相同的工具循环(见上文「Agent」的工具表),工具调用由 CLI 以 fenced-JSON 文本协议给出,本服务解析并执行,CLI 自身保持只读沙箱。`wait_render`/`wait_job` 是特地给这层准备的阻塞式工具——批处理场景一步拿终态,不用像对话那样排队查询。

在 `config.yaml` 的 `agents:` 里按名字启用(见 `config.example.yaml` 的注释样例),`agent_defaults:` 定 primary 和 fallback 链。业务代码永远不直接拼 subprocess,只经 `AgentManager`:

```python
from videogen_service.agents import build_manager

manager = build_manager(config)
result = await manager.run(agent="claude", task="分析这条渲染流水线")
result = await manager.run_with_fallback(task="…")   # primary 挂了自动换下家
```

每个 adapter 都是 headless 调用(prompt 走 stdin,Claude 用 `--output-format json`,Codex 用 `exec --output-last-message`),默认只读沙箱、限死在 `work/.agents/workspace`、带硬超时,超时/取消会连子进程树一起杀。某个 CLI 没装只是健康检查报 false(`GET /v1/agents` 可查,启动自检也有一行),fallback 链自动跳过,绝不拖垮本地渲染。本地 Ollama 那套照旧保留。

## 评测体系

Agent 的行为定义在提示词和 SKILL.md 这样的普通文本里，文本会漂移；`pytest` 只保证代码路径不炸，保证不了模型会不会选对模式、会不会漏标一个 `续`。`evals/` 是给模型参与的决策链单独准备的回归网：

- `evals/scenarios.md`：10 个"用户说什么 → 期望的工具编排 → 通过/部分/不通过判分标准"的场景，覆盖成片全链路、长片拆链、跨段一致性、身份锁定、红线守卫、技能路由、失败诊断、异步纪律等。
- `evals/README.md`：怎么跑一轮——每个场景开新会话、原文照贴、观察工具调用序列；回归阈值：总分降 >15% 或耗时 >2× 视为回归。
- `evals/DECISIONS.md`：钉住的默认值台账，每条带结论/证据/推翻条件——想改就得带新一轮数据来，不能空口驳回。
- `evals/rounds.md`：轮次留痕，含每场景评语和失败样态记录，是排查"这个行为什么时候开始跑偏"的第一站。

## 验证

```bash
python -m pytest -q
python -m mypy videogen_service
```

真实 CLI 集成测试默认跳过(会耗订阅额度),要跑:`AGENT_CLI_TESTS=1 python -m pytest tests/test_agents.py`。`pytest`/`mypy` 保证代码正确，`evals/`（见上）保证 Agent 用得对——两者互补，缺一不可。
