
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
用 [curl_cffi](https://github.com/lexiforest/curl_cffi) 模拟 Chrome TLS 指纹,配合真实浏览器的
`cf_clearance` 过 Cloudflare;通过 linux.do **授权登录**拿到本工具独立的会话,访问受信任等级限制的内容。

## 工具

| 工具 | 返回 | 说明 |
|------|------|------|
| `login(timeout_seconds=300)` | JSON | 授权登录(在浏览器里点「授权」),成功后返回当前用户 |
| `logout()` | JSON | 清除本工具保存的登录会话 |
| `whoami()` | JSON | 本工具当前登录的用户与信任等级 |
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
- 装有 Chrome(或 Chromium/Brave),平时在里面访问过 linux.do。
- 不登录也能读公开内容;要读受等级限制的板块,按下方「授权登录」做一次即可。

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

- **Claude Code**:`claude mcp add-json linuxdo '<上面的内容>'`,或写进 `.mcp.json` / 设置。
- **Cursor / Claude Desktop / Cline**:粘进各自的 MCP 配置文件即可。

## 过 Cloudflare

linux.do 对非浏览器客户端一律弹盾,光模拟 TLS 指纹已经过不去。工具的做法是借用真实浏览器
过盾后铸出的 `cf_clearance`,配上同一个 User-Agent 发请求:

1. **读日常 Chrome 里现成的 `cf_clearance`**(扫描所有 profile 取最新的一份;**只读这一个 cookie,
   不碰登录态 `_t`**)。macOS 首次会弹钥匙串授权「Chrome Safe Storage」,选「始终允许」。
2. 现成的过期了:在默认浏览器打开一次 linux.do 让它刷新,等写盘后再读(约半分钟内)。
3. 都不行(Windows / Firefox 等读不了 cookie)时,弹一个**专用 Chrome**(独立 profile,
   在 `~/.cache/linuxdo-mcp/chrome-profile`)让你点「请验证您是真人」。新 profile 铸的 clearance
   信任度低,可能时好时坏,只作兜底。

## 授权登录

> 为什么不直接用浏览器里的 `_t`?Discourse 每 10 分钟轮换一次 `_t`,工具一用主浏览器的 `_t`
> 就可能触发轮换,新 token 落到工具手里,**主浏览器一分钟后被顶下线**。所以改为授权登录,
> 拿一个**独立会话**,两边各用各的,永不冲突。

linux.do 收窄了 User API Key 的权限(不给 `read`/`write`),能用的只有 `one_time_password`:
授权后回调里带一个加密的一次性密码,工具用它换一个全新的登录会话,随后撤销这个 key。
回调只允许 `discourse://auth_redirect`,所以需要一个接收它的小程序。

### macOS(推荐):在日常浏览器里授权

```bash
# 一次性:装 discourse:// 回调小程序并设为默认处理程序(会记下原来的,如 FluxDO)
uvx --from git+https://github.com/mrsxs/linuxdo-mcp linuxdo-mcp install-handler

# 登录:默认浏览器打开授权页 → 点「授权」→ 允许打开 linuxdo-mcp-auth
uvx --from git+https://github.com/mrsxs/linuxdo-mcp linuxdo-mcp login
```

也可以在 MCP 客户端里直接让它调用 `login` 工具。小程序装在 `~/Applications/linuxdo-mcp-auth.app`,
只做一件事:把收到的回调地址写进 `~/.cache/linuxdo-mcp/callback.url`(权限 600);
回调内容是用本次登录临时生成的公钥加密的,只有发起登录的进程能解开。
`linuxdo-mcp uninstall-handler` 删除小程序并把 `discourse:` 还给原来的处理程序。

> 注意:设成默认后,FluxDO 等同样用 `discourse://` 回调的客户端,其授权登录回调会被这个小程序接走,
> 需要时先 `uninstall-handler`。

### 其他平台:在专用浏览器里授权

没装回调小程序时,`login` 会在专用 Chrome 里打开授权页,工具通过 CDP 直接截获回调;
需要在这个窗口里登录一次 linux.do(登录态保存在专用 profile,下次不用再登)。

### 兜底:手工指定 `_t`

```json
"env": { "LINUXDO_COOKIE": "_t=从隐身窗口复制的值" }
```

用隐身窗口或另一个浏览器登录后复制的 `_t` 也是独立会话;工具会接收轮换自动续期。

## 会话与续期

- 会话保存在 `~/.cache/linuxdo-mcp/session.json`(权限 600),每次请求接收 Discourse 轮换回来的新 `_t`,
  配一次通常长期有效。服务端判定会话失效时会清掉本地会话并提示重新 `login`。
- `logout` 只清本地;服务端会话可在 linux.do「偏好设置 → 安全 → 最近使用的设备」里注销。
- 不登录也能用,按匿名访问公开内容。

## 命令行

```bash
linuxdo-mcp                     # 作为 MCP 服务器运行(stdio)
linuxdo-mcp login | logout | whoami
linuxdo-mcp install-handler | uninstall-handler   # 仅 macOS
```

## 环境变量一览

| 变量 | 说明 |
|---|---|
| `LINUXDO_COOKIE` | 兜底:手工指定 `_t=xxx`(裸 token 也可) |
| `LINUXDO_BROWSER` | 读 `cf_clearance` 的日常浏览器:`chrome`(默认)/`chromium`/`brave` |
| `LINUXDO_UA` | 覆盖按浏览器版本自动拼出的 User-Agent |
| `LINUXDO_CHROME` | 专用浏览器的可执行文件路径(默认自动探测 Chrome/Chromium/Edge/Brave) |
| `LINUXDO_CF_TIMEOUT` | 等待过盾的秒数,默认 `180` |
| `LINUXDO_CACHE_DIR` | 缓存目录,默认 `~/.cache/linuxdo-mcp` |
| `LINUXDO_IMPERSONATE` | TLS 指纹,默认 `chrome` |

> ⚠️ `session.json` 里的 `_t` 等于你的 linux.do 登录凭证,**切勿分享给他人**。


## 本地运行(开发)

```bash
uvx --from . linuxdo-mcp        # 或 uv run linuxdo-mcp
uv run pytest                   # 单元测试(不访问网络)
```

## 备注

- 读取浏览器 cookie 依赖 [pycookiecheat](https://github.com/n8henrie/pycookiecheat),只取 linux.do 的 `cf_clearance`。
- 被 Cloudflare 拦截时会自动刷新 clearance 后重试一次;仍失败可设 `LINUXDO_IMPERSONATE=chrome146` 换指纹。
- 读取类工具全部是 GET;写操作只发生在登录时(换会话、撤销临时 key)。
