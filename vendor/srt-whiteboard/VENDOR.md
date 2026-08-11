# vendored: srt-whiteboard-animation

- 来源:https://github.com/geeklee/srt-whiteboard-animation
- 锁定 commit:`696a7243c0e6ffb6827676e539c2ca5ebae2bf6b`(2026-07-27)
- 许可:MIT(见本目录 LICENSE,版权归上游作者)
- 用途:白板手绘动画渲染引擎——线稿 PNG + 标注 JSON 逐笔绘制成 MP4。
  服务侧以子进程调用 `scripts/render_stream_whiteboard.py`,见
  `videogen_service/jobs.py` 的 whiteboard 内建能力。

## 本地化差异

- 不使用 `scripts/prepare_env.py` 的独立 venv:依赖(numpy、opencv-python-headless)
  并入本项目 pyproject;H.264 转码走系统 ffmpeg(平台 compose 能力的既有硬依赖),
  PyAV 备选路径不启用。
- 上游 SKILL.md 的"图像模型生成线稿"一步,在本平台由 t2i 能力(ComfyUI)承担,
  见 docs/whiteboard-animation.md。
- `assets/preview.html` 由服务静态路由挂出,供浏览器内校对标注。
- `assets/drawing-hand.png` 笔杆上有上游作者的签名文字,平台纪律是画面
  不出现文字/水印,所以服务渲染时显式传入修掉文字的版本
  `videogen_service/static/whiteboard-hand.png`(TELEA 修复笔杆区域,
  其余像素与原件一致);vendor 原件保持原样。

## 升级方式

对照上游新 commit 重新下载同名文件,更新本文件的锁定 commit;
不要在本目录内做功能性修改(有补丁需求时在服务侧包一层)。
