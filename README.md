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

- `config.yaml` 的 `comfyui`、`renderer`、`ollama`、`director`、`modes` 只属于这个项目。
- 四份 H3 API workflow 位于 `workflows/`，节点指针也只在这个项目配置。
- 原项目只配置 `videogen.service_url`、等待时间、本地预览目录和 B 站投稿声明。
- `story` 依赖 `ComfyUI_MiniMaxH3_Director`；不使用时可从本项目配置中删掉该模式。

## 提示词导演

H3 的提示词是一套结构化格式（三字段、`[Shot N]` 切换时间、`<d>` 台词标签）。官方 API 用闭源的
Context-IR 模块负责改写，本地这份工作由 `director` 承担。

改写只发生在 `POST /v1/enhance`，**不在渲染路径里**。控制面拿到结果、必要时手改，再把最终提示词
提交给 `/v1/renders`——所以一次渲染仍然只由它自己记录的 spec 决定，导演挂了也永远挡不住 GPU。

- `provider: anthropic` 走 Claude，需要 `pip install -e ".[anthropic]"` 和 `ANTHROPIC_API_KEY`。
  结构化输出保证三字段结构，官方指南作为固定前缀被缓存，重复调用的成本可以忽略。
- `provider: ollama` 走本地模型，不需要额外依赖和密钥，可以拿来和云端 A/B 对比。
- 提示词指南是 `prompts/h3_prompt_guide.md`，想换成官方原文直接替换这个文件。

无论哪个 provider，改写结果都要过三道确定性校验：`[Shot N]` 时间戳严格递增且落在**对齐后的真实
时长**内、`<d>` 里的台词和原文逐字一致、`non_diegetic_music` 不含情绪词。不合格就带着失败原因
重问一次；仍不合格则连同 `warnings` 一起返回，由人决定要不要用。

## HTTP seam

- `GET /health`：模式、分镜模式、限制，以及有没有配置导演。
- `POST /v1/validate`：在创建用户任务前校验模式、参考图和分镜，并返回对齐后的真实时长。
- `POST /v1/enhance`：把大白话改写成 H3 三字段格式，返回提示词、结构化字段和校验告警；没配导演时 503。
- `POST /v1/renders`：以 `render_id` 幂等提交渲染。
- `GET /v1/renders/{render_id}`：读取队列、进度或失败原因。
- `GET /v1/renders/{render_id}/media`：下载完成的 MP4。
- `DELETE /v1/renders/{render_id}`：删除终态渲染及其参考帧。

服务一次只让一个任务进入 GPU。状态以版本明确的 JSON 落在 `work/<render_id>/`；进程重启后，未完成任务会转为可重试的失败态，不会伪装成仍在运行。

## 验证

```bash
python -m pytest -q
python -m mypy videogen_service
```
