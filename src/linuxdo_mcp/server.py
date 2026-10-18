"""linux.do (Discourse) MCP 服务器。

过盾：专用浏览器铸出 cf_clearance，curl_cffi 带着它和同一 UA 访问（见 cf.py）。
认证：login 工具走 linux.do 授权页换一个 MCP 独立的 _t 会话（见 auth.py），
也可用环境变量 LINUXDO_COOKIE = "_t=你的token值" 手工指定。未登录时按匿名访问公开内容。
"""
import html
import json
import re
import subprocess
import sys
import threading
import time
import urllib.parse

import anyio

try:  # mcp >= 2：FastMCP 更名为 MCPServer
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp < 2
    from mcp.server.fastmcp import FastMCP as _Server

from . import auth, client, handler, session

BASE = client.BASE

mcp = _Server("linuxdo")


def _errors(body):
    try:
        errs = json.loads(body).get("errors")
    except (ValueError, AttributeError):
        return ""
    return "；".join(errs) if isinstance(errs, list) else ""


CACHE_TTL = 120
_CACHE_MAX = 256
_cache = {}  # (path, 会话 cookie) -> (过期时刻, 响应正文)
_CACHE_LOCK = threading.Lock()  # 同步工具在 MCP 的工作线程里并发执行


def _clear_cache():
    with _CACHE_LOCK:
        _cache.clear()


def _cached(key):
    with _CACHE_LOCK:
        hit = _cache.get(key)
    if hit and hit[0] > time.monotonic():
        return json.loads(hit[1])  # 每次重新解析，调用方改了也不污染缓存
    return None


def _remember(key, body):
    now = time.monotonic()
    with _CACHE_LOCK:
        if len(_cache) >= _CACHE_MAX:
            for k in [k for k, (exp, _) in _cache.items() if exp <= now] or list(_cache)[:_CACHE_MAX // 4]:
                _cache.pop(k, None)
        _cache[key] = (now + CACHE_TTL, body)


def _fetch(path, cache=True):
    """GET 并解析 JSON；同一地址 CACHE_TTL 秒内直接用缓存，少打 linux.do（连发易触发 Cloudflare）。

    缓存按会话 cookie 区分：换了账号或会话（含命令行 login、手改 session.json）不会读到旧账号的内容。"""
    key = (path, session.get_cookie())
    if cache and (data := _cached(key)) is not None:
        return data
    r = client.request("GET", path)
    body = r.text
    if r.headers.get("discourse-logged-out") and session.get_cookie():
        session.clear()  # 服务端判定 _t 已失效
        raise RuntimeError("登录会话已失效：请调用 login 工具重新授权登录。")
    if r.status_code in (401, 403):
        hint = "（当前未登录，可调用 login 授权登录后再试）" if not session.get_cookie() else ""
        raise RuntimeError(f"无权访问(HTTP {r.status_code})：{_errors(body) or '受信任等级或权限限制'}{hint}")
    if r.status_code == 404:
        raise RuntimeError(f"不存在(404)：{_errors(body) or path}")
    if r.status_code == 429:
        raise RuntimeError("被限流(429)：请降低频率，稍后重试。")
    if r.status_code != 200 or not body.lstrip().startswith(("{", "[")):
        raise RuntimeError(f"异常响应 HTTP {r.status_code}: {body[:200]}")
    data = json.loads(body)
    if cache:
        _remember(key, body)
    return data


def _strip_html(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def _html_to_text(s):
    """把 Discourse 渲染后的 cooked HTML 转成可读文本：
    保留段落换行、把代码块转成 ``` 围栏、列表转 - 项、数学转 $..$/$$..$$、加粗转 **。"""
    if not s:
        return ""
    t = s

    def _code(m):
        lang = (m.group(1) or "").strip().lower()
        if lang in ("plaintext", "text", "auto", "nohighlight", "none"):
            lang = ""
        body = html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip("\n")
        return f"\n\n```{lang}\n{body}\n```\n\n"

    # 代码块：<pre><code class="... lang-xxx ...">...</code></pre>
    t = re.sub(
        r'<pre><code(?:\s+class="[^"]*?lang-([\w+#.-]+)[^"]*")?[^>]*>(.*?)</code></pre>',
        _code, t, flags=re.S)
    # 行内代码
    t = re.sub(r"<code>(.*?)</code>",
               lambda m: "`" + html.unescape(re.sub(r"<[^>]+>", "", m.group(1))) + "`",
               t, flags=re.S)
    # 数学：块级 $$..$$、行内 $..$
    t = re.sub(r'<div class="math">(.*?)</div>',
               lambda m: "\n\n$$" + m.group(1).strip() + "$$\n\n", t, flags=re.S)
    t = re.sub(r'<span class="math">(.*?)</span>',
               lambda m: "$" + m.group(1).strip() + "$", t, flags=re.S)
    # 加粗
    t = re.sub(r"</?(?:strong|b)>", "**", t)
    # 列表
    t = re.sub(r"<li>", "\n- ", t)
    t = re.sub(r"</li>", "", t)
    t = re.sub(r"</?[uo]l>", "\n", t)
    # 段落 / 换行 / 标题 / 引用 / 块边界
    t = re.sub(r"<br\s*/?>", "\n", t)
    t = re.sub(r"</p>", "\n\n", t)
    t = re.sub(r"</h[1-6]>", "\n\n", t)
    t = re.sub(r"</blockquote>", "\n", t)
    t = re.sub(r"</div>", "\n", t)
    # 删除其余标签（如 <a>、<p>、<span> 等，保留其内部文本）
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    # 收敛空白
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _topic_url(slug, tid):
    return f"{BASE}/t/{slug or 'topic'}/{tid}"


def _as_topic_id(topic):
    """接受话题 id（int/数字串）或 linux.do 话题 URL，返回整数 id。
    URL 形如 https://linux.do/t/<slug>/<id>[/<楼层>]，取路径里 /t/ 后的数字段。"""
    if isinstance(topic, int):
        return topic
    t = str(topic).strip()
    if t.isdigit():
        return int(t)
    m = re.search(r"/t/(?:[^/]+/)?(\d+)", t)
    if m:
        return int(m.group(1))
    m = re.search(r"\d+", t)
    if m:
        return int(m.group(0))
    raise RuntimeError(f"无法从 {topic!r} 解析出话题 id（给数字 id 或话题 URL）。")


def _whoami():
    if not session.get_cookie():
        raise RuntimeError("未登录：请调用 login 工具（或命令行 `linuxdo-mcp login`）授权登录。")
    try:
        u = _fetch("/session/current.json", cache=False).get("current_user") or {}
    except RuntimeError as e:
        if "404" not in str(e):  # Discourse 对未登录的 current.json 回 404
            raise
        u = {}
    if not u:
        raise RuntimeError("未登录（会话无效）：请调用 login 工具重新授权登录。")
    session.set_username(u.get("username"))
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
    topic_id = _as_topic_id(topic_id)
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
            "content": _html_to_text(p.get("cooked")),
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
    idx = _category_index()
    tl = _fetch(f"/c/{cat['slug']}/{category_id}.json?page={page}").get("topic_list") or {}
    topics = tl.get("topics", [])
    return {
        "category": cat["name"],
        "category_id": category_id,
        "topic_count": cat["topic_count"],
        "page": page,
        "returned": len(topics),
        "more": bool(tl.get("more_topics_url")),
        "topics": [_topic_brief(t, idx) for t in topics],
    }


_SITE_CACHE = {"ts": 0.0, "cats": {}}
_LV_RE = re.compile(r"^(.*?)[,，]\s*Lv\s*([0-3])\s*$", re.I)


def _split_level(name):
    """从分类名解析等级：'开发调优, Lv1' -> ('开发调优', 1)；无后缀则 (name, None)。
    linux.do 不暴露 minimum_required_trust_level，等级信息写在子板名的 ', LvN' 后缀里。"""
    m = _LV_RE.match(name or "")
    if m:
        return m.group(1).strip(), int(m.group(2))
    return (name or "").strip() or None, None


def _category_index():
    """{category_id: {"name"(去掉 LvN 后缀), "trust_level"(int 或 None)}}，含子分类；
    缓存 5 分钟。取自 /site.json（覆盖父板+子板）；失败时退回旧缓存，不阻断列表请求。"""
    now = time.time()
    if _SITE_CACHE["cats"] and now - _SITE_CACHE["ts"] < 300:
        return _SITE_CACHE["cats"]
    try:
        cats = _fetch("/site.json").get("categories", [])
    except Exception:
        return _SITE_CACHE["cats"]
    idx = {}
    for c in cats:
        base, lv = _split_level(c.get("name"))
        if lv is None:
            lv = c.get("minimum_required_trust_level")
        idx[c.get("id")] = {"name": base, "trust_level": lv}
    _SITE_CACHE.update(ts=now, cats=idx)
    return idx


def _topic_brief(t, idx=None):
    idx = idx if idx is not None else {}
    cid = t.get("category_id")
    cat = idx.get(cid) or {}
    return {
        "id": t.get("id"),
        "title": t.get("title"),
        "category_id": cid,
        "category": cat.get("name"),
        "min_trust_level": cat.get("trust_level"),
        "posts_count": t.get("posts_count"),
        "views": t.get("views"),
        "like_count": t.get("like_count"),
        "created_at": t.get("created_at"),
        "url": _topic_url(t.get("slug"), t.get("id")),
    }


def _topics_page(path, extra):
    idx = _category_index()
    tl = _fetch(path).get("topic_list") or {}
    topics = tl.get("topics", [])
    return {**extra, "returned": len(topics),
            "more": bool(tl.get("more_topics_url")),
            "topics": [_topic_brief(t, idx) for t in topics]}


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
    end = d.get("start", 1) + d.get("returned", 0) - 1
    out = [
        f'> **{d["title"]}**',
        f'> 📍 {d["url"]} ｜ {d.get("views", 0)}浏览 · {d.get("like_count", 0)}赞 · '
        f'{d.get("posts_count", 0)}回复（共 {d.get("total_posts", 0)} 楼，'
        f'本次 {d.get("start", 1)}–{end}）',
    ]
    for p in d["posts"]:
        who = f'@{p["username"]}（楼主）' if p["floor"] == 1 else f'@{p["username"]}'
        out += ["", f'## #{p["floor"]} · {who}', "", p["content"], "", "---"]
    if out and out[-1] == "---":
        out.pop()
    return "\n".join(out).rstrip()


def _logout():
    session.clear()
    _clear_cache()
    return {"logged_out": True}


def _cli_login():
    where = "默认浏览器" if handler.ensure_installed() else "专用浏览器窗口"
    print(f"正在{where}打开 linux.do 授权页：需要时先过验证，然后点「授权」…", file=sys.stderr)
    auth.login()
    return _whoami()


LOGIN_TIMEOUT_RANGE = (30, 600)


@mcp.tool()
async def login(timeout_seconds: int = auth.DEFAULT_TIMEOUT) -> dict:
    """授权登录 linux.do。
    macOS 上若 discourse: 链接没有别的 App 接管，会先在 ~/Applications 生成回调小程序
    linuxdo-mcp-auth 并设为 discourse: 的默认处理程序（本机编译，不下载；
    `linuxdo-mcp uninstall-handler` 可删除并还原），然后在默认浏览器打开授权页：
    点「授权」并允许打开 linuxdo-mcp-auth 即可；若弹出专用浏览器窗口显示「请验证您是真人」，
    点一下过 Cloudflare 验证。discourse: 已归 FluxDO 等其它 App、或非 macOS 时不安装，
    改在专用浏览器里授权（需要在其中登录 linux.do）。
    换到的是本工具独立的登录会话，不影响你日常浏览器的登录。成功后返回当前用户。
    timeout_seconds 为等待你完成操作的最长秒数（30–600）。"""
    lo, hi = LOGIN_TIMEOUT_RANGE
    # 要等用户在浏览器里操作几分钟，放到线程里跑，别卡住 MCP 的事件循环
    await anyio.to_thread.run_sync(auth.login, min(max(timeout_seconds, lo), hi))
    _clear_cache()  # 换了会话，旧缓存可能是别的账号/权限下的内容
    return await anyio.to_thread.run_sync(_whoami)


@mcp.tool()
def logout() -> dict:
    """清除本工具保存的登录会话（之后按匿名访问；服务端会话可在 linux.do
    偏好设置→安全→最近使用的设备 里注销）。"""
    return _logout()


@mcp.tool()
def whoami() -> dict:
    """查看本工具当前登录的 linux.do 用户与信任等级；未登录时提示调用 login。"""
    return _whoami()


@mcp.tool()
def search(query: str, page: int = 1, pages: int = 1) -> dict:
    """全量搜索 linux.do。query 支持 Discourse 高级语法（order:latest、#分类、@用户、
    tags:标签、after:2025-01-01、in:title 等）。pages 为连续抓取的页数（每页约 50 条）。
    展示约定：Markdown 表格或列表，标题完整勿截断，纯文字勿用 emoji（易乱码）。"""
    return _search(query, page, pages)


@mcp.tool()
def get_topic(topic_id: int | str, posts: int = 20, start: int = 1) -> dict:
    """读取指定话题的详情与楼层正文。topic_id 可传数字 id，也可直接传 linux.do 话题
    URL（如 https://linux.do/t/xxx/2885565/1，会自动取出 id）。posts=返回楼层数，
    start=起始楼层(1-based，用于翻页，如 start=21 取第 21 楼起)。返回含 total_posts(总楼数)。"""
    return _topic(topic_id, posts, start)


@mcp.tool()
def format_search(query: str, page: int = 1, pages: int = 1) -> str:
    """同 search，但直接返回拼好的 Markdown（标题+URL+摘要列表），客户端可原样展示。"""
    return _format_search(query, page, pages)


@mcp.tool()
def format_topic(topic_id: int | str, posts: int = 20, start: int = 1) -> str:
    """同 get_topic，但直接返回拼好的 Markdown（出处头 + 逐楼表格），客户端可原样展示。
    topic_id 可传数字 id 或 linux.do 话题 URL（自动解析）。posts=楼层数，
    start=起始楼层(1-based，翻页用，如 start=21)。"""
    return _format_topic(topic_id, posts, start)


@mcp.tool()
def list_categories() -> dict:
    """列出所有板块/类别，含每个类别的话题数(topic_count)与帖子数(post_count)。"""
    cats = _categories()
    return {"count": len(cats), "categories": cats}


@mcp.tool()
def category_topics(category_id: int, page: int = 1) -> dict:
    """列出指定类别下的话题（每页约 30 条）。返回含该类别总话题数 topic_count、
    本页话题列表与是否有下一页。category_id 用 list_categories 查询。每条话题已含
    category、min_trust_level。展示约定同 latest_topics（Markdown 表格、完整标题、
    纯文字表头禁用 emoji）。"""
    return _category_topics(category_id, page)


@mcp.tool()
def list_tags() -> dict:
    """列出所有标签及各自的话题数(count)。"""
    tags = _tags()
    return {"count": len(tags), "tags": tags}


@mcp.tool()
def tag_topics(tag: str, page: int = 1) -> dict:
    """列出指定标签下的话题（每页约 30 条）。tag 用标签名（如「人工智能」）。每条话题
    已含 category、min_trust_level。展示约定同 latest_topics（Markdown 表格、完整
    标题、纯文字表头禁用 emoji）。"""
    return _tag_topics(tag, page)


@mcp.tool()
def user_info(username: str) -> dict:
    """查询用户资料：信任等级、注册/最后在线时间、发帖数、获赞数等。"""
    return _user_info(username)


@mcp.tool()
def latest_topics(page: int = 1) -> dict:
    """获取首页「最新」话题列表（每页约 30 条）。每条已含 category(分类名)、
    min_trust_level(最低等级要求，null=无限制)，无需再逐条 get_topic 查分类。

    展示约定：用 Markdown 表格，列依次为 标题(完整勿截断) | 分类 | 等级 | 回复 |
    点赞 | 链接；表头与单元格一律纯文字，禁用 emoji（多字节 emoji 在生成时可能
    碎成「����」乱码）。等级按 min_trust_level 显示「Lv0/1/2/3」，null 显示「—」。"""
    return _topics_page(f"/latest.json?page={page}", {"page": page})


@mcp.tool()
def top_topics(period: str = "weekly", page: int = 1) -> dict:
    """获取「热门」话题列表。period 取 daily/weekly/monthly/quarterly/yearly/all。
    每条已含 category、min_trust_level。展示约定同 latest_topics（Markdown 表格、
    完整标题、纯文字表头禁用 emoji）。"""
    return _top(period, page)


@mcp.tool()
def user_actions(username: str, limit: int = 20) -> dict:
    """获取某用户的发帖/回复活动（含摘要与跳转链接）。"""
    return _user_actions(username, limit)


def main():
    """无参数时作为 MCP 服务器运行；`linuxdo-mcp login|logout|whoami` 供命令行直接使用。"""
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if not cmd:
        mcp.run()
        return
    actions = {"login": _cli_login, "logout": _logout, "whoami": _whoami,
               "install-handler": handler.install, "uninstall-handler": handler.uninstall}
    if cmd not in actions:
        sys.exit(f"用法：linuxdo-mcp [{'|'.join(actions)}]（不带参数则作为 MCP 服务器运行）")
    try:
        print(json.dumps(actions[cmd](), ensure_ascii=False, indent=2))
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or b"").decode(errors="replace").strip()
        sys.exit(f"失败：{' '.join(map(str, e.cmd))} 返回 {e.returncode}{'：' + detail if detail else ''}")
    except (RuntimeError, OSError, ValueError) as e:
        sys.exit(f"失败：{e}")


if __name__ == "__main__":
    main()
