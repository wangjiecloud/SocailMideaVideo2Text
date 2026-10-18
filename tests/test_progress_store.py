"""progress_store：抖音博主合集续转进度存储的单元测试（临时目录，无网络）。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import progress_store  # noqa: E402

SEC_UID = "MS4wLjABAAAAtest0000000000000000000000000000000000"


def _sample_data():
    return {
        "nickname": "测试博主",
        "url": "https://www.douyin.com/user/" + SEC_UID,
        "total": 10,
        "videos": {
            "7000000000000000001": {
                "status": "ok",
                "section": "## [2026-10-08 14:19] 第一个",
                "title": "第一个",
                "create_time": 1760000000,
                "index": 1,
                "lang": "zh",
                "error": None,
            },
            "7000000000000000002": {
                "status": "failed",
                "section": None,
                "title": "第二个",
                "create_time": 1759000000,
                "index": 2,
                "lang": None,
                "error": "下载失败",
            },
            "7000000000000000003": {
                "status": "image",
                "section": "## [2026-09-01 10:00] 图文",
                "title": "图文",
                "create_time": 1758000000,
                "index": 3,
                "lang": None,
                "error": None,
            },
        },
    }


class ProgressStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())

    def test_load_missing_returns_empty_structure(self):
        data = progress_store.load_progress(self.temp_dir, SEC_UID)
        self.assertEqual(data["sec_uid"], SEC_UID)
        self.assertEqual(data["videos"], {})
        self.assertIsNone(data["total"])

    def test_load_empty_sec_uid(self):
        data = progress_store.load_progress(self.temp_dir, "")
        self.assertEqual(data["videos"], {})

    def test_save_and_load_roundtrip(self):
        progress_store.save_progress(self.temp_dir, SEC_UID, _sample_data())
        loaded = progress_store.load_progress(self.temp_dir, SEC_UID)
        self.assertEqual(loaded["nickname"], "测试博主")
        self.assertEqual(loaded["total"], 10)
        self.assertEqual(len(loaded["videos"]), 3)
        self.assertEqual(
            loaded["videos"]["7000000000000000001"]["section"],
            "## [2026-10-08 14:19] 第一个",
        )
        self.assertIn("updated_at", loaded)

    def test_save_writes_no_tmp_leftover(self):
        progress_store.save_progress(self.temp_dir, SEC_UID, _sample_data())
        leftovers = list(self.temp_dir.glob("channel_progress_*.tmp"))
        self.assertEqual(leftovers, [])

    def test_corrupted_file_returns_empty(self):
        path = progress_store.progress_path(self.temp_dir, SEC_UID)
        path.write_text("{not json", encoding="utf-8")
        data = progress_store.load_progress(self.temp_dir, SEC_UID)
        self.assertEqual(data["videos"], {})

    def test_resume_sections_filters_final_statuses(self):
        sections = progress_store.resume_sections(_sample_data())
        # failed 不算完成，续转时要重跑
        self.assertIn("7000000000000000001", sections)
        self.assertIn("7000000000000000003", sections)
        self.assertNotIn("7000000000000000002", sections)
        self.assertEqual(len(sections), 2)

    def test_resume_sections_empty_data(self):
        self.assertEqual(progress_store.resume_sections({}), {})
        self.assertEqual(progress_store.resume_sections(None), {})

    def test_done_count(self):
        self.assertEqual(progress_store.done_count(_sample_data()), 2)
        self.assertEqual(progress_store.done_count({}), 0)

    def test_delete_progress(self):
        progress_store.save_progress(self.temp_dir, SEC_UID, _sample_data())
        self.assertTrue(progress_store.progress_path(self.temp_dir, SEC_UID).exists())
        progress_store.delete_progress(self.temp_dir, SEC_UID)
        self.assertFalse(progress_store.progress_path(self.temp_dir, SEC_UID).exists())
        # 再删一次不报错
        progress_store.delete_progress(self.temp_dir, SEC_UID)

    def test_progress_path_is_deterministic_and_hashed(self):
        p1 = progress_store.progress_path(self.temp_dir, SEC_UID)
        p2 = progress_store.progress_path(self.temp_dir, SEC_UID)
        self.assertEqual(p1, p2)
        self.assertNotIn(SEC_UID, p1.name)
        self.assertTrue(p1.name.startswith("channel_progress_"))
        self.assertTrue(p1.name.endswith(".json"))
        # 不同 sec_uid 不冲突
        other = progress_store.progress_path(self.temp_dir, SEC_UID + "x")
        self.assertNotEqual(p1, other)

    def test_save_load_json_serializable_cycle(self):
        """JSON 往返不丢字段类型（create_time 是 int，续转排序依赖它）。"""
        progress_store.save_progress(self.temp_dir, SEC_UID, _sample_data())
        raw = json.loads(
            progress_store.progress_path(self.temp_dir, SEC_UID).read_text(
                encoding="utf-8"
            )
        )
        self.assertIsInstance(raw["videos"]["7000000000000000001"]["create_time"], int)


if __name__ == "__main__":
    unittest.main()
