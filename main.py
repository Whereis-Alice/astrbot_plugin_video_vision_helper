"""
AstrBot plugin: Video Vision Helper.

Convert video attachments into sampled JPEG frames and optional audio or
transcripts so multimodal models can reason about video content without a
native video input channel.
"""

from __future__ import annotations

import asyncio
import json
import math
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.message import TextPart
from astrbot.core.message.components import Reply, Video
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path


PLUGIN_ID = "astrbot_plugin_video_vision_helper"
PLUGIN_VERSION = "0.3.0"
PLUGIN_DESC = "\u5c06\u89c6\u9891\u62c6\u89e3\u4e3a\u5173\u952e\u5e27\u3001\u53ef\u9009\u97f3\u9891\u4e0e\u8f6c\u5199\u6587\u672c\uff0c\u589e\u5f3a\u591a\u6a21\u6001\u89c6\u9891\u7406\u89e3"
PLUGIN_REPO = "https://github.com/Whereis-Alice/astrbot_plugin_video_vision_helper"

DEFAULT_SUMMARY_TEMPLATE = (
    "[\u7cfb\u7edf\u63d0\u793a] \u672c\u6b21\u7528\u6237\u53d1\u9001\u4e86 {video_count} \u4e2a\u89c6\u9891\uff0c"
    "\u63d2\u4ef6\u5df2\u8986\u76d6 {segment_count} \u4e2a\u5206\u6790\u7247\u6bb5\u5e76\u62bd\u53d6 {frame_count} \u5f20\u5173\u952e\u5e27"
    "{audio_note}{transcript_note}\uff0c\u603b\u5206\u6790\u65f6\u957f\u7ea6 {coverage_seconds:.1f} \u79d2\u3002"
    "\u8bf7\u7efc\u5408\u955c\u5934\u53d8\u5316\u3001\u4eba\u7269\u52a8\u4f5c\u3001\u5b57\u5e55\u548c\u58f0\u97f3\u7ebf\u7d22\u7406\u89e3\u89c6\u9891\u5185\u5bb9\u3002"
)
DEFAULT_VIDEO_TEMPLATE = (
    "[\u89c6\u9891\u5206\u6790][{video_name}] \u8986\u76d6 {segment_count} \u4e2a\u7247\u6bb5\uff0c"
    "\u62bd\u53d6 {frame_count} \u5f20\u5173\u952e\u5e27{audio_note}{transcript_note}\uff0c"
    "\u5206\u6790\u65f6\u957f\u7ea6 {coverage_seconds:.1f} \u79d2\u3002"
)
DEFAULT_TRANSCRIPT_TEMPLATE = "[\u89c6\u9891\u97f3\u9891\u8f6c\u5199][{video_name}] \u5171 {segment_count} \u4e2a\u7247\u6bb5\uff1a\n{transcript}"
DEFAULT_TRANSCRIPT_SEGMENT_TEMPLATE = "- {segment_label} ({start_seconds:.1f}s-{end_seconds:.1f}s): {transcript}"


@dataclass(frozen=True)
class FFmpegPolicy:
    ffmpeg_path: str
    ffprobe_path: str
    command_timeout_seconds: int


@dataclass(frozen=True)
class FramePolicy:
    enabled: bool
    max_videos_per_request: int
    sampling_mode: str
    max_frames_per_video: int
    fixed_interval_seconds: float
    max_side: int
    jpeg_quality: int
    max_video_duration_seconds: int


@dataclass(frozen=True)
class SegmentPolicy:
    enabled: bool
    selection_mode: str
    segment_duration_seconds: int
    max_segments_per_video: int
    min_video_duration_seconds: int


@dataclass(frozen=True)
class AudioPolicy:
    mode: str
    sample_rate: int
    max_audio_duration_seconds: int
    fallback_to_attachment_on_stt_failure: bool


@dataclass(frozen=True)
class SttPolicy:
    backend: str
    astrbot_provider_id: str
    endpoint: str
    api_key: str
    model: str
    language: str
    prompt: str
    temperature: float
    timeout_seconds: int
    max_transcript_chars: int
    max_total_transcript_chars: int


@dataclass(frozen=True)
class HintPolicy:
    enabled: bool
    target: str
    remove_raw_video_marker_after_processing: bool
    summary_template: str
    video_template: str
    transcript_template: str
    transcript_segment_template: str


@dataclass(frozen=True)
class DownloadPolicy:
    quoted_video_download_enabled: bool
    max_download_size_mb: float
    timeout_seconds: int


@dataclass(frozen=True)
class RuntimePolicy:
    max_processing_seconds_per_video: int


@dataclass(frozen=True)
class PluginSettings:
    enabled: bool
    ffmpeg: FFmpegPolicy
    frame: FramePolicy
    segment: SegmentPolicy
    audio: AudioPolicy
    stt: SttPolicy
    hint: HintPolicy
    download: DownloadPolicy
    runtime: RuntimePolicy


@dataclass(frozen=True)
class VideoAttachment:
    name: str
    path: Path
    quoted: bool
    source_part_index: int | None = None


@dataclass(frozen=True)
class VideoProbeInfo:
    duration_seconds: float
    has_video_stream: bool
    has_audio_stream: bool


@dataclass(frozen=True)
class VideoSegment:
    index: int
    start_seconds: float
    duration_seconds: float
    label: str

    @property
    def end_seconds(self) -> float:
        return self.start_seconds + self.duration_seconds


@dataclass(frozen=True)
class TranscriptChunk:
    segment_label: str
    start_seconds: float
    end_seconds: float
    text: str


@dataclass(frozen=True)
class ProcessedVideo:
    attachment: VideoAttachment
    frame_paths: list[Path]
    audio_paths: list[Path]
    transcript_chunks: list[TranscriptChunk]
    segment_count: int
    coverage_seconds: float


@register(PLUGIN_ID, "Whereis-Alice", PLUGIN_DESC, PLUGIN_VERSION, PLUGIN_REPO)
class VideoVisionHelper(Star):
    """Turn video attachments into frames and optional audio understanding cues."""

    def __init__(
        self,
        context: Context,
        config: AstrBotConfig | dict[str, Any] | None = None,
    ) -> None:
        super().__init__(context, config)
        self.config = config or {}

    async def initialize(self) -> None:
        logger.info("[%s] plugin initialized", PLUGIN_ID)

    async def terminate(self) -> None:
        logger.info("[%s] plugin terminated", PLUGIN_ID)

    def _config_get(self, key: str, default: Any) -> Any:
        if hasattr(self.config, "get"):
            return self.config.get(key, default)
        return default

    def _config_section(self, key: str) -> dict[str, Any]:
        value = self._config_get(key, {})
        return value if isinstance(value, dict) else {}

    def _is_debug_enabled(self) -> bool:
        return self._read_bool(self._config_get("debug_logging", False), False)

    def _debug(self, message: str, *args: Any) -> None:
        if self._is_debug_enabled():
            logger.debug("[%s] " + message, PLUGIN_ID, *args)

    @staticmethod
    def _read_bool(value: Any, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"1", "true", "yes", "on"}:
                return True
            if lowered in {"0", "false", "no", "off"}:
                return False
        if value is None:
            return default
        return bool(value)

    @staticmethod
    def _read_int(
        value: Any,
        default: int,
        *,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int:
        try:
            result = int(value)
        except (TypeError, ValueError):
            result = default
        if minimum is not None:
            result = max(minimum, result)
        if maximum is not None:
            result = min(maximum, result)
        return result

    @staticmethod
    def _read_float(
        value: Any,
        default: float,
        *,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError):
            result = default
        if minimum is not None:
            result = max(minimum, result)
        if maximum is not None:
            result = min(maximum, result)
        return result

    @staticmethod
    def _read_str(value: Any, default: str) -> str:
        return value if isinstance(value, str) and value.strip() else default

    def _load_settings(self) -> PluginSettings:
        ffmpeg_conf = self._config_section("ffmpeg_policy")
        frame_conf = self._config_section("frame_policy")
        segment_conf = self._config_section("segment_policy")
        audio_conf = self._config_section("audio_policy")
        stt_conf = self._config_section("stt_policy")
        hint_conf = self._config_section("hint_policy")
        download_conf = self._config_section("download_policy")
        runtime_conf = self._config_section("runtime_policy")

        sampling_mode = self._read_str(frame_conf.get("sampling_mode"), "uniform")
        if sampling_mode not in {"uniform", "fixed_interval", "head_tail"}:
            sampling_mode = "uniform"

        segment_selection_mode = self._read_str(
            segment_conf.get("selection_mode"),
            "uniform",
        )
        if segment_selection_mode not in {"head_only", "uniform", "head_tail"}:
            segment_selection_mode = "uniform"

        audio_mode = self._read_str(audio_conf.get("mode"), "attach")
        if audio_mode not in {"disabled", "attach", "stt", "attach_and_stt"}:
            audio_mode = "attach"

        stt_backend = self._read_str(stt_conf.get("backend"), "disabled").strip().lower()
        stt_backend = {
            "astrbot": "astrbot_configured",
            "astrbot_provider": "astrbot_configured",
            "configured": "astrbot_configured",
            "openai": "openai_compatible",
        }.get(stt_backend, stt_backend)
        if stt_backend not in {
            "disabled",
            "astrbot_configured",
            "openai_compatible",
        }:
            stt_backend = "disabled"

        hint_target = self._read_str(hint_conf.get("target"), "extra_user_content")
        if hint_target not in {"extra_user_content", "prompt", "system_prompt"}:
            hint_target = "extra_user_content"

        return PluginSettings(
            enabled=self._read_bool(self._config_get("enabled", True), True),
            ffmpeg=FFmpegPolicy(
                ffmpeg_path=self._read_str(ffmpeg_conf.get("ffmpeg_path"), "ffmpeg"),
                ffprobe_path=self._read_str(ffmpeg_conf.get("ffprobe_path"), "ffprobe"),
                command_timeout_seconds=self._read_int(
                    ffmpeg_conf.get("command_timeout_seconds"),
                    120,
                    minimum=5,
                    maximum=3600,
                ),
            ),
            frame=FramePolicy(
                enabled=self._read_bool(frame_conf.get("enabled"), True),
                max_videos_per_request=self._read_int(
                    frame_conf.get("max_videos_per_request"),
                    2,
                    minimum=1,
                    maximum=16,
                ),
                sampling_mode=sampling_mode,
                max_frames_per_video=self._read_int(
                    frame_conf.get("max_frames_per_video"),
                    6,
                    minimum=1,
                    maximum=24,
                ),
                fixed_interval_seconds=self._read_float(
                    frame_conf.get("fixed_interval_seconds"),
                    2.0,
                    minimum=0.1,
                    maximum=600.0,
                ),
                max_side=self._read_int(
                    frame_conf.get("max_side"),
                    768,
                    minimum=64,
                    maximum=4096,
                ),
                jpeg_quality=self._read_int(
                    frame_conf.get("jpeg_quality"),
                    90,
                    minimum=30,
                    maximum=100,
                ),
                max_video_duration_seconds=self._read_int(
                    frame_conf.get("max_video_duration_seconds"),
                    30,
                    minimum=1,
                    maximum=60 * 60,
                ),
            ),
            segment=SegmentPolicy(
                enabled=self._read_bool(segment_conf.get("enabled"), False),
                selection_mode=segment_selection_mode,
                segment_duration_seconds=self._read_int(
                    segment_conf.get("segment_duration_seconds"),
                    30,
                    minimum=5,
                    maximum=60 * 30,
                ),
                max_segments_per_video=self._read_int(
                    segment_conf.get("max_segments_per_video"),
                    4,
                    minimum=1,
                    maximum=24,
                ),
                min_video_duration_seconds=self._read_int(
                    segment_conf.get("min_video_duration_seconds"),
                    45,
                    minimum=5,
                    maximum=60 * 60,
                ),
            ),
            audio=AudioPolicy(
                mode=audio_mode,
                sample_rate=self._read_int(
                    audio_conf.get("sample_rate"),
                    16000,
                    minimum=8000,
                    maximum=48000,
                ),
                max_audio_duration_seconds=self._read_int(
                    audio_conf.get("max_audio_duration_seconds"),
                    90,
                    minimum=1,
                    maximum=60 * 60,
                ),
                fallback_to_attachment_on_stt_failure=self._read_bool(
                    audio_conf.get("fallback_to_attachment_on_stt_failure"),
                    True,
                ),
            ),
            stt=SttPolicy(
                backend=stt_backend,
                astrbot_provider_id=self._read_str(
                    stt_conf.get("astrbot_provider_id"),
                    "",
                ),
                endpoint=self._read_str(stt_conf.get("endpoint"), ""),
                api_key=self._read_str(stt_conf.get("api_key"), ""),
                model=self._read_str(stt_conf.get("model"), ""),
                language=self._read_str(stt_conf.get("language"), ""),
                prompt=self._read_str(stt_conf.get("prompt"), ""),
                temperature=self._read_float(
                    stt_conf.get("temperature"),
                    0.0,
                    minimum=0.0,
                    maximum=2.0,
                ),
                timeout_seconds=self._read_int(
                    stt_conf.get("timeout_seconds"),
                    120,
                    minimum=5,
                    maximum=3600,
                ),
                max_transcript_chars=self._read_int(
                    stt_conf.get("max_transcript_chars"),
                    1200,
                    minimum=64,
                    maximum=20000,
                ),
                max_total_transcript_chars=self._read_int(
                    stt_conf.get("max_total_transcript_chars"),
                    2400,
                    minimum=128,
                    maximum=50000,
                ),
            ),
            hint=HintPolicy(
                enabled=self._read_bool(hint_conf.get("enabled"), True),
                target=hint_target,
                remove_raw_video_marker_after_processing=self._read_bool(
                    hint_conf.get("remove_raw_video_marker_after_processing"),
                    True,
                ),
                summary_template=self._read_str(
                    hint_conf.get("summary_template"),
                    DEFAULT_SUMMARY_TEMPLATE,
                ),
                video_template=self._read_str(
                    hint_conf.get("video_template"),
                    DEFAULT_VIDEO_TEMPLATE,
                ),
                transcript_template=self._read_str(
                    hint_conf.get("transcript_template"),
                    DEFAULT_TRANSCRIPT_TEMPLATE,
                ),
                transcript_segment_template=self._read_str(
                    hint_conf.get("transcript_segment_template"),
                    DEFAULT_TRANSCRIPT_SEGMENT_TEMPLATE,
                ),
            ),
            download=DownloadPolicy(
                quoted_video_download_enabled=self._read_bool(
                    download_conf.get("quoted_video_download_enabled"),
                    True,
                ),
                max_download_size_mb=self._read_float(
                    download_conf.get("max_download_size_mb"),
                    64.0,
                    minimum=0.0,
                    maximum=4096.0,
                ),
                timeout_seconds=self._read_int(
                    download_conf.get("timeout_seconds"),
                    120,
                    minimum=5,
                    maximum=3600,
                ),
            ),
            runtime=RuntimePolicy(
                max_processing_seconds_per_video=self._read_int(
                    runtime_conf.get("max_processing_seconds_per_video"),
                    180,
                    minimum=0,
                    maximum=60 * 60,
                ),
            ),
        )

    @staticmethod
    def _describe_video_component(video: Video) -> str:
        payload: list[str] = []
        for field_name in ("file", "path", "cover"):
            value = getattr(video, field_name, None)
            if isinstance(value, str) and value.strip():
                payload.append(f"{field_name}={value!r}")
        return ", ".join(payload) if payload else repr(video)

    @staticmethod
    def _strip_file_scheme(value: str) -> str:
        return value[8:] if value.startswith("file:///") else value

    def _collect_video_path_candidates(self, video: Video) -> list[Path]:
        temp_roots = [
            Path(get_astrbot_temp_path()),
            Path(tempfile.gettempdir()),
        ]
        candidates: list[Path] = []
        seen: set[str] = set()

        for field_name in ("path", "file"):
            raw_value = getattr(video, field_name, None)
            if not isinstance(raw_value, str) or not raw_value.strip():
                continue
            normalized = self._strip_file_scheme(raw_value.strip())
            lowered = normalized.lower()
            if lowered.startswith("http://") or lowered.startswith("https://"):
                continue

            raw_path = Path(normalized)
            path_variants = [raw_path]
            if not raw_path.is_absolute():
                path_variants.append(Path.cwd() / raw_path)
                for root in temp_roots:
                    path_variants.append(root / raw_path)
                if raw_path.name:
                    for root in temp_roots:
                        path_variants.append(root / raw_path.name)

            for candidate in path_variants:
                key = str(candidate)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(candidate)

        return candidates

    @staticmethod
    def _safe_event_get(event_like: Any, key: str, default: Any = None) -> Any:
        if isinstance(event_like, dict):
            return event_like.get(key, default)
        getter = getattr(event_like, "get", None)
        if callable(getter):
            try:
                return getter(key, default)
            except Exception:
                pass
        return getattr(event_like, key, default)

    @staticmethod
    def _extract_onebot_segments(event_like: Any) -> list[Any]:
        segments = VideoVisionHelper._safe_event_get(event_like, "message", [])
        return list(segments) if isinstance(segments, list) else []

    def _extract_reply_ids_from_raw_event(self, raw_event: Any) -> list[str]:
        reply_ids: list[str] = []
        for segment in self._extract_onebot_segments(raw_event):
            if self._safe_event_get(segment, "type", "") != "reply":
                continue
            data = self._safe_event_get(segment, "data", {})
            reply_id = self._read_str(self._safe_event_get(data, "id", ""), "")
            if reply_id:
                reply_ids.append(reply_id)
        return reply_ids

    def _collect_video_path_candidates_from_mapping(
        self,
        payload: dict[str, Any],
    ) -> list[Path]:
        temp_roots = [
            Path(get_astrbot_temp_path()),
            Path(tempfile.gettempdir()),
        ]
        candidates: list[Path] = []
        seen: set[str] = set()
        for field_name in ("path", "file", "name", "file_name"):
            raw_value = payload.get(field_name)
            if not isinstance(raw_value, str) or not raw_value.strip():
                continue
            normalized = self._strip_file_scheme(raw_value.strip())
            lowered = normalized.lower()
            if lowered.startswith("http://") or lowered.startswith("https://"):
                continue
            raw_path = Path(normalized)
            path_variants = [raw_path]
            if not raw_path.is_absolute():
                path_variants.append(Path.cwd() / raw_path)
                for root in temp_roots:
                    path_variants.append(root / raw_path)
                if raw_path.name:
                    for root in temp_roots:
                        path_variants.append(root / raw_path.name)
            for candidate in path_variants:
                key = str(candidate)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(candidate)
        return candidates

    def _guess_video_name_from_mapping(self, payload: dict[str, Any]) -> str:
        for field_name in ("file_name", "name", "file"):
            value = payload.get(field_name)
            if isinstance(value, str) and value.strip():
                return Path(self._strip_file_scheme(value.strip())).name
        url = payload.get("url")
        if isinstance(url, str) and url.strip():
            parsed = urlparse(url.strip())
            if parsed.path:
                return Path(parsed.path).name or f"{uuid.uuid4().hex}.mp4"
        return f"{uuid.uuid4().hex}.mp4"

    async def _download_video_from_url(
        self,
        event: AstrMessageEvent,
        url: str,
        suggested_name: str,
        policy: DownloadPolicy,
    ) -> Path | None:
        if not policy.quoted_video_download_enabled:
            self._debug("quoted video remote download is disabled, skipping url=%s", url)
            return None

        parsed = urlparse(url)
        suffix = Path(parsed.path).suffix or Path(suggested_name).suffix or ".mp4"
        output_path = self._make_temp_path(suffix)
        self._debug("downloading quoted video from url=%s to path=%s", url, output_path)
        max_bytes = int(policy.max_download_size_mb * 1024 * 1024) if policy.max_download_size_mb > 0 else 0
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=httpx.Timeout(float(policy.timeout_seconds)),
            ) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()

                    raw_content_length = response.headers.get("content-length", "").strip()
                    if max_bytes > 0 and raw_content_length.isdigit() and int(raw_content_length) > max_bytes:
                        logger.warning(
                            "[%s] skipped quoted video download because content-length exceeds limit: %s > %s bytes (%s)",
                            PLUGIN_ID,
                            raw_content_length,
                            max_bytes,
                            url,
                        )
                        return None

                    downloaded_bytes = 0
                    with output_path.open("wb") as file_obj:
                        async for chunk in response.aiter_bytes():
                            if not chunk:
                                continue
                            downloaded_bytes += len(chunk)
                            if max_bytes > 0 and downloaded_bytes > max_bytes:
                                logger.warning(
                                    "[%s] aborted quoted video download because size exceeds limit: %s > %s bytes (%s)",
                                    PLUGIN_ID,
                                    downloaded_bytes,
                                    max_bytes,
                                    url,
                                )
                                output_path.unlink(missing_ok=True)
                                return None
                            file_obj.write(chunk)
        except Exception as exc:
            logger.warning("[%s] failed to download quoted video from %s: %s", PLUGIN_ID, url, exc)
            output_path.unlink(missing_ok=True)
            return None
        if not output_path.exists() or output_path.stat().st_size <= 0:
            output_path.unlink(missing_ok=True)
            self._debug("downloaded quoted video is empty: %s", output_path)
            return None
        self._track_temp_file(event, output_path)
        return output_path

    async def _resolve_onebot_video_segment(
        self,
        event: AstrMessageEvent,
        payload: dict[str, Any],
        *,
        quoted: bool,
        download_policy: DownloadPolicy,
    ) -> VideoAttachment | None:
        candidates = self._collect_video_path_candidates_from_mapping(payload)
        url = self._read_str(payload.get("url"), "")
        name = self._guess_video_name_from_mapping(payload)
        self._debug(
            "resolving raw onebot %s video payload=%s candidates=%s",
            "quoted" if quoted else "direct",
            payload,
            [str(candidate) for candidate in candidates],
        )

        for candidate in candidates:
            if candidate.exists() and candidate.is_file():
                resolved = candidate.resolve()
                self._debug("resolved raw onebot video via local candidate: %s", resolved)
                return VideoAttachment(name=name or resolved.name, path=resolved, quoted=quoted)

        if url.startswith("http://") or url.startswith("https://"):
            downloaded_path = await self._download_video_from_url(
                event,
                url,
                name,
                download_policy,
            )
            if downloaded_path is not None:
                return VideoAttachment(
                    name=name or downloaded_path.name,
                    path=downloaded_path,
                    quoted=quoted,
                )

        return None

    async def _collect_aiocqhttp_reply_video_attachments(
        self,
        event: AstrMessageEvent,
        seen_paths: set[str],
        download_policy: DownloadPolicy,
    ) -> list[VideoAttachment]:
        bot = getattr(event, "bot", None)
        raw_message = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if bot is None or raw_message is None:
            return []

        reply_ids = self._extract_reply_ids_from_raw_event(raw_message)
        if not reply_ids:
            return []
        self._debug("aiocqhttp raw reply ids for quoted video fallback: %s", reply_ids)

        attachments: list[VideoAttachment] = []
        for reply_id in reply_ids:
            try:
                reply_event_data = await bot.call_action(
                    action="get_msg",
                    message_id=int(reply_id),
                )
            except Exception as exc:
                self._debug("aiocqhttp get_msg failed for reply id=%s: %s", reply_id, exc)
                continue

            for segment in self._extract_onebot_segments(reply_event_data):
                if self._safe_event_get(segment, "type", "") != "video":
                    continue
                payload = self._safe_event_get(segment, "data", {})
                if not isinstance(payload, dict):
                    continue
                attachment = await self._resolve_onebot_video_segment(
                    event,
                    payload,
                    quoted=True,
                    download_policy=download_policy,
                )
                if attachment is None:
                    continue
                key = str(attachment.path)
                if key in seen_paths:
                    continue
                seen_paths.add(key)
                attachments.append(attachment)

        if attachments:
            self._debug(
                "collected %s quoted video attachment(s) from aiocqhttp raw reply fallback",
                len(attachments),
            )
        return attachments

    async def _resolve_video_component_path(
        self,
        video: Video,
        *,
        quoted: bool,
    ) -> Path | None:
        component_desc = self._describe_video_component(video)
        candidates = self._collect_video_path_candidates(video)
        if candidates:
            self._debug(
                "trying to resolve %s video component from candidates=%s component=%s",
                "quoted" if quoted else "direct",
                [str(candidate) for candidate in candidates],
                component_desc,
            )

        for candidate in candidates:
            if candidate.exists() and candidate.is_file():
                resolved = candidate.resolve()
                self._debug(
                    "resolved %s video component via local candidate: %s",
                    "quoted" if quoted else "direct",
                    resolved,
                )
                return resolved

        try:
            resolved = Path(await video.convert_to_file_path())
        except Exception as exc:
            if quoted:
                self._debug(
                    "skipped unresolved quoted video component: %s component=%s",
                    exc,
                    component_desc,
                )
                return None
            logger.warning("[%s] failed to resolve video path from event: %s", PLUGIN_ID, exc)
            self._debug("direct video component details: %s", component_desc)
            return None

        if resolved.exists() and resolved.is_file():
            self._debug(
                "resolved %s video component via convert_to_file_path: %s",
                "quoted" if quoted else "direct",
                resolved,
            )
            return resolved

        if quoted:
            self._debug(
                "convert_to_file_path returned a non-file path for quoted video: %s component=%s",
                resolved,
                component_desc,
            )
            return None

        logger.warning("[%s] resolved video path is not a file: %s", PLUGIN_ID, resolved)
        self._debug("direct video component returned non-file path, details: %s", component_desc)
        return None

    @staticmethod
    def _read_content_part_text(part: Any) -> str | None:
        if isinstance(part, TextPart):
            return part.text
        if isinstance(part, dict) and part.get("type") == "text":
            text = part.get("text")
            return text if isinstance(text, str) else None
        return None

    @staticmethod
    def _parse_video_attachment_marker(
        text: str,
        *,
        source_part_index: int | None,
    ) -> VideoAttachment | None:
        prefixes = (
            ("[Video Attachment: name ", False),
            ("[Video Attachment in quoted message: name ", True),
        )
        for prefix, quoted in prefixes:
            if not text.startswith(prefix) or not text.endswith("]"):
                continue
            body = text[len(prefix) : -1]
            if ", path " not in body:
                continue
            name, path_str = body.rsplit(", path ", 1)
            path = Path(path_str.strip())
            return VideoAttachment(
                name=name.strip() or path.name,
                path=path,
                quoted=quoted,
                source_part_index=source_part_index,
            )
        return None

    def _collect_video_attachment_markers(
        self,
        req: ProviderRequest,
    ) -> list[VideoAttachment]:
        attachments: list[VideoAttachment] = []
        seen_paths: set[str] = set()
        for index, part in enumerate(getattr(req, "extra_user_content_parts", [])):
            text = self._read_content_part_text(part)
            if not text:
                continue
            attachment = self._parse_video_attachment_marker(
                text,
                source_part_index=index,
            )
            if not attachment:
                continue
            key = str(attachment.path)
            if key in seen_paths:
                continue
            seen_paths.add(key)
            attachments.append(attachment)
        self._debug("collected %s video attachment marker(s) from request", len(attachments))
        return attachments

    async def _collect_video_attachments_from_event(
        self,
        event: AstrMessageEvent,
        settings: PluginSettings,
    ) -> list[VideoAttachment]:
        attachments: list[VideoAttachment] = []
        seen_paths: set[str] = set()
        messages = event.get_messages()
        self._debug("falling back to event message chain for video resolution, component_count=%s", len(messages))

        for comp in messages:
            if isinstance(comp, Video):
                path = await self._resolve_video_component_path(comp, quoted=False)
                if path is None:
                    continue
                key = str(path)
                if key not in seen_paths:
                    seen_paths.add(key)
                    attachments.append(
                        VideoAttachment(name=path.name, path=path, quoted=False),
                    )
                continue

            if isinstance(comp, Reply) and comp.chain:
                self._debug("inspecting reply chain for quoted video attachments, component_count=%s", len(comp.chain))
                for reply_comp in comp.chain:
                    if not isinstance(reply_comp, Video):
                        continue
                    path = await self._resolve_video_component_path(reply_comp, quoted=True)
                    if path is None:
                        continue
                    key = str(path)
                    if key in seen_paths:
                        continue
                    seen_paths.add(key)
                    attachments.append(
                        VideoAttachment(name=path.name, path=path, quoted=True),
                    )

        aiocqhttp_reply_attachments = await self._collect_aiocqhttp_reply_video_attachments(
            event,
            seen_paths,
            settings.download,
        )
        attachments.extend(aiocqhttp_reply_attachments)
        self._debug("collected %s video attachment(s) from event fallback", len(attachments))
        return attachments

    async def _collect_video_attachments(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
        settings: PluginSettings,
    ) -> list[VideoAttachment]:
        attachments = self._collect_video_attachment_markers(req)
        if attachments:
            self._debug("using request markers as the video attachment source")
            return attachments
        self._debug("no video markers were injected by core, falling back to event parsing")
        return await self._collect_video_attachments_from_event(event, settings)

    @staticmethod
    def _track_temp_file(event: AstrMessageEvent, path: Path) -> None:
        tracker = getattr(event, "track_temporary_local_file", None)
        if callable(tracker):
            tracker(str(path))

    @staticmethod
    def _make_temp_path(suffix: str) -> Path:
        temp_root = Path(tempfile.gettempdir())
        return temp_root / f"{PLUGIN_ID}_{uuid.uuid4().hex}{suffix}"

    @staticmethod
    def _safe_parse_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _normalize_endpoint(endpoint: str) -> str:
        endpoint = endpoint.strip()
        if not endpoint:
            return ""
        endpoint = endpoint.rstrip("/")
        if endpoint.endswith("/audio/transcriptions"):
            return endpoint
        return endpoint + "/audio/transcriptions"

    @staticmethod
    def _get_provider_config(provider: Any) -> dict[str, Any]:
        provider_config = getattr(provider, "provider_config", {})
        return provider_config if isinstance(provider_config, dict) else {}

    def _get_provider_id(self, provider: Any) -> str:
        return self._read_str(self._get_provider_config(provider).get("id"), "")

    def _get_provider_label(self, provider: Any) -> str:
        provider_config = self._get_provider_config(provider)
        provider_type = self._read_str(
            provider_config.get("type"),
            provider.__class__.__name__,
        )
        provider_id = self._get_provider_id(provider)
        if provider_id:
            return f"{provider_type}({provider_id})"
        return provider_type

    def _get_all_astrbot_stt_providers(self) -> list[Any]:
        getter = getattr(self.context, "get_all_stt_providers", None)
        if callable(getter):
            try:
                providers = getter()
            except Exception as exc:
                logger.warning("[%s] failed to enumerate AstrBot STT providers: %s", PLUGIN_ID, exc)
            else:
                if providers:
                    return list(providers)

        provider_manager = getattr(self.context, "provider_manager", None)
        providers = getattr(provider_manager, "stt_provider_insts", None)
        if providers:
            return list(providers)
        return []

    def _get_configured_astrbot_stt_provider(
        self,
        event: AstrMessageEvent,
    ) -> Any | None:
        getter = getattr(self.context, "get_using_stt_provider", None)
        if not callable(getter):
            return None

        unified_msg_origin = getattr(event, "unified_msg_origin", None)
        if isinstance(unified_msg_origin, str) and unified_msg_origin:
            try:
                provider = getter(unified_msg_origin)
            except Exception as exc:
                logger.warning(
                    "[%s] failed to resolve session STT provider for %s: %s",
                    PLUGIN_ID,
                    unified_msg_origin,
                    exc,
                )
            else:
                if provider is not None:
                    return provider

        try:
            return getter()
        except Exception as exc:
            logger.warning("[%s] failed to resolve default STT provider: %s", PLUGIN_ID, exc)
            return None

    def _select_astrbot_stt_provider(
        self,
        event: AstrMessageEvent,
        policy: SttPolicy,
    ) -> Any | None:
        providers = self._get_all_astrbot_stt_providers()
        if not providers:
            logger.warning("[%s] AstrBot STT backend requested but no STT provider is available", PLUGIN_ID)
            return None
        self._debug("available AstrBot STT providers: %s", [self._get_provider_label(provider) for provider in providers])

        if policy.astrbot_provider_id:
            for provider in providers:
                if self._get_provider_id(provider) == policy.astrbot_provider_id:
                    self._debug("selected AstrBot STT provider by explicit id: %s", self._get_provider_label(provider))
                    return provider
            logger.warning(
                "[%s] AstrBot STT provider '%s' was not found, falling back to the configured provider",
                PLUGIN_ID,
                policy.astrbot_provider_id,
            )

        provider = self._get_configured_astrbot_stt_provider(event)
        if provider is not None:
            self._debug("selected AstrBot STT provider from session/default config: %s", self._get_provider_label(provider))
            return provider

        self._debug("falling back to the first available AstrBot STT provider: %s", self._get_provider_label(providers[0]))
        return providers[0]

    @staticmethod
    def _finalize_transcript(
        transcript: Any,
        policy: SttPolicy,
    ) -> str | None:
        normalized = " ".join(str(transcript or "").split())
        if not normalized:
            return None
        if len(normalized) > policy.max_transcript_chars:
            normalized = normalized[: policy.max_transcript_chars].rstrip() + "..."
        return normalized

    @staticmethod
    def _truncate_text(text: str, max_chars: int) -> str:
        if max_chars <= 0 or len(text) <= max_chars:
            return text
        if max_chars <= 3:
            return text[:max_chars]
        return text[: max_chars - 3].rstrip() + "..."

    def _limit_transcript_chunks(
        self,
        chunks: list[TranscriptChunk],
        max_total_chars: int,
    ) -> list[TranscriptChunk]:
        if max_total_chars <= 0:
            return chunks

        result: list[TranscriptChunk] = []
        remaining_chars = max_total_chars
        for chunk in chunks:
            if remaining_chars <= 0:
                break
            text = self._truncate_text(chunk.text, remaining_chars)
            if not text.strip():
                break
            result.append(replace(chunk, text=text))
            remaining_chars -= len(text)
        return result

    @staticmethod
    def _remaining_processing_seconds(
        started_at: float,
        policy: RuntimePolicy,
    ) -> float | None:
        if policy.max_processing_seconds_per_video <= 0:
            return None
        return max(
            0.0,
            float(policy.max_processing_seconds_per_video) - (time.monotonic() - started_at),
        )

    @staticmethod
    def _resolve_timeout_seconds(
        configured_seconds: int | float,
        remaining_seconds: float | None,
        *,
        minimum_seconds: float = 1.0,
    ) -> float:
        configured = max(float(configured_seconds), minimum_seconds)
        if remaining_seconds is None:
            return configured
        return max(minimum_seconds, min(configured, remaining_seconds))

    def _is_processing_budget_exhausted(
        self,
        attachment: VideoAttachment,
        started_at: float,
        policy: RuntimePolicy,
        *,
        stage: str,
    ) -> bool:
        remaining_seconds = self._remaining_processing_seconds(started_at, policy)
        if remaining_seconds is None or remaining_seconds > 0:
            return False
        logger.warning(
            "[%s] skipped remaining work for %s because processing time limit was reached at stage=%s",
            PLUGIN_ID,
            attachment.path,
            stage,
        )
        return True

    @staticmethod
    def _clamp_timestamp(timestamp_seconds: float, duration_seconds: float) -> float:
        if duration_seconds <= 0:
            return 0.0
        max_timestamp = max(duration_seconds - 0.05, 0.0)
        return max(0.0, min(timestamp_seconds, max_timestamp))

    def _sample_uniform_positions(
        self,
        start_seconds: float,
        end_seconds: float,
        count: int,
    ) -> list[float]:
        if count <= 0:
            return []
        if count == 1 or end_seconds <= start_seconds:
            return [round(start_seconds, 3)]
        step = (end_seconds - start_seconds) / float(count - 1)
        return [round(start_seconds + step * index, 3) for index in range(count)]

    def _fill_uniform_positions(
        self,
        existing: list[float],
        *,
        target_count: int,
        max_start: float,
    ) -> list[float]:
        if len(existing) >= target_count:
            return existing[:target_count]
        needed = target_count - len(existing)
        candidates = self._sample_uniform_positions(0.0, max_start, needed + len(existing))
        merged = sorted(self._dedupe_timestamps(existing + candidates))
        return merged[:target_count]

    def _build_segment_start_positions(
        self,
        duration_seconds: float,
        segment_duration_seconds: float,
        segment_count: int,
        selection_mode: str,
    ) -> list[float]:
        if segment_count <= 1:
            return [0.0]

        max_start = max(duration_seconds - segment_duration_seconds, 0.0)
        if max_start <= 0:
            return [0.0]

        if selection_mode == "head_only":
            return self._sample_uniform_positions(0.0, min(max_start, segment_duration_seconds * (segment_count - 1)), segment_count)

        if selection_mode == "head_tail":
            head_count = int(math.ceil(segment_count / 2))
            tail_count = segment_count - head_count
            head_positions = [
                min(max_start, segment_duration_seconds * index)
                for index in range(head_count)
            ]
            tail_positions = [
                max(0.0, max_start - segment_duration_seconds * index)
                for index in range(tail_count)
            ]
            merged = sorted(self._dedupe_timestamps(head_positions + tail_positions))
            return self._fill_uniform_positions(
                merged,
                target_count=segment_count,
                max_start=max_start,
            )

        return self._sample_uniform_positions(0.0, max_start, segment_count)

    def _build_video_segments(
        self,
        duration_seconds: float,
        coverage_budget_seconds: float,
        policy: SegmentPolicy,
    ) -> list[VideoSegment]:
        total_duration = max(duration_seconds, 0.0)
        if total_duration <= 0:
            return [VideoSegment(index=1, start_seconds=0.0, duration_seconds=0.0, label="\u7247\u6bb5 1")]

        coverage_budget = min(total_duration, max(coverage_budget_seconds, 0.0)) or total_duration
        if (
            not policy.enabled
            or total_duration <= float(policy.min_video_duration_seconds)
            or coverage_budget <= float(policy.segment_duration_seconds)
            or total_duration <= float(policy.segment_duration_seconds)
        ):
            return [
                VideoSegment(
                    index=1,
                    start_seconds=0.0,
                    duration_seconds=round(min(total_duration, coverage_budget), 3),
                    label="\u7247\u6bb5 1",
                ),
            ]

        segment_duration = min(float(policy.segment_duration_seconds), total_duration)
        segment_count = min(
            policy.max_segments_per_video,
            max(1, int(math.ceil(coverage_budget / max(segment_duration, 0.001)))),
        )
        starts = self._build_segment_start_positions(
            total_duration,
            segment_duration,
            segment_count,
            policy.selection_mode,
        )

        segments: list[VideoSegment] = []
        remaining_budget = coverage_budget
        for index, start_seconds in enumerate(starts, start=1):
            if remaining_budget <= 0:
                break
            actual_duration = min(
                segment_duration,
                total_duration - start_seconds,
                remaining_budget,
            )
            if actual_duration <= 0:
                continue
            segments.append(
                VideoSegment(
                    index=index,
                    start_seconds=round(start_seconds, 3),
                    duration_seconds=round(actual_duration, 3),
                    label=f"\u7247\u6bb5 {index}",
                ),
            )
            remaining_budget -= actual_duration

        return segments or [
            VideoSegment(
                index=1,
                start_seconds=0.0,
                duration_seconds=round(min(total_duration, coverage_budget), 3),
                label="\u7247\u6bb5 1",
            ),
        ]

    @staticmethod
    def _segment_key(segment: VideoSegment) -> str:
        return f"{segment.start_seconds:.3f}-{segment.duration_seconds:.3f}"

    @staticmethod
    def _distribute_items(total_count: int, bucket_count: int) -> list[int]:
        if total_count <= 0 or bucket_count <= 0:
            return []
        base_count = total_count // bucket_count
        remainder = total_count % bucket_count
        return [
            base_count + (1 if index < remainder else 0)
            for index in range(bucket_count)
        ]

    def _run_command(
        self,
        command: list[str],
        *,
        timeout_seconds: int,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )

    def _probe_video_info(
        self,
        video_path: Path,
        policy: FFmpegPolicy,
        *,
        timeout_seconds: int | None = None,
    ) -> VideoProbeInfo | None:
        command = [
            policy.ffprobe_path,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(video_path),
        ]
        try:
            result = self._run_command(
                command,
                timeout_seconds=timeout_seconds or policy.command_timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("[%s] ffprobe failed for %s: %s", PLUGIN_ID, video_path, exc)
            return None

        if result.returncode != 0:
            logger.warning(
                "[%s] ffprobe returned %s for %s: %s",
                PLUGIN_ID,
                result.returncode,
                video_path,
                result.stderr.strip(),
            )
            return None

        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            logger.warning("[%s] ffprobe output is not valid json for %s: %s", PLUGIN_ID, video_path, exc)
            return None

        streams = payload.get("streams") or []
        format_info = payload.get("format") or {}

        has_video_stream = any(stream.get("codec_type") == "video" for stream in streams)
        has_audio_stream = any(stream.get("codec_type") == "audio" for stream in streams)
        duration_seconds = self._safe_parse_float(format_info.get("duration"), 0.0)
        if duration_seconds <= 0:
            for stream in streams:
                stream_duration = self._safe_parse_float(stream.get("duration"), 0.0)
                if stream_duration > duration_seconds:
                    duration_seconds = stream_duration

        probe_info = VideoProbeInfo(
            duration_seconds=max(0.0, duration_seconds),
            has_video_stream=has_video_stream,
            has_audio_stream=has_audio_stream,
        )
        self._debug(
            "ffprobe info for %s: duration=%.3f has_video=%s has_audio=%s",
            video_path,
            probe_info.duration_seconds,
            probe_info.has_video_stream,
            probe_info.has_audio_stream,
        )
        return probe_info

    @staticmethod
    def _dedupe_timestamps(values: list[float]) -> list[float]:
        result: list[float] = []
        seen: set[float] = set()
        for value in values:
            rounded = round(max(0.0, value), 3)
            if rounded in seen:
                continue
            seen.add(rounded)
            result.append(rounded)
        return result

    def _decide_frame_timestamps(
        self,
        duration_seconds: float,
        policy: FramePolicy,
        *,
        frame_count: int | None = None,
    ) -> list[float]:
        target_duration = max(duration_seconds, 0.0)
        target_count = frame_count if frame_count is not None else policy.max_frames_per_video
        if target_count <= 1 or target_duration <= 0:
            return [0.0]

        if policy.sampling_mode == "fixed_interval":
            timestamps: list[float] = [0.0]
            cursor = policy.fixed_interval_seconds
            while cursor < target_duration and len(timestamps) < target_count:
                timestamps.append(cursor)
                cursor += policy.fixed_interval_seconds
            if len(timestamps) < target_count:
                timestamps.append(self._clamp_timestamp(target_duration, target_duration))
            return self._dedupe_timestamps(timestamps[:target_count])

        if policy.sampling_mode == "head_tail":
            end_timestamp = self._clamp_timestamp(target_duration, target_duration)
            if target_count == 2:
                return self._dedupe_timestamps([0.0, end_timestamp])

            head_count = target_count // 2
            tail_count = target_count // 2
            middle_count = target_count - head_count - tail_count
            head_window_end = max(target_duration * 0.35, 0.0)
            tail_window_start = min(max(target_duration * 0.65, 0.0), end_timestamp)
            if tail_window_start <= head_window_end:
                return self._decide_frame_timestamps(
                    target_duration,
                    FramePolicy(
                        enabled=policy.enabled,
                        max_videos_per_request=policy.max_videos_per_request,
                        sampling_mode="uniform",
                        max_frames_per_video=policy.max_frames_per_video,
                        fixed_interval_seconds=policy.fixed_interval_seconds,
                        max_side=policy.max_side,
                        jpeg_quality=policy.jpeg_quality,
                        max_video_duration_seconds=policy.max_video_duration_seconds,
                    ),
                    frame_count=target_count,
                )

            timestamps: list[float] = []
            if head_count > 0:
                timestamps.extend(self._sample_uniform_positions(0.0, head_window_end, head_count))
            if middle_count > 0:
                timestamps.append(round(target_duration / 2.0, 3))
            if tail_count > 0:
                if tail_count == 1:
                    timestamps.append(end_timestamp)
                else:
                    timestamps.extend(self._sample_uniform_positions(tail_window_start, end_timestamp, tail_count))
            return self._dedupe_timestamps(timestamps)

        step = target_duration / max(target_count - 1, 1)
        timestamps = [step * index for index in range(target_count)]
        return self._dedupe_timestamps(timestamps)

    @staticmethod
    def _jpeg_quality_to_qscale(quality: int) -> int:
        qscale = int(round((100 - quality) / 3.0)) + 2
        return max(2, min(31, qscale))

    def _extract_frame(
        self,
        video_path: Path,
        timestamp_seconds: float,
        output_path: Path,
        *,
        policy: FramePolicy,
        ffmpeg_policy: FFmpegPolicy,
        timeout_seconds: int | None = None,
    ) -> bool:
        command = [
            ffmpeg_policy.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video_path),
            "-ss",
            f"{timestamp_seconds:.3f}",
            "-frames:v",
            "1",
            "-vf",
            f"scale={policy.max_side}:{policy.max_side}:force_original_aspect_ratio=decrease",
            "-q:v",
            str(self._jpeg_quality_to_qscale(policy.jpeg_quality)),
            "-pix_fmt",
            "yuvj420p",
            str(output_path),
        ]
        try:
            result = self._run_command(
                command,
                timeout_seconds=timeout_seconds or ffmpeg_policy.command_timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning(
                "[%s] ffmpeg frame extraction failed for %s at %.3fs: %s",
                PLUGIN_ID,
                video_path,
                timestamp_seconds,
                exc,
            )
            return False

        if result.returncode != 0:
            logger.warning(
                "[%s] ffmpeg returned %s while extracting frame from %s: %s",
                PLUGIN_ID,
                result.returncode,
                video_path,
                result.stderr.strip(),
            )
            return False

        return output_path.exists() and output_path.stat().st_size > 0

    def _extract_audio(
        self,
        video_path: Path,
        output_path: Path,
        *,
        start_seconds: float = 0.0,
        duration_seconds: float | None = None,
        policy: AudioPolicy,
        ffmpeg_policy: FFmpegPolicy,
        timeout_seconds: int | None = None,
    ) -> bool:
        command = [
            ffmpeg_policy.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{max(0.0, start_seconds):.3f}",
            "-i",
            str(video_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(policy.sample_rate),
            "-t",
            str(duration_seconds or policy.max_audio_duration_seconds),
            "-f",
            "wav",
            str(output_path),
        ]
        try:
            result = self._run_command(
                command,
                timeout_seconds=timeout_seconds or ffmpeg_policy.command_timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("[%s] ffmpeg audio extraction failed for %s: %s", PLUGIN_ID, video_path, exc)
            return False

        if result.returncode != 0:
            logger.warning(
                "[%s] ffmpeg returned %s while extracting audio from %s: %s",
                PLUGIN_ID,
                result.returncode,
                video_path,
                result.stderr.strip(),
            )
            return False

        return output_path.exists() and output_path.stat().st_size > 0

    async def _transcribe_audio(
        self,
        event: AstrMessageEvent,
        audio_path: Path,
        policy: SttPolicy,
        *,
        timeout_seconds: float | None = None,
    ) -> str | None:
        if policy.backend == "disabled":
            return None
        if policy.backend == "astrbot_configured":
            provider = self._select_astrbot_stt_provider(event, policy)
            if provider is None:
                return None
            self._debug(
                "transcribing audio via AstrBot STT provider %s: %s",
                self._get_provider_label(provider),
                audio_path,
            )
            try:
                transcript = await provider.get_text(audio_url=str(audio_path))
            except Exception as exc:
                logger.warning(
                    "[%s] AstrBot STT provider %s failed for %s: %s",
                    PLUGIN_ID,
                    self._get_provider_label(provider),
                    audio_path,
                    exc,
                )
                return None
            return self._finalize_transcript(transcript, policy)

        if policy.backend != "openai_compatible":
            return None
        if not policy.endpoint or not policy.api_key or not policy.model:
            logger.warning(
                "[%s] stt requested but endpoint/api_key/model is incomplete",
                PLUGIN_ID,
            )
            return None

        endpoint = self._normalize_endpoint(policy.endpoint)
        self._debug(
            "transcribing audio via openai-compatible STT endpoint=%s model=%s language=%s file=%s",
            endpoint,
            policy.model,
            policy.language or "auto",
            audio_path,
        )
        headers = {"Authorization": f"Bearer {policy.api_key}"}
        data: dict[str, Any] = {
            "model": policy.model,
            "temperature": str(policy.temperature),
        }
        if policy.language:
            data["language"] = policy.language
        if policy.prompt:
            data["prompt"] = policy.prompt

        try:
            request_timeout = self._resolve_timeout_seconds(
                policy.timeout_seconds,
                timeout_seconds,
                minimum_seconds=1.0,
            )
            async with httpx.AsyncClient(timeout=request_timeout) as client:
                with audio_path.open("rb") as file_obj:
                    response = await client.post(
                        endpoint,
                        headers=headers,
                        data=data,
                        files={"file": (audio_path.name, file_obj, "audio/wav")},
                    )
            response.raise_for_status()
        except Exception as exc:
            logger.warning("[%s] stt request failed for %s: %s", PLUGIN_ID, audio_path, exc)
            return None

        transcript = ""
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            transcript = str(payload.get("text") or payload.get("transcript") or "").strip()
        else:
            transcript = response.text.strip()

        return self._finalize_transcript(transcript, policy)

    @staticmethod
    def _format_template(template: str, fallback: str, **kwargs: Any) -> str:
        try:
            return template.format(**kwargs)
        except Exception:
            return fallback.format(**kwargs)

    @staticmethod
    def _build_audio_note(audio_count: int) -> str:
        if audio_count <= 0:
            return ""
        return f"\uff0c\u5e76\u989d\u5916\u63d0\u53d6\u4e86 {audio_count} \u6761\u97f3\u9891\u4f9b\u652f\u6301\u97f3\u9891\u7684\u6a21\u578b\u4f7f\u7528"

    @staticmethod
    def _build_transcript_note(transcript_count: int) -> str:
        if transcript_count <= 0:
            return ""
        return f"\uff0c\u5e76\u9644\u52a0\u4e86 {transcript_count} \u6bb5\u97f3\u9891\u8f6c\u5199\u6587\u672c"

    def _build_summary_text(
        self,
        policy: HintPolicy,
        *,
        video_count: int,
        frame_count: int,
        segment_count: int,
        coverage_seconds: float,
        audio_count: int,
        transcript_count: int,
    ) -> str:
        return self._format_template(
            policy.summary_template,
            DEFAULT_SUMMARY_TEMPLATE,
            video_count=video_count,
            frame_count=frame_count,
            segment_count=segment_count,
            coverage_seconds=coverage_seconds,
            audio_note=self._build_audio_note(audio_count),
            transcript_note=self._build_transcript_note(transcript_count),
        )

    def _build_video_text(
        self,
        policy: HintPolicy,
        *,
        video_name: str,
        frame_count: int,
        segment_count: int,
        coverage_seconds: float,
        audio_count: int,
        transcript_count: int,
    ) -> str:
        return self._format_template(
            policy.video_template,
            DEFAULT_VIDEO_TEMPLATE,
            video_name=video_name,
            frame_count=frame_count,
            segment_count=segment_count,
            coverage_seconds=coverage_seconds,
            audio_note=self._build_audio_note(audio_count),
            transcript_note=self._build_transcript_note(transcript_count),
        )

    def _build_transcript_segment_text(
        self,
        policy: HintPolicy,
        chunk: TranscriptChunk,
    ) -> str:
        return self._format_template(
            policy.transcript_segment_template,
            DEFAULT_TRANSCRIPT_SEGMENT_TEMPLATE,
            segment_label=chunk.segment_label,
            start_seconds=chunk.start_seconds,
            end_seconds=chunk.end_seconds,
            transcript=chunk.text,
        )

    def _build_transcript_text(
        self,
        policy: HintPolicy,
        *,
        video_name: str,
        transcript_chunks: list[TranscriptChunk],
    ) -> str | None:
        if not transcript_chunks:
            return None
        transcript_body = "\n".join(
            self._build_transcript_segment_text(policy, chunk)
            for chunk in transcript_chunks
        )
        return self._format_template(
            policy.transcript_template,
            DEFAULT_TRANSCRIPT_TEMPLATE,
            video_name=video_name,
            segment_count=len(transcript_chunks),
            transcript=transcript_body,
        )

    @staticmethod
    def _has_hint_part(req: ProviderRequest, hint_text: str) -> bool:
        for part in getattr(req, "extra_user_content_parts", []):
            text = VideoVisionHelper._read_content_part_text(part)
            if text == hint_text:
                return True
        return False

    def _apply_texts(
        self,
        req: ProviderRequest,
        target: str,
        texts: list[str],
    ) -> None:
        payload = [text for text in texts if text.strip()]
        if not payload:
            return
        combined = "\n".join(payload).strip()
        self._debug("injecting %s hint text block(s) into target=%s", len(payload), target)

        if target == "system_prompt":
            if combined not in req.system_prompt:
                req.system_prompt = (
                    f"{req.system_prompt}\n\n{combined}".strip()
                    if req.system_prompt
                    else combined
                )
            return

        if target == "prompt":
            prompt = req.prompt or ""
            if combined not in prompt:
                req.prompt = f"{combined}\n\n{prompt}".strip()
            return

        for text in payload:
            if not self._has_hint_part(req, text):
                req.extra_user_content_parts.append(TextPart(text=text).mark_as_temp())

    async def _process_single_video(
        self,
        event: AstrMessageEvent,
        attachment: VideoAttachment,
        settings: PluginSettings,
    ) -> ProcessedVideo | None:
        if not attachment.path.exists() or not attachment.path.is_file():
            logger.warning("[%s] video path does not exist: %s", PLUGIN_ID, attachment.path)
            return None

        started_at = time.monotonic()
        if self._is_processing_budget_exhausted(
            attachment,
            started_at,
            settings.runtime,
            stage="probe",
        ):
            return None

        probe_timeout_seconds = int(
            math.ceil(
                self._resolve_timeout_seconds(
                    settings.ffmpeg.command_timeout_seconds,
                    self._remaining_processing_seconds(started_at, settings.runtime),
                ),
            ),
        )
        probe_info = await asyncio.to_thread(
            self._probe_video_info,
            attachment.path,
            settings.ffmpeg,
            timeout_seconds=probe_timeout_seconds,
        )
        if not probe_info or not probe_info.has_video_stream:
            logger.warning("[%s] file is not a probeable video: %s", PLUGIN_ID, attachment.path)
            return None
        self._debug(
            "processing video=%s quoted=%s duration=%.3f has_audio=%s",
            attachment.path,
            attachment.quoted,
            probe_info.duration_seconds,
            probe_info.has_audio_stream,
        )

        wants_attach = settings.audio.mode in {"attach", "attach_and_stt"}
        wants_stt = settings.audio.mode in {"stt", "attach_and_stt"}
        wants_audio_processing = probe_info.has_audio_stream and (wants_attach or wants_stt)
        frame_segments = self._build_video_segments(
            probe_info.duration_seconds,
            float(settings.frame.max_video_duration_seconds),
            settings.segment,
        ) if settings.frame.enabled else []
        audio_segments = self._build_video_segments(
            probe_info.duration_seconds,
            float(settings.audio.max_audio_duration_seconds),
            settings.segment,
        ) if wants_audio_processing else []

        if frame_segments:
            self._debug(
                "planned %s frame segment(s) for %s: %s",
                len(frame_segments),
                attachment.path,
                [
                    {
                        "label": segment.label,
                        "start": segment.start_seconds,
                        "duration": segment.duration_seconds,
                    }
                    for segment in frame_segments
                ],
            )
        if audio_segments:
            self._debug(
                "planned %s audio segment(s) for %s: %s",
                len(audio_segments),
                attachment.path,
                [
                    {
                        "label": segment.label,
                        "start": segment.start_seconds,
                        "duration": segment.duration_seconds,
                    }
                    for segment in audio_segments
                ],
            )

        frame_paths: list[Path] = []
        covered_segments: dict[str, float] = {}
        if settings.frame.enabled:
            frame_distribution = self._distribute_items(
                settings.frame.max_frames_per_video,
                len(frame_segments),
            )
            seen_timestamps: set[float] = set()
            for segment, segment_frame_count in zip(frame_segments, frame_distribution):
                if segment_frame_count <= 0:
                    continue
                if self._is_processing_budget_exhausted(
                    attachment,
                    started_at,
                    settings.runtime,
                    stage="frame_planning",
                ):
                    break

                local_timestamps = self._decide_frame_timestamps(
                    segment.duration_seconds,
                    settings.frame,
                    frame_count=segment_frame_count,
                )
                self._debug(
                    "frame timestamps for %s %s: %s",
                    attachment.path,
                    segment.label,
                    local_timestamps,
                )
                for local_index, local_timestamp in enumerate(local_timestamps, start=1):
                    if self._is_processing_budget_exhausted(
                        attachment,
                        started_at,
                        settings.runtime,
                        stage="frame_extraction",
                    ):
                        break
                    absolute_timestamp = self._clamp_timestamp(
                        segment.start_seconds + local_timestamp,
                        probe_info.duration_seconds,
                    )
                    rounded_timestamp = round(absolute_timestamp, 3)
                    if rounded_timestamp in seen_timestamps:
                        continue
                    seen_timestamps.add(rounded_timestamp)
                    output_path = self._make_temp_path(
                        f"_segment_{segment.index}_frame_{local_index}.jpg",
                    )
                    frame_timeout_seconds = int(
                        math.ceil(
                            self._resolve_timeout_seconds(
                                settings.ffmpeg.command_timeout_seconds,
                                self._remaining_processing_seconds(started_at, settings.runtime),
                            ),
                        ),
                    )
                    success = await asyncio.to_thread(
                        self._extract_frame,
                        attachment.path,
                        absolute_timestamp,
                        output_path,
                        policy=settings.frame,
                        ffmpeg_policy=settings.ffmpeg,
                        timeout_seconds=frame_timeout_seconds,
                    )
                    if not success:
                        output_path.unlink(missing_ok=True)
                        continue
                    frame_paths.append(output_path)
                    segment_key = self._segment_key(segment)
                    covered_segments[segment_key] = max(
                        covered_segments.get(segment_key, 0.0),
                        segment.duration_seconds,
                    )
                    self._track_temp_file(event, output_path)
            self._debug("extracted %s frame(s) for %s", len(frame_paths), attachment.path)

        audio_paths: list[Path] = []
        transcript_chunks: list[TranscriptChunk] = []
        if wants_audio_processing:
            remaining_audio_budget = float(settings.audio.max_audio_duration_seconds)
            for segment in audio_segments:
                if remaining_audio_budget <= 0:
                    break
                if self._is_processing_budget_exhausted(
                    attachment,
                    started_at,
                    settings.runtime,
                    stage="audio_extraction",
                ):
                    break

                audio_duration_seconds = min(segment.duration_seconds, remaining_audio_budget)
                if audio_duration_seconds <= 0:
                    break

                candidate_audio_path = self._make_temp_path(f"_segment_{segment.index}.wav")
                audio_timeout_seconds = int(
                    math.ceil(
                        self._resolve_timeout_seconds(
                            settings.ffmpeg.command_timeout_seconds,
                            self._remaining_processing_seconds(started_at, settings.runtime),
                        ),
                    ),
                )
                extracted_audio = await asyncio.to_thread(
                    self._extract_audio,
                    attachment.path,
                    candidate_audio_path,
                    start_seconds=segment.start_seconds,
                    duration_seconds=audio_duration_seconds,
                    policy=settings.audio,
                    ffmpeg_policy=settings.ffmpeg,
                    timeout_seconds=audio_timeout_seconds,
                )
                if not extracted_audio:
                    candidate_audio_path.unlink(missing_ok=True)
                    self._debug(
                        "audio extraction failed or produced no output for %s %s",
                        attachment.path,
                        segment.label,
                    )
                    continue

                self._debug(
                    "extracted audio for %s %s to %s",
                    attachment.path,
                    segment.label,
                    candidate_audio_path,
                )
                segment_key = self._segment_key(segment)
                covered_segments[segment_key] = max(
                    covered_segments.get(segment_key, 0.0),
                    audio_duration_seconds,
                )
                transcript: str | None = None
                if wants_stt and not self._is_processing_budget_exhausted(
                    attachment,
                    started_at,
                    settings.runtime,
                    stage="stt",
                ):
                    transcript = await self._transcribe_audio(
                        event,
                        candidate_audio_path,
                        settings.stt,
                        timeout_seconds=self._remaining_processing_seconds(
                            started_at,
                            settings.runtime,
                        ),
                    )
                if transcript:
                    transcript_chunks.append(
                        TranscriptChunk(
                            segment_label=segment.label,
                            start_seconds=segment.start_seconds,
                            end_seconds=segment.start_seconds + audio_duration_seconds,
                            text=transcript,
                        ),
                    )

                should_attach_audio = wants_attach or (
                    transcript is None
                    and not wants_attach
                    and settings.audio.fallback_to_attachment_on_stt_failure
                )
                if should_attach_audio:
                    audio_paths.append(candidate_audio_path)
                    self._track_temp_file(event, candidate_audio_path)
                else:
                    candidate_audio_path.unlink(missing_ok=True)

                remaining_audio_budget -= audio_duration_seconds

        transcript_chunks = self._limit_transcript_chunks(
            transcript_chunks,
            settings.stt.max_total_transcript_chars,
        )

        if not frame_paths and not audio_paths and not transcript_chunks:
            self._debug("video %s produced no usable multimodal artifacts", attachment.path)
            return None

        return ProcessedVideo(
            attachment=attachment,
            frame_paths=frame_paths,
            audio_paths=audio_paths,
            transcript_chunks=transcript_chunks,
            segment_count=len(covered_segments),
            coverage_seconds=sum(covered_segments.values()),
        )

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        settings = self._load_settings()
        if not settings.enabled:
            return

        if not settings.frame.enabled and settings.audio.mode == "disabled":
            return

        self._debug(
            "handling llm request with frame_enabled=%s audio_mode=%s stt_backend=%s hint_target=%s segment_enabled=%s download_limit_mb=%.2f processing_limit_s=%s",
            settings.frame.enabled,
            settings.audio.mode,
            settings.stt.backend,
            settings.hint.target,
            settings.segment.enabled,
            settings.download.max_download_size_mb,
            settings.runtime.max_processing_seconds_per_video or "disabled",
        )

        attachments = await self._collect_video_attachments(event, req, settings)
        if not attachments:
            self._debug("no video attachments were collected for this request")
            return

        attachments = attachments[: settings.frame.max_videos_per_request]
        self._debug("processing %s video attachment(s) after request limit", len(attachments))
        processed_results: list[ProcessedVideo] = []

        for attachment in attachments:
            logger.info("[%s] processing video attachment: %s", PLUGIN_ID, attachment.path)
            result = await self._process_single_video(event, attachment, settings)
            if result is not None:
                processed_results.append(result)

        if not processed_results:
            return

        removed_indices = {
            result.attachment.source_part_index
            for result in processed_results
            if result.attachment.source_part_index is not None
        }
        if settings.hint.remove_raw_video_marker_after_processing and removed_indices:
            req.extra_user_content_parts = [
                part
                for index, part in enumerate(req.extra_user_content_parts)
                if index not in removed_indices
            ]

        total_frame_count = 0
        attached_audio_count = 0
        transcript_count = 0
        total_segment_count = 0
        total_coverage_seconds = 0.0
        video_texts: list[str] = []
        transcript_texts: list[str] = []

        for result in processed_results:
            if result.frame_paths:
                req.image_urls.extend(str(path) for path in result.frame_paths)
                total_frame_count += len(result.frame_paths)
            if result.audio_paths:
                req.audio_urls.extend(str(path) for path in result.audio_paths)
                attached_audio_count += len(result.audio_paths)
            total_segment_count += result.segment_count
            total_coverage_seconds += result.coverage_seconds
            transcript_count += len(result.transcript_chunks)

            if settings.hint.enabled:
                video_texts.append(
                    self._build_video_text(
                        settings.hint,
                        video_name=result.attachment.name,
                        frame_count=len(result.frame_paths),
                        segment_count=result.segment_count,
                        coverage_seconds=result.coverage_seconds,
                        audio_count=len(result.audio_paths),
                        transcript_count=len(result.transcript_chunks),
                    ),
                )
                transcript_text = self._build_transcript_text(
                    settings.hint,
                    video_name=result.attachment.name,
                    transcript_chunks=result.transcript_chunks,
                )
                if transcript_text:
                    transcript_texts.append(transcript_text)

        if settings.hint.enabled:
            summary_text = self._build_summary_text(
                settings.hint,
                video_count=len(processed_results),
                frame_count=total_frame_count,
                segment_count=total_segment_count,
                coverage_seconds=total_coverage_seconds,
                audio_count=attached_audio_count,
                transcript_count=transcript_count,
            )
            self._apply_texts(
                req,
                settings.hint.target,
                [summary_text, *video_texts, *transcript_texts],
            )

        logger.info(
            "[%s] expanded %s video attachment(s): +%s frames, +%s audios, +%s transcripts across %s segment(s)",
            PLUGIN_ID,
            len(processed_results),
            total_frame_count,
            attached_audio_count,
            transcript_count,
            total_segment_count,
        )
