"""抖音博主合集的断点续转进度存储。

设计要点：
- 按博主的 sec_uid 独立一个 JSON 文件（temp/channel_progress_<hash>.json），
  与 tasks.json 解耦——任务记录可能被删除/取代，但已转录的视频小节应当
  保留，供下次续转直接复用（转录是纯文本产物，与 LLM 模型/摘要语言无关）。
- 逐视频追加写入：每完成一个视频（成功/无语音/图文/失败）都落盘一次，
  进程被杀、服务重启、Ctrl+C 都不丢已完成的部分。
- 任务完成时删除进度文件；删除任务时一并清理，避免陈旧进度让下次提交
  意外续转。

文件结构：
{
  "sec_uid": "MS4wLjAB...",
  "nickname": "全能的野人",
  "url": "https://www.douyin.com/user/...",
  "total": 1221,
  "updated_at": 1760000000,
  "videos": {
    "<aweme_id>": {
      "status": "ok" | "no_speech" | "image" | "failed",
      "section": "## [2026-10-08 14:19] ...",
      "title": "...", "create_time": 1760000000, "index": 3,
      "lang": "zh", "error": "..."
    }
  }
}
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# 这些状态的小节已是最终产物，续转时直接复用、跳过重跑
FINAL_STATUSES = frozenset({"ok", "no_speech", "image"})


def progress_path(temp_dir: Path, sec_uid: str) -> Path:
    """进度文件路径：sec_uid 哈希化，避免超长/特殊字符文件名。"""
    digest = hashlib.sha1(sec_uid.encode("utf-8")).hexdigest()[:20]
    return Path(temp_dir) / f"channel_progress_{digest}.json"


def load_progress(temp_dir: Path, sec_uid: str) -> dict:
    """读取进度；无文件或损坏时返回空结构。"""
    empty = {"sec_uid": sec_uid, "nickname": "", "url": "", "total": None, "videos": {}}
    if not sec_uid:
        return empty
    path = progress_path(temp_dir, sec_uid)
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("videos"), dict):
                data.setdefault("sec_uid", sec_uid)
                data.setdefault("nickname", "")
                data.setdefault("url", "")
                data.setdefault("total", None)
                return data
    except Exception as e:
        logger.warning(f"读取续转进度失败（按无进度处理）: {path} {e}")
    return empty


def save_progress(temp_dir: Path, sec_uid: str, data: dict) -> None:
    """原子写入进度文件（先写临时文件再 rename，进程被杀不产生半截 JSON）。"""
    if not sec_uid or not isinstance(data, dict):
        return
    data["sec_uid"] = sec_uid
    data["updated_at"] = int(time.time())
    path = progress_path(temp_dir, sec_uid)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as e:
        logger.warning(f"保存续转进度失败: {path} {e}")
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def delete_progress(temp_dir: Path, sec_uid: str) -> None:
    """任务完成/删除时清理进度文件。"""
    if not sec_uid:
        return
    try:
        progress_path(temp_dir, sec_uid).unlink(missing_ok=True)
    except Exception as e:
        logger.warning(f"删除续转进度失败: {e}")


def resume_sections(progress_data: dict) -> dict:
    """从进度数据提取可直接复用的小节（最终状态的视频）。"""
    videos = (progress_data or {}).get("videos") or {}
    return {
        vid: rec
        for vid, rec in videos.items()
        if isinstance(rec, dict) and rec.get("status") in FINAL_STATUSES
    }


def done_count(progress_data: dict) -> int:
    """已完成（最终状态）的视频数。"""
    return len(resume_sections(progress_data))
