/* ============================================================================
   NEU Helper — 中英双语

   ## 为什么这张表按「中文原文」索引,而不是发明一套 key

   界面上有六百多条文案,散在 index.html 和 app.js 里。要是改成 t('brief.gen')
   这种 key,等于把每一处都动一遍 —— 六百个改动点,而其中一部分中文字符串
   **压根不是文案**:备忘录的种类(`每周`/`每月`)是存进 prefs 的值,还有几处
   拿中文做相等比较。挨个判断哪个能包哪个不能包,错一个就是功能坏掉。

   按原文索引就没这个问题:表里查不到的原样返回,逻辑用的那些中文永远查不到。

   ## 为什么在 DOM 边界翻,而不是在每个调用点翻

   同理 —— 不想动那四百多个调用点。这里挂一个 MutationObserver:**凡是进了
   DOM 的中文,只要表里有,就换掉**。HTML 里写死的、app.js 用 el() 建的、
   .textContent 直接赋的,一网打尽,来源无所谓。

   代价是两条:
   - 插值文案(「还剩 3 天」)在 DOM 里已经拼好了,精确匹配对不上 —— 所以除了
     精确表还有一组 RULES 正则,专门收这种
   - 数据里万一混进和表项一模一样的字符串会被误翻。键都是界面短语,
     真有一门课叫「今天」才会撞上,认了

   ## 为什么切语言要刷新页面

   表是单向的(中文 -> 英文)。往回切需要反查表,而反查在「两条中文翻成同一句
   英文」时是二义的。刷新一次就没这回事:index.html 重新拉一份干净的中文,
   所有动态内容本来就是从后端重新取的,状态一点不丢。切语言是个低频的显式
   操作,闪一下完全可以接受。

   模型用哪门语言回答是另一半,在 applang.py —— 那边管的是给 Claude 的 prompt。
   ============================================================================ */

'use strict';

const CJK = /[一-鿿]/;

let LANG = 'zh';

function setLang(v) {
  LANG = v === 'en' ? 'en' : 'zh';
  document.documentElement.lang = LANG === 'en' ? 'en' : 'zh-CN';
}

/* 翻一个字符串。三道:精确表 -> 整条正则 -> 片段替换。

   **为什么要第三道。** 界面上很多句子是拼出来的:「2026-09-22(今天)」、
   「· 生成于 09:00:12」、「上次解析 09-22 · DS5110、CS5800」—— 中间夹着数据,
   整条永远对不上表,也写不出穷尽的整条正则。片段替换就认那几个固定的连接词,
   夹在中间的数据原样留着。 */
function t(s) {
  if (LANG !== 'en' || !s) return s;
  const hit = EN[s];
  if (hit !== undefined) return hit;
  for (let i = 0; i < RULES.length; i += 1) {
    if (RULES[i][0].test(s)) return s.replace(RULES[i][0], RULES[i][1]);
  }
  let out = s;
  for (let i = 0; i < FRAGS.length; i += 1) out = out.replace(FRAGS[i][0], FRAGS[i][1]);
  return out;
}

/* ── DOM 这一侧 ── */

// 这几个属性是给人看的,一起翻
const ATTRS = ['title', 'placeholder', 'aria-label', 'alt'];

function trText(node) {
  const raw = node.nodeValue;
  if (!raw || !CJK.test(raw)) return;
  // 前后的空白要留着:HTML 里的缩进换行也算文本节点的一部分,
  // 整个换掉会把排版弄乱
  const key = raw.trim();
  const got = t(key);
  if (got !== key) node.nodeValue = raw.replace(key, got);
}

function trAttrs(elm) {
  if (!elm || !elm.hasAttribute) return;
  for (let i = 0; i < ATTRS.length; i += 1) {
    const a = ATTRS[i];
    if (!elm.hasAttribute(a)) continue;
    const v = elm.getAttribute(a);
    if (!CJK.test(v)) continue;
    const got = t(v);
    if (got !== v) elm.setAttribute(a, got);
  }
}

/* 翻一棵子树,或者一个文本节点。 */
function translateNode(node) {
  if (!node) return;
  if (node.nodeType === 3) { trText(node); return; }
  if (node.nodeType !== 1) return;
  trAttrs(node);
  const kids = node.querySelectorAll ? node.querySelectorAll('*') : [];
  for (let i = 0; i < kids.length; i += 1) trAttrs(kids[i]);
  const walk = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
  const hits = [];
  while (walk.nextNode()) hits.push(walk.currentNode);
  for (let i = 0; i < hits.length; i += 1) trText(hits[i]);
}

let observer = null;

function connect() {
  observer.observe(document.body, {
    childList: true, subtree: true, characterData: true,
    attributes: true, attributeFilter: ATTRS,
  });
}

/* 就地翻,**不排到 requestAnimationFrame**。

   原来是攒一批、下一帧统一处理,想省点力气。两个地方会因此翻不了:
   - 收成悬浮球时主窗口是隐藏的,浏览器不再给帧 —— 那段时间里长出来的文字
     就一直是中文,直到窗口重新露面
   - 无头环境里压根没有帧(这个 bug 就是在自动化测试里量出来的:表是对的、
     t() 也是对的,DOM 就是不变)

   MutationObserver 的回调本来就是微任务,已经是批量的了;而且只翻这一批
   变动过的节点,不是整棵树。省那一帧换来两个失效场景,不划算。 */
function flush(nodes) {
  // 自己改出来的变动别再喂给自己。收敛本来是天然的(英文查不到中文键),
  // 断开只是省掉一整轮空转
  observer.disconnect();
  try {
    for (let i = 0; i < nodes.length; i += 1) translateNode(nodes[i]);
  } finally {
    connect();          // 中途抛了也得把观察者接回去,否则后面全不翻了
  }
}

/* 开工:把现有的 DOM 翻一遍,再盯住后面长出来的。 */
function startI18n(lang) {
  setLang(lang);
  if (LANG !== 'en') return;          // 中文是原文,什么都不用做
  translateNode(document.body);
  observer = new MutationObserver((list) => {
    const nodes = [];
    for (let i = 0; i < list.length; i += 1) {
      const m = list[i];
      if (m.type === 'childList') {
        for (let k = 0; k < m.addedNodes.length; k += 1) nodes.push(m.addedNodes[k]);
      } else {
        nodes.push(m.target);
      }
    }
    if (nodes.length) flush(nodes);
  });
  connect();
}

/* ── 插值文案。精确表查不到时按顺序试这些 ── */

const RULES = [
  [/^还剩 (\d+) 天$/, '$1 d left'],
  [/^(\d+) 天$/, '$1 d'],
  [/^(\d+) 小时$/, '$1 h'],
  [/^(\d+) 分$/, '$1 pts'],
  [/^(\d+) 轮$/, '$1 turns'],
  [/^(\d+) 封$/, '$1 mails'],
  [/^(\d+) 条$/, '$1 items'],
  [/^(\d+) 门课$/, '$1 courses'],
  [/^(\d+)月(\d+)日 – (\d+)月(\d+)日$/, '$1/$2 - $3/$4'],
  [/^当前 (v[\d.]+)$/, 'Now on $1'],
  [/^这段约 (\$[\d.]+)$/, 'About $1 for this chat'],
  [/^本会话约 (\$[\d.]+)$/, 'About $1 this session'],
  [/^(.+) 没有每周固定安排$/, (m, a) => `${a.replace(/、/g, ', ')} has nothing weekly`],
  [/^(\d+) 条被你删掉了,所以格子里看不到 —— 重新解析也不会把它们请回来。$/,
   '$1 you deleted are hidden, so the grid looks empty. Re-parsing puts them back.'],
  [/^(.+)\(分析时自己造的标签,没有内置定义\)$/,
   '$1 (made up during analysis, no built-in definition)'],
  // 这几条里的标签名自己也要翻,所以用函数替换而不是 $1
  [/^只看「(.+)」这一类$/, (m, a) => `Only ${t(a)}`],
  [/^回列表并只看「(.+)」$/, (m, a) => `Back to the list, ${t(a)} only`],
  [/^把「(.+)」从目录里删掉\(已经打过这个标签的邮件不受影响\)$/,
   (m, a) => `Drop "${t(a)}" from the list (mails already tagged keep it)`],
  /* 「甲 —— 乙」这种两段式,两头各自再过一遍表。**放在最后** —— 它宽到
     什么都能匹配,前面任何一条更具体的规则都该先赢。标签的悬停说明
     (「财务 —— 账单、缴费…」)就长这样,两头都在表里、合起来不在。 */
  [/^上次更新没落地:你点的是 v(.+),现在跑的还是 v(.+)$/,
   'Last update did not take: you asked for v$1, this is still v$2'],
  [/^(.+?) —— (.+)$/, (m, a, b) => t(a) + ' - ' + t(b)],
];

/* 拼出来的句子:只认固定的那几段连接词,中间的数据原样留着。
   **顺序有意义** —— 长的排前面,免得短的先把它切开。 */
const WEEK_EN = { 一: 'Mon', 二: 'Tue', 三: 'Wed', 四: 'Thu',
                  五: 'Fri', 六: 'Sat', 日: 'Sun' };

const FRAGS = [
  // **必须排在 /(\d+) 项/ 前面**,不然「2 项」先被换成「2 items」,
  // 这条整句就再也匹配不上了
  [/ · 已划掉 (\d+) 项\(不计入简报\)/g,
   ' · $1 crossed out (left out of the briefing)'],
  [/加载失败:/g, 'Could not load: '],
  [/后端没响应:/g, 'The backend did not answer: '],
  [/正在进行 · /g, 'In progress · '],
  [/ · 生成于 /g, ' · generated at '],
  [/上次解析 /g, 'Last parsed '],
  [/ · 累计 /g, ' · total '],
  [/(\d+) 条抽自课程页面/g, '$1 from course pages'],
  [/(\d+) 条来自邮件/g, '$1 from mail'],
  [/(\d+) 条手加/g, '$1 added by hand'],
  [/(\d+) 条备忘/g, '$1 memos'],
  [/(\d+) 封信说的是这件事/g, '$1 mails are about this'],
  // 括号要转义。不转就是一个捕获组,只匹配「今天」两个字 —— 原来那对括号会
  // 留在原地,翻出来是「2026-09-22( (today))」
  [/\(今天\)/g, ' (today)'],
  [/ · 最近 /g, ' · last '],
  [/(\d+) 轮/g, '$1 turns'],
  [/正在生成…/g, 'generating...'],
  [/到 (\d+:\d+)/g, 'until $1'],

  /* 学业页 */
  [/(\d+) 个作业/g, '$1 assignments'],
  [/(\d+) 个未提交/g, '$1 unsubmitted'],
  [/(\d+) 个文件/g, '$1 files'],
  [/(\d+) 项/g, '$1 items'],
  // **排在 /抓取于/ 前面。** 反过来的话只吃掉后半截,
  // 剩个「数据fetched 2026-09-22 10:00」
  [/数据抓取于 /g, 'Data fetched '],
  [/抓取于 /g, 'fetched '],
  [/ · 课件在 /g, ' · files in '],
  [/超过 (\d+) MB,默认不同步/g, 'over $1 MB, not synced by default'],
  [/当前 (\d+)%/g, '$1% now'],
  [/得分 /g, 'score '],
  [/同步中 /g, 'syncing '],
  [/截止 · /g, 'due · '],
  // 后端给的那个日期(server.py 的 today:「09月22日 周二」)。
  // 它夹在「Zihao · …」里,整条对不上表,只能按片段换
  [/(\d+)月(\d+)日 周([一二三四五六日])/g,
   (m, mo, d, w) => `${+mo}/${+d} ${WEEK_EN[w] || ''}`.trim()],
  // 光秃秃一个「周三」(被删条目那张清单、日程块上的小字)。
  // **前面挡一道**:「本周 / 上周 / 下周 / 每周 / 整周」里那个「周」
  // 不是星期几,换掉会得到「下Mon」
  [/(?<![上下本每整])周([一二三四五六日])/g, (m, w) => WEEK_EN[w] || m],
  [/^截止 /g, 'Due '],
  // 搜索结果那行计数。三段是拼出来的,所以按片段换
  [/(\d+) 封匹配/g, '$1 match'],
  [/,其中 (\d+) 封未读/g, ', $1 unread'],
  [/\(只列前 (\d+) 条\)/g, ' (showing the first $1)'],
  [/^标了 (\d+) 封,有账号没成功:/g,
   'Marked $1; some accounts failed: '],

  [/^装在 /g, 'Installed at '],
  // 更新对话框里那句「下载并替换…你的数据都不动」。中间夹着包的大小,
  // 整句对不上表,只能按片段换
  [/下载并替换当前版本\(约 (\d+) MB\),装好自动重启。/g,
   'Download and replace this version (about $1 MB), then restart. '],
  [/下载并替换当前版本,装好自动重启。/g,
   'Download and replace this version, then restart. '],
  [/你的邮件、对话、课件、设置都不动。/g,
   'Your mail, chats, course files and settings are untouched.'],


  /* 日程 */
  [/现在 (\d+:\d+)/g, 'Now $1'],
  [/今天还有「(.+?)」/g, 'still today: $1 '],
  [/今天 (\d+:\d+)/g, 'Today $1'],
  [/本周 /g, 'This week '],
  [/解析中 /g, 'Parsing '],
  [/解析出错:/g, 'Parse error: '],
  [/放回了 (\d+) 条你删掉的/g, 'restored $1 you had deleted'],
  [/(\d+) 门课源文没变,沿用上次的/g, '$1 courses unchanged, reusing the last result'],
  [/读不到课表:/g, 'Could not read the schedule: '],

  /* 邮箱 */
  [/读不到邮件:/g, 'Could not read mail: '],
  [/ · (\d+) 未读/g, ' · $1 unread'],
  [/更新于 /g, 'updated '],
  // 它现在是个按钮上的文字(可点的筛选),和旁边的说明文字不一样了 —— 大写
  [/· 盯着 (\d+) 封/g, '· Watching $1'],
  [/盯了 (\d+) 天/g, 'watched for $1 d'],
  [/盯住了/g, 'Watching it'],
  [/\/ (\d+) 页 · 共 (\d+) 封/g, '/ $1 pages · $2 mails'],
  [/另外 (\d+) 条链接/g, '$1 more links'],
  [/AI 认为用不上/g, 'the AI thinks you will not need them'],
  [/正在取邮件正文和附件 /g, 'Fetching bodies and attachments '],
  [/还有 (\d+) 封没取正文/g, '$1 still without a body'],
  [/还有 (\d+) 封没取/g, '$1 still to fetch'],
  [/还有 (\d+) 封没过目/g, '$1 still to triage'],
  [/AI 正在过目 /g, 'AI is triaging '],
  [/过目中 /g, 'Triaging '],
  [/已过目 (\d+) 封/g, '$1 triaged'],
  [/取正文中 /g, 'Fetching bodies '],
  [/正在取 /g, 'Fetching '],
  [/回列表并只看「(.+?)」/g, 'Back to the list, only $1'],
  [/只看「(.+?)」这一类/g, 'Only $1'],
  [/「(.+?)」这一组还没有来信。/g, 'Nothing from the $1 group yet.'],
  [/没有「(.+?)」这一类的邮件。/g, 'No $1 mail.'],
  [/(.+?) 这天没有邮件。/g, 'No mail on $1.'],
  [/这个发件人被你归进「(.+?)」/g, 'You put this sender in $1'],
  [/每天 (\d+):00 自动出一份邮件简报/g,
   'A mail briefing comes out at $1:00 every day'],
  [/还没有简报。每天 (\d+):00 会自动生成一份。/g,
   'No briefing yet. One is written at $1:00 every day.'],
  [/现在想看就点「生成今日」。/g, 'Want one now? Hit Generate today.'],

  /* 备忘录 / 更新 */
  [/(\d+) 条未完成/g, '$1 open'],
  [/第 (\d+) 周期/g, 'cycle $1'],
  [/上次 /g, 'last '],
  [/新增 (\d+) 更新 (\d+)/g, '$1 new, $2 updated'],
  [/有新版本 v([\d.]+)/g, 'New version v$1'],
  [/你现在是 v([\d.]+)/g, 'you are on v$1'],
  [/源码有 (\d+) 个新提交/g, '$1 new commits upstream'],
  [/代码已是 v([\d.]+),重启生效/g, 'code is on v$1, restart to apply'],
  [/源码落后 (\d+) 个提交/g, '$1 commits behind'],
  [/已经是最新的\(v([\d.]+)\)/g, 'Up to date (v$1)'],
  [/代码已经更新到 v([\d.]+)/g, 'The code is now on v$1'],
  [/你这个窗口还跑着 v([\d.]+),重启一下生效/g,
   'this window is still running v$1 - restart to apply'],
  [/点「更新并重启」快进到最新/g, 'hit Update and restart to fast-forward'],
  /* 版本那一行:当前 vX · 最新 vY —— 怎么办 */
  [/当前 v/g, 'now v'],
  [/最新 v/g, 'latest v'],
  [/ —— 重启生效/g, ' - restart to apply'],
  [/ —— 可以更新/g, ' - update available'],
  [/ —— 已是最新/g, ' - up to date'],
  [/ —— 源码落后 (\d+) 个提交/g, ' - $1 commits behind'],

  [/检测到更新,(\d+) 个版本的更新内容如下:/g,
   'An update is available. Here is what changed across $1 versions:'],

  /* 这一条是拼出来的:中间夹着日期,整条永远对不上表 */
  [/这条是从邮件里抽出来的/g, 'This came out of an email'],
  [/。时间留空 = 全天,摆到顶上那条全天条里;删掉不会再回来。/g,
   '. Leave the time empty for an all-day entry in the strip at the top; deleting one is permanent.'],

  // **兜底,放在最后。** 前面的规则可能只换走半句,留下一个光秃秃的「——」。
  // 破折号在英文里不这么用,统一收成一个连字符
  // 顿号英文里没有,换成逗号。**放在最后一条** —— 前面那些规则里
  // 有拿顿号当锚点的
  [/、/g, ', '],
  [/ —— /g, ' - '],
];

/* ── 表。左边是界面上的中文原文,一字不差 ──
   顺序大致按它在界面上出现的位置:顶栏 -> 学业 -> 日程 -> 邮箱 -> 设置。 */

const EN = {
  /* 顶栏 / 窗口 */
  '学业': 'Study',
  '邮箱': 'Mail',
  '日程': 'Schedule',
  '收成对话框': 'Collapse to chat',
  '刷新': 'Refresh',
  '钉在最前面': 'Keep on top',
  '设置': 'Settings',
  '收成悬浮球': 'Collapse to orb',
  '最小化': 'Minimize',
  '关闭': 'Close',
  '拖动移动窗口 · 双击展开': 'Drag to move, double-click to expand',
  '子页面': 'Section',
  '重新抓取 Canvas 数据': 'Re-fetch from Canvas',
  '最小化到任务栏': 'Minimize to the taskbar',

  /* 更新横幅 */
  '补全': 'Repair',
  '更新并重启': 'Update and restart',
  '改了什么': 'What changed',
  '这次先不提': 'Skip this one',

  /* 学业页 */
  '距离下一个截止': 'until the next deadline',
  '天': 'd',
  '小时': 'h',
  '课程表': 'Schedule',
  '待办': 'To do',
  '显示已提交': 'Show submitted',
  '只看未提交': 'Unsubmitted only',
  '成绩': 'Grades',
  '公告': 'Announcements',
  '返回': 'Back',
  '返回仪表盘': 'Back to the dashboard',
  '本地文件夹': 'Local folder',
  '同步课件': 'Sync files',
  '在 Canvas 打开': 'Open in Canvas',
  '作业': 'Assignments',
  '课件': 'Files',
  '问 Claude 这个作业': 'Ask Claude about this',

  /* 右栏 */
  '松手就把它关联到这次对话': 'Drop it here to attach it to this chat',
  '简报': 'Briefing',
  '对话': 'Chat',
  '备忘录': 'Memos',
  '复制': 'Copy',
  '生成今日': 'Generate today',
  '新对话': 'New chat',
  '删除': 'Delete',
  '发送': 'Send',
  '选择日期': 'Pick a date',
  '复制整份简报的原文': 'Copy the whole briefing as text',
  '选择对话': 'Pick a chat',
  '问点什么…': 'Ask something...',
  'Enter 发送 · Shift+Enter 换行': 'Enter to send, Shift+Enter for a new line',

  /* 备忘录 */
  '新增一条': 'Add one',
  '纯文字': 'Note',
  '某一天': 'On a day',
  '每周': 'Weekly',
  '每月': 'Monthly',
  '周一': 'Mon', '周二': 'Tue', '周三': 'Wed', '周四': 'Thu',
  '周五': 'Fri', '周六': 'Sat', '周日': 'Sun',
  '记下': 'Save',
  '取消': 'Cancel',
  '要记什么': 'What should I remember',

  /* 日程页 */
  '本周日程': 'This week',
  '全天': 'All day',
  '新增': 'Add',
  '重新解析': 'Re-parse',
  '上一周': 'Previous week',
  '下一周': 'Next week',
  '回到本周': 'Back to this week',
  '回到学业页': 'Back to Study',
  '在「只画有课的时段」和「0–24 全画」之间切':
    'Toggle between busy hours only and the full 0-24',
  '重新从课程正文里抽一遍(要跑模型)':
    'Extract again from the course pages (runs the model)',

  /* 日程条目面板 */
  '改这一条': 'Edit this one',
  '名称': 'Name',
  '类型': 'Kind',
  '上课': 'Lecture',
  '其他': 'Other',
  '时间': 'Time',
  '谁': 'Who',
  '地点': 'Where',
  '链接': 'Link',
  '备注': 'Note',
  '保存': 'Save',
  '还原': 'Revert',
  '看这封邮件': 'Open that mail',
  '课程简称': 'Course code',
  '老师 / TA 的名字': 'Instructor / TA',
  '教室号 / Zoom': 'Room / Zoom',
  'Zoom / 预约链接': 'Zoom / booking link',
  '丢掉手改,回到抽出来的样子': 'Discard your edits, back to what was extracted',
  '这条是从邮件里抽出来的,去看那封信': 'This came out of an email, open it',

  /* 邮箱页 */
  '垃圾箱': 'Trash',
  '所有人': 'Everyone',
  '时间 · 新→旧': 'Date, newest first',
  '时间 · 旧→新': 'Date, oldest first',
  '按重要程度': 'By importance',
  '所有日期': 'All dates',
  '所有级别': 'All levels',
  '普通以上': 'Normal and up',
  '留意以上': 'Worth a look and up',
  '只看要紧的': 'Important only',
  '只看未读': 'Unread only',
  '收取': 'Fetch',
  '松手就把这封邮件关联到对话': 'Drop it here to attach this mail to the chat',
  '邮件简报': 'Mail briefing',
  '邮件对话': 'Mail chat',
  '切到垃圾箱': 'Switch to Trash',
  '按发件人分组': 'By sender group',
  '排序': 'Sort',
  '看哪一天': 'Which day',
  '按重要程度过滤': 'Filter by importance',
  '选择账号': 'Pick an account',

  /* 设置 · 外观 */
  '设置分栏': 'Settings tabs',
  '通用': 'General',
  '外观': 'Appearance',
  '主题': 'Theme',
  '跟随系统': 'System',
  '浅色': 'Light',
  '深色': 'Dark',
  '海边': 'Beach',
  '像素': 'Pixel',
  '语言': 'Language',
  '玻璃模糊': 'Glass blur',
  '表面不透明度': 'Surface opacity',
  '整窗透明度': 'Window opacity',
  '过渡动画': 'Animations',
  '0 = 关掉模糊,最省电': '0 turns the blur off, easiest on the battery',
  '页面里玻璃板自己的底色;低于 45% 正文会发灰':
    'How solid the glass panels are. Below 45% the text starts to grey out',
  '真的透到桌面,代价是文字也跟着变透':
    'Really see-through to the desktop, and the text goes with it',

  /* 设置 · 悬浮球 / 弹窗 / 窗口 */
  '悬浮球': 'Floating orb',
  '直径': 'Diameter',
  '把球放回右下角': 'Put the orb back in the corner',
  '现在就收成球': 'Collapse to the orb now',
  '球:单击还原 · 双击展开 · 右键菜单 · 拖动挪位置':
    'Orb: click to restore, double-click to expand, right-click for the menu, drag to move',
  '信息弹窗': 'Notifications',
  '开启': 'On',
  '停留': 'Stay for',
  '秒': 's',
  '试一条': 'Try one',
  'Canvas 有新作业/新公告、或者来了新邮件时,右下角弹一条横条':
    'Pop a card in the corner when Canvas has new work or a new mail arrives',
  '窗口': 'Window',
  '对话框保持在最前': 'Keep the chat window on top',
  '只管对话框那一态;要所有形态都置顶,用标题栏的 📌':
    'Only the chat shape. For every shape, use the pin in the title bar',
  '开机自动打开': 'Open at login',
  '登录时自动开一次。闸门每天只放行一次,重启几遍也只会弹一个窗口':
    'Opens once when you log in. The gate lets it through once a day, so restarting a few times still gives you one window',
  '没能加上开机项': 'Could not add the login item',
  '没能去掉开机项': 'Could not remove the login item',
  '改不了开机项': 'Could not change the login item',
  '打开数据目录': 'Open the data folder',
  '打开启动文件夹': 'Open the startup folder',

  /* 设置 · 版本 */
  '版本': 'Version',
  '自动检查更新': 'Check for updates',
  '有新版本时弹一条': 'Pop a card for new versions',
  '补全安装': 'Repair the install',
  '现在检查': 'Check now',
  '每 6 小时问一次 GitHub 有没有新版本。会把你的 IP 告诉 GitHub':
    'Asks GitHub every 6 hours. That tells GitHub your IP',
  '有新版本时右下角报一句。和「信息弹窗」那个开关分开 —— 不想被新邮件打扰,不等于不想知道有更新':
    'A card in the corner for new versions. Separate from the notification switch: not wanting mail popups is not the same as not wanting updates',

  /* 设置 · 每日简报 */
  '每日简报': 'Daily briefing',
  '播报时刻': 'Time of day',
  '点(每天一次)': ':00, once a day',
  '带几天历史': 'Days of history',
  '立即生成今天的简报': 'Generate the briefing now',
  '把前几天的简报原文一起给模型,好说出变化;0 = 不带':
    'Feeds the last few briefings to the model so it can say what changed. 0 = none',

  /* 设置 · 数据口径 */
  '数据口径': 'What counts',
  '待办往后看': 'Look ahead',
  '几天内算紧急': 'Urgent within',
  '公告往前看': 'Announcements back',
  '重点课程': 'Focus courses',
  '列出划掉的条目': 'List dismissed items',
  '全部恢复': 'Restore all',
  '临近 = 这个数的两倍以内,再往后算充裕':
    'Anything within twice this is coming up; past that is comfortable',
  '填了之后简报优先盯这几门,其余的一句带过':
    'The briefing gives these the space and mentions the rest in one line',
  '关掉之后它们从列表里消失,存档还在,能全部恢复':
    'Turn it off and they leave the list. Nothing is deleted, you can restore them',

  /* 设置 · 课件同步 */
  '课件同步': 'File sync',
  '自动同步到本地': 'Sync to this computer',
  '单文件上限': 'Per-file limit',
  '立即同步': 'Sync now',
  '打开课件目录': 'Open the files folder',
  '启动后一次、之后每小时一次,增量;存到 data/downloads/':
    'Once at start, then hourly, incremental. Goes to data/downloads/',

  /* 设置 · 课程表 */
  '上课时间和 office hour': 'Class times and office hours',
  'Canvas 没有结构化接口': 'Canvas has no structured endpoint for these',
  '用哪个模型': 'Model',
  'haiku(便宜)': 'Haiku (cheap)',
  'sonnet(均衡)': 'Sonnet (balanced)',
  'opus(最准)': 'Opus (most accurate)',
  '邮件里的日程也画上': 'Draw mail events too',
  '备忘录也画进去': 'Draw memos too',
  '重新解析一次': 'Parse again',
  '清空课表': 'Clear the schedule',
  '邮件里有具体日期的事(面试、讲座、截止)也画进格子;只知道哪天的摆在顶上那条全天条里':
    'Dated things from your mail (interviews, talks, deadlines) go on the grid too. Day-only ones sit in the all-day strip',
  '备忘录里「每周」和「某一天」那两种,按 30 分钟的块画进格子':
    'The weekly and on-a-day memos, drawn as 30-minute blocks',
  '连手改和手加的一起清掉': 'Wipes your edits and hand-added entries too',

  /* 设置 · 我的情况 / AI 过目 */
  '我的情况': 'About me',
  '加一条': 'Add one',
  'AI 过目': 'AI triage',
  '新邮件自动过目': 'Triage new mail automatically',
  'Haiku · 最省': 'Haiku, cheapest',
  'Sonnet · 均衡(推荐)': 'Sonnet, balanced (recommended)',
  'Opus · 最准': 'Opus, most accurate',
  '取正文和附件': 'Fetch bodies and attachments',
  '过目剩下的': 'Triage the rest',
  '重挑链接': 'Re-pick links',
  '补抽日程': 'Backfill events',
  '全部重判': 'Re-judge everything',
  '这几行会原样进 AI 过目和邮件简报,作为判断依据':
    'These lines go verbatim into the triage and the mail briefing as the basis for judging',
  '打标签 / 评级 / 写摘要。每封信只过一次模型,结果存档复用':
    'Labels, level, summary. One model pass per mail, then the result is reused',
  '链接和附件要整封下下来才有。取过的不会再取第二次':
    'Links and attachments need the full message. Nothing is fetched twice',
  '只给「带链接但还没挑过链接」的信补一次 —— 比全部重判便宜,标签和摘要也不会被推翻':
    'Only for mails that have links but no picks yet. Cheaper than a full re-judge, and it leaves labels and summaries alone',
  '给最近 80 封里还没抽过日程的补一次 —— 面试、讲座、截止会画进日程表。顺带把这几封的标签和摘要一起按新规则重判':
    'Backfills events for up to 80 recent mails, so interviews, talks and deadlines land on the schedule. Their labels and summaries get re-judged too',
  '作废现有结论、按新的个人信息重看一遍(要再花一次钱)':
    'Throws away the current results and re-reads everything against your new profile (costs again)',

  /* 设置 · 标签 / 分组 / 盯住 */
  '标签目录': 'Label list',
  '坏标签 —— 这类信收进垃圾箱': 'Bad labels, these go to Trash',
  '加': 'Add',
  '恢复默认': 'Reset to defaults',
  '新标签,比如 实习': 'New label, e.g. Internship',
  '这份目录会进分析 prompt。往下面那个框里拖 = 设成坏标签':
    'This list goes into the triage prompt. Drag one into the box below to mark it bad',
  '拖进来 = 设成坏标签;拖回上面 = 收回来': 'Drag in to mark it bad, drag back up to undo',
  '发件人分组': 'Sender groups',
  '新分组,比如 导师': 'New group, e.g. Advisor',
  '分组跟着人走。分了组的人不会被坏标签藏起来;给谁分组:点开那封信,发件人旁边有下拉':
    'Groups follow the address. Grouped senders are never hidden by a bad label. To group someone, open the mail, there is a dropdown next to the sender',
  '还在盯的邮件': 'Mails you are watching',
  '在邮件列表里点 ☆ 就是盯住;简报会一直提醒到你标完成':
    'Star one in the list to watch it. The briefing keeps nagging until you mark it done',

  /* 设置 · 邮箱 */
  '后台收邮件': 'Fetch in the background',
  '简报时刻': 'Briefing time',
  '读过就同步已读': 'Mark as read on the server',
  '唯一会改动你邮箱的操作:只动「已读」这一个标记':
    'The one thing that writes to your mailbox, and it only touches the read flag',
  '账号': 'Accounts',
  '添加账号(QQ / 163 / Gmail / 其他 IMAP)': 'Add an account (QQ / 163 / Gmail / other IMAP)',
  '授权码': 'app password',
  'QQ 邮箱': 'QQ Mail',
  '网易 163': 'NetEase 163',
  '其他 IMAP': 'Other IMAP',
  '添加并验证': 'Add and verify',
  '邮箱地址': 'Email address',
  '授权码 / 应用专用密码': 'App password',
  'IMAP 服务器(选「其他」时填)': 'IMAP server (fill this in for Other)',

  /* ── 下面这些是 app.js 建出来的,不在 index.html 里 ── */

  /* 对话 */
  '我': 'Me',
  '关于': 'About',
  '出错': 'Error',
  '试试问': 'Try asking',
  '编辑': 'Edit',
  '复制这条的原文': 'Copy this message as text',
  '改一改,重新发一次': 'Edit it and send again',
  '复制整条': 'Copy the whole message',
  '复制整份简报': 'Copy the whole briefing',
  '全选这块': 'Select this block',
  '剪切': 'Cut',
  '粘贴': 'Paste',
  '全选': 'Select all',
  '已复制': 'Copied',
  '复制不了': 'Could not copy',
  '已剪切': 'Cut',
  '剪不动': 'Could not cut',
  '这儿是空的': 'Nothing here',
  '剪贴板是空的': 'The clipboard is empty',
  '读不到剪贴板,用 Ctrl+V': 'Cannot read the clipboard — use Ctrl+V',
  '发出去之后,这条以下的都不作数了': 'Everything below this message will be dropped',
  '等这轮答完': 'Wait for this answer',
  '确认删除?': 'Really delete?',
  '思考中…': 'Thinking...',
  '连接中…': 'Connecting...',
  '解析中…': 'Parsing...',
  '切换中…': 'Switching...',
  '这周我该先做哪个': 'What should I start with this week',
  '最近的作业具体要求是什么': 'What exactly does the next assignment ask for',
  '我有没有漏掉的公告': 'Did I miss any announcements',
  '各科成绩现在怎么样': 'How are my grades looking',

  /* 空状态 */
  '都完成了': 'All done',
  '没有待办 —— 未来四周是干净的。': 'Nothing to do — the next four weeks are clear.',
  '还没有备忘。点「新增」,或者把左边的作业、公告、邮件拖到这儿。':
    'No memos yet. Hit Add, or drag an assignment, announcement or mail in from the left.',
  '当前没有划掉的条目': 'Nothing is dismissed right now',
  '一条都没填。点「加一条」开始。': 'Nothing here yet. Hit Add one to start.',
  '目录是空的 —— 分析时它会自己造标签。':
    'The list is empty — triage will invent labels on its own.',
  '把标签拖到这儿': 'Drag a label in here',
  '安装是完整的': 'The install is complete',
  '不透明': 'Opaque',
  '份': '',

  /* 日程页 */
  '删掉的': 'Deleted',
  '恢复': 'Restore',
  '打开这封信': 'Open this mail',
  '这封信已经不在本地索引里了': 'That mail is no longer in the local index',
  '其他': 'Other',
  '邮件': 'Mail',
  '加一条': 'Add one',
  '这条来自邮件': 'This came from an email',

  /* 长说明 */
  '换成新代码并自动重启,你的邮件/对话/课件/设置都不动':
    'Swaps in the new code and restarts. Your mail, chats, files and settings are untouched',
  '按 API 标价折算的估值。订阅席位不按此扣费,但会消耗用量额度。':
    'An estimate at API list prices. A subscription seat is not billed this way, but it does use up quota.',
  'Outlook / 学校邮箱:在 Outlook 里设置转发到 QQ。':
    'Outlook / school mail: set up forwarding to QQ inside Outlook.',
  'QQ 要的是': 'QQ wants an',
  ',不是登录密码:QQ 邮箱 → 设置 → 账号 →\n              「IMAP/SMTP 服务」→ 开启 → 发短信验证 → 拿到 16 位授权码。':
    ', not your login password: QQ Mail -> Settings -> Account -> enable IMAP/SMTP -> verify by SMS -> you get a 16-character code.',
  '—— 它们是从课程首页、syllabus 和公告的正文里抽出来的,所以要跑一次模型\n             (一门课几分钱,源文没变就不会重抽)。抽错了在课程表里点那一格就能改,\n             改过的不会被下一次解析冲掉。':
    '- they are pulled out of the course home page, the syllabus and the announcements, which means running the model once (a few cents per course; nothing is re-read if the source has not changed). Got one wrong? Click the block in the schedule and fix it - your edit survives the next parse.',
  '一行一个课程简称,比如\nCS5800\nDS5110': 'One course code per line, e.g.\nCS5800\nDS5110',

  /* 学业页 · 列表与卡片 */
  '已过': 'past',
  '今天': 'today',
  '未命名': 'Untitled',
  '读取中…': 'Loading...',
  '读取失败': 'Could not load',
  '读不到这门课': 'Could not read this course',
  '全部已提交': 'all submitted',
  '已同步到本地': 'Synced to this computer',
  '还没同步': 'Not synced',
  '打开': 'Open',
  '文件夹': 'Folder',
  '在资源管理器里打开所在目录': 'Show the folder in Explorer',
  '仍然下载': 'Download anyway',
  '现在下载': 'Download now',
  '下载中…': 'Downloading...',
  '重试': 'Retry',
  '下载失败': 'Download failed',
  '这门课没有列出作业。': 'This course lists no assignments.',
  '已提交': 'Submitted',
  '未提交': 'Not submitted',
  '无期限': 'No due date',
  '附件': 'Attachments',
  '问 Claude': 'Ask Claude',
  '作业文件夹': 'Assignment folder',
  '打开这个作业在本地的目录': 'Open this assignment folder on this computer',
  '这门课没有课件,或者老师没开放文件区 / 模块。':
    'No files for this course, or the instructor has not opened the files area.',
  '打开这个模块在本地的目录': 'Open this module folder on this computer',
  '其他文件': 'Other files',
  '最近十天没有公告。': 'No announcements in the last ten days.',
  '最近三周没有公告。': 'No announcements in the last three weeks.',
  '打不开': 'Could not open',
  '打不开那个位置': 'Could not open that location',
  '整理中…': 'Tidying up...',
  'install 脚本': 'the install script',
  '配置问题': 'Setup problem',
  'Canvas 连不上': 'Cannot reach Canvas',

  /* 日程页 */
  '备忘': 'Memo',
  '改': 'edited',
  '还没读取': 'Not loaded yet',
  '还没解析过 —— 点进去抽一次': 'Not parsed yet - open it and run one pass',
  '今天没有固定日程': 'Nothing scheduled today',
  '还没解析过。点「重新解析」让它去读课程首页和 syllabus。':
    'Never parsed. Hit Re-parse and it will read the course pages and syllabus.',
  '时间留空 = 全天,摆到顶上那条全天条里;删掉不会再回来。':
    'Leave the time empty for an all-day entry in the strip at the top. Deleting one is permanent.',
  '这条是从课程页面里抽出来的。改了之后,重新解析不会把你的改动冲掉。':
    'This was extracted from the course pages. Your edits survive the next parse.',
  '(没有主题)': '(no subject)',
  '没存上': 'Could not save',
  '结束时间要晚于开始时间': 'The end time has to be after the start time',
  '连手改的一起清?': 'Clear your edits too?',
  '已清空': 'Cleared',

  /* 邮箱 */
  '全部账号': 'All accounts',
  '(未就绪)': '(not ready)',
  '还没有邮箱账号 —— 打开设置 → 邮箱,加一个。':
    'No mail account yet - open Settings -> Mail and add one.',
  '收取中…': 'Fetching...',
  '(筛选后)': '(filtered)',
  '看全部': 'Show all',
  '垃圾箱是空的。在设置 → 邮箱 → 标签目录里点 🗑 把某个标签设成坏标签,':
    'Trash is empty. In Settings -> Mail -> Label list, hit the bin on a label to mark it bad,',
  '带这个标签的信就会收进这儿。': 'and mail with that label lands here.',
  '这个级别以上没有邮件。': 'No mail at this level or above.',
  '没有未读。': 'Nothing unread.',
  '收件箱是空的(或者账号还没配好)。':
    'The inbox is empty (or the account is not set up yet).',
  '未读': 'unread',
  '(无主题)': '(no subject)',
  '这封还没过目,先显示主题': 'Not triaged yet, showing the subject',
  '已进日程': 'on the schedule',
  '这封信里有带日期的事,已经画进日程表了 —— 点这里去看':
    'This mail had something dated in it and it is on the schedule - click to see it',
  '没写理由': 'No reason given',
  '在网页打开': 'Open on the web',
  '标完成': 'Mark done',
  '标完成之后简报就不再提这封了': 'Once done, the briefing stops mentioning it',
  '盯住它:每天的邮件简报都会点名提醒,直到你标完成':
    'Watch it: the daily mail briefing keeps naming it until you mark it done',
  '盯住它:每天的邮件简报都会提醒到你标完成':
    'Watch it: the daily mail briefing keeps nagging until you mark it done',
  '打开这封(全文、附件、全部链接)': 'Open it (full text, attachments, every link)',
  '(无发件人)': '(no sender)',
  '上一页': 'Previous page',
  '下一页': 'Next page',
  '输入页码后回车跳转': 'Type a page number and press Enter',
  '超过设置里的单个附件上限,没有自动下载':
    'Over the per-file limit in Settings, so it was not downloaded',
  '\n单击打开 · 右键打开所在文件夹': '\nClick to open, right-click for the folder',
  /* 链接前面那个小标签(LINK_KINDS)。首字母大写 —— 同一排里还有
     「Canvas」「PDF」这种本来就大写的,小写的挤在中间很扎眼。
     「会议」在邮件标签那一档也用得上,一个词管两处 */
  '表单': 'Form', '职位': 'Job', '会议': 'Meeting', '日历': 'Calendar',
  '网盘': 'Drive', '视频': 'Video', '验证': 'Verify', '订单': 'Order',
  '账单': 'Bill',
  '返回列表': 'Back to the list',
  '返回列表(Esc)': 'Back to the list (Esc)',
  '标为已读': 'Mark as read',
  '标回未读': 'Mark as unread',
  '在邮箱里也标成已读': 'Mark it read on the server too',
  '在邮箱里也标回未读': 'Mark it unread on the server too',
  '取消盯住': 'Stop watching',
  '未分组': 'No group',
  '把这个发件人归一组:他的信不会被坏标签藏起来,也能单独筛出来看':
    'Put this sender in a group: their mail is never hidden by a bad label, and you can filter to it',
  '分组没存上:': 'Could not save the group: ',
  '正在取全文…': 'Fetching the full text...',
  '取不到全文(服务器上可能已经没有这封信了)':
    'Could not fetch the full text (the server may not have this message any more)',
  '这封信整体超过 25MB(多半是巨型附件),没有取正文 —— 去网页版看。':
    'The whole message is over 25MB (usually a huge attachment), so the body was not fetched - use the web client.',
  '这封信没有正文。': 'This message has no body.',
  '回收件箱': 'Back to the inbox',
  '切到垃圾箱(坏标签的信在这儿)': 'Switch to Trash (mail with bad labels lives here)',
  '全部': 'All',
  '过目失败:': 'Triage failed: ',
  '还没有邮件简报': 'No mail briefing yet',
  '读不到:': 'Could not read: ',
  '(空)': '(empty)',
  '正在生成邮件简报…': 'Writing the mail briefing...',
  '针对': 'About',
  '取消关联': 'Detach it',
  '打不开那封信:': 'Could not open that mail: ',
  '那封信已经不在本地索引里了': 'That mail is no longer in the local index',
  '同步已读失败:': 'Could not sync the read flag: ',
  '现在一封都没盯着。': 'You are not watching anything right now.',
  '刚标的': 'just now',

  /* 账号 */
  '还没有账号。': 'No accounts yet.',
  '已就绪': 'Ready',
  '未就绪': 'Not ready',
  '移除': 'Remove',
  '验证中…': 'Verifying...',
  '出错:': 'Error: ',
  '加好了,正在收邮件': 'Added, fetching mail now',
  '登不上:': 'Could not sign in: ',
  '未知原因': 'Unknown reason',
  '开始取…': 'Starting...',
  '都取过了': 'Everything has been fetched',
  '这个文件下不下来': 'This file cannot be downloaded',

  /* 简报 */
  '正在生成今天的简报…': "Writing today's briefing...",
  '不想等就点右上角「生成今日」。': 'Do not want to wait? Hit Generate today up there.',
  '还没有存档的简报。': 'No archived briefings yet.',
  '读取这一天的简报失败。': 'Could not load that day.',

  /* 备忘录 */
  '展开完整面板': 'Expand the full panel',
  '取消置顶': 'Unpin',
  '还剩': 'left',
  '到点时间:': 'Due at: ',
  '来自': 'from',
  '取消完成': 'Mark undone',
  '完成': 'Done',
  '删': 'Del',
  '只是一条文字': 'Just a note',
  '到点只提醒这一次': 'Remind me once, then done',
  '每周这一天': 'This weekday, every week',
  '每月这一号': 'This date, every month',
  '得选个时间': 'Pick a time',
  '没记上:': 'Could not save: ',
  '关': 'off',

  /* 同步 / 设置里的动作 */
  '还没同步过': 'Never synced',
  '当前划掉了 ': 'Dismissed right now: ',
  ' 项': ' items',
  '名目': 'Name',
  '内容': 'Value',
  '删掉这一条': 'Delete this one',
  '还没有人': 'Nobody yet',
  '把这个人移出分组': 'Take this person out of the group',
  '删掉这一组': 'Delete this group',
  '正在重挑链接…': 'Re-picking links...',
  '没有需要补的 —— 带链接的信都挑过了':
    'Nothing to do - every mail with links has been through it',
  '正在补抽日程…': 'Backfilling events...',
  '没有需要补的 —— 最近这些信都抽过日程了':
    'Nothing to do - the recent mail has all been through it',
  '正在重判…': 'Re-judging...',
  '开始过目…': 'Starting triage...',
  '没有需要过目的了': 'Nothing left to triage',

  /* 更新 / 自检 */
  '安装没做完:': 'The install is incomplete: ',
  '正在补…': 'Repairing...',
  '没什么要补的': 'Nothing to repair',
  '还差:': 'Still missing: ',
  ' —— 现在是完整的了': ' - it is complete now',
  '没补成:': 'Could not repair: ',
  '已经是最新的': 'Already up to date',
  '正在更新…': 'Updating...',
  '更新没成功:': 'The update did not go through: ',
  '重启生效': 'Restart to apply',
  '代码已经是新的了,只是这个窗口还跑着旧的 —— 点一下重起一份':
    'The code is already new, this window is just still running the old one - click to restart',
  '这一版没写更新说明。': 'This release has no notes.',
  '检测到更新,更新内容如下:': 'An update is available. Here is what changed:',
  '(没写说明)': '(no notes)',
  '在 GitHub 上看这个 Release': 'See this release on GitHub',
  '正在问 GitHub…': 'Asking GitHub...',
  '查不到:': 'Could not check: ',
  '没跑起来:': 'Did not start: ',

  /* ── 后端送过来的那些词 ──

     这一批**不在 index.html 和 app.js 里**,所以照着源码找是找不到的:
     它们在 Python 那边(仪表盘的统计名、作业紧急度、邮件的级别和标签),
     随接口下来直接进 DOM。按原文索引的好处这时候显出来 —— 前后端一个字
     都不用改,进了 DOM 就翻。 */

  /* 仪表盘统计(server.py 的 dashboard) */
  '未提交待办': 'To do',
  '学分课': 'Credit courses',
  '待拿分值': 'Points at stake',

  /* 作业紧急度(server.py 的 urgency_bucket) */
  '已过期': 'Overdue',
  '今天到期': 'Due today',
  '紧急': 'Urgent',
  '临近': 'Coming up',
  '充裕': 'Plenty of time',

  /* 邮件级别(mailai.py 的 LEVELS) */
  '要紧': 'Important',
  '留意': 'Worth a look',
  '普通': 'Normal',
  '噪音': 'Noise',
  '没过目': 'Not reviewed',

  /* 邮件标签(mailai.py 的 TAG_DEFS)。**值是分类键**,不只是文案 ——
     筛选、坏标签、分组都按它走。这里只换看得见的那份,option 的 value
     和 dataset 里的原文不动(ATTRS 不含它们),所以功能不受影响。 */
  '诈骗': 'Scam',
  '求职': 'Job hunt',
  '工作': 'Work',
  '行政': 'Admin',
  '财务': 'Money',
  '住房': 'Housing',
  '出行': 'Travel',
  '健康': 'Health',
  '订阅': 'Subscriptions',
  '广告': 'Ads',
  '社交': 'Social',
  '娱乐': 'Entertainment',
  '验证码': 'Codes',
  '系统通知': 'System',
  '未分类': 'Untagged',

  /* 标签的定义。悬停在标签上看得到,**和进模型 prompt 的是同一份** ——
     那边永远是中文(模型按中文判),这边只是让人读得懂 */
  '冒充他人或机构、钓鱼链接、索要密码或转账。**必须有实据**,见下面那条':
    'Impersonating a person or institution, phishing links, asking for a '
    + 'password or a transfer. **Needs hard evidence** - see the note below',
  '课程、作业、成绩、考试、导师和助教':
    'Courses, assignments, grades, exams, advisors and TAs',
  '投递、面试、招聘、实习、offer':
    'Applications, interviews, recruiting, internships, offers',
  '在职的事务、同事、项目、报销':
    'On-the-job matters, colleagues, projects, reimbursements',
  '具体的会面、约谈、预约、要到场的活动':
    'A specific meeting, appointment, booking, or event you have to attend',
  '学校、政府、签证、保险、税务的事务性通知':
    'Paperwork from the school, government, visa, insurance or tax offices',
  '账单、缴费、续费、退款、工资、到期提醒 —— 我真在用的服务发的':
    'Bills, payments, renewals, refunds, pay, expiry notices - from services '
    + 'I actually use',
  '房东、公寓、水电、网络、维修、租约':
    'Landlord, apartment, utilities, internet, repairs, lease',
  '机票、火车、酒店、行程变更':
    'Flights, trains, hotels, itinerary changes',
  '就诊、体检、保险理赔、药房':
    'Appointments, check-ups, insurance claims, pharmacy',
  '我自己订的通讯、周报、课程推送 —— 不用回,但可能想看':
    'Newsletters and digests I signed up for - nothing to answer, but I may '
    + 'want to read them',
  '促销、推广、我没订过的营销邮件':
    'Promotions and marketing I never signed up for',
  '社交网站的互动通知(点赞、关注、私信提醒)':
    'Activity notices from social sites (likes, follows, DM alerts)',
  '游戏、影音、兴趣社群':
    'Games, media, hobby communities',
  '一次性验证码、登录码、魔术链接':
    'One-time codes, login codes, magic links',
  '机器自动发的状态、告警、构建结果、日志':
    'Machine-generated status, alerts, build results, logs',

  /* 发现新版本时那个居中对话框 */
  '有新版本': 'An update is available',
  '立刻更新': 'Update now',
  '稍后手动更新': 'Later, I will do it myself',
  '跳过这个版本': 'Skip this version',
  '(这一版没写说明)': '(no notes for this version)',
  '取最新代码、快进、自动重启。你的邮件、对话、课件、设置都不动。':
    'Fetch the latest code, fast-forward, restart automatically. Your mail, '
    + 'chats, course files and settings are untouched.',

  /* 「盯着 N 封」那个开关 */
  '· 盯着的(已清空)': '· Watching (none left)',
  '只看盯着的这几封': 'Show only the ones you are watching',
  '回到全部邮件': 'Back to all mail',
  '没有盯着的邮件。在卡片右下角点 ☆ 就能盯住一封 —— 每天的邮件简报都会提醒,直到你标完成。':
    'Nothing is being watched. Hit the star at the bottom right of a card to '
    + 'watch one - the daily mail brief will keep reminding you until you '
    + 'mark it done.',

  /* 邮件搜索(顶上那条) */
  '搜索邮件': 'Search mail',
  '搜索邮件:发件人、主题、摘要、标签':
    'Search mail: sender, subject, summary, tags',
  '清空(Esc)': 'Clear (Esc)',
  '正在准备索引…': 'Building the index...',
  '没有匹配的邮件': 'No mail matches',
  '搜索索引读不到:': 'Could not load the search index: ',

  /* 一键已读 / 每封信上的已读开关 */
  '全部标已读': 'Mark all read',
  '确定?再点一次': 'Sure? Click again',
  '正在标…': 'Marking...',
  '没有未读的了': 'Nothing unread left',
  '一键已读没成功:': 'Mark-all-read did not work: ',
  '标已读': 'Mark read',
  '打不开那封信:': 'Could not open it: ',

  /* 「上次更新没落地」那条横幅 */
  '打开安装目录': 'Open the install folder',
  '知道了': 'Got it',
  '多半是这一份在压缩包或临时目录里跑,或者那个目录写不进去':
    'Most likely this copy is running from inside the zip or a temp folder, '
    + 'or that folder is not writable',

  /* 悬浮球那条备忘录浮窗自己的标题条 */
  '拖动移动窗口': 'Drag to move the window',
  '收起': 'Close',
};
