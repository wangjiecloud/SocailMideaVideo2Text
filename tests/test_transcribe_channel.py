"""transcribe.py 抖音博主合集 CLI 的完成清理行为（增量缓存语义，全 mock 无网络）。

Run: venv/bin/python -m unittest discover tests
"""

import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "backend"))

import progress_store  # noqa: E402
import transcribe  # noqa: E402

CHANNEL_SEC_UID = "MS4wLjABAAAAcliTestSecUid000"
CHANNEL_URL = f"https://www.douyin.com/user/{CHANNEL_SEC_UID}?from_tab_name=main"

_COMPLETED_RESULT = {
    "nickname": "CLI测试博主",
    "title": "CLI测试博主的视频合集",
    "paused": False,
    "script": "# CLI测试博主的视频合集\n\n## 第 1 个视频\n\n正文",
    "summary": "",
    "video_count": 1,
    "done_count": 1,
    "ok_count": 1,
    "no_speech_count": 0,
    "image_count": 0,
    "failed_count": 0,
    "failed": [],
    "detected_language": "zh",
    "no_speech": False,
}


class ChannelCliCompletionTests(unittest.TestCase):
    """CLI 合集完成后保留进度文件；下次重跑经 resume_sections 增量复用。"""

    def setUp(self):
        self.out_dir = Path(tempfile.mkdtemp(prefix="transcribe-cli-test-"))

    def tearDown(self):
        shutil.rmtree(self.out_dir, ignore_errors=True)

    @staticmethod
    def _args():
        return SimpleNamespace(source=CHANNEL_URL, summary_language="zh", no_llm=True)

    def _run(self):
        with mock.patch.object(transcribe, "ChannelProcessor") as processor_cls:
            processor_cls.return_value.process_creator = mock.AsyncMock(
                return_value=dict(_COMPLETED_RESULT)
            )
            result = asyncio.run(
                transcribe._run_channel(
                    self._args(),
                    mock.Mock(name="vp"),
                    mock.Mock(name="transcriber"),
                    mock.Mock(name="summarizer"),
                    self.out_dir,
                    quiet=True,
                )
            )
        return result, processor_cls.return_value.process_creator

    def test_completed_run_keeps_progress_and_resumes_next_time(self):
        """完成后进度文件保留；重跑同一命令时旧小节经 resume_sections 复用。"""
        progress_store.save_progress(
            self.out_dir,
            CHANNEL_SEC_UID,
            {
                "sec_uid": CHANNEL_SEC_UID,
                "total": 1,
                "videos": {
                    "7000000000000000001": {
                        "status": "ok",
                        "section": "## 旧视频\n\n旧正文",
                    }
                },
            },
        )
        result, process_creator = self._run()
        self.assertTrue(result["channel"])
        self.assertTrue(
            progress_store.progress_path(self.out_dir, CHANNEL_SEC_UID).is_file(),
            "CLI 合集完成后必须保留进度文件供增量重跑",
        )
        resume = process_creator.call_args.kwargs["resume_sections"]
        self.assertIn("7000000000000000001", resume)

    def test_run_without_progress_file_starts_from_scratch(self):
        """没有进度文件时 resume_sections 为空——删除进度文件即全量重跑。"""
        result, process_creator = self._run()
        self.assertEqual(process_creator.call_args.kwargs["resume_sections"], {})
        self.assertTrue(result["channel"])
