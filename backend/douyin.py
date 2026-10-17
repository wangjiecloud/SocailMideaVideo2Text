"""
抖音博主主页解析。

从 douyin.com/user/<sec_uid> 解析博主的视频列表（含发布时间），供博主批量转录使用。

数据来源按优先级：
1. PC 主站 SSR：GET www.douyin.com/user/<sec_uid> 返回的 HTML 内嵌
   RENDER_DATA（URL-encoded JSON）或 _ROUTER_DATA，含博主信息与首批视频。
2. Web API 翻页：GET /aweme/v1/web/aweme/post/（sec_user_id + max_cursor），
   拿到剩余视频；接口可能要求签名/cookie，失败时降级为只用 SSR 首批。

风控说明（2026 现状）：
- 抖音对非浏览器 TLS 指纹直接 TCP reset，因此用 curl_cffi 模拟 Chrome；
- 无 cookie 的匿名请求大概率只能拿到 SSR 首批（约 10-20 条），翻页 API
  需要登录态；设置环境变量 DOUYIN_COOKIE（浏览器里复制的原始 Cookie 字符串）
  可显著提高成功率；
- 部分网络环境（如公司出口）在 TLS 层直接阻断 douyin.com，此时会抛出
  DouyinError 并在消息中说明原因与对策。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import urllib.parse
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

# 翻页 API 的固定 query 参数（与网页版一致；签名参数缺失时接口可能拒绝）
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
            f"无法访问抖音（{url}）：{e}。"
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


def _normalize_aweme(aweme: dict) -> Optional[dict]:
    """把抖音 aweme 对象规整为 {id, title, create_time}；无效条目返回 None。"""
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
    return {"id": aweme_id, "title": title, "create_time": create_time}


async def _fetch_post_page(
    sec_uid: str, max_cursor: int, cookie: str
) -> tuple[list, Optional[int], int]:
    """调用翻页 API 拉一页视频；被风控/需签名时返回空列表。"""
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


async def fetch_profile_videos(
    url: str,
    cookie: str = "",
    max_videos: Optional[int] = None,
    on_first_page: Optional[Callable[[str, int], None]] = None,
) -> tuple[str, list[dict]]:
    """解析抖音博主主页，返回 (博主昵称, [{id, title, create_time}])。

    cookie: DOUYIN_COOKIE 内容（浏览器原始 Cookie 字符串）；空则匿名。
    max_videos: 上限保护；None 时最多 _MAX_VIDEOS_HARD_CAP。
    on_first_page: 首批视频解析完成后的回调 (昵称, 首批数量)，供进度展示。
    """
    from pipeline import douyin_sec_uid

    sec_uid = douyin_sec_uid(url)
    if not sec_uid:
        raise DouyinError(f"无法从 URL 提取抖音博主 sec_uid: {url}")

    cookie = cookie or _douyin_cookie()
    limit = max_videos or _MAX_VIDEOS_HARD_CAP

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

    # 翻页补齐（需要 cookie/签名才可能成功；失败静默降级为首批）
    cursor = max_cursor
    while has_more and cursor is not None and len(videos) < limit:
        page_awemes, next_cursor, page_has_more = await _fetch_post_page(
            sec_uid, cursor, cookie
        )
        if not page_awemes:
            break
        _absorb(page_awemes)
        has_more = page_has_more
        if next_cursor is None or next_cursor == cursor:
            break
        cursor = next_cursor

    if not videos:
        raise DouyinError(
            f"博主 {nickname or sec_uid[:24]} 的视频列表为空："
            "可能需要登录 Cookie（配置 DOUYIN_COOKIE）或该博主无公开作品"
        )

    logger.info(f"抖音博主 {nickname or '(未知)'} 解析到 {len(videos)} 个视频")
    return nickname, videos
