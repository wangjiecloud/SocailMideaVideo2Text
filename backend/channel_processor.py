"""
抖音博主批量转录编排。

输入博主主页 URL，解析其全部视频列表，逐个「下载音频 → Whisper 转录」，
按视频发布时间分节拼成一份合集文稿，最后对整份文稿做一次 LLM 摘要。

为什么不做逐视频的 LLM 优化/翻译：
一个博主可能有几百个视频，逐视频跑「优化 + 翻译」的成本与耗时都不现实；
合集的价值在于「全量、按时间线可检索」，因此只保留 Whisper 原文，再做
一次整体摘要。需要精修单个视频时走普通单视频流程即可。

无语音守卫与单视频流程一致：某个视频没检测到语音就如实标注，绝不把
占位文本喂给 LLM；整份合集一个有语音的视频都没有时直接跳过摘要。

进度约定（Web/CLI/MCP 三个入口共用同一套映射）：
- 5%   解析博主主页
- 8%   拿到视频列表
- 10-85% 逐视频下载/转录（按完成数线性插值，消息形如「正在转录第 i/n 个视频」）
- 88%  合集文稿组装完成
- 90%+ LLM 摘要（调用方收尾时置 100）
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from douyin import douyin_cookie, fetch_profile_videos
from pipeline import strip_transcript_meta, transcribed_speech

logger = logging.getLogger(__name__)


class ChannelProcessor:
    """博主批量转录编排器。不依赖 FastAPI，进度通过回调上报，三个入口共用。"""

    # 单视频下载+转录的最大尝试次数（抖音下载偶发失败，重试一次）
    VIDEO_ATTEMPTS = 2

    def __init__(self, video_processor, transcriber):
        self.video_processor = video_processor
        self.transcriber = transcriber

    async def process_creator(
        self,
        url: str,
        summarizer,
        summary_language: str,
        work_dir: Path,
        on_progress: Optional[Callable] = None,
        no_llm: bool = False,
        max_videos: Optional[int] = None,
    ) -> dict:
        """批量转录一个抖音博主的全部视频。

        Args:
            url: 博主主页 URL（douyin.com/user/<sec_uid>）
            summarizer: 请求级 Summarizer（None 时跳过摘要）
            summary_language: 摘要语言代码
            work_dir: 临时文件父目录（内部会再建独占子目录并负责清理）
            on_progress: 进度回调 (progress:int, message:str)，同步或协程函数均可
            no_llm: 跳过摘要，只输出合集转录文本
            max_videos: 上限保护；None 表示解析到的全部视频

        Returns:
            见模块 docstring 的结果字段；所有视频都失败时抛异常。
        """
        await self._report(on_progress, 5, "正在解析博主主页...")

        cookie = douyin_cookie()
        nickname, videos = await fetch_profile_videos(
            url, cookie=cookie, max_videos=max_videos
        )
        total = len(videos)
        title = f"{nickname}的视频合集" if nickname else "抖音博主视频合集"
        await self._report(
            on_progress,
            8,
            f"博主「{nickname or '未知'}」共 {total} 个视频，开始逐个转录...",
        )

        # 独占子目录：无论成功、失败还是取消，finally 里整体清理
        owned_dir = work_dir / f"channel_{uuid.uuid4().hex[:8]}"
        owned_dir.mkdir(parents=True)

        sections: list[str] = []
        failed: list[dict] = []
        lang_counter: dict[str, int] = {}
        ok_count = 0
        no_speech_count = 0

        try:
            for index, video in enumerate(videos, start=1):
                base = 10 + round(75 * (index - 1) / max(total, 1))
                display_title = (video.get("title") or "").strip() or f"视频{index}"
                await self._report(
                    on_progress,
                    base,
                    f"正在转录第 {index}/{total} 个视频：{display_title[:50]}",
                )

                try:
                    raw_script = await self._transcribe_one(
                        video, index, owned_dir, cookie
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"第 {index} 个视频「{display_title}」处理失败: {e}")
                    failed.append(
                        {"index": index, "title": display_title, "error": str(e)[:200]}
                    )
                    continue

                lang = (
                    getattr(self.transcriber, "last_detected_language", None) or ""
                ).strip()
                if lang:
                    lang_counter[lang] = lang_counter.get(lang, 0) + 1

                # 无语音守卫：如实标注，绝不把占位文本当正文交给 LLM
                if not transcribed_speech(raw_script):
                    no_speech_count += 1
                    body = "_该视频未检测到语音，无转录内容_"
                    logger.info(f"第 {index}/{total} 个视频未检测到语音")
                else:
                    ok_count += 1
                    body = strip_transcript_meta(raw_script)

                sections.append(self._build_section(index, video, body))
        finally:
            shutil.rmtree(owned_dir, ignore_errors=True)

        if not sections:
            raise Exception(
                f"博主「{nickname or '未知'}」的 {total} 个视频全部处理失败，"
                f"无法生成合集。首个错误: {failed[0]['error'] if failed else '未知'}"
            )

        combined = "\n\n".join(sections)
        script = f"# {title}\n\n{combined}\n\nsource: {url}\n"
        await self._report(on_progress, 88, "合集文稿已生成，正在收尾...")

        detected_language = (
            max(lang_counter, key=lang_counter.get) if lang_counter else ""
        )

        summary = ""
        if no_llm:
            logger.info("no_llm：跳过合集摘要")
        elif ok_count == 0:
            # 整份合集没有任何语音，摘要会让 LLM 对着占位文本编造内容
            logger.warning("所有视频均未检测到语音，跳过合集摘要")
        elif summarizer is None:
            logger.info("未提供 summarizer，跳过合集摘要")
        else:
            await self._report(
                on_progress,
                90,
                "正在生成合集摘要（视频较多时可能需要几分钟）...",
            )
            try:
                summary = await summarizer.summarize(combined, summary_language, title)
            except Exception as e:
                # 摘要只是附加产物，失败不能拖垮已完成的转录合集
                logger.error(f"合集摘要生成失败: {e}")

        return {
            "nickname": nickname,
            "title": title,
            "script": script,
            "summary": summary,
            "video_count": total,
            "ok_count": ok_count,
            "no_speech_count": no_speech_count,
            "failed_count": len(failed),
            "failed": failed,
            "detected_language": detected_language,
            "no_speech": ok_count == 0 and no_speech_count > 0,
        }

    async def _transcribe_one(
        self, video: dict, index: int, work_dir: Path, cookie: str
    ) -> str:
        """下载并转录单个视频，返回原始转录 Markdown；失败抛异常。

        音频临时文件在 finally 里逐个清理，避免几百个视频的博主把磁盘吃满。
        """
        video_url = f"https://www.douyin.com/video/{video['id']}"
        stem = f"audio_{index:04d}"
        last_error: Optional[Exception] = None

        for attempt in range(1, self.VIDEO_ATTEMPTS + 1):
            try:
                audio_path = await self.video_processor.download_video_audio(
                    video_url, work_dir, stem, cookie=cookie or None
                )
                return await self.transcriber.transcribe(audio_path)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                last_error = e
                if attempt < self.VIDEO_ATTEMPTS:
                    await asyncio.sleep(1)
            finally:
                for leftover in work_dir.glob(f"{stem}.*"):
                    try:
                        leftover.unlink(missing_ok=True)
                    except Exception:
                        pass

        raise Exception(f"下载/转录失败: {last_error}")

    @staticmethod
    def _build_section(index: int, video: dict, body: str) -> str:
        """单视频小节：## [发布时间] 标题 + 正文；无发布时间时退化为序号。"""
        title = (video.get("title") or "").strip() or f"视频{index}"
        create_time = video.get("create_time")
        if create_time:
            try:
                stamp = datetime.fromtimestamp(int(create_time)).strftime(
                    "%Y-%m-%d %H:%M"
                )
                header = f"## [{stamp}] {title}"
            except (TypeError, ValueError, OSError, OverflowError):
                header = f"## {index}. {title}"
        else:
            header = f"## {index}. {title}"
        return f"{header}\n\n{body}"

    @staticmethod
    async def _report(on_progress, progress: int, message: str) -> None:
        """上报进度；回调可以是同步函数或协程函数，None 表示不上报。"""
        if on_progress is None:
            return
        result = on_progress(progress, message)
        if asyncio.iscoroutine(result):
            await result
