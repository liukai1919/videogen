# VideoTube Videogen

VideoTube Studio 的独立本地渲染服务。它拥有 MiniMax H3、ComfyUI workflow、参考帧副本、渲染进度和渲染产物；原来的 `vediotube` 项目继续拥有网页、用户任务、预览副本和 B 站投稿。

```text
浏览器 → vediotube:8000 → 本服务:8020 → ComfyUI:8188
                └────────→ B 站投稿
```

这个边界刻意保持同步：原项目提交一个远端 render，轮询状态，完成后把 MP4 下载回自己的 `work/videogen/`。因此现有前端 URL、SQLite 任务记录和发布流程都不用改。

## 安装

Windows PowerShell：

```powershell
cd D:\vediotube-videogen
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item config.example.yaml config.yaml
```

WSL2：

```bash
cd /mnt/d/vediotube-videogen
python3 -m venv .wsl-venv
.wsl-venv/bin/python -m pip install -e '.[dev]'
cp config.example.yaml config.yaml
```

先启动 ComfyUI，再用刚创建的虚拟环境启动本服务。

Windows PowerShell：

```powershell
.\.venv\Scripts\videogen-service.exe --config config.yaml
```

WSL2：

```bash
.wsl-venv/bin/videogen-service --config config.yaml
```

默认只监听 `127.0.0.1:8020`。确认 `http://127.0.0.1:8020/health` 返回 `status: ok` 后，再启动 `vediotube` 的 Web 控制台并打开 `http://127.0.0.1:8000/videogen`。

## 配置归属

- `config.yaml` 的 `comfyui`、`renderer`、`ollama`、`script`、`modes` 只属于这个项目。
- 四份 H3 API workflow 位于 `workflows/`，节点指针也只在这个项目配置。
- 原项目只配置 `videogen.service_url`、等待时间、本地预览目录和 B 站投稿声明。
- `story` 依赖 `ComfyUI_MiniMaxH3_Director`；不使用时可从本项目配置中删掉该模式。

## HTTP seam

- `GET /health`：模式、分镜模式和限制，外加 `script` 段的字幕总结配置。
- `POST /v1/scripts`：传一个 YouTube 网址，取字幕、让 Ollama 总结成分镜脚本。
- `POST /v1/validate`：在创建用户任务前校验模式、参考图和分镜，并返回对齐后的真实时长。
- `POST /v1/renders`：以 `render_id` 幂等提交渲染。
- `GET /v1/renders/{render_id}`：读取队列、进度或失败原因。
- `GET /v1/renders/{render_id}/media`：下载完成的 MP4。
- `DELETE /v1/renders/{render_id}`：删除终态渲染及其参考帧。

## 原创科普：从 YouTube 网址到分镜

`POST /v1/scripts` 把一条 YouTube 链接变成可以直接提交渲染的分镜脚本，分两步走，中间留出人工审阅：

```bash
curl -s http://127.0.0.1:8020/v1/scripts \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://youtu.be/dQw4w9WgXcQ", "target_seconds": 60, "guidance": "面向初中生"}'
```

服务依次做三件事：yt-dlp 按 `script.subtitle_languages` 的顺序取字幕（人工字幕优先于自动字幕），把字幕截到 `script.max_transcript_chars` 后交给 Ollama，再把模型返回的 JSON 收敛成本服务自己的分镜格式。返回里 `prompt` 是拼好的 `[0s-6s] …` 分镜文本，`mode` 是 `story`，`seconds` 是按 H3 长度步长对齐后的真实总时长——三个字段原样填进 `POST /v1/renders` 即可开始生成，`shots[].narration` 是给配音留的解说词，不参与画面。

每段分镜的时长会被夹到 `renderer.min_seconds`/`max_seconds` 之间，段数和总时长按 `script`（或请求里的 `max_shots`、`target_seconds`）截断，所以模型跑偏也不会提交一份渲染器不接受的脚本。

这一步只花 CPU 和 Ollama，不碰 GPU；要不要渲染、渲染几次仍然由原项目决定。取不到字幕、Ollama 没启动这类上游问题返回 `503`，链接不是 YouTube、模型没给出分镜这类返回 `400`。需要登录态或代理才能取字幕时，填 `script.cookies_file` 和 `script.proxy`。

服务一次只让一个任务进入 GPU。状态以版本明确的 JSON 落在 `work/<render_id>/`；进程重启后，未完成任务会转为可重试的失败态，不会伪装成仍在运行。

## 验证

```bash
python -m pytest -q
python -m mypy videogen_service
```
