# 白板手绘动画能力(whiteboard)

把一张线稿图按标注顺序"逐笔画出来"的手绘动画:笔尖沿线走、先勾线后上色、
未画到的区域保持空白纸面,配上解说就是经典的白板讲解片。引擎 vendored 自
[geeklee/srt-whiteboard-animation](https://github.com/geeklee/srt-whiteboard-animation)
(MIT,锁定版本见 `vendor/srt-whiteboard/VENDOR.md`),OpenCV 纯 CPU 渲染,
H.264 转码复用系统 ffmpeg——和 H3 渲染完全不抢卡,`needs_gpu=False`,
可以在出视频的同时并行出白板段。

科普纪录片(science-doc)讲抽象原理时,白板段是"示意画面"的一种便宜、
可控的实现:没有模型幻觉,画面完全由标注决定,天然满足"画面不出现文字"
之外的另一条纪律——不伪装成真实影像。

## 在平台里的位置

内建能力,和 `compose`、`review` 同级,不进 config;只要 vendor 目录在、
依赖装了(numpy + opencv-python-headless,已在 pyproject 主依赖),
启动自检就会亮 `✓ 白板引擎`。缺了只降级成 `!`,不挡服务。

一条任务 = 一幕(scene):**线稿资产** + **标注 JSON** → MP4。

```
资产中心(线稿 PNG)──┐
                      ├─ POST /v1/jobs {capability: "whiteboard"} → whiteboard.mp4
标注 JSON(annotation)┘
```

- 线稿从资产中心取(`asset_id`):可以是 t2i 能力(FLUX)生成后
  `save-asset` 存进去的,也可以是手工上传的。
- 标注是文本字段直接带在任务里(`annotation`),提交口先做形状校验
  (`elements[].reveal.startMs/durationMs` 必须是数),坏标注坏在请求上,
  不进队列。
- 产物固定叫 `whiteboard.mp4`;机器上既无 ffmpeg 也无 PyAV 时引擎保留
  mp4v 编码的 `whiteboard_raw.mp4`,任务同样算成功(播放器兼容性差些)。

提交示例:

```bash
curl -X POST http://127.0.0.1:8020/v1/jobs -H "Content-Type: application/json" -d '{
  "job_id": "wb-scene-01",
  "capability": "whiteboard",
  "asset_id": "<线稿资产 id>",
  "annotation": "{\"sceneDurationMs\": 9000, \"elements\": [...]}"
}'
```

## 一幕怎么生产(分工)

上游 skill 的七步流程搬到本平台后,每一步都有归属:

| 步骤 | 谁来做 | 平台落点 |
| --- | --- | --- |
| 1. 解说/字幕定稿 | 既有脚本管线 | `scripting.py` 分镜 + TTS 配音,或现成 SRT |
| 2. 线稿生成 | t2i 能力(FLUX) | 提示词按上游规范:米色纸面、简笔勾线、留白、无文字;产物 `save-asset` 存资产 |
| 3. 标注撰写 | Agent(云端大脑)或人工 | 看图写 `annotation.json`:每个语义区域的像素框、叙事顺序、起止毫秒 |
| 4. 标注校对 | 人 | 打开 `/whiteboard/preview`(vendored 校对页,拖框、改序、拉时间轴,File System Access 存盘) |
| 5. 渲染一幕 | whiteboard 任务 | 本文档上面的 API |
| 6. 多幕合成 | compose 或 ffmpeg | 各幕 MP4 顺序拼接,再与配音/字幕合成 |

标注的完整字段语义(reveal 方向、maskPaddingPx、protectedRegions、
handPath……)以 `vendor/srt-whiteboard/SKILL.md` 为准,服务只校验形状不
复述契约。`vendor/srt-whiteboard/scripts/parse_srt.py` 可以直接跑,
把 SRT 切成 25-35 秒的幕并给出关键帧建议,当分幕草稿用。

## 与上游的差异

- 线稿不走云端图像模型,走本机 t2i(执行权留在本机的总原则)。
- 渲染不用上游的独立 venv(`prepare_env.py`),依赖并入主 venv;
  转码优先系统 ffmpeg,不装 PyAV。
- 校对页由服务挂在 `/whiteboard/preview`,不落第二份拷贝,升级引擎
  (按 VENDOR.md 换 vendor 目录)页子跟着走。

## 已知边界与下一步

- Agent 工具面(`agent.py` ToolBox)还没有 `whiteboard` 工具:对话里
  Agent 目前不能直接排白板任务,要走 API/工作台。等标注撰写的质量
  (大脑看图给像素框的准确率)验证过再收进工具面。
- 逐幕时长上限没有额外限制,引擎单进程超时 1800s;一幕控制在
  30 秒左右(上游建议)最稳。
- 多幕拼接暂时用 compose/ffmpeg 手工拼;`merge_scenes.py` 的
  "尺寸不一致回退重编码"逻辑如需服务化,再包成能力。
