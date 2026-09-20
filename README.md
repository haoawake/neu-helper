# NEU Helper

> 把 Canvas、课件、邮箱和 Claude Code 放进一个真正能常驻桌面的学习助手里。

**NEU Helper** 是一个面向 Northeastern University 学生的非官方桌面助手。它直接读取 Canvas 数据，在桌面上集中展示 **DDL、成绩、公告、课程文件、每日简报和 AI 对话**；同时把 Canvas 暴露成 MCP，让 Claude Code 在任何目录里都能查询你的课程信息。

默认 Canvas 实例是 `https://northeastern.instructure.com`，也可以改成其他 Canvas 实例。

- Windows 10 / 11：可直接使用打包版
- macOS：支持源码运行，也可以在 Mac 上自行构建 `.app`
- Canvas 数据：只读
- AI：通过 Claude Code CLI
- UI：本地运行，不需要部署服务器

> 这个项目和 Northeastern University、Instructure / Canvas、Anthropic 均无官方隶属关系。

---

## 它能做什么

### 📚 Canvas 学业仪表盘

打开应用就能看到真正需要盯的东西，而不是再去 Canvas 里点八层菜单：

- 即将截止和逾期作业
- 提交状态、分值、要求摘要
- 各课程成绩
- 公告
- 课程文件和附件
- 每门课的独立详情页

仪表盘本身直接走 Canvas API，**不消耗模型额度**。

### 🤖 每日简报 + AI 对话

NEU Helper 可以调用 Claude Code，根据你的 Canvas 数据和本地同步的课件生成每日简报，也可以直接问：

```text
我这周还有什么要交？
DS5110 的 HW1 到底要求什么？
CS5800 最近有没有新公告？
这份课件哪里讲了 heapsort？
```

作业、公告、课程文件可以直接拖到聊天区作为上下文，不需要重新把名字打一遍。

### 🔌 Canvas MCP

安装脚本会把 `canvas_mcp.py` 注册成用户级 Claude Code MCP server。

因此不只是在 NEU Helper 里，在任意目录打开 Claude Code 也可以直接问课程相关问题：

```text
我最近有哪些 deadline？
帮我看看 DS5110 当前成绩。
打开 CS5800 PA1 的要求。
```

MCP 只提供读取能力，不会替你提交作业、修改成绩或删除 Canvas 内容。

### 📄 课件同步与全文搜索

课程文件可以增量同步到本地，并为 PDF / DOCX / PPTX 生成适合搜索的文本副本。

这带来两个实际好处：

1. Claude 不需要每次重新下载同一份课件
2. 可以跨多份 slides / 作业文件做全文搜索

项目内还带两个 Claude Code skill：

- `course-files`：搜索和读取本地课件
- `course-sync`：手动触发课程文件同步

### ✉️ 邮箱

NEU Helper 还有一套独立的邮箱页面：

- IMAP 收件箱
- 未读 / 重要程度 / 标签筛选
- AI 自动归类和一句话摘要
- **AI 挑链接**：一封营销信常带十几条链接（退订、隐私政策、社交图标、跟踪跳转），
  卡片上只显示 AI 认为你真的可能会点的那几条，并且**用一句话说清点进去能干什么**
  （「填这个表登记感兴趣的研究小组，9/21 10am 前」「看 CS5800 新建的这次讲座录像作业」），
  地址在下面一行小字里；其余收成一行「另外 N 条」，点开全文仍然看得到。
  实测本地 65 封带链接的邮件共 607 条链接，只留下 25 条
- 邮件每日简报
- 独立邮件对话历史
- 发件人分组
- 本地垃圾标签规则

邮件 AI 分析会缓存结果，已经分析过的邮件不会每次刷新都重新把正文塞进模型。

目前邮箱接入以 **IMAP + 授权码** 为主。对于不再支持密码式 IMAP 的学校 Outlook / Microsoft 365 邮箱，更简单的方案是先在 Outlook 中转发到一个可通过 IMAP 读取的邮箱。

### 🔄 自动检查更新

装了 Release 版之后，应用每 6 小时问一次 GitHub 有没有新版本。有的话仪表盘
顶部出现一条横幅，点「更新」就会下载、替换、自动重启 —— 你的邮件、对话、
课件、设置（`data/` 和 `CLAUDE.md`）一律不动。

- 设置 → 通用 → 版本，可以随时手动检查，也能关掉自动检查
  （这个请求会把你的 IP 告诉 GitHub）
- 横幅上的 ✕ 只是「这次先不提」，**不是永久忽略** —— 下一个版本还会提醒
- 源码版（`git clone`）不会自己替换文件，只提示你 `git pull`
- macOS 目前只提示、不代劳：替换 `.app` 的流程没在真机上验证过，
  点「更新」会打开下载页

### 🩹 安装不完整能自己补

装机脚本万一跑到一半失败(真发生过:PowerShell 把一句正常提示当成致命错误,
脚本在第 2 步终止),应用照样能开 —— 但 MCP 没注册、快捷方式没建。

这时候仪表盘顶部会出现一条橙色横幅,点「补全」就行,**不用重跑脚本、更不用
重装**。设置 → 通用 → 版本里也有这个按钮。它补的是:Canvas MCP 注册、
`CLAUDE.md`、权限配置、桌面和开机快捷方式。

> 这和「更新」是两回事:更新换的是**代码**,补全补的是**代码之外的配置**。
> 所以更新不会碰你的 MCP 注册和快捷方式。

### 🫧 三种桌面形态

应用不是只能在一个大窗口和你抢屏幕：

| 模式 | 用途 |
|---|---|
| **悬浮球** | 常驻桌面，低干扰，有状态提示 |
| **对话框** | 快速问两句 |
| **完整面板** | 仪表盘 + 简报 + 对话 + 邮箱 |

收起和恢复时会记住窗口位置、大小和之前的模式。

---

## 快速开始

## Windows：推荐直接用 Release

最新版本：

**[Download latest release](https://github.com/haoawake/neu-helper/releases/latest)**

当前 `v1.0.0` 提供：

```text
NEU-Helper-win-x64.zip
```

### 1. 解压

把 zip 解压到一个长期保留的位置。程序会在自身目录旁写入 `data/`，里面保存本地缓存、对话、简报和设置。

### 2. 创建 Canvas Access Token

在 Canvas 中进入：

```text
Account → Settings → New Access Token
```

拿到类似下面的 token：

```text
14523~xxxxxxxxxxxxxxxx
```

不要把它提交到 Git。

### 3. 运行安装脚本

在解压目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File setup.ps1 -Token "14523~xxxxxxxxxxxxxxxx"
```

安装脚本会完成凭据保存、桌面快捷方式、开机启动、Canvas MCP 注册和自检。

### 4. 打开 NEU Helper

双击桌面上的 **NEU Helper** 即可。

---

## 从源码运行

需要 **Python 3.9+**。

### Windows

```powershell
git clone https://github.com/haoawake/neu-helper.git
cd neu-helper
powershell -ExecutionPolicy Bypass -File install.ps1 -Token "14523~xxxxxxxxxxxxxxxx"
```

安装器会自动检查并安装主要 Python 依赖：

- `requests`
- `flask`
- `pywebview`

运行：

```powershell
pythonw app.py
```

也可以直接使用安装器创建的桌面快捷方式。

### macOS

```bash
git clone https://github.com/haoawake/neu-helper.git
cd neu-helper
chmod +x install.sh
./install.sh --token "14523~xxxxxxxxxxxxxxxx"
```

运行：

```bash
python3 app.py
```

### macOS 打包

目前 Release 没有预构建的 macOS 包，需要在 Mac 上构建：

```bash
./packaging/build_mac.sh
```

产物会放在 `dist/`。PyInstaller 的原生依赖与构建机器的平台和架构相关，所以 macOS 包不能在 Windows 上交叉构建。

---

## 连接 Claude Code

NEU Helper 的 **Canvas 仪表盘、邮箱列表和本地备忘录** 不依赖 Claude Code 也能运行；但下面这些功能需要本机已经安装并登录 Claude Code：

- 每日 AI 简报
- 课业 AI 对话
- 邮件 AI 分类和邮件简报
- Canvas MCP 查询
- `course-files` / `course-sync` skills

NEU Helper **不要求你另外填写 Anthropic API Key**。桌面应用直接调用本机的 `claude` CLI，所以只要 Claude Code 自己已经能正常使用，NEU Helper 就能复用同一套登录状态。

### 1. 安装 Claude Code

Anthropic 目前推荐原生安装方式。

**Windows PowerShell：**

```powershell
irm https://claude.ai/install.ps1 | iex
```

也可以用 WinGet：

```powershell
winget install Anthropic.ClaudeCode
```

**macOS / Linux / WSL：**

```bash
curl -fsSL https://claude.ai/install.sh | bash
```

macOS 也可以用 Homebrew：

```bash
brew install --cask claude-code
```

安装后先确认命令存在：

```bash
claude --version
```

如果 Windows 安装成功但终端提示找不到 `claude`，重开一个终端；原生安装器通常把它放在：

```text
%USERPROFILE%\.local\bin
```

### 2. 登录 Claude Code

第一次运行：

```bash
claude
```

然后按终端提示在浏览器中完成登录。如果已经进入 Claude Code，也可以执行：

```text
/login
```

登录完成后先随便发一句消息，确认普通 Claude Code 会话本身可以正常回答。

### 3. 让 NEU Helper 注册 Canvas MCP

通常**不用手动做这一步**。

运行 NEU Helper 的安装脚本时：

```powershell
# Windows 源码版
powershell -ExecutionPolicy Bypass -File install.ps1 -Token "14523~xxxxxxxxxxxxxxxx"
```

或：

```bash
# macOS
./install.sh --token "14523~xxxxxxxxxxxxxxxx"
```

安装器会检测 `claude` 命令，并把仓库里的 `canvas_mcp.py` 注册为名为 `canvas` 的 **user-scope MCP server**。

它做的事情本质上相当于：

```bash
claude mcp remove canvas -s user
claude mcp add canvas -s user -- python /absolute/path/to/neu-helper/canvas_mcp.py
```

macOS 上解释器通常是 `python3`；安装器会使用它实际找到的 Python 绝对路径，所以正常情况下不需要自己改命令。

`-s user` 很重要：这意味着 Canvas MCP 不只属于 NEU Helper 这个目录。注册成功后，你在**任何目录**启动 Claude Code，都可以访问同一套 Canvas 工具。

### 4. 检查 MCP 是否连接成功

可以先在终端查看：

```bash
claude mcp list
```

应该能看到名为：

```text
canvas
```

也可以启动：

```bash
claude
```

然后输入：

```text
/mcp
```

确认 `canvas` 已连接。

最后直接试一句：

```text
帮我看看最近 7 天有哪些 Canvas 作业要交。
```

如果 Claude 能调用 Canvas 工具并返回你的真实课程数据，接入就完成了。

### 5. 自动注册失败时手动添加

最常见的情况是安装 NEU Helper 时 `claude` 还不在 PATH，所以安装器只能跳过 MCP 注册。

先确认：

```bash
claude --version
python --version
```

Windows 上如果 `python` 可用，在 **neu-helper 项目目录**运行：

```powershell
claude mcp remove canvas -s user
claude mcp add canvas -s user -- python "$PWD\canvas_mcp.py"
```

macOS：

```bash
claude mcp remove canvas -s user
claude mcp add canvas -s user -- python3 "$(pwd)/canvas_mcp.py"
```

然后重新检查：

```bash
claude mcp list
```

如果 MCP 存在但读不到 Canvas，再检查 Canvas token 是否已经由安装脚本写入：

```text
~/.canvas-helper/config.json
```

不要把这个文件贴到 issue、截图或 commit 里，它包含你的 Canvas 凭据。

### 6. `CLAUDE.md` 是干什么的

首次安装会从：

```text
CLAUDE.example.md
```

生成本地：

```text
CLAUDE.md
```

这里保存的是 NEU Helper 给 Claude 的项目级使用规则，例如：

- 你的课程简称和课程名怎么对应
- 简报应该关注什么
- 本地课件应该怎么查
- 哪些信息必须重新通过 Canvas MCP 获取

**第一次安装后请把课程对照表改成你自己的课程。**

`CLAUDE.md` 不应该包含 Canvas token 或邮箱密码。它的作用是告诉 Claude “怎么理解你的课程环境”，不是当密码保险箱。人类已经发明了足够多种把密钥传上 GitHub 的方式，这个项目没必要再贡献一种。

### Claude 接入实际上分两层

| 层 | 作用 | 没有它会怎样 |
|---|---|---|
| **Claude Code CLI** | 运行 AI 对话、简报、邮件分析 | AI 功能不可用，但普通 Canvas 仪表盘仍能用 |
| **Canvas MCP** | 给 Claude 提供课程、作业、成绩、公告等实时 Canvas 数据 | Claude 能聊天，但查不到你的 Canvas 实时数据 |

所以排障时可以很快判断：

- `claude` 都跑不起来 → 先修 Claude Code 安装 / 登录
- `claude` 能聊天，但 `/mcp` 没有 `canvas` → 修 MCP 注册
- `canvas` 在，但工具报认证错误 → 修 Canvas token
- 仪表盘正常、Claude 也正常，但回答不知道课程简称 → 检查 `CLAUDE.md`

---

## 常用操作

### 桌面应用

- 悬浮球单击：恢复上一次窗口形态
- 悬浮球双击：打开完整面板
- 完整面板：查看课程、DDL、成绩、公告、文件、邮箱和简报
- 作业 / 公告 / 文件：拖到右侧聊天区后可直接围绕它提问

### 强制同步课件

可以在设置里点击 **立即同步**，也可以让 Claude Code 使用 `course-sync` skill。

### 查看本地课件

同步后的内容保存在 `data/` 下的课程目录中，并生成适合全文检索的文本副本。

---

## 数据和隐私

NEU Helper 会处理 Canvas token、邮箱授权码和你的课程 / 邮件数据，所以这部分比“液态玻璃用了几层渐变”重要得多。

### 凭据

Canvas 凭据保存在：

```text
~/.canvas-helper/config.json
```

邮箱凭据保存在同一配置目录下，而不是 Git 仓库中。

- Windows：安装器尝试通过 ACL 限制为当前用户访问
- macOS：使用用户目录权限保护
- token / 邮箱密码不会写进 README、`CLAUDE.md` 或项目源码

### Canvas 权限

当前 Canvas 集成是**只读**的。

NEU Helper 可以读取课程、作业、成绩、公告和文件，但不会：

- 提交作业
- 修改成绩
- 删除内容
- 更改课程配置

### 邮箱权限

邮箱主要用于读取和本地分类。唯一可能写回服务器的操作是已读状态，并且可以在设置中关闭。

### 本地服务

桌面 UI 背后的 HTTP 服务：

- 只监听 `127.0.0.1`
- 使用随机端口
- API 请求需要当前进程生成的 token

---

## 打包版和源码版的差异

Windows 打包版可以直接运行，不需要系统 Python。

但仓库里的两个课件辅助 skill 会调用 `python` / `python3`。因此：

- 普通 Canvas 仪表盘：正常
- AI 对话：正常，只要 Claude Code 可用
- 已同步 `.txt` 的全文搜索：正常
- 依赖 Python 的课件辅助脚本：打包版没有系统 Python 时不可用

如果你想开发项目、调试课件处理脚本或完整使用这些 skill，推荐源码版。

---

## 项目结构

核心代码大致分成下面几层：

```text
neu-helper/
├─ app.py                # 桌面应用入口
├─ server.py             # 本地 HTTP 后端与桌面业务逻辑
├─ canvas_api.py         # Canvas REST API
├─ canvas_mcp.py         # Claude Code MCP server
├─ chat_bridge.py        # Claude CLI 对话桥接
├─ briefings.py          # 学业简报
├─ filesync.py           # 课程文件增量同步与文本抽取
│
├─ mailbox.py            # IMAP 邮箱
├─ mailai.py             # 邮件 AI 分类 / 摘要
├─ mailflags.py
├─ mailparts.py
├─ mailpeople.py
│
├─ gui/
│  ├─ index.html
│  ├─ app.js
│  └─ app.css
│
├─ native_win32.py       # Windows 原生窗口实现
├─ native_cocoa.py       # macOS 原生窗口实现
├─ orb_win32.py          # Windows 悬浮球
├─ orb_cocoa.py          # macOS 悬浮球
│
├─ install.ps1
├─ install.sh
├─ selfcheck.py
└─ packaging/
```

UI 使用 pywebview 承载前端，本地 Python 后端负责 Canvas、邮件、Claude CLI、同步和状态管理；Windows / macOS 的原生窗口行为分别由 Win32 和 Cocoa 实现。

---

## 自检

仓库包含：

```bash
python selfcheck.py
```

用于检查配置、平台逻辑和一批不需要真实 GUI 交互的行为。

Windows `v1.0.0` 打包版已经进行过实际运行验证，包括窗口、悬浮球、本地后端、MCP 和安装流程。

macOS 目前主要完成代码层和逻辑自检，真实 AppKit 窗口层级、文字渲染和交互手感仍需要在不同 Mac 设备上继续验证。

---

## 为什么这个项目不是一个 Canvas 网页壳

NEU Helper 的目标不是把 Canvas 再套一层 WebView，而是把学习过程中真正分散的几件事接到一起：

```text
Canvas 数据
    ↓
桌面仪表盘 ── 本地课件索引
    ↓              ↓
每日简报 ← Claude Code → 对话 / MCP
    ↑
邮箱摘要与提醒
```

Canvas 负责事实数据，本地同步负责可搜索的课程资料，Claude 负责需要理解和总结的部分。能不用模型解决的地方就不用模型解决。

这比让 AI 每次重新读一遍所有课件和 80 封邮件既便宜，也靠谱得多。
