# Pulls the subtitle track of a YouTube video so the script studio has source
# material. YouTube has no free caption API, so this drives yt-dlp: it resolves
# the watch page, picks a caption track by configured language preference, and
# downloads that track through yt-dlp's own HTTP stack (which carries the
# cookies, proxy and headers the caption URLs expect).

from collections.abc import Iterable
import html
import json
import re
from typing import Any, NamedTuple, Protocol
from urllib.parse import parse_qs, urlparse

from videogen_service.config import ScriptConfig

_YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtube-nocookie.com",
        "www.youtube-nocookie.com",
        "youtu.be",
        "www.youtu.be",
    }
)
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_PATH_PREFIXES = ("/shorts/", "/embed/", "/live/", "/v/")
# Preferred caption serialisations, best first. json3 carries one event per cue
# with an explicit append flag, which is the only format where YouTube's rolling
# auto-captions can be de-duplicated exactly instead of heuristically.
_CAPTION_FORMATS = ("json3", "vtt", "srv1", "ttml")
_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")


class TranscriptError(RuntimeError):
    pass


class TranscriptUnavailable(TranscriptError):
    """The video exists but no usable caption track could be fetched."""


class Transcript(NamedTuple):
    video_id: str
    title: str
    url: str
    language: str
    automatic: bool
    duration_seconds: float
    text: str


class TranscriptFetcher(Protocol):
    def fetch(self, url: str) -> Transcript: ...


def canonical_youtube_url(url: str) -> tuple[str, str]:
    """Return ``(video_id, watch_url)`` for any accepted YouTube URL shape."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise TranscriptError("请输入 http/https 开头的 YouTube 链接")
    host = (parsed.hostname or "").lower()
    if host not in _YOUTUBE_HOSTS:
        raise TranscriptError(f"只支持 YouTube 链接,收到的是: {host or url}")
    video_id = _extract_video_id(parsed.path, parsed.query, host=host)
    if video_id is None:
        raise TranscriptError("链接里没有视频 id,请用视频页面的完整地址")
    return video_id, f"https://www.youtube.com/watch?v={video_id}"


def _extract_video_id(path: str, query: str, *, host: str) -> str | None:
    if host in {"youtu.be", "www.youtu.be"}:
        candidate = path.strip("/").split("/")[0]
        return candidate if _VIDEO_ID.fullmatch(candidate) else None
    if path == "/watch":
        for candidate in parse_qs(query).get("v", []):
            if _VIDEO_ID.fullmatch(candidate):
                return candidate
        return None
    for prefix in _PATH_PREFIXES:
        if path.startswith(prefix):
            candidate = path[len(prefix) :].split("/")[0]
            return candidate if _VIDEO_ID.fullmatch(candidate) else None
    return None


class YtDlpTranscriptFetcher:
    """Fetches captions with yt-dlp, imported lazily so tests stay offline."""

    def __init__(self, config: ScriptConfig) -> None:
        self._config = config

    def fetch(self, url: str) -> Transcript:
        video_id, watch_url = canonical_youtube_url(url)
        try:
            from yt_dlp import YoutubeDL
        except ImportError as error:  # pragma: no cover - depends on install
            raise TranscriptUnavailable(
                "未安装 yt-dlp,请在本服务的虚拟环境里 pip install yt-dlp"
            ) from error

        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "skip_download": True,
            "socket_timeout": self._config.fetch_timeout_seconds,
        }
        if self._config.cookies_file is not None:
            options["cookiefile"] = str(self._config.cookies_file)
        if self._config.proxy:
            options["proxy"] = self._config.proxy

        with YoutubeDL(options) as downloader:
            try:
                info = downloader.extract_info(watch_url, download=False)
            except Exception as error:  # yt-dlp raises its own error hierarchy
                raise TranscriptUnavailable(f"读取 YouTube 视频失败: {error}") from error
            if not isinstance(info, dict):
                raise TranscriptUnavailable("yt-dlp 没有返回视频信息")
            duration = float(info.get("duration") or 0.0)
            if duration > self._config.max_source_seconds:
                raise TranscriptError(
                    f"源视频 {duration / 60:.0f} 分钟,超过上限 "
                    f"{self._config.max_source_seconds / 60:.0f} 分钟"
                )
            track = select_caption_track(info, self._config.subtitle_languages)
            try:
                payload = downloader.urlopen(track.url).read()
            except Exception as error:
                raise TranscriptUnavailable(f"下载字幕失败: {error}") from error

        text = parse_captions(payload, extension=track.extension)
        if not text:
            raise TranscriptUnavailable("字幕是空的,换一个有字幕的视频试试")
        return Transcript(
            video_id=video_id,
            title=str(info.get("title") or video_id),
            url=watch_url,
            language=track.language,
            automatic=track.automatic,
            duration_seconds=duration,
            text=text,
        )


class CaptionTrack(NamedTuple):
    language: str
    automatic: bool
    extension: str
    url: str


def select_caption_track(
    info: dict[str, Any], languages: Iterable[str]
) -> CaptionTrack:
    """Pick a caption track, preferring human subtitles over auto-captions."""
    preferences = [language.lower() for language in languages]
    manual = _tracks(info.get("subtitles"))
    automatic = _tracks(info.get("automatic_captions"))
    for pool, is_auto in ((manual, False), (automatic, True)):
        for preference in preferences:
            for language, formats in pool.items():
                if not _language_matches(language, preference):
                    continue
                chosen = _choose_format(formats)
                if chosen is not None:
                    extension, url = chosen
                    return CaptionTrack(language, is_auto, extension, url)
    available = sorted(set(manual) | set(automatic))
    raise TranscriptUnavailable(
        "这个视频没有可用字幕(想要的语言: "
        f"{', '.join(preferences)};视频有: {', '.join(available) or '无'})"
    )


def _tracks(value: Any) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(value, dict):
        return {}
    return {
        str(language): [item for item in formats if isinstance(item, dict)]
        for language, formats in value.items()
        if isinstance(formats, list)
    }


def _language_matches(language: str, preference: str) -> bool:
    tag = language.lower()
    return tag == preference or tag.split("-")[0] == preference.split("-")[0]


def _choose_format(formats: list[dict[str, Any]]) -> tuple[str, str] | None:
    for extension in _CAPTION_FORMATS:
        for item in formats:
            url = item.get("url")
            if item.get("ext") == extension and isinstance(url, str) and url:
                return extension, url
    return None


def parse_captions(payload: bytes, *, extension: str) -> str:
    text = payload.decode("utf-8", errors="replace")
    if extension == "json3":
        return parse_json3(text)
    return parse_vtt(text)


def parse_json3(payload: str) -> str:
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as error:
        raise TranscriptUnavailable("字幕不是合法的 json3 格式") from error
    events = document.get("events") if isinstance(document, dict) else None
    if not isinstance(events, list):
        return ""
    parts: list[str] = []
    for event in events:
        if not isinstance(event, dict) or event.get("aAppend"):
            continue
        segments = event.get("segs")
        if not isinstance(segments, list):
            continue
        line = "".join(
            str(segment.get("utf8", ""))
            for segment in segments
            if isinstance(segment, dict)
        )
        line = _WHITESPACE.sub(" ", line).strip()
        if line:
            parts.append(line)
    return _WHITESPACE.sub(" ", " ".join(parts)).strip()


def parse_vtt(payload: str) -> str:
    # Auto-captions repeat each line across consecutive cues as they scroll, so
    # a short window of recently emitted lines is what keeps the text readable.
    recent: list[str] = []
    kept: list[str] = []
    for raw in payload.splitlines():
        line = raw.strip()
        if not line or "-->" in line or line.isdigit():
            continue
        if line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE", "STYLE")):
            continue
        line = _WHITESPACE.sub(" ", html.unescape(_TAG.sub("", line))).strip()
        if not line or line in recent:
            continue
        kept.append(line)
        recent.append(line)
        if len(recent) > 4:
            recent.pop(0)
    return " ".join(kept).strip()
