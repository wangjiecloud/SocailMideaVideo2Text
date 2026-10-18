"""pipeline：抖音 URL 识别与转录元信息剥离的纯函数测试。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import pipeline as pl  # noqa: E402


class DouyinSecUidTest(unittest.TestCase):
    def test_user_path_url(self):
        url = "https://www.douyin.com/user/MS4wLjABAAAAabcdefghijk123_-"
        self.assertEqual(pl.douyin_sec_uid(url), "MS4wLjABAAAAabcdefghijk123_-")
        self.assertTrue(pl.is_douyin_profile_url(url))

    def test_user_path_with_trailing_slash(self):
        url = "https://www.douyin.com/user/MS4wLjABAAAAabcdefghijk123_-/"
        self.assertEqual(pl.douyin_sec_uid(url), "MS4wLjABAAAAabcdefghijk123_-")

    def test_profile_query_url(self):
        url = (
            "https://www.douyin.com/user/profile?sec_uid=MS4wLjABAAAAxyz789&from=qrcode"
        )
        self.assertEqual(pl.douyin_sec_uid(url), "MS4wLjABAAAAxyz789")
        self.assertTrue(pl.is_douyin_profile_url(url))

    def test_http_scheme(self):
        url = "http://www.douyin.com/user/MS4wLjABAAAAxyz789"
        self.assertEqual(pl.douyin_sec_uid(url), "MS4wLjABAAAAxyz789")

    def test_video_url_is_not_profile(self):
        # 单视频页不是博主主页
        url = "https://www.douyin.com/video/7123456789012345678"
        self.assertEqual(pl.douyin_sec_uid(url), "")
        self.assertFalse(pl.is_douyin_profile_url(url))

    def test_short_link_is_not_supported(self):
        # v.douyin.com 短链需要跳转解析，当前不支持
        self.assertEqual(pl.douyin_sec_uid("https://v.douyin.com/abc123/"), "")
        self.assertFalse(pl.is_douyin_profile_url("https://v.douyin.com/abc123/"))

    def test_non_douyin_url(self):
        self.assertEqual(pl.douyin_sec_uid("https://www.youtube.com/watch?v=x"), "")
        self.assertEqual(pl.douyin_sec_uid(""), "")
        self.assertEqual(pl.douyin_sec_uid(None), "")


class StripTranscriptMetaTest(unittest.TestCase):
    RAW = (
        "# Video Transcription\n"
        "\n"
        "**Detected Language:** zh\n"
        "**Language Probability:** 0.98\n"
        "\n"
        "## Transcription Content\n"
        "\n"
        "**[00:00 - 00:05]**\n"
        "\n"
        "大家好，这是正文。\n"
    )

    def test_strips_headers_keeps_body(self):
        out = pl.strip_transcript_meta(self.RAW)
        self.assertNotIn("# Video Transcription", out)
        self.assertNotIn("Detected Language", out)
        self.assertNotIn("Language Probability", out)
        self.assertNotIn("## Transcription Content", out)
        # 时间戳与正文必须保留
        self.assertIn("**[00:00 - 00:05]**", out)
        self.assertIn("大家好，这是正文。", out)

    def test_keeps_non_transcription_headings(self):
        raw = "# 随便什么标题\n\n## 小节\n\n正文"
        out = pl.strip_transcript_meta(raw)
        self.assertIn("# 随便什么标题", out)
        self.assertIn("## 小节", out)

    def test_empty_input(self):
        self.assertEqual(pl.strip_transcript_meta(""), "")
        self.assertEqual(pl.strip_transcript_meta(None), "")


class MergeTranscriptTextTest(unittest.TestCase):
    RAW = (
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

    def test_merges_into_single_paragraph(self):
        out = pl.merge_transcript_text(self.RAW)
        self.assertNotIn("**[", out)
        self.assertNotIn("Detected Language", out)
        self.assertNotIn("#", out)
        # 中文相邻直接拼接，不加空格
        self.assertEqual(out, "第一句。第二句。")

    def test_english_pieces_joined_with_space(self):
        raw = (
            "## Transcription Content\n"
            "\n"
            "**[00:00 - 00:02]**\n"
            "\n"
            "Hello world.\n"
            "\n"
            "**[00:02 - 00:04]**\n"
            "\n"
            "Second line.\n"
        )
        self.assertEqual(pl.merge_transcript_text(raw), "Hello world. Second line.")

    def test_empty_and_marker_only_input(self):
        self.assertEqual(pl.merge_transcript_text(""), "")
        self.assertEqual(pl.merge_transcript_text(None), "")
        self.assertEqual(pl.merge_transcript_text("**[00:00 - 00:01]**\n\n"), "")


if __name__ == "__main__":
    unittest.main()
