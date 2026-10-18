"""
与运行环境无关的管线辅助函数。

这里只放纯函数：不依赖 FastAPI、不依赖全局任务状态、没有副作用。
Web 服务（main.py）、命令行（transcribe.py）和 MCP 服务（mcp_server.py）
共用同一份实现，避免「无语音判定」这类关键逻辑在多处各写一遍而逐渐走偏。
"""

import re

# 能作为「原视频/原音频」提供的媒体类型
MEDIA_MIME = {
    ".mp4": "video/mp4",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".flv": "video/x-flv",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
}
VIDEO_EXT = frozenset({".mp4", ".mkv", ".webm", ".mov", ".flv"})

# 本地上传/命令行输入允许的扩展名
UPLOAD_ALLOWED_EXT = frozenset(
    {".txt", ".mp3", ".mp4", ".m4a", ".wav", ".webm", ".mkv", ".ogg", ".flac"}
)

NO_SPEECH_NOTICE = (
    "_No speech detected in this video — transcript, summary and translation "
    "are unavailable._"
)


def media_kind(ext: str) -> str:
    """按扩展名判断该用 <video> 还是 <audio> 呈现。"""
    return "video" if ext.lower() in VIDEO_EXT else "audio"


def sanitize_title_for_filename(title: str) -> str:
    """将视频标题清洗为安全的文件名片段。"""
    if not title:
        return "untitled"
    # 仅保留字母数字、下划线、连字符与空格
    safe = re.sub(r"[^\w\-\s]", "", title)
    # 压缩空白并转为下划线
    safe = re.sub(r"\s+", "_", safe).strip("._-")
    # 最长限制，避免过长文件名问题
    return safe[:80] or "untitled"


def transcribed_speech(raw_script: str) -> str:
    """
    取出转录结果里真正的语音文字（去掉标题、语言元信息、时间戳、source 行）。

    返回空字符串表示这条视频没有可用语音。这个判断很重要：把空文稿丢给 LLM
    会让它对着空输入凭空编造出一整段对话，然后翻译和摘要再把这段虚构内容
    一路传下去，用户看到的就是完全不存在于视频里的「转录」。
    """
    if not raw_script:
        return ""

    marker = "## Transcription Content"
    idx = raw_script.find(marker)
    body = raw_script[idx + len(marker) :] if idx >= 0 else raw_script

    # 时间戳标记（**[00:00 - 00:03]**）本身不算语音内容
    body = re.sub(r"\*\*\[[^\]]*\]\*\*", " ", body)
    # 元信息与来源行
    body = re.sub(
        r"^\s*(\*\*(Detected Language|Language Probability)\*\*.*|source:.*|#.*)$",
        " ",
        body,
        flags=re.M,
    )
    # 纯文本上传为空时的占位符
    body = body.replace("(empty)", " ")

    return body.strip()


def txt_to_raw_transcript_markdown(body: str) -> str:
    """将纯文本包装为与 Whisper 输出结构一致的 Markdown。"""
    text = body.strip() if body.strip() else "(empty)"
    return "\n".join(
        [
            "# Video Transcription",
            "",
            "**Detected Language:**",
            "**Language Probability:** —",
            "",
            "## Transcription Content",
            "",
            text,
        ]
    )


# 抖音博主主页：douyin.com/user/<sec_uid>（含 ?sec_uid= 查询参数形态）
_DOUYIN_PROFILE_RE = re.compile(
    r"^https?://(?:www\.)?douyin\.com/user/"
    r"(?:profile\?(?:.*&)?sec_uid=)?"
    r"(?P<sec_uid>MS4wLjABAAAA[A-Za-z0-9._-]+)",
    re.IGNORECASE,
)


def douyin_sec_uid(url: str) -> str:
    """从抖音博主主页 URL 提取 sec_uid；不是博主主页时返回空字符串。"""
    if not url:
        return ""
    match = _DOUYIN_PROFILE_RE.match(url.strip())
    return match.group("sec_uid") if match else ""


def is_douyin_profile_url(url: str) -> bool:
    """判断 URL 是否为抖音博主主页（区别于单视频 douyin.com/video/<id>）。"""
    return bool(douyin_sec_uid(url))


def format_subtitle_markdown(entries: list, language: str) -> str:
    """把字幕条目格式化为与 Whisper 输出结构一致的 Markdown。

    三个入口的字幕链路共用：yt-dlp 字幕（video_processor）与抖音 cla_info
    字幕（channel_processor）产出同一种结构，下游的无语音判定/元信息剥离
    逻辑因此无需区分字幕来源。

    entries: [{start: "MM:SS", end: "MM:SS", text: str}]
    """
    lines = [
        "# Video Transcription",
        "",
        f"**Detected Language:** {language}",
        "**Language Probability:** 1.00",
        "",
        "## Transcription Content",
        "",
    ]
    for entry in entries:
        lines.append(f"**[{entry['start']} - {entry['end']}]**")
        lines.append("")
        lines.append(entry["text"])
        lines.append("")
    return "\n".join(lines)


def strip_transcript_meta(raw_script: str) -> str:
    """去掉 Whisper 输出的标题与语言元信息，保留时间戳与正文段落。

    博主合集场景逐视频拼接时用它避免每个小节都重复一遍
    “# Video Transcription / **Detected Language:**”头。
    """
    if not raw_script:
        return ""
    lines = []
    for line in raw_script.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append("")
            continue
        if stripped.startswith("# ") and "Transcription" in stripped:
            continue
        if stripped.startswith("**Detected Language") or stripped.startswith(
            "**Language Probability"
        ):
            continue
        if stripped.startswith("## Transcription Content"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


# CJK 字符（汉字/假名/谚文）：相邻片段任一侧是 CJK 时直接拼接，不加空格
_CJK_CHAR_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")


def merge_transcript_text(raw_script: str) -> str:
    """去掉元信息与时间戳，把转录正文合并为一个整段。

    博主合集的每个视频小节不再按时间戳切分，同一视频（同一日期小节）的
    内容直接拼成一段连续文本，便于阅读与检索；字幕与 Whisper 两种来源的
    输出结构一致，无需区分处理。
    """
    if not raw_script:
        return ""
    pieces: list[str] = []
    for line in raw_script.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # 时间戳标记（**[00:00 - 00:03]**）与元信息行都不算正文
        if stripped.startswith("**[") and stripped.endswith("]**"):
            continue
        if stripped.startswith("#"):
            continue
        if stripped.startswith("**Detected Language") or stripped.startswith(
            "**Language Probability"
        ):
            continue
        if stripped == "(empty)":
            continue
        pieces.append(stripped)

    if not pieces:
        return ""
    merged = pieces[0]
    for piece in pieces[1:]:
        if _CJK_CHAR_RE.match(merged[-1]) or _CJK_CHAR_RE.match(piece[0]):
            merged += piece
        else:
            merged += " " + piece
    return merged
