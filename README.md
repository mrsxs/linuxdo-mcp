
# linuxdo-mcp

#### 本帖使用社区公益推广，符合推广要求。我申明并遵循社区要求的以下内容：
* **我的项目是免费使用的，无收费（变相收费、赞助）部分：** 是 
* **我的帖子已经打上 #公益推广 标签：** 是 
* **我的项目属于个人项目，与公司或商业机构无关：** 是 
* **我的项目不存在QQ、TG等群组引流：** 是 
* **我的项目不存在非运营必要的网站引流：** 是 
* **我的项目不存在为他人推广、AFF：** 是 
* **我的项目无关联的商业项目：** 是 
* **我的站点存在登录，并已接入 LINUX DO Connect：** 否
* **我帖子内的项目介绍，AI生成、润色内容部分已截图发出：** 是 
* **以上选择我承诺是永久有效的，接受社区和佬友监督：** 是 

  
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
- `get_topic` / `format_topic` 的 `topic_id` 可直接传话题 URL（如 `https://linux.do/t/xxx/2885565`），自动解析出 id。
- 搜索高级语法:`order:latest`、`#分类`、`@用户`、`tags:标签`、`after:2025-01-01`、`in:title` 等。

## 前置

- 安装 [uv](https://docs.astral.sh/uv/)(提供 `uvx`)。
- 准备一个 linux.do 的登录 cookie(`_t`),给法见下方「登录配置」。只需 `_t`,**不需要** `cf_clearance`。

## 配置(复制到你的 MCP 客户端)

`uvx` 会自动拉取并运行,无需下载代码。基础配置:

```json
{
  "mcpServers": {
    "linuxdo": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/mrsxs/linuxdo-mcp", "linuxdo-mcp"]
    }
  }
}
```

再按下面「登录配置」二选一填 `env`。

- **Claude Code**:`claude mcp add-json linuxdo '<上面的内容>'`,或写进 `.mcp.json` / 设置。
- **Cursor / Claude Desktop / Cline**:粘进各自的 MCP 配置文件即可。

## 登录配置(两种方式,二选一)

> 为什么不直接读你平时用的浏览器?因为工具和主浏览器**共用同一个 `_t`** 时,
> Discourse 的 token 轮换会判定异常、把会话作废,导致**主浏览器被顶下线**。
> 所以默认 `LINUXDO_READ_BROWSER=0`(不读浏览器),请用下面任一「独立身份」。

### 方式一:独立 token(任何平台通用,最省心)

在**隐身窗口**或另一个浏览器登录 linux.do,F12 → Application → Cookies → `https://linux.do`
→ 复制 `_t` 的值,填进 `env`:

```json
"env": { "LINUXDO_COOKIE": "_t=你复制的值" }
```

只需填一次:工具之后自动接收 Discourse 轮换、写回缓存**自续期**,长期免维护,且与主浏览器互不影响。

### 方式二:专用 Chrome profile 自动读(macOS/Linux,免手工复制)

在 Chrome 右上角头像 →「添加」新建一个 profile(例:显示名 `linuxdo`),在其中登录 linux.do,然后:

```json
"env": {
  "LINUXDO_READ_BROWSER": "1",
  "LINUXDO_CHROME_PROFILE": "linuxdo"
}
```

`LINUXDO_CHROME_PROFILE` 可填**显示名**(Chrome 菜单里看到的,如 `linuxdo`)或**目录名**(如 `Profile 1`);
填错会列出所有可用 profile 供对照。工具首次自动读该 profile 做 bootstrap,之后同样走缓存自续期。
你日常用的主 profile(`Default`)完全不受影响——只要不在这个专用 profile 里刷 linux.do 就永不冲突。

## cookie 获取顺序与续期

1. 缓存文件 `~/.cache/linuxdo-mcp/cookie.json`(权限 600,工具自维护、含轮换续期,最新);
2. 环境变量 `LINUXDO_COOKIE`(方式一,首次 bootstrap 后写入缓存);
3. 浏览器 cookie 库(方式二,仅当 `LINUXDO_READ_BROWSER=1`)。

`_t` 是 Discourse 的滚动 token,工具每次请求都会接收服务器轮换回来的新值写回缓存,
所以配一次通常长期有效;遇 401/403 会清缓存并提示更新。

方式二各平台授权差异:

| 平台 | Chrome/Chromium/Brave | Firefox |
|---|---|---|
| macOS | 首次弹一次钥匙串授权框,选「始终允许」;uv 缓存重建致解释器路径变化时会再弹一次 | 免授权 |
| Linux | 一般免授权(cookie 若在已上锁的 gnome-keyring/kwallet 则需解锁) | 免授权 |
| Windows | **不支持**(pycookiecheat 只支持 macOS/Linux),请用方式一或 `LINUXDO_BROWSER=firefox` | 免授权 |

## 环境变量一览

| 变量 | 说明 |
|---|---|
| `LINUXDO_COOKIE` | 方式一:手工指定 cookie,形如 `_t=xxx`(裸 token 也可) |
| `LINUXDO_READ_BROWSER` | 方式二:置 `1` 才允许读浏览器(默认 `0`,防止与主浏览器互顶) |
| `LINUXDO_CHROME_PROFILE` | 方式二:Chrome 系 profile 的显示名或目录名(如 `linuxdo` / `Profile 1`) |
| `LINUXDO_BROWSER` | `chrome`(默认)/`chromium`/`brave`/`slack`/`firefox` |
| `LINUXDO_COOKIE_TTL` | 缓存有效期秒数,默认 `2592000`(30 天,配合自续期) |
| `LINUXDO_CACHE_DIR` | 缓存目录,默认 `~/.cache/linuxdo-mcp` |
| `LINUXDO_IMPERSONATE` | TLS 指纹,默认 `chrome` |

> ⚠️ `_t` 等于你的 linux.do 登录凭证,只填进自己的本地配置,**切勿分享给他人**。


## 本地运行(开发)

```bash
uvx --from . linuxdo-mcp        # 或 uv run src/linuxdo_mcp/server.py
```

## 备注

- cookie 过期返回 401/403 时会清缓存并提示更新;按你选的方式重配一次即可(方式二一般不会到期,浏览器保持登录时会自动续期)。
- 读取浏览器 cookie 依赖 [pycookiecheat](https://github.com/n8henrie/pycookiecheat),只读取 linux.do 一个域名下的 cookie。
- 偶发被 Cloudflare 拦截时会自动重试 3 次;仍失败可设 `LINUXDO_IMPERSONATE=chrome131`(或 `chrome124`)换指纹。
- 所有请求为只读 GET,不做任何写操作。
