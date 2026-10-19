"""
抖音博主批量转录编排。

输入博主主页 URL，解析其全部视频列表，逐个「取字幕（有则免转录）→ 下载音频
→ Whisper 转录」，按视频发布时间分节拼成一份合集文稿，最后对整份文稿做一次
LLM 摘要。

单个视频的处理链（优先级从高到低）：
1. cla_info 字幕：列表接口暴露了字幕直链时直接下载文本（WebVTT/utterances
   JSON），解析成与 Whisper 输出同构的 Markdown——零下载音频、零转录开销。
2. 低码率直链：列表接口 video.bit_rate 里码率最低的 CDN 直链（约 250-650kbps），
   流式下载 mp4 后 ffmpeg 抽取音轨再转录，比 yt-dlp 稳定且省 4 倍以上带宽。
3. 分享页兜底：m.douyin.com/share/video/<id> 纯 HTTP 解析出 playwm 播放直链
   （302 到 CDN mp4），顺带补齐缺失的标题/发布时间；PC 端直链被风控或过期时
   的备用通道。
4. yt-dlp：最后的兜底（抖音 PC 端目前被签名墙挡住，基本走不到，留作抖音策略
   放松时的免费恢复路径）。

图文作品（图集）没有音轨，直接跳过转录并在合集中如实标注，不占成功/失败计数。

为什么不做逐视频的 LLM 优化/翻译：
一个博主可能有几百个视频，逐视频跑「优化 + 翻译」的成本与耗时都不现实；
合集的价值在于「全量、按时间线可检索」，因此只保留原文（字幕或 Whisper），
再做一次整体摘要。需要精修单个视频时走普通单视频流程即可。

无语音守卫与单视频流程一致：某个视频没检测到语音就如实标注，绝不把
占位文本喂给 LLM；整份合集一个有语音的视频都没有时直接跳过摘要。

断点续转与暂停：逐视频的处理结果通过 on_video_record 交给调用方持久化
（progress_store），重跑同一博主时经 resume_sections 复用已完成的视频小节、
只补剩余部分；pause_check 在两个视频之间生效，绝不打断进行中的视频。
暂停/中断的合集没有最终产物，已完成的进度全在续转进度文件里。

进度约定（Web/CLI/MCP 三个入口共用同一套映射）：
- 5%   解析博主主页
- 8%   拿到视频列表
- 10-85% 逐视频取字幕/下载/转录（按完成数线性插值，消息形如「正在转录第 i/n 个视频」）
- 88%  合集文稿组装完成
- 90%+ LLM 摘要（调用方收尾时置 100）
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from douyin import (
    douyin_cookie,
    download_media,
    fetch_profile_videos,
    fetch_share_video_info,
    fetch_text,
)
from pipeline import (
    format_subtitle_markdown,
    merge_transcript_text,
    transcribed_speech,
)
import subtitle_ocr

logger = logging.getLogger(__name__)

# 单视频转录来源 → 页面日志里展示的标签
SOURCE_LABELS = {
    "caption": "cla_info字幕",
    "direct": "低码率直链+Whisper",
    "direct_ocr": "低码率直链+硬字幕OCR",
    "share": "分享页直链+Whisper",
    "share_ocr": "分享页直链+硬字幕OCR",
    "ytdlp": "yt-dlp+Whisper",
}


class _ShareImagePost(Exception):
    """列表数据说是视频、分享页说是图文：按图文作品跳过，不计失败。

    图集的 aweme_type 变体不止 150 一种，列表侧检测漏掉时由分享页兜底纠正。
    """


class _TranscribeFailed(Exception):
    """媒体已成功下载并规范化、转录本身失败。

    换渠道重新下载同一个视频没有意义（浪费带宽），直接上抛走重试。
    """


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
        resume_sections: Optional[dict] = None,
        pause_check: Optional[Callable] = None,
        on_video_record: Optional[Callable] = None,
        on_list: Optional[Callable] = None,
        on_log: Optional[Callable] = None,
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
            resume_sections: 断点续转——aweme_id → 小节记录（status/section/
                create_time 等，来自 progress_store），其中最终状态
                （ok/no_speech/image）的视频直接复用、不再重跑
            pause_check: 无参 callable，返回真值时在当前视频完成后停止循环
                （暂停语义：绝不打断进行中的视频，进度已通过 on_video_record 落盘）
            on_video_record: 单视频处理完成回调（含失败/图文），参数为记录
                dict（id/status/section/title/create_time/index/lang/error），
                调用方据此持久化续转进度
            on_list: 拿到视频列表后回调 (nickname, total)，调用方可提前落盘
                列表信息（服务重启后未完成任务面板仍能显示 N/M 进度）
            on_log: 实时日志回调 (line:str)，同步或协程函数均可；逐视频的
                开始/结果/失败原因等细节通过它推到页面

        Returns:
            见模块 docstring 的结果字段；所有视频都失败时抛异常。
            请求暂停时返回 ``paused: True``（无 script/summary，已完成的
            小节已全部通过 on_video_record 交给调用方持久化）。
        """
        await self._report(on_progress, 5, "正在解析博主主页...")

        cookie = douyin_cookie()
        nickname, videos = await fetch_profile_videos(
            url, cookie=cookie, max_videos=max_videos
        )
        total = len(videos)
        title = f"{nickname}的视频合集" if nickname else "抖音博主视频合集"
        if on_list is not None:
            try:
                on_list(nickname, total)
            except Exception as e:
                logger.warning(f"on_list 回调失败（忽略）: {e}")
        await self._report(
            on_progress,
            8,
            f"博主「{nickname or '未知'}」共 {total} 个视频，开始逐个转录...",
        )
        await self._notify(
            on_log, f"博主「{nickname or '未知'}」共 {total} 个视频，开始逐个转录"
        )

        # 独占子目录：无论成功、失败还是取消，finally 里整体清理
        owned_dir = work_dir / f"channel_{uuid.uuid4().hex[:8]}"
        owned_dir.mkdir(parents=True)

        # 续转预置：最终状态的视频直接复用小节并计入统计
        resumed: dict[str, dict] = {}
        records: list[dict] = []
        failed: list[dict] = []
        lang_counter: dict[str, int] = {}
        ok_count = 0
        no_speech_count = 0
        image_count = 0
        for vid, rec in (resume_sections or {}).items():
            if not isinstance(rec, dict) or not rec.get("section"):
                continue
            resumed[str(vid)] = rec
            records.append(rec)
            status = rec.get("status")
            if status == "ok":
                ok_count += 1
                lang = (rec.get("lang") or "").strip()
                if lang:
                    lang_counter[lang] = lang_counter.get(lang, 0) + 1
            elif status == "no_speech":
                no_speech_count += 1
            elif status == "image":
                image_count += 1
        if resumed:
            await self._report(
                on_progress,
                8,
                f"检测到未完成的合集进度，已续转 {len(resumed)} 个视频，继续处理剩余视频...",
            )
            await self._notify(
                on_log, f"检测到断点进度，复用 {len(resumed)} 个已完成的视频"
            )

        paused = False
        try:
            for index, video in enumerate(videos, start=1):
                vid = str(video.get("id") or "")
                if vid and vid in resumed:
                    continue  # 上次已完成，直接跳过

                # 暂停检查放在两个视频之间：进行中的视频不会被中断
                if pause_check is not None:
                    try:
                        should_pause = bool(pause_check())
                    except Exception:
                        should_pause = False
                    if should_pause:
                        paused = True
                        logger.info(
                            f"任务暂停请求生效：已完成至第 {index - 1}/{total} 个视频"
                        )
                        break

                base = 10 + round(75 * (index - 1) / max(total, 1))
                display_title = (video.get("title") or "").strip() or f"视频{index}"
                await self._report(
                    on_progress,
                    base,
                    f"正在转录第 {index}/{total} 个视频：{display_title[:50]}",
                )

                record = None
                source = ""
                # 图文作品没有音轨，跳过转录但保留小节占位，保证时间线完整
                if video.get("is_image"):
                    image_count += 1
                    logger.info(f"第 {index}/{total} 个视频为图文作品，跳过转录")
                    await self._notify(
                        on_log,
                        f"[{index}/{total}] 图文作品，跳过：{display_title[:50]}",
                    )
                    record = {
                        "id": vid,
                        "status": "image",
                        "section": self._build_section(
                            index, video, "_图文作品，无音轨_"
                        ),
                        "title": display_title,
                        "create_time": video.get("create_time"),
                        "index": index,
                        "lang": None,
                        "error": None,
                    }
                else:
                    try:
                        raw_script, lang, source = await self._transcribe_one(
                            video, index, owned_dir, cookie
                        )
                    except asyncio.CancelledError:
                        raise
                    except _ShareImagePost:
                        # 列表漏判的图集：分享页纠正为图文，按跳过处理
                        image_count += 1
                        logger.info(
                            f"第 {index}/{total} 个视频经分享页确认为图文作品，跳过转录"
                        )
                        await self._notify(
                            on_log,
                            f"[{index}/{total}] 经分享页确认为图文作品，跳过：{display_title[:50]}",
                        )
                        record = {
                            "id": vid,
                            "status": "image",
                            "section": self._build_section(
                                index, video, "_图文作品，无音轨_"
                            ),
                            "title": display_title,
                            "create_time": video.get("create_time"),
                            "index": index,
                            "lang": None,
                            "error": None,
                        }
                    except Exception as e:
                        logger.warning(
                            f"第 {index} 个视频「{display_title}」处理失败: {e}"
                        )
                        await self._notify(
                            on_log,
                            f"[{index}/{total}] 失败：{display_title[:50]} — {str(e)[:120]}",
                        )
                        failed.append(
                            {
                                "index": index,
                                "title": display_title,
                                "error": str(e)[:200],
                            }
                        )
                        record = {
                            "id": vid,
                            "status": "failed",
                            "section": None,
                            "title": display_title,
                            "create_time": video.get("create_time"),
                            "index": index,
                            "lang": None,
                            "error": str(e)[:200],
                        }
                    else:
                        if lang:
                            lang_counter[lang] = lang_counter.get(lang, 0) + 1

                        # 无语音守卫：如实标注，绝不把占位文本当正文交给 LLM
                        if not transcribed_speech(raw_script):
                            no_speech_count += 1
                            body = "_该视频未检测到语音，无转录内容_"
                            logger.info(f"第 {index}/{total} 个视频未检测到语音")
                            await self._notify(
                                on_log,
                                f"[{index}/{total}] 未检测到语音：{display_title[:50]}",
                            )
                            status = "no_speech"
                        else:
                            ok_count += 1
                            body = merge_transcript_text(raw_script)
                            status = "ok"
                            await self._notify(
                                on_log,
                                f"[{index}/{total}] 转录成功（{SOURCE_LABELS.get(source, source)}）：{display_title[:50]}",
                            )
                        record = {
                            "id": vid,
                            "status": status,
                            "section": self._build_section(index, video, body),
                            "title": display_title,
                            "create_time": video.get("create_time"),
                            "index": index,
                            "lang": lang or None,
                            "error": None,
                        }

                if record is None:  # 理论不可达，防御
                    continue
                # 失败视频没有小节，不参与合集组装，但仍通过回调落盘（续转时要重跑）
                if record.get("section"):
                    records.append(record)
                if on_video_record is not None:
                    try:
                        on_video_record(record)
                    except Exception as e:
                        logger.warning(f"on_video_record 回调失败（忽略）: {e}")
        finally:
            shutil.rmtree(owned_dir, ignore_errors=True)

        done_count = ok_count + no_speech_count + image_count

        if paused:
            return {
                "nickname": nickname,
                "title": title,
                "paused": True,
                "script": "",
                "summary": "",
                "video_count": total,
                "done_count": done_count,
                "ok_count": ok_count,
                "no_speech_count": no_speech_count,
                "image_count": image_count,
                "failed_count": len(failed),
                "failed": failed,
                "detected_language": "",
                "no_speech": False,
            }

        if not records:
            raise Exception(
                f"博主「{nickname or '未知'}」的 {total} 个视频全部处理失败，"
                f"无法生成合集。首个错误: {failed[0]['error'] if failed else '未知'}"
            )

        # 按发布时间倒序组装（与列表接口的新视频在前一致）；无发布时间的排最后。
        # 续转场景下博主可能在两次运行之间发了新视频，按时间排序才能正确归位。
        records.sort(key=lambda r: -self._safe_create_time(r.get("create_time")))
        sections = [r["section"] for r in records]
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
            "paused": False,
            "script": script,
            "summary": summary,
            "video_count": total,
            "done_count": done_count,
            "ok_count": ok_count,
            "no_speech_count": no_speech_count,
            "image_count": image_count,
            "failed_count": len(failed),
            "failed": failed,
            "detected_language": detected_language,
            "no_speech": ok_count == 0 and no_speech_count > 0,
        }

    async def _transcribe_one(
        self, video: dict, index: int, work_dir: Path, cookie: str
    ) -> tuple[str, str, str]:
        """处理单个视频，返回 (原始转录 Markdown, 语言代码, 来源)；失败抛异常。

        语言代码：字幕路径取 caption_lang，音频路径取 Whisper 检测结果——
        不能在调用方读 transcriber.last_detected_language，字幕路径不经过
        transcriber，读到的是上一个视频的残留值。
        来源：caption/direct/share/ytdlp（SOURCE_LABELS 的键），用于页面日志
        展示这条转录走的是哪条链路。
        音频/字幕临时文件在 finally 里按 stem 前缀清理，避免几百个视频的
        博主把磁盘吃满。
        """
        stem = f"audio_{index:04d}"
        last_error: Optional[Exception] = None

        for attempt in range(1, self.VIDEO_ATTEMPTS + 1):
            try:
                return await self._process_video_once(video, work_dir, stem, cookie)
            except asyncio.CancelledError:
                raise
            except _ShareImagePost:
                raise  # 图文判定是确定性的，重试没有意义
            except Exception as e:
                last_error = e
                if attempt < self.VIDEO_ATTEMPTS:
                    await asyncio.sleep(1)
            finally:
                for pattern in (f"{stem}.*", f"norm_{stem}.*"):
                    for leftover in work_dir.glob(pattern):
                        try:
                            leftover.unlink(missing_ok=True)
                        except Exception:
                            pass

        raise Exception(f"下载/转录失败: {last_error}")

    async def _process_video_once(
        self, video: dict, work_dir: Path, stem: str, cookie: str
    ) -> tuple[str, str, str]:
        """单视频处理链的一次完整尝试：字幕 → 低码率直链 → 分享页 → yt-dlp。

        返回 (原始转录 Markdown, 语言代码, 来源)。
        """
        # 1) cla_info 字幕优先：能直接拿文本就不下载音频
        caption_result = await self._fetch_caption_markdown(
            video, work_dir, stem, cookie
        )
        if caption_result is not None:
            script, lang = caption_result
            return script, lang, "caption"

        # 2) 列表接口给的低码率 CDN 直链（约 6 小时时效，列表解析后立刻用没问题）
        low_gear_url = (video.get("low_gear_url") or "").strip()
        if low_gear_url:
            try:
                script, lang, ocr_used = await self._download_and_transcribe(
                    low_gear_url, work_dir, stem, cookie
                )
                return script, lang, "direct_ocr" if ocr_used else "direct"
            except asyncio.CancelledError:
                raise
            except _TranscribeFailed:
                raise
            except Exception as e:
                logger.warning(f"低码率直链下载失败，转分享页兜底: {e}")

        # 3) 分享页兜底：拿 playwm 直链，顺带补齐标题/发布时间
        try:
            share = await fetch_share_video_info(video["id"], cookie)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # 分享页失败不拖垮该视频：跳过直链下载，直接落 yt-dlp
            logger.warning(f"分享页解析失败，尝试 yt-dlp: {e}")
            share = None

        if share:
            self._merge_share_info(video, share)
            if share.get("is_image"):
                raise _ShareImagePost(video["id"])
            for play_url in share.get("play_urls") or []:
                try:
                    script, lang, ocr_used = await self._download_and_transcribe(
                        play_url, work_dir, stem, cookie
                    )
                    return script, lang, "share_ocr" if ocr_used else "share"
                except asyncio.CancelledError:
                    raise
                except _TranscribeFailed:
                    raise
                except Exception as e:
                    logger.warning(f"playwm 直链下载失败，尝试下一个: {e}")

        # 4) yt-dlp 最后兜底
        video_url = f"https://www.douyin.com/video/{video['id']}"
        audio_path = await self.video_processor.download_video_audio(
            video_url, work_dir, stem, cookie=cookie or None
        )
        script = await self.transcriber.transcribe(audio_path)
        lang = (getattr(self.transcriber, "last_detected_language", None) or "").strip()
        return script, lang, "ytdlp"

    async def _fetch_caption_markdown(
        self, video: dict, work_dir: Path, stem: str, cookie: str
    ) -> Optional[tuple[str, str]]:
        """下载并解析 cla_info 字幕，返回 (Markdown, 语言代码)；无字幕/失败返回 None。

        失败（下载异常/解析为空）不抛异常：字幕只是加速通道，失败后调用方
        自然落入音频下载链路。
        """
        caption_url = (video.get("caption_url") or "").strip()
        if not caption_url:
            return None

        try:
            content = await fetch_text(caption_url, cookie=cookie)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"字幕下载失败，回退音频链路: {e}")
            return None

        if not content or not content.strip():
            return None

        caption_format = (video.get("caption_format") or "").lower()
        try:
            if "json" in caption_format or content.lstrip().startswith("{"):
                entries = self._parse_caption_json(content)
            else:
                # WebVTT：复用 video_processor 的解析器（含滚动追加去重），
                # 落盘成 stem 前缀文件，由 _transcribe_one 的清理逻辑统一回收
                vtt_path = work_dir / f"{stem}.vtt"
                vtt_path.write_text(content, encoding="utf-8")
                entries = self.video_processor._parse_vtt(str(vtt_path))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"字幕解析失败，回退音频链路: {e}")
            return None

        if not entries:
            return None

        caption_lang = (video.get("caption_lang") or "").strip()
        logger.info(f"视频 {video['id']} 命中字幕直取（{len(entries)} 条目）")
        return format_subtitle_markdown(entries, caption_lang or "zh"), caption_lang

    async def _download_and_transcribe(
        self, url: str, work_dir: Path, stem: str, cookie: str
    ) -> tuple[str, str, bool]:
        """流式下载媒体 → 硬字幕 OCR → ffmpeg 抽取 16k 单声道 m4a → Whisper。

        画面里烧了字幕时 OCR 直接给出创作者原文，跳过音频规范化与 Whisper
        （快语速内容的同音字错认远多于 OCR）；OCR 未命中再走音频链路。

        返回 (原始转录 Markdown, 语言代码, 是否走了 OCR)。
        下载/规范化失败抛普通异常（调用方可换渠道重下）；转录失败抛
        _TranscribeFailed（换渠道重新下载没有意义，直接走重试）。
        """
        media_path = work_dir / f"{stem}.mp4"
        try:
            await download_media(url, media_path, cookie=cookie)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            raise Exception(f"媒体下载/规范化失败: {e}") from e

        # 硬字幕 OCR：mp4 已在本地，先试画面字幕；未命中返回空串继续走音频
        ocr_text = await subtitle_ocr.extract_hard_subtitles(media_path, work_dir)
        if ocr_text:
            logger.info(f"硬字幕 OCR 命中（{len(ocr_text)} 字符），跳过 Whisper")
            return ocr_text, "", True

        try:
            audio_path = await self.video_processor.normalize_local_media_to_m4a(
                media_path, work_dir, out_name=f"norm_{stem}"
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            raise Exception(f"媒体下载/规范化失败: {e}") from e

        try:
            script = await self.transcriber.transcribe(audio_path)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            raise _TranscribeFailed(f"转录失败: {e}") from e

        lang = (getattr(self.transcriber, "last_detected_language", None) or "").strip()
        return script, lang, False

    @staticmethod
    def _merge_share_info(video: dict, share: dict) -> None:
        """用分享页信息补齐列表数据缺失的标题/发布时间（就地更新 video）。"""
        if not isinstance(share, dict):
            return
        if share.get("title") and not (video.get("title") or "").strip():
            video["title"] = share["title"]
        if share.get("create_time") and not video.get("create_time"):
            video["create_time"] = share["create_time"]

    @staticmethod
    def _parse_caption_json(content: str) -> list:
        """解析抖音 utterances JSON 字幕：{"utterances": [{text, start_time, end_time}]}。"""
        try:
            data = json.loads(content)
        except ValueError:
            return []
        utterances = data.get("utterances") if isinstance(data, dict) else None
        if not isinstance(utterances, list):
            return []
        entries = []
        for item in utterances:
            if not isinstance(item, dict):
                continue
            text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
            if not text:
                continue
            entries.append(
                {
                    "start": ChannelProcessor._fmt_seconds(item.get("start_time")),
                    "end": ChannelProcessor._fmt_seconds(item.get("end_time")),
                    "text": text,
                }
            )
        return entries

    @staticmethod
    def _fmt_seconds(value) -> str:
        """把秒数（str/float/int）格式化为 MM:SS。"""
        try:
            seconds = max(0, int(float(value)))
        except (TypeError, ValueError):
            return "00:00"
        return f"{seconds // 60:02d}:{seconds % 60:02d}"

    @staticmethod
    def _safe_create_time(value) -> int:
        """发布时间戳的安全转换；缺失/非法返回 0（组装时排最后）。"""
        try:
            return int(value)
        except (TypeError, ValueError, OSError, OverflowError):
            return 0

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

    @staticmethod
    async def _notify(on_log, line: str) -> None:
        """推送实时日志；回调可以是同步函数或协程函数，None 表示不推送。"""
        if on_log is None:
            return
        result = on_log(line)
        if asyncio.iscoroutine(result):
            await result
