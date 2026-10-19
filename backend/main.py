from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import os
from pathlib import Path

# 加载项目根目录的 .env（backend/ 子目录启动时 start.py 的 load_dotenv 不会覆盖这里）
try:
    from dotenv import load_dotenv as _load_dotenv

    _load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)
except Exception:
    pass

import asyncio
import logging
import threading
import time
from contextlib import asynccontextmanager
from typing import Optional
import aiofiles
import uuid
import json
import re
import openai

from video_processor import VideoProcessor
from transcriber import Transcriber
from summarizer import Summarizer
from translator import Translator
from channel_processor import ChannelProcessor
import model_registry
import progress_store
from pipeline import (
    MEDIA_MIME,
    NO_SPEECH_NOTICE,
    UPLOAD_ALLOWED_EXT,
    VIDEO_EXT,
    douyin_sec_uid as _douyin_sec_uid,
    is_douyin_profile_url as _is_douyin_profile_url,
    media_kind as _media_kind,
    sanitize_title_for_filename as _sanitize_title_for_filename,
    transcribed_speech as _transcribed_speech,
    txt_to_raw_transcript_markdown as _txt_to_raw_transcript_markdown,
)

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 关闭后仍未结束的后台线程，最多再等这么多秒就强制退出；None 表示不强制（测试用）
SHUTDOWN_FORCE_EXIT_SECONDS: Optional[float] = 3


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    # LLM 调用、Whisper 转录、yt-dlp 下载都跑在线程里，无法中断；Python 退出时会等它们跑完，
    # 热重载因此可能卡上几分钟。未完成的任务会在下次启动时被标记为中断。
    if SHUTDOWN_FORCE_EXIT_SECONDS is None:
        return

    def force_exit():
        time.sleep(SHUTDOWN_FORCE_EXIT_SECONDS)
        logger.warning("关闭超时，仍有后台线程未结束，强制退出进程")
        os._exit(0)

    threading.Thread(target=force_exit, daemon=True).start()


app = FastAPI(title="AI视频转录器", version="1.0.0", lifespan=lifespan)

# CORS中间件配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def revalidate_frontend(request, call_next):
    """页面与前端静态文件每次都向服务器确认（命中时仅返回 304）。

    不带 Cache-Control 时浏览器会按 Last-Modified 自行缓存数天，
    改了 app.js 后用户仍跑旧代码，而新的 index.html 已生效，两者对不上。
    """
    response = await call_next(request)
    path = request.url.path
    if (
        path == "/" or path.startswith("/static/")
    ) and "cache-control" not in response.headers:
        response.headers["Cache-Control"] = "no-cache"
    return response


# 获取项目根目录
PROJECT_ROOT = Path(__file__).parent.parent

# 挂载静态文件
app.mount("/static", StaticFiles(directory=str(PROJECT_ROOT / "static")), name="static")

# 创建临时目录
# TRANSCRIBER_TEMP_DIR 供测试隔离使用，避免读写真实的 temp/tasks.json
TEMP_DIR = Path(os.getenv("TRANSCRIBER_TEMP_DIR") or PROJECT_ROOT / "temp")
TEMP_DIR.mkdir(exist_ok=True)

# 初始化处理器
video_processor = VideoProcessor()
transcriber = Transcriber(model_size=os.getenv("WHISPER_MODEL_SIZE", "base"))
# LLM 默认走 litellm（ms.json 模型列表 + llm-config.json 凭据），配置缺失时回退环境变量
summarizer = Summarizer(**model_registry.resolve_llm_settings())
translator = Translator(**model_registry.resolve_llm_settings())

# 存储任务状态 - 使用文件持久化
TASKS_FILE = TEMP_DIR / "tasks.json"
tasks_lock = threading.Lock()


def load_tasks():
    """加载任务状态"""
    try:
        if TASKS_FILE.exists():
            with open(TASKS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except:
        pass
    return {}


def save_tasks(tasks_data):
    """保存任务状态"""
    try:
        with tasks_lock:
            with open(TASKS_FILE, "w", encoding="utf-8") as f:
                json.dump(tasks_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"保存任务状态失败: {e}")


async def broadcast_task_update(task_id: str, task_data: dict):
    """向所有连接的SSE客户端广播任务状态更新"""
    logger.info(
        f"广播任务更新: {task_id}, 状态: {task_data.get('status')}, 连接数: {len(sse_connections.get(task_id, []))}"
    )
    if task_id in sse_connections:
        connections_to_remove = []
        for queue in sse_connections[task_id]:
            try:
                await queue.put(json.dumps(task_data, ensure_ascii=False))
                logger.debug(f"消息已发送到队列: {task_id}")
            except Exception as e:
                logger.warning(f"发送消息到队列失败: {e}")
                connections_to_remove.append(queue)

        # 移除断开的连接
        for queue in connections_to_remove:
            sse_connections[task_id].remove(queue)

        # 如果没有连接了，清理该任务的连接列表
        if not sse_connections[task_id]:
            del sse_connections[task_id]


INTERRUPTED_MESSAGE = "服务重启，任务已中断，请重新提交"


def mark_interrupted_tasks(tasks_data: dict) -> int:
    """把上次进程遗留的 processing 任务标记为终态。

    进程重启后这些任务已无协程在跑，若保持 processing，前端订阅后会永远卡在旧进度。
    博主合集任务有逐视频的续转进度文件，标记为 paused 让前端显示为可续转，
    而不是不可恢复的失败。
    """
    count = 0
    for task in tasks_data.values():
        if task.get("status") == "processing":
            if task.get("channel"):
                task.update(
                    {
                        "status": "paused",
                        "error": None,
                        "message": "服务重启，任务已暂停，可续转继续",
                    }
                )
            else:
                task.update(
                    {
                        "status": "error",
                        "error": INTERRUPTED_MESSAGE,
                        "message": INTERRUPTED_MESSAGE,
                    }
                )
            count += 1
    return count


# 启动时加载任务状态
tasks = load_tasks()
if mark_interrupted_tasks(tasks):
    save_tasks(tasks)
# 存储正在处理的URL，防止重复处理
processing_urls = set()
# 存储活跃的任务对象，用于控制和取消
active_tasks = {}
# 存储SSE连接，用于实时推送状态更新
sse_connections = {}

# 任务的实时日志（内存态，不落盘）：task_id → [line, ...]
# 转换过程中的逐视频结果/失败原因通过 SSE 推到页面，重连时随首条消息回放
task_logs = {}
# 单任务缓存的日志行数上限，防止超长任务把内存和页面撑爆
MAX_TASK_LOG_LINES = 500


async def broadcast_task_log(task_id: str, line: str) -> None:
    """缓存并推送一条任务实时日志到所有 SSE 订阅者。"""
    logs = task_logs.setdefault(task_id, [])
    logs.append(line)
    if len(logs) > MAX_TASK_LOG_LINES:
        del logs[: len(logs) - MAX_TASK_LOG_LINES]

    if task_id not in sse_connections:
        return
    payload = json.dumps({"type": "log", "line": line}, ensure_ascii=False)
    for queue in list(sse_connections[task_id]):
        try:
            await queue.put(payload)
        except Exception as e:
            logger.warning(f"发送日志到队列失败: {e}")


SUPERSEDED_MESSAGE = "已被同一链接的新任务（不同模型或摘要语言）取代"


def task_settings(summary_language: str, model_id: str) -> dict:
    """决定处理结果的设置；同一链接只有这些都相同时才复用正在跑的任务。

    模型网关地址由 model_id 经 ms.json 解析得出，不参与比较。
    """
    return {
        "summary_language": (summary_language or "").strip(),
        "model_id": (model_id or "").strip(),
    }


def find_running_task(url: str) -> Optional[str]:
    """返回当前进程里确实还在执行、且链接相同的任务 ID。"""
    for tid, job in active_tasks.items():
        if not job.done() and tasks.get(tid, {}).get("url") == url:
            return tid
    return None


async def supersede_task(task_id: str) -> None:
    """取消旧任务并把它标记为失败，让仍在订阅它的页面收到终态。

    LLM 调用跑在线程里无法中途打断，协程会在当前这次调用返回后才真正退出。
    """
    job = active_tasks.pop(task_id, None)
    if job is not None and not job.done():
        job.cancel()
    tasks[task_id].update(
        {
            "status": "error",
            "error": SUPERSEDED_MESSAGE,
            "message": SUPERSEDED_MESSAGE,
        }
    )
    save_tasks(tasks)
    await broadcast_task_update(task_id, tasks[task_id])


# 本地上传：允许的类型与大小上限（MB），可用环境变量 UPLOAD_MAX_MB 调整
UPLOAD_MAX_MB = int(os.getenv("UPLOAD_MAX_MB", "200"))

# 原视频下载：清晰度上限，避免 4K 源拖慢任务并占满磁盘
VIDEO_MAX_HEIGHT = int(os.getenv("VIDEO_MAX_HEIGHT", "720"))


def _resolve_temp_file(filename: str, allowed_ext: frozenset) -> Path:
    """校验前端传入的文件名并解析为 temp 目录下的真实路径。"""
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="文件名格式无效")

    ext = Path(filename).suffix.lower()
    if ext not in allowed_ext:
        raise HTTPException(
            status_code=400, detail=f"不支持的文件类型: {ext or '(none)'}"
        )

    file_path = TEMP_DIR / filename
    # 二次防御：即使扩展名校验通过，也确认最终路径没跳出 temp
    if file_path.parent.resolve() != TEMP_DIR.resolve():
        raise HTTPException(status_code=400, detail="文件名格式无效")
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    return file_path


def _safe_download_name(raw: str, expected_ext: str) -> Optional[str]:
    """把前端建议的下载文件名清洗为安全值，扩展名强制与磁盘文件一致。"""
    if not raw:
        return None
    base = os.path.basename(raw.replace("\\", "/"))
    stem = _sanitize_title_for_filename(Path(base).stem)
    if not stem or stem == "untitled":
        return None
    return f"{stem}{expected_ext}"


async def _resolve_media(
    task_id: str,
    safe_title: str,
    media_task: Optional[asyncio.Task],
    media_filename: Optional[str],
    media_download_name: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    """
    收尾等待并行进行的原视频下载，返回 (media_filename, media_download_name)。

    重复 await 同一个已完成的 Task 是安全的（直接返回缓存结果），因此慢速路径
    提前 await 过也不影响这里再次等待。
    """
    if media_task is None:
        return media_filename, media_download_name

    if not media_task.done():
        tasks[task_id].update({"progress": 92, "message": "正在准备原视频..."})
        save_tasks(tasks)
        await broadcast_task_update(task_id, tasks[task_id])

    try:
        video_path, _ = await media_task
    except Exception as e:
        logger.warning(f"等待原视频下载失败: {e}")
        video_path = None

    if video_path:
        return Path(video_path).name, f"{safe_title}{Path(video_path).suffix}"
    return media_filename, media_download_name


def _media_result_fields(
    media_filename: Optional[str],
    media_download_name: Optional[str],
) -> dict:
    """把媒体文件信息整理成写入任务结果的字段；文件不存在时返回空 dict。"""
    if not media_filename:
        return {}

    media_path = TEMP_DIR / media_filename
    if not media_path.exists():
        logger.warning(f"媒体文件不存在，跳过原视频展示: {media_filename}")
        return {}

    return {
        "media_filename": media_filename,
        "media_download_name": media_download_name or media_filename,
        "media_kind": _media_kind(media_path.suffix.lower()),
        # 传字节数，由前端决定用 KB/MB/GB 展示
        "media_size_bytes": media_path.stat().st_size,
    }


async def _run_post_extract_pipeline(
    task_id: str,
    raw_script: str,
    video_title: str,
    source_ref: str,
    summary_language: str,
    request_summarizer: Summarizer,
    dedup_url: Optional[str] = None,
    api_key: str = "",
    model_base_url: str = "",
    model_id: str = "",
    media_task: Optional[asyncio.Task] = None,
    media_filename: Optional[str] = None,
    media_download_name: Optional[str] = None,
) -> None:
    """取得 raw_script 后的共用管线：归档、优化、翻译、摘要、附带原视频、广播。

    media_task: 并行进行中的原视频下载任务，会在收尾前 await（可能已完成）。
    media_filename / media_download_name: 已就位的媒体文件（如本地上传的原件）。
    """
    short_id = task_id.replace("-", "")[:6]
    safe_title = _sanitize_title_for_filename(video_title)

    try:
        raw_md_filename = f"raw_{safe_title}_{short_id}.md"
        raw_md_path = TEMP_DIR / raw_md_filename
        with open(raw_md_path, "w", encoding="utf-8") as f:
            f.write((raw_script or "") + f"\n\nsource: {source_ref}\n")
        tasks[task_id].update({"raw_script_file": raw_md_filename})
        save_tasks(tasks)
        await broadcast_task_update(task_id, tasks[task_id])
    except Exception as e:
        logger.error(f"保存原始转录Markdown失败: {e}")

    # ── 无语音短路 ────────────────────────────────────────────────────
    # 视频里没有人说话时，绝不能把空文稿交给 LLM：模型会编造出一整段与视频
    # 毫无关系的对话，再经翻译、摘要放大，用户完全无法分辨真假。
    if not _transcribed_speech(raw_script):
        logger.warning(
            f"任务 {task_id} 未检测到任何语音，跳过优化/翻译/摘要以避免 LLM 编造内容"
        )

        media_filename, media_download_name = await _resolve_media(
            task_id, safe_title, media_task, media_filename, media_download_name
        )

        detected_language = (
            transcriber.get_detected_language(raw_script) or ""
        ).strip()
        notice = NO_SPEECH_NOTICE
        script_with_title = f"# {video_title}\n\n{notice}\n\nsource: {source_ref}\n"

        script_filename = f"transcript_{safe_title}_{short_id}.md"
        async with aiofiles.open(
            TEMP_DIR / script_filename, "w", encoding="utf-8"
        ) as f:
            await f.write(script_with_title)

        task_result = {
            "status": "completed",
            "progress": 100,
            "message": "未检测到语音内容",
            "no_speech": True,
            "video_title": video_title,
            "script": script_with_title,
            "summary": "",
            "script_path": str(TEMP_DIR / script_filename),
            "short_id": short_id,
            "safe_title": safe_title,
            "detected_language": detected_language,
            "summary_language": summary_language,
        }
        task_result.update(_media_result_fields(media_filename, media_download_name))

        tasks[task_id].update(task_result)
        save_tasks(tasks)
        await broadcast_task_update(task_id, tasks[task_id])

        if dedup_url:
            processing_urls.discard(dedup_url)
        if task_id in active_tasks:
            del active_tasks[task_id]
        return

    tasks[task_id].update(
        {
            "progress": 55,
            "message": "正在优化转录文本...",
        }
    )
    save_tasks(tasks)
    await broadcast_task_update(task_id, tasks[task_id])

    script = await request_summarizer.optimize_transcript(raw_script)

    script_with_title = f"# {video_title}\n\n{script}\n\nsource: {source_ref}\n"

    detected_language = transcriber.get_detected_language(raw_script)
    detected_language = (detected_language or "").strip()
    if not detected_language:
        detected_language = translator.infer_language_code(raw_script)
    detected_language = (
        translator.normalize_lang_code(detected_language) or detected_language
    )

    logger.info(f"检测到的语言: {detected_language}, 摘要语言: {summary_language}")

    translation_content = None
    translation_filename = None
    translation_path = None

    # 与请求级 Summarizer 保持同一套 LLM 解析，避免优化/摘要用一个模型、翻译用另一个
    request_llm = model_registry.resolve_llm_settings(
        model=model_id, api_key=api_key, base_url=model_base_url
    )
    if request_llm:
        request_translator = Translator(**request_llm)
    else:
        request_translator = translator

    need_translation = translator.languages_differ_for_translation(
        detected_language, summary_language
    )

    if need_translation:
        logger.info(f"需要翻译: {detected_language} -> {summary_language}")
        tasks[task_id].update(
            {
                "progress": 70,
                "message": "正在生成翻译...",
            }
        )
        save_tasks(tasks)
        await broadcast_task_update(task_id, tasks[task_id])

        translation_content = await request_translator.translate_text(
            script, summary_language, detected_language
        )
        translation_with_title = (
            f"# {video_title}\n\n{translation_content}\n\nsource: {source_ref}\n"
        )
        translation_filename = f"translation_{safe_title}_{short_id}.md"
        translation_path = TEMP_DIR / translation_filename
        async with aiofiles.open(translation_path, "w", encoding="utf-8") as f:
            await f.write(translation_with_title)
    else:
        logger.info(
            f"不需要翻译: detected_language={detected_language}, summary_language={summary_language}, "
            f"need_translation={need_translation}"
        )

    tasks[task_id].update(
        {
            "progress": 80,
            "message": "正在生成摘要...",
        }
    )
    save_tasks(tasks)
    await broadcast_task_update(task_id, tasks[task_id])

    summary = await request_summarizer.summarize(script, summary_language, video_title)
    summary_with_source = summary + f"\n\nsource: {source_ref}\n"

    script_filename = f"transcript_{task_id}.md"
    script_path = TEMP_DIR / script_filename
    async with aiofiles.open(script_path, "w", encoding="utf-8") as f:
        await f.write(script_with_title)

    new_script_filename = f"transcript_{safe_title}_{short_id}.md"
    new_script_path = TEMP_DIR / new_script_filename
    try:
        if script_path.exists():
            script_path.rename(new_script_path)
            script_path = new_script_path
    except Exception:
        pass

    summary_filename = f"summary_{safe_title}_{short_id}.md"
    summary_path = TEMP_DIR / summary_filename
    async with aiofiles.open(summary_path, "w", encoding="utf-8") as f:
        await f.write(summary_with_source)

    # 下载与转录/摘要并行跑，到这里通常已经完成
    media_filename, media_download_name = await _resolve_media(
        task_id, safe_title, media_task, media_filename, media_download_name
    )

    task_result = {
        "status": "completed",
        "progress": 100,
        "message": "处理完成！",
        "video_title": video_title,
        "script": script_with_title,
        "summary": summary_with_source,
        "script_path": str(script_path),
        "summary_path": str(summary_path),
        "short_id": short_id,
        "safe_title": safe_title,
        "detected_language": detected_language,
        "summary_language": summary_language,
    }

    if translation_content and translation_path:
        task_result.update(
            {
                "translation": translation_with_title,
                "translation_path": str(translation_path),
                "translation_filename": translation_filename,
            }
        )

    task_result.update(_media_result_fields(media_filename, media_download_name))

    tasks[task_id].update(task_result)
    save_tasks(tasks)
    logger.info(f"任务完成，准备广播最终状态: {task_id}")
    await broadcast_task_update(task_id, tasks[task_id])
    logger.info(f"最终状态已广播: {task_id}")

    if dedup_url:
        processing_urls.discard(dedup_url)
    if task_id in active_tasks:
        del active_tasks[task_id]


@app.get("/")
async def read_root():
    """返回前端页面"""
    return FileResponse(str(PROJECT_ROOT / "static" / "index.html"))


@app.get("/api/litellm/models")
async def litellm_models():
    """返回 ms.json 中的模型列表（与 lunwenAgent 运行时共用同一份配置）。"""
    entries = model_registry.load_model_entries()
    default_model = model_registry.default_model_id()
    return {
        "default_model": default_model,
        "data": [
            {"id": e["model"], "name": e["name"], "base_url": e["base_url"]}
            for e in entries
        ],
    }


@app.post("/api/models")
async def list_models(
    base_url: str = Form(default=""),
    api_key: str = Form(default=""),
):
    """Proxy: fetch model list from any OpenAI-compatible API."""
    effective_key = api_key or os.getenv("OPENAI_API_KEY", "")
    effective_url = base_url.rstrip("/") or os.getenv("OPENAI_BASE_URL") or None

    if not effective_key:
        raise HTTPException(status_code=400, detail="API key is required")

    try:
        client = openai.OpenAI(api_key=effective_key, base_url=effective_url)
        resp = await asyncio.to_thread(client.models.list)
        models = [{"id": m.id, "name": getattr(m, "name", m.id)} for m in resp.data]
        # Sort by id for readability
        models.sort(key=lambda x: x["id"])
        return {"data": models}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


async def _enqueue_upload_job(
    file: UploadFile,
    summary_language: str,
    api_key: str,
    model_base_url: str,
    model_id: str,
) -> dict:
    """保存上传文件并入队 process_upload_task，返回 {task_id, message}。"""
    raw_name = file.filename or "upload.bin"
    if ".." in raw_name or "/" in raw_name or "\\" in raw_name:
        raise HTTPException(status_code=400, detail="Invalid filename")
    safe_name = os.path.basename(raw_name)
    ext = Path(safe_name).suffix.lower()
    if ext not in UPLOAD_ALLOWED_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {ext or '(none)'}",
        )

    max_bytes = UPLOAD_MAX_MB * 1024 * 1024
    task_id = str(uuid.uuid4())
    unique_stem = task_id.replace("-", "")[:12]
    dest = TEMP_DIR / f"upload_{unique_stem}{ext}"

    total = 0
    with open(dest, "wb") as out_f:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                try:
                    dest.unlink(missing_ok=True)
                except Exception:
                    pass
                raise HTTPException(
                    status_code=413,
                    detail=f"File exceeds limit of {UPLOAD_MAX_MB} MB",
                )
            out_f.write(chunk)

    if total == 0:
        try:
            dest.unlink(missing_ok=True)
        except Exception:
            pass
        raise HTTPException(status_code=400, detail="Empty file")

    video_title = _sanitize_title_for_filename(Path(safe_name).stem) or "upload"
    source_label = f"upload:{safe_name}"

    tasks[task_id] = {
        "status": "processing",
        "progress": 0,
        "message": "开始处理上传文件...",
        "script": None,
        "summary": None,
        "error": None,
        "url": source_label,
    }
    save_tasks(tasks)

    bg = asyncio.create_task(
        process_upload_task(
            task_id,
            dest,
            safe_name,
            video_title,
            ext,
            summary_language,
            api_key,
            model_base_url,
            model_id,
        )
    )
    active_tasks[task_id] = bg

    return {"task_id": task_id, "message": "任务已创建，正在处理中..."}


@app.post("/api/process-video")
async def process_video(
    url: str = Form(default=""),
    summary_language: str = Form(default="zh"),
    api_key: str = Form(default=""),
    model_base_url: str = Form(default=""),
    model_id: str = Form(default=""),
    download_video: str = Form(default="1"),
    file: Optional[UploadFile] = File(None),
):
    """
    处理视频链接或本地上传（multipart 中带 file 且无有效 URL 时走上传流程）。
    上传与 URL 共用此路径，便于反向代理只放行 /api/process-video 的环境。
    """
    want_video = str(download_video).strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
        "",
    )
    try:
        if file is not None and (file.filename or "").strip():
            return await _enqueue_upload_job(
                file, summary_language, api_key, model_base_url, model_id
            )

        stripped = (url or "").strip()
        if not stripped:
            raise HTTPException(
                status_code=400,
                detail="Provide a video URL or upload a file",
            )

        url = stripped
        # 抖音博主主页：走批量转录（解析全部视频 → 逐个转录 → 合集 + 摘要）
        is_channel = _is_douyin_profile_url(url)

        # 同一链接正在处理：设置相同则复用，换了模型或摘要语言则取消旧任务重新开始
        settings = task_settings(summary_language, model_id)
        running_id = find_running_task(url)
        if running_id:
            if tasks[running_id].get("settings") == settings:
                return {"task_id": running_id, "message": "该视频正在处理中，请等待..."}
            logger.info(f"链接 {url} 的设置已变更，取消旧任务 {running_id} 并新建任务")
            await supersede_task(running_id)

        # 生成唯一任务ID
        task_id = str(uuid.uuid4())

        # 标记URL为正在处理
        processing_urls.add(url)

        # 初始化任务状态
        tasks[task_id] = {
            "status": "processing",
            "progress": 0,
            "message": "开始解析抖音博主主页..." if is_channel else "开始处理视频...",
            "script": None,
            "summary": None,
            "error": None,
            "url": url,  # 保存URL用于去重
            "settings": settings,
            # 博主任务打标：重启后的遗留任务按 paused（可续转）而非 error 处理
            "channel": is_channel,
        }
        save_tasks(tasks)

        # 创建并跟踪异步任务
        if is_channel:
            task = asyncio.create_task(
                process_channel_task(
                    task_id,
                    url,
                    summary_language,
                    api_key,
                    model_base_url,
                    model_id,
                )
            )
        else:
            task = asyncio.create_task(
                process_video_task(
                    task_id,
                    url,
                    summary_language,
                    api_key,
                    model_base_url,
                    model_id,
                    want_video,
                )
            )
        active_tasks[task_id] = task

        return {"task_id": task_id, "message": "任务已创建，正在处理中..."}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"处理视频时出错: {str(e)}")
        raise HTTPException(status_code=500, detail=f"处理失败: {str(e)}")


async def process_video_task(
    task_id: str,
    url: str,
    summary_language: str,
    api_key: str = "",
    model_base_url: str = "",
    model_id: str = "",
    want_video: bool = True,
):
    """
    异步处理视频任务
    """
    media_task = None
    try:
        # ── 阶段一：优先尝试获取平台字幕（快速路径） ──────────────────────
        tasks[task_id].update(
            {"status": "processing", "progress": 10, "message": "正在检测视频字幕..."}
        )
        save_tasks(tasks)
        await broadcast_task_update(task_id, tasks[task_id])
        await asyncio.sleep(0.1)

        # 原视频下载与字幕/转录/摘要并行，收尾时才等它，尽量不增加总耗时
        if want_video:
            media_task = asyncio.create_task(
                video_processor.download_video(
                    url,
                    TEMP_DIR,
                    f"video_{task_id.replace('-', '')[:12]}",
                    VIDEO_MAX_HEIGHT,
                )
            )

        # 用户选了模型或提供了凭据：按 ms.json/litellm 解析后创建请求级 Summarizer
        request_llm = model_registry.resolve_llm_settings(
            model=model_id, api_key=api_key, base_url=model_base_url
        )
        if request_llm:
            request_summarizer = Summarizer(**request_llm)
            logger.info(
                f"使用请求级 LLM 配置: model={request_llm.get('model')}, "
                f"base_url={request_llm.get('base_url')}"
            )
        else:
            request_summarizer = summarizer  # 全局实例（litellm 默认/环境变量）

        subtitle_text, sub_title, sub_lang = await video_processor.fetch_subtitles(
            url, TEMP_DIR
        )

        if subtitle_text:
            # ── 快速路径：有字幕，跳过音频下载和 Whisper ──────────────────
            video_title = sub_title
            raw_script = subtitle_text
            # 把语言写入 transcriber，保持下游逻辑一致
            transcriber.last_detected_language = sub_lang

            tasks[task_id].update(
                {
                    "progress": 40,
                    "message": f"字幕获取成功（{sub_lang}），正在处理文本...",
                }
            )
            save_tasks(tasks)
            await broadcast_task_update(task_id, tasks[task_id])
        else:
            # ── 慢速路径：无字幕，下载音频 → Whisper 转录 ─────────────────
            tasks[task_id].update(
                {"progress": 15, "message": "未找到字幕，正在下载视频音频..."}
            )
            save_tasks(tasks)
            await broadcast_task_update(task_id, tasks[task_id])

            audio_path = None
            video_title = None

            # 已经在下载原视频时，直接从它抽音轨，避免把同一个视频下载两遍
            if media_task is not None:
                video_path, media_title = await media_task
                if video_path:
                    try:
                        audio_path = await video_processor.normalize_local_media_to_m4a(
                            Path(video_path), TEMP_DIR
                        )
                        video_title = media_title or sub_title or "unknown"
                        logger.info("复用原视频抽取音轨，跳过独立音频下载")
                    except Exception as e:
                        logger.warning(f"从原视频抽音轨失败，回退独立音频下载: {e}")
                        audio_path = None

            if audio_path is None:
                audio_path, video_title = await video_processor.download_and_convert(
                    url, TEMP_DIR, prefetched_title=sub_title or None
                )

            tasks[task_id].update(
                {"progress": 35, "message": "音频下载完成，准备转录..."}
            )
            save_tasks(tasks)
            await broadcast_task_update(task_id, tasks[task_id])

            tasks[task_id].update(
                {"progress": 40, "message": "正在转录音频（Whisper）..."}
            )
            save_tasks(tasks)
            await broadcast_task_update(task_id, tasks[task_id])

            raw_script = await transcriber.transcribe(audio_path)

        await _run_post_extract_pipeline(
            task_id=task_id,
            raw_script=raw_script,
            video_title=video_title,
            source_ref=url,
            summary_language=summary_language,
            request_summarizer=request_summarizer,
            dedup_url=url,
            api_key=api_key,
            model_base_url=model_base_url,
            model_id=model_id,
            media_task=media_task,
        )

        # 不要立即删除临时文件！保留给用户下载
        # 文件会在一定时间后自动清理或用户手动清理

    except asyncio.CancelledError:
        # 被新任务取代或被删除；CancelledError 不是 Exception，下面的分支接不住。
        # 状态与 active_tasks 已由取消方处理，这里只停掉原视频下载。
        logger.info(f"任务 {task_id} 已取消")
        if media_task is not None and not media_task.done():
            media_task.cancel()
        raise
    except Exception as e:
        logger.error(f"任务 {task_id} 处理失败: {str(e)}")
        # 主流程已失败，不要让原视频下载在后台继续跑
        if media_task is not None and not media_task.done():
            media_task.cancel()
        # 从处理列表中移除URL
        processing_urls.discard(url)

        # 从活跃任务列表中移除
        if task_id in active_tasks:
            del active_tasks[task_id]

        tasks[task_id].update(
            {"status": "error", "error": str(e), "message": f"处理失败: {str(e)}"}
        )
        save_tasks(tasks)
        await broadcast_task_update(task_id, tasks[task_id])


async def process_channel_task(
    task_id: str,
    url: str,
    summary_language: str,
    api_key: str = "",
    model_base_url: str = "",
    model_id: str = "",
):
    """
    抖音博主主页批量转录任务。

    解析博主全部视频 → 逐个「下载音频 → Whisper 转录」→ 按发布时间分节拼
    合集文稿 → 整体做一次 LLM 摘要。不做逐视频的优化/翻译（几百个视频的
    成本不现实），也不下载保留原视频（合集场景只有文本产物）。

    断点续转：逐视频结果写入 progress_store（按 sec_uid 独立文件），重启/
    暂停/中断后重新提交同一博主时自动复用已完成的视频小节，只补剩余部分。
    """
    sec_uid = _douyin_sec_uid(url) or ""
    progress_data = progress_store.load_progress(TEMP_DIR, sec_uid) if sec_uid else {}
    resume = progress_store.resume_sections(progress_data)
    if resume:
        logger.info(f"博主任务续转: {url} 已有 {len(resume)} 个视频的进度")

    try:
        # 与单视频任务同一套 LLM 解析：请求级模型优先，litellm 兜底
        request_llm = model_registry.resolve_llm_settings(
            model=model_id, api_key=api_key, base_url=model_base_url
        )
        if request_llm:
            request_summarizer = Summarizer(**request_llm)
            logger.info(
                f"博主任务使用请求级 LLM: model={request_llm.get('model')}, "
                f"base_url={request_llm.get('base_url')}"
            )
        else:
            request_summarizer = summarizer  # 全局实例（litellm 默认/环境变量）

        channel_processor = ChannelProcessor(video_processor, transcriber)

        async def on_progress(progress: int, message: str) -> None:
            tasks[task_id].update({"progress": progress, "message": message})
            save_tasks(tasks)
            await broadcast_task_update(task_id, tasks[task_id])

        async def on_log(line: str) -> None:
            await broadcast_task_log(task_id, line)

        def on_list(nickname: str, total: int) -> None:
            """拿到列表就落盘：服务此时被杀，未完成面板仍能显示 N/M 进度。"""
            if not sec_uid:
                return
            progress_data["nickname"] = nickname or ""
            progress_data["url"] = url
            progress_data["total"] = total
            progress_store.save_progress(TEMP_DIR, sec_uid, progress_data)

        def on_video_record(record: dict) -> None:
            if not sec_uid:
                return
            vid = str(record.get("id") or "")
            if not vid:
                return
            videos = progress_data.setdefault("videos", {})
            videos[vid] = {
                k: record.get(k)
                for k in (
                    "status",
                    "section",
                    "title",
                    "create_time",
                    "index",
                    "lang",
                    "error",
                )
            }
            progress_store.save_progress(TEMP_DIR, sec_uid, progress_data)

        def pause_check() -> bool:
            return bool(tasks.get(task_id, {}).get("pause_requested"))

        result = await channel_processor.process_creator(
            url=url,
            summarizer=request_summarizer,
            summary_language=summary_language,
            work_dir=TEMP_DIR,
            on_progress=on_progress,
            resume_sections=resume,
            pause_check=pause_check,
            on_video_record=on_video_record,
            on_list=on_list,
            on_log=on_log,
        )

        # 请求暂停：已完成的小节都在进度文件里，任务标记为 paused 等待续转
        if result.get("paused"):
            tasks[task_id].update(
                {
                    "status": "paused",
                    "pause_requested": False,
                    "message": (
                        f"已暂停：已完成 {result['done_count']}/{result['video_count']} 个视频，"
                        "可在下方「未完成的博主任务」中续转"
                    ),
                    "video_title": result["title"],
                    "video_count": result["video_count"],
                    "done_count": result["done_count"],
                    "ok_count": result["ok_count"],
                    "no_speech_count": result["no_speech_count"],
                    "image_count": result["image_count"],
                    "failed_count": result["failed_count"],
                }
            )
            save_tasks(tasks)
            await broadcast_task_update(task_id, tasks[task_id])
            logger.info(
                f"博主任务已暂停: {task_id}（{result['done_count']}/{result['video_count']}）"
            )
            return

        title = result["title"]
        safe_title = _sanitize_title_for_filename(title)
        short_id = task_id.replace("-", "")[:6]

        script_filename = f"transcript_{safe_title}_{short_id}.md"
        script_path = TEMP_DIR / script_filename
        async with aiofiles.open(script_path, "w", encoding="utf-8") as f:
            await f.write(result["script"])

        summary = result.get("summary") or ""
        summary_path = None
        if summary:
            summary_filename = f"summary_{safe_title}_{short_id}.md"
            summary_path = TEMP_DIR / summary_filename
            async with aiofiles.open(summary_path, "w", encoding="utf-8") as f:
                await f.write(summary + f"\n\nsource: {url}\n")

        done_message = (
            f"博主合集完成：共 {result['video_count']} 个视频"
            f"（成功 {result['ok_count']}，无语音 {result['no_speech_count']}，"
            f"图文 {result['image_count']}，失败 {result['failed_count']}）"
        )

        task_result = {
            "status": "completed",
            "progress": 100,
            "message": done_message,
            "video_title": title,
            "script": result["script"],
            "summary": summary,
            "script_path": str(script_path),
            "short_id": short_id,
            "safe_title": safe_title,
            "detected_language": result["detected_language"],
            "summary_language": summary_language,
            # 博主模式标记与统计，前端据此展示徽章与「第 x/n 个」进度
            "channel": True,
            "video_count": result["video_count"],
            "ok_count": result["ok_count"],
            "no_speech_count": result["no_speech_count"],
            "image_count": result["image_count"],
            "failed_count": result["failed_count"],
        }
        if summary_path is not None:
            task_result["summary_path"] = str(summary_path)
        if result["no_speech"]:
            task_result["no_speech"] = True
        if result["failed"]:
            task_result["failed_videos"] = result["failed"]

        tasks[task_id].update(task_result)
        # 合集已完成：移除同链接的旧 paused 任务记录（其进度已并入本任务结果）。
        # 续转进度文件保留下来作为增量缓存：再次提交同一博主只补新视频、
        # 重试失败视频；想全量重跑时删除本任务即可（删除任务会清理进度文件）
        for tid in [
            t
            for t, d in tasks.items()
            if t != task_id and d.get("url") == url and d.get("status") == "paused"
        ]:
            del tasks[tid]
        save_tasks(tasks)
        logger.info(f"博主任务完成，准备广播最终状态: {task_id}")
        await broadcast_task_update(task_id, tasks[task_id])

    except asyncio.CancelledError:
        # 被新任务取代或被删除；状态与 active_tasks 已由取消方处理。
        # 已完成视频的进度文件保留，续转仍可接着跑。
        logger.info(f"博主任务 {task_id} 已取消")
        raise
    except Exception as e:
        logger.error(f"博主任务 {task_id} 处理失败: {str(e)}")
        tasks[task_id].update(
            {"status": "error", "error": str(e), "message": f"处理失败: {str(e)}"}
        )
        save_tasks(tasks)
        await broadcast_task_update(task_id, tasks[task_id])
    finally:
        processing_urls.discard(url)
        if task_id in active_tasks:
            del active_tasks[task_id]


@app.post("/api/process-upload")
async def process_upload(
    file: UploadFile = File(...),
    summary_language: str = Form(default="zh"),
    api_key: str = Form(default=""),
    model_base_url: str = Form(default=""),
    model_id: str = Form(default=""),
):
    """独立上传入口；逻辑与 multipart 带 file 的 /api/process-video 相同。"""
    return await _enqueue_upload_job(
        file, summary_language, api_key, model_base_url, model_id
    )


async def process_upload_task(
    task_id: str,
    saved_path: Path,
    original_name: str,
    video_title: str,
    ext_lower: str,
    summary_language: str,
    api_key: str = "",
    model_base_url: str = "",
    model_id: str = "",
):
    source_ref = f"upload:{original_name}"
    try:
        request_llm = model_registry.resolve_llm_settings(
            model=model_id, api_key=api_key, base_url=model_base_url
        )
        if request_llm:
            request_summarizer = Summarizer(**request_llm)
            logger.info(
                f"上传任务使用请求级 LLM: model={request_llm.get('model')}, "
                f"base_url={request_llm.get('base_url')}"
            )
        else:
            request_summarizer = summarizer

        if ext_lower == ".txt":
            tasks[task_id].update(
                {
                    "progress": 20,
                    "message": "正在读取文本文件...",
                }
            )
            save_tasks(tasks)
            await broadcast_task_update(task_id, tasks[task_id])

            body = saved_path.read_text(encoding="utf-8", errors="replace")
            if not body.strip():
                raise Exception("文本文件为空")
            transcriber.last_detected_language = None
            raw_script = _txt_to_raw_transcript_markdown(body)
        else:
            tasks[task_id].update(
                {
                    "progress": 15,
                    "message": "正在转换音频格式...",
                }
            )
            save_tasks(tasks)
            await broadcast_task_update(task_id, tasks[task_id])

            audio_path = await video_processor.normalize_local_media_to_m4a(
                saved_path, TEMP_DIR
            )

            tasks[task_id].update(
                {
                    "progress": 35,
                    "message": "音频准备完成，准备转录...",
                }
            )
            save_tasks(tasks)
            await broadcast_task_update(task_id, tasks[task_id])

            tasks[task_id].update(
                {
                    "progress": 40,
                    "message": "正在转录音频（Whisper）...",
                }
            )
            save_tasks(tasks)
            await broadcast_task_update(task_id, tasks[task_id])

            raw_script = await transcriber.transcribe(audio_path)

        # 上传的原件本身就是「原视频」，无需再下载，直接挂到结果上
        is_media_upload = ext_lower != ".txt"

        await _run_post_extract_pipeline(
            task_id=task_id,
            raw_script=raw_script,
            video_title=video_title,
            source_ref=source_ref,
            summary_language=summary_language,
            request_summarizer=request_summarizer,
            dedup_url=None,
            api_key=api_key,
            model_base_url=model_base_url,
            model_id=model_id,
            media_filename=saved_path.name if is_media_upload else None,
            media_download_name=original_name if is_media_upload else None,
        )

    except Exception as e:
        logger.error(f"任务 {task_id} 处理失败: {str(e)}")
        if task_id in active_tasks:
            del active_tasks[task_id]
        tasks[task_id].update(
            {
                "status": "error",
                "error": str(e),
                "message": f"处理失败: {str(e)}",
            }
        )
        save_tasks(tasks)
        await broadcast_task_update(task_id, tasks[task_id])


@app.get("/api/task-status/{task_id}")
async def get_task_status(task_id: str):
    """
    获取任务状态
    """
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="任务不存在")

    return tasks[task_id]


@app.get("/api/task-stream/{task_id}")
async def task_stream(task_id: str):
    """
    SSE实时任务状态流
    """
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="任务不存在")

    async def event_generator():
        # 创建任务专用的队列
        queue = asyncio.Queue()

        # 将队列添加到连接列表
        if task_id not in sse_connections:
            sse_connections[task_id] = []
        sse_connections[task_id].append(queue)

        try:
            # 立即发送当前状态；带上有缓存的实时日志，页面重连时能回放
            current_task = dict(tasks.get(task_id, {}))
            cached_logs = task_logs.get(task_id)
            if cached_logs:
                current_task["logs"] = list(cached_logs)
            yield f"data: {json.dumps(current_task, ensure_ascii=False)}\n\n"
            # 任务已结束就不再挂着连接，否则这条流永远不会关闭
            if current_task.get("status") in ["completed", "error"]:
                return

            # 持续监听状态更新
            while True:
                try:
                    # 等待状态更新，超时时间30秒发送心跳
                    data = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield f"data: {data}\n\n"

                    # 如果任务完成或失败，结束流
                    task_data = json.loads(data)
                    if task_data.get("status") in ["completed", "error"]:
                        break

                except asyncio.TimeoutError:
                    # 发送心跳保持连接
                    yield f"data: {json.dumps({'type': 'heartbeat'}, ensure_ascii=False)}\n\n"

        except asyncio.CancelledError:
            logger.info(f"SSE连接被取消: {task_id}")
        except Exception as e:
            logger.error(f"SSE流异常: {e}")
        finally:
            # 清理连接
            if task_id in sse_connections and queue in sse_connections[task_id]:
                sse_connections[task_id].remove(queue)
                if not sse_connections[task_id]:
                    del sse_connections[task_id]

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET",
            "Access-Control-Allow-Headers": "Cache-Control",
        },
    )


DOWNLOAD_ALLOWED_EXT = frozenset({".md"}) | frozenset(MEDIA_MIME)


@app.get("/api/download/{filename}")
async def download_file(filename: str, name: str = ""):
    """
    直接从temp目录下载文件（转录 .md 与原视频/音频）。

    name: 可选，前端建议的展示文件名（会被清洗，扩展名强制与实际文件一致）。
    """
    try:
        file_path = _resolve_temp_file(filename, DOWNLOAD_ALLOWED_EXT)
        ext = file_path.suffix.lower()
        media_type = "text/markdown" if ext == ".md" else MEDIA_MIME[ext]
        download_name = _safe_download_name(name, ext) or filename

        return FileResponse(
            file_path,
            filename=download_name,
            media_type=media_type,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"下载文件失败: {e}")
        raise HTTPException(status_code=500, detail=f"下载失败: {str(e)}")


@app.get("/api/media/{filename}")
async def stream_media(filename: str):
    """
    内联播放原视频/音频，供结果页的 <video>/<audio> 直接引用。

    不设置 Content-Disposition，浏览器才会内联播放而不是触发下载；
    FileResponse 本身支持 Range 请求，所以进度条可以拖动。
    """
    try:
        file_path = _resolve_temp_file(filename, frozenset(MEDIA_MIME))
        return FileResponse(
            file_path,
            media_type=MEDIA_MIME[file_path.suffix.lower()],
            headers={
                "Accept-Ranges": "bytes",
                "Cache-Control": "private, max-age=3600",
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"读取媒体文件失败: {e}")
        raise HTTPException(status_code=500, detail=f"读取失败: {str(e)}")


def _other_task_needs_progress(exclude_task_id: str, sec_uid: str) -> bool:
    """是否还有其他同博主任务（运行中/已暂停）依赖该续转进度文件。

    同一博主可能同时存在多条任务记录（如一条失败、一条暂停），进度文件按
    sec_uid 共享，删除任务时需确认没有别的任务还要用它续转。
    """
    for tid, info in tasks.items():
        if tid == exclude_task_id or not info.get("channel"):
            continue
        if info.get("status") not in ("processing", "paused"):
            continue
        if _douyin_sec_uid(info.get("url") or "") == sec_uid:
            return True
    return False


@app.delete("/api/task/{task_id}")
async def delete_task(task_id: str):
    """
    取消并删除任务
    """
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="任务不存在")

    task_info = tasks[task_id]

    # 如果任务还在运行，先取消它
    if task_id in active_tasks:
        task = active_tasks[task_id]
        if not task.done():
            task.cancel()
            logger.info(f"任务 {task_id} 已被取消")
        del active_tasks[task_id]

    # 从处理URL列表中移除
    task_url = task_info.get("url")
    if task_url:
        processing_urls.discard(task_url)

    # 博主任务：一并清理续转进度文件，避免下次提交同一博主时意外续转；
    # 同博主还有运行/暂停中的任务时要保留（进度文件按 sec_uid 共享）
    if task_info.get("channel") and task_url:
        sec_uid = _douyin_sec_uid(task_url)
        if sec_uid and not _other_task_needs_progress(task_id, sec_uid):
            progress_store.delete_progress(TEMP_DIR, sec_uid)

    # 删除任务记录
    del tasks[task_id]
    task_logs.pop(task_id, None)
    save_tasks(tasks)
    return {"message": "任务已取消并删除"}


@app.post("/api/task/{task_id}/pause")
async def pause_task(task_id: str):
    """
    请求暂停一个正在运行的博主合集任务。

    暂停不打断进行中的视频：当前视频转录完成并落盘后任务转入 paused 状态，
    已完成的进度保存在续转进度文件里，重新提交同一博主链接即可续转。
    """
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="任务不存在")

    task_info = tasks[task_id]
    if not task_info.get("channel"):
        raise HTTPException(status_code=400, detail="仅抖音博主合集任务支持暂停")
    if task_id not in active_tasks or active_tasks[task_id].done():
        raise HTTPException(status_code=400, detail="任务不在运行中")
    if task_info.get("pause_requested"):
        return {"message": "已请求暂停，将在当前视频完成后暂停"}

    task_info["pause_requested"] = True
    save_tasks(tasks)
    logger.info(f"任务 {task_id} 收到暂停请求")
    return {"message": "已请求暂停，将在当前视频转录完成后暂停"}


@app.get("/api/tasks/unfinished")
async def get_unfinished_tasks():
    """
    未完成的博主合集任务（暂停/中断），前端据此渲染「未完成的博主任务」面板。

    进度数字优先取任务记录；任务记录缺失时（服务重启后标记前的瞬间）从
    续转进度文件补齐。
    """
    items = []
    for tid, task in tasks.items():
        if task.get("status") != "paused" or not task.get("channel"):
            continue
        url = task.get("url") or ""
        done = task.get("done_count")
        total = task.get("video_count")
        # 重启遗留的 paused 任务没有 done_count：从进度文件补
        if (done is None or total is None) and url:
            sec_uid = _douyin_sec_uid(url)
            if sec_uid:
                pdata = progress_store.load_progress(TEMP_DIR, sec_uid)
                if pdata.get("videos"):
                    if total is None:
                        total = pdata.get("total")
                    if done is None:
                        done = progress_store.done_count(pdata)
        items.append(
            {
                "task_id": tid,
                "url": url,
                "title": task.get("video_title") or "",
                "nickname": task.get("nickname") or "",
                "progress": task.get("progress", 0),
                "message": task.get("message", ""),
                "video_count": total,
                "done_count": done or 0,
            }
        )
    # 后创建的排前面（tasks 字典按插入序，最后插入的最新）
    items.reverse()
    return {"unfinished": items}


@app.get("/api/tasks/active")
async def get_active_tasks():
    """
    获取当前活跃任务列表（用于调试）
    """
    active_count = len(active_tasks)
    processing_count = len(processing_urls)
    return {
        "active_tasks": active_count,
        "processing_urls": processing_count,
        "task_ids": list(active_tasks.keys()),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
