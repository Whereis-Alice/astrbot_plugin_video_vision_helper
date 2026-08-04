from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from astrbot.core.message.components import File

from main import (
    DEFAULT_GROUP_FILE_VIDEO_EXTENSIONS,
    DownloadedVideo,
    DownloadPolicy,
    VideoVisionHelper,
)


class FakeBot:
    def __init__(self, *, fail_with_busid: bool = False) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail_with_busid = fail_with_busid

    async def call_action(self, action: str, **params):
        self.calls.append((action, params))
        if self.fail_with_busid and "busid" in params:
            raise TypeError("busid is not supported")
        return {
            "data": {
                "url": "https://files.example.test/download?token=secret",
                "file_name": "group-video.mp4",
                "file_size": 1024,
            },
        }


class FakeEvent:
    def __init__(self, raw_message=None, *, bot=None, messages=None, message_str="") -> None:
        self.bot = bot or FakeBot()
        self.message_str = message_str
        self.message_obj = SimpleNamespace(
            raw_message=raw_message or {},
            message=list(messages or []),
            message_str=message_str,
        )
        self.tracked_files: list[str] = []

    def get_group_id(self) -> str:
        return "123456"

    def track_temporary_local_file(self, path: str) -> None:
        self.tracked_files.append(path)

    def get_messages(self):
        return self.message_obj.message


def make_plugin() -> VideoVisionHelper:
    plugin = object.__new__(VideoVisionHelper)
    plugin.config = {}
    return plugin


def make_policy(**overrides) -> DownloadPolicy:
    values = {
        "quoted_video_download_enabled": True,
        "group_file_video_enabled": True,
        "group_file_video_extensions": DEFAULT_GROUP_FILE_VIDEO_EXTENSIONS,
        "group_file_empty_prompt_fallback_enabled": True,
        "group_file_empty_prompt_fallback_text": "Please analyze this video file.",
        "max_download_size_mb": 128.0,
        "timeout_seconds": 120,
    }
    values.update(overrides)
    return DownloadPolicy(**values)


class GroupFileVideoTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolves_local_astrbot_file_component(self) -> None:
        plugin = make_plugin()
        plugin._download_video_from_url = AsyncMock()
        with tempfile.NamedTemporaryFile(suffix=".MP4", delete=False) as file_obj:
            path = Path(file_obj.name)
        self.addCleanup(path.unlink, missing_ok=True)

        result = await plugin._resolve_file_video_component(
            FakeEvent(),
            File(
                name="clip.MP4",
                file=str(path),
                url="https://files.example.test/clip.MP4?token=secret",
            ),
            quoted=False,
            download_policy=make_policy(),
        )

        self.assertIsNotNone(result.attachment)
        self.assertEqual(result.attachment.path, path.resolve())
        self.assertEqual(result.attachment.name, "clip.MP4")
        plugin._download_video_from_url.assert_not_awaited()

    async def test_ignores_non_video_file_component(self) -> None:
        plugin = make_plugin()
        plugin._download_video_from_url = AsyncMock()

        result = await plugin._resolve_file_video_component(
            FakeEvent(),
            File(name="notes.txt", url="https://files.example.test/notes.txt"),
            quoted=False,
            download_policy=make_policy(),
        )

        self.assertIsNone(result.attachment)
        self.assertIsNone(result.skipped_notice)
        plugin._download_video_from_url.assert_not_awaited()

    async def test_raw_onebot_group_file_uses_file_id_and_busid(self) -> None:
        plugin = make_plugin()
        downloaded_path = Path(tempfile.gettempdir()) / "resolved-group-video.mp4"
        plugin._download_video_from_url = AsyncMock(
            return_value=DownloadedVideo(path=downloaded_path),
        )
        event = FakeEvent()

        result = await plugin._resolve_onebot_file_video_segment(
            event,
            {
                "file": "group-video.mp4",
                "file_id": "file-123",
                "busid": 102,
                "size": 1024,
            },
            quoted=False,
            download_policy=make_policy(),
        )

        self.assertIsNotNone(result.attachment)
        self.assertEqual(result.attachment.name, "group-video.mp4")
        self.assertEqual(event.bot.calls[0][0], "get_group_file_url")
        self.assertEqual(event.bot.calls[0][1]["group_id"], 123456)
        self.assertEqual(event.bot.calls[0][1]["file_id"], "file-123")
        self.assertEqual(event.bot.calls[0][1]["busid"], 102)
        plugin._download_video_from_url.assert_awaited_once()

    async def test_onebot_group_file_retries_without_busid(self) -> None:
        plugin = make_plugin()
        downloaded_path = Path(tempfile.gettempdir()) / "resolved-group-video.mp4"
        plugin._download_video_from_url = AsyncMock(
            return_value=DownloadedVideo(path=downloaded_path),
        )
        event = FakeEvent(bot=FakeBot(fail_with_busid=True))

        result = await plugin._resolve_onebot_file_video_segment(
            event,
            {
                "file": "group-video.mp4",
                "file_id": "file-456",
                "busid": 7,
            },
            quoted=True,
            download_policy=make_policy(),
        )

        self.assertIsNotNone(result.attachment)
        self.assertEqual(len(event.bot.calls), 2)
        self.assertIn("busid", event.bot.calls[0][1])
        self.assertNotIn("busid", event.bot.calls[1][1])

    async def test_collects_raw_direct_group_file_when_file_component_is_missing(self) -> None:
        plugin = make_plugin()
        downloaded_path = Path(tempfile.gettempdir()) / "resolved-direct-group-video.mp4"
        plugin._download_video_from_url = AsyncMock(
            return_value=DownloadedVideo(path=downloaded_path),
        )
        event = FakeEvent(
            {
                "group_id": 123456,
                "message": [
                    {
                        "type": "file",
                        "data": {
                            "file": "raw-video.mkv",
                            "file_id": "raw-file-id",
                            "busid": 9,
                        },
                    },
                ],
            },
        )

        result = await plugin._collect_aiocqhttp_direct_file_video_attachments(
            event,
            set(),
            set(),
            make_policy(),
        )

        self.assertEqual(len(result.attachments), 1)
        self.assertEqual(result.attachments[0].name, "group-video.mp4")
        self.assertFalse(result.skipped_notices)

    def test_extension_config_accepts_common_separators(self) -> None:
        plugin = make_plugin()
        self.assertEqual(
            plugin._read_video_extensions("mp4; .MKV | webm"),
            (".mp4", ".mkv", ".webm"),
        )

    def test_collects_core_file_attachment_marker(self) -> None:
        plugin = make_plugin()
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as file_obj:
            video_path = Path(file_obj.name)
        self.addCleanup(video_path.unlink, missing_ok=True)
        req = SimpleNamespace(
            extra_user_content_parts=[
                {
                    "type": "text",
                    "text": (
                        "[File Attachment: name core-video.mp4, "
                        f"path {video_path}]"
                    ),
                },
                {
                    "type": "text",
                    "text": "[File Attachment: name document.txt, path /tmp/document.txt]",
                },
            ],
        )

        attachments = plugin._collect_video_attachment_markers(req, make_policy())

        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].name, "core-video.mp4")
        self.assertEqual(attachments[0].path, video_path.resolve())

    def test_ignores_empty_core_file_attachment_marker(self) -> None:
        plugin = make_plugin()
        req = SimpleNamespace(
            extra_user_content_parts=[
                {
                    "type": "text",
                    "text": "[File Attachment: name core-video.mp4, path ]",
                },
            ],
        )

        attachments = plugin._collect_video_attachment_markers(req, make_policy())

        self.assertEqual(attachments, [])

    async def test_empty_core_marker_falls_back_to_raw_onebot_file(self) -> None:
        plugin = make_plugin()
        downloaded_path = Path(tempfile.gettempdir()) / "resolved-empty-marker-video.mp4"
        plugin._download_video_from_url = AsyncMock(
            return_value=DownloadedVideo(path=downloaded_path),
        )
        event = FakeEvent(
            {
                "group_id": 123456,
                "message": [
                    {
                        "type": "file",
                        "data": {
                            "file": "core-video.mp4",
                            "file_id": "raw-after-empty-marker",
                            "busid": 9,
                        },
                    },
                ],
            },
        )
        req = SimpleNamespace(
            extra_user_content_parts=[
                {
                    "type": "text",
                    "text": "[File Attachment: name core-video.mp4, path ]",
                },
            ],
        )

        result = await plugin._collect_video_attachments(
            event,
            req,
            plugin._load_settings(),
        )

        self.assertEqual(len(result.attachments), 1)
        self.assertEqual(result.attachments[0].path, downloaded_path)

    async def test_adds_fallback_prompt_before_core_builds_file_only_request(self) -> None:
        plugin = make_plugin()
        event = FakeEvent(
            messages=[
                File(
                    name="group-video.mp4",
                    url="https://files.example.test/group-video.mp4",
                ),
            ],
        )

        await plugin.on_waiting_llm_request(event)

        self.assertEqual(event.message_str, "请分析这个视频文件的内容。")
        self.assertEqual(event.message_obj.message_str, event.message_str)

    async def test_keeps_existing_prompt_for_file_video(self) -> None:
        plugin = make_plugin()
        event = FakeEvent(
            messages=[File(name="group-video.mp4", url="https://files.example.test/video")],
            message_str="请看结尾发生了什么",
        )

        await plugin.on_waiting_llm_request(event)

        self.assertEqual(event.message_str, "请看结尾发生了什么")


if __name__ == "__main__":
    unittest.main()
