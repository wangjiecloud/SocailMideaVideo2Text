"""channel_processor：抖音博主批量转录编排的单元测试（全 mock，无网络）。"""

import asyncio
import contextlib
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import channel_processor as cp_mod  # noqa: E402
from channel_processor import ChannelProcessor  # noqa: E402
from video_processor import VideoProcessor as RealVideoProcessor  # noqa: E402


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


def _default_share_info(video_id):
    return {
        "id": str(video_id),
        "title": "",
        "create_time": None,
        "duration": 60,
        "play_urls": [f"https://aweme.snssdk.com/playwm/{video_id}"],
        "is_image": False,
    }


_SAMPLE_VTT = (
    "WEBVTT\n"
    "\n"
    "00:01.000 --> 00:03.000\n"
    "字幕第一句\n"
    "\n"
    "00:04.000 --> 00:06.000\n"
    "字幕第二句\n"
)


class FakeVideoProcessor:
    """提供新链路的 normalize_local_media_to_m4a 与 yt-dlp 兜底下载。"""

    def __init__(self, fail_audio_ids=()):
        self.fail_audio_ids = set(fail_audio_ids)
        self.normalized = []
        self.audio_downloads = []

    async def normalize_local_media_to_m4a(self, input_path, output_dir, out_name=None):
        input_path = Path(input_path)
        self.normalized.append(str(input_path))
        if not input_path.exists():
            raise Exception("媒体文件不存在")
        out = Path(output_dir) / f"{out_name or 'norm'}.m4a"
        out.write_bytes(b"\x00" * 16)
        return str(out)

    async def download_video_audio(
        self, url, output_dir, stem, cookie=None, cookie_domain=".douyin.com"
    ):
        video_id = url.rsplit("/", 1)[-1]
        self.audio_downloads.append({"url": url, "stem": stem, "cookie": cookie})
        if video_id in self.fail_audio_ids:
            raise Exception("下载失败")
        path = Path(output_dir) / f"{stem}.m4a"
        path.write_bytes(b"\x00" * 16)
        return str(path)

    def _parse_vtt(self, filepath):
        # 复用真实解析器，让字幕路径测的是真实现
        return RealVideoProcessor()._parse_vtt(filepath)


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


class _Recorder:
    def __init__(self):
        self.share_calls = []
        self.download_calls = []
        self.text_calls = []


class ChannelProcessorTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.work_root = Path(tempfile.mkdtemp())
        self.media = _Recorder()

        # 硬字幕 OCR 默认 mock 为未命中：现有测试不真跑 ffmpeg/OCR 引擎；
        # OCR 专项用例在自己的 with 里再叠一层 patch 覆盖返回值
        async def _no_ocr(video_path, work_dir):
            return ""

        self._ocr_patcher = mock.patch.object(
            cp_mod.subtitle_ocr, "extract_hard_subtitles", _no_ocr
        )
        self._ocr_patcher.start()
        self.addCleanup(self._ocr_patcher.stop)

    def _patch_fetch(self, nickname="测试博主", videos=None):
        videos = VIDEOS if videos is None else videos

        async def fake_fetch(url, cookie="", max_videos=None, on_first_page=None):
            return nickname, [dict(v) for v in videos]

        return mock.patch.object(cp_mod, "fetch_profile_videos", fake_fetch)

    def _patch_cookie(self, value=""):
        return mock.patch.object(cp_mod, "douyin_cookie", return_value=value)

    def _patch_media(
        self,
        share=None,
        fail_share_ids=(),
        fail_download_urls=(),
        caption_text=None,
    ):
        """patch 掉 channel_processor 引用的 douyin 网络函数；调用记录在 self.media。"""
        recorder = self.media

        async def fake_share(video_id, cookie=""):
            recorder.share_calls.append({"id": str(video_id), "cookie": cookie})
            if str(video_id) in set(fail_share_ids):
                raise Exception("分享页失败")
            if share is not None:
                return dict(share(str(video_id)))
            return _default_share_info(video_id)

        async def fake_download(url, dest_path, cookie=""):
            recorder.download_calls.append({"url": url, "cookie": cookie})
            if url in set(fail_download_urls):
                raise Exception("直链下载失败")
            dest = Path(dest_path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"\x00" * 32)
            return dest

        async def fake_text(url, cookie=""):
            recorder.text_calls.append({"url": url, "cookie": cookie})
            if caption_text is None:
                raise Exception("字幕下载失败")
            return caption_text

        stack = contextlib.ExitStack()
        stack.enter_context(
            mock.patch.object(cp_mod, "fetch_share_video_info", fake_share)
        )
        stack.enter_context(mock.patch.object(cp_mod, "download_media", fake_download))
        stack.enter_context(mock.patch.object(cp_mod, "fetch_text", fake_text))
        return stack

    async def test_basic_collection(self):
        vp = FakeVideoProcessor()
        transcriber = FakeTranscriber([_raw_script("第一段"), _raw_script("第二段")])
        summarizer = FakeSummarizer()
        processor = ChannelProcessor(vp, transcriber)

        with (
            self._patch_fetch(),
            self._patch_cookie(),
            self._patch_media(),
        ):
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
        self.assertEqual(result["image_count"], 0)
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

        # 默认链路：每个视频都走了分享页兜底 + playwm 下载
        self.assertEqual(len(self.media.share_calls), 2)
        self.assertEqual(len(self.media.download_calls), 2)
        self.assertEqual(
            self.media.download_calls[0]["url"],
            "https://aweme.snssdk.com/playwm/7000000000000000001",
        )

    async def test_no_speech_video_is_marked(self):
        vp = FakeVideoProcessor()
        transcriber = FakeTranscriber([_raw_script("有语音"), NO_SPEECH_RAW])
        summarizer = FakeSummarizer()
        processor = ChannelProcessor(vp, transcriber)

        with (
            self._patch_fetch(),
            self._patch_cookie(),
            self._patch_media(),
        ):
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

        with (
            self._patch_fetch(),
            self._patch_cookie(),
            self._patch_media(),
        ):
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
        vp = FakeVideoProcessor(fail_audio_ids={"7000000000000000002"})
        transcriber = FakeTranscriber([_raw_script("只有第一个能成功")])
        summarizer = FakeSummarizer()
        processor = ChannelProcessor(vp, transcriber)
        processor.VIDEO_ATTEMPTS = 1  # 失败重试在单独的用例里验证

        with (
            self._patch_fetch(),
            self._patch_cookie(),
            self._patch_media(fail_share_ids={"7000000000000000002"}),
        ):
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
        both = {"7000000000000000001", "7000000000000000002"}
        vp = FakeVideoProcessor(fail_audio_ids=both)
        transcriber = FakeTranscriber([])
        processor = ChannelProcessor(vp, transcriber)
        processor.VIDEO_ATTEMPTS = 1

        with (
            self._patch_fetch(),
            self._patch_cookie(),
            self._patch_media(fail_share_ids=both),
        ):
            with self.assertRaisesRegex(Exception, "全部处理失败"):
                await processor.process_creator(
                    url="https://www.douyin.com/user/MS4wLjABAAAAfake",
                    summarizer=FakeSummarizer(),
                    summary_language="zh",
                    work_dir=self.work_root,
                )

    async def test_transcribe_one_retries(self):
        """处理链整体失败时按 VIDEO_ATTEMPTS 重试；成功后清理临时文件。"""
        processor = ChannelProcessor(FakeVideoProcessor(), FakeTranscriber([]))
        calls = {"n": 0}

        async def flaky_once(video, work_dir, stem, cookie):
            calls["n"] += 1
            if calls["n"] == 1:
                raise Exception("偶发失败")
            return "重试后的脚本", "zh", "direct"

        processor._process_video_once = flaky_once
        work_dir = self.work_root / "retry_dir"
        work_dir.mkdir()

        script, lang, source = await processor._transcribe_one(
            {"id": "1", "title": "标题"}, 1, work_dir, ""
        )
        self.assertEqual(calls["n"], 2)
        self.assertEqual((script, lang, source), ("重试后的脚本", "zh", "direct"))

    async def test_transcribe_one_raises_after_attempts(self):
        processor = ChannelProcessor(FakeVideoProcessor(), FakeTranscriber([]))
        processor.VIDEO_ATTEMPTS = 1

        async def always_fail(video, work_dir, stem, cookie):
            raise Exception("永久失败")

        processor._process_video_once = always_fail
        work_dir = self.work_root / "fail_dir"
        work_dir.mkdir()

        with self.assertRaisesRegex(Exception, "下载/转录失败: 永久失败"):
            await processor._transcribe_one({"id": "1", "title": "t"}, 1, work_dir, "")

    async def test_on_log_reports_per_video_results(self):
        """on_log 回调逐视频推送来源与结果，供页面实时展示。"""
        vp = FakeVideoProcessor()
        transcriber = FakeTranscriber([_raw_script("第一段"), _raw_script("第二段")])
        processor = ChannelProcessor(vp, transcriber)
        logs = []

        async def on_log(line):
            logs.append(line)

        with (
            self._patch_fetch(),
            self._patch_cookie(),
            self._patch_media(),
        ):
            await processor.process_creator(
                url="https://www.douyin.com/user/MS4wLjABAAAAfake",
                summarizer=FakeSummarizer(),
                summary_language="zh",
                work_dir=self.work_root,
                on_log=on_log,
            )

        self.assertTrue(any("共 2 个视频" in line for line in logs))
        self.assertEqual(sum("转录成功" in line for line in logs), 2)
        # 默认链路经分享页直链下载，日志里标注来源
        self.assertEqual(sum("分享页直链+Whisper" in line for line in logs), 2)
        # 每条成功日志都带 [i/2] 进度前缀
        import re as _re

        self.assertTrue(
            all(_re.search(r"\[\d+/2\]", line) for line in logs if "转录成功" in line)
        )

    async def test_ocr_hit_skips_whisper(self):
        """画面烧录字幕命中 OCR：直接用 OCR 文本，跳过音频规范化与 Whisper。"""
        vp = FakeVideoProcessor()
        # Whisper 若被调用会 IndexError（脚本队列已空），测试即失败
        transcriber = FakeTranscriber([])
        processor = ChannelProcessor(vp, transcriber)
        logs = []

        async def on_log(line):
            logs.append(line)

        videos = [
            {
                "id": "7000000000000000001",
                "title": "OCR视频",
                "create_time": 1760000000,
                "low_gear_url": "https://cdn.example.com/low1.mp4",
            },
        ]

        async def fake_ocr(video_path, work_dir):
            return "OCR 提取的字幕正文"

        with (
            self._patch_fetch(videos=videos),
            self._patch_cookie(),
            self._patch_media(),
            mock.patch.object(cp_mod.subtitle_ocr, "extract_hard_subtitles", fake_ocr),
        ):
            result = await processor.process_creator(
                url="https://www.douyin.com/user/MS4wLjABAAAAfake",
                summarizer=FakeSummarizer(),
                summary_language="zh",
                work_dir=self.work_root,
                on_log=on_log,
            )

        self.assertEqual(result["ok_count"], 1)
        self.assertEqual(result["detected_language"], "")  # OCR 路径无语言检测
        self.assertIn("OCR 提取的字幕正文", result["script"])
        # OCR 命中时既不抽音频也不跑 Whisper
        self.assertEqual(vp.normalized, [])
        self.assertTrue(any("低码率直链+硬字幕OCR" in line for line in logs))

    async def test_ocr_miss_falls_back_to_whisper(self):
        """OCR 未命中（无烧录字幕）：回落音频规范化 + Whisper，标签如实标注。"""
        vp = FakeVideoProcessor()
        transcriber = FakeTranscriber([_raw_script("Whisper 正文")])
        processor = ChannelProcessor(vp, transcriber)
        logs = []

        async def on_log(line):
            logs.append(line)

        videos = [
            {
                "id": "7000000000000000001",
                "title": "无字幕视频",
                "create_time": 1760000000,
                "low_gear_url": "https://cdn.example.com/low1.mp4",
            },
        ]

        async def fake_ocr(video_path, work_dir):
            return ""

        with (
            self._patch_fetch(videos=videos),
            self._patch_cookie(),
            self._patch_media(),
            mock.patch.object(cp_mod.subtitle_ocr, "extract_hard_subtitles", fake_ocr),
        ):
            result = await processor.process_creator(
                url="https://www.douyin.com/user/MS4wLjABAAAAfake",
                summarizer=FakeSummarizer(),
                summary_language="zh",
                work_dir=self.work_root,
                on_log=on_log,
            )

        self.assertEqual(result["ok_count"], 1)
        self.assertEqual(result["detected_language"], "zh")
        self.assertIn("Whisper 正文", result["script"])
        self.assertEqual(len(vp.normalized), 1)  # 音频规范化确实执行
        self.assertTrue(any("低码率直链+Whisper" in line for line in logs))
        self.assertFalse(any("硬字幕OCR" in line for line in logs))

    async def test_section_body_merged_into_paragraph(self):
        """同一视频（同一日期小节）的正文合并为整段，不再按时间戳切分。"""
        vp = FakeVideoProcessor()
        raw = (
            "# Video Transcription\n"
            "\n"
            "**Detected Language:** zh\n"
            "**Language Probability:** 0.98\n"
            "\n"
            "## Transcription Content\n"
            "\n"
            "**[00:00 - 00:02]**\n"
            "\n"
            "第一句。\n"
            "\n"
            "**[00:02 - 00:04]**\n"
            "\n"
            "第二句。\n"
        )
        transcriber = FakeTranscriber([raw])
        processor = ChannelProcessor(vp, transcriber)

        with (
            self._patch_fetch(videos=[VIDEOS[0]]),
            self._patch_cookie(),
            self._patch_media(),
        ):
            result = await processor.process_creator(
                url="https://www.douyin.com/user/MS4wLjABAAAAfake",
                summarizer=FakeSummarizer(),
                summary_language="zh",
                work_dir=self.work_root,
            )

        self.assertNotIn("**[", result["script"])
        self.assertIn("第一句。第二句。", result["script"])

    async def test_no_llm_skips_summary(self):
        vp = FakeVideoProcessor()
        transcriber = FakeTranscriber([_raw_script(), _raw_script()])
        summarizer = FakeSummarizer()
        processor = ChannelProcessor(vp, transcriber)

        with (
            self._patch_fetch(),
            self._patch_cookie(),
            self._patch_media(),
        ):
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

        with (
            self._patch_fetch(),
            self._patch_cookie(),
            self._patch_media(),
        ):
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

        # 音频临时文件与工作目录全部清理（含低码率 mp4 与 norm_* 产物）
        self.assertEqual(list(self.work_root.rglob("audio_*")), [])
        self.assertEqual(list(self.work_root.rglob("norm_*")), [])
        channel_dirs = [
            d for d in self.work_root.iterdir() if d.name.startswith("channel_")
        ]
        self.assertEqual(channel_dirs, [])
        # 结果字段完整
        self.assertEqual(result["video_count"], 2)

    async def test_cookie_passthrough_to_media_chain(self):
        vp = FakeVideoProcessor()
        transcriber = FakeTranscriber([_raw_script(), _raw_script()])
        processor = ChannelProcessor(vp, transcriber)

        with (
            self._patch_fetch(),
            self._patch_cookie("ttwid=abc"),
            self._patch_media(),
        ):
            await processor.process_creator(
                url="https://www.douyin.com/user/MS4wLjABAAAAfake",
                summarizer=FakeSummarizer(),
                summary_language="zh",
                work_dir=self.work_root,
            )

        # 分享页解析与媒体下载都带上了 DOUYIN_COOKIE
        self.assertEqual(len(self.media.share_calls), 2)
        self.assertTrue(all(c["cookie"] == "ttwid=abc" for c in self.media.share_calls))
        self.assertEqual(len(self.media.download_calls), 2)
        self.assertTrue(
            all(c["cookie"] == "ttwid=abc" for c in self.media.download_calls)
        )

    async def test_sync_progress_callback_supported(self):
        """CLI 传的是同步回调，也必须能上报。"""
        vp = FakeVideoProcessor()
        transcriber = FakeTranscriber([_raw_script(), _raw_script()])
        processor = ChannelProcessor(vp, transcriber)

        events = []

        def on_progress(progress, message):
            events.append((progress, message))

        with (
            self._patch_fetch(),
            self._patch_cookie(),
            self._patch_media(),
        ):
            await processor.process_creator(
                url="https://www.douyin.com/user/MS4wLjABAAAAfake",
                summarizer=FakeSummarizer(),
                summary_language="zh",
                work_dir=self.work_root,
                on_progress=on_progress,
            )

        self.assertTrue(events)

    # ------------------------------------------------------------------
    # 单视频处理链：字幕优先 → 低码率直链 → 分享页兜底 → yt-dlp
    # ------------------------------------------------------------------

    async def test_caption_first_avoids_download(self):
        """列表暴露了 cla_info 字幕时直接取文本，不下载音频、不跑 Whisper。"""
        videos = [
            {
                "id": "7000000000000000001",
                "title": "有字幕的视频",
                "create_time": 1760000000,
                "caption_url": "https://cdn.example/vtt?sign=1",
                "caption_format": "webvtt",
                "caption_lang": "zh",
            },
            dict(VIDEOS[1]),
        ]
        vp = FakeVideoProcessor()
        transcriber = FakeTranscriber([_raw_script("无字幕视频走转录")])
        processor = ChannelProcessor(vp, transcriber)

        with (
            self._patch_fetch(videos=videos),
            self._patch_cookie(),
            self._patch_media(caption_text=_SAMPLE_VTT),
        ):
            result = await processor.process_creator(
                url="https://www.douyin.com/user/MS4wLjABAAAAfake",
                summarizer=FakeSummarizer(),
                summary_language="zh",
                work_dir=self.work_root,
            )

        # 字幕视频：只拉了字幕文本，没下载媒体、没转录
        self.assertEqual(len(self.media.text_calls), 1)
        self.assertEqual(
            self.media.text_calls[0]["url"], "https://cdn.example/vtt?sign=1"
        )
        self.assertEqual(len(self.media.download_calls), 1)  # 只有第二个视频下载
        self.assertEqual(len(vp.normalized), 1)
        self.assertIn("字幕第一句", result["script"])
        self.assertIn("字幕第二句", result["script"])
        self.assertIn("无字幕视频走转录", result["script"])
        # 字幕语言计入语言统计
        self.assertEqual(result["detected_language"], "zh")

    async def test_caption_json_format(self):
        """utterances JSON 字幕也能解析。"""
        videos = [
            {
                "id": "7000000000000000001",
                "title": "JSON 字幕视频",
                "create_time": 1760000000,
                "caption_url": "https://cdn.example/caption.json",
                "caption_format": "json",
                "caption_lang": "zh",
            }
        ]
        caption_json = json.dumps(
            {
                "utterances": [
                    {"text": "JSON 字幕一", "start_time": "0.5", "end_time": "2.5"},
                    {"text": "JSON 字幕二", "start_time": "3", "end_time": "5"},
                ]
            },
            ensure_ascii=False,
        )
        transcriber = FakeTranscriber([])
        processor = ChannelProcessor(FakeVideoProcessor(), transcriber)

        with (
            self._patch_fetch(videos=videos),
            self._patch_cookie(),
            self._patch_media(caption_text=caption_json),
        ):
            result = await processor.process_creator(
                url="https://www.douyin.com/user/MS4wLjABAAAAfake",
                summarizer=FakeSummarizer(),
                summary_language="zh",
                work_dir=self.work_root,
            )

        self.assertIn("JSON 字幕一", result["script"])
        # 同一视频的字幕条目合并为整段，不再保留时间戳
        self.assertIn("JSON 字幕一JSON 字幕二", result["script"])
        self.assertNotIn("**[", result["script"])
        self.assertEqual(self.media.download_calls, [])

    async def test_caption_failure_falls_back_to_share(self):
        """字幕下载失败不拖垮视频：自然落回分享页下载链路。"""
        videos = [
            {
                "id": "7000000000000000001",
                "title": "字幕坏了的视频",
                "create_time": 1760000000,
                "caption_url": "https://cdn.example/vtt?expired=1",
                "caption_format": "webvtt",
                "caption_lang": "zh",
            }
        ]
        transcriber = FakeTranscriber([_raw_script("兜底转录成功")])
        processor = ChannelProcessor(FakeVideoProcessor(), transcriber)

        with (
            self._patch_fetch(videos=videos),
            self._patch_cookie(),
            self._patch_media(),  # caption_text=None → fetch_text 抛错
        ):
            result = await processor.process_creator(
                url="https://www.douyin.com/user/MS4wLjABAAAAfake",
                summarizer=FakeSummarizer(),
                summary_language="zh",
                work_dir=self.work_root,
            )

        self.assertEqual(result["ok_count"], 1)
        self.assertIn("兜底转录成功", result["script"])
        self.assertEqual(len(self.media.share_calls), 1)
        self.assertEqual(len(self.media.download_calls), 1)

    async def test_low_gear_url_direct_download(self):
        """列表给了低码率直链时直连 CDN，不查分享页。"""
        videos = [
            {
                "id": "7000000000000000001",
                "title": "低码率视频",
                "create_time": 1760000000,
                "low_gear_url": "https://v11-weba.douyinvod.com/low.mp4",
            },
            dict(VIDEOS[1]),
        ]
        transcriber = FakeTranscriber(
            [_raw_script("低码率直下"), _raw_script("分享页兜底")]
        )
        processor = ChannelProcessor(FakeVideoProcessor(), transcriber)

        with (
            self._patch_fetch(videos=videos),
            self._patch_cookie(),
            self._patch_media(),
        ):
            result = await processor.process_creator(
                url="https://www.douyin.com/user/MS4wLjABAAAAfake",
                summarizer=FakeSummarizer(),
                summary_language="zh",
                work_dir=self.work_root,
            )

        # 第一个视频走低码率直链（不查分享页），第二个走分享页
        self.assertEqual(
            self.media.download_calls[0]["url"],
            "https://v11-weba.douyinvod.com/low.mp4",
        )
        self.assertEqual(len(self.media.share_calls), 1)
        self.assertEqual(self.media.share_calls[0]["id"], "7000000000000000002")
        self.assertIn("低码率直下", result["script"])
        self.assertIn("分享页兜底", result["script"])

    async def test_low_gear_failure_falls_to_share(self):
        """低码率直链过期/失败时回退分享页拿 playwm 直链。"""
        videos = [
            {
                "id": "7000000000000000001",
                "title": "直链过期",
                "create_time": 1760000000,
                "low_gear_url": "https://v11-weba.douyinvod.com/expired.mp4",
            }
        ]
        transcriber = FakeTranscriber([_raw_script("分享页救回来了")])
        processor = ChannelProcessor(FakeVideoProcessor(), transcriber)

        with (
            self._patch_fetch(videos=videos),
            self._patch_cookie(),
            self._patch_media(
                fail_download_urls={"https://v11-weba.douyinvod.com/expired.mp4"}
            ),
        ):
            result = await processor.process_creator(
                url="https://www.douyin.com/user/MS4wLjABAAAAfake",
                summarizer=FakeSummarizer(),
                summary_language="zh",
                work_dir=self.work_root,
            )

        self.assertEqual(result["ok_count"], 1)
        self.assertIn("分享页救回来了", result["script"])
        # 两次下载：低码率失败 + playwm 成功
        self.assertEqual(len(self.media.download_calls), 2)
        self.assertEqual(len(self.media.share_calls), 1)

    async def test_share_enriches_title_and_time(self):
        """列表缺标题/发布时间时用分享页信息补齐小节头。"""
        videos = [
            {
                "id": "7000000000000000001",
                "title": "",
                "create_time": None,
            }
        ]

        def share_factory(video_id):
            info = _default_share_info(video_id)
            info["title"] = "分享页补齐的标题"
            info["create_time"] = 1792205880
            return info

        transcriber = FakeTranscriber([_raw_script("正文")])
        processor = ChannelProcessor(FakeVideoProcessor(), transcriber)

        with (
            self._patch_fetch(videos=videos),
            self._patch_cookie(),
            self._patch_media(share=share_factory),
        ):
            result = await processor.process_creator(
                url="https://www.douyin.com/user/MS4wLjABAAAAfake",
                summarizer=FakeSummarizer(),
                summary_language="zh",
                work_dir=self.work_root,
            )

        stamp = datetime.fromtimestamp(1792205880).strftime("%Y-%m-%d %H:%M")
        self.assertIn(f"## [{stamp}] 分享页补齐的标题", result["script"])

    async def test_image_post_skipped(self):
        """图文作品无音轨：跳过转录、如实标注、单独计数。"""
        videos = [
            dict(VIDEOS[0]),
            {
                "id": "7000000000000000002",
                "title": "图集作品",
                "create_time": 1760000500,
                "is_image": True,
            },
        ]
        transcriber = FakeTranscriber([_raw_script("唯一的口播")])
        processor = ChannelProcessor(FakeVideoProcessor(), transcriber)

        with (
            self._patch_fetch(videos=videos),
            self._patch_cookie(),
            self._patch_media(),
        ):
            result = await processor.process_creator(
                url="https://www.douyin.com/user/MS4wLjABAAAAfake",
                summarizer=FakeSummarizer(),
                summary_language="zh",
                work_dir=self.work_root,
            )

        self.assertEqual(result["image_count"], 1)
        self.assertEqual(result["ok_count"], 1)
        self.assertEqual(result["failed_count"], 0)
        self.assertIn("图文作品，无音轨", result["script"])
        self.assertIn("图集作品", result["script"])  # 小节占位保留时间线
        # 图文视频没有触达下载链路
        self.assertEqual(len(self.media.download_calls), 1)
        self.assertEqual(
            self.media.download_calls[0]["url"],
            "https://aweme.snssdk.com/playwm/7000000000000000001",
        )

    async def test_ytdlp_last_resort(self):
        """分享页也失败时退到 yt-dlp 下载。"""
        vp = FakeVideoProcessor()
        transcriber = FakeTranscriber(
            [_raw_script("yt-dlp 救场"), _raw_script("第二个")]
        )
        processor = ChannelProcessor(vp, transcriber)
        processor.VIDEO_ATTEMPTS = 1

        with (
            self._patch_fetch(),
            self._patch_cookie("ttwid=abc"),
            self._patch_media(fail_share_ids={"7000000000000000001"}),
        ):
            result = await processor.process_creator(
                url="https://www.douyin.com/user/MS4wLjABAAAAfake",
                summarizer=FakeSummarizer(),
                summary_language="zh",
                work_dir=self.work_root,
            )

        self.assertEqual(result["ok_count"], 2)
        # 只有第一个视频走了 yt-dlp，带上了 cookie
        self.assertEqual(len(vp.audio_downloads), 1)
        self.assertEqual(
            vp.audio_downloads[0]["url"],
            "https://www.douyin.com/video/7000000000000000001",
        )
        self.assertEqual(vp.audio_downloads[0]["cookie"], "ttwid=abc")
        self.assertIn("yt-dlp 救场", result["script"])

    async def test_share_reports_image_counts_skipped(self):
        """列表说是视频、分享页说是图文：纠正为图文跳过，不编造转录、不计失败。"""
        videos = [dict(VIDEOS[0])]

        def share_factory(video_id):
            info = _default_share_info(video_id)
            info["is_image"] = True
            return info

        vp = FakeVideoProcessor()
        processor = ChannelProcessor(vp, FakeTranscriber([]))

        with (
            self._patch_fetch(videos=videos),
            self._patch_cookie(),
            self._patch_media(share=share_factory),
        ):
            result = await processor.process_creator(
                url="https://www.douyin.com/user/MS4wLjABAAAAfake",
                summarizer=FakeSummarizer(),
                summary_language="zh",
                work_dir=self.work_root,
            )

        self.assertEqual(result["image_count"], 1)
        self.assertEqual(result["failed_count"], 0)
        self.assertIn("图文作品，无音轨", result["script"])
        # 没有媒体下载发生
        self.assertEqual(self.media.download_calls, [])
        self.assertEqual(vp.audio_downloads, [])

    # ── 暂停与断点续转 ─────────────────────────────────────────

    async def test_pause_stops_after_current_video(self):
        """pause_check 在两个视频之间生效：当前视频完成落盘后停止，无最终产物。"""
        vp = FakeVideoProcessor()
        transcriber = FakeTranscriber([_raw_script("第一段"), _raw_script("不该出现")])
        summarizer = FakeSummarizer()
        processor = ChannelProcessor(vp, transcriber)

        checks = []

        def pause_check():
            checks.append(True)
            return len(checks) >= 2  # 第二次检查（开始第 2 个视频前）触发暂停

        with (
            self._patch_fetch(),
            self._patch_cookie(),
            self._patch_media(),
        ):
            result = await processor.process_creator(
                url="https://www.douyin.com/user/MS4wLjABAAAAfake",
                summarizer=summarizer,
                summary_language="zh",
                work_dir=self.work_root,
                pause_check=pause_check,
            )

        self.assertTrue(result["paused"])
        self.assertEqual(result["done_count"], 1)
        self.assertEqual(result["ok_count"], 1)
        self.assertEqual(result["video_count"], 2)
        # 暂停没有最终产物，也不做摘要
        self.assertEqual(result["script"], "")
        self.assertEqual(result["summary"], "")
        self.assertEqual(summarizer.calls, [])
        # 第二个视频没有被转录
        self.assertEqual(len(self.media.download_calls), 1)

    async def test_pause_immediately_returns_zero_done(self):
        """一开始就请求暂停：0 个完成也不抛「全部失败」异常。"""

        def pause_check():
            return True

        processor = ChannelProcessor(FakeVideoProcessor(), FakeTranscriber([]))
        with (
            self._patch_fetch(),
            self._patch_cookie(),
            self._patch_media(),
        ):
            result = await processor.process_creator(
                url="https://www.douyin.com/user/MS4wLjABAAAAfake",
                summarizer=FakeSummarizer(),
                summary_language="zh",
                work_dir=self.work_root,
                pause_check=pause_check,
            )

        self.assertTrue(result["paused"])
        self.assertEqual(result["done_count"], 0)
        self.assertEqual(result["ok_count"], 0)

    async def test_resume_skips_done_videos(self):
        """续转：已完成（最终状态）的视频不重跑，小节直接复用并计入统计。"""
        resume = {
            "7000000000000000001": {
                "status": "ok",
                "section": "## [2026-10-08 14:19] 上次的第一个视频\n\n旧正文",
                "title": "上次的第一个视频",
                "create_time": 1760000000,
                "index": 1,
                "lang": "zh",
                "error": None,
            }
        }
        vp = FakeVideoProcessor()
        # 只给第二个视频的转录文本：若第一个视频被重跑，FakeTranscriber 会 pop 空列表报错
        transcriber = FakeTranscriber([_raw_script("新补的第二段")])
        summarizer = FakeSummarizer()
        processor = ChannelProcessor(vp, transcriber)

        with (
            self._patch_fetch(),
            self._patch_cookie(),
            self._patch_media(),
        ):
            result = await processor.process_creator(
                url="https://www.douyin.com/user/MS4wLjABAAAAfake",
                summarizer=summarizer,
                summary_language="zh",
                work_dir=self.work_root,
                resume_sections=resume,
            )

        self.assertFalse(result["paused"])
        self.assertEqual(result["ok_count"], 2)
        self.assertEqual(result["done_count"], 2)
        # 旧小节原样复用，新小节按转录生成
        self.assertIn("旧正文", result["script"])
        self.assertIn("新补的第二段", result["script"])
        # 只下载/转录了第二个视频
        self.assertEqual(len(self.media.download_calls), 1)
        # 摘要输入包含新旧两段
        self.assertEqual(len(summarizer.calls), 1)
        self.assertIn("旧正文", summarizer.calls[0]["transcript"])

    async def test_resume_retries_failed_videos(self):
        """续转只复用最终状态：failed 的视频要重跑。"""
        resume = {
            "7000000000000000001": {
                "status": "failed",
                "section": None,
                "title": "上次失败的视频",
                "create_time": 1760000000,
                "index": 1,
                "lang": None,
                "error": "上次挂了",
            }
        }
        transcriber = FakeTranscriber(
            [_raw_script("重试成功一"), _raw_script("第二段")]
        )
        processor = ChannelProcessor(FakeVideoProcessor(), transcriber)

        with (
            self._patch_fetch(),
            self._patch_cookie(),
            self._patch_media(),
        ):
            result = await processor.process_creator(
                url="https://www.douyin.com/user/MS4wLjABAAAAfake",
                summarizer=FakeSummarizer(),
                summary_language="zh",
                work_dir=self.work_root,
                resume_sections=resume,
            )

        self.assertEqual(result["ok_count"], 2)
        self.assertIn("重试成功一", result["script"])

    async def test_resume_orders_by_create_time_desc(self):
        """续转后混合排序：博主两次运行之间发了新视频，按发布时间归位。"""
        videos = [
            {
                "id": "7000000000000000009",
                "title": "新发的视频",
                "create_time": 1770000000,
            },
            {
                "id": "7000000000000000001",
                "title": "旧视频一",
                "create_time": 1760000000,
            },
            {
                "id": "7000000000000000002",
                "title": "旧视频二",
                "create_time": 1750000000,
            },
        ]
        resume = {
            "7000000000000000001": {
                "status": "ok",
                "section": "## 旧小节一",
                "title": "旧视频一",
                "create_time": 1760000000,
                "index": 1,
                "lang": "zh",
                "error": None,
            },
            "7000000000000000002": {
                "status": "ok",
                "section": "## 旧小节二",
                "title": "旧视频二",
                "create_time": 1750000000,
                "index": 2,
                "lang": "zh",
                "error": None,
            },
        }
        transcriber = FakeTranscriber([_raw_script("新视频正文")])
        processor = ChannelProcessor(FakeVideoProcessor(), transcriber)

        with (
            self._patch_fetch(videos=videos),
            self._patch_cookie(),
            self._patch_media(),
        ):
            result = await processor.process_creator(
                url="https://www.douyin.com/user/MS4wLjABAAAAfake",
                summarizer=FakeSummarizer(),
                summary_language="zh",
                work_dir=self.work_root,
                resume_sections=resume,
            )

        script = result["script"]
        self.assertLess(script.index("新发的视频"), script.index("旧小节一"))
        self.assertLess(script.index("旧小节一"), script.index("旧小节二"))

    async def test_on_video_record_and_on_list_callbacks(self):
        """逐视频回调带全字段；列表回调在转录开始前拿到昵称与总数。"""
        vp = FakeVideoProcessor()
        transcriber = FakeTranscriber([_raw_script("第一段"), NO_SPEECH_RAW])
        processor = ChannelProcessor(vp, transcriber)
        records = []
        lists = []

        with (
            self._patch_fetch(),
            self._patch_cookie(),
            self._patch_media(),
        ):
            result = await processor.process_creator(
                url="https://www.douyin.com/user/MS4wLjABAAAAfake",
                summarizer=FakeSummarizer(),
                summary_language="zh",
                work_dir=self.work_root,
                on_video_record=records.append,
                on_list=lambda nickname, total: lists.append((nickname, total)),
            )

        self.assertEqual(lists, [("测试博主", 2)])
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["id"], "7000000000000000001")
        self.assertEqual(records[0]["status"], "ok")
        self.assertEqual(records[0]["lang"], "zh")
        self.assertEqual(records[0]["create_time"], 1760000000)
        self.assertIn("第一段", records[0]["section"])
        self.assertEqual(records[1]["status"], "no_speech")
        # 回调抛异常不拖垮主流程
        self.assertEqual(result["ok_count"], 1)
        self.assertEqual(result["no_speech_count"], 1)

    async def test_resume_counts_in_result(self):
        """续转预置的 no_speech/image 状态正确计入统计与无语音判定。"""
        resume = {
            "7000000000000000001": {
                "status": "no_speech",
                "section": "## 上次无语音\n\n_该视频未检测到语音，无转录内容_",
                "title": "无语音视频",
                "create_time": 1760000000,
                "index": 1,
                "lang": None,
                "error": None,
            },
            "7000000000000000002": {
                "status": "image",
                "section": "## 上次图文\n\n_图文作品，无音轨_",
                "title": "图文",
                "create_time": 1750000000,
                "index": 2,
                "lang": None,
                "error": None,
            },
        }
        processor = ChannelProcessor(FakeVideoProcessor(), FakeTranscriber([]))

        with (
            self._patch_fetch(),
            self._patch_cookie(),
            self._patch_media(),
        ):
            result = await processor.process_creator(
                url="https://www.douyin.com/user/MS4wLjABAAAAfake",
                summarizer=FakeSummarizer(),
                summary_language="zh",
                work_dir=self.work_root,
                resume_sections=resume,
            )

        self.assertEqual(result["ok_count"], 0)
        self.assertEqual(result["no_speech_count"], 1)
        self.assertEqual(result["image_count"], 1)
        self.assertEqual(result["done_count"], 2)
        # 全部无语音（含图文占位）→ 跳过摘要、no_speech 如实上报
        self.assertTrue(result["no_speech"])
        self.assertEqual(result["summary"], "")


if __name__ == "__main__":
    unittest.main()
