"""douyin：博主主页 SSR JSON 解析与规整的单元测试（构造 fixture，无网络）。"""

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


def _aweme(aid, desc="视频", create_time=1760000000):
    return {"aweme_id": str(aid), "desc": desc, "create_time": create_time}


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
    def test_valid(self):
        out = dy._normalize_aweme(_aweme(123, "标题", 1760000000))
        self.assertEqual(out, {"id": "123", "title": "标题", "create_time": 1760000000})

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


class FetchProfileVideosTest(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_url_raises(self):
        with self.assertRaises(dy.DouyinError):
            await dy.fetch_profile_videos("https://www.douyin.com/video/123")

    async def test_ssr_flow_without_pagination(self):
        data = _loader_data([_aweme(1, "视频一"), _aweme(2, "视频二")], has_more=0)
        resp = FakeResp(text=_router_data_html(data))
        with mock.patch.object(dy, "_get", return_value=resp) as fake_get:
            nickname, videos = await dy.fetch_profile_videos(
                "https://www.douyin.com/user/MS4wLjABAAAAfake"
            )
        self.assertEqual(nickname, "测试博主")
        self.assertEqual([v["id"] for v in videos], ["1", "2"])
        self.assertEqual(videos[0]["title"], "视频一")
        # DOUYIN_COOKIE 未设置时匿名请求
        with mock.patch.dict("os.environ", {}, clear=True):
            pass
        fake_get.assert_called_once()

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

    async def test_blocked_page_raises(self):
        resp = FakeResp(text="<html>风控验证页</html>")
        with mock.patch.object(dy, "_get", return_value=resp):
            with self.assertRaises(dy.DouyinError):
                await dy.fetch_profile_videos(
                    "https://www.douyin.com/user/MS4wLjABAAAAfake"
                )

    async def test_empty_video_list_raises(self):
        data = _loader_data([], nickname="空博主")
        resp = FakeResp(text=_router_data_html(data))
        with mock.patch.object(dy, "_get", return_value=resp):
            with self.assertRaises(dy.DouyinError):
                await dy.fetch_profile_videos(
                    "https://www.douyin.com/user/MS4wLjABAAAAfake"
                )


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
