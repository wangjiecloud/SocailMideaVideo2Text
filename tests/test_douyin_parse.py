"""douyin：博主主页解析、浏览器兜底编排与单视频媒体获取的单元测试（无网络）。"""

import asyncio
import json
import sys
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import douyin as dy  # noqa: E402


class FakeResp:
    def __init__(self, text="", status_code=200, payload=None):
        self.text = text
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _aweme(aid, desc="视频", create_time=1760000000, author=None):
    aweme = {"aweme_id": str(aid), "desc": desc, "create_time": create_time}
    if author is not None:
        aweme["author"] = {"nickname": author}
    return aweme


def _render_data_html(data: dict) -> str:
    encoded = urllib.parse.quote(json.dumps(data, ensure_ascii=False))
    return (
        f'<html><head><script id="RENDER_DATA" type="application/json">'
        f"{encoded}</script></head><body>ok</body></html>"
    )


def _router_data_html(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    return f"<html><script>window._ROUTER_DATA = {payload};</script></html>"


def _loader_data(awemes, nickname="测试博主", has_more=0, max_cursor=100):
    return {
        "loaderData": {
            "user_MS4wLjABAAAAfake": {
                "post": {
                    "data": {
                        "aweme_list": awemes,
                        "max_cursor": max_cursor,
                        "has_more": has_more,
                        "user": {"nickname": nickname},
                    }
                }
            }
        }
    }


def _patch_browser(nickname="浏览器博主", awemes=None, error=None):
    """mock 掉 Playwright 浏览器兜底（单测绝不启动真实浏览器）。"""
    if awemes is None:
        awemes = [_aweme(101, "浏览器视频一"), _aweme(102, "浏览器视频二")]
    normalized = [dy._normalize_aweme(a) for a in awemes]

    def fake_browser(sec_uid, cookie, limit, on_first_page=None):
        if error is not None:
            raise error
        if on_first_page:
            on_first_page(nickname, len(normalized))
        return nickname, normalized

    # 不用 new= 传普通函数：side_effect 让 as 拿到 MagicMock，可断言调用次数
    return mock.patch.object(
        dy, "_fetch_profile_videos_browser_sync", side_effect=fake_browser
    )


class ParseRouterDataTest(unittest.TestCase):
    def test_render_data_form(self):
        data = _loader_data([_aweme(1)])
        parsed = dy._parse_router_data(_render_data_html(data))
        self.assertIsNotNone(parsed)
        nickname, awemes, cursor, has_more = dy._extract_awemes(parsed)
        self.assertEqual(nickname, "测试博主")
        self.assertEqual(len(awemes), 1)
        self.assertEqual(cursor, 100)
        self.assertEqual(has_more, 0)

    def test_router_data_form(self):
        data = _loader_data([_aweme(1), _aweme(2)], has_more=1)
        parsed = dy._parse_router_data(_router_data_html(data))
        self.assertIsNotNone(parsed)
        nickname, awemes, cursor, has_more = dy._extract_awemes(parsed)
        self.assertEqual(nickname, "测试博主")
        self.assertEqual(len(awemes), 2)
        self.assertEqual(has_more, 1)

    def test_render_data_nested_key_fallback(self):
        # RENDER_DATA 顶层 key 任意命名，值里藏 post.data
        data = {
            "opaque_key": {
                "post": {
                    "data": {"aweme_list": [_aweme(9)], "user": {"nickname": "别名"}}
                }
            }
        }
        parsed = dy._parse_router_data(_render_data_html(data))
        nickname, awemes, _cursor, _more = dy._extract_awemes(parsed)
        self.assertEqual(nickname, "别名")
        self.assertEqual(len(awemes), 1)

    def test_risk_control_page_returns_none(self):
        # 风控验证页：没有可识别的数据结构
        self.assertIsNone(dy._parse_router_data("<html>请完成验证</html>"))
        self.assertIsNone(dy._parse_router_data(""))


class NormalizeAwemeTest(unittest.TestCase):
    def test_minimal_aweme(self):
        out = dy._normalize_aweme(_aweme(123, "标题", 1760000000))
        self.assertEqual(
            out,
            {
                "id": "123",
                "title": "标题",
                "create_time": 1760000000,
                "duration": None,
                "caption_url": "",
                "caption_format": "",
                "caption_lang": "",
                "low_gear_url": "",
                "is_image": False,
            },
        )

    def test_camel_case_keys(self):
        out = dy._normalize_aweme(
            {"awemeId": "456", "desc": "", "createTime": "1760000000"}
        )
        self.assertEqual(out["id"], "456")
        self.assertEqual(out["create_time"], 1760000000)
        self.assertEqual(out["title"], "")

    def test_invalid_id_rejected(self):
        self.assertIsNone(dy._normalize_aweme({"aweme_id": "abc"}))  # 非纯数字
        self.assertIsNone(dy._normalize_aweme({}))
        self.assertIsNone(dy._normalize_aweme("not a dict"))
        self.assertIsNone(dy._normalize_aweme(None))

    def test_bad_create_time_becomes_none(self):
        out = dy._normalize_aweme({"aweme_id": "1", "desc": "x", "create_time": "abc"})
        self.assertIsNone(out["create_time"])

    def test_media_fields_extracted(self):
        aweme = {
            "aweme_id": "789",
            "desc": "口播视频",
            "create_time": 1760000100,
            "duration": 61500,
            "cla_info": {
                "caption_infos": [
                    {
                        "url": "https://cdn.example/vtt?sign=1",
                        "caption_format": "webvtt",
                        "lang": "zh",
                    }
                ]
            },
            "video": {
                "duration": 61500,
                "bit_rate": [
                    {"bit_rate": 900000, "play_addr": {"url_list": ["https://h"]}},
                    {"bit_rate": 300000, "play_addr": {"url_list": ["https://l"]}},
                ],
            },
        }
        out = dy._normalize_aweme(aweme)
        self.assertEqual(out["duration"], 61)  # 毫秒 → 秒
        self.assertEqual(out["caption_url"], "https://cdn.example/vtt?sign=1")
        self.assertEqual(out["caption_format"], "webvtt")
        self.assertEqual(out["caption_lang"], "zh")
        self.assertEqual(out["low_gear_url"], "https://l")  # 码率最低的直链
        self.assertFalse(out["is_image"])

    def test_caption_in_video_cla_info(self):
        # 详情接口的 cla_info 挂在 aweme.video 下
        aweme = {
            "aweme_id": "2",
            "video": {
                "cla_info": {
                    "caption_infos": [{"url": "https://cdn/vtt", "lang": "eng"}]
                }
            },
        }
        out = dy._normalize_aweme(aweme)
        self.assertEqual(out["caption_url"], "https://cdn/vtt")
        self.assertEqual(out["caption_lang"], "eng")
        self.assertEqual(out["caption_format"], "")

    def test_low_gear_falls_back_to_play_addr(self):
        aweme = {
            "aweme_id": "3",
            "video": {
                "bit_rate": [],
                "play_addr": {"url_list": ["https://cdn/orig"]},
            },
        }
        out = dy._normalize_aweme(aweme)
        self.assertEqual(out["low_gear_url"], "https://cdn/orig")

    def test_image_post_detected(self):
        out = dy._normalize_aweme(
            {"aweme_id": "4", "desc": "图集", "images": [{"url": "x"}]}
        )
        self.assertTrue(out["is_image"])
        out2 = dy._normalize_aweme({"aweme_id": "5", "aweme_type": 150})
        self.assertTrue(out2["is_image"])
        # 普通视频 aweme_type=0 / media_type=4 不算图文
        out3 = dy._normalize_aweme({"aweme_id": "6", "aweme_type": 0, "media_type": 4})
        self.assertFalse(out3["is_image"])


class CollectPostVideosTest(unittest.TestCase):
    def test_pagination_and_nickname(self):
        pages = [
            {
                "status_code": 0,
                "aweme_list": [
                    _aweme(1, author="主页博主"),
                    _aweme(2, author="主页博主"),
                ],
                "max_cursor": 2000,
                "has_more": 1,
            },
            {
                "status_code": 0,
                "aweme_list": [_aweme(3)],
                "max_cursor": 3000,
                "has_more": 0,
            },
        ]
        cursors = []

        def fetch_json(cursor):
            cursors.append(cursor)
            return pages.pop(0)

        nickname, videos = dy._collect_post_videos(fetch_json, 100)
        self.assertEqual(cursors, [0, 2000])
        self.assertEqual(nickname, "主页博主")
        self.assertEqual([v["id"] for v in videos], ["1", "2", "3"])

    def test_limit_stops_early(self):
        pages = [
            {
                "status_code": 0,
                "aweme_list": [_aweme(i) for i in range(1, 6)],
                "max_cursor": 2000,
                "has_more": 1,
            },
        ]
        calls = []

        def fetch_json(cursor):
            calls.append(cursor)
            return pages[0]

        _nick, videos = dy._collect_post_videos(fetch_json, 3)
        self.assertEqual(len(videos), 3)
        self.assertEqual(len(calls), 1)  # 够数后不再翻页

    def test_none_response_stops(self):
        nickname, videos = dy._collect_post_videos(lambda cursor: None, 10)
        self.assertEqual((nickname, videos), ("", []))

    def test_nonzero_status_stops(self):
        def fetch_json(cursor):
            return {"status_code": 11110, "aweme_list": []}

        _nick, videos = dy._collect_post_videos(fetch_json, 10)
        self.assertEqual(videos, [])

    def test_stuck_cursor_stops(self):
        # max_cursor 停止前进时必须终止，否则死循环：
        # 第 1 页 0→2000（前进），第 2 页 2000→2000（卡住）→ 停止
        page = {
            "status_code": 0,
            "aweme_list": [_aweme(1)],
            "max_cursor": 2000,
            "has_more": 1,
        }
        calls = []

        def fetch_json(cursor):
            calls.append(cursor)
            return dict(page)

        _nick, videos = dy._collect_post_videos(fetch_json, 100)
        self.assertEqual(calls, [0, 2000])
        self.assertEqual(len(videos), 1)  # 第二页同 ID 被去重

    def test_dedup_by_id(self):
        page = {
            "status_code": 0,
            "aweme_list": [_aweme(1), _aweme(1), _aweme(2)],
            "max_cursor": 2000,
            "has_more": 0,
        }
        _nick, videos = dy._collect_post_videos(lambda cursor: dict(page), 100)
        self.assertEqual([v["id"] for v in videos], ["1", "2"])

    def test_on_first_page_callback(self):
        page = {
            "status_code": 0,
            "aweme_list": [_aweme(1, author="首批博主")],
            "max_cursor": 2000,
            "has_more": 1,
        }
        seen = []
        dy._collect_post_videos(
            lambda cursor: dict(page),
            100,
            on_first_page=lambda n, c: seen.append((n, c)),
        )
        self.assertEqual(seen, [("首批博主", 1)])


class ResolveLimitTest(unittest.TestCase):
    def test_explicit_param_wins(self):
        self.assertEqual(dy._resolve_limit(5), 5)

    def test_env_var(self):
        with mock.patch.dict("os.environ", {"DOUYIN_MAX_VIDEOS": "3"}):
            self.assertEqual(dy._resolve_limit(None), 3)
            self.assertEqual(dy._resolve_limit(7), 7)  # 显式参数优先

    def test_invalid_env_ignored(self):
        with mock.patch.dict("os.environ", {"DOUYIN_MAX_VIDEOS": "abc"}):
            self.assertEqual(dy._resolve_limit(None), dy._MAX_VIDEOS_HARD_CAP)

    def test_default_hard_cap(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(dy._resolve_limit(None), dy._MAX_VIDEOS_HARD_CAP)


class FetchProfileVideosTest(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_url_raises(self):
        with self.assertRaises(dy.DouyinError):
            await dy.fetch_profile_videos("https://www.douyin.com/video/123")

    async def test_ssr_complete_skips_browser(self):
        data = _loader_data([_aweme(1, "视频一"), _aweme(2, "视频二")], has_more=0)
        resp = FakeResp(text=_router_data_html(data))
        with (
            mock.patch.object(dy, "_get", return_value=resp),
            _patch_browser() as browser,
        ):
            nickname, videos = await dy.fetch_profile_videos(
                "https://www.douyin.com/user/MS4wLjABAAAAfake"
            )
        self.assertEqual(nickname, "测试博主")
        self.assertEqual([v["id"] for v in videos], ["1", "2"])
        browser.assert_not_called()  # SSR 完整时绝不启动浏览器

    async def test_cookie_from_env_is_used(self):
        data = _loader_data([_aweme(1)], has_more=0)
        resp = FakeResp(text=_router_data_html(data))
        with (
            mock.patch.dict("os.environ", {"DOUYIN_COOKIE": "ttwid=abc; msToken=xyz"}),
            mock.patch.object(dy, "_get", return_value=resp) as fake_get,
        ):
            await dy.fetch_profile_videos(
                "https://www.douyin.com/user/MS4wLjABAAAAfake"
            )
        self.assertEqual(
            fake_get.call_args.kwargs.get("cookie"), "ttwid=abc; msToken=xyz"
        )

    async def test_max_videos_limit(self):
        awemes = [_aweme(i) for i in range(1, 6)]
        data = _loader_data(awemes, has_more=0)
        resp = FakeResp(text=_router_data_html(data))
        with mock.patch.object(dy, "_get", return_value=resp):
            _nick, videos = await dy.fetch_profile_videos(
                "https://www.douyin.com/user/MS4wLjABAAAAfake", max_videos=3
            )
        self.assertEqual(len(videos), 3)

    async def test_env_max_videos_limit(self):
        awemes = [_aweme(i) for i in range(1, 6)]
        data = _loader_data(awemes, has_more=0)
        resp = FakeResp(text=_router_data_html(data))
        with (
            mock.patch.dict("os.environ", {"DOUYIN_MAX_VIDEOS": "2"}),
            mock.patch.object(dy, "_get", return_value=resp),
        ):
            _nick, videos = await dy.fetch_profile_videos(
                "https://www.douyin.com/user/MS4wLjABAAAAfake"
            )
        self.assertEqual(len(videos), 2)

    async def test_blocked_page_falls_back_to_browser(self):
        # 2026 现状：SSR 返回风控页/应用外壳，靠浏览器兜底拿列表
        resp = FakeResp(text="<html>风控验证页</html>")
        with (
            mock.patch.object(dy, "_get", return_value=resp),
            _patch_browser(nickname="兜底博主") as browser,
        ):
            nickname, videos = await dy.fetch_profile_videos(
                "https://www.douyin.com/user/MS4wLjABAAAAfake"
            )
        browser.assert_called_once()
        self.assertEqual(nickname, "兜底博主")
        self.assertEqual([v["id"] for v in videos], ["101", "102"])

    async def test_empty_ssr_falls_back_to_browser(self):
        data = _loader_data([], nickname="空博主")
        resp = FakeResp(text=_router_data_html(data))
        with (
            mock.patch.object(dy, "_get", return_value=resp),
            _patch_browser() as browser,
        ):
            nickname, videos = await dy.fetch_profile_videos(
                "https://www.douyin.com/user/MS4wLjABAAAAfake"
            )
        browser.assert_called_once()
        self.assertEqual([v["id"] for v in videos], ["101", "102"])

    async def test_incomplete_ssr_falls_back_to_browser(self):
        # SSR 只拿到首批且没翻完（has_more=1 但翻页 API 被挡）→ 用浏览器全量列表
        data = _loader_data([_aweme(1)], has_more=1, max_cursor=500)
        resp = FakeResp(text=_router_data_html(data))

        async def empty_page(sec_uid, cursor, cookie):
            return [], None, 0

        with (
            mock.patch.object(dy, "_get", return_value=resp),
            mock.patch.object(dy, "_fetch_post_page", side_effect=empty_page),
            _patch_browser() as browser,
        ):
            _nickname, videos = await dy.fetch_profile_videos(
                "https://www.douyin.com/user/MS4wLjABAAAAfake"
            )
        browser.assert_called_once()
        # 返回的是浏览器的完整列表，不是 SSR 的残缺首批
        self.assertEqual([v["id"] for v in videos], ["101", "102"])

    async def test_all_paths_fail_raises_with_details(self):
        resp = FakeResp(text="<html>风控验证页</html>")
        with (
            mock.patch.object(dy, "_get", return_value=resp),
            _patch_browser(error=dy.DouyinError("playwright 未安装")),
        ):
            with self.assertRaises(dy.DouyinError) as ctx:
                await dy.fetch_profile_videos(
                    "https://www.douyin.com/user/MS4wLjABAAAAfake"
                )
        # 错误信息带两条路径的失败原因
        self.assertIn("SSR", str(ctx.exception))
        self.assertIn("playwright 未安装", str(ctx.exception))

    async def test_on_first_page_fires_once(self):
        data = _loader_data([], nickname="空博主")  # SSR 首批为空不触发
        resp = FakeResp(text=_router_data_html(data))
        seen = []
        with (
            mock.patch.object(dy, "_get", return_value=resp),
            _patch_browser(),
        ):
            await dy.fetch_profile_videos(
                "https://www.douyin.com/user/MS4wLjABAAAAfake",
                on_first_page=lambda n, c: seen.append((n, c)),
            )
        # 只触发一次，且来自浏览器的首批（count=2），不是 SSR 的空批（count=0）
        self.assertEqual(seen, [("浏览器博主", 2)])


class FetchShareVideoInfoTest(unittest.IsolatedAsyncioTestCase):
    def _share_html(self, item=None):
        if item is None:
            item = {
                "aweme_id": "769",
                "desc": "分享页标题",
                "create_time": 1792205880,
                "video": {
                    "duration": 270467,
                    "play_addr": {
                        "url_list": [
                            "https://aweme.snssdk.com/aweme/v1/playwm/?video_id=v0d00&ratio=720p&line=0"
                        ]
                    },
                },
            }
        payload = json.dumps(
            {
                "loaderData": {
                    "video_(id)/page": {"videoInfoRes": {"item_list": [item]}}
                }
            },
            ensure_ascii=False,
        )
        return f"<html><script>window._ROUTER_DATA = {payload};</script></html>"

    async def test_parse_ok(self):
        resp = FakeResp(text=self._share_html())
        with mock.patch.object(dy, "_get_mobile", return_value=resp) as fake_get:
            info = await dy.fetch_share_video_info("769", "ttwid=abc")
        self.assertEqual(
            info,
            {
                "id": "769",
                "title": "分享页标题",
                "create_time": 1792205880,
                "duration": 270,
                "play_urls": [
                    "https://aweme.snssdk.com/aweme/v1/playwm/?video_id=v0d00&ratio=720p&line=0"
                ],
                "is_image": False,
            },
        )
        self.assertIn("m.douyin.com/share/video/769", fake_get.call_args.args[0])
        self.assertEqual(fake_get.call_args.kwargs.get("cookie"), "ttwid=abc")

    async def test_cookie_from_env(self):
        resp = FakeResp(text=self._share_html())
        with (
            mock.patch.dict("os.environ", {"DOUYIN_COOKIE": "ttwid=env"}),
            mock.patch.object(dy, "_get_mobile", return_value=resp) as fake_get,
        ):
            await dy.fetch_share_video_info("769")
        self.assertEqual(fake_get.call_args.kwargs.get("cookie"), "ttwid=env")

    async def test_invalid_id_raises(self):
        with self.assertRaises(dy.DouyinError):
            await dy.fetch_share_video_info("abc")
        with self.assertRaises(dy.DouyinError):
            await dy.fetch_share_video_info("")

    async def test_http_error_raises(self):
        resp = FakeResp(status_code=404)
        with mock.patch.object(dy, "_get_mobile", return_value=resp):
            with self.assertRaises(dy.DouyinError):
                await dy.fetch_share_video_info("769")

    async def test_missing_router_data_raises(self):
        resp = FakeResp(text="<html>验证页</html>")
        with mock.patch.object(dy, "_get_mobile", return_value=resp):
            with self.assertRaises(dy.DouyinError):
                await dy.fetch_share_video_info("769")

    async def test_empty_item_list_raises(self):
        payload = json.dumps(
            {"loaderData": {"video_(id)/page": {"videoInfoRes": {"item_list": []}}}}
        )
        resp = FakeResp(
            text=f"<html><script>window._ROUTER_DATA = {payload};</script></html>"
        )
        with mock.patch.object(dy, "_get_mobile", return_value=resp):
            with self.assertRaises(dy.DouyinError):
                await dy.fetch_share_video_info("769")


class FetchTextTest(unittest.IsolatedAsyncioTestCase):
    async def test_ok(self):
        resp = FakeResp(text="WEBVTT\n\n00:01 --> 00:02\n你好")
        with mock.patch.object(dy, "_get", return_value=resp) as fake_get:
            text = await dy.fetch_text("https://cdn/vtt", cookie="ttwid=abc")
        self.assertEqual(text, "WEBVTT\n\n00:01 --> 00:02\n你好")
        self.assertEqual(fake_get.call_args.kwargs.get("cookie"), "ttwid=abc")

    async def test_http_error_raises(self):
        resp = FakeResp(status_code=403)
        with mock.patch.object(dy, "_get", return_value=resp):
            with self.assertRaises(dy.DouyinError):
                await dy.fetch_text("https://cdn/vtt")


class FetchPostPageTest(unittest.IsolatedAsyncioTestCase):
    async def test_ok(self):
        payload = {
            "status_code": 0,
            "aweme_list": [_aweme(1)],
            "max_cursor": 55,
            "has_more": 1,
        }
        resp = FakeResp(payload=payload)
        with mock.patch.object(dy, "_get", return_value=resp):
            awemes, cursor, has_more = await dy._fetch_post_page("sec", 10, "")
        self.assertEqual(len(awemes), 1)
        self.assertEqual(cursor, 55)
        self.assertEqual(has_more, 1)

    async def test_nonzero_status_stops_pagination(self):
        payload = {"status_code": 8, "aweme_list": []}
        resp = FakeResp(payload=payload)
        with mock.patch.object(dy, "_get", return_value=resp):
            awemes, cursor, has_more = await dy._fetch_post_page("sec", 10, "")
        self.assertEqual((awemes, cursor, has_more), ([], None, 0))

    async def test_http_error_stops_pagination(self):
        resp = FakeResp(status_code=403)
        with mock.patch.object(dy, "_get", return_value=resp):
            awemes, cursor, has_more = await dy._fetch_post_page("sec", 10, "")
        self.assertEqual((awemes, cursor, has_more), ([], None, 0))

    async def test_network_error_stops_pagination(self):
        def boom(url, cookie=""):
            raise dy.DouyinError("无法访问抖音")

        with mock.patch.object(dy, "_get", side_effect=boom):
            awemes, cursor, has_more = await dy._fetch_post_page("sec", 10, "")
        self.assertEqual((awemes, cursor, has_more), ([], None, 0))


if __name__ == "__main__":
    unittest.main()
