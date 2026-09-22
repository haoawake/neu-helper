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

**课程列表只有这学期的。** Canvas 的 `enrollment_state=active` 只挡掉它自己结课了的
注册；老师忘了手动关课的话（NEU 的学期又一个日期都不填，Canvas 也就不会自动把它算成
past enrollment），上学期的课会一直挂在在读列表里 —— 课表、课件同步、每日简报于是
都跟着算上它。所以这里按 NEU 的学期代码再筛一道：`CS5800.MERGED17-21.202710` 和
`202710_1 Fall 2026 Semester Full Term` 里那串 `202710` 就是学期，数字越大越新，
只留当季及以后的。

- 门槛是**按日历算**的，不是「取最新的那个学期」—— 下学期的课提前挂出来，不会把
  这学期的挤掉
- 换季之后还留 45 天：期末考完成绩陆续出的那几周，上学期的课还看得见
- 抽不出学期代码的课一律保留 —— 培训模块和 orientation（学期名是「默认学期」
  「Group Courses Term」）不跟学期走，没有「过期」这回事

### 🤖 每日简报 + AI 对话

NEU Helper 可以调用 Claude Code，根据你的 Canvas 数据和本地同步的课件生成每日简报，也可以直接问：

```text
我这周还有什么要交？
DS5110 的 HW1 到底要求什么？
CS5800 最近有没有新公告？
这份课件哪里讲了 heapsort？
```

作业、公告、课程文件可以直接拖到聊天区作为上下文，不需要重新把名字打一遍。

简报和对话都按 **markdown 渲染**，公式也认：`$O(n^2)$`、`$\Theta(n \log n)$`、
`$rac{n}{2}$`、`$\sum_{i=1}^{n}$` 会画成上下标、分式和符号，而不是一屏星号和
反斜杠。**没有引入 KaTeX**——这是个本地应用，没有 CDN 可用（联网渲染公式还会把
你在看什么告诉别人），而把 KaTeX 整个塞进仓库是 300KB 的字体加脚本，为偶尔一个
Θ(n log n) 不值得。所以认的是一个子集：上下标、`rac`、`\sqrt`、希腊字母和常用
符号，认不出的命令**原样显示**（显示成 `oobar{x}` 你一眼知道是没支持，猜错了
反而看不出来）。`$` 是钱还是公式单独判，「这次花了 $0.10 和 $0.20」不会被吃成
一坨乱码。

### 🔌 Canvas MCP

安装脚本会把 `canvas_mcp.py` 注册成用户级 Claude Code MCP server。

因此不只是在 NEU Helper 里，在任意目录打开 Claude Code 也可以直接问课程相关问题：

```text
我最近有哪些 deadline？
帮我看看 DS5110 当前成绩。
打开 CS5800 PA1 的要求。
```

MCP 只提供读取能力，不会替你提交作业、修改成绩或删除 Canvas 内容。

### 🗓 日程（顶层页）

**学业、邮箱、日程**——三个顶层页并列。日程是一整屏的周视图：横轴周日到周六，
纵轴时刻，上面画着四类东西：

| 画的是什么 | 从哪来 |
|---|---|
| 上课时间 | 课程首页 / syllabus 的正文，过一次模型抽出来 |
| 老师和 TA 的 office hour | 同上 |
| 面试、讲座、预约、交表截止 | **邮件**，过目那一次顺带抽的，不额外花钱 |
| 「每周」和「某一天」的备忘 | 备忘录（第三列） |

今天那一列有一条随时间走的红线；导航上那个数字是今天还剩几件事；仪表盘上还有
一行「今天接下来是什么」。收成对话框形态时这一页会退回学业页——七列的表格在
几百像素宽里读不了。

**这两样 Canvas 没有结构化接口。** 实测（2026 秋，东北大学 Canvas）：

| 想拿的东西 | 有没有接口 | 实际情况 |
|---|---|---|
| office hour | `/appointment_groups` | 空 —— 老师没用 Canvas 的 Scheduler |
| 上课时间 | `/courses/:id/sections` | 只有 section 名字，没有 meeting time |
| 任何一种 | `/calendar_events` | 整学期都是空的 |

时间实际写在**课程首页表格、syllabus 和公告的正文里**，是人写的散文：

```
Section 21, CRN 19658: Wednesdays 11:00 am - 2:20 pm  Room 1010
Office Hours:  Monday 2:00 – 3:00 PM (on campus)
```

所以 NEU Helper 的做法是：把这些正文捞出来过一次模型，抽成结构化条目存进
`data/schedule.json`，之后界面渲染只读这份存档。**源文没变就不会重抽**，
一门课几分钱。合并课（一门课底下好几个 section）会先用
`/users/self/enrollments` 查出你注册的是哪一节，只画那一节。

第一次解析要你自己点（它花钱）；之后每小时自动重抽一次，**源文没变就不调模型**，
所以老师哪天在公告里补上 TA office hour，下一个整点它自己就进格子了。

抽错了点那一格就能改；改过的条目不会被下一次解析冲掉，删掉的也不会被请回来。
`data/schedule.json` 是一直往上攒的，但**只有还在读的课会画进格子** —— 学期一换，
上学期的课时自己就退场了（解析结果留着不删，那门课万一回到在读列表，不用再花一次
模型钱）。
老师还没公布的（「TA office hour 待定」「预约制，没有固定时间」）不会被编造成
格子，而是在表格下面单独列一行说明。

**邮件里的日程和标签是同一次过目的产出**，所以一封信仍然只过一次模型（详见下面
《邮箱》）。有具体时刻的落进格子，只知道哪天的摆在表头下面那条**全天条**里——
全天的不能画成 00:00–23:59 的块，那会把纵轴撑成 0–24，两门真课挤成一条缝。

时间是**换算过的**：邮件里写 `3pm ET`，画在本机时区的 12:00，块上标「原文
15:00 ET」。`CST` 故意不换算——它既是美国中部标准时（-6）又是中国标准时（+8），
差 14 小时，猜错比不猜坏得多，那种照实标「没换算」。带日程的邮件在收件箱里挂一个
「🗓 已进日程」，点一下跳到日程页；日程表上的块点开能跳回原信。删掉一条不会再回来
（那封信不会再过第二次模型）。存量邮件要补抽，在设置 → 邮箱点「补抽日程」
（要花钱，一次最多 80 封）。

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
- **AI 抽日程**：有具体日期的事（面试、讲座、预约、交表截止）会被画进日程表，
  和标签、摘要、挑链接是**同一次调用**的产出——一封信只过一次模型，不为这个
  功能多花一分钱。抽出来的东西过一层校验才画：日期要落在收信日附近、一封最多
  两条、跨度超 12 小时的降级成全天、带「广告/社交/验证码/系统通知/订阅/诈骗」
  标签的信一律不收（除非那个发件人被你归了组）——钓鱼信最爱写「24 小时内处理」，
  那正好是个日期
- 邮件每日简报
- 独立邮件对话历史
- 发件人分组
- 本地垃圾标签规则

**级别和标签的口径。** 标签是一张**带定义**的表而不是一串名字，不然同一类信会
在几个标签之间飘。两条是踩过坑写死的：

- **「诈骗」要有实据**——域名和自称的机构对不上、索要密码或卡号、催着立刻点、
  链接指向另一个域名。光是催你交钱、说要到期了**不算**：运营商的续费提醒、宽带
  账单是正经事务。（原来没这条限制，结果自己手机卡的续费提醒被判成了诈骗；那比
  漏判一封钓鱼信更糟——真要办的事被当成骗局划掉，而且会让人不再信这个级别。）
- **级别以「我的情况」为准**，不是通用标准。设置里填的专业、住址、在用的服务、
  在找什么，那几行直接决定同一封信是 2 还是 0；过目的结论里会点名用了哪一条
  （「你填了住在 27north，这是房东发的」）。所以那几栏值得花两分钟填

邮件 AI 分析会缓存结果，已经分析过的邮件不会每次刷新都重新把正文塞进模型。

目前邮箱接入以 **IMAP + 授权码** 为主。对于不再支持密码式 IMAP 的学校 Outlook / Microsoft 365 邮箱，更简单的方案是先在 Outlook 中转发到一个可通过 IMAP 读取的邮箱。

### 🔄 自动检查更新

应用每 6 小时问一次 GitHub 有没有新版本。有的话右下角弹一条、仪表盘顶部挂一条
横幅，**点那一个按钮就完事** —— 换代码、重启一步到位，你的邮件、对话、课件、设置
（`data/` 和 `CLAUDE.md`）一律不动。**两种安装方式都是自动的**，只是走的路不一样：

| 怎么装的 | 点「更新」会做什么 |
|---|---|
| 打包版（Release 的 zip） | 下载新 zip → 换掉 exe → 重启 |
| 源码版（`git clone`） | `git fetch` → 快进到最新提交 → 重启 |

源码版**只做快进这一种操作**，不 stash、不 reset、不 checkout：

- 本地改过被 git 跟踪的文件 → 停下来告诉你是哪几个，不覆盖
- 本地有没推上去的提交、和远端分叉了 → 停下来，让你自己 push 或 rebase
- 没加进 git 的临时文件不挡着更新；`data/` 和 `CLAUDE.md` 在 `.gitignore` 里，
  git 本来就不碰

**「更新内容」给的是从你这个版本到最新版之间的全部说明。** 不是只有最新那一份
——落后三个版本的人该看到这三版加起来改了什么，中间两版做了什么否则永远不知道，
而那里面可能正有他一直在等的修复。点横幅上的「改了什么」就地展开，一版一个小标题。
源码版没有 Release notes，那就列最近几条提交标题。

**只有一个按钮，文案随状态变。** 你不该先判断"我现在属于哪种情况"再选按钮：

| 实际情况 | 按钮写什么 | 点下去做什么 |
|---|---|---|
| 有新 Release / 源码落后几个提交 | 更新并重启 | 换代码 → 自动重启 |
| 代码已经换新、但这个窗口还跑着旧的 | 重启生效 | 直接重起一份 |

第二种情况值得说一句：**"有更新吗"和"点更新"原来问的是两件不同的事** ——
前者拿 GitHub Release 和内存里的版本比，后者做 git 快进。工作区已经追平而进程
还没重启时，两者会互相打脸（横幅说"有新版本"，点下去说"已经是最新的代码了"）。
现在判据换成「**磁盘上的 `version.py` vs 内存里的 `VERSION`**」——不联网、不猜，
而且顺手覆盖了"我自己 git pull 过"这种情况。

**源码版还会看一眼 `origin`。** 检查更新问的是 Releases 接口，可源码版的更新根本
不经过 Release——代码一推上 `main` 就能快进拿到。所以它额外 `git fetch` 一次、
数一下落后几个提交，横幅上写「源码有 3 个新提交」。只做读：工作区、分支、HEAD
一概不碰。

其他：

- 设置 → 通用 → 版本，可以随时手动检查、关掉自动检查（这个请求会把你的 IP 告诉
  GitHub），也能单独关掉「有新版本时弹一条」——它**和「信息弹窗」那个开关是分开的**：
  不想被新邮件打扰，不等于不想知道有更新
- 横幅上的 ✕ 是「跳过这个版本」，**只跳过这一个** —— 下一个版本还会提醒。
  记在设置里，重开窗口还算数；手动点一次「检查更新」会把它解开
- 源码但没有 `.git`（比如直接下的 zip 源码）无从更新起，只能提示去下载页
- macOS 的**打包版**只提示、不代劳：替换 `.app` 的流程没在真机上验证过，
  点「更新」会打开下载页。源码版在 macOS 上没这个问题 —— git 就是 git

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
├─ timetable.py          # 每周课表：从课程正文里抽上课时间和 office hour
├─ memos.py              # 备忘录
│
├─ mailbox.py            # IMAP 邮箱
├─ mailai.py             # 邮件 AI:分类 / 摘要 / 挑链接 / 抽日程(同一次调用)
├─ mailevents.py         # 邮件日程:拍平、去重、套用户的删改,交给日程表画
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
