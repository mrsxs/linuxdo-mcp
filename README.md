# linuxdo-mcp

搜索 / 阅读 [linux.do](https://linux.do)(Discourse 论坛)的 MCP 服务器。
用 [curl_cffi](https://github.com/lexiforest/curl_cffi) 模拟 Chrome TLS 指纹绕过 Cloudflare,
凭登录 cookie 访问受信任等级限制的内容。

## 工具

| 工具 | 返回 | 说明 |
|------|------|------|
| `whoami()` | JSON | 当前 cookie 对应的登录用户与信任等级 |
| `search(query, page=1, pages=1)` | JSON | 全量搜索,`query` 支持 Discourse 高级语法 |
| `get_topic(topic_id, posts=5)` | JSON | 话题详情 + 前 N 楼正文 |
| `list_categories()` | JSON | 所有板块,含各自话题数 `topic_count`、帖子数 `post_count` |
| `category_topics(category_id, page=1)` | JSON | 指定类别下的话题列表(每页约 30),含该类别总话题数 |
| `list_tags()` | JSON | 所有标签及各自话题数 `count` |
| `tag_topics(tag, page=1)` | JSON | 指定标签下的话题列表 |
| `user_info(username)` | JSON | 用户资料:信任等级、头衔、发帖数、获赞数、注册/在线时间 |
| `latest_topics(page=1)` | JSON | 首页「最新」话题 |
| `top_topics(period="weekly", page=1)` | JSON | 「热门」话题,period: daily/weekly/monthly/quarterly/yearly/all |
| `user_actions(username, limit=20)` | JSON | 某用户的发帖/回复活动(含摘要与链接) |
| `format_search(query, page=1, pages=1)` | Markdown | 同 search,直接返回成品 Markdown(标题+URL+摘要) |
| `format_topic(topic_id, posts=20)` | Markdown | 同 get_topic,直接返回成品 Markdown(出处头+逐楼表格) |

- `format_*` 工具返回拼好的 Markdown 字符串,客户端可原样展示;其余返回结构化 JSON。
- 搜索高级语法:`order:latest`、`#分类`、`@用户`、`tags:标签`、`after:2025-01-01`、`in:title` 等。

## 前置

- 安装 [uv](https://docs.astral.sh/uv/)(提供 `uvx`)。
- 拿到自己的 cookie:浏览器登录 linux.do → F12 → Application → Cookies → `https://linux.do` → 复制 `_t` 的 Value。
  只需 `_t`,**不需要** `cf_clearance`。

## 配置(复制到你的 MCP 客户端)

无需下载代码,`uvx` 会自动从仓库拉取并运行。把下面这段加进客户端的 MCP 配置,
填上**你自己的** `_t`:

```json
{
  "mcpServers": {
    "linuxdo": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/mrsxs/linuxdo-mcp", "linuxdo-mcp"],
      "env": {
        "LINUXDO_COOKIE": "_t=你的token值"
      }
    }
  }
}
```

- **Claude Code**:`claude mcp add-json linuxdo '<上面的内容>'`,或写进 `.mcp.json` / 设置。
- **Cursor / Claude Desktop / Cline**:粘进各自的 MCP 配置文件即可。

> ⚠️ `_t` 等于你的 linux.do 登录凭证,只填进自己的本地配置,**切勿分享给他人**。

## 本地运行(开发)

```bash
export LINUXDO_COOKIE='_t=...'
uvx --from . linuxdo-mcp        # 或 uv run src/linuxdo_mcp/server.py
```

## 备注

- cookie 过期会返回 401/403,重新导出 `_t` 即可。
- 偶发被 Cloudflare 拦截时会自动重试 3 次;仍失败可设 `LINUXDO_IMPERSONATE=chrome131`(或 `chrome124`)换指纹。
- 所有请求为只读 GET,不做任何写操作。
