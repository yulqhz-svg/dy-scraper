"""
dy-scraper2.2 - Flask 后端
"""

from pathlib import Path
from datetime import datetime

from flask import Flask, request, jsonify, send_file, render_template, send_from_directory
from flask_cors import CORS
import xlsxwriter

from scraper import scraper, init_db, get_db, load_keywords, BASE_DIR, parse_video_url, is_video_url
from src.license.license_manager import license_manager

app = Flask(__name__)
CORS(app)

init_db()

KEYWORDS_FILE = BASE_DIR / "keywords.txt"


# ==================== 页面路由 ====================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/img/<path:filename>")
def serve_img(filename):
    return send_from_directory(BASE_DIR / "img", filename)


# ==================== API 路由 ====================

@app.route("/api/start", methods=["POST"])
def api_start():
    """启动采集"""
    if scraper.status == "running":
        return jsonify({"ok": False, "msg": "采集任务已在运行中"})

    data = request.get_json() or {}
    mode = data.get("mode", "auto")

    # ---- License 权限校验 ----
    lic = license_manager.verify()
    tier = lic.get("tier", "free")

    if mode == "search" and tier == "free":
        return jsonify({"ok": False, "msg": "搜索结果采集为会员功能，请先激活会员"})

    scraper.set_config(
        mode=mode,
        target=data.get("target", ""),
        video_list=data.get("video_list", ""),
        search_keyword=data.get("search_keyword", ""),
        search_max_videos=int(data.get("search_max_videos", 0)),
        max_videos=int(data.get("max_videos", 0)),
        max_comments_per_video=int(data.get("max_comments_per_video", 0)),
        scroll_times=int(data.get("scroll_times", 10)),
        delay_min=float(data.get("delay_min", 2.0)),
        delay_max=float(data.get("delay_max", 5.0)),
        headless=bool(data.get("headless", False)),
        filter_region=data.get("filter_region", ""),
        filter_time_start=data.get("filter_time_start", ""),
        filter_time_end=data.get("filter_time_end", ""),
        filter_text_include=data.get("filter_text_include", ""),
        filter_text_exclude=data.get("filter_text_exclude", ""),
        fetch_sub_comments=bool(data.get("fetch_sub_comments", True)),
        tier=tier,
    )

    # 验证必填项
    if mode == "batch_videos":
        if tier == "free":
            return jsonify({"ok": False, "msg": "指定视频采集为会员功能，请先激活会员"})
        if not scraper.video_list.strip():
            return jsonify({"ok": False, "msg": "请输入要采集的视频链接（每行一个）"})
        video_count = len(scraper._parse_video_list(scraper.video_list))
        if video_count == 0:
            return jsonify({"ok": False, "msg": "未识别到有效的视频链接"})
    elif mode == "search":
        if not scraper.search_keyword.strip():
            return jsonify({"ok": False, "msg": "请输入搜索关键词"})
    else:
        if not scraper.target:
            return jsonify({"ok": False, "msg": "请输入博主主页链接、博主ID、或视频链接"})

        if tier == "free":
            if not is_video_url(scraper.target.strip()):
                return jsonify({"ok": False, "msg": "免费版仅支持单个视频评论采集，会员版支持博主主页全量采集"})

    scraper.start()
    return jsonify({"ok": True, "msg": "采集任务已启动"})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    """停止采集"""
    scraper.stop()
    return jsonify({"ok": True, "msg": "已发送停止信号"})


@app.route("/api/status", methods=["GET"])
def api_status():
    """获取采集状态"""
    return jsonify({
        "ok": True,
        "status": scraper.status,
        "progress": scraper.progress,
    })


@app.route("/api/keywords", methods=["GET"])
def api_get_keywords():
    """获取关键词列表"""
    kws = load_keywords()
    return jsonify({"ok": True, "keywords": kws, "count": len(kws)})


@app.route("/api/keywords", methods=["POST"])
def api_save_keywords():
    """保存关键词列表"""
    data = request.get_json() or {}
    kws = data.get("keywords", [])
    with open(KEYWORDS_FILE, "w", encoding="utf-8") as f:
        for kw in kws:
            kw = kw.strip()
            if kw:
                f.write(kw + "\n")
    scraper.keywords = load_keywords()
    return jsonify({"ok": True, "msg": f"已保存 {len(kws)} 个关键词"})


@app.route("/api/stats", methods=["GET"])
def api_stats():
    """获取统计信息"""
    conn = get_db()
    try:
        total = conn.execute("SELECT COUNT(*) as c FROM comments").fetchone()["c"]
        intent = conn.execute("SELECT COUNT(*) as c FROM comments WHERE is_intent = 1").fetchone()["c"]
        videos = conn.execute("SELECT COUNT(*) as c FROM videos WHERE crawl_status = 'done'").fetchone()["c"]
        return jsonify({
            "ok": True,
            "total_comments": total,
            "intent_comments": intent,
            "videos_crawled": videos,
        })
    finally:
        conn.close()


@app.route("/api/comments", methods=["GET"])
def api_comments():
    """查询评论列表"""
    intent_only = request.args.get("intent", "0")
    page = int(request.args.get("page", 1))
    per_page = min(int(request.args.get("per_page", 50)), 500)
    offset = (page - 1) * per_page

    conn = get_db()
    try:
        if intent_only == "1":
            where = "WHERE is_intent = 1"
            count_sql = "SELECT COUNT(*) as c FROM comments WHERE is_intent = 1"
        else:
            where = ""
            count_sql = "SELECT COUNT(*) as c FROM comments"

        total = conn.execute(count_sql).fetchone()["c"]
        rows = conn.execute(
            f"SELECT * FROM comments {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            (per_page, offset)
        ).fetchall()
        return jsonify({
            "ok": True,
            "total": total,
            "page": page,
            "per_page": per_page,
            "data": [dict(r) for r in rows],
        })
    finally:
        conn.close()


@app.route("/api/export", methods=["GET"])
def api_export():
    """导出 Excel（使用 xlsxwriter，超 6 万行自动拆分为多个文件并打包 ZIP）"""
    import zipfile
    import os

    MAX_ROWS = 60000

    conn = get_db()
    try:
        total_all = conn.execute("SELECT COUNT(*) as c FROM comments").fetchone()["c"]
        total_intent = conn.execute(
            "SELECT COUNT(*) as c FROM comments WHERE is_intent = 1").fetchone()["c"]
    finally:
        conn.close()

    if not total_all:
        return jsonify({"ok": False, "msg": "没有可导出的数据"})

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    num_files = max(1, (total_all + MAX_ROWS - 1) // MAX_ROWS)

    # ---- 预定义格式 ----
    def _make_formats(wb):
        return (
            wb.add_format({
                "font_name": "微软雅黑", "bold": True, "font_color": "#FFFFFF",
                "font_size": 11, "bg_color": "#4472C4",
                "align": "center", "valign": "vcenter", "border": 1,
            }),
            wb.add_format({
                "font_name": "微软雅黑", "font_size": 10,
                "valign": "vcenter", "text_wrap": True, "border": 1,
            }),
            wb.add_format({
                "font_name": "微软雅黑", "font_size": 10,
                "valign": "vcenter", "text_wrap": True, "border": 1,
                "bg_color": "#FFF2CC",
            }),
            wb.add_format({
                "font_name": "微软雅黑", "font_size": 12, "font_color": "#999999",
            }),
        )

    headers = ["评论人昵称", "抖音ID", "加密用户ID", "地区", "评论内容", "评论时间", "点赞数",
               "视频标题", "视频链接", "命中关键词", "是否意向客户", "采集时间"]
    col_widths = [16, 20, 28, 12, 50, 18, 10, 30, 35, 20, 14, 20]

    def _build_sheet(ws, cursor, header_fmt, cell_fmt, intent_fmt, is_intent_sheet=False):
        for ci, h in enumerate(headers):
            ws.write(0, ci, h, header_fmt)
            ws.set_column(ci, ci, col_widths[ci])
        row_idx = 1
        for row in cursor:
            values = [
                row["nickname"], row["douyin_id"], row["sec_user_id"], row["region"],
                row["content"], row["comment_time"],
                row["like_count"], row["video_title"], row["video_url"],
                row["matched_keywords"], "是" if row["is_intent"] else "否", row["crawl_time"],
            ]
            fmt = intent_fmt if (not is_intent_sheet and row["is_intent"]) else cell_fmt
            ws.write_row(row_idx, 0, values, fmt)
            row_idx += 1
        if row_idx > 1:
            ws.freeze_panes(1, 0)
            ws.autofilter(0, 0, row_idx - 1, len(headers) - 1)
        return row_idx

    # ---- 单文件（≤6 万行）直接下载 ----
    if num_files == 1:
        filename = f"dy-scraper2.2_{ts}.xlsx"
        filepath = BASE_DIR / "exports" / filename

        wb = xlsxwriter.Workbook(str(filepath), {"constant_memory": True, "strings_to_urls": False})
        try:
            header_fmt, cell_fmt, intent_fmt, empty_fmt = _make_formats(wb)

            ws_all = wb.add_worksheet("全部评论")
            conn = get_db()
            try:
                cursor = conn.execute("SELECT * FROM comments ORDER BY id DESC")
                _build_sheet(ws_all, cursor, header_fmt, cell_fmt, intent_fmt)
            finally:
                conn.close()

            ws_intent = wb.add_worksheet("意向客户")
            conn = get_db()
            try:
                cursor = conn.execute("SELECT * FROM comments WHERE is_intent = 1 ORDER BY id DESC")
                ic = _build_sheet(ws_intent, cursor, header_fmt, cell_fmt, intent_fmt, is_intent_sheet=True)
            finally:
                conn.close()
            if ic <= 1:
                ws_intent.write("A1", "暂无符合关键词的意向评论", empty_fmt)
        finally:
            wb.close()
        return send_file(str(filepath), as_attachment=True, download_name=filename,
                        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    # ---- 多文件（>6 万行）拆分为 ZIP ----
    rows_per_file = (total_all + num_files - 1) // num_files
    intent_per_file = (total_intent + num_files - 1) // num_files

    xlsx_files = []
    try:
        for part in range(num_files):
            part_no = part + 1
            filename = f"dy-scraper2.2_{ts}_part{part_no}.xlsx"
            filepath = BASE_DIR / "exports" / filename

            wb = xlsxwriter.Workbook(str(filepath), {"constant_memory": True, "strings_to_urls": False})
            try:
                header_fmt, cell_fmt, intent_fmt, empty_fmt = _make_formats(wb)

                offset = part * rows_per_file
                ws_all = wb.add_worksheet("全部评论")
                conn = get_db()
                try:
                    cursor = conn.execute(
                        "SELECT * FROM comments ORDER BY id DESC LIMIT ? OFFSET ?",
                        (rows_per_file, offset))
                    _build_sheet(ws_all, cursor, header_fmt, cell_fmt, intent_fmt)
                finally:
                    conn.close()

                intent_offset = part * intent_per_file
                ws_intent = wb.add_worksheet("意向客户")
                conn = get_db()
                try:
                    cursor = conn.execute(
                        "SELECT * FROM comments WHERE is_intent = 1 ORDER BY id DESC LIMIT ? OFFSET ?",
                        (intent_per_file, intent_offset))
                    ic = _build_sheet(ws_intent, cursor, header_fmt, cell_fmt, intent_fmt, is_intent_sheet=True)
                finally:
                    conn.close()
                if ic <= 1:
                    ws_intent.write("A1", "暂无符合关键词的意向评论", empty_fmt)
            finally:
                wb.close()
            xlsx_files.append(filepath)
    except Exception:
        for fp in xlsx_files:
            os.remove(str(fp))
        raise

    # 打包 ZIP
    zip_filename = f"dy-scraper2.2_{ts}.zip"
    zip_path = BASE_DIR / "exports" / zip_filename
    with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED) as zf:
        for fp in xlsx_files:
            zf.write(str(fp), fp.name)
    for fp in xlsx_files:
        os.remove(str(fp))

    return send_file(str(zip_path), as_attachment=True, download_name=zip_filename,
                    mimetype="application/zip")


@app.route("/api/upload-video-list", methods=["POST"])
def api_upload_video_list():
    """上传包含视频链接的 .txt 文件，返回解析后的视频列表"""
    if "file" not in request.files:
        return jsonify({"ok": False, "msg": "未选择文件"})

    f = request.files["file"]
    if f.filename == "":
        return jsonify({"ok": False, "msg": "文件名为空"})

    ext = Path(f.filename).suffix.lower()
    if ext not in (".txt", ".csv"):
        return jsonify({"ok": False, "msg": "仅支持 .txt 或 .csv 文件"})

    try:
        text = f.read().decode("utf-8-sig")
    except Exception:
        text = f.read().decode("gbk", errors="ignore")

    videos = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # CSV 格式取第一列
        if ext == ".csv" and "," in line:
            line = line.split(",")[0].strip().strip('"')
        info = parse_video_url(line)
        if info:
            info["_source"] = line
            videos.append(info)

    return jsonify({
        "ok": True,
        "count": len(videos),
        "videos": [{"video_id": v["video_id"], "url": v["url"], "source": v.get("_source", "")}
                    for v in videos],
        "raw_text": text.strip(),
    })


@app.route("/api/parse-video-list", methods=["POST"])
def api_parse_video_list():
    """解析粘贴的视频列表文本"""
    data = request.get_json() or {}
    text = data.get("text", "")

    videos = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        info = parse_video_url(line)
        if info:
            info["_source"] = line
            videos.append(info)

    return jsonify({
        "ok": True,
        "count": len(videos),
        "videos": [{"video_id": v["video_id"], "url": v["url"], "source": v.get("_source", "")}
                    for v in videos],
    })


@app.route("/api/clear-data", methods=["POST"])
def api_clear_data():
    """清空采集数据"""
    conn = get_db()
    try:
        conn.execute("DELETE FROM comments")
        conn.execute("DELETE FROM videos")
        conn.commit()
        return jsonify({"ok": True, "msg": "数据已清空"})
    finally:
        conn.close()


@app.route("/api/license/status", methods=["GET"])
def api_license_status():
    """获取授权状态"""
    lic = license_manager.verify()
    return jsonify({
        "ok": True,
        "tier": lic.get("tier", "free"),
        "valid": lic.get("valid", False),
        "expiry": lic.get("expiry"),
        "offline": lic.get("offline", False),
        "reason": lic.get("reason", ""),
        "machineCode": license_manager.machine_code,
    })


@app.route("/api/license/activate", methods=["POST"])
def api_license_activate():
    """激活 License"""
    data = request.get_json() or {}
    license_key = (data.get("licenseKey") or "").strip()
    if not license_key:
        return jsonify({"ok": False, "msg": "请输入 License Key"})
    result = license_manager.activate(license_key)
    if result.get("success"):
        return jsonify({"ok": True, "msg": "激活成功", "expiry": result["expiry"]})
    return jsonify({"ok": False, "msg": result.get("error", "激活失败")})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8080, debug=False)
