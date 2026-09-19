---
name: course-sync
description: 把 Canvas 上的课件和作业附件同步到本地 data/downloads(分门别类、增量)。当用户说同步课件、更新课程文件、"新的 slides 下来了吗"、"这周的讲义有没有"、或者你在本地找不到某个应该存在的课件时使用。也用于查看上次同步的时间和结果。
---

# 同步课件到本地

桌面应用开着的时候会**自动**同步(启动一次 + 每小时一次,增量),所以大多数时候
不用手动跑。用这个技能的场合只有两个:

1. 用户说「刚上传的东西还没下来」—— 老师刚发的材料,等不到下一个整点
2. 桌面应用没开,而你现在就需要某份课件

## 看一眼状态

> **macOS 上把命令里的 `python` 换成 `python3`。**那边通常没有 `python` 这个命令,
> 白名单里两种都放过了,所以直接写 `python3` 就能跑。

```bash
python .claude/skills/course-sync/scripts/sync_now.py --status
```

输出上次同步时间、这一轮新增/更新/跳过的数量、以及本地一共多少个文件。
**先看这个** —— 如果上次同步就在几分钟前、而且用户要的文件已经在本地,
就不用再同步了。

## 同步

```bash
python .claude/skills/course-sync/scripts/sync_now.py            # 增量,常用
python .claude/skills/course-sync/scripts/sync_now.py --force    # 连课程数据一起重拉
```

跑完会打印目录结构的变化。几十秒到一两分钟,取决于有多少新文件。

脚本会自己挑解释器(项目记录的那个,`requests` 装在那边),所以直接跑就行。

## 注意

- 同步**只下文件**,不改 Canvas 上的任何东西(整个项目的 Canvas 访问都是只读的)
- 超过 80MB 的默认跳过(课程录像动辄 100~200MB)。用户确实要某一个的话,
  让他在桌面应用的课程单页那一行点「仍然下载」,或者在设置里把上限调高
- 同步完之后要看文件内容,用 `course-files` 技能
- 如果报 401,是 Canvas token 失效了 —— 让用户重新生成再跑一次 `install.ps1 -Token "新token"`
