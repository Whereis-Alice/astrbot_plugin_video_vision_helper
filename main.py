"""
AstrBot plugin: Video Vision Helper.

Convert video attachments into sampled JPEG frames and optional audio or
transcripts so multimodal models can reason about video content without a
native video input channel.
"""

from __future__ import annotations

import asyncio
import contextlib
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
from astrbot.core.agent.message import ImageURLPart, TextPart
from astrbot.core.message.components import Reply, Video
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path


PLUGIN_ID = "astrbot_plugin_video_vision_helper"
PLUGIN_VERSION = "0.4.3"
PLUGIN_DESC = "\u5c06\u89c6\u9891\u62c6\u89e3\u4e3a\u5173\u952e\u5e27\u3001\u53ef\u9009\u97f3\u9891\u4e0e\u8f6c\u5199\u6587\u672c\uff0c\u5e76\u63d0\u4f9b\u63d2\u4ef6\u4e34\u65f6\u6587\u4ef6\u6e05\u7406\u7b56\u7565"
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
DEFAULT_SKIPPED_VIDEO_TEMPLATE = (
    "[\u89c6\u9891\u5904\u7406\u63d0\u793a][{video_name}] \u8be5\u89c6\u9891\u672a\u88ab\u63d2\u4ef6\u5904\u7406\uff1a{reason}\u3002"
    "{detail}"
)


@dataclass(frozen=True)
class FFmpegPolicy:
    ffmpeg_path: str
    ffprobe_path: str
    command_timeout_seconds: int
    frame_seek_mode: str


@dataclass(frozen=True)
class FramePolicy:
    enabled: bool
    max_videos_per_request: int
    sampling_mode: str
    max_frames_per_video: int
    max_long_video_frames_per_video: int
    persist_sampled_frames_to_history: bool
    max_images_per_request: int
    max_total_frame_bytes_mb: float
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
    log_transcript_preview: bool
    transcript_preview_chars: int


@dataclass(frozen=True)
class HintPolicy:
    enabled: bool
    target: str
    remove_raw_video_marker_after_processing: bool
    summary_template: str
    video_template: str
    transcript_template: str
    transcript_segment_template: str
    skipped_video_template: str


@dataclass(frozen=True)
class DownloadPolicy:
    quoted_video_download_enabled: bool
    max_download_size_mb: float
    timeout_seconds: int


@dataclass(frozen=True)
class RuntimePolicy:
    max_processing_seconds_per_video: int


@dataclass(frozen=True)
class CleanupPolicy:
    enabled: bool
    cleanup_on_startup: bool
    cleanup_after_request: bool
    ttl_hours: int
    min_cleanup_interval_minutes: int
    max_files_per_run: int


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
    cleanup: CleanupPolicy


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


@dataclass(frozen=True)
class SkippedVideoNotice:
    video_name: str
    reason: str
    detail: str = ""


@dataclass(frozen=True)
class FrameReference:
    result_index: int
    path: Path


@dataclass(frozen=True)
class DownloadedVideo:
    path: Path | None = None
    skipped_notice: SkippedVideoNotice | None = None


@dataclass(frozen=True)
class ResolvedVideoSegment:
    attachment: VideoAttachment | None = None
    skipped_notice: SkippedVideoNotice | None = None


@dataclass(frozen=True)
class CollectedVideoAttachments:
    attachments: list[VideoAttachment]
    skipped_notices: list[SkippedVideoNotice]


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
        self._last_cleanup_at = 0.0
        self._cleanup_task: asyncio.Task[None] | None = None

    async def initialize(self) -> None:
        logger.info("[%s] plugin initialized", PLUGIN_ID)
        settings = self._load_settings()
        if settings.enabled and settings.cleanup.enabled and settings.cleanup.cleanup_on_startup:
            await self._run_cleanup(settings.cleanup, reason="startup", force=True)

    async def terminate(self) -> None:
        if self._cleanup_task is not None and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cleanup_task
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
        cleanup_conf = self._config_section("cleanup_policy")

        sampling_mode = self._read_str(frame_conf.get("sampling_mode"), "uniform")
        if sampling_mode not in {"uniform", "fixed_interval", "head_tail"}:
            sampling_mode = "uniform"

        persist_sampled_frames_to_history = self._read_bool(
            frame_conf.get("persist_sampled_frames_to_history"),
            False,
        )
        legacy_do_not_persist = frame_conf.get("do_not_persist_sampled_frames")
        if legacy_do_not_persist is not None:
            persist_sampled_frames_to_history = not self._read_bool(
                legacy_do_not_persist,
                True,
            )

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

        frame_seek_mode = self._read_str(
            ffmpeg_conf.get("frame_seek_mode"),
            "fast",
        ).strip().lower()
        if frame_seek_mode not in {"fast", "accurate"}:
            frame_seek_mode = "fast"

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
                frame_seek_mode=frame_seek_mode,
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
                    10,
                    minimum=1,
                    maximum=24,
                ),
                max_long_video_frames_per_video=self._read_int(
                    frame_conf.get("max_long_video_frames_per_video"),
                    24,
                    minimum=1,
                    maximum=64,
                ),
                persist_sampled_frames_to_history=persist_sampled_frames_to_history,
                max_images_per_request=self._read_int(
                    frame_conf.get("max_images_per_request"),
                    32,
                    minimum=1,
                    maximum=128,
                ),
                max_total_frame_bytes_mb=self._read_float(
                    frame_conf.get("max_total_frame_bytes_mb"),
                    15.0,
                    minimum=0.0,
                    maximum=256.0,
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
                    90,
                    minimum=1,
                    maximum=60 * 60,
                ),
            ),
            segment=SegmentPolicy(
                enabled=self._read_bool(segment_conf.get("enabled"), True),
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
                log_transcript_preview=self._read_bool(
                    stt_conf.get("log_transcript_preview"),
                    False,
                ),
                transcript_preview_chars=self._read_int(
                    stt_conf.get("transcript_preview_chars"),
                    300,
                    minimum=32,
                    maximum=5000,
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
                skipped_video_template=self._read_str(
                    hint_conf.get("skipped_video_template"),
                    DEFAULT_SKIPPED_VIDEO_TEMPLATE,
                ),
            ),
            download=DownloadPolicy(
                quoted_video_download_enabled=self._read_bool(
                    download_conf.get("quoted_video_download_enabled"),
                    True,
                ),
                max_download_size_mb=self._read_float(
                    download_conf.get("max_download_size_mb"),
                    128.0,
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
            cleanup=CleanupPolicy(
                enabled=self._read_bool(cleanup_conf.get("enabled"), True),
                cleanup_on_startup=self._read_bool(
                    cleanup_conf.get("cleanup_on_startup"),
                    True,
                ),
                cleanup_after_request=self._read_bool(
                    cleanup_conf.get("cleanup_after_request"),
                    True,
                ),
                ttl_hours=self._read_int(
                    cleanup_conf.get("ttl_hours"),
                    24,
                    minimum=1,
                    maximum=24 * 30,
                ),
                min_cleanup_interval_minutes=self._read_int(
                    cleanup_conf.get("min_cleanup_interval_minutes"),
                    30,
                    minimum=0,
                    maximum=24 * 60,
                ),
                max_files_per_run=self._read_int(
                    cleanup_conf.get("max_files_per_run"),
                    200,
                    minimum=1,
                    maximum=10000,
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
        *,
        declared_size_bytes: int | None = None,
    ) -> DownloadedVideo:
        if not policy.quoted_video_download_enabled:
            self._debug("quoted video remote download is disabled, skipping url=%s", url)
            return DownloadedVideo(
                skipped_notice=SkippedVideoNotice(
                    video_name=suggested_name,
                    reason="远程视频下载已关闭",
                    detail="请在插件配置中打开“允许从 quoted video 的远程 URL 下载文件”，或让平台提供本地视频路径。",
                ),
            )

        parsed = urlparse(url)
        suffix = Path(parsed.path).suffix or Path(suggested_name).suffix or ".mp4"
        output_path = self._make_temp_path(suffix)
        self._debug("downloading quoted video from url=%s to path=%s", url, output_path)
        max_bytes = int(policy.max_download_size_mb * 1024 * 1024) if policy.max_download_size_mb > 0 else 0
        if max_bytes > 0 and declared_size_bytes is not None and declared_size_bytes > max_bytes:
            logger.warning(
                "[%s] skipped quoted video download because declared file_size exceeds limit: %s > %s bytes (%s)",
                PLUGIN_ID,
                declared_size_bytes,
                max_bytes,
                url,
            )
            return DownloadedVideo(
                skipped_notice=SkippedVideoNotice(
                    video_name=suggested_name,
                    reason="远程视频超过下载体积限制",
                    detail=(
                        f"平台上报文件大小约 {self._format_size(declared_size_bytes)}，"
                        f"当前限制为 {self._format_size(max_bytes)}。"
                    ),
                ),
            )

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
                        return DownloadedVideo(
                            skipped_notice=SkippedVideoNotice(
                                video_name=suggested_name,
                                reason="远程视频超过下载体积限制",
                                detail=(
                                    f"响应头显示文件大小约 {self._format_size(int(raw_content_length))}，"
                                    f"当前限制为 {self._format_size(max_bytes)}。"
                                ),
                            ),
                        )

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
                                return DownloadedVideo(
                                    skipped_notice=SkippedVideoNotice(
                                        video_name=suggested_name,
                                        reason="远程视频超过下载体积限制",
                                        detail=(
                                            f"已下载内容超过 {self._format_size(max_bytes)} 后中止，"
                                            f"当前配置不处理更大的视频。"
                                        ),
                                    ),
                                )
                            file_obj.write(chunk)
        except Exception as exc:
            logger.warning("[%s] failed to download quoted video from %s: %s", PLUGIN_ID, url, exc)
            output_path.unlink(missing_ok=True)
            return DownloadedVideo(
                skipped_notice=SkippedVideoNotice(
                    video_name=suggested_name,
                    reason="远程视频下载失败",
                    detail=f"下载请求未成功：{exc}",
                ),
            )
        if not output_path.exists() or output_path.stat().st_size <= 0:
            output_path.unlink(missing_ok=True)
            self._debug("downloaded quoted video is empty: %s", output_path)
            return DownloadedVideo(
                skipped_notice=SkippedVideoNotice(
                    video_name=suggested_name,
                    reason="远程视频下载结果为空",
                    detail="平台返回了视频 URL，但插件没有获得可用的视频文件。",
                ),
            )
        self._track_temp_file(event, output_path)
        return DownloadedVideo(path=output_path)

    async def _resolve_onebot_video_segment(
        self,
        event: AstrMessageEvent,
        payload: dict[str, Any],
        *,
        quoted: bool,
        download_policy: DownloadPolicy,
    ) -> ResolvedVideoSegment:
        candidates = self._collect_video_path_candidates_from_mapping(payload)
        url = self._read_str(payload.get("url"), "")
        name = self._guess_video_name_from_mapping(payload)
        declared_size_bytes = self._safe_parse_positive_int(payload.get("file_size"))
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
                return ResolvedVideoSegment(
                    attachment=VideoAttachment(name=name or resolved.name, path=resolved, quoted=quoted),
                )

        if url.startswith("http://") or url.startswith("https://"):
            downloaded_video = await self._download_video_from_url(
                event,
                url,
                name,
                download_policy,
                declared_size_bytes=declared_size_bytes,
            )
            if downloaded_video.path is not None:
                return ResolvedVideoSegment(
                    attachment=VideoAttachment(
                        name=name or downloaded_video.path.name,
                        path=downloaded_video.path,
                        quoted=quoted,
                    ),
                )
            if downloaded_video.skipped_notice is not None:
                return ResolvedVideoSegment(skipped_notice=downloaded_video.skipped_notice)

        return ResolvedVideoSegment()

    async def _collect_aiocqhttp_reply_video_attachments(
        self,
        event: AstrMessageEvent,
        seen_paths: set[str],
        download_policy: DownloadPolicy,
    ) -> CollectedVideoAttachments:
        bot = getattr(event, "bot", None)
        raw_message = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if bot is None or raw_message is None:
            return CollectedVideoAttachments(attachments=[], skipped_notices=[])

        reply_ids = self._extract_reply_ids_from_raw_event(raw_message)
        if not reply_ids:
            return CollectedVideoAttachments(attachments=[], skipped_notices=[])
        self._debug("aiocqhttp raw reply ids for quoted video fallback: %s", reply_ids)

        attachments: list[VideoAttachment] = []
        skipped_notices: list[SkippedVideoNotice] = []
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
                resolved = await self._resolve_onebot_video_segment(
                    event,
                    payload,
                    quoted=True,
                    download_policy=download_policy,
                )
                if resolved.skipped_notice is not None:
                    skipped_notices.append(resolved.skipped_notice)
                attachment = resolved.attachment
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
        if skipped_notices:
            self._debug(
                "collected %s skipped quoted video notice(s) from aiocqhttp raw reply fallback",
                len(skipped_notices),
            )
        return CollectedVideoAttachments(
            attachments=attachments,
            skipped_notices=skipped_notices,
        )

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
    ) -> CollectedVideoAttachments:
        attachments: list[VideoAttachment] = []
        skipped_notices: list[SkippedVideoNotice] = []
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

        aiocqhttp_reply_result = await self._collect_aiocqhttp_reply_video_attachments(
            event,
            seen_paths,
            settings.download,
        )
        attachments.extend(aiocqhttp_reply_result.attachments)
        skipped_notices.extend(aiocqhttp_reply_result.skipped_notices)
        self._debug("collected %s video attachment(s) from event fallback", len(attachments))
        return CollectedVideoAttachments(
            attachments=attachments,
            skipped_notices=skipped_notices,
        )

    async def _collect_video_attachments(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
        settings: PluginSettings,
    ) -> CollectedVideoAttachments:
        attachments = self._collect_video_attachment_markers(req)
        if attachments:
            self._debug("using request markers as the video attachment source")
            return CollectedVideoAttachments(attachments=attachments, skipped_notices=[])
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
    def _make_temp_image_part(path: Path) -> ImageURLPart:
        return ImageURLPart(
            image_url=ImageURLPart.ImageURL(url=str(path)),
        ).mark_as_temp()

    def _inject_frame_artifacts(
        self,
        req: ProviderRequest,
        frame_paths: list[Path],
        *,
        persist_to_history: bool,
    ) -> None:
        if persist_to_history:
            req.image_urls.extend(str(path) for path in frame_paths)
            return
        for path in frame_paths:
            req.extra_user_content_parts.append(self._make_temp_image_part(path))

    @staticmethod
    def _temp_file_prefix() -> str:
        return f"{PLUGIN_ID}_"

    @staticmethod
    def _plugin_temp_root() -> Path:
        return Path(tempfile.gettempdir()).resolve()

    def _cleanup_expired_temp_files(self, policy: CleanupPolicy) -> tuple[int, int, int]:
        if not policy.enabled:
            return (0, 0, 0)

        temp_root = self._plugin_temp_root()
        ttl_seconds = policy.ttl_hours * 3600
        now = time.time()
        candidates: list[tuple[float, Path, int]] = []
        scanned_count = 0

        for path in temp_root.glob(f"{self._temp_file_prefix()}*"):
            scanned_count += 1
            try:
                resolved_path = path.resolve()
                if resolved_path.parent != temp_root or not resolved_path.is_file():
                    continue
                stat_result = resolved_path.stat()
            except OSError as exc:
                self._debug("failed to inspect temp file during cleanup: %s (%s)", path, exc)
                continue

            if now - stat_result.st_mtime < ttl_seconds:
                continue
            candidates.append((stat_result.st_mtime, resolved_path, stat_result.st_size))

        candidates.sort(key=lambda item: item[0])
        deleted_count = 0
        deleted_bytes = 0

        for _, path, size in candidates[: policy.max_files_per_run]:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                self._debug("failed to cleanup expired temp file %s: %s", path, exc)
                continue
            deleted_count += 1
            deleted_bytes += size

        if deleted_count > 0:
            logger.info(
                "[%s] cleaned up %s expired temp file(s), freed %s (scanned=%s, ttl=%sh)",
                PLUGIN_ID,
                deleted_count,
                self._format_size(deleted_bytes),
                scanned_count,
                policy.ttl_hours,
            )
        else:
            self._debug(
                "cleanup scanned %s plugin temp file(s), no expired files found (ttl=%sh)",
                scanned_count,
                policy.ttl_hours,
            )

        return (deleted_count, deleted_bytes, scanned_count)

    async def _run_cleanup(
        self,
        policy: CleanupPolicy,
        *,
        reason: str,
        force: bool = False,
    ) -> None:
        if not policy.enabled:
            return

        now = time.monotonic()
        min_interval_seconds = policy.min_cleanup_interval_minutes * 60
        if not force and now - self._last_cleanup_at < min_interval_seconds:
            self._debug(
                "skip temp cleanup for reason=%s because interval guard is active (%s min)",
                reason,
                policy.min_cleanup_interval_minutes,
            )
            return

        self._last_cleanup_at = now
        self._debug("running temp cleanup for reason=%s", reason)
        try:
            await asyncio.to_thread(self._cleanup_expired_temp_files, policy)
        except Exception as exc:
            logger.warning("[%s] temp cleanup failed for reason=%s: %s", PLUGIN_ID, reason, exc)

    def _schedule_cleanup(self, policy: CleanupPolicy, *, reason: str) -> None:
        if not policy.enabled:
            return
        if self._cleanup_task is not None and not self._cleanup_task.done():
            self._debug("skip temp cleanup scheduling for reason=%s because previous cleanup is still running", reason)
            return

        self._cleanup_task = asyncio.create_task(
            self._run_cleanup(policy, reason=reason),
        )

    @staticmethod
    def _safe_parse_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_parse_positive_int(value: Any) -> int | None:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _format_size(byte_count: int | float) -> str:
        size = float(max(byte_count, 0))
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                if unit == "B":
                    return f"{int(size)} {unit}"
                return f"{size:.2f} {unit}"
            size /= 1024.0
        return f"{size:.2f} GB"

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

    @staticmethod
    def _build_contiguous_video_segments(
        total_duration: float,
        coverage_budget: float,
        segment_duration: float,
        segment_count: int,
    ) -> list[VideoSegment]:
        segments: list[VideoSegment] = []
        cursor = 0.0
        remaining_budget = coverage_budget
        for index in range(1, segment_count + 1):
            if cursor >= total_duration or remaining_budget <= 0:
                break
            actual_duration = min(
                segment_duration,
                total_duration - cursor,
                remaining_budget,
            )
            if actual_duration <= 0:
                break
            segments.append(
                VideoSegment(
                    index=index,
                    start_seconds=round(cursor, 3),
                    duration_seconds=round(actual_duration, 3),
                    label=f"\u7247\u6bb5 {index}",
                ),
            )
            cursor += actual_duration
            remaining_budget -= actual_duration
        return segments

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
        full_cover_segment_count = int(math.ceil(total_duration / max(segment_duration, 0.001)))
        if (
            coverage_budget >= total_duration
            and segment_count >= full_cover_segment_count
        ):
            return self._build_contiguous_video_segments(
                total_duration,
                coverage_budget,
                segment_duration,
                segment_count,
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

    @staticmethod
    def _distribute_items_by_weights(
        total_count: int,
        weights: list[float],
    ) -> list[int]:
        if total_count <= 0 or not weights:
            return []

        positive_indices = [
            index
            for index, weight in enumerate(weights)
            if weight > 0
        ]
        if not positive_indices:
            return VideoVisionHelper._distribute_items(total_count, len(weights))

        allocations = [0 for _ in weights]
        if total_count <= len(positive_indices):
            ranked_indices = sorted(
                positive_indices,
                key=lambda index: weights[index],
                reverse=True,
            )
            for index in ranked_indices[:total_count]:
                allocations[index] = 1
            return allocations

        for index in positive_indices:
            allocations[index] = 1

        remaining = total_count - len(positive_indices)
        total_weight = sum(weights[index] for index in positive_indices)
        raw_shares = [
            (
                index,
                remaining * weights[index] / total_weight,
            )
            for index in positive_indices
        ]
        for index, raw_share in raw_shares:
            allocations[index] += int(math.floor(raw_share))

        assigned = sum(allocations)
        leftover = total_count - assigned
        ranked_remainders = sorted(
            raw_shares,
            key=lambda item: item[1] - math.floor(item[1]),
            reverse=True,
        )
        for index, _raw_share in ranked_remainders[:leftover]:
            allocations[index] += 1
        return allocations

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
                    replace(policy, sampling_mode="uniform"),
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
        base_args = [
            ffmpeg_policy.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
        ]
        input_args = [
            "-i",
            str(video_path),
        ]
        if ffmpeg_policy.frame_seek_mode == "fast":
            # Fast seek is much cheaper for many independent frame extractions.
            input_args = [
                "-ss",
                f"{timestamp_seconds:.3f}",
                "-i",
                str(video_path),
            ]

        output_args = [
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
        if ffmpeg_policy.frame_seek_mode == "accurate":
            output_args = [
                "-ss",
                f"{timestamp_seconds:.3f}",
                *output_args,
            ]
        command = [*base_args, *input_args, *output_args]
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
            finalized = self._finalize_transcript(transcript, policy)
            self._log_transcript_preview(audio_path, finalized, policy)
            return finalized

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

        finalized = self._finalize_transcript(transcript, policy)
        self._log_transcript_preview(audio_path, finalized, policy)
        return finalized

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

    def _build_skipped_video_text(
        self,
        policy: HintPolicy,
        notice: SkippedVideoNotice,
    ) -> str:
        return self._format_template(
            policy.skipped_video_template,
            DEFAULT_SKIPPED_VIDEO_TEMPLATE,
            video_name=notice.video_name,
            reason=notice.reason,
            detail=notice.detail,
        )

    def _log_transcript_preview(
        self,
        audio_path: Path,
        transcript: str | None,
        policy: SttPolicy,
    ) -> None:
        if not transcript or not policy.log_transcript_preview:
            return
        preview = self._truncate_text(transcript, policy.transcript_preview_chars)
        self._debug(
            "stt transcript preview for %s: %s",
            audio_path,
            preview,
        )

    def _apply_frame_payload_budget(
        self,
        results: list[ProcessedVideo],
        policy: FramePolicy,
    ) -> tuple[list[ProcessedVideo], list[SkippedVideoNotice]]:
        if not results:
            return [], []

        max_images = max(1, policy.max_images_per_request)
        max_bytes = (
            int(policy.max_total_frame_bytes_mb * 1024 * 1024)
            if policy.max_total_frame_bytes_mb > 0
            else 0
        )
        selected_by_result: dict[int, list[Path]] = {index: [] for index in range(len(results))}
        selected_count = 0
        selected_bytes = 0
        dropped_count = 0
        dropped_bytes = 0

        for result_index, result in enumerate(results):
            for frame_path in result.frame_paths:
                try:
                    frame_size = frame_path.stat().st_size
                except OSError:
                    self._debug("dropping missing extracted frame before injection: %s", frame_path)
                    dropped_count += 1
                    continue

                if selected_count >= max_images:
                    dropped_count += 1
                    dropped_bytes += frame_size
                    continue

                if max_bytes > 0 and selected_bytes + frame_size > max_bytes:
                    dropped_count += 1
                    dropped_bytes += frame_size
                    continue

                selected_by_result[result_index].append(frame_path)
                selected_count += 1
                selected_bytes += frame_size

        if dropped_count > 0:
            budget_text = (
                f"{self._format_size(max_bytes)}"
                if max_bytes > 0
                else "不限制"
            )
            logger.warning(
                "[%s] dropped %s extracted frame(s) before injection because image budget was reached: selected=%s/%s, bytes=%s/%s, dropped_bytes=%s",
                PLUGIN_ID,
                dropped_count,
                selected_count,
                max_images,
                self._format_size(selected_bytes),
                budget_text,
                self._format_size(dropped_bytes),
            )

        budget_skipped_notices: list[SkippedVideoNotice] = []
        budgeted_results: list[ProcessedVideo] = []
        for result_index, result in enumerate(results):
            selected_frames = selected_by_result.get(result_index, [])
            if (
                result.frame_paths
                and not selected_frames
                and not result.audio_paths
                and not result.transcript_chunks
            ):
                budget_skipped_notices.append(
                    SkippedVideoNotice(
                        video_name=result.attachment.name,
                        reason="图片预算不足",
                        detail=(
                            f"该视频抽出的 {len(result.frame_paths)} 张关键帧未注入；"
                            f"当前单次请求最多注入 {max_images} 张图片，"
                            f"图片总体积预算为 {self._format_size(max_bytes) if max_bytes > 0 else '不限制'}。"
                        ),
                    ),
                )
                continue
            budgeted_results.append(replace(result, frame_paths=selected_frames))

        self._debug(
            "frame payload budget result: selected_frames=%s selected_bytes=%s dropped_frames=%s",
            selected_count,
            self._format_size(selected_bytes),
            dropped_count,
        )
        return budgeted_results, budget_skipped_notices

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
            is_long_video = (
                settings.segment.enabled
                and probe_info.duration_seconds > float(settings.segment.min_video_duration_seconds)
                and len(frame_segments) > 1
            )
            effective_frame_budget = (
                settings.frame.max_long_video_frames_per_video
                if is_long_video
                else settings.frame.max_frames_per_video
            )
            self._debug(
                "frame budget for %s: long_video=%s budget=%s short_budget=%s long_budget=%s segment_count=%s",
                attachment.path,
                is_long_video,
                effective_frame_budget,
                settings.frame.max_frames_per_video,
                settings.frame.max_long_video_frames_per_video,
                len(frame_segments),
            )
            frame_distribution = self._distribute_items_by_weights(
                effective_frame_budget,
                [segment.duration_seconds for segment in frame_segments],
            )
            self._debug(
                "frame distribution for %s: %s",
                attachment.path,
                [
                    {
                        "label": segment.label,
                        "duration": segment.duration_seconds,
                        "frames": frame_count,
                    }
                    for segment, frame_count in zip(frame_segments, frame_distribution)
                ],
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
            if settings.cleanup.cleanup_after_request:
                self._schedule_cleanup(settings.cleanup, reason="request_noop")
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

        collection = await self._collect_video_attachments(event, req, settings)
        attachments = collection.attachments
        skipped_notices = list(collection.skipped_notices)
        if not attachments:
            self._debug("no video attachments were collected for this request")
            if settings.hint.enabled and skipped_notices:
                self._apply_texts(
                    req,
                    settings.hint.target,
                    [
                        self._build_skipped_video_text(settings.hint, notice)
                        for notice in skipped_notices
                    ],
                )
            if settings.cleanup.cleanup_after_request:
                self._schedule_cleanup(settings.cleanup, reason="request_no_video")
            return

        if len(attachments) > settings.frame.max_videos_per_request:
            skipped_notices.extend(
                SkippedVideoNotice(
                    video_name=attachment.name,
                    reason="超过单次请求视频数量限制",
                    detail=(
                        f"当前配置单次最多处理 {settings.frame.max_videos_per_request} 个视频，"
                        "该视频未进入本次处理队列。"
                    ),
                )
                for attachment in attachments[settings.frame.max_videos_per_request :]
            )
        attachments = attachments[: settings.frame.max_videos_per_request]
        self._debug("processing %s video attachment(s) after request limit", len(attachments))
        processed_results: list[ProcessedVideo] = []

        for attachment in attachments:
            logger.info("[%s] processing video attachment: %s", PLUGIN_ID, attachment.path)
            result = await self._process_single_video(event, attachment, settings)
            if result is not None:
                processed_results.append(result)
            else:
                skipped_notices.append(
                    SkippedVideoNotice(
                        video_name=attachment.name,
                        reason="视频未产出可用的多模态内容",
                        detail="插件没有成功抽取关键帧、音频附件或转写文本；可能是格式不受 FFmpeg 支持、处理超时，或当前策略关闭了相关处理。",
                    ),
                )

        processed_results, budget_skipped_notices = self._apply_frame_payload_budget(
            processed_results,
            settings.frame,
        )
        skipped_notices.extend(budget_skipped_notices)

        if not processed_results and not skipped_notices:
            if settings.cleanup.cleanup_after_request:
                self._schedule_cleanup(settings.cleanup, reason="request_empty_result")
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
                self._inject_frame_artifacts(
                    req,
                    result.frame_paths,
                    persist_to_history=settings.frame.persist_sampled_frames_to_history,
                )
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
            hint_texts: list[str] = []
            if processed_results:
                hint_texts.append(
                    self._build_summary_text(
                        settings.hint,
                        video_count=len(processed_results),
                        frame_count=total_frame_count,
                        segment_count=total_segment_count,
                        coverage_seconds=total_coverage_seconds,
                        audio_count=attached_audio_count,
                        transcript_count=transcript_count,
                    ),
                )
            hint_texts.extend(video_texts)
            hint_texts.extend(transcript_texts)
            hint_texts.extend(
                self._build_skipped_video_text(settings.hint, notice)
                for notice in skipped_notices
            )
            self._apply_texts(
                req,
                settings.hint.target,
                hint_texts,
            )

        logger.info(
            "[%s] expanded %s video attachment(s): +%s frames, +%s audios, +%s transcripts across %s segment(s), skipped=%s",
            PLUGIN_ID,
            len(processed_results),
            total_frame_count,
            attached_audio_count,
            transcript_count,
            total_segment_count,
            len(skipped_notices),
        )
        if settings.cleanup.cleanup_after_request:
            self._schedule_cleanup(settings.cleanup, reason="request_done")
