# NEU Helper

NEU Helper 是面向 Northeastern University 学习场景的桌面助手，整合 Canvas、课程文件、邮箱、日程、Claude Code 与本地桌面交互。

项目采用本地 Python 后端与 pywebview 前端，支持 Windows 10/11 与 macOS。Canvas 默认实例为 `https://northeastern.instructure.com`，配置项支持其他 Canvas 实例。

> 项目由社区独立维护，与 Northeastern University、Instructure / Canvas、Anthropic 保持独立项目关系。

## 核心功能

### Canvas 学业仪表盘

- 即将截止与逾期作业
- 提交状态、分值与要求摘要
- 课程成绩
- 课程公告
- 课程文件与附件
- 课程详情页
- 基于 NEU 学期代码的课程筛选
- 学期切换后的成绩查看缓冲期

Canvas 仪表盘直接使用 Canvas REST API，适合日常查询与状态浏览。

### AI 简报与对话

NEU Helper 可调用本机 Claude Code CLI，结合 Canvas 数据与本地课程文件生成：

- 每日学业简报
- 课程问答
- 作业要求摘要
- 公告分析
- 课件全文问答

聊天区支持作业、公告与课程文件拖入上下文，并提供：

- Markdown 渲染
- 常用数学公式渲染
- 消息复制
- 用户消息编辑与重新发送
- 文本选择与右键菜单
- 简报全文复制

### Canvas MCP

安装流程可将 `canvas_mcp.py` 注册为用户级 Claude Code MCP server。注册完成后，Claude Code 可在任意工作目录中查询课程、作业、成绩、公告与课程文件信息。

### 日程

日程页采用周视图，集中展示：

| 类型 | 数据来源 |
|---|---|
| 上课时间 | 课程首页、syllabus、公告 |
| Office hour | 课程首页、syllabus、公告 |
| 面试、讲座、预约、截止事项 | 邮件 AI 结构化结果 |
| 周期与单日备忘 | 本地备忘录 |

课程正文经过一次结构化提取后保存到 `data/schedule.json`。源内容哈希用于增量刷新，用户编辑结果保留在本地状态中。

邮件日程支持时区转换、全天事项、邮件关联、相似事项合并与原始邮件跳转。

### 课程文件同步与全文搜索

课程文件可增量同步到本地，并为 PDF、DOCX、PPTX 生成文本副本，支持跨文件全文检索。

仓库包含两个 Claude Code skill：

- `course-files`：搜索与读取本地课件
- `course-sync`：触发课程文件同步

### 邮箱

邮箱模块基于 IMAP，提供：

- 收件箱浏览
- 即时搜索：发件人、主题、AI 摘要、标签
- 重点标记与待处理邮件置顶
- 阅读状态、重要程度与时间组合排序
- 「盯着 N 封」快速筛选
- 单封与批量已读状态操作
- AI 分类与一句话摘要
- AI 链接筛选与用途说明
- 邮件日程提取
- 邮件每日简报
- 独立邮件对话历史
- 发件人分组
- 本地标签规则
- AI 结果缓存

邮件搜索在前端完成内存过滤，适合高频查询。

### 主题与语言

主题提供六种模式：

- 跟随系统
- 浅色
- 深色
- NEU
- 海边
- 像素

海边主题采用玻璃、水面与海景视觉；像素主题采用方角、硬投影、等宽字体、像素背景与等距草方块视觉。

界面语言支持中文与 English。语言设置同时作用于界面文案，以及新生成的 Claude 对话、简报、邮件摘要与日程标题。

### 桌面形态

应用提供三种桌面形态：

| 模式 | 主要用途 |
|---|---|
| 悬浮球 | 桌面常驻与状态提示 |
| 对话框 | 快速提问 |
| 完整面板 | 学业、邮箱、日程、简报与设置 |

Windows 提供通知区图标，macOS 提供菜单栏图标，支持打开、切换窗口形态与退出。

### 自动更新

应用每 6 小时检查 GitHub Release 与源码状态。

| 安装类型 | 更新流程 |
|---|---|
| Windows Release 包 | 下载新版本、替换应用文件、重启 |
| macOS Release 包 | 下载新版本、替换应用 bundle、迁移本地数据、重启 |
| Git 源码版 | `git fetch`、快进更新、重启 |

更新页展示当前版本、目标版本、版本说明与安装位置。源码版同时检查远端提交状态。

## 快速开始

### Windows Release

下载最新版本：

[Download latest release](https://github.com/haoawake/neu-helper/releases/latest)

解压 `NEU-Helper-win-x64.zip` 到长期使用的目录，然后创建 Canvas Access Token：

```text
Canvas → Account → Settings → New Access Token
```

运行安装脚本：

```powershell
powershell -ExecutionPolicy Bypass -File setup.ps1 -Token "14523~xxxxxxxxxxxxxxxx"
```

安装流程配置 Canvas 凭据、桌面快捷方式、开机启动、Canvas MCP 与自检。

### Windows 源码版

环境要求：Python 3.9+

```powershell
git clone https://github.com/haoawake/neu-helper.git
cd neu-helper
powershell -ExecutionPolicy Bypass -File install.ps1 -Token "14523~xxxxxxxxxxxxxxxx"
pythonw app.py
```

安装器负责主要 Python 依赖：

- `requests`
- `flask`
- `pywebview`

### macOS

```bash
git clone https://github.com/haoawake/neu-helper.git
cd neu-helper
chmod +x install.sh
./install.sh --token "14523~xxxxxxxxxxxxxxxx"
python3 app.py
```

Apple Silicon Release 提供 `NEU-Helper-mac-arm64.zip`。首次启动可使用 Finder 右键菜单中的“打开”，或通过“系统设置 → 隐私与安全性”完成启动授权。

Intel Mac 可在本机运行：

```bash
./packaging/build_mac.sh
```

构建产物位于：

```text
dist/NEU-Helper-mac-<arch>.zip
```

## Claude Code

AI 简报、AI 对话、邮件 AI、Canvas MCP 与课程 skill 使用本机 Claude Code CLI。

安装 Claude Code 后运行：

```bash
claude --version
claude
```

安装脚本会注册名为 `canvas` 的用户级 MCP server。可通过以下命令检查：

```bash
claude mcp list
```

项目首次安装时会从 `CLAUDE.example.md` 生成 `CLAUDE.md`。该文件用于保存课程简称、课程环境与 Claude 使用规则。

## 数据与隐私

### 凭据

Canvas 凭据保存在：

```text
~/.canvas-helper/config.json
```

邮箱凭据保存在同一用户配置目录。

### Canvas 权限范围

Canvas 集成采用读取权限，覆盖：

- 课程
- 作业
- 成绩
- 公告
- 课程文件

### 邮箱权限范围

邮箱模块以读取、分类、搜索与本地缓存为主，并支持邮件已读状态同步。

### 本地服务

桌面 UI 的本地 HTTP 服务采用：

- `127.0.0.1`
- 随机端口
- 当前进程生成的 API token

## 打包版与源码版

Windows Release 包可直接运行。源码版适合开发、调试、课程文件处理脚本与完整 skill 工作流。

课程辅助 skill 会调用 `python` 或 `python3`，源码环境可直接提供对应解释器。

## 项目结构

```text
neu-helper/
├─ app.py
├─ server.py
├─ canvas_api.py
├─ canvas_mcp.py
├─ chat_bridge.py
├─ briefings.py
├─ filesync.py
├─ timetable.py
├─ memos.py
├─ mailbox.py
├─ mailai.py
├─ mailevents.py
├─ gui/
│  ├─ index.html
│  ├─ app.js
│  └─ app.css
├─ native_win32.py
├─ native_cocoa.py
├─ orb_win32.py
├─ orb_cocoa.py
├─ install.ps1
├─ install.sh
├─ selfcheck.py
└─ packaging/
```

前端由 pywebview 承载，本地 Python 后端负责 Canvas、邮件、Claude CLI、课程同步与状态管理；Windows 与 macOS 分别使用 Win32 和 Cocoa 原生窗口层。

## 发版

Release 使用 `v*` tag 触发 GitHub Actions 构建。

标准流程：

1. 更新 `version.py` 中的 `VERSION`
2. 编写 `packaging/notes/v<版本>.md`
3. 创建并推送 `v<版本>` tag

Release 文案建议采用以下结构：

```markdown
# vX.Y.Z — 功能主题

## 更新内容

- 新增功能
- 交互优化
- 平台支持
- 性能调整

## 平台

- Windows: ...
- macOS: ...

## 完整变更

https://github.com/haoawake/neu-helper/compare/<previous>...vX.Y.Z
```

标题聚焦功能主题，正文聚焦用户可见行为、平台范围、兼容性、数据处理与升级方式。

## 自检

运行：

```bash
python selfcheck.py
```

自检覆盖配置、平台逻辑、更新流程与一组桌面应用行为。

## 技术定位

NEU Helper 将学习数据分为三层：

```text
Canvas / 邮箱
     ↓
本地结构化数据与课程文件索引
     ↓
桌面仪表盘 / 日程 / Claude Code / MCP
```

Canvas 与邮箱提供事实数据，本地索引负责检索与缓存，Claude Code 负责语义理解、摘要与问答。
