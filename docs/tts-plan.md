# TTS 配音:技术选型与开发计划

手绘白板路线成为主力品类后,成片质量最大的洞是配音:平台至今只有"TTS 缺失
→ 降级为 SRT 字幕"这一条路(evals R1 S1/S10 都是按降级判的分)。本文定
引擎选型、时序设计和里程碑,目标是让 science-doc 白板片带上真人感的解说。

先说清楚接入面有多小:`kind: tts` 的能力契约已经在
`videogen_service/config.py` 和 `jobs.py` 里活着——config 声明一条本地命令
模板(`{text_file}` → `{output_file}`),jobs 子进程执行、compose 收音频,
测试也有(`tests/test_jobs.py` 的 fake-tts)。**缺的从来不是接入代码,
是引擎选择和"配音之后时序归谁"的设计。**

## 选型

约束按重要性排:① 中文旁白质量(这是给 B 站观众听的);② 许可干净
(视频可能商业化,权重许可 NC 的直接出局);③ 本地运行、显存开销可控
(H3 渲染峰值 30GB+,TTS 不能凑热闹);④ 社区活跃、装得起来。

| 引擎 | 中文质量 | 许可 | 显存 | 判定 |
| --- | --- | --- | --- | --- |
| **CosyVoice 2**(阿里 FunAudioLLM) | 优,韵律自然,3s 参考音零样本克隆 | Apache-2.0(代码+权重) | ~2-4GB,CPU 可跑(慢) | **默认引擎** |
| **IndexTTS-2**(B 站开源) | 优,带情感控制和**显式时长控制** | 代码开源;权重商用需申请授权 | ~8GB | 挑战者:A/B 试听,许可确认后可升级为默认 |
| GPT-SoVITS | 良,少样本克隆最强,中文社区最大 | MIT | ~4GB | 二期"音色人设"用,不当默认(装配重、韵律偶发不稳) |
| Kokoro | 中文一般 | Apache-2.0 | 82M,CPU 实时 | 仅作无卡兜底,不推荐主力 |
| F5-TTS / fish-speech / ChatTTS | 良-优 | 权重 CC-BY-NC | — | **出局**(商用许可不干净) |
| edge-tts | 良 | 微软云端服务 | 0 | **出局**(违背"执行权留在本机";最多调试期临时用,不进 config 示例) |

结论:**CosyVoice 2 起步,IndexTTS-2 做 A/B 挑战者**。IndexTTS-2 的时长
控制对白板时序对齐是杀手级特性(见下),且出自 B 站、对中文科普语气的
适配值得期待——但许可要先落实,不干净就留在实验分支。

## 时序设计:配音先行,时长从真实音频反推

白板路线目前的因果链是"SRT 时间轴 → sceneDurationMs → 区域 startMs",
而 SRT 的时间是编剧工具估出来的。**接入 TTS 后因果必须反转**:

```
分镜 beat 文本 ──(每 beat 一条 tts 任务)──▶ beat.wav × N
beat.wav 实测时长(ffprobe) ──▶ 真实 SRT + sceneDurationMs
真实 SRT ──▶ 白板 annotation 的区域 startMs/durationMs
beat.wav 顺序拼接(段间 250ms 呼吸) ──▶ narration.wav ──▶ compose
```

关键取巧:**按 beat 逐段合成,每段音频的物理时长就是该 beat 的时长**,
不需要句级时间戳、不需要强制对齐(forced alignment)、不需要扩 tts 契约
——现有"一条 text → 一个音频"的任务粒度天然就是对的。SRT 由拼接偏移量
累加生成,和音轨精确一致;白板每幕的 `sceneDurationMs` = 该幕 beat 音频
总长 + 收尾凝视 0.5s,上游 skill 的"25-35 秒/幕"从建议值变成实测值。

反转带来的红线:接了 TTS 之后,**估算时长的 SRT 不得再进 compose**
(两套时间轴必然漂移,字幕对不上嘴)。编剧工具的 SRT 只在无 TTS 降级
路径里继续使用。

## 里程碑

### M-TTS-0 选型冒烟(0.5-1 天)

脱离平台,在本机把 CosyVoice 2 和 IndexTTS-2 各装起来,用同一段
science-doc 解说词(建议 3 段:陈述句、设问句、数字单位密集句)各出样本,
耳朵盲评。同时落实 IndexTTS-2 权重的商用许可条款。产出:引擎决定 +
样本存档。**不写平台代码。**

### M-TTS-1 能力接入(1 天)

- `tools/tts_cosyvoice.py` 包装脚本,严格遵守 `{text_file}` → `{output_file}`
  契约:读 UTF-8 文本、合成、写 wav、退出码说话。参考音色文件路径做成
  脚本参数,写死在 config 的 command 里。
- `config.yaml` 增 `local-tts` 能力项:`needs_gpu: true`(jobs.py 的
  GPU 轮转会自动让它和 H3 渲染错峰;白板渲染是 CPU,照常并行),
  `timeout_seconds` 按冷启动实测放宽(模型每次调用冷加载,首期接受,
  优化见风险节)。
- 验证:`POST /v1/jobs` 出真音频、能力表 `list_capabilities` 亮出来、
  启动自检 `✓`。现有 fake-tts 测试已覆盖契约,不需要新测试。

### M-TTS-2 配音驱动时序(2-3 天)

- 编排层落地上面的因果反转:agent 按 beat 逐段提交 tts 任务 → ffprobe
  实测时长 → 生成真实 SRT 和 `sceneDurationMs` → 写白板 annotation。
  优先做成 agent 的工作流纪律(系统提示 + skill 缺省),而不是新代码:
  平台已有的任务、资产、compose 原语足够表达整条链。
- 拼接与呼吸间隔用 ffmpeg concat 完成(可给 compose 加薄参数,也可先
  让 agent 用现有原语拼);段间隔 250ms 起步,评测后调。
- science-doc skill 增补配音缺省:口语、语速中等偏慢、数字读法(
  "864x480"读"八六四乘四八零"这类坑要在解说词层解决,不指望引擎)。

### M-TTS-3 全链成片验证(1-2 天)

选一个真实选题,跑通:分镜 → 逐 beat 配音 → 线稿(t2i)→ 标注 →
白板逐幕渲染 → 多幕合并 → compose(narration.wav + 字幕烧制)。
音频规范一次定死:48kHz、响度 loudnorm 到 -16 LUFS。产出第一支
带配音的白板成片——这支片子同时就是归因实验的 C 版本。

### M-TTS-4 评测固化(0.5-1 天)

- scenarios 增两个场景:①"做一支带配音的白板讲解片"——判 agent 是否
  配音先行、SRT 是否来自实测时长(工具调用参数可判,不必等渲染);
  ②"TTS 能力不在时"——判降级为编剧 SRT 路径是否还通(守住 R2 已
  拿到的分)。
- 跑一轮记入 rounds.md;"配音先行"结论稳定 3 轮后进 DECISIONS.md。

### 二期(不排期,记方向)

- 音色人设:GPT-SoVITS 少样本克隆或 IndexTTS-2 情感标签,给频道一个
  固定"声音形象"。
- BGM 与音效:配乐库 + ffmpeg sidechaincompress 闪避人声。
- 常驻推理进程:冷启动优化(见风险)。

## 风险与已知坑

- **冷启动延迟**:包装脚本每次调用冷加载模型,单 beat 可能 10-30s 起。
  首期用 `timeout_seconds` 硬扛(beat 数 × 冷启动是线性痛,不是阻塞痛);
  真不可忍再上常驻进程(包装脚本变成瘦客户端连本地端口),契约不变。
- **GPU 争抢**:CosyVoice 2-4GB 与 H3 30GB 峰值同挤会 OOM。`needs_gpu:
  true` 交给 jobs.py 轮转即可,不要为省时间标成 false。
- **WSL/Windows 分裂**:TTS 跟着服务跑在 WSL 侧,走 CUDA-WSL;引擎装进
  独立 venv(不进本项目 pyproject——依赖树太重,污染主环境),config 的
  command 直接写该 venv 的解释器绝对路径。
- **多音字与数字读法**:所有引擎都有此类失误。纪律定在解说词层:编剧
  产出的 narration 就该是"念出来"的文本;evals 场景抽查。
- **IndexTTS-2 许可**:商用授权未落实前,其产物不得进任何要发布的成片。

## 与既有决策的关系

- 白板能力 `needs_gpu=False`、与渲染并行的设计不动(docs/whiteboard-animation.md)。
- `/v1` 冻结契约不动:tts 是 config 声明的能力,videotube 流水线无感知。
- vendor 纪律不动:不改 srt-whiteboard 目录,时序对齐全部发生在标注生成侧。
