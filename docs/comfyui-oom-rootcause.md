# ComfyUI 渲染中途崩溃——根因调查与修复(2026-08-10)

## 症状

story 渲染进行中,渲染服务侧报 `Connection refused` / `ConnectionResetError
[Errno 104]`,ComfyUI 随后自行恢复在线。2026-08-09/10 两晚 story 渲染
7/10 失败;10s 单块也中招,与块长无清晰相关。comfyui.log 里没有任何
traceback(这是关键线索:SIGKILL 级死亡才会一字不留)。

## 证据链

1. **部署形态**:ComfyUI 跑在 WSL 内,systemd 单元 `comfyui.service`
   (`Restart=on-failure` + `RestartSec=5`——"死而复生"的机制),日志
   append 到 `~/comfyui.log`,启动参数
   `--disable-pinned-memory --reserve-vram 2.0 --mmap-torch-files`。
2. **内核击杀记录**(`journalctl -k`):两晚 **7 次**
   `Out of memory: Killed process (python), task_memcg=comfyui.service`,
   击杀时 anon-rss 均为 **30.4~30.5GB**;7 个时间戳与 7 条 FAILED 渲染
   一一对应(21:15/21:23/21:33/22:07/22:56/23:29/23:34)。
3. **探针渲染**(固定负载 24s 双块,每 5s 采 /proc/PID/status):
   - 空载 RssAnon **24.1GB**,RssFile 仅 66MB——`--mmap-torch-files`
     并未把权重留在文件页,几乎全部物化为匿名内存;
   - 渲染启动 5 秒内跳到 **31.75GB**(全程峰值),这是文本编码/模型
     腾挪窗口;**每个块的开头都重过一次**(pass1/2/3 峰值 30.0~30.3GB);
   - 采样稳态 ~20.5GB,VAE 解码收尾 ~22.3GB——帧数不是矛盾主体。
4. **死线算术**:WSL 上限 32GB(.wslconfig,2026-08-09 为救 Windows
   从 52GB 压下来的),系统自留 ~1.5GB → 击杀线 ~30.5GB;渲染启动
   峰值 31.75GB 直接穿线。swap 16GB 救不了:GPU 注册的宿主内存页
   不可换出。成与败只差别的进程当刻要不要几百 MB——所以呈现为
   ~60% 的"概率性"崩溃。

## 已排除

- 前缀帧/continuous_reference:源码核查为空转,从未进渲染路径
  (docs/continuity-prefix-frames.md)。
- 块时长:10s 块同样被杀(22:07),峰值来自模型腾挡不是帧缓冲。
- CUDA OOM / 驱动 TDR:日志无 CUDA 报错;是 Linux 内核 OOM。

## 修复

`C:\Users\LiuK0\.wslconfig` 的 `memory=32GB → 40GB`(宿主 63.7GB;
Windows 侧上限余 24GB,自用 ~14GB,富余 ~10GB,远离 52GB 时代
"Windows 只剩 1.5GB"的旧坑)。峰值 31.75GB 对 40GB = **8GB+ 余量**。
`wsl --shutdown` 后保活+systemd 自愈拉起,重启后 WSL 报 39GB 可用。

## 验证(同负载连发 3 条,曾经约六成阵亡的工况)

2026-08-10 00:49–01:19,《灯塔守望者》24s 双块、seed=808,连发 3 条:
**3/3 DONE,耗时 572s / 573s / 572s,期间内核零 OOM 击杀**。
修复前同类工况(2026-08-09/10 夜)失败率约 60-70%。

已知薄点:**冷启动首渲**(ComfyUI 重启后第一发,模型初次物化叠加渲染
启动)瞬时峰值实测 39.76GB,贴着 40GB 上限靠 swap 接住(VmSwap 2.6GB)
活了下来。日常热态峰值 31.75GB,余量充足;冷启只在重启后出现,而崩溃
根因修掉后重启本身变稀。若将来冷启动出现击杀,升到 44GB(Windows 侧
将只剩 ~20GB 上限,先观察再动)。

## 后续可选优化(暂不动)

- ComfyUI 空载 24GB 匿名驻留的成分剖析(文本编码器 Qwen3-VL 常驻?
  `--cache-none` 可清但代价是每次重载,先不动);
- 节点 VAEDecode 无 tiling(executor_core.py:68),若将来上更长块或
  更高分辨率再议;
- 若再遇紧张:优先看渲染启动窗口能否避免 TE 与 UNet 同刻驻留。
