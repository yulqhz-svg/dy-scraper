"""
dy-scraper2.2 API 客户端 — 基于 httpx + a_bogus 签名
Playwright 仅用于登录，数据采集全部走 HTTP 请求
"""

import re
import json
import random
import urllib.parse
from pathlib import Path

import execjs
import httpx

BASE_DIR = Path(__file__).parent
DOUYIN_JS_PATH = BASE_DIR / "libs" / "douyin.js"

_douyin_sign_obj = None


def _get_sign_obj():
    global _douyin_sign_obj
    if _douyin_sign_obj is None:
        js_code = open(DOUYIN_JS_PATH, encoding="utf-8-sig").read()
        _douyin_sign_obj = execjs.compile(js_code)
    return _douyin_sign_obj


def get_web_id():
    """生成随机 webid"""

    def e(t):
        if t is not None:
            return str(t ^ (int(16 * random.random()) >> (t // 4)))
        else:
            return "".join(
                [str(int(1e7)), "-", str(int(1e3)), "-", str(int(4e3)), "-", str(int(8e3)), "-", str(int(1e11))]
            )

    web_id = "".join(e(int(x)) if x in "018" else x for x in e(None))
    return web_id.replace("-", "")[:19]


def get_a_bogus(url: str, params: str, user_agent: str) -> str:
    """计算 a_bogus 签名参数"""
    sign_obj = _get_sign_obj()
    sign_js_name = "sign_reply" if "/reply" in url else "sign_datail"
    return sign_obj.call(sign_js_name, params, user_agent)


class DouyinApiClient:
    """抖音 HTTP API 客户端（同步版）"""

    def __init__(self, cookie_str: str, user_agent: str, ms_token: str = ""):
        self._host = "https://www.douyin.com"
        self._webid = get_web_id()
        self._ms_token = ms_token
        self._ua = user_agent
        self.headers = {
            "User-Agent": user_agent,
            "Cookie": cookie_str,
            "Host": "www.douyin.com",
            "Origin": "https://www.douyin.com",
            "Referer": "https://www.douyin.com/",
            "Content-Type": "application/json;charset=UTF-8",
        }
        self._client = httpx.Client(timeout=30)

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _build_params(self, uri: str, params: dict) -> dict:
        """注入公共参数和 a_bogus 签名"""
        common = {
            "device_platform": "webapp",
            "aid": "6383",
            "channel": "channel_pc_web",
            "version_code": "190600",
            "version_name": "19.6.0",
            "update_version_code": "170400",
            "pc_client_type": "1",
            "cookie_enabled": "true",
            "browser_language": "zh-CN",
            "browser_platform": "Win32",
            "browser_name": "Chrome",
            "browser_version": "131.0.0.0",
            "browser_online": "true",
            "engine_name": "Blink",
            "os_name": "Windows",
            "os_version": "10",
            "cpu_core_num": "8",
            "device_memory": "8",
            "engine_version": "131.0.0.0",
            "platform": "PC",
            "screen_width": "1536",
            "screen_height": "864",
            "effective_type": "4g",
            "round_trip_time": "50",
            "webid": self._webid,
            "msToken": self._ms_token,
        }
        params.update(common)
        query_string = urllib.parse.urlencode(params)

        if "/v1/web/general/search" not in uri:
            a_bogus = get_a_bogus(uri, query_string, self._ua)
            params["a_bogus"] = a_bogus

        return params

    def _get(self, uri: str, params: dict = None, headers: dict = None) -> dict:
        if params is None:
            params = {}
        params = self._build_params(uri, params)
        h = headers or self.headers
        resp = self._client.get(f"{self._host}{uri}", params=params, headers=h)
        return resp.json()

    def _post(self, uri: str, data: dict = None) -> dict:
        if data is None:
            data = {}
        params = self._build_params(uri, data)
        return self._client.post(
            f"{self._host}{uri}", params=params, headers=self.headers
        ).json()

    # ---- 用户 API ----

    def get_user_info(self, sec_user_id: str) -> dict:
        return self._get("/aweme/v1/web/user/profile/other/", {
            "sec_user_id": sec_user_id,
            "publish_video_strategy_type": 2,
            "personal_center_strategy": 1,
        })

    def get_user_aweme_posts(self, sec_user_id: str, max_cursor: str = "") -> dict:
        return self._get("/aweme/v1/web/aweme/post/", {
            "sec_user_id": sec_user_id,
            "count": 18,
            "max_cursor": max_cursor,
            "locate_query": "false",
            "publish_video_strategy_type": 2,
        })

    def get_all_user_aweme_posts(self, sec_user_id: str) -> list:
        """获取用户全部视频列表"""
        result = []
        max_cursor = ""
        has_more = 1
        while True:
            resp = self.get_user_aweme_posts(sec_user_id, max_cursor)
            has_more = resp.get("has_more", 0)
            max_cursor = str(resp.get("max_cursor", ""))
            aweme_list = resp.get("aweme_list") or []
            if aweme_list:
                result.extend(aweme_list)
            if not has_more or not aweme_list:
                break
        return result

    # ---- 搜索 API ----

    def search_videos(self, keyword: str, offset: int = 0, count: int = 20) -> dict:
        """搜索视频"""
        return self._get("/aweme/v1/web/general/search/single/", {
            "keyword": keyword,
            "offset": offset,
            "count": count,
            "search_source": "normal_search",
            "query_correct_type": "1",
            "is_filter_search": "0",
            "publish_time": "0",
            "sort_type": "0",
        })

    def get_search_video_list(self, keyword: str, max_count: int = 0) -> list:
        """获取搜索结果全部视频列表，返回 [{video_id, title, url}, ...]"""
        import time

        result = []
        offset = 0
        has_more = 1
        empty_pages = 0

        while True:
            if max_count and len(result) >= max_count:
                break
            if empty_pages >= 5:
                break

            try:
                resp = self.search_videos(keyword, offset)
                data = resp.get("data") or []
                has_more = resp.get("has_more", 0)

                if not data:
                    empty_pages += 1
                    offset += 20
                    time.sleep(0.5)
                    continue

                empty_pages = 0
                for item in data:
                    aweme_info = item.get("aweme_info") or {}
                    aweme_id = str(aweme_info.get("aweme_id", ""))
                    if aweme_id:
                        title = aweme_info.get("desc", "") or aweme_info.get("preview_title", "")
                        result.append({
                            "video_id": aweme_id,
                            "title": title[:200] if title else "",
                            "url": f"https://www.douyin.com/video/{aweme_id}"
                        })
                        if max_count and len(result) >= max_count:
                            break

                if not has_more:
                    break

                offset += 20
                time.sleep(0.5)

            except Exception:
                empty_pages += 1
                time.sleep(1.0)

        return result

    # ---- 视频 API ----

    def get_video_detail(self, aweme_id: str) -> dict:
        headers = self.headers.copy()
        del headers["Origin"]
        return self._get("/aweme/v1/web/aweme/detail/", {"aweme_id": aweme_id}, headers)

    # ---- 评论 API ----

    def get_aweme_comments(self, aweme_id: str, cursor: int = 0,
                           whale_cut_token: str = "") -> dict:
        """获取视频评论，支持 whale_cut_token 透传以突破分页限制"""
        params = {
            "aweme_id": aweme_id,
            "cursor": cursor,
            "count": 50,
            "item_type": 0,
        }
        # 只有拿到真正的 token 才走 whale cut 分页，否则走传统 cursor 分页
        if whale_cut_token:
            params["cut_version"] = 1
            params["whale_cut_token"] = whale_cut_token
        return self._get("/aweme/v1/web/comment/list/", params)

    def get_sub_comments(self, aweme_id: str, comment_id: str, cursor: int = 0) -> dict:
        return self._get("/aweme/v1/web/comment/list/reply/", {
            "comment_id": comment_id,
            "cursor": cursor,
            "count": 20,
            "item_type": 0,
            "item_id": aweme_id,
        })

    def get_all_comments(
        self,
        aweme_id: str,
        max_count: int = 0,
        crawl_interval: float = 0.5,
        fetch_sub_comments: bool = False,
    ) -> list:
        """获取视频全部评论（含子评论）"""
        import time

        result = []
        cursor = 0
        empty_pages = 0
        consecutive_errors = 0
        max_empty_pages = 6

        while True:
            if max_count and len(result) >= max_count:
                break
            if empty_pages >= max_empty_pages:
                break

            try:
                comments_res = self.get_aweme_comments(aweme_id, cursor)
                new_cursor = comments_res.get("cursor", 0)
                comments = comments_res.get("comments") or []

                if not comments:
                    empty_pages += 1
                    consecutive_errors = 0
                    if isinstance(new_cursor, int) and new_cursor > 0:
                        cursor = new_cursor
                    time.sleep(crawl_interval)
                    continue

                empty_pages = 0
                consecutive_errors = 0
                cursor = new_cursor
            except Exception:
                consecutive_errors += 1
                retry_delay = min(5.0, 1.0 * consecutive_errors)
                if consecutive_errors >= 4:
                    empty_pages += 1
                    consecutive_errors = 0
                time.sleep(retry_delay)
                continue

            if max_count and len(result) + len(comments) > max_count:
                comments = comments[: max_count - len(result)]

            result.extend(comments)

            if fetch_sub_comments:
                for comment in comments:
                    if comment.get("reply_comment_total", 0) > 0:
                        sub_result = self._fetch_all_sub_comments(
                            aweme_id, comment.get("cid"), crawl_interval
                        )
                        result.extend(sub_result)

            time.sleep(crawl_interval)

        return result

    def _fetch_all_sub_comments(self, aweme_id: str, comment_id: str, crawl_interval: float) -> list:
        import time

        result = []
        cursor = 0
        has_more = 1

        while True:
            try:
                resp = self.get_sub_comments(aweme_id, comment_id, cursor)
                new_cursor = resp.get("cursor", 0)
                sub_comments = resp.get("comments") or []
                if sub_comments:
                    result.extend(sub_comments)
                    cursor = new_cursor
                else:
                    break
                time.sleep(crawl_interval)
            except Exception:
                time.sleep(1.0)
                continue

        return result
