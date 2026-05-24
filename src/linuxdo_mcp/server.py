"""linux.do (Discourse) MCP 服务器。

通过 curl_cffi 模拟 Chrome TLS 指纹绕过 Cloudflare，用 _t cookie 认证。
认证：环境变量 LINUXDO_COOKIE = "_t=你的token值"（每个用户用自己的）。

暴露工具：whoami / search / get_topic。
"""
import html
import json
import os
import re
import time
import urllib.parse

from curl_cffi import requests as creq
from mcp.server.fastmcp import FastMCP

BASE = "https://linux.do"
IMPERSONATE = os.environ.get("LINUXDO_IMPERSONATE", "chrome")

mcp = FastMCP("linuxdo")


def _cookie_header():
    raw = os.environ.get("LINUXDO_COOKIE", "").strip()
    if not raw:
        raise RuntimeError("未设置 LINUXDO_COOKIE 环境变量（值形如 _t=xxxxx）。")
    return raw if "=" in raw else f"_t={raw}"


def _blocked(body):
    return "Just a moment" in body[:600] or "challenge-platform" in body[:2000]


def _fetch(path):
    url = path if path.startswith("http") else BASE + path
    headers = {"Accept": "application/json", "Cookie": _cookie_header()}
    last = ""
    for attempt in range(3):
        try:
            r = creq.get(url, headers=headers, impersonate=IMPERSONATE, timeout=30)
        except Exception as e:
            last = f"请求失败：{e}"
            time.sleep(0.8 * (attempt + 1))
            continue
        body = r.text
        if _blocked(body):
            last = "被 Cloudflare 拦截"
            time.sleep(0.8 * (attempt + 1))
            continue
        if r.status_code in (401, 403):
            raise RuntimeError(f"认证失败({r.status_code})：cookie 可能已过期，请重新导出 _t。")
        if r.status_code == 429:
            raise RuntimeError("被限流(429)：请降低频率，稍后重试。")
        if r.status_code != 200 or not body.lstrip().startswith(("{", "[")):
            raise RuntimeError(f"异常响应 HTTP {r.status_code}: {body[:200]}")
        return json.loads(body)
    raise RuntimeError(f"{last}（已重试 3 次）。可设 LINUXDO_IMPERSONATE=chrome131 换指纹。")


def _strip_html(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def _topic_url(slug, tid):
    return f"{BASE}/t/{slug or 'topic'}/{tid}"


def _whoami():
    u = _fetch("/session/current.json").get("current_user") or {}
    if not u:
        raise RuntimeError("未登录（cookie 无效或为空）。")
    return {k: u.get(k) for k in ("username", "name", "trust_level", "admin", "moderator")}


def _search(query, page, pages):
    seen, results, last = set(), [], None
    for i in range(pages):
        q = urllib.parse.quote(query)
        last = _fetch(f"/search.json?q={q}&page={page + i}")
        topics = {t["id"]: t for t in last.get("topics", [])}
        cats = {c["id"]: c.get("name") for c in last.get("categories", [])}
        for p in last.get("posts", []):
            tid = p.get("topic_id")
            if tid in seen:
                continue
            seen.add(tid)
            t = topics.get(tid, {})
            results.append({
                "topic_id": tid,
                "title": t.get("title"),
                "category": cats.get(t.get("category_id")),
                "tags": [tag.get("name") for tag in (t.get("tags") or [])],
                "blurb": _strip_html(p.get("blurb")),
                "posts_count": t.get("posts_count"),
                "created_at": t.get("created_at"),
                "url": _topic_url(t.get("slug"), tid),
            })
        if not (last.get("grouped_search_result") or {}).get("more_full_page_results"):
            break
        if i + 1 < pages:
            time.sleep(0.6)
    gsr = (last or {}).get("grouped_search_result") or {}
    return {"term": gsr.get("term"), "count": len(results),
            "more_results": gsr.get("more_full_page_results", False), "results": results}


def _topic(topic_id, posts, start):
    j = _fetch(f"/t/{topic_id}.json")
    stream = (j.get("post_stream") or {}).get("stream", [])
    have = {p["id"]: p for p in (j.get("post_stream") or {}).get("posts", [])}
    # 从第 start 楼(1-based)起取 posts 个楼层；Discourse 首批只回前 ~20 楼，
    # 其余按 stream 里的 id 分批补抓（每批 20 个）。
    want_ids = stream[max(start - 1, 0):max(start - 1, 0) + posts]
    missing = [pid for pid in want_ids if pid not in have]
    for i in range(0, len(missing), 20):
        chunk = missing[i:i + 20]
        qs = "&".join(f"post_ids[]={pid}" for pid in chunk)
        extra = _fetch(f"/t/{topic_id}/posts.json?{qs}")
        for p in (extra.get("post_stream") or {}).get("posts", []):
            have[p["id"]] = p
        if i + 20 < len(missing):
            time.sleep(0.4)
    ordered = [have[pid] for pid in want_ids if pid in have]
    return {
        "id": j.get("id"),
        "title": j.get("title"),
        "category_id": j.get("category_id"),
        "tags": [t.get("name") if isinstance(t, dict) else t for t in (j.get("tags") or [])],
        "posts_count": j.get("posts_count"),
        "total_posts": len(stream),
        "start": start,
        "returned": len(ordered),
        "views": j.get("views"),
        "like_count": j.get("like_count"),
        "url": _topic_url(j.get("slug"), j.get("id")),
        "posts": [{
            "floor": p.get("post_number"),
            "username": p.get("username"),
            "created_at": p.get("created_at"),
            "content": _strip_html(p.get("cooked")),
        } for p in ordered],
    }


def _categories():
    cats = (_fetch("/categories.json").get("category_list") or {}).get("categories", [])
    return [{
        "id": c.get("id"),
        "slug": c.get("slug"),
        "name": c.get("name"),
        "topic_count": c.get("topic_count"),
        "post_count": c.get("post_count"),
        "minimum_required_trust_level": c.get("minimum_required_trust_level"),
        "description": _strip_html(c.get("description_text") or c.get("description")),
    } for c in cats]


def _category_topics(category_id, page):
    cat = next((c for c in _categories() if c["id"] == category_id), None)
    if not cat:
        raise RuntimeError(f"找不到类别 id={category_id}，请先用 list_categories 查看可用类别。")
    tl = _fetch(f"/c/{cat['slug']}/{category_id}.json?page={page}").get("topic_list") or {}
    topics = tl.get("topics", [])
    return {
        "category": cat["name"],
        "category_id": category_id,
        "topic_count": cat["topic_count"],
        "page": page,
        "returned": len(topics),
        "more": bool(tl.get("more_topics_url")),
        "topics": [_topic_brief(t) for t in topics],
    }


def _topic_brief(t):
    return {
        "id": t.get("id"),
        "title": t.get("title"),
        "category_id": t.get("category_id"),
        "posts_count": t.get("posts_count"),
        "views": t.get("views"),
        "like_count": t.get("like_count"),
        "created_at": t.get("created_at"),
        "url": _topic_url(t.get("slug"), t.get("id")),
    }


def _topics_page(path, extra):
    tl = _fetch(path).get("topic_list") or {}
    topics = tl.get("topics", [])
    return {**extra, "returned": len(topics),
            "more": bool(tl.get("more_topics_url")),
            "topics": [_topic_brief(t) for t in topics]}


def _tags():
    tags = _fetch("/tags.json").get("tags", [])
    return [{"name": t.get("name"), "count": t.get("count"),
             "description": t.get("description")} for t in tags]


def _tag_topics(tag, page):
    return _topics_page(f"/tag/{urllib.parse.quote(tag)}.json?page={page}",
                        {"tag": tag, "page": page})


def _user_info(username):
    uq = urllib.parse.quote(username)
    u = _fetch(f"/u/{uq}.json").get("user") or {}
    if not u:
        raise RuntimeError(f"找不到用户 {username}。")
    info = {k: u.get(k) for k in
            ("username", "name", "trust_level", "title", "created_at", "last_seen_at", "badge_count")}
    summary = _fetch(f"/u/{uq}/summary.json").get("user_summary") or {}
    info.update({k: summary.get(k) for k in
                 ("topic_count", "post_count", "likes_given", "likes_received",
                  "days_visited", "solved_count")})
    return info


TOP_PERIODS = ("daily", "weekly", "monthly", "quarterly", "yearly", "all")


def _top(period, page):
    if period not in TOP_PERIODS:
        raise RuntimeError(f"period 须为 {list(TOP_PERIODS)} 之一。")
    return _topics_page(f"/top.json?period={period}&page={page}",
                        {"period": period, "page": page})


def _user_actions(username, limit):
    u = urllib.parse.quote(username)
    acts = _fetch(f"/user_actions.json?offset=0&username={u}&filter=4,5").get("user_actions", [])[:limit]
    return {"username": username, "count": len(acts), "actions": [{
        "action_type": a.get("action_type"),
        "created_at": a.get("created_at"),
        "topic_id": a.get("topic_id"),
        "post_number": a.get("post_number"),
        "excerpt": _strip_html(a.get("excerpt")),
        "url": (f"{BASE}/t/{a.get('slug')}/{a.get('topic_id')}/{a.get('post_number')}"
                if a.get("slug") else None),
    } for a in acts]}


def _md_cell(s):
    return (s or "").replace("|", "\\|").replace("\n", "<br>").strip()


def _format_search(query, page, pages):
    d = _search(query, page, pages)
    more = "（还有更多结果）" if d["more_results"] else ""
    lines = [f'搜索「{d["term"]}」命中 {d["count"]} 条{more}：', ""]
    for r in d["results"]:
        tags = " ".join(f"#{t}" for t in (r["tags"] or []))
        lines.append(f'- **{r["title"]}** {tags}'.rstrip())
        lines.append(f'  📍 {r["url"]}')
        if r["blurb"]:
            lines.append(f'  {r["blurb"]}')
    return "\n".join(lines)


def _format_topic(topic_id, posts, start):
    d = _topic(topic_id, posts, start)
    head = (f'> **{d["title"]}**\n'
            f'> 📍 {d["url"]} ｜ {d.get("views", 0)}浏览 · '
            f'{d.get("like_count", 0)}赞 · {d.get("posts_count", 0)}回复'
            f'（共 {d.get("total_posts", 0)} 楼，本次 {d.get("start", 1)}–'
            f'{d.get("start", 1) + d.get("returned", 0) - 1}）\n')
    rows = ["| 楼层 | 用户 | 原话 |", "|---|---|---|"]
    for p in d["posts"]:
        who = f'{p["username"]}（楼主）' if p["floor"] == 1 else p["username"]
        rows.append(f'| #{p["floor"]} | {who} | {_md_cell(p["content"])} |')
    return head + "\n" + "\n".join(rows)


@mcp.tool()
def whoami() -> dict:
    """查看当前 cookie 对应的 linux.do 登录用户与信任等级。"""
    return _whoami()


@mcp.tool()
def search(query: str, page: int = 1, pages: int = 1) -> dict:
    """全量搜索 linux.do。query 支持 Discourse 高级语法（order:latest、#分类、@用户、
    tags:标签、after:2025-01-01、in:title 等）。pages 为连续抓取的页数（每页约 50 条）。"""
    return _search(query, page, pages)


@mcp.tool()
def get_topic(topic_id: int, posts: int = 20, start: int = 1) -> dict:
    """读取指定话题的详情与楼层正文。posts=返回楼层数，start=起始楼层(1-based，用于翻页，
    如 start=21 取第 21 楼起)。返回含 total_posts(总楼数)。"""
    return _topic(topic_id, posts, start)


@mcp.tool()
def format_search(query: str, page: int = 1, pages: int = 1) -> str:
    """同 search，但直接返回拼好的 Markdown（标题+URL+摘要列表），客户端可原样展示。"""
    return _format_search(query, page, pages)


@mcp.tool()
def format_topic(topic_id: int, posts: int = 20, start: int = 1) -> str:
    """同 get_topic，但直接返回拼好的 Markdown（出处头 + 逐楼表格），客户端可原样展示。
    posts=楼层数，start=起始楼层(1-based，翻页用，如 start=21)。"""
    return _format_topic(topic_id, posts, start)


@mcp.tool()
def list_categories() -> dict:
    """列出所有板块/类别，含每个类别的话题数(topic_count)与帖子数(post_count)。"""
    cats = _categories()
    return {"count": len(cats), "categories": cats}


@mcp.tool()
def category_topics(category_id: int, page: int = 1) -> dict:
    """列出指定类别下的话题（每页约 30 条）。返回含该类别总话题数 topic_count、
    本页话题列表与是否有下一页。category_id 用 list_categories 查询。"""
    return _category_topics(category_id, page)


@mcp.tool()
def list_tags() -> dict:
    """列出所有标签及各自的话题数(count)。"""
    tags = _tags()
    return {"count": len(tags), "tags": tags}


@mcp.tool()
def tag_topics(tag: str, page: int = 1) -> dict:
    """列出指定标签下的话题（每页约 30 条）。tag 用标签名（如「人工智能」）。"""
    return _tag_topics(tag, page)


@mcp.tool()
def user_info(username: str) -> dict:
    """查询用户资料：信任等级、注册/最后在线时间、发帖数、获赞数等。"""
    return _user_info(username)


@mcp.tool()
def latest_topics(page: int = 1) -> dict:
    """获取首页「最新」话题列表（每页约 30 条）。"""
    return _topics_page(f"/latest.json?page={page}", {"page": page})


@mcp.tool()
def top_topics(period: str = "weekly", page: int = 1) -> dict:
    """获取「热门」话题列表。period 取 daily/weekly/monthly/quarterly/yearly/all。"""
    return _top(period, page)


@mcp.tool()
def user_actions(username: str, limit: int = 20) -> dict:
    """获取某用户的发帖/回复活动（含摘要与跳转链接）。"""
    return _user_actions(username, limit)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
