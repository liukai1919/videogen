"""Persistent, bounded downloads of public web videos through yt-dlp."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import ipaddress
import json
from pathlib import Path
import secrets
import socket
from threading import Event, RLock
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit


class DownloadError(ValueError):
    pass


def validate_url(url: str) -> str:
    url = url.strip()
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or len(url) > 2048:
        raise DownloadError("请输入有效的 HTTP/HTTPS 视频链接")
    if parsed.username or parsed.password:
        raise DownloadError("链接不能包含账号信息")
    host = parsed.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise DownloadError("不支持本机或局域网地址")
    try:
        addresses = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
    except (OSError, ValueError) as error:
        raise DownloadError(f"无法解析网址：{host}") from error
    if not addresses or any(
        not ipaddress.ip_address(address[4][0]).is_global for address in addresses
    ):
        raise DownloadError("不支持本机或局域网地址")
    return url


class DownloadEngine(Protocol):
    def info(self, url: str) -> dict[str, Any]: ...

    def download(
        self,
        url: str,
        kind: str,
        format_id: str | None,
        directory: Path,
        progress: Callable[[int], None],
        cancelled: Event,
    ) -> Path: ...


class YtDlpEngine:
    def __init__(self, *, cookies_file: Path | None = None, proxy: str | None = None) -> None:
        self.cookies_file = cookies_file
        self.proxy = proxy

    def _options(self) -> dict[str, Any]:
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": True,
            "socket_timeout": 30,
            "retries": 3,
            "fragment_retries": 3,
        }
        if self.cookies_file is not None:
            options["cookiefile"] = str(self.cookies_file)
        if self.proxy:
            options["proxy"] = self.proxy
        return options

    def info(self, url: str) -> dict[str, Any]:
        from yt_dlp import YoutubeDL

        with YoutubeDL({**self._options(), "skip_download": True}) as ydl:
            raw = ydl.extract_info(url, download=False)
        if not isinstance(raw, dict):
            raise DownloadError("无法读取视频信息")
        best: dict[int, dict[str, Any]] = {}
        for item in raw.get("formats") or []:
            height = item.get("height")
            if not isinstance(height, int) or item.get("vcodec") == "none":
                continue
            if height not in best or (item.get("tbr") or 0) > (best[height].get("tbr") or 0):
                best[height] = item
        return {
            "title": str(raw.get("title") or ""),
            "thumbnail": str(raw.get("thumbnail") or ""),
            "duration": raw.get("duration"),
            "formats": [
                {"id": item["format_id"], "height": height}
                for height, item in sorted(best.items(), reverse=True)
            ],
        }

    def download(
        self,
        url: str,
        kind: str,
        format_id: str | None,
        directory: Path,
        progress: Callable[[int], None],
        cancelled: Event,
    ) -> Path:
        from yt_dlp import YoutubeDL

        def hook(data: dict[str, Any]) -> None:
            if cancelled.is_set():
                raise InterruptedError("下载已取消")
            if data.get("status") == "finished":
                progress(99)
            elif data.get("status") == "downloading":
                total = data.get("total_bytes") or data.get("total_bytes_estimate")
                if total:
                    progress(min(99, int(100 * (data.get("downloaded_bytes") or 0) / total)))

        options = self._options()
        options.update({"outtmpl": str(directory / "media.%(ext)s"), "progress_hooks": [hook]})
        if kind == "audio":
            options.update({
                "format": "bestaudio/best",
                "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}],
            })
        else:
            options.update({
                "format": f"{format_id}+bestaudio/best" if format_id else "bestvideo+bestaudio/best",
                "merge_output_format": "mp4",
                "postprocessors": [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}],
            })
        with YoutubeDL(options) as ydl:
            ydl.download([url])
        if cancelled.is_set():
            raise InterruptedError("下载已取消")
        suffix = ".mp3" if kind == "audio" else ".mp4"
        files = [p for p in directory.glob("media.*") if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
        chosen = next((p for p in files if p.suffix.lower() == suffix), None)
        if chosen is None:
            raise DownloadError(f"下载结束但没有生成 {suffix} 文件，请确认 FFmpeg 可用")
        return chosen


class DownloadService:
    def __init__(self, work_dir: Path, *, engine: DownloadEngine | None = None) -> None:
        self.directory = work_dir / ".downloads"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.engine = engine or YtDlpEngine()
        self.lock = RLock()
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="video-download")
        self.cancellations: dict[str, Event] = {}
        self.records: dict[str, dict[str, Any]] = {}
        for path in self.directory.glob("*/task.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                if record.get("task_id") != path.parent.name:
                    continue
                if record.get("status") in {"QUEUED", "RUNNING"}:
                    record["status"] = "FAILED"
                    record["error"] = "服务重启中断了下载，可重试"
                    self._write(record)
                self.records[record["task_id"]] = record
            except (OSError, ValueError, KeyError):
                continue

    def _write(self, record: dict[str, Any]) -> None:
        directory = self.directory / record["task_id"]
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "task.json"
        temporary = directory / "task.json.tmp"
        temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)

    def info(self, url: str) -> dict[str, Any]:
        return self.engine.info(validate_url(url))

    def submit_many(self, urls: list[str], *, kind: str, format_id: str | None = None) -> list[dict[str, Any]]:
        unique = list(dict.fromkeys(validate_url(url) for url in urls))
        if not unique or len(unique) > 10:
            raise DownloadError("一次请输入 1 至 10 个不同链接")
        return [self.submit(url, kind=kind, format_id=format_id) for url in unique]

    def submit(self, url: str, *, kind: str, format_id: str | None = None) -> dict[str, Any]:
        url = validate_url(url)
        if kind not in {"video", "audio"}:
            raise DownloadError("格式只能是 video 或 audio")
        if format_id is not None and (len(format_id) > 80 or not all(c.isalnum() or c in "._-" for c in format_id)):
            raise DownloadError("清晰度格式无效")
        if kind == "audio" and format_id:
            raise DownloadError("音频下载不接受视频清晰度")
        task_id = f"download_{secrets.token_hex(8)}"
        record = {
            "task_id": task_id,
            "url": url,
            "kind": kind,
            "format_id": format_id,
            "status": "QUEUED",
            "progress": 0,
            "error": None,
            "filename": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        with self.lock:
            self.records[task_id] = record
            self.cancellations[task_id] = Event()
            self._write(record)
            self.executor.submit(self._run, task_id)
            return self._view(record)

    def _run(self, task_id: str) -> None:
        with self.lock:
            record = self.records[task_id]
            cancelled = self.cancellations[task_id]
            if cancelled.is_set():
                return
            record["status"] = "RUNNING"
            self._write(record)
        directory = self.directory / task_id

        def progress(value: int) -> None:
            with self.lock:
                if value > record["progress"] and record["status"] == "RUNNING":
                    record["progress"] = min(99, value)
                    self._write(record)

        try:
            path = self.engine.download(record["url"], record["kind"], record["format_id"], directory, progress, cancelled)
            if cancelled.is_set():
                raise InterruptedError()
            with self.lock:
                record.update(status="DONE", progress=100, filename=path.name)
                self._write(record)
        except InterruptedError:
            with self.lock:
                record.update(status="CANCELLED", error="下载已取消")
                self._write(record)
            self._clean_media(directory)
        except Exception as error:
            with self.lock:
                if cancelled.is_set():
                    record.update(status="CANCELLED", error="下载已取消")
                else:
                    record.update(status="FAILED", error=str(error)[:500])
                self._write(record)
            self._clean_media(directory)
        finally:
            with self.lock:
                self.cancellations.pop(task_id, None)

    def _clean_media(self, directory: Path) -> None:
        for path in directory.iterdir():
            if path.is_file() and path.name != "task.json":
                path.unlink(missing_ok=True)

    @staticmethod
    def _view(record: dict[str, Any]) -> dict[str, Any]:
        return {**record, "media_url": f"/v1/downloads/{record['task_id']}/media" if record["status"] == "DONE" else None}

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            return [self._view(record) for record in sorted(self.records.values(), key=lambda r: r["created_at"], reverse=True)]

    def get(self, task_id: str) -> dict[str, Any]:
        with self.lock:
            if task_id not in self.records:
                raise DownloadError("下载任务不存在")
            return self._view(self.records[task_id])

    def cancel(self, task_id: str) -> dict[str, Any]:
        with self.lock:
            record = self.records.get(task_id)
            if record is None:
                raise DownloadError("下载任务不存在")
            if record["status"] not in {"QUEUED", "RUNNING"}:
                raise DownloadError("只能取消等待中或运行中的下载")
            self.cancellations[task_id].set()
            if record["status"] == "QUEUED":
                record.update(status="CANCELLED", error="下载已取消")
                self._write(record)
            return self._view(record)

    def retry(self, task_id: str) -> dict[str, Any]:
        old = self.get(task_id)
        if old["status"] not in {"FAILED", "CANCELLED"}:
            raise DownloadError("只能重试失败或取消的下载")
        return self.submit(old["url"], kind=old["kind"], format_id=old["format_id"])

    def media_path(self, task_id: str) -> Path:
        record = self.get(task_id)
        if record["status"] != "DONE" or not record["filename"]:
            raise DownloadError("文件尚未下载完成")
        filename = str(record["filename"])
        if Path(filename).name != filename:
            raise DownloadError("下载文件名无效")
        path = self.directory / task_id / filename
        if not path.is_file():
            raise DownloadError("下载文件不存在")
        return path

    def close(self) -> None:
        with self.lock:
            for event in self.cancellations.values():
                event.set()
        self.executor.shutdown(wait=False, cancel_futures=True)
