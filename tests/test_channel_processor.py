"""channel_processor：抖音博主批量转录编排的单元测试（全 mock，无网络）。"""

import asyncio
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import channel_processor as cp_mod  # noqa: E402
from channel_processor import ChannelProcessor  # noqa: E402


def _raw_script(text="大家好，这是测试正文。", lang="zh"):
    return (
        "# Video Transcription\n"
        "\n"
        f"**Detected Language:** {lang}\n"
        "**Language Probability:** 0.98\n"
        "\n"
        "## Transcription Content\n"
        "\n"
        "**[00:00 - 00:05]**\n"
        "\n"
        f"{text}\n"
    )


NO_SPEECH_RAW = (
    "# Video Transcription\n"
    "\n"
    "**Detected Language:** zh\n"
    "**Language Probability:** 0.50\n"
    "\n"
    "## Transcription Content\n"
    "\n"
)


VIDEOS = [
    {"id": "7000000000000000001", "title": "第一个视频", "create_time": 1760000000},
    {"id": "7000000000000000002", "title": "第二个视频", "create_time": None},
]


class FakeVideoProcessor:
    def __init__(self, fail_ids=()):
        self.fail_ids = set(fail_ids)
        self.calls = []

    async def download_video_audio(
        self, url, output_dir, stem, cookie=None, cookie_domain=".douyin.com"
    ):
        video_id = url.rsplit("/", 1)[-1]
        self.calls.append({"url": url, "stem": stem, "cookie": cookie})
        if video_id in self.fail_ids:
            raise Exception("下载失败")
        path = Path(output_dir) / f"{stem}.m4a"
        path.write_bytes(b"\x00" * 16)
        return str(path)


class FakeTranscriber:
    def __init__(self, scripts):
        self._scripts = list(scripts)
        self.last_detected_language = None

    async def transcribe(self, audio_path):
        self.last_detected_language = "zh"
        return self._scripts.pop(0)


class FakeSummarizer:
    def __init__(self, summary_text="这是合集摘要。"):
        self.calls = []
        self.summary_text = summary_text

    async def summarize(self, transcript, target_language, video_title=None):
        self.calls.append({"transcript": transcript, "language": target_language})
        return f"# {video_title}\n\n{self.summary_text}"


class ChannelProcessorTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.work_root = Path(tempfile.mkdtemp())

    def _patch_fetch(self, nickname="测试博主", videos=None):
        videos = VIDEOS if videos is None else videos

        async def fake_fetch(url, cookie="", max_videos=None, on_first_page=None):
            return nickname, [dict(v) for v in videos]

        return mock.patch.object(cp_mod, "fetch_profile_videos", fake_fetch)

    def _patch_cookie(self, value=""):
        return mock.patch.object(cp_mod, "douyin_cookie", return_value=value)

    async def test_basic_collection(self):
        vp = FakeVideoProcessor()
        transcriber = FakeTranscriber([_raw_script("第一段"), _raw_script("第二段")])
        summarizer = FakeSummarizer()
        processor = ChannelProcessor(vp, transcriber)

        with self._patch_fetch(), self._patch_cookie():
            result = await processor.process_creator(
                url="https://www.douyin.com/user/MS4wLjABAAAAfake",
                summarizer=summarizer,
                summary_language="zh",
                work_dir=self.work_root,
            )

        self.assertEqual(result["nickname"], "测试博主")
        self.assertEqual(result["title"], "测试博主的视频合集")
        self.assertEqual(result["video_count"], 2)
        self.assertEqual(result["ok_count"], 2)
        self.assertEqual(result["no_speech_count"], 0)
        self.assertEqual(result["failed_count"], 0)
        self.assertFalse(result["no_speech"])
        self.assertEqual(result["detected_language"], "zh")

        # 分节标题：有 create_time 用时间，没有则用序号
        stamp1 = datetime.fromtimestamp(1760000000).strftime("%Y-%m-%d %H:%M")
        self.assertIn(f"## [{stamp1}] 第一个视频", result["script"])
        self.assertIn("## 2. 第二个视频", result["script"])
        self.assertIn("第一段", result["script"])
        self.assertIn("第二段", result["script"])
        # 元信息头被剥掉，只出现一次（顶层标题）
        self.assertEqual(result["script"].count("# Video Transcription"), 0)
        # source 行
        self.assertIn(
            "source: https://www.douyin.com/user/MS4wLjABAAAAfake", result["script"]
        )

        # 摘要恰好调用一次，输入是拼接后的合集正文
        self.assertEqual(len(summarizer.calls), 1)
        self.assertIn("第一个视频", summarizer.calls[0]["transcript"])
        self.assertIn("合集摘要", result["summary"])

    async def test_no_speech_video_is_marked(self):
        vp = FakeVideoProcessor()
        transcriber = FakeTranscriber([_raw_script("有语音"), NO_SPEECH_RAW])
        summarizer = FakeSummarizer()
        processor = ChannelProcessor(vp, transcriber)

        with self._patch_fetch(), self._patch_cookie():
            result = await processor.process_creator(
                url="https://www.douyin.com/user/MS4wLjABAAAAfake",
                summarizer=summarizer,
                summary_language="zh",
                work_dir=self.work_root,
            )

        self.assertEqual(result["ok_count"], 1)
        self.assertEqual(result["no_speech_count"], 1)
        self.assertFalse(result["no_speech"])
        self.assertIn("该视频未检测到语音", result["script"])
        # 只要有一个视频有语音，摘要照常生成
        self.assertEqual(len(summarizer.calls), 1)

    async def test_all_no_speech_skips_summary(self):
        vp = FakeVideoProcessor()
        transcriber = FakeTranscriber([NO_SPEECH_RAW, NO_SPEECH_RAW])
        summarizer = FakeSummarizer()
        processor = ChannelProcessor(vp, transcriber)

        with self._patch_fetch(), self._patch_cookie():
            result = await processor.process_creator(
                url="https://www.douyin.com/user/MS4wLjABAAAAfake",
                summarizer=summarizer,
                summary_language="zh",
                work_dir=self.work_root,
            )

        # 无语音守卫：绝不把占位文本喂给 LLM
        self.assertTrue(result["no_speech"])
        self.assertEqual(result["ok_count"], 0)
        self.assertEqual(summarizer.calls, [])
        self.assertEqual(result["summary"], "")

    async def test_failed_video_is_skipped(self):
        vp = FakeVideoProcessor(fail_ids={"7000000000000000002"})
        transcriber = FakeTranscriber([_raw_script("只有第一个能成功")])
        summarizer = FakeSummarizer()
        processor = ChannelProcessor(vp, transcriber)

        with self._patch_fetch(), self._patch_cookie():
            result = await processor.process_creator(
                url="https://www.douyin.com/user/MS4wLjABAAAAfake",
                summarizer=summarizer,
                summary_language="zh",
                work_dir=self.work_root,
            )

        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(result["failed"][0]["title"], "第二个视频")
        self.assertIn("下载失败", result["failed"][0]["error"])
        self.assertEqual(result["ok_count"], 1)
        # 成功的视频照常进合集
        self.assertIn("只有第一个能成功", result["script"])

    async def test_all_failed_raises(self):
        vp = FakeVideoProcessor(fail_ids={"7000000000000000001", "7000000000000000002"})
        transcriber = FakeTranscriber([])
        processor = ChannelProcessor(vp, transcriber)

        with self._patch_fetch(), self._patch_cookie():
            with self.assertRaisesRegex(Exception, "全部处理失败"):
                await processor.process_creator(
                    url="https://www.douyin.com/user/MS4wLjABAAAAfake",
                    summarizer=FakeSummarizer(),
                    summary_language="zh",
                    work_dir=self.work_root,
                )

    async def test_no_llm_skips_summary(self):
        vp = FakeVideoProcessor()
        transcriber = FakeTranscriber([_raw_script(), _raw_script()])
        summarizer = FakeSummarizer()
        processor = ChannelProcessor(vp, transcriber)

        with self._patch_fetch(), self._patch_cookie():
            result = await processor.process_creator(
                url="https://www.douyin.com/user/MS4wLjABAAAAfake",
                summarizer=summarizer,
                summary_language="zh",
                work_dir=self.work_root,
                no_llm=True,
            )

        self.assertEqual(result["summary"], "")
        self.assertEqual(summarizer.calls, [])
        self.assertEqual(result["ok_count"], 2)

    async def test_progress_callback_and_cleanup(self):
        vp = FakeVideoProcessor()
        transcriber = FakeTranscriber([_raw_script(), _raw_script()])
        processor = ChannelProcessor(vp, transcriber)

        events = []

        async def on_progress(progress, message):
            events.append((progress, message))

        with self._patch_fetch(), self._patch_cookie():
            result = await processor.process_creator(
                url="https://www.douyin.com/user/MS4wLjABAAAAfake",
                summarizer=FakeSummarizer(),
                summary_language="zh",
                work_dir=self.work_root,
                on_progress=on_progress,
            )

        progresses = [p for p, _ in events]
        self.assertEqual(progresses, sorted(progresses))
        self.assertIn(5, progresses)
        self.assertIn(88, progresses)
        self.assertIn(90, progresses)
        # 「第 x/n 个视频」消息
        self.assertTrue(any("第 1/2 个视频" in msg for _, msg in events))
        self.assertTrue(any("第 2/2 个视频" in msg for _, msg in events))

        # 音频临时文件与工作目录全部清理
        leftovers = list(self.work_root.rglob("audio_*"))
        self.assertEqual(leftovers, [])
        channel_dirs = [
            d for d in self.work_root.iterdir() if d.name.startswith("channel_")
        ]
        self.assertEqual(channel_dirs, [])
        # 结果字段完整
        self.assertEqual(result["video_count"], 2)

    async def test_cookie_passthrough_to_downloader(self):
        vp = FakeVideoProcessor()
        transcriber = FakeTranscriber([_raw_script(), _raw_script()])
        processor = ChannelProcessor(vp, transcriber)

        with self._patch_fetch(), self._patch_cookie("ttwid=abc"):
            await processor.process_creator(
                url="https://www.douyin.com/user/MS4wLjABAAAAfake",
                summarizer=FakeSummarizer(),
                summary_language="zh",
                work_dir=self.work_root,
            )

        # 每个视频的下载都带上了 DOUYIN_COOKIE
        self.assertEqual(len(vp.calls), 2)
        self.assertTrue(all(c["cookie"] == "ttwid=abc" for c in vp.calls))
        # 下载的是单视频页
        self.assertEqual(
            vp.calls[0]["url"], "https://www.douyin.com/video/7000000000000000001"
        )

    async def test_sync_progress_callback_supported(self):
        """CLI 传的是同步回调，也必须能上报。"""
        vp = FakeVideoProcessor()
        transcriber = FakeTranscriber([_raw_script(), _raw_script()])
        processor = ChannelProcessor(vp, transcriber)

        events = []

        def on_progress(progress, message):
            events.append((progress, message))

        with self._patch_fetch(), self._patch_cookie():
            await processor.process_creator(
                url="https://www.douyin.com/user/MS4wLjABAAAAfake",
                summarizer=FakeSummarizer(),
                summary_language="zh",
                work_dir=self.work_root,
                on_progress=on_progress,
            )

        self.assertTrue(events)


if __name__ == "__main__":
    unittest.main()
