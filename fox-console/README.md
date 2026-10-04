# 🦊 小狐狸控制台（fox-console）

给 `E:\little fox\Jianer_Next_QQ_Bot` 这个 QQ 机器人做的**本机图形化设置界面**。
一个独立的小网站，浏览器打开就能改 AI 模型、密钥、人格话术、音色、预设、用户权限，
还能启停机器人、看实时日志、一键回滚配置。

**它不修改机器人的任何代码**，只读写配置类文件；每次写盘前自动备份，写坏了随时还原。

---

## 一、启动

双击 **`start-console.bat`**（前台窗口，会自动用机器人自带的 venv Python）；
想无窗口常驻托盘就双击 **`start-console-tray.bat`**。

```bat
cd "E:\little fox\fox-console"
"E:\little fox\Jianer_Next_QQ_Bot\venv\Scripts\python.exe" server.py          :: 前台窗口
"E:\little fox\Jianer_Next_QQ_Bot\venv\Scripts\pythonw.exe" tray.py          :: 托盘常驻（无窗口）
```

启动后窗口里会打印一行地址，**第一次必须用带 token 的那一行**：

```
访问地址   : http://127.0.0.1:5011/?token=xxxxxxxxxxxxxxxx
```

之后浏览器会记住 30 天，直接开 `http://127.0.0.1:5011/` 就行。

### 托盘模式与开机自启

- **托盘**（`tray.py`，纯 pywin32，无额外依赖）：后台起服务 + 系统托盘狐狸图标。
  双击图标打开控制台；右键菜单：打开控制台 / 复制访问地址 / 打开日志目录 / 重启机器人 / 退出控制台。
  日志写在 `logs/tray.log`（用 pythonw 启动时看不到控制台，就靠它排查）。
  **若托盘图标创建失败**（无桌面会话、被安全软件拦），会退回前台窗口模式并明确提示，不会静默失败。
- **开机自启**：「概览 → 常驻与开机自启」一个开关，做的是在启动文件夹里放/删
  `%APPDATA%\...\Startup\小狐狸控制台.bat`（纯 ASCII，调用 `start-console-tray.bat`），
  **不动注册表**；关掉开关时会先备份再删。
- 「现在就启动托盘模式」会另起一份无窗口实例（自动换端口），确认能用之后再关掉前台窗口即可。

常用参数：

| 参数 | 作用 |
|---|---|
| `--port 5010` | 换端口（默认 5010；被占用时**自动改用 5011~5019** 并打印实际地址） |
| `--bot-dir "D:\别的目录"` | 指向另一个机器人目录 |
| `--no-token` | 关闭鉴权（**只在本机调试时用**） |
| `--force-port` | 端口被占用也强行绑定（一般别用） |
| `--selftest 6` | 托盘自检：起服务 + 建图标 + 跑 6 秒消息循环后退出 |


## 二、八个页面能改什么

| 页面 | 内容 |
|---|---|
| **概览** | 机器人状态（三态判定）/ OneBot 端口 / 后端与模型 / Key 来源 / 配置文件体检 / **保存行为**（保存后自动重启）/ **常驻与开机自启** / **字段审计** / 启停按钮 |
| **AI 模型与密钥** | `ai_backend`、cloud/local 的 base_url 与模型、`cloud_reasoning`、`cloud_extra_headers`、三个 Key、Gemini 地址模型、`default_mode`、ASR 开关/模型/语言/**下载超时**/**最长处理时长**，四个「测试连通」按钮 |
| **模型注册表** | `Others.models`：供应商 providers（type/base_url/key_env/extra_headers）、模型 registry（id/provider/model/label/tags/aliases/reasoning）、`default`、`fallback_chain`，以及「从当前配置生成初始注册表」 |
| **人格与话术** | `bot_name` `bot_name_en` `reminder` `slogan`、夸奖语、戳一戳拒绝语、私聊引用/正在输入/**思考气泡**、`confused_words`（越界回话）、`affinity_daily_cap`（好感度每日上限） |
| **语音 TTS** | `Others.TTS` 的 `voiceColor` `rate` `volume` `pitch`（作为默认值生效）、在线声线清单、**试听**；每会话声线覆盖在「用户与权限」页 |
| **预设与角色** | `prerequisites/current.json` + 各预设 `.txt` 正文，可新建/改名/改适用 QQ 号/删除 |
| **用户与权限** | `owner` `ROOT_User` `Super_User.ini` `Manage_User.ini` `Auto_approval` `black_list` `silents`，以及每个会话的语音开关与声线 |
| **连接与运维** | `protocol` `Connection.*` `Log_level` `uin`、运行状态、启停/重启、实时日志（3 秒刷新） |
| **备份还原** | 列出所有自动备份，一键还原 |

### 保存后自动重启（一键生效）

「概览 → 保存行为」里有两个开关（存在 `console.json`，与机器人配置分开）：

- **保存配置后自动重启机器人**：之后任何一次保存都会带上重启；
- **触发方式**：`auto` = 只在机器人本来就在跑时重启（推荐）；`force` = 没跑就顺手启动它。

各页底部另有 **「保存并重启机器人」** 按钮（等于本次强制重启）。重启只换 `main.py`，
**NapCat 不动**；流程是「停止 → 等旧进程真的退出（最多 15 秒）→ 启动 → 等 `jianer.lock` 出现（最多 40 秒）」，
结果（含失败原因）会回显在「保存行为」卡片里。群里会有 5~15 秒无响应，正在聊天的用户上下文会重置。

### 模型注册表接到哪里

`Tools/model_registry.py`（需求⑤）从 **`config.json → Others.models`** 读取，字段与本页一致
（`providers` / `registry` / `default` / `fallback_chain`）。所以这一页配好、保存、重启即生效，
支持 `~模型` 命令族按 用户 > 群 > 全局 三级就近解析。早期版本若把 `models` 放在顶层，
保存时会被自动搬进 `Others.models` 并给出告警。

### 字段审计（界面 ↔ 代码 ↔ 配置）

「概览 → 字段审计」会扫描机器人源码（`main.py` + `AI_bot/` + `Tools/` + `plugins/` + `prerequisites/`，
不含官方向导 `app.py` 与临时脚本），把三份清单对起来：

- **代码在用但界面没有** → 机器人新加的功能字段会在这里立刻冒出来（带出处 `文件:行号`）；
- **界面有但代码没读** → 界面在骗人（example：`Others.TTS` 曾经就是这种状态，现已接线）；
- **配置里有但代码没读** → 可能是拼错或遗留。

它同时能识别 `others.get("x")`、`others["x"]`、`o = _others()` 之后的 `o.get("x")`、
以及 `_cfg_number("x", …)` 这类取值助手。**控制台自己的字段声明**来自 `app.js` 里的
`path: 'Others.xxx'` 和 `UI_EXTRA_KEYS`（给自定义编辑器用），所以两边不会各说各话。


## 三、它动哪些文件

| 文件 | 什么时候动 |
|---|---|
| `config.json` | 保存 AI / 模型注册表 / 人格 / 语音 / 连接 / 名单设置时 |
| `prerequisites/current.json` 与 `prerequisites/*.txt` | 增删改预设时 |
| `Super_User.ini` `Manage_User.ini` | 保存用户与权限时 |
| `voice_prefs.json` | 保存语音开关时（`seen` 运行时间戳只保留/删除，不会被覆盖） |
| `%APPDATA%\...\Startup\小狐狸控制台.bat` | 只有你打开/关闭「开机自启」时 |
| `fox-console/backups/` `logs/` `preview/` `console.json` | 控制台自己的备份、日志、试听文件、token 与设置 |

不动的东西：`main.py`、`AI_bot/`、`Tools/`、`plugins/`、`NapCat.Shell/`、`dist/`。

> 例外：为了让界面上的字段**真的生效**，本次对机器人侧做了三处**很小且向后兼容**的改动
> （不属于控制台运行时的行为，只在机器人启动时生效）：
> `Tools/ai_backend.py`（`cloud_reasoning` 显式配置优先，未配置时保持原来的按模型名判断）、
> `Tools/asr.py`（`asr_timeout_seconds` / `asr_max_seconds`）、
> `Tools/tts_local.py`（`Others.TTS` 作为「没指定声线时」的默认值）。
> 都是"读不到就用原默认值"，不改任何既有行为。


## 四、安全与可靠性设计

1. **只监听 127.0.0.1**，外部机器连不上；除 `/api/health` 外所有接口都要 token
   （查询串 / HttpOnly Cookie / `X-Auth-Token` 头三选一）。
2. **写前必备份**：备份命名 `原名.时间戳.原因.bak`，每种文件最多留 40 份；还原前再备份一次当前文件。
3. **原子写**：先写临时文件 + `fsync`，再 `os.replace`，断电不会留下半个 JSON；写完立刻回读校验。
4. **只认合法 UTF-8**：读不动就报错不写；写盘固定 UTF-8 无 BOM、`ensure_ascii=False`（中文不转义）、LF 换行。
5. **路径越界一律拒绝**：预设 ID / 文件名只允许目标目录下的单层文件（`..`、`/`、`\`、盘符、`~` 全拒）。
6. **不认识的配置键原样保留**，机器人以后加的新字段不会被控制台抹掉。
7. **乐观锁**：页面读到的 `mtime` 与磁盘不一致时返回 409，避免覆盖别处（比如机器人自己）刚写的内容。
8. **绝不误杀进程**：只有"命令行里确实带本项目 `main.py`"的进程才会被结束；扫描被拦时退化为
   「锁文件 PID + 进程启动时间吻合」的推断，两条路都不成立就什么都不做。
9. **幂等无损**：正常配置保存两次，文件逐字节一致（唯一会自动修的是 `ROOT_User: [""]` → `[]`，运行时等价）。

### 机器人状态的三种判定（重要）

`main.py` 用 `jianer.lock`（`O_EXCL`）做单实例锁，而它自己的存活判断**只看 PID 死活**——
PID 被系统复用时会误报"已在运行"并拒绝启动。控制台因此分三档，宁可说"不知道"也不硬猜：

| 档位 | 依据 | 界面表现 |
|---|---|---|
| `confirmed` | ① 全盘扫描到命令行带本项目 `main.py` 的进程；② **直接查锁文件里那个 PID 的命令行**（`start_jianer.bat` 用 `python main.py` 相对写法，全盘扫描会漏，必须这样补） | 「机器人运行中（PID …）」+ 说明是靠哪条路确认的 |
| `probable` | 命令行读不到（权限/沙箱）时，锁文件 PID 的**启动时间**与锁文件写入时间相差在 -180~15 秒内 | 「很可能在运行」+ 依据 + 「万一判错了：强行清理锁文件」出口 |
| `none` | 两者都不成立 | 「未运行」或「无法确认」 |

只有「命令行不是 main.py」**且**「时间也对不上」才判为**残留锁**（PID 被复用），
这时给出「清理残留锁文件」按钮（先备份再删，不碰任何进程）。

> 这条判定是拿真实运行的机器人测出来的：最初只按"绝对路径"匹配进程，而实际机器人是
> `.\venv\Scripts\python.exe main.py` 启动的，导致扫描可用时误报「未运行 + 残留锁」，
> 进而可能诱导你清锁再起第二个实例。现在按 PID 查命令行，`confirmed` 能正确命中。


### 端口占用

Windows 允许两个进程绑同一端口，所以启动时会先探测：若 5010 已被占用，
**自动改用 5011/5012…** 并打印实际地址；也可 `--port 5050` 指定，或 `--force-port` 强行绑定。
后台运行时可以用 `POST /api/shutdown {"confirm":true}` 优雅关闭（避免留下收不掉的进程）。


## 五、测试

```bat
cd "E:\little fox\fox-console"
..\Jianer_Next_QQ_Bot\venv\Scripts\python.exe tests\smoke_test.py      :: 148 项后端断言
..\Jianer_Next_QQ_Bot\venv\Scripts\python.exe tests\check_dom_refs.py  :: 前端 id 引用自洽性
..\Jianer_Next_QQ_Bot\venv\Scripts\python.exe tests\ui_smoke.py http://127.0.0.1:5011 <token>  :: 真实浏览器渲染 + 截图
```

`smoke_test.py` 全程在 `.test-sandbox\` 临时副本上操作，并且**监控每一次写盘调用的目标路径**，
最后断言所有写入（含备份目录、控制台设置、启动项）都在沙箱内 —— 这比"比对真实文件哈希"更硬
（工作区可能有别人同时在改文件）。

覆盖：鉴权（4 种方式 + 401）、配置往返无损与幂等、非法值规整、乐观锁 409、预设 CRUD、
路径穿越拒绝、同一 QQ 号挂两个预设拒绝、名单落盘、`voice_prefs.seen` 保留、备份与还原、
三态进程判定与残留锁、`stop` 不误杀、**保存后重启（auto 跳过 / force 失败路径 / 非法值）**、
**字段审计（代码↔界面↔配置）**、**模型注册表校验 + 顶层迁移 + 双份取舍**、
**控制台设置持久化**、**开机自启开关（生成/备份/删除/纯 ASCII/CRLF）**、
404 不被改写成 500、静态资源、写入越界监控。



## 六、常见问题

- **保存了但机器人行为没变**：`config.json` 是启动时读进内存的，除 `~后端` 这种即时指令外，
  大多需要**重启机器人**（页面上有按钮）。
- **试听失败**：edge-tts 需要联网；报错信息会写进 `fox-console/logs/tts.log`。
  在线声线清单拉不到时，内置的 7 个中文音色照样能用。
- **360/杀软拦 127.0.0.1 端口**：允许本机回环即可；端口冲突就 `--port 5011`。
- **token 忘了**：删除 `fox-console/console.json` 后重启，会生成新 token。
- **一键启动（含 NapCat）**：控制台只能拉起脚本，扫码登录仍需在 NapCat 窗口里完成。

## 七、目录结构

```
fox-console/
├─ server.py                   Flask 路由、token 鉴权、端口占用检测、静态页面
├─ fox_core.py                 配置读写/校验/备份/预设/名单/进程/重启/审计/注册表/自启/TTS
├─ tray.py                     托盘常驻（pywin32）：后台服务 + 图标 + 右键菜单
├─ tts_worker.py               edge-tts 子进程（列声线、合成试听）
├─ start-console.bat           前台窗口启动
├─ start-console-tray.bat      托盘模式启动（pythonw，无窗口）
├─ static/                     index.html + style.css + app.js + favicon.ico（零依赖，无构建）
├─ tests/smoke_test.py         148 项后端冒烟测试（沙箱副本 + 写入监控）
├─ tests/check_dom_refs.py     前端 id 引用静态检查
├─ tests/ui_smoke.py           真实 Chromium 渲染 + 逐页截图
├─ console.json                运行时生成：token / 端口 / 保存行为设置
├─ backups/ logs/ preview/     备份、日志（含 tray.log）、试听产物
└─ screenshots/                ui_smoke.py 产出的界面截图
```
