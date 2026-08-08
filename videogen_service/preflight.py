# Startup self-check. The failure modes this service actually has on a fresh
# machine are all environmental — a workflow exported in the wrong format, a
# ComfyUI that is not up yet, an Ollama without the configured model, a missing
# yt-dlp — and each one otherwise surfaces much later as a failed render. The
# launcher scripts call this before handing over to uvicorn.

from collections.abc import Callable
import json
from pathlib import Path
from typing import Literal, NamedTuple
import unicodedata
from urllib.error import HTTPError, URLError
import urllib.request

from videogen_service.comfyui import ComfyUiError, load_workflow
from videogen_service.config import ServiceConfig

Status = Literal["ok", "warn", "fail"]

_SYMBOLS: dict[Status, str] = {"ok": "✓", "warn": "!", "fail": "✗"}


class Check(NamedTuple):
    name: str
    status: Status
    detail: str


class Probe(NamedTuple):
    body: bytes | None
    error: str | None


def http_probe(url: str, *, timeout: float = 5.0) -> Probe:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return Probe(response.read(), None)
    except HTTPError as error:
        return Probe(None, f"HTTP {error.code}")
    except (TimeoutError, URLError, OSError) as error:
        return Probe(None, str(error))


def run_checks(
    config: ServiceConfig, *, probe: Callable[[str], Probe] = http_probe
) -> list[Check]:
    return [
        _check_address(config),
        _check_work_dir(config),
        *_check_workflows(config),
        _check_comfyui(config, probe),
        _check_ollama(config, probe),
        _check_yt_dlp(config),
    ]


def has_failure(checks: list[Check]) -> bool:
    return any(check.status == "fail" for check in checks)


def format_checks(checks: list[Check]) -> str:
    width = max(_display_width(check.name) for check in checks)
    lines = [
        f"{_SYMBOLS[check.status]} {check.name}"
        f"{' ' * (width - _display_width(check.name))}  {check.detail}"
        for check in checks
    ]
    return "\n".join(lines)


def _display_width(text: str) -> int:
    # 中文名字占两列，用字符数对齐会把这张表排歪。
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def _check_address(config: ServiceConfig) -> Check:
    return Check(
        "服务地址",
        "ok",
        f"http://{config.host}:{config.port}/  ← 生成台就在这个地址",
    )


def _check_work_dir(config: ServiceConfig) -> Check:
    try:
        config.work_dir.mkdir(parents=True, exist_ok=True)
        probe = config.work_dir / ".write-test"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as error:
        return Check("工作目录", "fail", f"{config.work_dir} 不可写: {error}")
    return Check("工作目录", "ok", str(config.work_dir))


def _check_workflows(config: ServiceConfig) -> list[Check]:
    checks: list[Check] = []
    for mode, settings in sorted(config.modes.items()):
        name = f"{mode} workflow"
        try:
            workflow = load_workflow(settings.workflow_file)
        except ComfyUiError as error:
            checks.append(Check(name, "fail", str(error)))
            continue
        missing = [
            pointer
            for pointers in settings.inputs.values()
            for pointer in pointers
            if pointer.partition(".")[0] not in workflow
        ]
        if missing:
            checks.append(Check(name, "fail", f"指向了不存在的节点: {missing}"))
            continue
        checks.append(Check(name, "ok", settings.workflow_file.name))
    return checks


def _check_comfyui(config: ServiceConfig, probe: Callable[[str], Probe]) -> Check:
    base_url = config.comfyui.base_url.rstrip("/")
    result = probe(f"{base_url}/system_stats")
    if result.error is not None:
        return Check(
            "ComfyUI",
            "warn",
            f"{base_url} 连不上({result.error})；先启动它，否则渲染会立刻失败"
            + _wsl_hint(base_url),
        )
    return Check("ComfyUI", "ok", base_url)


def is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text(errors="replace").lower()
    except OSError:
        return False


def windows_host_ip() -> str | None:
    """The default gateway, which is how WSL2's NAT reaches the Windows host."""
    return _route_gateway(Path("/proc/net/route"))


def _route_gateway(path: Path) -> str | None:
    try:
        rows = path.read_text(encoding="utf-8").splitlines()[1:]
    except OSError:
        return None
    for row in rows:
        fields = row.split()
        if len(fields) < 3 or fields[1] != "00000000":
            continue
        try:
            packed = int(fields[2], 16).to_bytes(4, "little")
        except ValueError:
            continue
        return ".".join(str(byte) for byte in packed)
    return None


def _wsl_hint(base_url: str) -> str:
    # In WSL2's default NAT mode, 127.0.0.1 is WSL itself — a ComfyUI or Ollama
    # running on Windows is simply not there, and the operator would otherwise
    # go looking for a crashed process that is actually running fine.
    if not is_wsl() or not any(
        host in base_url for host in ("127.0.0.1", "localhost", "::1")
    ):
        return ""
    address = windows_host_ip()
    target = f"http://{address}:端口" if address else "Windows 主机的 IP"
    return (
        f"。这是在 WSL 里:127.0.0.1 指的是 WSL 自己。若它跑在 Windows 上，"
        f"把 config.yaml 里的地址改成 {target}，"
        "或者给 WSL 开镜像网络(.wslconfig 里 networkingMode=mirrored)"
    )


def _check_ollama(config: ServiceConfig, probe: Callable[[str], Probe]) -> Check:
    if not config.script.enabled:
        return Check("Ollama", "ok", "字幕总结已关闭，不需要它")
    base_url = config.ollama.base_url.rstrip("/")
    wanted = config.script.model or config.ollama.model
    result = probe(f"{base_url}/api/tags")
    if result.error is not None:
        return Check(
            "Ollama",
            "warn",
            f"{base_url} 连不上({result.error})；渲染不受影响，字幕总结会失败"
            + _wsl_hint(base_url),
        )
    installed = _model_names(result.body)
    if installed and not any(
        name == wanted or name.split(":")[0] == wanted.split(":")[0]
        for name in installed
    ):
        return Check(
            "Ollama",
            "warn",
            f"没有找到模型 {wanted}，装一个: ollama pull {wanted}",
        )
    return Check("Ollama", "ok", f"{base_url} · {wanted}")


def _model_names(body: bytes | None) -> list[str]:
    if body is None:
        return []
    try:
        document = json.loads(body)
    except json.JSONDecodeError:
        return []
    models = document.get("models") if isinstance(document, dict) else None
    if not isinstance(models, list):
        return []
    return [
        str(model["name"])
        for model in models
        if isinstance(model, dict) and isinstance(model.get("name"), str)
    ]


def _check_yt_dlp(config: ServiceConfig) -> Check:
    if not config.script.enabled:
        return Check("yt-dlp", "ok", "字幕总结已关闭，不需要它")
    try:
        from yt_dlp import version
    except ImportError:
        return Check(
            "yt-dlp",
            "warn",
            '没装；跑一次 pip install -e ".[dev]" 再用 YouTube 起稿',
        )
    return Check("yt-dlp", "ok", str(getattr(version, "__version__", "已安装")))
