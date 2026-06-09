"""
dy-scraper2.2 采集引擎
Playwright 负责登录 + Session 维持，httpx + a_bogus 签名负责数据采集
"""

import re
import json
import time
import random
import sqlite3
from datetime import datetime
from threading import Thread, Event
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from douyin_api import DouyinApiClient

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
EXPORT_DIR = BASE_DIR / "exports"
COOKIE_FILE = DATA_DIR / "cookies.json"
DB_FILE = DATA_DIR / "douyin.db"

# store_region 代码 → 中文名映射
REGION_CODE_MAP = {
    'bj': '北京', 'sh': '上海', 'tj': '天津', 'cq': '重庆',
    'gd': '广东', 'zj': '浙江', 'js': '江苏', 'sc': '四川',
    'sd': '山东', 'ha': '河南', 'hb': '湖北', 'hn': '湖南',
    'fj': '福建', 'ah': '安徽', 'jx': '江西', 'ln': '辽宁',
    'sx': '山西', 'sn': '陕西', 'jl': '吉林', 'hl': '黑龙江',
    'he': '河北', 'gz': '贵州', 'yn': '云南', 'gs': '甘肃',
    'qh': '青海', 'hi': '海南', 'tw': '台湾', 'hk': '香港',
    'mo': '澳门', 'xz': '西藏', 'nx': '宁夏', 'xj': '新疆',
    'nm': '内蒙古', 'gx': '广西',
}

DATA_DIR.mkdir(exist_ok=True)
EXPORT_DIR.mkdir(exist_ok=True)


def load_keywords():
    kw_file = BASE_DIR / "keywords.txt"
    if not kw_file.exists():
        return []
    keywords = []
    with open(kw_file, "r", encoding="utf-8") as f:
        for line in f:
            kw = line.strip()
            if kw:
                keywords.append(kw)
    return keywords


def init_db():
    conn = sqlite3.connect(str(DB_FILE))
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id TEXT NOT NULL,
            video_title TEXT DEFAULT '',
            video_url TEXT DEFAULT '',
            nickname TEXT DEFAULT '',
            douyin_id TEXT DEFAULT '',
            sec_user_id TEXT DEFAULT '',
            region TEXT DEFAULT '',
            content TEXT DEFAULT '',
            comment_time TEXT DEFAULT '',
            like_count INTEGER DEFAULT 0,
            is_intent INTEGER DEFAULT 0,
            matched_keywords TEXT DEFAULT '',
            crawl_time TEXT DEFAULT ''
        )
    """)
    # 兼容旧表：尝试新增列
    for col, dtype in [("sec_user_id", "TEXT DEFAULT ''"), ("region", "TEXT DEFAULT ''")]:
        try:
            cur.execute(f"ALTER TABLE comments ADD COLUMN {col} {dtype}")
        except Exception:
            pass
    cur.execute("""
        CREATE TABLE IF NOT EXISTS videos (
            video_id TEXT PRIMARY KEY,
            video_title TEXT DEFAULT '',
            video_url TEXT DEFAULT '',
            comment_count INTEGER DEFAULT 0,
            crawl_status TEXT DEFAULT 'pending',
            crawl_time TEXT DEFAULT ''
        )
    """)
    conn.commit()
    conn.close()


def get_db():
    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row
    return conn


def save_cookies(context):
    cookies = context.cookies()
    with open(COOKIE_FILE, "w", encoding="utf-8") as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)


def load_cookies(context):
    if not COOKIE_FILE.exists():
        return False
    with open(COOKIE_FILE, "r", encoding="utf-8") as f:
        cookies = json.load(f)
    context.add_cookies(cookies)
    return True


def get_cookie_str(context):
    """从 Playwright context 提取 cookie 字符串"""
    cookies = context.cookies()
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies)


def get_ms_token(page):
    """从 localStorage 提取 msToken"""
    try:
        local_storage = page.evaluate("() => window.localStorage")
        return local_storage.get("xmst") or ""
    except Exception:
        return ""


def extract_video_id(url_or_id: str) -> str:
    sec_match = re.search(r'sec_uid=([A-Za-z0-9_\-\.%]+)', url_or_id)
    if sec_match:
        from urllib.parse import unquote
        return unquote(sec_match.group(1))
    user_match = re.search(r'user/([A-Za-z0-9_\-]+)', url_or_id)
    if user_match:
        return user_match.group(1)
    return url_or_id.strip()


def is_video_url(target: str) -> bool:
    """判断输入是否为单个视频链接（支持 /video/ID、?modal_id=、?vid=、纯数字ID）"""
    target = target.strip()
    if re.search(r'/video/(\d+)', target):
        return True
    if re.search(r'[?&](?:modal_id|vid)=(\d+)', target):
        return True
    return target.isdigit()


def parse_video_url(target: str) -> dict:
    """从视频链接中提取视频信息，返回 {video_id, title, url}"""
    target = target.strip()
    # /video/ID 格式
    m = re.search(r'/video/(\d+)', target)
    if m:
        vid = m.group(1)
        url = target if target.startswith("http") else f"https://www.douyin.com/video/{vid}"
        return {"video_id": vid, "title": "", "url": url}
    # ?modal_id= 或 &vid= 格式（用户主页下点开单个视频）
    m = re.search(r'[?&](?:modal_id|vid)=(\d+)', target)
    if m:
        vid = m.group(1)
        return {"video_id": vid, "title": "", "url": f"https://www.douyin.com/video/{vid}"}
    # 纯数字
    if target.isdigit():
        return {"video_id": target, "title": "", "url": f"https://www.douyin.com/video/{target}"}
    return {}


class CookieStaleError(Exception):
    """Cookie 失效异常 — 触发重新登录"""
    pass


def random_delay(min_s=0.5, max_s=2.0):
    time.sleep(random.uniform(min_s, max_s))


def ts_to_str(ts):
    """Unix 时间戳转字符串"""
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


class DouyinScraper:
    """抖音采集引擎 — API 拦截 + DOM 兜底"""

    def __init__(self):
        self.stop_event = Event()
        self.status = "idle"
        self.progress = {"videos_done": 0, "videos_total": 0, "comments_total": 0, "current": ""}
        self.keywords = load_keywords()
        self._user_region_cache = {}  # sec_uid → 中文地区名

    def set_config(self, **kwargs):
        self.mode = kwargs.get("mode", "auto")  # auto / batch_videos / search
        self.target = kwargs.get("target", "")
        self.video_list = kwargs.get("video_list", "")  # 批量视频：每行一个URL/ID
        self.search_keyword = kwargs.get("search_keyword", "")  # 搜索关键词
        self.search_max_videos = kwargs.get("search_max_videos", 0)
        self.max_videos = kwargs.get("max_videos", 0)
        self.max_comments_per_video = kwargs.get("max_comments_per_video", 0)
        self.scroll_times = kwargs.get("scroll_times", 10)
        self.delay_min = kwargs.get("delay_min", 2.0)
        self.delay_max = kwargs.get("delay_max", 5.0)
        self.headless = kwargs.get("headless", False)
        self.keywords = load_keywords()
        self.tier = kwargs.get("tier", "free")  # free / vip
        # 新增筛选条件
        self.filter_region = kwargs.get("filter_region", "").strip()
        self.filter_time_start = kwargs.get("filter_time_start", "")
        self.filter_time_end = kwargs.get("filter_time_end", "")
        self.filter_text_include = kwargs.get("filter_text_include", "")
        self.filter_text_exclude = kwargs.get("filter_text_exclude", "")
        self.fetch_sub_comments = kwargs.get("fetch_sub_comments", True)  # 是否采集子回复

    def _resolve_user_region(self, api_client, sec_uid):
        """通过用户资料 API 获取地区（store_region），带缓存"""
        if not sec_uid or not api_client:
            return ""
        if sec_uid in self._user_region_cache:
            return self._user_region_cache[sec_uid]
        try:
            profile = api_client.get_user_info(sec_uid)
            user_data = profile.get("user") or {}
            store_region = (user_data.get("store_region") or "").strip()
            if store_region and store_region.startswith("cn-"):
                code = store_region[3:]  # "cn-bj" → "bj"
                region_name = REGION_CODE_MAP.get(code, store_region)
                self._user_region_cache[sec_uid] = region_name
                return region_name
        except Exception:
            pass
        self._user_region_cache[sec_uid] = ""
        return ""

    def check_intent(self, text: str):
        if not text or not self.keywords:
            return []
        matched = []
        for kw in self.keywords:
            if kw in text:
                matched.append(kw)
        return matched

    def save_comment(self, video_id, video_title, video_url, nickname, douyin_id,
                     sec_user_id, region, content, comment_time, like_count, matched_kws):
        conn = get_db()
        try:
            conn.execute("""
                INSERT INTO comments (video_id, video_title, video_url, nickname, douyin_id,
                                      sec_user_id, region, content, comment_time, like_count,
                                      is_intent, matched_keywords, crawl_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                video_id, video_title, video_url, nickname, douyin_id,
                sec_user_id, region, content, comment_time, like_count,
                1 if matched_kws else 0,
                ",".join(matched_kws) if matched_kws else "",
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ))
            conn.commit()
        finally:
            conn.close()

    def save_video(self, video_id, title, url, comment_count=0, status="done"):
        conn = get_db()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO videos (video_id, video_title, video_url,
                                               comment_count, crawl_status, crawl_time)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (video_id, title, url, comment_count, status,
                  datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit()
        finally:
            conn.close()

    def video_already_crawled(self, video_id):
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT crawl_status FROM videos WHERE video_id = ?", (video_id,)
            ).fetchone()
            return row is not None and row["crawl_status"] == "done"
        finally:
            conn.close()

    # ==================== 视频列表采集 ====================

    def scrape_user_videos(self, api_client):
        """抓取用户主页视频列表 — 直接调 API 分页"""
        user_id = extract_video_id(self.target.strip())

        self.progress["current"] = "正在获取用户视频列表..."

        videos_data = []
        seen_ids = set()
        max_cursor = ""
        has_more = 1
        no_progress = 0

        while True:
            if self.stop_event.is_set():
                break
            if self.max_videos and len(videos_data) >= self.max_videos:
                break
            if no_progress >= 5:
                break

            prev_count = len(videos_data)

            try:
                resp = api_client.get_user_aweme_posts(user_id, max_cursor)
                has_more = resp.get("has_more", 0)
                max_cursor = str(resp.get("max_cursor", ""))
                aweme_list = resp.get("aweme_list") or []

                if not aweme_list:
                    no_progress += 1
                    self.progress["current"] = "???????????..."
                    random_delay(self.delay_min, self.delay_max * 2)
                    continue

                for a in aweme_list:
                    aweme_id = str(a.get("aweme_id", ""))
                    if aweme_id and aweme_id not in seen_ids:
                        title = a.get("desc", "") or a.get("preview_title", "")
                        seen_ids.add(aweme_id)
                        videos_data.append({
                            "video_id": aweme_id,
                            "title": title[:200] if title else "",
                            "url": f"https://www.douyin.com/video/{aweme_id}"
                        })
                        if self.max_videos and len(videos_data) >= self.max_videos:
                            break

                self.progress["videos_total"] = len(videos_data)
                self.progress["current"] = f"??? {len(videos_data)} ???..."

                if len(videos_data) == prev_count:
                    no_progress += 1
                else:
                    no_progress = 0

            except Exception as e:
                no_progress += 1
                self.progress["current"] = f"API ????: {e}"

            random_delay(self.delay_min, self.delay_max)

        return videos_data

    # ==================== 评论采集（直接调 API，cursor 分页） ====================

    def scrape_comments(self, api_client, video_info):
        """抓取单个视频评论 — 直接调评论 API，cursor 分页"""
        vid = video_info["video_id"]
        vtitle = video_info["title"]
        vurl = video_info["url"]

        if self.video_already_crawled(vid):
            self.progress["current"] = f"跳过已采集: {vtitle[:30]}"
            return 0

        self.progress["current"] = f"正在采集评论: {vtitle[:30]}..."

        comment_count = 0
        seen_cids = set()
        reply_queue = []  # 累积有子回复的评论 (cid, reply_count)

        cursor = 0
        whale_cut_token = ""
        empty_pages = 0
        consecutive_errors = 0
        max_empty_pages = 10

        while True:
            if self.stop_event.is_set():
                break
            if self.max_comments_per_video and comment_count >= self.max_comments_per_video:
                break
            if empty_pages >= max_empty_pages:
                break

            try:
                resp = api_client.get_aweme_comments(vid, cursor, whale_cut_token)
                has_more = resp.get("has_more", 0)
                new_cursor = resp.get("cursor", 0)
                comments = resp.get("comments") or []

                # 提取 whale_cut_token 用于下一页
                new_token = resp.get("whale_cut_token") or ""

                if not comments:
                    if has_more == 0:
                        break
                    empty_pages += 1
                    consecutive_errors = 0
                    if new_token:
                        whale_cut_token = new_token
                    elif isinstance(new_cursor, int) and new_cursor > 0:
                        cursor = new_cursor
                    else:
                        cursor = cursor + 50
                    random_delay(self.delay_min, self.delay_max * 2)
                    continue

                consecutive_errors = 0
                if new_token:
                    whale_cut_token = new_token
                cursor = new_cursor if new_cursor else cursor + len(comments)

                if self.max_comments_per_video:
                    remaining = self.max_comments_per_video - comment_count
                    if len(comments) > remaining:
                        comments = comments[:remaining]

                new_count, reply_cids = self._process_api_comments(
                    comments, vid, vtitle, vurl, seen_cids, api_client)
                comment_count += new_count
                reply_queue.extend(reply_cids)

                if not has_more and not new_token:
                    break

                if new_count == 0:
                    empty_pages += 1
                else:
                    empty_pages = 0

                self.progress["current"] = (
                    f"已采集 {comment_count} 条评论 "
                    f"({vtitle[:20]}...)"
                )

            except Exception as e:
                consecutive_errors += 1
                retry_delay = min(5.0, 1.0 * consecutive_errors)
                self.progress["current"] = f"评论 API 错误: {e} (重试 #{consecutive_errors})"
                if consecutive_errors >= 6:
                    empty_pages += 1
                    consecutive_errors = 0
                import time
                time.sleep(retry_delay)
                continue

            random_delay(self.delay_min / 2, self.delay_max / 2)

        # ---- 采集子回复 ----
        if self.fetch_sub_comments and reply_queue and not self.stop_event.is_set():
            sub_count = self._fetch_sub_comments(
                api_client, reply_queue, vid, vtitle, vurl, seen_cids)
            comment_count += sub_count

        if self.stop_event.is_set():
            self.save_video(vid, vtitle, vurl, comment_count, "stopped")
        else:
            self.save_video(vid, vtitle, vurl, comment_count, "done")
        return comment_count

    def _fetch_sub_comments(self, api_client, reply_queue, vid, vtitle, vurl, seen_cids):
        """采集子回复（嵌套评论）"""
        sub_count = 0
        total_reply = sum(rc for _, rc in reply_queue)
        done = 0

        for cid, reply_count in reply_queue:
            if self.stop_event.is_set():
                break
            if self.max_comments_per_video and sub_count >= self.max_comments_per_video:
                break

            self.progress["current"] = (
                f"正在采集子回复 ({done + 1}/{len(reply_queue)})，"
                f"已采集 {sub_count} 条..."
            )

            cursor = 0
            has_more = 1
            empty_pages = 0

            while has_more and empty_pages < 3:
                try:
                    resp = api_client.get_sub_comments(vid, cid, cursor)
                    has_more = resp.get("has_more", 0)
                    cursor = resp.get("cursor", 0)
                    replies = resp.get("comments") or []

                    if not replies:
                        empty_pages += 1
                        random_delay(0.3, 0.6)
                        continue

                    empty_pages = 0
                    # 子回复用同样的处理管线
                    reply_count_saved, _ = self._process_api_comments(
                        replies, vid, vtitle, vurl, seen_cids, api_client)
                    sub_count += reply_count_saved

                except Exception:
                    empty_pages += 1
                    time.sleep(0.5)

                random_delay(0.2, 0.4)

            done += 1

        return sub_count

    def _process_api_comments(self, comments, vid, vtitle, vurl, seen_cids, api_client=None):
        """解析评论列表并保存，返回 (saved_count, reply_cids)
        reply_cids: [(cid, reply_count), ...] 有子回复的评论 ID 列表
        """
        count = 0
        reply_cids = []
        for c in comments:
            if self.stop_event.is_set():
                break
            if self.max_comments_per_video and count >= self.max_comments_per_video:
                break

            cid = c.get("cid", "")
            content = (c.get("text") or "").strip()
            if not content:
                continue
            if cid and cid in seen_cids:
                continue
            seen_cids.add(cid)

            # 记录有子回复的评论
            reply_count = c.get("reply_comment_total", 0)
            if reply_count > 0 and self.fetch_sub_comments:
                reply_cids.append((cid, reply_count))

            user = c.get("user") or {}
            nickname = (user.get("nickname") or "").strip()
            douyin_id = (user.get("short_id") or user.get("unique_id") or "").strip()
            sec_user_id = (user.get("sec_uid") or user.get("uid") or "").strip()
            # 地区：评论级 ip_label > 用户资料 store_region > user.region
            region = (
                c.get("ip_label") or c.get("ip_attr") or c.get("user_text")
                or user.get("ip_location") or user.get("ip_attribute")
                or user.get("province") or user.get("city")
                or user.get("addr") or ""
            ).strip()
            # user.region 单独判断："CN"/"cn" 是无效的国家码，不要
            if not region:
                ur = (user.get("region") or "").strip()
                if ur and ur.upper() not in ("CN", ""):
                    region = ur
            # 组合 province + city
            if not region and user.get("province") and user.get("city"):
                region = f"{user['province']} {user['city']}".strip()
            # 仍然为空或仅为"CN" → 通过用户资料 API 获取 store_region
            if (not region or region.upper() == "CN") and sec_user_id and api_client:
                resolved = self._resolve_user_region(api_client, sec_user_id)
                if resolved:
                    region = resolved
            # 最终兜底：如果仍是 CN，则不保存误导性数据
            if region.upper() == "CN":
                region = ""
            comment_time = ts_to_str(c.get("create_time", 0))
            like_count = c.get("digg_count", 0)

            # 应用筛选条件
            if self.filter_region and self.filter_region not in region:
                continue
            if self.filter_time_start and comment_time < self.filter_time_start:
                continue
            if self.filter_time_end and comment_time > self.filter_time_end:
                continue
            if self.filter_text_include:
                includes = [kw.strip() for kw in self.filter_text_include.split(",") if kw.strip()]
                if not any(kw in content for kw in includes):
                    continue
            if self.filter_text_exclude:
                excludes = [kw.strip() for kw in self.filter_text_exclude.split(",") if kw.strip()]
                if any(kw in content for kw in excludes):
                    continue

            matched_kws = self.check_intent(content)
            self.save_comment(vid, vtitle, vurl, nickname, douyin_id,
                              sec_user_id, region,
                              content, comment_time, like_count, matched_kws)
            count += 1
            self.progress["comments_total"] += 1
        return count, reply_cids

    # ==================== 浏览器搜索 ====================

    def _browser_search(self, page, max_videos: int = 0) -> list:
        """通过浏览器搜索页获取视频列表
        拦截 /aweme/v1/web/search/item/ 响应，提取 data[].aweme_info
        """
        from urllib.parse import quote

        keyword = self.search_keyword.strip()
        search_url = f"https://www.douyin.com/search/{quote(keyword)}?type=video"

        videos = []
        seen = set()

        def _add_items(items):
            for item in (items or []):
                aweme = (item.get("aweme_info") or {}) if isinstance(item, dict) else {}
                vid = str(aweme.get("aweme_id", ""))
                if vid and vid not in seen:
                    seen.add(vid)
                    title = aweme.get("desc", "") or aweme.get("preview_title", "")
                    videos.append({
                        "video_id": vid,
                        "title": title[:200] if title else "",
                        "url": f"https://www.douyin.com/video/{vid}"
                    })

        def on_response(response):
            if "/aweme/v1/web/search/item" not in response.url:
                return
            try:
                body = response.json()
                _add_items(body.get("data") or [])
            except Exception:
                pass

        page.on("response", on_response)

        self.progress["current"] = "正在打开搜索结果页..."
        page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
        random_delay(3, 5)

        # 滚动加载更多
        scroll_count = 0
        max_scrolls = max(max_videos // 10, 30) if max_videos else 60
        prev_count = 0
        no_progress = 0

        while scroll_count < max_scrolls:
            if self.stop_event.is_set():
                break
            if max_videos and len(videos) >= max_videos:
                break
            if no_progress >= 8:
                break

            page.evaluate("window.scrollBy(0, window.innerHeight * 0.8)")
            random_delay(1.5, 3)
            scroll_count += 1

            if len(videos) == prev_count and scroll_count > 5:
                no_progress += 1
            else:
                no_progress = 0
            prev_count = len(videos)

            self.progress["current"] = (
                f"正在搜索「{keyword}」... 已收集 {len(videos)} 个视频 "
                f"(滚动 {scroll_count}/{max_scrolls})"
            )

        page.remove_listener("response", on_response)
        return videos

    # ==================== 批量视频解析 ====================

    def _parse_video_list(self, text: str) -> list:
        """解析批量视频输入文本，每行一个 URL 或视频 ID"""
        videos = []
        seen = set()
        for line in text.strip().split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            info = parse_video_url(line)
            if info and info["video_id"] not in seen:
                seen.add(info["video_id"])
                videos.append(info)
        return videos

    # ==================== 主流程 ====================

    def run(self):
        self.stop_event.clear()
        self.status = "running"
        self.progress = {"videos_done": 0, "videos_total": 0, "comments_total": 0, "current": "正在初始化..."}

        mode = self.mode
        target = self.target.strip()

        # ---- 预解析视频列表（batch / search 模式会覆盖） ----
        single_video = {}
        pre_resolved_videos = []  # 提前解析好的视频列表

        if mode == "batch_videos":
            pre_resolved_videos = self._parse_video_list(self.video_list)
            if not pre_resolved_videos:
                self.status = "error"
                self.progress["current"] = "错误：未提供有效的视频链接，请每行输入一个视频URL或ID"
                return
        elif mode == "search":
            if not self.search_keyword.strip():
                self.status = "error"
                self.progress["current"] = "错误：请输入搜索关键词"
                return
        else:  # auto 模式 — 自动判断
            if is_video_url(target):
                single_video = parse_video_url(target)
                if not single_video:
                    self.status = "error"
                    self.progress["current"] = "错误：无法解析视频链接"
                    return
            else:
                user_id = extract_video_id(target)
                if not user_id:
                    self.status = "error"
                    self.progress["current"] = "错误：无法解析博主 ID 或视频链接"
                    return

        try:
            with sync_playwright() as pw:
                self.progress["current"] = "正在启动浏览器..."
                browser = pw.chromium.launch(
                    headless=self.headless,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--disable-infobars",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--window-size=1366,768",
                    ]
                )

                context = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                    viewport={"width": 1366, "height": 768},
                    locale="zh-CN",
                )

                context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', { get: () => false });
                    Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh'] });
                    Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
                    Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });

                    Object.defineProperty(navigator, 'plugins', {
                        get: () => {
                            const arr = [
                                { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
                                { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
                                { name: 'Native Client', filename: 'internal-nacl-plugin' },
                            ];
                            arr.item = i => arr[i];
                            arr.namedItem = n => arr.find(p => p.name === n);
                            arr.refresh = () => {};
                            return arr;
                        }
                    });

                    window.chrome = {
                        runtime: {},
                        loadTimes: function() { return {}; },
                        csi: function() { return {}; },
                        app: {},
                    };

                    const origQuery = navigator.permissions.query.bind(navigator.permissions);
                    navigator.permissions.query = (params) => {
                        if (params.name === 'notifications') {
                            return Promise.resolve({ state: 'prompt', onchange: null });
                        }
                        return origQuery(params);
                    };
                """)

                page = context.new_page()

                # ---- 登录 ----
                cookie_loaded = load_cookies(context)

                def do_login():
                    self.progress["current"] = "未检测到登录状态，请在弹出的浏览器中手动登录抖音..."
                    page.goto("https://www.douyin.com", wait_until="domcontentloaded", timeout=30000)
                    self.progress["current"] = "请在浏览器中扫码登录抖音，登录成功后程序自动继续..."
                    try:
                        page.wait_for_selector(
                            '[data-e2e="user-avatar"], [class*="avatar"], [class*="Avatar"]',
                            timeout=120000)
                        save_cookies(context)
                        self.progress["current"] = "登录成功，已保存登录状态"
                        return True
                    except PWTimeout:
                        self.progress["current"] = "登录超时，将使用当前状态继续"
                        return False

                if not cookie_loaded:
                    do_login()

                user_agent = page.evaluate("() => navigator.userAgent")

                # ========== 搜索模式：先搜后取凭证（避免 douyin.com 首页污染 session） ==========
                if mode == "search":
                    search_max = self.search_max_videos or self.max_videos
                    videos = self._browser_search(page, search_max)

                    if not videos:
                        self.progress["current"] = (
                            f"未搜索到「{self.search_keyword}」相关视频，请尝试更换关键词")
                        self.status = "error"
                        return

                    # 从搜索结果页提取凭证（页面已在 douyin.com 域名下）
                    cookie_str = get_cookie_str(context)
                    ms_token = get_ms_token(page)

                    api_client = DouyinApiClient(
                        cookie_str=cookie_str,
                        user_agent=user_agent,
                        ms_token=ms_token,
                    )

                    self.progress["current"] = (
                        f"搜索到 {len(videos)} 条「{self.search_keyword}」相关视频，"
                        "开始逐条采集评论..."
                    )

                else:
                    # ========== 非搜索模式：先取凭证再采集 ==========
                    cookie_str = get_cookie_str(context)
                    ms_token = get_ms_token(page)

                    if not ms_token:
                        page.goto("https://www.douyin.com", wait_until="domcontentloaded", timeout=30000)
                        random_delay(2, 3)
                        ms_token = get_ms_token(page)
                        cookie_str = get_cookie_str(context)

                    api_client = DouyinApiClient(
                        cookie_str=cookie_str,
                        user_agent=user_agent,
                        ms_token=ms_token,
                    )

                    # 检查登录状态
                    try:
                        local_storage = page.evaluate("() => window.localStorage")
                        is_logged_in = (
                            local_storage.get("HasUserLogin") == "1"
                            or "LOGIN_STATUS=1" in cookie_str
                        )
                        if not is_logged_in:
                            raise CookieStaleError("未登录")
                    except CookieStaleError:
                        self.progress["current"] = "Cookie 失效，正在重新登录..."
                        COOKIE_FILE.unlink(missing_ok=True)
                        do_login()
                        cookie_str = get_cookie_str(context)
                        ms_token = get_ms_token(page)
                        api_client = DouyinApiClient(
                            cookie_str=cookie_str,
                            user_agent=user_agent,
                            ms_token=ms_token,
                        )

                    videos = []

                    if mode == "batch_videos":
                        videos = pre_resolved_videos
                        self.progress["current"] = (
                            f"批量模式：共 {len(videos)} 个视频，开始逐条采集评论..."
                        )

                    elif single_video:
                        videos = [single_video]

                    else:
                        # auto 模式 — 用户主页
                        page.goto("https://www.douyin.com", wait_until="domcontentloaded", timeout=30000)
                        random_delay(2, 3)

                        videos = self.scrape_user_videos(api_client)

                        if not videos:
                            self.progress["current"] = (
                                "未获取到视频列表，可能原因：博主 ID 错误 / 私密账号 / "
                                "需登录后查看")
                            self.status = "error"
                            api_client.close()
                            return

                # 限制视频数量
                max_v = self.search_max_videos or self.max_videos
                if self.tier == "free":
                    max_v = min(max_v, 2) if max_v else 2
                if max_v and len(videos) > max_v:
                    videos = videos[:max_v]

                self.progress["videos_total"] = len(videos)
                self.progress["current"] = (
                    f"共 {len(videos)} 条视频，开始逐条采集评论..."
                )

                # ========== 逐条采集评论 ==========
                cookies_refreshed = False
                for idx, video in enumerate(videos):
                    if self.stop_event.is_set():
                        break
                    if self.max_videos and idx >= self.max_videos:
                        break

                    self.progress["videos_done"] = idx + 1
                    try:
                        self.scrape_comments(api_client, video)
                    except CookieStaleError:
                        if not cookies_refreshed:
                            self.progress["current"] = "Cookie 失效，正在重新登录..."
                            COOKIE_FILE.unlink(missing_ok=True)
                            do_login()
                            cookie_str = get_cookie_str(context)
                            ms_token = get_ms_token(page)
                            api_client = DouyinApiClient(
                                cookie_str=cookie_str,
                                user_agent=user_agent,
                                ms_token=ms_token,
                            )
                            cookies_refreshed = True
                            try:
                                self.scrape_comments(api_client, video)
                            except CookieStaleError:
                                self.progress["current"] = (
                                    f"重新登录后仍失败，跳过: {video['title'][:30]}")
                        else:
                            self.progress["current"] = (
                                f"Cookie 失效，跳过: {video['title'][:30]}")
                    except Exception as e:
                        self.progress["current"] = (
                            f"采集失败，跳过: {video['title'][:30]} — {e}")

                    delay = random.uniform(self.delay_min, self.delay_max)
                    self.progress["current"] = f"等待 {delay:.1f} 秒后采集下一条..."
                    time.sleep(delay)

                self.status = "stopped" if self.stop_event.is_set() else "completed"
                self.progress["current"] = (
                    "采集已停止" if self.stop_event.is_set()
                    else f"采集完成！共 {self.progress['videos_done']} 条视频，"
                         f"{self.progress['comments_total']} 条评论")

                api_client.close()

                try:
                    save_cookies(context)
                except Exception:
                    pass
                try:
                    browser.close()
                except Exception:
                    pass

        except Exception as e:
            self.status = "error"
            self.progress["current"] = f"采集出错: {str(e)}"

    def start(self):
        t = Thread(target=self.run, daemon=True)
        t.start()

    def stop(self):
        self.stop_event.set()
        self.status = "stopped"


scraper = DouyinScraper()
