"""
抖音博主主页解析与媒体获取。

从 douyin.com/user/<sec_uid> 解析博主的视频列表（含发布时间），供博主批量转录使用；
并提供逐视频的字幕文本、媒体文件下载能力。

数据来源按优先级：
1. PC 主站 SSR：GET www.douyin.com/user/<sec_uid> 返回的 HTML 内嵌
   RENDER_DATA（URL-encoded JSON）或 _ROUTER_DATA，含博主信息与首批视频。
   2026 年起该路径大概率只返回应用外壳配置（拿不到视频列表），仅作为免浏览器的
   快路径保留——一旦能解析出完整列表就直接用，否则落入浏览器兜底。
2. Playwright 无头浏览器兜底：打开抖音页面后，在页面内 evaluate
   fetch('/aweme/v1/web/aweme/post/?...') 翻页——抖音自己的 SDK 会自动对请求
   签名（Argus/Lynx），绕过纯 HTTP 请求被 403 的问题。需要
   `pip install playwright && playwright install chromium`。
3. 移动端分享页（m.douyin.com/share/video/<id>）：单视频信息兜底，
   纯 HTTP + safari_ios 指纹即可访问，返回标题/发布时间/playwm 播放直链。

风控说明（2026 现状）：
- 抖音对非浏览器 TLS 指纹直接 TCP reset，因此用 curl_cffi 模拟 Chrome/Safari 指纹；
- 匿名请求基本全被拦，必须设置环境变量 DOUYIN_COOKIE（浏览器里复制的原始
  Cookie 字符串，由浏览器兜底与分享页两条链路共用）；
- 翻页直连 API 已被签名墙挡住（403 Blocked by ArgusSecurityPlugin），签名只能
  在真实浏览器页面内由抖音 SDK 生成，因此完整列表只能走浏览器兜底；
- 部分网络环境（如公司出口）在 TLS 层直接阻断 douyin.com，此时会抛出
  DouyinError 并在消息中说明原因与对策。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import urllib.parse
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# 请求头：Referer 与浏览器一致可降低风控触发概率
_SSR_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.douyin.com/",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Upgrade-Insecure-Requests": "1",
}

# 移动端 UA：m.douyin.com 分享页与 playwm 下载链路都认移动端指纹
_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

# 浏览器兜底用的桌面 UA（与 Playwright Chromium 版本对齐）
_DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)

# 翻页 API 的固定 query 参数（与网页版一致；签名由页面内 SDK 自动生成）
_POST_API = "https://www.douyin.com/aweme/v1/web/aweme/post/"
_POST_API_FIXED_PARAMS = {
    "device_platform": "webapp",
    "aid": "6383",
    "channel": "channel_pc_web",
    "account_region": "CN",
    "pc_client_type": "1",
    "version_code": "170038",
    "count": "20",
    "locate_query": "false",
    "show_live_replay_strategy": "need-show",
    "need_time_list": "1",
    "time_list_query": "0",
    "whale_cut_token": "",
    "cut_version": "1",
    "user_agent_age": "1",
}

# SSR 首批 + 翻页的总上限保护（博主可能有上千视频，避免接口风暴）
_MAX_VIDEOS_HARD_CAP = 2000

# 页面内 fetch 一次 post API 的 JS（SDK 自动加签名头；返回原始状态与 JSON）
_FETCH_PAGE_JS = """
async (args) => {
    const params = new URLSearchParams({
        device_platform: 'webapp', aid: '6383', channel: 'channel_pc_web',
        sec_user_id: args.secUid, max_cursor: String(args.maxCursor),
        locate_query: 'false', show_live_replay_strategy: 'need-show',
        count: '20', need_time_list: '1', time_list_query: '0',
        whale_cut_token: '', cut_version: '1', user_agent_age: '1',
    });
    const resp = await fetch('/aweme/v1/web/aweme/post/?' + params.toString(), {
        credentials: 'include',
    });
    const text = await resp.text();
    try {
        return {status: resp.status, json: JSON.parse(text)};
    } catch (e) {
        return {status: resp.status, json: null, head: text.slice(0, 200)};
    }
}
"""


class DouyinError(Exception):
    """博主主页解析失败（网络不可达 / 被风控 / 页面结构变化）。"""


def douyin_cookie() -> str:
    """读取 DOUYIN_COOKIE 环境变量（浏览器复制的原始 Cookie 字符串）。"""
    return (os.getenv("DOUYIN_COOKIE") or "").strip()


# 兼容旧名
_douyin_cookie = douyin_cookie


def _get(url: str, cookie: str = "") -> "curl_cffi.requests.Response":
    """用 Chrome TLS 指纹发起 GET；导入延迟到函数内便于测试与降级。"""
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError as e:
        raise DouyinError(
            "缺少依赖 curl_cffi（抖音需要浏览器 TLS 指纹），请 pip install curl_cffi"
        ) from e

    headers = dict(_SSR_HEADERS)
    if cookie:
        headers["Cookie"] = cookie
    try:
        return cffi_requests.get(url, headers=headers, impersonate="chrome", timeout=30)
    except Exception as e:
        raise DouyinError(
            f"无法访问抖音（{url[:120]}）：{e}。"
            "常见原因：当前网络出口被抖音拦截（如公司网络），"
            "请更换网络或配置 DOUYIN_COOKIE 后重试。"
        ) from e


def _get_mobile(url: str, cookie: str = "") -> "curl_cffi.requests.Response":
    """用 iOS Safari TLS 指纹 + 移动端 UA 发起 GET（m.douyin.com 分享页专用）。"""
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError as e:
        raise DouyinError(
            "缺少依赖 curl_cffi（抖音需要浏览器 TLS 指纹），请 pip install curl_cffi"
        ) from e

    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://www.douyin.com/",
        "User-Agent": _MOBILE_UA,
    }
    if cookie:
        headers["Cookie"] = cookie
    try:
        return cffi_requests.get(
            url, headers=headers, impersonate="safari_ios", timeout=30
        )
    except Exception as e:
        raise DouyinError(
            f"无法访问抖音（{url[:120]}）：{e}。"
            "常见原因：当前网络出口被抖音拦截（如公司网络），"
            "请更换网络或配置 DOUYIN_COOKIE 后重试。"
        ) from e


def _parse_router_data(html: str) -> Optional[dict]:
    """从主页 HTML 提取内嵌 JSON（RENDER_DATA 或 _ROUTER_DATA）。"""
    # 形态 1：<script id="RENDER_DATA" type="application/json">%7B...%7D</script>
    m = re.search(r'<script[^>]+id="RENDER_DATA"[^>]*>(.*?)</script>', html, re.S)
    if m:
        try:
            return json_loads_forgiving(urllib.parse.unquote(m.group(1)))
        except Exception as e:
            logger.debug(f"RENDER_DATA 解析失败: {e}")

    # 形态 2：window._ROUTER_DATA = {...};
    m = re.search(r"window\._ROUTER_DATA\s*=\s*(\{.*?\})\s*[;<]", html, re.S)
    if m:
        try:
            return json_loads_forgiving(m.group(1))
        except Exception as e:
            logger.debug(f"_ROUTER_DATA 解析失败: {e}")

    # 形态 3：self.__pace_f 流式数据里嵌的 JSON（新版 RSC 结构），保底搜 aweme_list
    m = re.search(r'\{"loaderData".*?"aweme_list"', html, re.S)
    if m:
        # 找包含它的最外层大括号：直接逐段尝试不现实，退而求其次找完整 JSON 行
        for line in html.split("\n"):
            if '"aweme_list"' in line and line.strip().startswith(
                ("{", "self.__pace_f")
            ):
                try:
                    raw = (
                        line.strip()
                        .removeprefix("self.__pace_f.push(")
                        .rstrip(");")
                        .strip('"')
                    )
                    return json_loads_forgiving(raw)
                except Exception:
                    continue
    return None


def json_loads_forgiving(text: str) -> dict:
    import json

    return json.loads(text)


def _traverse(data, *paths):
    """按多条候选路径在嵌套 dict 中找第一个非空值；适配抖音多版本结构。"""
    for path in paths:
        node = data
        ok = True
        for key in path:
            if isinstance(node, dict):
                # user_(sec_uid) 这类动态 key：用前缀匹配
                if key.endswith("*"):
                    prefix = key[:-1]
                    node = next(
                        (v for k, v in node.items() if k.startswith(prefix)), None
                    )
                else:
                    node = node.get(key)
            else:
                ok = False
                break
            if node is None:
                ok = False
                break
        if ok and node is not None:
            return node
    return None


def _extract_awemes(data: dict) -> tuple[str, list, Optional[int], int]:
    """从 SSR JSON 中提取 (博主昵称, 视频列表, max_cursor, has_more)。"""
    post = _traverse(
        data,
        ("loaderData", "user_*", "post"),
        ("loaderData", "(user)", "post"),
    )
    if post is None:
        # RENDER_DATA 结构：任意顶层 key → post
        for value in (data or {}).values():
            if isinstance(value, dict):
                post = _traverse(value, ("post",), ("post", "data"))
                if post:
                    break

    user_data = None
    aweme_list = []
    max_cursor = None
    has_more = 0
    if isinstance(post, dict):
        inner = post.get("data") if isinstance(post.get("data"), dict) else post
        aweme_list = (inner or {}).get("aweme_list") or (inner or {}).get("posts") or []
        max_cursor = (inner or {}).get("max_cursor")
        has_more = int((inner or {}).get("has_more") or 0)
        user_data = _traverse(inner or {}, ("user",), ("user", "user_info"))
        if user_data is None:
            user_data = _traverse(post, ("user", "user_info"), ("user",))

    nickname = ""
    if isinstance(user_data, dict):
        nickname = (
            user_data.get("nickname") or user_data.get("nick_name") or ""
        ).strip()
    return nickname, list(aweme_list), max_cursor, has_more


def _extract_caption(aweme: dict) -> tuple[str, str, str]:
    """从 aweme 提取字幕信息，返回 (url, format, lang)；无字幕返回三个空串。

    cla_info 在列表接口位于 aweme 顶层，在详情接口位于 aweme.video 下，两处都找。
    """
    cla = aweme.get("cla_info")
    if not isinstance(cla, dict):
        video = aweme.get("video")
        cla = video.get("cla_info") if isinstance(video, dict) else None
    infos = (cla or {}).get("caption_infos") or []
    for info in infos:
        if not isinstance(info, dict):
            continue
        url = (info.get("url") or "").strip()
        if not url:
            continue
        fmt = (info.get("caption_format") or info.get("format") or "").strip().lower()
        lang = (info.get("lang") or info.get("language") or "").strip()
        return url, fmt, lang
    return "", "", ""


def _extract_low_gear_url(aweme: dict) -> str:
    """提取码率最低的可播放直链（约 250-650kbps，比原画省 4 倍以上带宽）。

    URL 由 CDN 签发、约 6 小时过期；列表解析后立刻下载没问题，跨进程复用则会 403。
    """
    video = aweme.get("video")
    bit_rates = video.get("bit_rate") if isinstance(video, dict) else None
    best_rate: Optional[int] = None
    best_url = ""
    for entry in bit_rates or []:
        if not isinstance(entry, dict):
            continue
        urls = ((entry.get("play_addr") or {}).get("url_list")) or []
        if not urls or not (urls[0] or "").strip():
            continue
        rate = entry.get("bit_rate") or 0
        if best_rate is None or rate < best_rate:
            best_rate = rate
            best_url = urls[0].strip()
    if best_url:
        return best_url
    # 兜底：play_addr 第一条（原画直链，带宽消耗大但可用）
    if isinstance(video, dict):
        urls = (video.get("play_addr") or {}).get("url_list") or []
        if urls and (urls[0] or "").strip():
            return urls[0].strip()
    return ""


def _normalize_aweme(aweme: dict) -> Optional[dict]:
    """把抖音 aweme 对象规整为统一结构；无效条目返回 None。

    返回字段：
    - id/title/create_time/duration(秒)：基础信息
    - caption_url/caption_format/caption_lang：cla_info 字幕（有则免转录直取文本）
    - low_gear_url：低码率直链（有则免 yt-dlp 直连 CDN 下载）
    - is_image：图文作品（无音轨，跳过转录）
    """
    if not isinstance(aweme, dict):
        return None
    aweme_id = str(
        aweme.get("aweme_id") or aweme.get("awemeId") or aweme.get("id") or ""
    ).strip()
    if not aweme_id.isdigit():
        return None
    title = (aweme.get("desc") or aweme.get("description") or "").strip()
    create_time = aweme.get("create_time") or aweme.get("createTime")
    try:
        create_time = int(create_time) if create_time else None
    except (TypeError, ValueError):
        create_time = None

    video = aweme.get("video") if isinstance(aweme.get("video"), dict) else {}
    duration_ms = video.get("duration") or aweme.get("duration")
    try:
        duration = int(duration_ms) // 1000 if duration_ms else None
    except (TypeError, ValueError):
        duration = None

    caption_url, caption_format, caption_lang = _extract_caption(aweme)

    return {
        "id": aweme_id,
        "title": title,
        "create_time": create_time,
        "duration": duration,
        "caption_url": caption_url,
        "caption_format": caption_format,
        "caption_lang": caption_lang,
        "low_gear_url": _extract_low_gear_url(aweme),
        "is_image": bool(aweme.get("images")) or aweme.get("aweme_type") == 150,
    }


def _resolve_limit(max_videos: Optional[int]) -> int:
    """解析视频数上限：显式参数 > DOUYIN_MAX_VIDEOS 环境变量 > 硬上限。"""
    if max_videos and max_videos > 0:
        return max_videos
    env_raw = (os.getenv("DOUYIN_MAX_VIDEOS") or "").strip()
    if env_raw:
        try:
            env_limit = int(env_raw)
        except ValueError:
            logger.warning(f"DOUYIN_MAX_VIDEOS 配置非法: {env_raw!r}，忽略")
        else:
            if env_limit > 0:
                return env_limit
    return _MAX_VIDEOS_HARD_CAP


def _cookie_header_to_playwright(cookie: str) -> list[dict]:
    """把浏览器复制的原始 Cookie 字符串转成 Playwright add_cookies 的格式。"""
    pairs = []
    for pair in (cookie or "").split(";"):
        name, _, value = pair.strip().partition("=")
        if name:
            pairs.append(
                {"name": name, "value": value, "domain": ".douyin.com", "path": "/"}
            )
    return pairs


def _collect_post_videos(
    fetch_json: Callable[[int], Optional[dict]],
    limit: int,
    on_first_page: Optional[Callable[[str, int], None]] = None,
) -> tuple[str, list[dict]]:
    """分页循环收集博主视频（纯逻辑，与浏览器解耦便于测试）。

    Args:
        fetch_json: 同步函数，入参 max_cursor（毫秒时间戳，首页传 0），返回
            post API 的 JSON dict；请求失败或无数据时返回 None。
        limit: 视频数上限。
        on_first_page: 首页解析完成后的回调 (昵称, 首页数量)。

    Returns:
        (博主昵称, 规整后的视频列表)。昵称取自首页任意视频的 author.nickname。
    """
    videos: list[dict] = []
    seen: set[str] = set()
    nickname = ""
    cursor = 0
    first_page = True
    prev_has_more = False

    while len(videos) < limit:
        data = fetch_json(cursor)
        if not isinstance(data, dict):
            break
        status = data.get("status_code")
        if status not in (0, None):
            logger.info(f"抖音 post API status_code={status}，停止翻页")
            break
        aweme_list = data.get("aweme_list") or []

        if first_page:
            for raw in aweme_list:
                if not isinstance(raw, dict):
                    continue
                nick = ((raw.get("author") or {}).get("nickname") or "").strip()
                if nick:
                    nickname = nick
                    break
            if on_first_page:
                try:
                    on_first_page(nickname, len(aweme_list))
                except Exception:
                    pass
            first_page = False

        if not aweme_list:
            if prev_has_more:
                # 上一页明确说还有更多，本页却空了：典型的匿名访问截断，
                # 不告警的话用户只会看到「解析到 N 个视频」而不知原因
                logger.warning(
                    f"抖音 post API 上一页 has_more=1 但本页返回空列表，"
                    f"疑似未登录被截断（仅解析到 {len(videos)} 个视频）："
                    "请配置 DOUYIN_COOKIE（浏览器登录后的 Cookie 字符串）后重试"
                )
            break

        for raw in aweme_list:
            normalized = _normalize_aweme(raw)
            if normalized and normalized["id"] not in seen and len(videos) < limit:
                seen.add(normalized["id"])
                videos.append(normalized)

        has_more = int(data.get("has_more") or 0)
        next_cursor = data.get("max_cursor")
        if not has_more or next_cursor is None or next_cursor == cursor:
            break
        prev_has_more = True
        cursor = next_cursor

    return nickname, videos


def _fetch_profile_videos_browser_sync(
    sec_uid: str,
    cookie: str,
    limit: int,
    on_first_page: Optional[Callable[[str, int], None]] = None,
) -> tuple[str, list[dict]]:
    """Playwright 无头浏览器兜底（同步实现，由 async 调用方放到线程里跑）。

    打开抖音页面让 SDK 就绪后，在页面内 evaluate fetch 翻页——签名头由抖音
    自己的 SDK 生成，这是目前唯一能拿到完整视频列表的途径。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise DouyinError(
            "需要 Playwright 浏览器兜底但未安装，"
            "请执行 pip install playwright && playwright install chromium"
        ) from e

    profile_url = f"https://www.douyin.com/user/{sec_uid}"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                user_agent=_DESKTOP_UA,
                viewport={"width": 1280, "height": 800},
            )
            cookies = _cookie_header_to_playwright(cookie)
            if cookies:
                try:
                    context.add_cookies(cookies)
                except Exception as e:
                    logger.warning(f"浏览器注入 Cookie 失败（将匿名访问）: {e}")

            page = context.new_page()
            try:
                page.goto(profile_url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                logger.warning(f"抖音博主页打开失败，回退主页: {e}")
                page.goto(
                    "https://www.douyin.com/",
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
            # 等 SDK 完成初始化（签名器就绪），否则页面内 fetch 会被 403
            page.wait_for_timeout(4000)

            def _fetch_json(max_cursor: int) -> Optional[dict]:
                last_result = None
                for _attempt in range(3):
                    try:
                        result = page.evaluate(
                            _FETCH_PAGE_JS,
                            {"secUid": sec_uid, "maxCursor": max_cursor},
                        )
                    except Exception as e:
                        logger.warning(f"抖音页面内 fetch 异常: {e}")
                        result = None
                    if isinstance(result, dict):
                        if result.get("status") == 200 and isinstance(
                            result.get("json"), dict
                        ):
                            return result["json"]
                        last_result = result
                    page.wait_for_timeout(2500)
                logger.warning(f"抖音页面内 fetch 多次失败: {last_result}")
                return None

            return _collect_post_videos(_fetch_json, limit, on_first_page)
        finally:
            try:
                browser.close()
            except Exception:
                pass


async def _fetch_post_page(
    sec_uid: str, max_cursor: int, cookie: str
) -> tuple[list, Optional[int], int]:
    """直连翻页 API 拉一页视频；被风控/需签名时返回空列表。"""
    params = dict(_POST_API_FIXED_PARAMS)
    params.update(
        {
            "sec_user_id": sec_uid,
            "max_cursor": str(max_cursor),
        }
    )
    url = _POST_API + "?" + urllib.parse.urlencode(params)

    def _do():
        resp = _get(url, cookie=cookie)
        if resp.status_code != 200:
            logger.info(f"抖音翻页 API HTTP {resp.status_code}，停止翻页")
            return None
        return resp.json()

    try:
        data = await asyncio.to_thread(_do)
    except DouyinError as e:
        logger.warning(f"抖音翻页 API 请求失败: {e}")
        return [], None, 0
    except Exception as e:
        logger.warning(f"抖音翻页 API 响应异常: {e}")
        return [], None, 0

    if not isinstance(data, dict):
        return [], None, 0

    status = data.get("status_code")
    if status not in (0, None):
        logger.info(
            f"抖音翻页 API status_code={status}（可能需要签名/cookie），停止翻页"
        )
        return [], None, 0
    aweme_list = data.get("aweme_list") or []
    next_cursor = data.get("max_cursor")
    has_more = int(data.get("has_more") or 0)
    return list(aweme_list), next_cursor, has_more


async def _fetch_profile_videos_ssr(
    sec_uid: str,
    cookie: str,
    limit: int,
    on_first_page: Optional[Callable[[str, int], None]] = None,
) -> tuple[str, list[dict], bool]:
    """SSR 快路径：纯 HTTP 解析主页内嵌数据，返回 (昵称, 视频列表, 是否完整)。

    complete=False 表示还有未翻完的页（翻页 API 被签名墙挡住），调用方应落入
    浏览器兜底拿全量列表，而不是拿着残缺列表当全集。
    """
    page_url = f"https://www.douyin.com/user/{sec_uid}"

    def _do_ssr():
        resp = _get(page_url, cookie=cookie)
        if resp.status_code != 200:
            raise DouyinError(
                f"抖音主页请求失败 HTTP {resp.status_code}："
                "可能被风控拦截，建议配置 DOUYIN_COOKIE（浏览器登录后的 Cookie）后重试"
            )
        return resp.text

    html = await asyncio.to_thread(_do_ssr)

    data = _parse_router_data(html)
    if data is None:
        raise DouyinError(
            "抖音主页返回内容不含可解析的数据（被风控验证页拦截或页面结构已变化）。"
            "建议：1) 配置 DOUYIN_COOKIE 环境变量；2) 更换网络环境后重试"
        )

    nickname, aweme_list, max_cursor, has_more = _extract_awemes(data)
    if on_first_page:
        try:
            on_first_page(nickname, len(aweme_list))
        except Exception:
            pass

    videos: list[dict] = []
    seen: set[str] = set()

    def _absorb(raw_list: list) -> None:
        for raw in raw_list:
            normalized = _normalize_aweme(raw)
            if normalized and normalized["id"] not in seen and len(videos) < limit:
                seen.add(normalized["id"])
                videos.append(normalized)

    _absorb(aweme_list)

    complete = not has_more or len(videos) >= limit
    cursor = max_cursor
    while has_more and cursor is not None and len(videos) < limit:
        page_awemes, next_cursor, page_has_more = await _fetch_post_page(
            sec_uid, cursor, cookie
        )
        if not page_awemes:
            break
        _absorb(page_awemes)
        has_more = page_has_more
        complete = not has_more or len(videos) >= limit
        if complete or next_cursor is None or next_cursor == cursor:
            break
        cursor = next_cursor

    return nickname, videos, complete


async def fetch_profile_videos(
    url: str,
    cookie: str = "",
    max_videos: Optional[int] = None,
    on_first_page: Optional[Callable[[str, int], None]] = None,
) -> tuple[str, list[dict]]:
    """解析抖音博主主页，返回 (博主昵称, [{id, title, create_time, ...}])。

    编排：SSR 快路径（纯 HTTP，能拿全就直接用）→ Playwright 浏览器兜底
    （页面内 fetch 翻页，签名由抖音 SDK 生成）。两条路都失败时抛出
    DouyinError，消息里带上各自的失败原因。

    cookie: DOUYIN_COOKIE 内容（浏览器原始 Cookie 字符串）；空则取环境变量。
    max_videos: 上限保护；None 时取 DOUYIN_MAX_VIDEOS 环境变量，再缺省用硬上限。
    on_first_page: 首批视频解析完成后的回调 (昵称, 首批数量)，供进度展示。
    """
    from pipeline import douyin_sec_uid

    sec_uid = douyin_sec_uid(url)
    if not sec_uid:
        raise DouyinError(f"无法从 URL 提取抖音博主 sec_uid: {url}")

    cookie = cookie or _douyin_cookie()
    limit = _resolve_limit(max_videos)

    # on_first_page 只允许触发一次：SSR 拿到首批后若还要落浏览器兜底，避免重复上报
    reported = {"done": False}

    def _on_first(nick: str, count: int) -> None:
        # count<=0（如 SSR 只拿到应用外壳）不触发：留给能拿到真实首批的路径上报
        if on_first_page is None or reported["done"] or count <= 0:
            return
        reported["done"] = True
        try:
            on_first_page(nick, count)
        except Exception:
            pass

    errors: list[str] = []

    try:
        nickname, videos, complete = await _fetch_profile_videos_ssr(
            sec_uid, cookie, limit, _on_first
        )
        if videos and complete:
            logger.info(
                f"抖音博主 {nickname or '(未知)'}（SSR）解析到 {len(videos)} 个视频"
            )
            return nickname, videos
        if not videos:
            errors.append("SSR 未解析到视频列表")
        else:
            errors.append(f"SSR 只解析到 {len(videos)} 个视频且未翻完")
    except DouyinError as e:
        errors.append(f"SSR 失败: {e}")
    except Exception as e:  # SSR 内部未预期异常也不阻断兜底
        logger.warning(f"SSR 快路径异常: {e}")
        errors.append(f"SSR 异常: {e}")

    try:
        nickname, videos = await asyncio.to_thread(
            _fetch_profile_videos_browser_sync, sec_uid, cookie, limit, _on_first
        )
    except DouyinError as e:
        errors.append(f"浏览器兜底失败: {e}")
    except Exception as e:
        logger.warning(f"浏览器兜底异常: {e}")
        errors.append(f"浏览器兜底异常: {e}")
    else:
        if videos:
            logger.info(
                f"抖音博主 {nickname or '(未知)'}（浏览器）解析到 {len(videos)} 个视频"
            )
            return nickname, videos
        errors.append(f"浏览器兜底解析到 0 个视频（博主 {nickname or '未知'}）")

    raise DouyinError(
        f"抖音博主主页解析失败（sec_uid={sec_uid[:24]}…）。尝试记录："
        + "；".join(errors)
        + "。建议：1) 确认 DOUYIN_COOKIE 已配置且未过期；"
        "2) 确认已安装 playwright（pip install playwright && playwright install chromium）；"
        "3) 更换网络环境后重试"
    )


async def fetch_share_video_info(video_id: str, cookie: str = "") -> dict:
    """解析单视频信息（移动端分享页兜底，纯 HTTP 无需浏览器）。

    PC 端接口被签名墙挡住时用它拿标题/发布时间/playwm 播放直链；
    playwm 直链会 302 到 CDN mp4，可直接交给 download_media。

    Returns:
        {id, title, create_time, duration(秒), play_urls: [str], is_image}
    Raises:
        DouyinError：请求失败 / 页面结构变化 / 视频不存在。
    """
    video_id = str(video_id or "").strip()
    if not video_id.isdigit():
        raise DouyinError(f"非法的抖音视频 ID: {video_id!r}")

    url = f"https://m.douyin.com/share/video/{video_id}"

    def _do():
        resp = _get_mobile(url, cookie=cookie or douyin_cookie())
        if resp.status_code != 200:
            raise DouyinError(
                f"分享页请求失败 HTTP {resp.status_code}："
                "视频可能已删除/私密，或被风控拦截"
            )
        return resp.text

    html = await asyncio.to_thread(_do)

    # 注意终结符必须是 </script>：分享页 JSON 里可能含分号，
    # 通用解析的 [;<] 终结符会截断（句尾分号则允许）
    m = re.search(r"window\._ROUTER_DATA\s*=\s*(\{.*?\})\s*;?\s*</script>", html, re.S)
    if not m:
        raise DouyinError(
            f"分享页无 _ROUTER_DATA（视频 {video_id} 可能已删除或被风控）"
        )
    try:
        data = json_loads_forgiving(m.group(1))
    except Exception as e:
        raise DouyinError(f"分享页数据解析失败: {e}") from e

    loader = (data or {}).get("loaderData") or {}
    item = None
    for value in loader.values():
        if not isinstance(value, dict):
            continue
        info = value.get("videoInfoRes") or {}
        items = info.get("item_list") or []
        if items and isinstance(items[0], dict):
            item = items[0]
            break
    if item is None:
        raise DouyinError(f"分享页未找到视频 {video_id} 的信息（可能已删除）")

    video = item.get("video") if isinstance(item.get("video"), dict) else {}
    play_urls = [u for u in ((video.get("play_addr") or {}).get("url_list") or []) if u]

    duration_ms = item.get("duration") or video.get("duration")
    try:
        duration = int(duration_ms) // 1000 if duration_ms else None
    except (TypeError, ValueError):
        duration = None

    create_time = item.get("create_time")
    try:
        create_time = int(create_time) if create_time else None
    except (TypeError, ValueError):
        create_time = None

    return {
        "id": str(item.get("aweme_id") or item.get("awemeId") or video_id),
        "title": (item.get("desc") or item.get("description") or "").strip(),
        "create_time": create_time,
        "duration": duration,
        "play_urls": play_urls,
        "is_image": bool(item.get("images")) or item.get("aweme_type") == 150,
    }


async def fetch_text(url: str, cookie: str = "") -> str:
    """下载文本资源（字幕 VTT 等）并返回解码后的文本内容。"""
    if not url:
        raise DouyinError("fetch_text: URL 为空")

    def _do():
        resp = _get(url, cookie=cookie)
        if resp.status_code != 200:
            raise DouyinError(f"文本下载失败 HTTP {resp.status_code}: {url[:120]}")
        return resp.text

    return await asyncio.to_thread(_do)


async def download_media(url: str, dest_path: Path, cookie: str = "") -> Path:
    """流式下载媒体文件（mp4 等）到 dest_path，成功返回该路径。

    兼容两种形态：aweme.snssdk.com 的 playwm 重定向直链（302 到 CDN）与
    CDN 上的直链（列表接口给的低码率地址）。
    """
    if not url:
        raise DouyinError("download_media: URL 为空")
    dest_path = Path(dest_path)

    def _do():
        try:
            from curl_cffi import requests as cffi_requests
        except ImportError as e:
            raise DouyinError(
                "缺少依赖 curl_cffi（抖音需要浏览器 TLS 指纹），请 pip install curl_cffi"
            ) from e

        headers = {
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://www.douyin.com/",
            "User-Agent": _MOBILE_UA,
        }
        if cookie:
            headers["Cookie"] = cookie

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        resp = None
        try:
            resp = cffi_requests.get(
                url,
                headers=headers,
                impersonate="safari_ios",
                stream=True,
                timeout=600,
            )
            if resp.status_code not in (200, 206):
                raise DouyinError(f"媒体下载失败 HTTP {resp.status_code}: {url[:120]}")
            with open(dest_path, "wb") as f:
                if hasattr(resp, "iter_content"):
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
                else:  # 老版本 curl_cffi 无流式接口：整体读入
                    f.write(resp.content)
        except DouyinError:
            raise
        except Exception as e:
            raise DouyinError(f"媒体下载失败: {e}") from e
        finally:
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass

        if not dest_path.exists() or dest_path.stat().st_size == 0:
            raise DouyinError(f"媒体下载完成但文件为空: {dest_path.name}")

    await asyncio.to_thread(_do)
    return dest_path
