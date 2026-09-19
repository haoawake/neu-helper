---
name: course-files
description: 查看和搜索本地课件 / 作业文件 —— PDF、PPTX、DOCX、XLSX、ipynb。当用户问某份课件或作业里写了什么、要看某节课的 slides、想知道作业要求全文、提到 data/downloads 下的文件、对话里出现 .pdf/.pptx/.docx/.ipynb 这类路径、或者要在所有课件里找某个知识点(「哪节课讲过 heapsort」)时,用这个。
---

# 读课件和作业文件

桌面应用把 Canvas 上的文件同步到了本地(见 [filesync.py](../../../filesync.py)):

> **macOS 上把命令里的 `python` 换成 `python3`。**那边通常没有 `python` 这个命令,
> 白名单里两种都放过了,所以直接写 `python3` 就能跑。

```
data/downloads/
  index.json                          所有文件的索引
  DS5110/
    _课程概览.md                      作业 + DDL + 要求摘要 + 附件路径
    作业/Homework 1/HW1.ipynb
    作业/Homework 1/HW1.ipynb.txt     ← 纯文本副本
    课件/Week 9/W9.pdf
    课件/Week 9/W9.pdf.txt            ← 纯文本副本,每页前面有 [p.N] 标记
```

## 最重要的一件事:每个文件旁边都有一份 `.txt`

同步的时候已经把 PDF / DOCX / PPTX / ipynb 的文字抽好了,放在**同一个目录、同名加
`.txt`**。所以:

- **搜**:直接 `Grep` `data/downloads` 里的 `*.txt`。一次就扫完所有课件,
  连 PDF 也在内。
- **读**:直接 `Read` 那份 `.txt`。PDF 的 `.txt` 里有 `[p.12]` 这样的页码标记,
  引用时说清是第几页。
- 不需要跑任何脚本,也不需要额外权限 —— Read 和 Grep 本来就能用。

先看 `_课程概览.md`:作业清单、DDL、分值、提交状态、附件在哪儿都在里面,
大部分问题一个文件就够。要作业要求全文和 rubric 再用 `canvas_assignment_detail`。

## 什么时候用脚本

`.txt` 覆盖不到的场合,才用下面这个:

```bash
python .claude/skills/course-files/scripts/coursedoc.py outline "W9.pdf"
python .claude/skills/course-files/scripts/coursedoc.py read "W9.pdf" --pages 8-14
python .claude/skills/course-files/scripts/coursedoc.py find "naive bayes" --course DS5110
python .claude/skills/course-files/scripts/coursedoc.py list DS5110
```

- `outline` —— 22 页的 slides 先看一眼每页标题,再决定读哪几页,比整份读便宜
- `read --pages` —— 只要某几页 / 某几张 slide
- `find` —— 关键词里的空白不敏感(搜 `heap sort` 也能中 `Heapsort`)
- 刚上传、`.txt` 还没生成的文件

脚本会自己挑解释器(缺 PyMuPDF 时换成项目记录的那个),直接跑就行。
**如果 Bash / PowerShell 调用被拒**(非交互会话里很常见),别纠缠 ——
退回上面那条路:Grep + Read 那些 `.txt`,覆盖率是一样的。

## 要看图的时候

公式、手写、图表、表格截图 —— `.txt` 里没有。**直接 Read 那个 PDF**
(Read 支持 PDF,能看见版面和图),必要时配合 `outline` 先定位页码。

## 回答的时候

- 说清是**哪个文件的第几页 / 第几张 slide**,不要把几份 slides 混成一段
- 课件内容和 DDL / 提交状态是两回事:**时间和状态一律用 MCP 工具现查**,
  同步下来的文件是当时的快照
- 超过 80MB 的课程录像默认没同步(DS5110 有 5 个),不要假装读过;
  用户要的话让他在课程单页那一行点「仍然下载」
- 文件不在本地就用 `course-sync` 技能同步一次。**不要**指望 `canvas_*` 工具
  拿文件正文 —— 那套是只读的元数据接口
