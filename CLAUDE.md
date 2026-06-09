# dy-scraper2.2

抖音评论采集工具。Playwright 负责登录与会话维持，httpx + a_bogus 签名负责数据采集，远程 SCF License 验证实现免费/会员分级。

## 项目结构

```
dy-scraper2.2/

├── data/
│   ├── douyin.db               # SQLite 数据库（comments + videos）
│   ├── cookies.json            # 抖音登录态 cookies
│   └── chrome_profile/         # Playwright 持久化浏览器 Profile
└── exports/                    # Excel 导出目录
```

## 启动方式

```bash
pip install -r requirements.txt
playwright install chromium
python app.py            # → http://127.0.0.1:8080
```

## 采集模式

1. **博主主页模式**：输入博主 ID/链接 → 抓取用户全量视频列表 → 逐条采集评论
2. **指定视频模式**：粘贴多个视频链接 / 导入 .TXT 文件 → 批量采集（会员功能）
3. **搜索结果模式**：输入搜索关键词 → 抓取搜索结果视频列表 → 采集评论（会员功能）

## 权限分级

| 功能 | 免费版 | 会员版 |
|------|--------|--------|
| 单个视频评论采集 | 支持（最多 2 条视频） | 无限制 |
| 博主主页全量采集 | 不支持 | 支持 |
| 搜索结果采集 | 不支持 | 支持 |
| 指定视频批量采集 | 不支持 | 支持 |
| 子回复采集 | 支持 | 支持 |
| 地区/时间/文本筛选 | 支持 | 支持 |
| Excel 导出 | 支持 | 支持 |


