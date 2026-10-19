"""subtitle_ocr：硬字幕 OCR 提取的单元测试（纯函数，无网络、无真实 OCR）。"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import subtitle_ocr  # noqa: E402


def _frames(texts_per_frame, conf=0.9, center=0.5):
    """把 [[行文本, ...], ...] 构造成 build_subtitle_text 的入参（默认居中）。"""
    return [
        [(center, text, conf) for text in texts] if texts else None
        for texts in texts_per_frame
    ]


def _box(top, left=30, right=330, bottom=60):
    """4 点坐标框（默认水平居中于 360 宽的帧）。"""
    return [
        [left, top],
        [right, top],
        [right, bottom],
        [left, bottom],
    ]


class NormalizeLineTests(unittest.TestCase):
    def test_watermark_line_is_dropped(self):
        self.assertEqual(subtitle_ocr._normalize_line("@全能的野人"), "")

    def test_symbolic_line_is_dropped(self):
        self.assertEqual(subtitle_ocr._normalize_line("。。。"), "")
        self.assertEqual(subtitle_ocr._normalize_line("&&&"), "")

    def test_cjk_internal_spaces_removed(self):
        self.assertEqual(
            subtitle_ocr._normalize_line("我想 为我家 带来"), "我想为我家带来"
        )

    def test_english_spaces_kept(self):
        self.assertEqual(subtitle_ocr._normalize_line("hello world"), "hello world")

    def test_plain_text_untouched(self):
        self.assertEqual(subtitle_ocr._normalize_line("理想的收益"), "理想的收益")


class ParseEngineResultTests(unittest.TestCase):
    FRAME_W = 360.0

    def test_none_returns_empty(self):
        self.assertEqual(subtitle_ocr._parse_engine_result(None, self.FRAME_W), [])

    def test_rows_sorted_by_box_position(self):
        raw = [
            [[_box(60), "第二行", 0.9]],
            [[_box(10), "第一行", 0.95]],
        ]
        raw = [raw[0][0], raw[1][0]]  # 展平成 [[box, text, conf], ...]
        result = subtitle_ocr._parse_engine_result(raw, self.FRAME_W)
        self.assertEqual([text for _, text, _ in result], ["第一行", "第二行"])

    def test_center_ratio_computed(self):
        # 框 30-330，帧宽 360 → 中心 180/360 = 0.5
        raw = [[_box(10), "居中文本", 0.9]]
        ((center, text, _),) = subtitle_ocr._parse_engine_result(raw, self.FRAME_W)
        self.assertAlmostEqual(center, 0.5)
        self.assertEqual(text, "居中文本")

    def test_bad_rows_skipped(self):
        raw = [["单元素行"], [_box(5), "文本", 0.9]]
        result = subtitle_ocr._parse_engine_result(raw, self.FRAME_W)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][1], "文本")

    def test_zero_width_treated_as_centered(self):
        raw = [[_box(5, left=0, right=10), "文本", 0.9]]
        ((center, _, _),) = subtitle_ocr._parse_engine_result(raw, 0.0)
        self.assertEqual(center, 0.5)


class BuildSubtitleTextTests(unittest.TestCase):
    def test_empty_input_returns_empty(self):
        self.assertEqual(subtitle_ocr.build_subtitle_text([]), "")

    def test_consecutive_duplicate_lines_collapse(self):
        """同一条字幕横跨多个采样帧，只保留一次。"""
        frames = _frames(
            [
                ["甲句"],
                ["甲句"],
                ["乙句"],
                ["乙句"],
                ["丙句"],
                ["丙句"],
                ["丁句"],
                ["丁句"],
                [],
                [],
            ]
        )
        text = subtitle_ocr.build_subtitle_text(frames)
        self.assertEqual(text, "甲句\n乙句\n丙句\n丁句")

    def test_two_line_subtitle_partial_change(self):
        """双行字幕：第一行不变、第二行变化时，只有新行进入正文。"""
        frames = _frames(
            [["甲句", "上句"], ["甲句", "乙句"], ["乙句", "丙句"], ["丙句", "丁句"]]
            + [["丁句", "丁句二"], ["丁句", "丁句二"]]
        )
        text = subtitle_ocr.build_subtitle_text(frames)
        self.assertEqual(text, "甲句\n上句\n乙句\n丙句\n丁句\n丁句二")

    def test_off_center_ui_labels_filtered(self):
        """不居中的软件 UI 标签/板书不是字幕，也不计入任何统计。"""
        ui_frames = [
            [
                (0.5, f"字幕正文{i}", 0.9),
                (0.15, "分时成交量", 0.9),  # 左侧 UI 面板
                (0.85, "右上角标签", 0.9),
            ]
            for i in range(1, 7)
        ]
        text = subtitle_ocr.build_subtitle_text(ui_frames)
        self.assertEqual(text, "\n".join(f"字幕正文{i}" for i in range(1, 7)))

    def test_static_disclaimer_filtered(self):
        """几乎每帧都在的居中文本是免责声明/水印，整条剔除。"""
        frames = [
            [(0.5, "投资有风险入市需谨慎", 0.9), (0.5, f"字幕第{i}句", 0.9)]
            for i in range(1, 13)
        ]
        text = subtitle_ocr.build_subtitle_text(frames)
        self.assertIn("字幕第1句", text)
        self.assertIn("字幕第12句", text)
        self.assertNotIn("投资有风险", text)

    def test_ppt_video_without_speech_subtitles_rejected(self):
        """底部只有免责声明（全部静态）的 PPT 视频：动态行为 0 → 判定无烧录字幕。"""
        frames = [
            [(0.5, "投资有风险入市需谨慎", 0.9), (0.5, "仅做整理不构成建议", 0.9)]
        ] * 20
        self.assertEqual(subtitle_ocr.build_subtitle_text(frames), "")

    def test_low_dynamic_coverage_returns_empty(self):
        """只有开头几帧有动态字幕（后面是未烧字幕的内容）→ 宁可回落 Whisper。"""
        frames = _frames([[f"第{i}句"] for i in range(1, 8)] + [None] * 33)
        self.assertEqual(subtitle_ocr.build_subtitle_text(frames), "")

    def test_too_few_dynamic_frames_returns_empty(self):
        frames = _frames([["短句"]] * 3)
        self.assertEqual(subtitle_ocr.build_subtitle_text(frames), "")

    def test_too_few_distinct_lines_returns_empty(self):
        """一句话循环整条视频（歌词类）：帧数/覆盖都够但行数不足门槛。"""
        frames = _frames([["同一句歌词"]] * 12)
        self.assertEqual(subtitle_ocr.build_subtitle_text(frames), "")

    def test_repeated_line_kept_when_not_persistent(self):
        """副歌/口头禅类重复行：出现频率低于静态门槛（25%）时如实保留。"""
        frames = []
        for verse in range(3):
            for i in range(1, 7):
                frames.append([(0.5, f"主歌{verse}_{i}句", 0.9)])
            frames.append([(0.5, "他们说", 0.9)])  # 3/21 ≈ 14% 帧，远低于静态门槛
        text = subtitle_ocr.build_subtitle_text(frames)
        self.assertIn("他们说", text)
        self.assertIn("主歌0_1句", text)
        self.assertIn("主歌2_6句", text)

    def test_watermark_and_low_confidence_filtered(self):
        """@水印行与低置信度行不参与正文，也不计入覆盖帧。"""
        frames = [
            [
                (0.5, "@全能的野人", 0.95),
                (0.5, f"正文第{i}句", 0.95),
                (0.5, "低置信度噪声", 0.3),
            ]
            for i in range(1, 11)
        ]
        text = subtitle_ocr.build_subtitle_text(frames)
        self.assertEqual(text, "\n".join(f"正文第{i}句" for i in range(1, 11)))
        self.assertNotIn("全能的野人", text)
        self.assertNotIn("低置信度噪声", text)

    def test_thresholds_overridable(self):
        """门槛参数可调（测试/调参用）。"""
        frames = _frames([["短句"]] * 4 + [[]] * 6)
        self.assertEqual(subtitle_ocr.build_subtitle_text(frames), "")
        self.assertEqual(
            subtitle_ocr.build_subtitle_text(
                frames,
                min_dynamic_frames=3,
                min_dynamic_coverage=0.3,
                min_distinct_lines=1,
            ),
            "短句",
        )


class PickFpsTests(unittest.TestCase):
    def test_short_video_uses_1fps(self):
        self.assertEqual(subtitle_ocr._pick_fps(60), 1.0)
        self.assertEqual(subtitle_ocr._pick_fps(150), 1.0)

    def test_long_video_downsamples(self):
        self.assertEqual(subtitle_ocr._pick_fps(151), 0.5)
        self.assertEqual(subtitle_ocr._pick_fps(553), 0.5)

    def test_unknown_duration_defaults_to_long(self):
        self.assertEqual(subtitle_ocr._pick_fps(0), 0.5)


class EnabledTests(unittest.TestCase):
    def test_env_disables_ocr(self):
        for value in ("0", "off", "false", "no", "OFF"):
            with (
                mock.patch.dict(os.environ, {"SUBTITLE_OCR": value}),
                mock.patch.object(subtitle_ocr, "ocr_available", return_value=True),
            ):
                self.assertFalse(subtitle_ocr.is_enabled(), value)

    def test_enabled_by_default_when_available(self):
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(subtitle_ocr, "ocr_available", return_value=True),
        ):
            self.assertTrue(subtitle_ocr.is_enabled())

    def test_disabled_when_dependency_missing(self):
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(subtitle_ocr, "ocr_available", return_value=False),
        ):
            self.assertFalse(subtitle_ocr.is_enabled())


class FfmpegCmdTests(unittest.TestCase):
    def test_cmd_contains_sampling_and_crop_filters(self):
        cmd = subtitle_ocr._ffmpeg_frames_cmd(
            Path("/tmp/v.mp4"), Path("/tmp/frames"), fps=0.5
        )
        self.assertEqual(cmd[0], "ffmpeg")
        vf = cmd[cmd.index("-vf") + 1]
        self.assertIn("fps=0.5", vf)
        self.assertIn("crop=iw:ih*0.35:0:ih*0.65", vf)
        self.assertNotIn("scale", vf)  # 保持原始分辨率，小字号 OCR 才准
        self.assertEqual(cmd[cmd.index("-frames:v") + 1], "400")
        self.assertTrue(str(cmd[-1]).endswith("f_%05d.png"))


class ExtractHardSubtitlesTests(unittest.IsolatedAsyncioTestCase):
    """extract_hard_subtitles 的可用性短路逻辑（OCR 引擎本身不真跑）。"""

    async def test_returns_empty_when_disabled(self):
        with mock.patch.object(subtitle_ocr, "is_enabled", return_value=False):
            self.assertEqual(
                await subtitle_ocr.extract_hard_subtitles(
                    Path("/nonexistent.mp4"), Path("/tmp")
                ),
                "",
            )

    async def test_returns_empty_for_missing_file(self):
        with (
            mock.patch.object(subtitle_ocr, "is_enabled", return_value=True),
            mock.patch.object(
                subtitle_ocr, "_extract_frames", side_effect=AssertionError
            ),
        ):
            self.assertEqual(
                await subtitle_ocr.extract_hard_subtitles(
                    Path("/nonexistent.mp4"), Path("/tmp")
                ),
                "",
            )

    async def test_exception_returns_empty_not_raises(self):
        """抽帧/OCR 抛异常不外泄：调用方靠空串回落 Whisper。"""
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp)
            video = work_root / "v.mp4"
            video.write_bytes(b"\x00" * 16)
            with (
                mock.patch.object(subtitle_ocr, "is_enabled", return_value=True),
                mock.patch.object(
                    subtitle_ocr, "_extract_frames", side_effect=RuntimeError("boom")
                ),
            ):
                self.assertEqual(
                    await subtitle_ocr.extract_hard_subtitles(video, work_root), ""
                )
            # 临时帧目录已清理
            self.assertFalse((work_root / "ocr_v").exists())


if __name__ == "__main__":
    unittest.main()
