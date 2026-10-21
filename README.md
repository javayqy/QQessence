# QQ群精华消息本地存档

把 QQ 群「精华消息」抓取下来，保存在本地，离线随时查看、随时导出。

旧版是一个 2023 年的脚本（`main.py` + `output_html.py`），依赖固定 XPath 解析、
变量硬编码在源码里、导出 HTML 极其简陋。本项目已重构为 **本地 Web 应用**：
Python + Flask 后端负责抓取与存储，原生前端负责浏览与导出，数据以纯 JSON 落在
`data/` 目录，不需要数据库，也不需要联网加载任何 CDN 资源。

---

## 快速开始

```bash
# 1. 安装依赖（建议使用虚拟环境）
pip install -r requirements.txt

# 2. 启动
python app.py
```

启动后会自动打开浏览器（默认 `http://127.0.0.1:5000/`）。端口被占用时会自动 +1 重试。

常用参数：

```bash
python app.py --port 8080     # 指定起始端口
python app.py --no-browser    # 不自动打开浏览器
```

## 使用流程

1. **拿 Cookie**：浏览器登录 <https://qun.qq.com>，打开开发者工具 →
   Network → 任意请求 → 复制请求头里的整段 `Cookie`。
2. **粘贴保存**：在左侧「连接」卡片把 Cookie 整段粘进去点「保存」。
   支持 `a=b; c=d;` 形式，也支持带 `Cookie:` 前缀或多行粘贴。
3. **填群号拉取**：在「群」卡片填写群号（5–12 位数字）点「拉取并保存」。
   若群列表接口可用，会直接列出你加入的群，点一下即可填入群号。
4. **查看 / 导出**：主区域按卡片流展示消息；搜索框实时过滤；「只看有图」可筛图；
   右上角可导出 HTML / Markdown / JSON；「下载图片」把图片抓到本地。

> 抓取到的每次结果都会存成一个**快照**（`data/snapshots/<群号>_<时间>/`），
> 历史记录里可以随时切回任意一次快照，也可以单个删除。

## 目录结构

```
QQessence-main/
├── app.py                  # Flask 入口 + 全部路由 + 启动器
├── core/
│   ├── __init__.py
│   ├── qq_client.py        # Cookie 解析 / bkn / 精华页请求 / 群列表 / 分页
│   ├── parser.py           # __INITIAL_STATE__ 提取 + 消息归一化（+ XPath 兜底）
│   ├── storage.py          # 快照存储、索引、配置
│   ├── downloader.py       # 图片并发下载（去重 / 跳过已存在）
│   └── exporter.py         # 导出 HTML（图片内联 base64）/ Markdown / JSON
├── static/
│   ├── index.html          # 单页界面
│   ├── app.js              # 原生 ES6 交互
│   └── styles.css          # 手写 CSS 设计系统（浅色 / 深色）
├── data/                   # 运行时生成，已被 .gitignore 忽略
│   ├── config.json         # 本机 Cookie（明文存储，仅接口回显时脱敏）
│   ├── index.json          # 快照索引
│   └── snapshots/<id>/     # meta.json + messages.json + images/
├── tests/                  # pytest 测试（260 条，全部离线运行）
├── legacy/                 # 2023 年旧脚本原样归档
├── requirements.txt
├── LICENSE                 # MIT
└── .gitignore              # data/ 等敏感内容已排除
```

> `data/` 不存在也能直接启动，程序会自行创建。

## 架构说明

### 抓取链路

```
app.py  /api/fetch
   └─ qq_client.QQEssenceClient.fetch_essence(gc, max_pages)
        ├─ fetch_essence_page()   带随机 seq/random 请求 indexPc
        └─ parser.parse_page()
             ├─ extract_initial_state()  解析 window.__INITIAL_STATE__
             ├─ extract_msg_list()       取 msgList
             └─ normalize_messages()     归一化为统一消息模型
   └─ storage.create_snapshot()       落盘 meta.json + messages.json + 更新 index.json
   └─ downloader.download_images()    （可选）并发下载图片
```

### 统一消息模型

```jsonc
{
  "id": "消息 ID 或列表下标",
  "sender_uin": "123456",
  "sender_name": "昵称",
  "avatar": "https://q1.qlogo.cn/...",
  "time_raw": "2023-04-25 12:00:00",
  "time_iso": "2023-04-25T12:00:00",
  "text": "纯文本，多行用 \n 连接",
  "images": ["https://..."],
  "links": [{ "url": "https://...", "title": "标题" }],
  "files": [{ "name": "a.zip", "size": "1.2 MB", "url": "https://..." }],
  "type": "text|image|mix|file|link|forward|unknown",
  "raw": { /* 原始 msg 对象，永远保留，便于适配线上结构变化 */ }
}
```

### 为什么解析 `__INITIAL_STATE__` 而不是 XPath

目标页面 `https://qun.qq.com/essence/indexPc` 现在是 Vue SSR 页面，服务端把数据
直接注入到页面尾部的 `window.__INITIAL_STATE__`。旧版按
`//*[@id="app"]/div[2]/div[i]` 的固定 XPath 解析，一旦前端改版就整站失效。
新版以 JSON 状态为主路径，DOM 解析仅作兜底（且只做尽力而为的抽取）。

### 防御式归一化

线上 `msgList` 的字段结构没有任何公开文档，所以 `parser.py` **不硬编码字段路径**，
而是递归遍历对象的全部键值，按「常见键名优先 + 值形态兜底」抽取：

- 昵称：`senderName | nickName | nick | name | uinName`
- Uin：`senderUin | uin | fromUin | qq`
- 时间：`msgTime | time | sendTime | createTime`（支持秒 / 毫秒时间戳与字符串）
- 正文：`msgContent | content | text | summary | title`
- 图片 / 链接：对所有字符串做正则抽 `https?://...`，按域名与后缀判定
- 头像：`avatar | face | portrait`，缺失时用 `q1.qlogo.cn` 按 QQ 号兜底

任何一步异常都不会丢消息：会被收敛为 `type: "unknown"` 且完整保留 `raw`。

## API 一览

所有接口统一返回 `{ "ok": true, "data": ... }` 或 `{ "ok": false, "error": "...", "hint": "..." }`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 前端单页 |
| GET | `/api/config` | 读取 Cookie 配置（脱敏，只回显 uin 与 skey 末 4 位） |
| POST | `/api/config` | 保存 Cookie，body `{"cookie": "..."}` |
| POST | `/api/config/clear` | 清除 Cookie |
| GET | `/api/groups` | 群列表；`ec != 0` 时返回友好错误并提示手动输入群号 |
| POST | `/api/fetch` | 抓取并生成快照，body `{"gc": "...", "max_pages": 3, "download_images": false}` |
| GET | `/api/snapshots` | 快照索引列表 |
| GET | `/api/snapshots/<sid>` | 快照详情（meta + messages） |
| DELETE | `/api/snapshots/<sid>` | 删除快照（含图片目录） |
| POST | `/api/snapshots/<sid>/download` | 下载该快照图片 |
| GET | `/api/snapshots/<sid>/export?format=html\|md\|json` | 导出 |
| GET | `/media/<sid>/<filename>` | 图片静态服务 |

## 常见问题

**Q：群列表一直是空的 / 提示 `ec=4`？**
这是当前线上行为：`get_group_list` 接口对部分 Cookie 不接受鉴权（通常是 `skey`
作用域不足或已失效）。应用不会因此不可用——**直接在「群」卡片手动输入群号**
即可拉取精华。响应里的 `hint` 字段会说明具体原因。

**Q：拉取成功但消息数是 0？**
三种可能：该群确实没有精华消息；该账号没有该群权限；Cookie 已失效。
此时 `meta.json` 里的 `state.hasServerError` 会是 `true`，可据此诊断。

**Q：图片全是「图片已失效」？**
QQ 的图片直链带时效签名，群被封或链接过期后必然失效。建议抓取后立刻点
「下载图片」，把图片落到 `data/snapshots/<id>/images/`，之后导出的 HTML 会把
图片转成 base64 内联，永久有效。

**Q：导出的 HTML 打不开图片？**
导出时若图片未下载，会保留远程直链并带 `onerror` 降级提示。先「下载图片」再导出即可内联。

**Q：Cookie 会不会泄露？**
`data/config.json` 只在本机，已被 `.gitignore` 忽略；前端接口永远只回显
`uin` 与 `skey` 末 4 位，不会把完整 skey 返回给浏览器。

**Q：为什么有些「链接卡片 / 音乐分享」消息的标题出现了两次？**
归一化时把 `title` / `desc` / `summary` 这类键也算作正文候选，属于刻意保留的兜底
（保证在字段结构变化时「至少还能看到点内容」）。副作用是纯卡片型消息的标题会同时
出现在正文段落和下方链接卡里，`type` 也可能落到 `text` / `forward` 而非 `link`。
这只影响观感，不影响数据完整性——`raw` 原样保留，需要精确控制时可直接消费 `raw`。

**Q：端口被占用？**
`app.py` 会自动从 5000 起逐个尝试，日志里会打印实际使用的端口。
也可以显式指定：`python app.py --port 8080`。

## 安全与隐私

本项目只在**本机**运行，不上传任何数据到第三方服务器，但以下几点必须知道：

- `data/config.json` 以**明文**保存你粘贴的 Cookie。任何能读这个文件的人都能以你的
  身份访问 qun.qq.com，请勿把它提交到 Git、放进网盘或发给他人。
- `data/snapshots/` 保存的是**真实群聊内容**：成员 QQ 号、昵称、头像、聊天文本与图片。
  把它公开发布等于公开群成员的隐私信息，请自行判断合规性。
- 上面的两类内容都已被 `.gitignore` 排除（`data/` 整个目录），`git status` 里不会出现。
- 前端接口 `/api/config` 永远只回显 `uin` 与 `skey` 末 4 位，完整 skey 不会返回给浏览器。
- 导出的 HTML 已对消息内容做 XSS 转义，可安全地发给别人查看。

### 发布 / 分享源码前的自查

```bash
git status --ignored --short | grep data/   # 应只剩 data/ 被忽略的提示
grep -rn "skey=" --include="*.py" --include="*.json" . | grep -v tests/
```

若你只想分享代码、不想带走自己的存档，把 `data/` 目录整体移出项目目录即可。

## 许可证

[MIT](LICENSE)。原项目作者：<https://github.com/User-Time/requests_qq_essence>。
本项目与腾讯公司无关联，使用者需自行确保行为符合《腾讯服务协议》与相关法律法规。

## 致谢

原项目：<https://github.com/User-Time/requests_qq_essence>（旧脚本见 `legacy/`）。
谨以此项目悼念被封掉的、积累了上百条精华消息的旧群。
