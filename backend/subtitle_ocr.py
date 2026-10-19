"""
烧录（硬）字幕 OCR 提取。

抖音创作者常把字幕直接烧进画面（区别于 cla_info 服务端字幕轨），Whisper
语音识别对股票类快语速内容的同音字错认率高（「理想的收益」→「定想的收益」），
而画面里的字幕是创作者亲手敲的正确文本。下载到 mp4 后先对画面底部字幕带
抽帧 OCR，判定带烧录字幕就直接用 OCR 文本、跳过 Whisper；未命中返回空串，
调用方继续走音频转录链路。

只认「字幕」，不认「画面上的一切文字」——PPT 讲解视频的底部区域是软件
UI 标签和静态免责声明，整屏拿过来只会得到碎片噪声，还顶掉了口播正文。
因此一条 OCR 文本行要同时满足：

1. 置信度 ≥ MIN_LINE_CONFIDENCE（无字幕画面偶尔会脑补低置信度文本）；
2. 水平居中（|中心偏移 - 0.5| ≤ CENTER_TOLERANCE）——烧录字幕/歌词
   都居中排版，软件 UI 标签和板书散落各处；
3. 非静态：出现帧占比 ≤ STATIC_FRAME_SHARE——「投资有风险入市需谨慎」
   这类免责声明和水印几乎每帧都在，字幕只停留几秒。

视频级门槛（三挡都过才算「带烧录字幕」）：
- 有动态字幕行的帧数 ≥ MIN_DYNAMIC_FRAMES（防零星误检）；
- 动态字幕帧覆盖率 ≥ MIN_DYNAMIC_COVERAGE（防只烧了几句关键话的
  视频拿 OCR 片段当全文、漏掉没烧字幕的口播）；
- 去重后的动态字幕行数 ≥ MIN_DISTINCT_LINES。

可用性：
- 依赖 rapidocr-onnxruntime（软依赖，未安装时整体跳过，不影响原链路）；
- 环境变量 SUBTITLE_OCR=0/off/false/no 可强制关闭（排查问题时用）。

所有 CPU 密集操作（引擎初始化、逐帧 OCR）通过 asyncio.to_thread 执行，
不阻塞事件循环；抽帧用 ffmpeg 子进程（fps 自适应视频时长，长视频降采样，
帧数上限保护）。临时帧目录在 finally 里整体清理。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import threading
from collections import Counter
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# SUBTITLE_OCR 置为这些值时整体关闭
_DISABLED_VALUES = frozenset({"0", "off", "false", "no"})

# 行级过滤
MIN_LINE_CONFIDENCE = 0.55
CROP_RATIO = 0.35  # 底部字幕带：抖音字幕/歌词/免责声明所在区域
CENTER_TOLERANCE = 0.2  # 字幕行水平中心偏移容差（居中排版）
STATIC_FRAME_SHARE = 0.25  # 出现帧占比超过它的行按免责声明/水印丢弃

# 视频级门槛
MIN_DYNAMIC_FRAMES = 6
MIN_DYNAMIC_COVERAGE = 0.35
MIN_DISTINCT_LINES = 4

# 抽帧：短视频 1 帧/秒，长视频 2 秒/帧（烧录字幕至少停留约 2 秒）
OCR_FPS_SHORT = 1.0
OCR_FPS_LONG = 0.5
SHORT_VIDEO_SECONDS = 150
MAX_FRAMES = 400

# 水印行：@昵称（分享页 playwm 视频右下角水印）不算字幕
_WATERMARK_RE = re.compile(r"^\s*@")
# 纯符号/空白行（去掉非文字字符后不足 2 个字符）
_SYMBOLIC_RE = re.compile(r"[\W_]+", re.UNICODE)
# 中文字符之间的空格是 OCR 噪声，去掉；英文单词间空格保留
_CJK_GAP_RE = re.compile(
    r"(?<=[\u3400-\u9fff\u3000-\u303f\uff00-\uffef])\s+(?=[\u3400-\u9fff\u3000-\u303f\uff00-\uffef])"
)

_engine = None
_engine_lock = threading.Lock()


def ocr_available() -> bool:
    """rapidocr-onnxruntime 是否已安装（软依赖，导入失败不算错误）。"""
    try:
        import rapidocr_onnxruntime  # noqa: F401
    except ImportError:
        return False
    return True


def is_enabled() -> bool:
    """OCR 硬字幕提取是否启用：未显式关闭且依赖可用。"""
    if (os.environ.get("SUBTITLE_OCR") or "").strip().lower() in _DISABLED_VALUES:
        return False
    return ocr_available()


def _get_engine():
    """惰性初始化 RapidOCR 引擎（加载模型较慢，全局单例）。"""
    global _engine
    with _engine_lock:
        if _engine is None:
            from rapidocr_onnxruntime import RapidOCR

            _engine = RapidOCR()
    return _engine


def _normalize_line(text: str) -> str:
    """单行 OCR 文本清洗；返回空串表示该行不参与字幕判定。"""
    text = (text or "").strip()
    if not text or _WATERMARK_RE.match(text):
        return ""
    # 中文字符间空格是 OCR 分辨率噪声；纯符号行不是字幕
    text = _CJK_GAP_RE.sub("", text)
    if len(_SYMBOLIC_RE.sub("", text)) < 2:
        return ""
    return text


def _parse_engine_result(raw, frame_width: float) -> list[tuple[float, str, float]]:
    """RapidOCR 原始输出 → [(center_x_ratio, text, conf), ...]，按框位置排序。

    raw 形如 [[box(4 点坐标), text, conf], ...] 或 None（该帧无检出）。
    center_x_ratio 是文本框中心相对帧宽的比例（0.5 = 水平居中）。
    """
    if not raw:
        return []
    rows = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) < 3:
            continue
        box, text, conf = item[0], str(item[1]), item[2]
        try:
            ys = [float(p[1]) for p in box]
            xs = [float(p[0]) for p in box]
            top = min(ys)
            center_x = (min(xs) + max(xs)) / 2
        except (TypeError, ValueError, IndexError):
            continue
        try:
            confidence = float(conf)
        except (TypeError, ValueError):
            confidence = 0.0
        ratio = center_x / frame_width if frame_width else 0.5
        rows.append(((top, center_x), ratio, text, confidence))
    rows.sort(key=lambda r: r[0])
    return [(ratio, text, conf) for _, ratio, text, conf in rows]


def build_subtitle_text(
    frame_results: list[Optional[list[tuple[float, str, float]]]],
    *,
    min_line_confidence: float = MIN_LINE_CONFIDENCE,
    center_tolerance: float = CENTER_TOLERANCE,
    static_frame_share: float = STATIC_FRAME_SHARE,
    min_dynamic_frames: int = MIN_DYNAMIC_FRAMES,
    min_dynamic_coverage: float = MIN_DYNAMIC_COVERAGE,
    min_distinct_lines: int = MIN_DISTINCT_LINES,
) -> str:
    """把逐帧 OCR 结果组装成字幕全文；不构成「带烧录字幕」判定时返回空串。

    Args:
        frame_results: 按时间序的每帧行列表 [(center_x_ratio, text, conf), ...]，
            None/空列表表示该帧没有检出

    过滤链：置信度 → 居中 → 静态行（免责声明/水印）剔除 → 连续去重。
    """
    total = len(frame_results)
    if total == 0:
        return ""

    # 1) 逐帧清洗：置信度、归一化、居中（不居中的是 UI 标签/板书，不是字幕）
    per_frame: list[list[str]] = []
    for rows in frame_results:
        lines = []
        for center_x, text, conf in rows or []:
            text = _normalize_line(text)
            if not text or conf < min_line_confidence:
                continue
            if abs(center_x - 0.5) > center_tolerance:
                continue
            lines.append(text)
        per_frame.append(lines)

    text_frames = sum(1 for lines in per_frame if lines)
    if not text_frames:
        return ""

    # 2) 静态行统计：几乎每帧都在的居中文本是免责声明/固定水印，不是字幕
    line_frame_counts: Counter = Counter()
    for lines in per_frame:
        for text in set(lines):
            line_frame_counts[text] += 1
    static_limit = max(4.0, static_frame_share * text_frames)

    # 3) 动态字幕帧统计 + 连续去重（同一句字幕横跨多个采样帧只保留一次）
    dynamic_frames = 0
    distinct_lines: set[str] = set()
    out_lines: list[str] = []
    prev_lines: set[str] = set()
    for lines in per_frame:
        dynamic = [t for t in lines if line_frame_counts[t] <= static_limit]
        if dynamic:
            dynamic_frames += 1
        for text in dynamic:
            distinct_lines.add(text)
            if text not in prev_lines:
                out_lines.append(text)
        prev_lines = set(dynamic)

    if dynamic_frames < min_dynamic_frames:
        return ""
    if dynamic_frames / total < min_dynamic_coverage:
        return ""
    if len(distinct_lines) < min_distinct_lines:
        return ""
    return "\n".join(out_lines)


def _pick_fps(duration_seconds: float) -> float:
    """按视频时长选抽帧频率：字幕至少停留约 2 秒，长视频降采样控时。

    时长未知（0/探测失败）按长视频处理——密集采样受 MAX_FRAMES 截断，
    只会覆盖前几分钟，保守采样才能覆盖全长。
    """
    if 0 < duration_seconds <= SHORT_VIDEO_SECONDS:
        return OCR_FPS_SHORT
    return OCR_FPS_LONG


def _ffmpeg_frames_cmd(
    video_path: Path,
    frames_dir: Path,
    fps: float,
    max_frames: int = MAX_FRAMES,
    crop_ratio: float = CROP_RATIO,
) -> list[str]:
    """ffmpeg 抽帧命令：fps 采样 + 底部字幕带裁剪（保持原始分辨率）。"""
    vf = f"fps={fps},crop=iw:ih*{crop_ratio:.2f}:0:ih*{1 - crop_ratio:.2f}"
    return [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video_path),
        "-vf",
        vf,
        "-frames:v",
        str(max_frames),
        str(frames_dir / "f_%05d.png"),
    ]


async def _probe_duration(video_path: Path) -> float:
    """ffprobe 读视频时长（秒）；失败返回 0（调用方按保守频率处理）。"""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "csv=p=0",
        str(video_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    try:
        return float(out.decode().strip())
    except ValueError:
        return 0.0


async def _extract_frames(video_path: Path, frames_dir: Path) -> list[Path]:
    """按时长自适应频率抽帧到 frames_dir，返回按序帧文件列表；失败抛异常。"""
    duration = await _probe_duration(video_path)
    cmd = _ffmpeg_frames_cmd(video_path, frames_dir, _pick_fps(duration))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 抽帧失败: {stderr.decode(errors='replace')[:200]}")
    return sorted(frames_dir.glob("f_*.png"))


def _ocr_frames(
    engine, frames: list[Path], frame_width: float
) -> list[list[tuple[float, str, float]]]:
    """逐帧 OCR（在线程池里执行，不阻塞事件循环）；单帧失败按无检出处理。"""
    results = []
    for frame in frames:
        try:
            raw, _ = engine(str(frame))
        except Exception as e:
            logger.warning(f"OCR 单帧失败（按无字幕处理）: {frame.name} {e}")
            raw = None
        results.append(_parse_engine_result(raw, frame_width))
    return results


def _frame_width(frame: Path) -> float:
    """读帧宽（居中判定用）；读不出时返回 0（按居中处理，不做过滤）。"""
    try:
        from PIL import Image

        with Image.open(frame) as im:
            return float(im.width)
    except Exception:
        return 0.0


async def extract_hard_subtitles(video_path, work_dir) -> str:
    """提取视频画面里烧录的硬字幕全文。

    不可用（依赖未装/被环境变量关闭）、未检出（不构成字幕判定）或任何
    失败一律返回空串——调用方据此回落 Whisper 音频转录，绝不让 OCR 异常
    拖垮原链路。

    Args:
        video_path: 已下载的 mp4 文件路径
        work_dir: 帧临时目录的父目录（内部建 ocr_<stem>/ 并负责清理）
    """
    if not is_enabled():
        return ""
    video_path = Path(video_path)
    if not video_path.is_file():
        return ""

    frames_dir = Path(work_dir) / f"ocr_{video_path.stem}"
    try:
        shutil.rmtree(frames_dir, ignore_errors=True)
        frames_dir.mkdir(parents=True, exist_ok=True)
        frames = await _extract_frames(video_path, frames_dir)
        if not frames:
            return ""
        engine = await asyncio.to_thread(_get_engine)
        frame_width = _frame_width(frames[0])
        frame_results = await asyncio.to_thread(
            _ocr_frames, engine, frames, frame_width
        )
        return build_subtitle_text(frame_results)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(f"硬字幕 OCR 失败（回退音频转录）: {e}")
        return ""
    finally:
        shutil.rmtree(frames_dir, ignore_errors=True)
