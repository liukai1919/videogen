# VideoTube Videogen

VideoTube Studio 的独立本地渲染服务。它拥有 MiniMax H3、ComfyUI workflow、参考帧副本、渲染进度和渲染产物；原来的 `videotube` 项目继续拥有自动化流水线、用户任务、预览副本和 B 站投稿；视频生成台这一页已经搬到本项目。

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

反过来不用管:WSL 里监听 `127.0.0.1:8020` 的服务,Windows 的浏览器直接就能打开。

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

先启动 ComfyUI,再启动本服务。服务起来后直接打开 `http://127.0.0.1:8020/` 就是视频生成台。

## 配置归属

- `config.yaml` 的 `comfyui`、`renderer`、`ollama`、`script`、`modes` 只属于这个项目。
- 四份 H3 API workflow 位于 `workflows/`，节点指针也只在这个项目配置。
- 原项目只配置 `videogen.service_url`、等待时间、本地预览目录和 B 站投稿声明。
- `story` 依赖 `ComfyUI_MiniMaxH3_Director`；不使用时可从本项目配置中删掉该模式。

## HTTP seam

- `GET /`、`GET /static/*`：视频生成台页面和它的三个静态文件。
- `GET /health`：模式、分镜模式和限制。字段是和 `videotube` 之间的固定契约（那边用 `extra="forbid"` 解析），新东西一律不要往里加。
- `GET /v1/scripts/config`：字幕总结的配置，页面用它决定默认时长和分镜数。
- `POST /v1/scripts`：传一个 YouTube 网址，取字幕、让 Ollama 总结成分镜脚本。可选 `skill` 套用一份具名创作规范，可选 `project_id` 把结果自动存成项目草稿。
- `GET /v1/skills`：列出 `skills/` 里的创作预设。
- `GET/POST /v1/projects`、`GET/DELETE /v1/projects/{id}`、`POST /v1/projects/{id}/renders`：项目工作区——脚本草稿和渲染的归档容器，见下文。
- `GET /v1/renders`：列出全部渲染，带上当初提交的参数，页面的任务列表用它。
- `POST /v1/validate`：在创建用户任务前校验模式、参考图和分镜，并返回对齐后的真实时长。
- `POST /v1/renders`：以 `render_id` 幂等提交渲染。
- `GET /v1/renders/{render_id}`：读取队列、进度或失败原因。
- `GET /v1/renders/{render_id}/media`：下载完成的 MP4。
- `POST /v1/renders/{render_id}/retry`：用磁盘上的原请求重排一条失败的渲染。
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

每段分镜的时长会被夹到 `renderer.min_seconds`/`max_seconds` 之间，段数和总时长按 `script`（或请求里的 `max_shots`、`target_seconds`）截断，所以模型跑偏也不会提交一份渲染器不接受的脚本。

这一步只花 CPU 和 Ollama，不碰 GPU；要不要渲染、渲染几次仍然由原项目决定。取不到字幕、Ollama 没启动这类上游问题返回 `503`，链接不是 YouTube、模型没给出分镜这类返回 `400`。需要登录态或代理才能取字幕时，填 `script.cookies_file` 和 `script.proxy`。

服务一次只让一个任务进入 GPU。状态以版本明确的 JSON 落在 `work/<render_id>/`；进程重启后，未完成任务会转为可重试的失败态，不会伪装成仍在运行。

## 项目与 Skill

这两个概念把孤立的"出脚本"和"出视频"串成一条可回溯的流程（设计与后续阶段见 `docs/refactor-design.md`）：

- **项目**：一次创作的容器，落盘在 `work/.projects/<project_id>/project.json`。页面顶部选中一个项目后，生成的分镜脚本自动存成一版草稿（可随时回填表单重渲），提交的渲染自动挂进项目。删除项目不动已关联的渲染。
- **Skill**：`skills/` 下每个子目录一份具名创作规范，结构是两个文件——`SKILL.md`（给模型读的说明书，整体注入总结 Prompt）和 `meta.yaml`（名称、描述、分类和可选默认参数 style/target_seconds/shot_seconds/max_shots/output_language）。请求里显式给的字段永远压过 Skill 默认值，Skill 默认值压过 config 默认值；`额外要求` 排在 Skill 规范之后，单次覆盖仍然可行。改动 Skill 文件立即生效，不用重启服务；写坏的 Skill 只会自己下线并留一条警告。仓库自带 `skills/science-doc` 作为样例。

## 验证

```bash
python -m pytest -q
python -m mypy videogen_service
```
