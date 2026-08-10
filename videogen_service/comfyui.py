# Drives a local ComfyUI server over its HTTP API to render images and videos.
# Inputs are an API-format workflow plus the values patched into its nodes.
# Failures raise ComfyUiError so callers can fall back to a real video frame.

import base64
from collections.abc import Callable, Iterator
from contextlib import contextmanager
import copy
import json
import logging
import os
from pathlib import Path
import socket
import ssl
import threading
import time
from typing import IO, Any, NamedTuple
from urllib.error import HTTPError, URLError
import urllib.parse
import urllib.request
import uuid

# Every value we patch is addressed as "<node_id>.<field>" so a workflow exported
# from any ComfyUI build can be wired up from config instead of hard-coded here.
POINTER_SEPARATOR = "."

_LOGGER = logging.getLogger("videogen_service.comfyui")

# RFC 6455 opcodes. Only these five ever come off ComfyUI's socket.
_OPCODE_CONTINUATION = 0x0
_OPCODE_TEXT = 0x1
_OPCODE_BINARY = 0x2
_OPCODE_CLOSE = 0x8
_OPCODE_PING = 0x9
_OPCODE_PONG = 0xA
# A status line or header longer than this is not something we can parse, and a
# server that sends one must not be allowed to grow the buffer without bound.
_MAX_HEADER_LINE = 8_192
# Preview images share the progress socket and can run to a few megabytes; a
# length past this means the stream is misaligned, not that a frame is large.
_MAX_FRAME_BYTES = 16 * 1024 * 1024


class ComfyUiCancelled(RuntimeError):
    """渲染被调用方主动取消。刻意不继承 ComfyUiError:上层把 ComfyUiError
    一律包装成渲染失败,而取消要走自己的终态文案。"""


class ComfyUiError(RuntimeError):
    pass


class RenderedOutput(NamedTuple):
    # SaveVideo reports its mp4 under the same "images" key SaveImage uses, so the
    # server-side filename is the only thing that tells a caller what it received.
    data: bytes
    filename: str


class ProgressEvent(NamedTuple):
    # One tick of a single node's own step budget, exactly as ComfyUI reports it.
    # Turning these into a job-wide percentage is left to the caller, because
    # only the caller knows how many sampler passes it asked the graph for.
    node: str
    value: int
    maximum: int


def load_workflow(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ComfyUiError(f"未找到 ComfyUI workflow: {path}") from error
    except json.JSONDecodeError as error:
        raise ComfyUiError(f"ComfyUI workflow 不是合法 JSON: {path}") from error
    if not isinstance(raw, dict) or not raw:
        raise ComfyUiError(f"ComfyUI workflow 必须是非空 JSON 对象: {path}")
    if "nodes" in raw and "links" in raw:
        # The editor's own save format cannot be submitted; /prompt wants the
        # flat {node_id: {class_type, inputs}} shape behind "Export (API)".
        raise ComfyUiError(
            f"{path} 是 ComfyUI 界面格式,请用 Workflow → Export (API) 导出 API 格式"
        )
    return raw


def patch_workflow(
    workflow: dict[str, Any],
    *,
    pointers: dict[str, list[str]],
    values: dict[str, Any],
) -> dict[str, Any]:
    patched = copy.deepcopy(workflow)
    for name, value in values.items():
        targets = pointers.get(name)
        if not targets:
            raise ComfyUiError(f"workflow inputs 缺少 {name} 的节点指针")
        for pointer in targets:
            node_id, _, field = pointer.partition(POINTER_SEPARATOR)
            node = patched.get(node_id)
            if not isinstance(node, dict):
                raise ComfyUiError(f"workflow 里没有节点 {node_id}(来自指针 {pointer})")
            inputs = node.get("inputs")
            if not isinstance(inputs, dict) or field not in inputs:
                class_type = node.get("class_type", "?")
                raise ComfyUiError(
                    f"节点 {node_id}({class_type})没有输入 {field}(来自指针 {pointer})"
                )
            inputs[field] = value
    return patched


class ComfyUiClient:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        poll_interval_seconds: float,
    ) -> None:
        configured_url = base_url if "://" in base_url else f"http://{base_url}"
        self.base_url = configured_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.client_id = str(uuid.uuid4())

    def render(
        self,
        workflow: dict[str, Any],
        *,
        on_progress: Callable[[ProgressEvent], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> RenderedOutput:
        deadline = time.monotonic() + self.timeout_seconds
        # The socket opens before the prompt is submitted, otherwise the first
        # ticks of a fast graph land before anyone is listening.
        with self._watch_progress(on_progress) as stream:
            prompt_id = self._submit(workflow)
            if stream is not None:
                stream.expect(prompt_id)
            entry = self._await_history(
                prompt_id, deadline=deadline, should_cancel=should_cancel
            )
        reference = _first_result(entry)
        return RenderedOutput(
            data=self._download(reference), filename=reference["filename"]
        )

    @contextmanager
    def _watch_progress(
        self, on_progress: Callable[[ProgressEvent], None] | None
    ) -> Iterator["_ProgressStream | None"]:
        # Progress is a courtesy, never a precondition: a ComfyUI that will not
        # hand over its websocket still renders, the console just falls back to
        # showing elapsed time.
        stream: _ProgressStream | None = None
        if on_progress is not None:
            stream = _ProgressStream(
                base_url=self.base_url,
                client_id=self.client_id,
                on_progress=on_progress,
                connect_timeout=min(self.timeout_seconds, 10.0),
            )
            try:
                stream.start()
            except ComfyUiError as error:
                _LOGGER.warning("拿不到 ComfyUI 实时进度,只显示计时: %s", error)
                stream = None
        try:
            yield stream
        finally:
            if stream is not None:
                stream.close()

    def upload_image(self, path: Path) -> str:
        # LoadImage names a file inside ComfyUI's own input directory, so a
        # reference frame has to be handed over before the workflow can cite it.
        boundary = f"----comfy{uuid.uuid4().hex}"
        body = _multipart_image(path, boundary=boundary)
        response = self._request_json(
            "/upload/image",
            data=body,
            content_type=f"multipart/form-data; boundary={boundary}",
        )
        name = response.get("name")
        if not isinstance(name, str) or not name:
            raise ComfyUiError(f"ComfyUI 未返回上传后的文件名: {path.name}")
        subfolder = response.get("subfolder")
        return f"{subfolder}/{name}" if isinstance(subfolder, str) and subfolder else name

    def _submit(self, workflow: dict[str, Any]) -> str:
        payload = {"prompt": workflow, "client_id": self.client_id}
        response = self._request_json(
            "/prompt",
            data=json.dumps(payload).encode("utf-8"),
        )
        node_errors = response.get("node_errors")
        if isinstance(node_errors, dict) and node_errors:
            raise ComfyUiError(f"ComfyUI 拒绝了 workflow: {_compact(node_errors)}")
        prompt_id = response.get("prompt_id")
        if not isinstance(prompt_id, str) or not prompt_id:
            raise ComfyUiError("ComfyUI 未返回 prompt_id")
        return prompt_id

    def _await_history(
        self,
        prompt_id: str,
        *,
        deadline: float,
        should_cancel: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        while True:
            if should_cancel is not None and should_cancel():
                self._interrupt_if_ours(prompt_id)
                raise ComfyUiCancelled("渲染已取消")
            history = self._request_json(f"/history/{urllib.parse.quote(prompt_id)}")
            entry = history.get(prompt_id)
            if isinstance(entry, dict):
                _raise_for_execution_error(entry)
                if isinstance(entry.get("outputs"), dict) and entry["outputs"]:
                    return entry
            if time.monotonic() >= deadline:
                raise ComfyUiError(
                    f"ComfyUI 生成超时({self.timeout_seconds:.0f}s),"
                    "可调大 config.yaml 的 comfyui.timeout_seconds"
                )
            time.sleep(self.poll_interval_seconds)

    def _interrupt_if_ours(self, prompt_id: str) -> None:
        """/interrupt 是全实例级的,这台机器的 ComfyUI 还有别的用户;
        只有确认正在跑的 prompt 就是我们这条时才打断,否则放它烧完
        (等待方随即停止轮询,产物被丢弃)。尽力而为,失败不拦取消。"""
        try:
            queue = self._request_json("/queue")
            running = queue.get("queue_running") or []
            ours = any(
                isinstance(item, list) and len(item) > 1 and item[1] == prompt_id
                for item in running
            )
            if ours:
                self._request_bytes("/interrupt", data=b"{}")
                _LOGGER.info("已请求 ComfyUI 中断当前渲染 %s", prompt_id)
        except ComfyUiError as error:
            _LOGGER.warning("取消时中断 ComfyUI 失败(忽略): %s", error)

    def _download(self, reference: dict[str, str]) -> bytes:
        query = urllib.parse.urlencode(reference)
        content = self._request_bytes(f"/view?{query}")
        if not content:
            raise ComfyUiError(f"ComfyUI 返回了空文件: {reference['filename']}")
        return content

    def _request_json(
        self,
        path: str,
        *,
        data: bytes | None = None,
        content_type: str = "application/json",
    ) -> dict[str, Any]:
        body = self._request_bytes(path, data=data, content_type=content_type)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ComfyUiError(f"ComfyUI {path} 返回了非 JSON 响应") from error
        if not isinstance(payload, dict):
            raise ComfyUiError(f"ComfyUI {path} 返回的不是 JSON 对象")
        return payload

    def _request_bytes(
        self,
        path: str,
        *,
        data: bytes | None = None,
        content_type: str = "application/json",
    ) -> bytes:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers={"Content-Type": content_type} if data else {},
            method="POST" if data else "GET",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds
            ) as response:
                read: bytes = response.read()
                return read
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[-2_000:]
            raise ComfyUiError(
                f"ComfyUI HTTP {error.code} ({path}): {detail or error.reason}"
            ) from error
        except (TimeoutError, URLError) as error:
            raise ComfyUiError(
                f"连接 ComfyUI 失败({self.base_url}{path}): {error}，"
                "请确认 ComfyUI 已启动"
            ) from error


class _ProgressStream:
    # Sampler progress exists only on ComfyUI's websocket; /history answers "is
    # it finished" and nothing in between. One read-only stream is not worth a
    # websocket dependency, so this speaks the sliver of RFC 6455 the server
    # actually uses — the same trade _multipart_image makes against requests.
    # Server frames are never masked and its JSON arrives one frame at a time,
    # so reading is a header, a length, and a payload; the only frame we ever
    # write is a pong, which is why the masking here is write-only.

    def __init__(
        self,
        *,
        base_url: str,
        client_id: str,
        on_progress: Callable[[ProgressEvent], None],
        connect_timeout: float,
    ) -> None:
        self._base_url = base_url
        self._client_id = client_id
        self._on_progress = on_progress
        self._connect_timeout = connect_timeout
        self._socket: socket.socket | None = None
        self._reader: IO[bytes] | None = None
        self._thread: threading.Thread | None = None
        self._closed = False
        self._prompt_id: str | None = None

    def start(self) -> None:
        self._socket = self._connect()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="comfyui-progress"
        )
        self._thread.start()

    def expect(self, prompt_id: str) -> None:
        # Everything queued under this client_id reports on the same socket, so
        # a leftover job's ticks must not be allowed to drive this job's bar.
        self._prompt_id = prompt_id

    def close(self) -> None:
        # Closing the socket under the reader is what unblocks it; it checks
        # _closed and treats the resulting error as the shutdown it is.
        self._closed = True
        if self._socket is not None:
            try:
                self._socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._socket.close()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _connect(self) -> socket.socket:
        parts = urllib.parse.urlsplit(self._base_url)
        host = parts.hostname or "127.0.0.1"
        port = parts.port or (443 if parts.scheme == "https" else 80)
        path = f"{parts.path}/ws?clientId={urllib.parse.quote(self._client_id)}"
        try:
            sock = socket.create_connection(
                (host, port), timeout=self._connect_timeout
            )
        except OSError as error:
            raise ComfyUiError(f"连接 ComfyUI 进度通道失败({host}:{port}): {error}") from error
        try:
            if parts.scheme == "https":
                sock = ssl.create_default_context().wrap_socket(
                    sock, server_hostname=host
                )
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            sock.sendall(
                (
                    f"GET {path} HTTP/1.1\r\n"
                    f"Host: {host}:{port}\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Key: {key}\r\n"
                    "Sec-WebSocket-Version: 13\r\n"
                    "\r\n"
                ).encode("ascii")
            )
            reader = sock.makefile("rb")
            status_line = reader.readline(_MAX_HEADER_LINE)
            if b"101" not in status_line.split():
                raise ComfyUiError(
                    "ComfyUI 没有升级进度通道: "
                    f"{status_line.decode('latin-1').strip() or '连接被关闭'}"
                )
            while True:
                line = reader.readline(_MAX_HEADER_LINE)
                if line in (b"\r\n", b"\n", b""):
                    break
        except ComfyUiError:
            sock.close()
            raise
        except OSError as error:
            sock.close()
            raise ComfyUiError(f"ComfyUI 进度通道握手失败: {error}") from error
        # Frames are read blocking from here on: a timeout landing mid-frame
        # would leave the stream misaligned, and close() is what ends the read.
        sock.settimeout(None)
        self._reader = reader
        return sock

    def _run(self) -> None:
        try:
            for payload in self._messages():
                self._dispatch(payload)
        except Exception as error:
            # A dropped socket costs the operator a progress bar, never a
            # render, so it is reported and the reader simply stops.
            if not self._closed:
                _LOGGER.warning("ComfyUI 进度通道断开,改用计时显示: %s", error)
        finally:
            if self._reader is not None:
                self._reader.close()

    def _messages(self) -> Iterator[bytes]:
        pending = b""
        collecting = False
        while True:
            frame = self._read_frame()
            if frame is None:
                return
            final, opcode, payload = frame
            if opcode == _OPCODE_CLOSE:
                return
            if opcode == _OPCODE_PING:
                self._pong(payload)
                continue
            if opcode == _OPCODE_TEXT:
                pending, collecting = payload, True
            elif opcode == _OPCODE_BINARY:
                # Preview images ride this same socket; only the JSON matters.
                pending, collecting = b"", False
            elif opcode == _OPCODE_CONTINUATION and collecting:
                pending += payload
            else:
                continue
            if final:
                if collecting:
                    yield pending
                pending, collecting = b"", False

    def _read_frame(self) -> tuple[bool, int, bytes] | None:
        reader = self._reader
        if reader is None:
            return None
        header = _read_exactly(reader, 2)
        if header is None:
            return None
        if header[1] & 0x80:
            # A server frame is never masked. One that is means this is not the
            # protocol we think it is, and the byte stream cannot be trusted.
            return None
        length = header[1] & 0x7F
        if length == 126:
            extended = _read_exactly(reader, 2)
        elif length == 127:
            extended = _read_exactly(reader, 8)
        else:
            extended = b""
        if extended is None:
            return None
        if extended:
            length = int.from_bytes(extended, "big")
        if length > _MAX_FRAME_BYTES:
            return None
        payload = _read_exactly(reader, length) if length else b""
        if payload is None:
            return None
        return bool(header[0] & 0x80), header[0] & 0x0F, payload

    def _pong(self, payload: bytes) -> None:
        sock = self._socket
        if sock is None:
            return
        # Client frames must be masked, so the one frame we ever send is too.
        # Control payloads are capped at 125 bytes, so the length fits inline.
        body = payload[:125]
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(body))
        sock.sendall(bytes([0x80 | _OPCODE_PONG, 0x80 | len(body)]) + mask + masked)

    def _dispatch(self, payload: bytes) -> None:
        try:
            message = json.loads(payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if not isinstance(message, dict) or message.get("type") != "progress":
            return
        data = message.get("data")
        if not isinstance(data, dict):
            return
        prompt_id = data.get("prompt_id")
        if (
            self._prompt_id is not None
            and isinstance(prompt_id, str)
            and prompt_id != self._prompt_id
        ):
            return
        value = data.get("value")
        maximum = data.get("max")
        if not isinstance(value, int) or not isinstance(maximum, int) or maximum <= 0:
            return
        self._on_progress(
            ProgressEvent(node=str(data.get("node") or ""), value=value, maximum=maximum)
        )


def _read_exactly(reader: IO[bytes], count: int) -> bytes | None:
    # A buffered reader returns fewer bytes than asked only at end of stream,
    # which for a websocket means the peer hung up mid-frame.
    data = reader.read(count)
    return data if data is not None and len(data) == count else None


def _multipart_image(path: Path, *, boundary: str) -> bytes:
    try:
        content = path.read_bytes()
    except OSError as error:
        raise ComfyUiError(f"读取参考图失败: {path}: {error}") from error
    # ComfyUI only looks at the field name and the filename, and overwrite keeps
    # a re-run from piling up "frame (2).png" copies in its input directory.
    header = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="overwrite"\r\n\r\n'
        "true\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="{path.name}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8")
    return header + content + f"\r\n--{boundary}--\r\n".encode("utf-8")


def _raise_for_execution_error(entry: dict[str, Any]) -> None:
    status = entry.get("status")
    if not isinstance(status, dict):
        return
    if status.get("status_str") != "error":
        return
    messages = status.get("messages")
    raise ComfyUiError(f"ComfyUI 执行失败: {_compact(messages)}")


def _first_result(entry: dict[str, Any]) -> dict[str, str]:
    outputs = entry.get("outputs")
    if not isinstance(outputs, dict):
        raise ComfyUiError("ComfyUI 历史记录缺少 outputs")
    for node_output in outputs.values():
        if not isinstance(node_output, dict):
            continue
        # SaveVideo's PreviewVideo reports {"images": [...], "animated": [true]},
        # the same shape SaveImage uses, so one lookup covers both node families.
        results = node_output.get("images")
        if not isinstance(results, list):
            continue
        for result in results:
            if not isinstance(result, dict):
                continue
            filename = result.get("filename")
            if not isinstance(filename, str) or not filename:
                continue
            return {
                "filename": filename,
                "subfolder": str(result.get("subfolder") or ""),
                "type": str(result.get("type") or "output"),
            }
    raise ComfyUiError(
        "ComfyUI 完成了任务但没有输出文件,请检查 workflow 是否有 SaveImage 或 SaveVideo"
    )


def _compact(payload: object) -> str:
    try:
        text = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(payload)
    return text[:1_000]
