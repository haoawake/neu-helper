/* ============================================================================
   NEU Helper — 前端逻辑

   和后端走本地 HTTP,不用 pywebview 的 js_api 桥:那个桥每次返回值都要跨线程
   marshal 回 WebView2 的 UI 线程,高频往返会把窗口卡死成「未响应」。
   聊天流是一条长驻 SSE 连接,走浏览器自己的网络栈。
   ============================================================================ */

'use strict';

const $ = (id) => document.getElementById(id);

const state = {
  data: null,
  showDone: false,
  streaming: false,
  activeAssignment: null,
  bubble: null,        // 当前正在追加增量的那个气泡
  bubbleRaw: '',       // 该气泡累积的 markdown 原文
  tab: 'brief',        // 'brief' | 'chat'
  chatId: '',          // 当前这段对话的 id(data/chats.json 里的)
  chats: [],           // 对话目录(最近更新在前)
  contextTurns: 10,    // resume 续不上时至少能带回多少轮
  courseId: 0,         // 课程单页当前是哪门课(0 = 没打开)
  course: null,        // 课程单页的数据
  courseSeg: 'hw',     // 课程单页的分段:hw / mat / ann
  sync: null,          // 课件同步的进度
  page: 'study',       // 子页面:study | mail
  mail: null,          // 邮件拉取状态
  mailMsgs: [],        // 收件箱
  mailAccount: '',     // 列表按账号过滤
  mailUnreadOnly: false,
  mailTab: 'brief',    // 邮箱右栏:brief | chat
  mailBubble: null,    // 邮件对话正在流式的那个气泡
  mailBubbleRaw: '',
  mailStreaming: false,
  mailBriefLive: '',
  mailBriefToday: '',
  mailBriefIndex: [],
  mailChatId: '',
  mailChats: [],
  mailCtx: [],         // 拖进邮件对话的邮件
  mailCtxSent: false,
  mailMinLevel: 0,     // 列表只看这个级别以上(0 = 全都看)
  mailSort: 'date_desc',
  mailDay: '',         // 只看某一天(空 = 所有日期)
  mailTag: '',         // 只看某个标签
  mailTagCounts: {},   // 当前这批里每个标签有几封
  mailDays: [],        // 有邮件的日期
  mailAi: null,        // AI 过目的进度
  mailFill: null,      // 补抓正文的进度
  memos: [],           // 备忘录
  memoPending: 0,
  memoTick: 0,         // 倒计时那个定时器
  mailBox: 'in',       // 'in' 收件箱 | 'trash' 垃圾箱(坏标签的信在这儿)
  mailPerson: '',      // 只看某个发件人分组
  mailBoxes: null,     // 两个箱子各有多少 + 各分组多少
  mailTrashTags: [],
  mailGroups: [],
  mailPage: 1,         // 列表第几页(一页 50 封)
  mailPages: 1,
  mailTotal: 0,
  mailOne: '',         // 单封视图正在看哪一封(空 = 在看列表)
  mailScroll: 0,       // 进单封之前列表滚到哪儿了,退出要还原
  mailBody: {},        // 已经取回来的全文,按 id 缓存
  mailCatalog: [],     // 标签目录
  watching: [],        // 还在盯的邮件(标了重点、没标完成)
  prefsTab: 'general', // 设置面板停在哪一栏
  lazyFlush: [],       // 设置里几个 textarea 的"立刻落盘"回调
  ctx: [],             // 拖进对话的关联对象(作业/公告/文件)
  ctxSent: false,      // 这组关联对象有没有已经随某条消息发过去了
  briefPinned: false,  // 用户自己翻过日期吗(翻过才保留他的选择)
  mailBriefPinned: false,
  briefIndex: [],      // 简报目录(日期倒序)
  briefToday: '',      // 今天的日期字符串
  briefLive: '',       // 正在流式生成的简报原文
  briefGenerating: false,
  mode: 'full',        // 'orb' | 'chat' | 'full'(orb 时这个窗口是隐藏的)
  showDismissed: true, // 划掉的条目是否还列出来(默认列,方便撤回)
  prefs: {},           // data/prefs.json 的内容(设置面板改它)
  dismissedCount: 0,
};

/* ─────────────────────────── 工具函数 ─────────────────────────── */

// 一律用 textContent 写入,不拼 HTML —— Canvas 的作业标题和公告是外部内容
function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = String(text);
  return n;
}

function statusPill(status, icon, label) {
  const pill = el('span', 'status');
  pill.dataset.status = status;
  pill.appendChild(el('span', 'status-icon', icon));
  pill.appendChild(el('span', null, label));
  return pill;
}

function fmtDays(d) {
  if (d === null || d === undefined) return '—';
  if (d < 0) return '已过';
  if (d < 1) return '今天';
  return `${Math.round(d)} 天`;
}

/* ─────────── 和后端的通道:本地 HTTP ───────────
   token 由窗口 URL 带进来,每个请求都要附上 —— 接口吐的是个人学业数据,
   不能让本机其他进程随便读 */

const KEY = new URLSearchParams(location.search).get('k') || '';

function apiUrl(path, extra) {
  const u = new URL(path, location.origin);
  u.searchParams.set('k', KEY);
  if (extra) Object.keys(extra).forEach((a) => u.searchParams.set(a, extra[a]));
  return u.toString();
}

async function apiGet(path, extra) {
  const r = await fetch(apiUrl(path, extra));
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}

async function apiPost(path, body) {
  const r = await fetch(apiUrl(path), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}

// 应用内没有地址栏和登录态,外链一律交给系统浏览器
function openExternal(url) {
  if (url) apiPost('/api/open', { url: url }).catch(() => {});
}

/* ───────────────────── 极简 markdown 渲染 ─────────────────────
   只建 DOM 节点、只用 textContent —— 模型输出同样当不可信内容处理,
   绝不走 innerHTML。支持:标题、无序/有序列表、围栏代码、粗体、斜体、
   行内代码、链接。 */

function mdInline(text, parent) {
  // 顺序有讲究:行内代码最先匹配,否则代码里的 * 会被当成强调标记
  const RE = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(\[[^\]\n]+\]\([^)\s]+\))|(\*[^*\n]+\*)/;
  let rest = text;
  let guard = 0;
  while (rest && guard++ < 2000) {
    const m = rest.match(RE);
    if (!m) { parent.appendChild(document.createTextNode(rest)); return; }
    if (m.index > 0) parent.appendChild(document.createTextNode(rest.slice(0, m.index)));
    const tok = m[0];
    if (tok.startsWith('`')) {
      parent.appendChild(el('code', 'md-code', tok.slice(1, -1)));
    } else if (tok.startsWith('**')) {
      parent.appendChild(el('strong', null, tok.slice(2, -2)));
    } else if (tok.startsWith('[')) {
      const mm = tok.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
      const a = el('a', 'md-link', mm[1]);
      const href = mm[2];
      if (/^https?:\/\//.test(href)) {
        // 应用内没有地址栏和登录态,链接一律交给系统浏览器
        a.addEventListener('click', (e) => {
          e.preventDefault();
          openExternal(href);
        });
      }
      parent.appendChild(a);
    } else {
      parent.appendChild(el('em', null, tok.slice(1, -1)));
    }
    rest = rest.slice(m.index + tok.length);
  }
}

const MD_BLOCK_START = /^(#{1,6}\s|```|\s*[-*+]\s|\s*\d+\.\s)/;

function renderMarkdown(raw, container) {
  container.textContent = '';
  const lines = raw.split('\n');
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];

    if (/^```/.test(line)) {                        // 围栏代码
      const buf = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) { buf.push(lines[i]); i++; }
      i++;
      const pre = el('pre', 'md-pre');
      pre.appendChild(el('code', null, buf.join('\n')));
      container.appendChild(pre);
      continue;
    }

    const head = line.match(/^#{1,6}\s+(.*)$/);     // 标题
    if (head) {
      const n = el('div', 'md-h');
      mdInline(head[1], n);
      container.appendChild(n);
      i++;
      continue;
    }

    if (/^\s*[-*+]\s+/.test(line)) {                // 无序列表
      const ul = el('ul', 'md-ul');
      while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
        const li = el('li');
        mdInline(lines[i].replace(/^\s*[-*+]\s+/, ''), li);
        ul.appendChild(li);
        i++;
      }
      container.appendChild(ul);
      continue;
    }

    if (/^\s*\d+\.\s+/.test(line)) {               // 有序列表
      const ol = el('ol', 'md-ul');
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        const li = el('li');
        mdInline(lines[i].replace(/^\s*\d+\.\s+/, ''), li);
        ol.appendChild(li);
        i++;
      }
      container.appendChild(ol);
      continue;
    }

    if (!line.trim()) { i++; continue; }

    const buf = [];                                  // 段落
    while (i < lines.length && lines[i].trim() && !MD_BLOCK_START.test(lines[i])) {
      buf.push(lines[i]);
      i++;
    }
    const p = el('div', 'md-p');
    mdInline(buf.join('\n'), p);
    container.appendChild(p);
  }
}

/* ─────────────────────────── 仪表盘渲染 ─────────────────────────── */

async function loadDashboard(force = false) {
  const btn = $('btnRefresh');
  btn.classList.add('spinning');
  try {
    const d = await apiGet('/api/dashboard', { force: force ? '1' : '0' });
    state.data = d;
    render(d);
  } catch (err) {
    showBanner(`加载失败:${err}`);
  } finally {
    btn.classList.remove('spinning');
  }
}

function showBanner(msg, strong) {
  const b = $('banner');
  b.textContent = '';
  if (strong) b.appendChild(el('strong', null, strong + ' '));
  b.appendChild(document.createTextNode(msg));
  b.hidden = false;
}

function render(d) {
  if (d.error) {
    $('hero').hidden = true;
    showBanner(
      d.error === 'config'
        ? '找不到或读不到 Canvas token,跑一次 install.ps1 重新写入。'
        : d.message,
      d.error === 'config' ? '配置问题' : 'Canvas 连不上'
    );
    $('appbarMeta').textContent = '';
    return;
  }
  $('banner').hidden = true;

  $('appbarMeta').textContent = `${d.profile.name} · ${d.today}`;
  const n = d.dismissed_count || 0;
  state.dismissedCount = n;
  $('paneFoot').textContent =
    `数据抓取于 ${d.fetched_at}` + (n ? ` · 已划掉 ${n} 项(不计入简报)` : '');

  renderOverdue(d.overdue);
  renderHero(d.hero);
  renderStats(d.stats);
  renderTodo(d.todo);
  renderGrades(d.courses);
  renderAnnouncements(d.announcements);
  renderCourseChips(d.courses, d.todo);
  // 悬浮球上的角标由后端画(球是原生窗口),/api/dashboard 里顺手就更新了
}

function renderOverdue(list) {
  const box = $('overdueAlert');
  box.textContent = '';
  if (!list || !list.length) { box.hidden = true; return; }
  box.hidden = false;
  box.appendChild(el('span', 'alert-icon', '■'));
  const tx = el('div', 'alert-text');
  tx.appendChild(el('b', null, `${list.length} 项已过期且未提交`));
  const ul = el('ul', 'alert-list');
  list.forEach((t) =>
    ul.appendChild(el('li', null, `${t.course_short} · ${t.title.trim()} · ${t.due_short}`))
  );
  tx.appendChild(ul);
  box.appendChild(tx);
}

function renderHero(h) {
  const hero = $('hero');
  if (!h) { hero.hidden = true; return; }
  hero.hidden = false;
  if (h.days < 1) {
    $('heroDays').textContent = String(Math.max(0, h.hours));
    $('heroUnit').textContent = '小时';
  } else {
    $('heroDays').textContent = String(h.days);
    $('heroUnit').textContent = '天';
  }
  $('heroTitle').textContent = `${h.course} · ${h.title}`;
  $('heroDue').textContent = `截止 ${h.due}`;

  const wrap = $('heroStatus');
  wrap.textContent = '';
  wrap.appendChild(statusPill(h.status, h.status_icon, h.status_label));
}

function renderStats(stats) {
  const row = $('statRow');
  row.textContent = '';
  stats.forEach((s) => {
    const card = el('div', 'stat');
    card.appendChild(el('div', 'stat-value', s.value));
    card.appendChild(el('div', 'stat-label', s.label));
    row.appendChild(card);
  });
}

function renderTodo(todo) {
  const list = $('todoList');
  list.textContent = '';

  let visible = state.showDone ? todo : todo.filter((t) => !t.submitted);
  if (!state.showDismissed) visible = visible.filter((t) => !t.dismissed);
  $('btnToggleDone').textContent = state.showDone ? '只看未提交' : '显示已提交';

  if (!visible.length) {
    list.appendChild(el('div', 'empty', '没有待办 —— 未来四周是干净的。'));
    return;
  }

  visible.forEach((t, i) => {
    const card = el(
      'div',
      'card' + (t.submitted ? ' is-done' : '') + (t.dismissed ? ' is-dismissed' : '')
    );
    // 逐个错开 24ms 入场,列表看起来是"流"进来的而不是一次性闪现
    card.style.animationDelay = `${Math.min(i, 8) * 24}ms`;

    // 拖到右栏就关联到对话。划掉的条目也能拖 —— 特意拿出来问一句很正常
    card.draggable = true;
    card.addEventListener('dragstart', (e) => {
      startCtxDrag(e, {
        kind: 'assignment',
        title: t.title.trim(),
        course: t.course_short,
        due: t.due_local || t.due_short,
        points: t.points,
        submitted: t.submitted,
        dismissed: !!t.dismissed,
        course_id: t.course_id,
        assignment_id: t.plannable_id,
        url: t.url,
      });
    });

    const top = el('div', 'card-top');

    // 划掉按钮:点它只切换忽略状态,不打开详情(下面 stopPropagation)
    const dismiss = el('button', 'dismiss-btn', '✓');
    dismiss.type = 'button';
    dismiss.title = t.dismissed ? '撤回:重新纳入简报' : '划掉:以后的简报不再提这项';
    dismiss.setAttribute('aria-pressed', String(!!t.dismissed));
    dismiss.addEventListener('click', (e) => {
      e.stopPropagation();
      toggleDismiss(t, card);
    });
    top.appendChild(dismiss);

    top.appendChild(statusPill(t.status, t.status_icon, t.status_label));
    top.appendChild(el('div', 'card-title', t.title.trim()));
    top.appendChild(el('div', 'card-spacer'));
    top.appendChild(el('div', 'card-left', fmtDays(t.days_left)));
    card.appendChild(top);

    const meta = el('div', 'card-meta');
    meta.appendChild(el('span', 'chip', t.course_short));
    if (t.points !== null && t.points !== undefined) {
      meta.appendChild(el('span', 'dot-sep', '·'));
      meta.appendChild(el('span', null, `${t.points} 分`));
    }
    meta.appendChild(el('span', 'dot-sep', '·'));
    meta.appendChild(el('span', null, t.due_short));
    if (t.submitted) {
      meta.appendChild(el('span', 'dot-sep', '·'));
      meta.appendChild(el('span', null, '已提交'));
    }
    card.appendChild(meta);

    card.addEventListener('click', () => openAssignment(t));
    list.appendChild(card);
  });
}

/* 划掉 / 撤回一条 DDL。
   先就地改样式(删除线动画立刻跑起来),再等后端确认后整体重载 ——
   否则要等一整个网络往返才有反馈,手感很钝。 */
async function toggleDismiss(item, card) {
  const next = !item.dismissed;
  item.dismissed = next;
  card.classList.toggle('is-dismissed', next);
  const btn = card.querySelector('.dismiss-btn');
  if (btn) {
    btn.setAttribute('aria-pressed', String(next));
    btn.title = next ? '撤回:重新纳入简报' : '划掉:以后的简报不再提这项';
  }
  try {
    await apiPost('/api/dismiss', { key: item.key, on: next, title: item.title });
  } catch (e) {
    // 后端没写成功就把样式退回去,别让界面撒谎
    item.dismissed = !next;
    card.classList.toggle('is-dismissed', !next);
    reportError('dismiss', (e && e.message) || String(e), '', 0, e && e.stack);
    return;
  }
  // 统计、hero、逾期告警都受影响,重算一遍
  await loadDashboard(true);
}

function renderGrades(courses) {
  const table = $('gradeTable');
  table.textContent = '';

  courses.forEach((c) => {
    const tr = el('tr');
    tr.appendChild(Object.assign(el('td', 'g-name', c.short), { title: c.name }));

    const meterCell = el('td', 'g-meter');
    if (c.has_score) {
      const track = el('div', 'meter');
      const fill = el('div', 'meter-fill');
      fill.style.width = `${Math.max(0, Math.min(100, c.current_score))}%`;
      track.appendChild(fill);
      meterCell.appendChild(track);
    }
    tr.appendChild(meterCell);

    const val = el(
      'td',
      'g-value' + (c.has_score ? '' : ' is-empty'),
      c.has_score ? `${c.current_score.toFixed(1)}%` : '暂无评分'
    );
    tr.appendChild(val);
    table.appendChild(tr);
  });
}

function renderAnnouncements(anns) {
  const list = $('annList');
  list.textContent = '';
  if (!anns || !anns.length) {
    list.appendChild(el('div', 'empty', '最近十天没有公告。'));
    return;
  }
  anns.forEach((a) => {
    const card = el('div', 'ann');
    const top = el('div', 'ann-top');
    top.appendChild(el('span', 'chip', a.course));
    top.appendChild(el('span', 'ann-title', a.title));
    top.appendChild(el('span', 'muted', a.days_ago !== null ? `${a.days_ago} 天前` : ''));
    card.appendChild(top);
    card.appendChild(el('div', 'ann-excerpt', a.excerpt));
    card.draggable = true;
    card.addEventListener('dragstart', (e) => {
      startCtxDrag(e, {
        kind: 'announcement',
        title: a.title,
        course: a.course,
        excerpt: a.excerpt,
        url: a.url,
      });
    });
    card.addEventListener('click', () => {
      openExternal(a.url);
    });
    list.appendChild(card);
  });
}

/* ─────────────────────────── 作业详情 ─────────────────────────── */

async function openAssignment(item) {
  if (item.type !== 'assignment' || !item.course_id) {
    openExternal(item.url);
    return;
  }
  // planner 返回的 url 形如 /courses/<cid>/assignments/<aid>
  const m = (item.url || '').match(/assignments\/(\d+)/);
  if (!m) {
    openExternal(item.url);
    return;
  }

  $('sheetTitle').textContent = item.title.trim();
  $('sheetMeta').textContent = '';
  $('sheetBody').textContent = '读取中…';
  $('sheetBackdrop').hidden = false;

  const d = await apiGet('/api/assignment/' + item.course_id + '/' + Number(m[1]));
  if (d.error) { $('sheetBody').textContent = `读取失败:${d.error}`; return; }

  state.activeAssignment = { ...d, course_short: item.course_short };

  const meta = $('sheetMeta');
  meta.textContent = '';
  [
    item.course_short,
    `${d.points} 分`,
    `截止 ${d.due}`,
    d.state === 'unsubmitted' ? '未提交' : d.state,
  ].forEach((t) => meta.appendChild(el('span', 'chip', t)));

  $('sheetBody').textContent = d.description || '(这个作业没写描述,要求可能在附件或课件里)';
}

function closeSheet() {
  $('sheetBackdrop').hidden = true;
  state.activeAssignment = null;
}

/* ─────────────────────────── 对话 ─────────────────────────── */

function addMsg(role, text, ctx) {
  return addMsgIn($('chatLog'), role, text, ctx);
}

/* 往指定的对话框里加一条消息。课业和邮箱两个页面共用。 */
function addMsgIn(log, role, text, ctx) {
  const msg = el('div', `msg msg-${role}`);
  msg.appendChild(el('div', 'msg-role', role === 'user' ? '我' : role === 'error' ? '出错' : 'Claude'));
  const body = el('div', 'msg-body', text || '');
  msg.appendChild(body);
  // 这一轮关联了什么,在气泡下面留一行 —— 回看历史时才知道当时在问哪个东西
  if (ctx && ctx.length) {
    const line = el('div', 'msg-ctx');
    line.appendChild(el('span', null, '关于 '));
    ctx.forEach((c, i) => {
      if (i) line.appendChild(el('span', null, '、'));
      line.appendChild(el('span', 'msg-ctx-item',
        (c.course ? c.course + ' ' : '') + (c.title || '')));
    });
    msg.appendChild(line);
  }
  log.appendChild(msg);
  log.scrollTop = log.scrollHeight;
  return body;
}

/* ── 增量渲染的合并 ──
   一次 rAF 最多重建一遍,两条通道各自排队。不合并的话增量一来就重建一次,
   肉眼看到的就是界面每隔一下"刷新"一下(还带着重放的入场动画)。 */
const pending = { chat: false, brief: false, mailchat: false, mailbrief: false };

function scheduleRender(which) {
  if (pending[which]) return;
  pending[which] = true;
  requestAnimationFrame(() => {
    pending[which] = false;
    try {
      if (which === 'chat') flushChatBubble();
      else if (which === 'brief') flushBriefBody();
      else if (which === 'mailchat') flushMailChat();
      else flushMailBrief();
    } catch (e) {
      reportError('flush-' + which, (e && e.message) || String(e), '', 0, e && e.stack);
    }
  });
}

function flushChatBubble() {
  if (!state.bubble) return;
  renderMarkdown(state.bubbleRaw, state.bubble);
  $('chatLog').scrollTop = $('chatLog').scrollHeight;
}

function flushBriefBody() {
  const box = $('briefBody');
  box.textContent = '';
  box.classList.add('streaming');
  box.appendChild(el('div', 'brief-meta', `${state.briefToday} · 正在生成…`));
  const body = el('div');
  renderMarkdown(state.briefLive, body);
  box.appendChild(body);
  box.scrollTop = box.scrollHeight;
}

function setChatStatus(text) {
  const bar = $('chatStatus');
  if (!text) { bar.hidden = true; return; }
  bar.textContent = '';
  bar.appendChild(el('span', 'pulse'));
  bar.appendChild(el('span', null, text));
  bar.hidden = false;
}

function setStreaming(on) {
  state.streaming = on;
  $('btnSend').disabled = on;
  if (!on) {
    setChatStatus(null);
    if (state.bubble) {
      // 写完了:补最后一次渲染,再把光标和 streaming 摘掉
      renderMarkdown(state.bubbleRaw, state.bubble);
      state.bubble.classList.remove('caret', 'streaming');
    }
    state.bubble = null;
  }
}

// 轮询取回的事件逐条走这里
function handleChatEvent(ev) {
  switch (ev.kind) {
    case 'start':
      setStreaming(true);
      setChatStatus('思考中…');
      break;
    case 'thinking':
      setChatStatus('思考中…');
      break;
    case 'tool':
      setChatStatus(ev.text + '…');
      break;
    case 'delta':
      if (!state.bubble) {
        state.bubble = addMsg('assistant', '');
        // streaming:重建出来的子元素不要重放入场动画
        state.bubble.classList.add('caret', 'streaming');
        state.bubbleRaw = '';
      }
      setChatStatus(null);
      state.bubbleRaw += ev.text;
      scheduleRender('chat');
      break;
    case 'error':
      addMsg('error', ev.text);
      break;
    case 'cost':
      // 订阅席位下这不是扣费,是按 API 标价的折算估值;标题里写清楚,免得吓人
      if (ev.session_cost) {
        const m = $('sideMeta');
        m.textContent = `本会话约 $${ev.session_cost.toFixed(3)}`;
        m.title = '按 API 标价折算的估值。订阅席位不按此扣费,但会消耗用量额度。';
      }
      break;
    case 'done':
      setStreaming(false);
      // 标题和轮数都变了,而且这轮回答刚落盘,顺手刷一下目录
      loadChatIndex();
      break;
  }
}

/* 一条长驻 SSE 连接,应用生命周期内只开一次。
   断了 EventSource 会自己重连,不用我们管。 */
let stream = null;

function connectStream() {
  if (stream) return;
  stream = new EventSource(apiUrl('/api/chat/stream'));
  stream.onmessage = (e) => {
    try {
      const ev = JSON.parse(e.data);
      // 三条通道共用这一条连接,按 channel 分流
      if (ev.channel === 'briefing') handleBriefEvent(ev);
      else if (ev.channel === 'window') handleWindowEvent(ev);
      else if (ev.channel === 'mailchat') handleMailChatEvent(ev);
      else if (ev.channel === 'mailbrief') handleMailBriefEvent(ev);
      else handleChatEvent(ev);
    } catch (err) { /* 半条消息,下一帧会补齐 */ }
  };
}

async function sendChat(text, opts) {
  text = (text || '').trim();
  if (!text || state.streaming) return;
  // 关联对象只在"这组对象变化后的第一条消息"附过去:后续追问靠会话上下文
  // 接着就行,每轮都重发是白烧额度
  const ctx = state.ctxSent ? [] : state.ctx.slice();
  // 开机简报是自动发的,显示成用户说的话会很怪
  if (!(opts && opts.silent)) addMsg('user', text, state.ctx.slice());
  $('chatInput').value = '';
  autoGrow();
  setStreaming(true);          // 先锁住输入,别等后端第一个事件
  setChatStatus('连接中…');
  let r;
  try {
    r = await apiPost('/api/chat', { message: text, context: ctx });
    if (ctx.length) state.ctxSent = true;
  } catch (e) {
    addMsg('error', '后端没响应:' + e.message);
    setStreaming(false);
    return;
  }
  if (!r.ok) { addMsg('error', r.message); setStreaming(false); }
}

function autoGrow() {
  autoGrowEl($('chatInput'));
}

/* 输入框跟着内容长高。两个页面的输入框共用。 */
function autoGrowEl(ta) {
  ta.style.height = 'auto';
  ta.style.height = Math.min(ta.scrollHeight, 140) + 'px';
}

function renderSuggests() {
  const log = $('chatLog');
  const wrap = el('div', 'msg');
  wrap.appendChild(el('div', 'msg-role', '试试问'));
  const row = el('div', 'suggests');
  [
    '这周我该先做哪个',
    '最近的作业具体要求是什么',
    '我有没有漏掉的公告',
    '各科成绩现在怎么样',
  ].forEach((q) => {
    const b = el('button', 'suggest', q);
    b.type = 'button';
    b.addEventListener('click', () => sendChat(q));
    row.appendChild(b);
  });
  wrap.appendChild(row);
  log.appendChild(wrap);
}

/* ─────────────────────────── 课程单页 ───────────────────────────

   点仪表盘上的课程卡片进来。一个请求(/api/course/<id>)拿全:
   作业(带要求摘要和附件)、老师排的课件模块、这门课的公告。

   文件的本地副本由后台同步器负责(filesync.py):启动后同步一次、之后每小时
   一次,增量的。所以这里只显示"有没有本地副本",不在这儿现下。 */

/* 目录名要和后端算出来的一致(filesync.safe / server._safe_name):
   Windows 不让用的字符换成下划线,首尾的空格和点去掉。
   不一致的话「打开文件夹」会指到一个不存在的路径。 */
function safeSeg(name) {
  return String(name || '')
    .replace(/[<>:"/\\|?*\u0000-\u001f]/g, '_')
    .replace(/^[\s.]+|[\s.]+$/g, '')
    .slice(0, 80) || '未命名';
}

function fmtSize(n) {
  if (!n && n !== 0) return '';
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + ' KB';
  return (n / 1024 / 1024).toFixed(1) + ' MB';
}

function renderCourseChips(courses, todo) {
  const box = $('courseChips');
  box.textContent = '';
  // 有未提交作业的课排前面 —— 那才是现在要点进去的
  const pending = {};
  (todo || []).forEach((t) => {
    if (!t.submitted && !t.dismissed && t.course_id) {
      pending[t.course_id] = (pending[t.course_id] || 0) + 1;
    }
  });
  const sorted = (courses || []).slice().sort(
    (a, b) => (pending[b.id] || 0) - (pending[a.id] || 0));
  sorted.forEach((c) => {
    const chip = el('button', 'course-chip');
    chip.type = 'button';
    chip.title = c.name || '';
    chip.appendChild(el('span', 'course-chip-name', c.short));
    const n = pending[c.id] || 0;
    if (n) chip.appendChild(el('span', 'course-chip-n', String(n)));
    chip.addEventListener('click', () => openCourse(c.id));
    box.appendChild(chip);
  });
}

async function openCourse(id) {
  state.courseId = id;
  $('dashView').hidden = true;
  $('courseView').hidden = false;
  $('courseName').textContent = '读取中…';
  $('courseSub').textContent = '';
  $('courseBody').textContent = '';
  let d;
  try {
    d = await apiGet(`/api/course/${id}`);
  } catch (e) {
    $('courseName').textContent = '读取失败';
    $('courseSub').textContent = e.message;
    return;
  }
  if (d.error) {
    $('courseName').textContent = '读不到这门课';
    $('courseSub').textContent = d.message || '';
    return;
  }
  state.course = d;
  $('courseName').textContent = `${d.short} · ${d.name}`;
  const undone = (d.assignments || []).filter((a) => !a.submitted).length;
  const bits = [
    `${(d.assignments || []).length} 个作业`,
    undone ? `${undone} 个未提交` : '全部已提交',
    `${d.file_count || 0} 个文件`,
  ];
  if (d.score && d.score.has) bits.push(`当前 ${d.score.current.toFixed(1)}%`);
  $('courseSub').textContent = bits.join(' · ');
  $('courseFoot').textContent = `抓取于 ${d.fetched_at} · 课件在 data/downloads/${d.short}/`;
  renderCourseSeg(state.courseSeg || 'hw');
}

function closeCourse() {
  $('courseView').hidden = true;
  $('dashView').hidden = false;
  state.courseId = 0;
}

function renderCourseSeg(which) {
  state.courseSeg = which;
  document.querySelectorAll('#courseSeg .seg-btn').forEach((b) => {
    b.classList.toggle('is-on', b.dataset.v === which);
  });
  const d = state.course;
  const box = $('courseBody');
  box.textContent = '';
  if (!d) return;
  if (which === 'hw') renderCourseHomework(box, d);
  else if (which === 'mat') renderCourseMaterial(box, d);
  else renderCourseAnns(box, d);
}

function localChip(f, courseShort, title) {
  /* 一个文件行:有本地副本就能直接打开,没有就说"等同步"。
     整行可以拖到右边提问 —— 拖的是本地路径,Claude 用 Read 就能看内容。 */
  const row = el('div', 'file-row' + (f.local ? ' is-local' : ''));
  row.draggable = true;
  row.addEventListener('dragstart', (e) => {
    startCtxDrag(e, {
      kind: 'file',
      title: f.name,
      course: courseShort,
      course_id: state.courseId,
      file_id: f.id,
      path: f.local || null,
      size: f.size,
      note: title || '',
    });
  });
  // 三种状态要分清:已同步 / 太大所以默认没下 / 还没轮到
  const limit = (state.course && state.course.max_sync_bytes) || 0;
  const tooBig = !f.local && limit && (f.size || 0) > limit;
  if (tooBig) row.classList.add('is-big');
  const main = el('div', 'file-main');
  main.appendChild(el('div', 'file-name', f.name));
  main.appendChild(el('div', 'file-meta muted', [
    fmtSize(f.size),
    f.local ? '已同步到本地'
      : tooBig ? `超过 ${Math.round(limit / 1024 / 1024)} MB,默认不同步` : '还没同步',
  ].filter(Boolean).join(' · ')));
  row.appendChild(main);

  const act = el('div', 'file-act');
  if (f.local) {
    const open = el('button', 'link-btn', '打开');
    open.type = 'button';
    open.addEventListener('click', () => openLocal(f.local));
    act.appendChild(open);
    const dir = el('button', 'link-btn', '文件夹');
    dir.type = 'button';
    dir.title = '在资源管理器里打开所在目录';
    dir.addEventListener('click', () => revealLocal(f.local));
    act.appendChild(dir);
  } else {
    const dl = el('button', 'link-btn', tooBig ? '仍然下载' : '现在下载');
    dl.type = 'button';
    dl.addEventListener('click', async () => {
      dl.textContent = '下载中…';
      dl.disabled = true;
      let r = null;
      try {
        r = await apiPost('/api/files/download',
          { course_id: state.courseId, file_id: f.id });
      } catch (e) {
        r = { ok: false, error: e.message };
      }
      if (r && r.ok) {
        openCourse(state.courseId);
      } else {
        dl.textContent = '重试';
        dl.disabled = false;
        showBanner((r && r.error) || '下载失败');
      }
    });
    act.appendChild(dl);
  }
  row.appendChild(act);
  return row;
}

function renderCourseHomework(box, d) {
  const list = d.assignments || [];
  if (!list.length) {
    box.appendChild(el('div', 'empty', '这门课没有列出作业。'));
    return;
  }
  list.forEach((a, i) => {
    const card = el('div', 'card hw-card' + (a.submitted ? ' is-done' : ''));
    card.style.animationDelay = `${Math.min(i, 8) * 20}ms`;
    card.draggable = true;
    card.addEventListener('dragstart', (e) => {
      startCtxDrag(e, {
        kind: 'assignment',
        title: a.name,
        course: d.short,
        due: a.due_local,
        points: a.points,
        submitted: a.submitted,
        course_id: d.id,
        assignment_id: a.id,
        url: a.url,
      });
    });

    const top = el('div', 'card-top');
    top.appendChild(statusPill(
      a.submitted ? 'good' : (a.days_left !== null && a.days_left < 3 ? 'serious' : 'none'),
      a.submitted ? '●' : '○',
      a.submitted ? '已提交' : '未提交'));
    top.appendChild(el('div', 'card-title', a.name));
    top.appendChild(el('div', 'card-spacer'));
    top.appendChild(el('div', 'card-left', a.days_left === null ? '无期限' : fmtDays(a.days_left)));
    card.appendChild(top);

    const meta = el('div', 'card-meta');
    [a.due_local || '无期限',
     a.points !== null && a.points !== undefined ? `${a.points} 分` : '',
     a.score !== null && a.score !== undefined ? `得分 ${a.score}` : '',
     (a.submission_types || []).join('/'),
    ].filter(Boolean).forEach((t, j) => {
      if (j) meta.appendChild(el('span', 'dot', '·'));
      meta.appendChild(el('span', null, t));
    });
    card.appendChild(meta);

    if (a.brief) {
      const req = el('div', 'hw-brief');
      renderMarkdown(a.brief, req);
      card.appendChild(req);
    }
    if (a.attachments && a.attachments.length) {
      const wrap = el('div', 'hw-atts');
      wrap.appendChild(el('div', 'hw-atts-label muted', '附件'));
      a.attachments.forEach((f) => wrap.appendChild(localChip(f, d.short, a.name)));
      card.appendChild(wrap);
    }

    const foot = el('div', 'hw-foot');
    const ask = el('button', 'link-btn', '问 Claude');
    ask.type = 'button';
    ask.addEventListener('click', () => {
      addCtx({
        kind: 'assignment', title: a.name, course: d.short, due: a.due_local,
        points: a.points, submitted: a.submitted, course_id: d.id,
        assignment_id: a.id, url: a.url,
      });
    });
    foot.appendChild(ask);
    if (d.local_dir) {
      const dir = el('button', 'link-btn', '作业文件夹');
      dir.type = 'button';
      dir.title = '打开这个作业在本地的目录';
      dir.addEventListener('click',
        () => revealLocal(`${d.local_dir}\\作业\\${safeSeg(a.name)}`));
      foot.appendChild(dir);
    }
    const open = el('button', 'link-btn', '在 Canvas 打开');
    open.type = 'button';
    open.addEventListener('click', () => openExternal(a.url));
    foot.appendChild(open);
    card.appendChild(foot);

    box.appendChild(card);
  });
}

function renderCourseMaterial(box, d) {
  const mods = d.modules || [];
  const loose = d.files || [];
  if (!mods.length && !loose.length) {
    box.appendChild(el('div', 'empty',
      '这门课没有课件,或者老师没开放文件区 / 模块。'));
    return;
  }
  mods.forEach((m) => {
    const sec = el('section', 'mod');
    const head = el('div', 'mod-head');
    head.appendChild(el('h3', 'mod-name', m.name));
    head.appendChild(el('span', 'muted', `${(m.items || []).length} 项`));
    if (d.local_dir) {
      head.appendChild(el('span', 'mod-spacer'));
      const dir = el('button', 'link-btn', '文件夹');
      dir.type = 'button';
      dir.title = '打开这个模块在本地的目录';
      dir.addEventListener('click',
        () => revealLocal(`${d.local_dir}\\课件\\${safeSeg(m.name)}`));
      head.appendChild(dir);
    }
    sec.appendChild(head);
    (m.items || []).forEach((it) => {
      if (it.file) {
        sec.appendChild(localChip(it.file, d.short, m.name));
        return;
      }
      const row = el('div', 'mod-item');
      row.appendChild(el('span', 'mod-type', it.type || ''));
      row.appendChild(el('span', 'mod-title', it.title || ''));
      if (it.url) {
        row.classList.add('is-link');
        row.addEventListener('click', () => openExternal(it.url));
      }
      sec.appendChild(row);
    });
    box.appendChild(sec);
  });
  if (loose.length) {
    const sec = el('section', 'mod');
    const head = el('div', 'mod-head');
    head.appendChild(el('h3', 'mod-name', '其他文件'));
    head.appendChild(el('span', 'muted', `${loose.length} 个`));
    sec.appendChild(head);
    loose.forEach((f) => sec.appendChild(localChip(f, d.short, '其他')));
    box.appendChild(sec);
  }
}

function renderCourseAnns(box, d) {
  const anns = d.announcements || [];
  if (!anns.length) {
    box.appendChild(el('div', 'empty', '最近三周没有公告。'));
    return;
  }
  anns.forEach((a) => {
    const card = el('div', 'ann');
    card.draggable = true;
    card.addEventListener('dragstart', (e) => {
      startCtxDrag(e, {
        kind: 'announcement', title: a.title, course: d.short,
        excerpt: a.excerpt, url: a.url,
      });
    });
    const top = el('div', 'ann-top');
    top.appendChild(el('span', 'ann-title', a.title));
    top.appendChild(el('span', 'muted', a.posted || ''));
    card.appendChild(top);
    card.appendChild(el('div', 'ann-excerpt', a.excerpt));
    card.addEventListener('click', () => openExternal(a.url));
    box.appendChild(card);
  });
}

function openLocal(path) {
  apiPost('/api/files/open', { path: path }).then((r) => {
    if (r && !r.ok) showBanner(r.error || '打不开');
  }).catch(() => {});
}

/* 在资源管理器里打开所在目录(文件会被选中)。目录还没建出来的话,
   后端会退到最近一个存在的父目录 —— 比弹"路径不存在"有用。 */
function revealLocal(path) {
  apiPost('/api/files/reveal', { path: path }).then((r) => {
    if (r && !r.ok) showBanner(r.error || '打不开那个位置');
  }).catch(() => {});
}

/* 同步进度:后台同步器每动一下都会顺着 SSE 推一条过来 */
function renderSyncState(st) {
  state.sync = st;
  if (!$('prefsBackdrop').hidden) syncPrefsUI();
  const btn = $('btnCourseSync');
  if (!btn) return;
  if (st.running) {
    btn.textContent = st.total
      ? `同步中 ${st.done}/${st.total}` : '整理中…';
    btn.disabled = true;
  } else {
    btn.textContent = '同步课件';
    btn.disabled = false;
    if (st.last && state.syncSeen !== st.last) {
      state.syncSeen = st.last;
      // 同步完了刷新一下当前课程页,"还没同步"要变成"打开"
      if (state.courseId) openCourse(state.courseId);
    }
  }
}

function wireCourseView() {
  $('btnCourseBack').addEventListener('click', closeCourse);
  document.querySelectorAll('#courseSeg .seg-btn').forEach((b) => {
    b.addEventListener('click', () => renderCourseSeg(b.dataset.v));
  });
  $('btnCourseFolder').addEventListener('click', () => {
    const d = state.course;
    if (d && d.local_dir) revealLocal(d.local_dir);
  });
  $('btnCourseOpen').addEventListener('click', () => {
    const d = state.course;
    if (d && d.url) openExternal(d.url);
  });
  $('btnCourseSync').addEventListener('click', async () => {
    try {
      const r = await apiPost('/api/sync', {});
      if (r && r.state) renderSyncState(r.state);
    } catch (e) {
      reportError('sync', (e && e.message) || String(e), '', 0, e && e.stack);
    }
  });
}

/* ═════════════════════════ 邮箱子页面 ═════════════════════════

   左边收件箱(3 分钟自动收一次),右边邮件简报 / 邮件对话。
   **和课业那套完全分开**:存档、CLI 通道、历史记录都是独立的两份,
   所以课业简报在生成的时候你还能问邮件的事,互不打断。 */

function setPage(page) {
  state.page = page === 'mail' ? 'mail' : 'study';
  document.body.dataset.page = state.page;
  document.querySelectorAll('#pageSeg .seg-btn').forEach((b) => {
    b.classList.toggle('is-on', b.dataset.v === state.page);
  });
  savePrefs({ page: state.page });
  // 换了子页面 = 换了界面:简报那一栏回到最新那一份。
  // 光解钉不够 —— 还得重拉一次,选中项才会被重算
  state.briefPinned = false;
  state.mailBriefPinned = false;
  if (state.page === 'study' && state.tab === 'brief') loadBriefIndex();
  if (state.page === 'mail') {
    loadMail();
    // 无条件重拉:已经有存档目录的时候也要重算选中项,不然还停在上次翻到那天
    if (state.mailTab === 'brief' || !state.mailBriefIndex.length) loadMailBriefIndex();
    if (!state.mailChatId) loadMailChatIndex().then(() => {
      if (state.mailChatId) openMailChat(state.mailChatId);
    });
  }
}

/* ── 收件箱 ── */

async function loadMail() {
  let d;
  try {
    // 排序和筛选一律后端做:前端只拿 120 条,「按重要程度排」要是只排这 120 条,
    // 第 121 条那封要紧的就永远排不进来
    d = await apiGet('/api/mail', {
      page: String(state.mailPage || 1),
      account: state.mailAccount || '',
      unread: state.mailUnreadOnly ? '1' : '0',
      sort: state.mailSort || 'date_desc',
      box: state.mailBox || 'in',
      person: state.mailPerson || '',
      day: state.mailDay || '',
      tag: state.mailTag || '',
      level: String(state.mailMinLevel || 0),
    });
  } catch (e) {
    showMailBanner('读不到邮件:' + e.message);
    return;
  }
  state.mailMsgs = d.messages || [];
  state.mailTotal = d.total || 0;
  state.mailPages = d.pages || 1;
  state.mailPage = d.page || 1;
  state.mailTagCounts = d.tag_counts || {};
  state.mailDays = d.days || [];
  state.mailCatalog = d.tags || [];
  state.mailAi = d.ai || null;
  state.mailFill = d.fill || null;
  state.mailBoxes = d.boxes || null;
  state.mailTrashTags = d.trash_tags || [];
  state.mailGroups = d.groups || [];
  renderBoxBtn();
  renderPersonSel();
  renderMailDays();
  renderMailTagBar();
  renderAiBar();
  renderMailAccounts(d.accounts || []);
  // watching 是顶层字段,顺手并进 state 里 —— 顶栏和设置的角标都读它
  renderMailState(Object.assign({}, d.state || {}, { watching: d.watching || 0 }));
  renderMailList();
}

function showMailBanner(msg) {
  const b = $('mailBanner');
  b.textContent = msg;
  b.hidden = !msg;
}

function renderMailAccounts(accs) {
  const sel = $('mailAccount');
  const cur = sel.value;
  sel.textContent = '';
  const all = el('option', null, '全部账号');
  all.value = '';
  sel.appendChild(all);
  accs.forEach((a) => {
    const o = el('option', null, (a.label || a.email) + (a.ready ? '' : '(未就绪)'));
    o.value = a.id;
    o.title = a.last_error || a.email || '';
    sel.appendChild(o);
  });
  sel.value = state.mailAccount || cur || '';
  // 只有一个账号的时候这个下拉没有意义,藏掉给顶栏让位(顶栏已经很挤了)
  sel.hidden = accs.length < 2;
  // 账号的报错要看得见 —— 授权码过期这种事只会在这里露头
  const bad = accs.filter((a) => a.last_error);
  showMailBanner(bad.length ? `${bad[0].label}:${bad[0].last_error}` : '');
  if (!accs.length) {
    showMailBanner('还没有邮箱账号 —— 打开设置 → 邮箱,加一个。');
  }
}

function renderMailState(st) {
  state.mail = st;
  const n = st.unread || 0;
  const badge = $('mailBadge');
  if (n > 0) {
    badge.textContent = String(n);
    badge.hidden = false;
  } else {
    badge.hidden = true;
  }
  // 有筛选的时候要说清"这是筛过的" —— 光写「9 封 · 33 未读」会让人以为丢了信
  const filtered = !!(state.mailTag || state.mailDay || state.mailMinLevel
    || state.mailUnreadOnly);
  const shown = state.mailTotal || state.mailMsgs.length;
  $('mailCount').textContent = st.running ? '收取中…'
    : `${shown} 封`
      + (filtered ? '(筛选后)' : '')
      + (n ? ` · ${n} 未读` : '');
  $('mailMeta').textContent = st.updated ? `更新于 ${st.updated}` : '';
  $('btnMailFetch').disabled = !!st.running;
  $('btnMailUnread').textContent = state.mailUnreadOnly ? '看全部' : '只看未读';
  const w = st.watching || (state.mail && state.mail.watching) || 0;
  const wc = $('mailWatchCount');
  if (wc) {
    wc.textContent = w ? `· 盯着 ${w} 封` : '';
  }
  $('mailFoot').textContent = (st.errors && st.errors.length)
    ? st.errors[st.errors.length - 1] : '';
}

function renderMailList() {
  // 正在看单封的时候后台照常拉邮件,但不能把视图切回列表
  if (state.mailOne) {
    const m = state.mailMsgs.find((x) => x.id === state.mailOne);
    if (m) renderMailOne(m);
    return;
  }
  const list = $('mailList');
  list.textContent = '';
  if (!state.mailMsgs.length) {
    const why = state.mailBox === 'trash'
      ? '垃圾箱是空的。在设置 → 邮箱 → 标签目录里点 🗑 把某个标签设成坏标签,'
        + '带这个标签的信就会收进这儿。'
      : state.mailPerson ? `「${state.mailPerson}」这一组还没有来信。`
        : state.mailTag ? `没有「${state.mailTag}」这一类的邮件。`
      : state.mailDay ? `${state.mailDay} 这天没有邮件。`
        : state.mailMinLevel ? '这个级别以上没有邮件。'
          : state.mailUnreadOnly ? '没有未读。'
            : '收件箱是空的(或者账号还没配好)。';
    list.appendChild(el('div', 'empty', why));
    renderMailPager();
    return;
  }
  state.mailMsgs.forEach((m, i) => {
    list.appendChild(mailCard(m, i));
  });
  renderMailPager();
}

/* 一张卡片 = 固定的五段。顺序和留白都是定死的,扫列表的时候眼睛不用重新找。 */
function mailCard(m, i) {
  const lv = m.rank ? m.rank.level : 1;
  const row = el('div', 'mail-item'
    + (m.unread ? ' is-unread' : '')
    + (m.star ? ' is-star' : '')
    + (m.done ? ' is-done' : '')
    + (lv === 0 ? ' is-low' : ''));
  row.style.animationDelay = `${Math.min(i, 10) * 16}ms`;
  row.draggable = true;
  row.addEventListener('dragstart', (e) => {
    startCtxDrag(e, {
      kind: 'mail', title: m.subject, course: m.account_label,
      from: m.from, date: m.date_local, excerpt: m.snippet, url: m.web_url,
    });
  });

  // ① 发件人。这一行只有它(未读的前面加一颗实心点 —— 蓝色左边线是
  //    颜色单独承载信息,加个形状才说得清)
  const who = el('div', 'mail-who');
  if (m.unread) {
    const dot = el('span', 'unread-dot');
    dot.title = '未读';
    who.appendChild(dot);
  }
  who.appendChild(document.createTextNode(senderName(m.from)));
  who.title = (m.unread ? '未读 · ' : '') + (m.from || '');
  row.appendChild(who);

  // ② 一句话总结。还没过目的没有总结,退回主题 —— 总比空一行强
  const sum = m.rank && m.rank.summary;
  const line2 = el('div', 'mail-sum' + (sum ? '' : ' is-fallback'),
    sum || m.subject || '(无主题)');
  if (!sum) line2.title = '这封还没过目,先显示主题';
  row.appendChild(line2);

  // ③ 分组 + 级别 + 标签。分组排最前 —— 它比标签高一级
  const tagrow = el('div', 'mail-tagrow');
  if (m.person) {
    const p = el('span', 'person-chip', m.person);
    p.title = `这个发件人被你归进「${m.person}」—— 他的信不会被坏标签藏起来`;
    tagrow.appendChild(p);
  }
  if (m.rank) {
    const chip = el('span', 'mail-rank' + (m.rank.pending ? ' is-pending' : ''));
    chip.dataset.lv = String(lv);
    chip.appendChild(el('span', null, m.rank.icon));
    chip.appendChild(el('span', null, m.rank.label));
    chip.title = m.rank.pending
      ? (m.rank.why || '') + ' —— AI 还没过目,这只是本机粗判'
      : (m.rank.why || '没写理由');
    tagrow.appendChild(chip);
  }
  ((m.rank && m.rank.tags) || []).forEach((t) => {
    const b = el('button', 'tag-chip', t);
    b.type = 'button';
    b.title = `只看「${t}」这一类`;
    b.addEventListener('click', (ev) => {
      ev.stopPropagation();
      setMailTag(state.mailTag === t ? '' : t);
    });
    tagrow.appendChild(b);
  });
  row.appendChild(tagrow);

  // ④ 附件
  const files = m.files || [];
  if (files.length) row.appendChild(renderMailFiles(files));

  // ⑤..n 链接,一行一条。**只显示 AI 挑中的**(它读过正文,知道哪条
  //    是报名表单、哪条是退订)。一条都没挑中时也还会有一行"另外 N 条",
  //    点进去看全文 —— 判错了不能让链接彻底消失。
  const links = m.links || [];
  if (links.length) row.appendChild(renderMailLinks(links, m));

  // 末行:时间、操作、☆、展开箭头
  const act = el('div', 'mail-act');
  act.appendChild(el('span', 'mail-date muted', m.date_local || ''));
  const ask = el('button', 'link-btn', '问 Claude');
  ask.type = 'button';
  ask.addEventListener('click', () => {
    addMailCtx({
      kind: 'mail', title: m.subject, course: m.account_label,
      from: m.from, date: m.date_local, excerpt: m.snippet, url: m.web_url,
    });
  });
  act.appendChild(ask);
  if (m.web_url) {
    const open = el('button', 'link-btn', '在网页打开');
    open.type = 'button';
    open.addEventListener('click', () => openExternal(m.web_url));
    act.appendChild(open);
  }
  if (m.star && !m.done) {
    const fin = el('button', 'link-btn', '标完成');
    fin.type = 'button';
    fin.title = '标完成之后简报就不再提这封了';
    fin.addEventListener('click', () => flagMail(m, { done: true }));
    act.appendChild(fin);
  }
  if (m.star_days) act.appendChild(el('span', 'muted', `盯了 ${m.star_days} 天`));

  act.appendChild(el('span', 'mail-act-spacer'));
  const star = el('button', 'mail-star' + (m.star ? ' is-on' : ''),
    m.star ? '★' : '☆');
  star.type = 'button';
  star.title = m.star
    ? `盯住了${m.star_days ? '(' + m.star_days + ' 天)' : ''} —— 每天的邮件简报都会提醒,直到标完成`
    : '盯住它:每天的邮件简报都会点名提醒,直到你标完成';
  star.addEventListener('click', (ev) => {
    ev.stopPropagation();
    flagMail(m, { star: !m.star, done: false });
  });
  act.appendChild(star);
  const more = el('button', 'mail-more', '›');
  more.type = 'button';
  more.title = '打开这封(全文、附件、全部链接)';
  more.addEventListener('click', (ev) => {
    ev.stopPropagation();
    openMailOne(m);
  });
  act.appendChild(more);
  row.appendChild(act);

  // 整张卡片都能点开 —— 但别抢了里面那些按钮和链接的点击
  row.addEventListener('click', (ev) => {
    if (ev.target.closest('button, a, input, select')) return;
    openMailOne(m);
  });
  return row;
}

/* 发件人那一行只要名字。`"Chen, Alex" <a.chen@example.edu>` 这种
   优先取引号里的显示名;没有显示名就用地址本身。 */
function senderName(raw) {
  const s = (raw || '').trim();
  if (!s) return '(无发件人)';
  const m = s.match(/^\s*"?([^"<]*?)"?\s*<([^>]+)>\s*$/);
  if (m) {
    const name = (m[1] || '').trim();
    return name || m[2].trim();
  }
  return s;
}

/* ── 分页 ──
   一页 50 封。翻页不是前端切数组 —— 筛选和排序都在后端做完再切片,
   不然"按重要程度排"只会在当前这 50 封里排。 */
function renderMailPager() {
  const box = $('mailPager');
  if (!box) return;
  box.textContent = '';
  const pages = state.mailPages || 1;
  if (pages <= 1) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  const cur = state.mailPage || 1;
  const mk = (label, to, dis, title) => {
    const b = el('button', 'pg-btn', label);
    b.type = 'button';
    b.disabled = !!dis;
    if (title) b.title = title;
    b.addEventListener('click', () => gotoMailPage(to));
    return b;
  };
  box.appendChild(mk('‹', cur - 1, cur <= 1, '上一页'));
  const jump = el('input', 'pg-input');
  jump.type = 'number';
  jump.min = '1';
  jump.max = String(pages);
  jump.value = String(cur);
  jump.title = '输入页码后回车跳转';
  jump.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      gotoMailPage(Number(jump.value));
    }
  });
  jump.addEventListener('change', () => gotoMailPage(Number(jump.value)));
  box.appendChild(jump);
  box.appendChild(el('span', 'pg-total', `/ ${pages} 页 · 共 ${state.mailTotal} 封`));
  box.appendChild(mk('›', cur + 1, cur >= pages, '下一页'));
}

function gotoMailPage(n) {
  const pages = state.mailPages || 1;
  state.mailPage = Math.max(1, Math.min(Math.round(n) || 1, pages));
  loadMail();
  const p = $('paneMail');
  if (p) p.scrollTop = 0;
}

/* ── 附件、链接、全文 ── */

function fmtSize(n) {
  if (!n) return '';
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return Math.round(n / 1024) + ' KB';
  return (n / 1024 / 1024).toFixed(1) + ' MB';
}

/* 附件在收信那一刻就下到 data/mail_files/<日期>/ 了,这里点的是本地文件 */
function renderMailFiles(files) {
  const box = el('div', 'mail-files');
  box.appendChild(el('span', 'mail-rowicon', '📎'));
  files.forEach((f) => {
    if (f.too_big || !f.path) {
      const s = el('span', 'mail-file is-off',
        `${f.name}(${fmtSize(f.size)},太大没下)`);
      s.title = '超过设置里的单个附件上限,没有自动下载';
      box.appendChild(s);
      return;
    }
    const b = el('button', 'mail-file');
    b.type = 'button';
    b.appendChild(el('span', null, f.name));
    if (f.size) b.appendChild(el('span', 'mail-file-size', fmtSize(f.size)));
    b.title = f.path + '\n单击打开 · 右键打开所在文件夹';
    b.addEventListener('click', () => {
      apiPost('/api/mail/file', { path: f.path }).catch(() => {});
    });
    b.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      apiPost('/api/mail/file', { path: f.path, reveal: true }).catch(() => {});
    });
    box.appendChild(b);
  });
  return box;
}

/* 链接:一行一条,前面一个短标签说明这是个什么链接。

   标签怎么来的:正文里 <a> 的锚文本最准(发信人自己写的"点这里干什么"),
   没有锚文本就按域名和路径猜一个类别。一长串带 query 的 URL 当标题是没法读的。

   默认最多 6 条 —— 一封营销邮件能抽出十几条,全铺出来卡片就变成链接墙了。 */
const LINK_KINDS = [
  [/docs\.google\.com\/forms|qualtrics|forms\.office|jinshuju|wjx\.cn/i, '表单'],
  [/\/jobs?\/|greenhouse|lever\.co|workday|myworkdayjobs|smartrecruiters/i, '职位'],
  [/zoom\.us|teams\.microsoft|meet\.google|webex/i, '会议'],
  [/calendar\.google|\/calendar|outlook\.office.*calendar/i, '日历'],
  [/instructure\.com|\/canvas\//i, 'Canvas'],
  [/drive\.google|dropbox|onedrive|sharepoint|1drv\.ms/i, '网盘'],
  [/youtube\.com|youtu\.be|bilibili/i, '视频'],
  [/\.pdf($|\?)/i, 'PDF'],
  [/linkedin\.com\/(comm\/)?in\//i, '主页'],
  [/\/(login|signin|verify|confirm|activate)/i, '验证'],
  [/\/(track|order|shipment|package)/i, '订单'],
  [/\/(pay|billing|invoice|receipt)/i, '账单'],
];

function linkLabel(l) {
  const t = (l.text || '').trim();
  if (t) return t.length > 18 ? t.slice(0, 18) + '…' : t;
  for (const [re, name] of LINK_KINDS) {
    if (re.test(l.url)) return name;
  }
  try {
    return new URL(l.url).hostname.replace(/^www\./, '');
  } catch (e) {
    return '链接';
  }
}

/* AI 过目时挑出来的那几条链接。

   返回 null 表示"这封信还没有挑选结果"(这一版之前过目的,或者还没过目)
   —— 调用方据此退回老行为,而不是显示成"一条都不值得点"。 */
function pickedLinks(m) {
  const r = m.rank;
  if (!r || !Array.isArray(r.links)) return null;
  const all = m.links || [];
  // n 是 1 起的序号,对应 mailai.links_of 给模型看的那份清单
  return r.links
    .map((p) => (all[p.n - 1] ? { ...all[p.n - 1], ai: p.label } : null))
    .filter(Boolean);
}

function renderMailLinks(links, m, all) {
  const box = el('div', 'mail-linklist');
  let show;
  let rest = 0;
  const picked = all ? null : pickedLinks(m);
  if (picked) {
    // AI 挑过了:只显示它挑中的,其余收进下面那一行
    show = picked;
    rest = links.length - picked.length;
  } else {
    show = all ? links : links.slice(0, 6);
    rest = links.length - show.length;
  }
  show.forEach((l) => {
    const line = el('div', 'mail-linkrow');
    line.appendChild(el('span', 'll-icon', '🔗'));
    // AI 给的标签优先 —— 它读过正文,比按域名猜准
    line.appendChild(el('span', 'll-label', l.ai || linkLabel(l)));
    const b = el('button', 'll-url');
    b.type = 'button';
    // 地址本身也露出来(去掉协议头,短一点),不然点之前不知道要去哪儿
    b.textContent = l.url.replace(/^https?:\/\//, '');
    b.title = '打开 ' + l.url;
    b.addEventListener('click', () => openExternal(l.url));
    line.appendChild(b);
    box.appendChild(line);
  });
  if (rest > 0) {
    // 措辞分两种:AI 筛过的说"另外 N 条",没筛过的说"还有 N 条" ——
    // 前者是"我替你滤掉了",后者是"这里只是放不下"
    const more = el('button', 'mail-linkmore' + (picked ? ' is-filtered' : ''),
      picked ? `另外 ${rest} 条链接(AI 认为用不上)` : `还有 ${rest} 条链接`);
    more.type = 'button';
    more.addEventListener('click', (ev) => {
      ev.stopPropagation();
      openMailOne(m);
    });
    box.appendChild(more);
  }
  return box;
}

/* ── 单封视图 ──

   占满左栏,右栏的对话不受影响:只是把 #paneMail 里列表那几块藏掉
   (靠 .is-one 这个类),不动 grid 布局。退出回列表,滚动位置也还原。 */

function openMailOne(m) {
  const pane = $('paneMail');
  if (!state.mailOne) state.mailScroll = pane.scrollTop;
  state.mailOne = m.id;
  pane.classList.add('is-one');
  $('mailOne').hidden = false;
  renderMailOne(m);
  pane.scrollTop = 0;
  if (state.mailBody[m.id] === undefined) loadMailBody(m);
  // 在这儿读了,邮箱里也该是读过的
  if (m.unread && state.prefs.mailMarkRead !== false) setMailSeen(m, true);
}

/* 同步已读状态到服务器。**这是唯一会改动邮箱的操作**,所以:
   失败了就把本地状态退回去、并且把服务器的原话摆出来 ——
   不能这边显示已读、邮箱里还是未读。 */
async function setMailSeen(m, seen) {
  const was = m.unread;
  m.unread = !seen;                      // 先改本地,界面立刻跟上
  if (state.mailOne === m.id) renderMailOne(m);
  try {
    const r = await apiPost('/api/mail/seen', { id: m.id, seen: !!seen });
    if (r && r.state) renderMailState(Object.assign({}, r.state,
      { watching: (state.mail || {}).watching }));
  } catch (e) {
    m.unread = was;
    showMailBanner('同步已读失败:' + ((e && e.message) || e));
    if (state.mailOne === m.id) renderMailOne(m);
  }
}

function closeMailOne() {
  if (!state.mailOne) return;
  state.mailOne = '';
  const pane = $('paneMail');
  pane.classList.remove('is-one');
  $('mailOne').hidden = true;
  $('mailOne').textContent = '';
  renderMailList();
  pane.scrollTop = state.mailScroll || 0;
}

async function loadMailBody(m) {
  try {
    const d = await apiGet('/api/mail/body', { id: m.id });
    state.mailBody[m.id] = d.text || '';
    if (d.links) m.links = d.links;
    if (d.files) m.files = d.files;
  } catch (e) {
    state.mailBody[m.id] = null;
  }
  if (state.mailOne === m.id) renderMailOne(m);
}

function renderMailOne(m) {
  const box = $('mailOne');
  box.textContent = '';

  // 顶栏:返回 + 时间 + 几个操作。sticky,翻到正文深处也点得着
  const bar = el('div', 'one-bar');
  const back = el('button', 'one-back');
  back.type = 'button';
  back.appendChild(el('span', null, '‹'));
  back.appendChild(el('span', null, '返回列表'));
  back.title = '返回列表(Esc)';
  back.addEventListener('click', closeMailOne);
  bar.appendChild(back);
  bar.appendChild(el('span', 'muted', m.date_local || ''));
  bar.appendChild(el('span', 'one-bar-spacer'));

  const ask = el('button', 'link-btn', '问 Claude');
  ask.type = 'button';
  ask.addEventListener('click', () => {
    addMailCtx({
      kind: 'mail', title: m.subject, course: m.account_label,
      from: m.from, date: m.date_local, excerpt: m.snippet, url: m.web_url,
    });
  });
  bar.appendChild(ask);
  if (m.web_url) {
    const open = el('button', 'link-btn', '在网页打开');
    open.type = 'button';
    open.addEventListener('click', () => openExternal(m.web_url));
    bar.appendChild(open);
  }
  if (m.star && !m.done) {
    const fin = el('button', 'link-btn', '标完成');
    fin.type = 'button';
    fin.addEventListener('click', async () => {
      await flagMail(m, { done: true });
      renderMailOne(m);
    });
    bar.appendChild(fin);
  }
  // 点错了要有退路 —— 这是会改动邮箱的操作,必须能撤
  const rd = el('button', 'link-btn', m.unread ? '标为已读' : '标回未读');
  rd.type = 'button';
  rd.title = m.unread ? '在邮箱里也标成已读' : '在邮箱里也标回未读';
  rd.addEventListener('click', () => setMailSeen(m, !!m.unread));
  bar.appendChild(rd);

  const star = el('button', 'mail-star' + (m.star ? ' is-on' : ''),
    m.star ? '★' : '☆');
  star.type = 'button';
  star.title = m.star ? '取消盯住' : '盯住它:每天的邮件简报都会提醒到你标完成';
  star.addEventListener('click', async () => {
    await flagMail(m, { star: !m.star, done: false });
    renderMailOne(m);
  });
  bar.appendChild(star);
  box.appendChild(bar);

  // 发件人 / 地址 / 主题。发件人后面跟一个分组选择器 ——
  // 认一次人,以后他发的每一封都算
  const whoLine = el('div', 'one-tags');
  whoLine.appendChild(el('span', 'one-who', senderName(m.from)));
  const gsel = el('select', 'select one-group');
  const none = el('option', null, '未分组');
  none.value = '';
  gsel.appendChild(none);
  (state.mailGroups || []).forEach((g) => {
    const o = el('option', null, g);
    o.value = g;
    gsel.appendChild(o);
  });
  gsel.value = m.person || '';
  gsel.title = '把这个发件人归一组:他的信不会被坏标签藏起来,也能单独筛出来看';
  gsel.addEventListener('change', async () => {
    try {
      const r = await apiPost('/api/mail/person',
        { id: m.id, group: gsel.value });
      m.person = r.group || '';
      state.mailBoxes = r.boxes || state.mailBoxes;
      // 同一个人的其他信也要跟着变
      const a = (m.from || '').toLowerCase();
      state.mailMsgs.forEach((x) => {
        if ((x.from || '').toLowerCase() === a) x.person = m.person;
      });
      renderMailOne(m);
      // 分组会改变这封信属于哪个箱子(分了组就不再被坏标签藏着),
      // 列表得重新拉一遍 —— 不然退回去看到的还是旧的那一份缓存
      loadMail();
    } catch (e) {
      showMailBanner('分组没存上:' + ((e && e.message) || e));
    }
  });
  whoLine.appendChild(gsel);
  box.appendChild(whoLine);
  if ((m.from || '').includes('<')) {
    box.appendChild(el('div', 'one-addr', m.from));
  }
  box.appendChild(el('div', 'one-subject', m.subject || '(无主题)'));

  // 一句话总结(过目过的才有)
  if (m.rank && m.rank.summary) {
    box.appendChild(el('div', 'one-sum', m.rank.summary));
  }

  // 级别 + 标签
  const tags = el('div', 'one-tags');
  if (m.rank) {
    const chip = el('span', 'mail-rank' + (m.rank.pending ? ' is-pending' : ''));
    chip.dataset.lv = String(m.rank.level);
    chip.appendChild(el('span', null, m.rank.icon));
    chip.appendChild(el('span', null, m.rank.label));
    chip.title = m.rank.why || '';
    tags.appendChild(chip);
  }
  ((m.rank && m.rank.tags) || []).forEach((t) => {
    const b2 = el('button', 'tag-chip', t);
    b2.type = 'button';
    b2.title = `回列表并只看「${t}」`;
    b2.addEventListener('click', () => {
      closeMailOne();
      setMailTag(t);
    });
    tags.appendChild(b2);
  });
  box.appendChild(tags);

  if ((m.files || []).length) box.appendChild(renderMailFiles(m.files));
  if ((m.links || []).length) box.appendChild(renderMailLinks(m.links, m, true));

  // 正文
  const body = state.mailBody[m.id];
  const pre = el('div', 'one-body');
  if (body === undefined) {
    pre.appendChild(el('span', 'muted', '正在取全文…'));
  } else if (body === null) {
    pre.appendChild(el('span', 'muted',
      '取不到全文(服务器上可能已经没有这封信了)'));
  } else if (!body.trim()) {
    pre.appendChild(el('span', 'muted', m.too_big
      ? '这封信整体超过 25MB(多半是巨型附件),没有取正文 —— 去网页版看。'
      : '这封信没有正文。'));
  } else {
    // **一律走 textContent。** 正文是发信人给的,拼 innerHTML 等于把页面交出去
    linkifyInto(pre, body);
  }
  box.appendChild(pre);
}

const URL_IN_TEXT = /https?:\/\/[^\s<>"'）)\]】,,;;]+/g;

function linkifyInto(node, text) {
  let last = 0;
  text.replace(URL_IN_TEXT, (url, idx) => {
    if (idx > last) node.appendChild(document.createTextNode(text.slice(last, idx)));
    const a = el('button', 'mail-inline-link', url);
    a.type = 'button';
    a.title = '打开 ' + url;
    a.addEventListener('click', () => openExternal(url));
    node.appendChild(a);
    last = idx + url.length;
    return url;
  });
  if (last < text.length) node.appendChild(document.createTextNode(text.slice(last)));
}

/* ── 收件箱 / 垃圾箱 ──

   坏标签(设置 → 邮箱 → 标签目录里点 🗑 设的)命中的信不进收件箱,
   只在这儿。**分了组的发件人豁免** —— 见 server.is_trash。 */

function renderBoxBtn() {
  const b = $('btnMailBox');
  if (!b) return;
  const trash = state.mailBox === 'trash';
  b.classList.toggle('is-on', trash);
  b.title = trash ? '回收件箱' : '切到垃圾箱(坏标签的信在这儿)';
  const n = (state.mailBoxes || {}).trash || 0;
  const badge = $('mailTrashN');
  badge.textContent = n > 99 ? '99+' : String(n);
  badge.hidden = !n;
  document.body.dataset.mailbox = state.mailBox || 'in';
}

function toggleMailBox() {
  state.mailBox = state.mailBox === 'trash' ? 'in' : 'trash';
  state.mailPage = 1;
  state.mailTag = '';          // 两个箱子的标签是两套,别带过去
  loadMail();
}

function renderPersonSel() {
  const sel = $('mailPerson');
  if (!sel) return;
  const counts = (state.mailBoxes || {}).person || {};
  sel.textContent = '';
  const all = el('option', null, '所有人');
  all.value = '';
  sel.appendChild(all);
  (state.mailGroups || []).forEach((g) => {
    const n = counts[g] || 0;
    const o = el('option', null, n ? `${g}(${n})` : g);
    o.value = g;
    sel.appendChild(o);
  });
  sel.value = state.mailPerson || '';
}

/* ── 排序 / 按天 / 按标签 ── */

function renderMailDays() {
  const sel = $('mailDay');
  const cur = state.mailDay || '';
  sel.textContent = '';
  const all = el('option', null, '所有日期');
  all.value = '';
  sel.appendChild(all);
  state.mailDays.forEach((d) => {
    const o = el('option', null, d);
    o.value = d;
    sel.appendChild(o);
  });
  // 选中的那天可能已经被筛掉了(比如同时开着「只看未读」),补一个选项进去,
  // 否则下拉会自己跳回"所有日期",看起来像筛选被吞了
  if (cur && !state.mailDays.includes(cur)) {
    const o = el('option', null, cur);
    o.value = cur;
    sel.appendChild(o);
  }
  sel.value = cur;
}

function renderMailTagBar() {
  const bar = $('mailTagBar');
  bar.textContent = '';
  const counts = state.mailTagCounts || {};
  // 当前选中的那个标签一定要在,否则点了之后它自己会从条上消失
  const names = Object.keys(counts);
  if (state.mailTag && !names.includes(state.mailTag)) names.push(state.mailTag);
  if (!names.length) {
    bar.hidden = true;
    return;
  }
  bar.hidden = false;
  const all = el('button', 'tag-chip' + (state.mailTag ? '' : ' is-on'), '全部');
  all.type = 'button';
  all.addEventListener('click', () => setMailTag(''));
  bar.appendChild(all);
  names.sort((a, b) => (counts[b] || 0) - (counts[a] || 0) || a.localeCompare(b));
  names.forEach((t) => {
    const b = el('button', 'tag-chip' + (state.mailTag === t ? ' is-on' : ''));
    b.type = 'button';
    b.appendChild(el('span', null, t));
    if (counts[t]) b.appendChild(el('span', 'tag-n', String(counts[t])));
    b.addEventListener('click', () => setMailTag(state.mailTag === t ? '' : t));
    bar.appendChild(b);
  });
}

function setMailTag(t) {
  state.mailTag = t || '';
  state.mailPage = 1;     // 换了筛选还停在第 7 页多半是空的
  loadMail();
}

/* 状态条。补抓排在过目前面 —— 过目要读正文,补抓是它的前提。
   只在真的有事(在跑 / 还有没处理的 / 出错)的时候才占位置 */
function renderAiBar() {
  const bar = $('mailAiBar');
  const f = state.mailFill;
  if (f && f.running) {
    bar.textContent = `正在取邮件正文和附件 ${f.done}/${f.total} 封`;
    bar.hidden = false;
    return;
  }
  if (f && f.pending) {
    bar.textContent = `还有 ${f.pending} 封没取正文(链接和附件要取过才有)`;
    bar.hidden = false;
    return;
  }
  const a = state.mailAi;
  if (!a) {
    bar.hidden = true;
    return;
  }
  const err = (a.errors || [])[a.errors ? a.errors.length - 1 : 0];
  if (a.running) {
    bar.textContent = `AI 正在过目 ${a.done}/${a.total} 封`
      + (a.cost ? ` · 本轮 $${a.cost}` : '');
    bar.hidden = false;
  } else if (err) {
    bar.textContent = '过目失败:' + err;
    bar.hidden = false;
  } else if (a.pending) {
    bar.textContent = `还有 ${a.pending} 封没过目 —— 设置 → 邮箱可以手动催一下`;
    bar.hidden = false;
  } else {
    bar.hidden = true;
  }
}

/* 标注:★ 盯住 / 完成。
   先改本地再发请求 —— 点一下要立刻有反应,不然会以为没点上。 */
async function flagMail(m, patch) {
  Object.assign(m, patch);
  if (patch.star) m.star_days = 0;
  if (patch.star === false) { m.star_days = 0; m.done = false; }
  renderMailList();
  try {
    const r = await apiPost('/api/mail/flag', Object.assign({ id: m.id }, patch));
    if (r && r.flags) {
      m.star = !!r.flags.star;
      m.done = !!r.flags.done;
    }
    if (state.mail) state.mail.watching = r.watching;
  } catch (e) {
    reportError('mailflag', (e && e.message) || String(e), '', 0, e && e.stack);
  }
  // 级别会跟着变(盯住 = 3 级),所以重新拉一遍拿新的 rank
  loadMail();
  if (!$('prefsBackdrop').hidden && state.prefsTab === 'mail') loadWatching();
}

/* ── 邮件简报(独立存档)── */

async function loadMailBriefIndex() {
  let d;
  try {
    d = await apiGet('/api/mail/briefings');
  } catch (e) {
    return;
  }
  state.mailBriefIndex = d.index || [];
  state.mailBriefToday = d.today;
  const sel = $('mailBriefDate');
  const keep = sel.value;
  sel.textContent = '';
  state.mailBriefIndex.forEach((b) => {
    const o = el('option', null, b.date + (b.date === d.today ? '(今天)' : ''));
    o.value = b.date;
    sel.appendChild(o);
  });
  if (!state.mailBriefIndex.length) {
    const o = el('option', null, '还没有邮件简报');
    o.value = '';
    sel.appendChild(o);
    showMailBrief(null, d);
    return;
  }
  // 默认最新那一份;只有用户自己翻过日期(mailBriefPinned)才保留他的选择
  sel.value = state.mailBriefPinned
    && state.mailBriefIndex.some((x) => x.date === keep)
    ? keep : state.mailBriefIndex[0].date;
  showMailBrief(sel.value);
}

async function showMailBrief(date, meta) {
  const box = $('mailBriefBody');
  box.textContent = '';
  box.classList.remove('streaming');
  if (!date) {
    const hour = (meta && meta.brief_hour) || state.prefs.mailHour || 9;
    box.appendChild(el('div', 'empty',
      `每天 ${hour}:00 自动出一份邮件简报。现在想看就点「生成今日」。`));
    return;
  }
  let d;
  try {
    d = await apiGet(`/api/mail/briefings/${date}`);
  } catch (e) {
    box.appendChild(el('div', 'empty', '读不到:' + e.message));
    return;
  }
  box.appendChild(el('div', 'brief-meta', `${date}${d.at ? ' · 生成于 ' + d.at : ''}`));
  const body = el('div');
  renderMarkdown(d.text || '(空)', body);
  box.appendChild(body);
}

function handleMailBriefEvent(ev) {
  const box = $('mailBriefBody');
  switch (ev.kind) {
    case 'start':
      state.mailBriefLive = '';
      setMailBriefStatus('正在生成邮件简报…');
      break;
    case 'tool':
      setMailBriefStatus(ev.text + '…');
      break;
    case 'delta':
      setMailBriefStatus(null);
      state.mailBriefLive += ev.text;
      scheduleRender('mailbrief');
      break;
    case 'error': {
      const err = el('div', 'msg msg-error');
      err.appendChild(el('div', 'msg-body', ev.text));
      box.appendChild(err);
      break;
    }
    case 'done':
      setMailBriefStatus(null);
      box.classList.remove('streaming');
      setTimeout(loadMailBriefIndex, 400);
      break;
  }
}

function flushMailBrief() {
  const box = $('mailBriefBody');
  box.textContent = '';
  box.classList.add('streaming');
  box.appendChild(el('div', 'brief-meta', `${state.mailBriefToday} · 正在生成…`));
  const body = el('div');
  renderMarkdown(state.mailBriefLive, body);
  box.appendChild(body);
  box.scrollTop = box.scrollHeight;
}

function setMailBriefStatus(text) {
  const bar = $('mailBriefStatus');
  if (!text) { bar.hidden = true; return; }
  bar.textContent = '';
  bar.appendChild(el('span', 'pulse'));
  bar.appendChild(el('span', null, text));
  bar.hidden = false;
}

/* ── 邮件对话(独立历史)── */

function setMailStreaming(on) {
  state.mailStreaming = on;
  $('btnMailSend').disabled = on;
  if (!on) {
    setMailChatStatus(null);
    if (state.mailBubble) {
      renderMarkdown(state.mailBubbleRaw, state.mailBubble);
      state.mailBubble.classList.remove('caret', 'streaming');
    }
    state.mailBubble = null;
  }
}

function setMailChatStatus(text) {
  const bar = $('mailChatStatus');
  if (!text) { bar.hidden = true; return; }
  bar.textContent = '';
  bar.appendChild(el('span', 'pulse'));
  bar.appendChild(el('span', null, text));
  bar.hidden = false;
}

function handleMailChatEvent(ev) {
  switch (ev.kind) {
    case 'start':
      setMailStreaming(true);
      setMailChatStatus('思考中…');
      break;
    case 'thinking':
      setMailChatStatus('思考中…');
      break;
    case 'tool':
      setMailChatStatus(ev.text + '…');
      break;
    case 'delta':
      if (!state.mailBubble) {
        state.mailBubble = addMsgIn($('mailChatLog'), 'assistant', '');
        state.mailBubble.classList.add('caret', 'streaming');
        state.mailBubbleRaw = '';
      }
      setMailChatStatus(null);
      state.mailBubbleRaw += ev.text;
      scheduleRender('mailchat');
      break;
    case 'error':
      addMsgIn($('mailChatLog'), 'error', ev.text);
      break;
    case 'cost':
      if (ev.session_cost) {
        const m = $('mailSideMeta');
        m.textContent = `这段约 $${ev.session_cost.toFixed(3)}`;
        m.title = '按 API 标价折算的估值。订阅席位不按此扣费,但会消耗用量额度。';
      }
      break;
    case 'done':
      setMailStreaming(false);
      loadMailChatIndex();
      break;
  }
}

function flushMailChat() {
  if (!state.mailBubble) return;
  renderMarkdown(state.mailBubbleRaw, state.mailBubble);
  const log = $('mailChatLog');
  log.scrollTop = log.scrollHeight;
}

async function sendMailChat(text) {
  text = (text || '').trim();
  if (!text || state.mailStreaming) return;
  const ctx = state.mailCtxSent ? [] : state.mailCtx.slice();
  addMsgIn($('mailChatLog'), 'user', text, state.mailCtx.slice());
  $('mailChatInput').value = '';
  autoGrowEl($('mailChatInput'));
  setMailStreaming(true);
  setMailChatStatus('连接中…');
  let r;
  try {
    r = await apiPost('/api/mailchat', { message: text, context: ctx });
    if (ctx.length) state.mailCtxSent = true;
  } catch (e) {
    addMsgIn($('mailChatLog'), 'error', '后端没响应:' + e.message);
    setMailStreaming(false);
    return;
  }
  if (!r.ok) { addMsgIn($('mailChatLog'), 'error', r.message); setMailStreaming(false); }
}

async function loadMailChatIndex() {
  let d;
  try {
    d = await apiGet('/api/mailchats');
  } catch (e) {
    return;
  }
  state.mailChats = d.index || [];
  state.mailChatId = d.active || '';
  const sel = $('mailChatPick');
  sel.textContent = '';
  state.mailChats.forEach((c) => {
    const o = el('option', null, `${c.title}${c.turns ? ` · ${c.turns} 轮` : ''}`);
    o.value = c.id;
    sel.appendChild(o);
  });
  if (state.mailChatId) sel.value = state.mailChatId;
}

async function openMailChat(id) {
  if (!id) return;
  let r;
  try {
    r = await apiPost('/api/mailchats/open', { id: id });
  } catch (e) {
    return;
  }
  if (!r.ok || !r.chat) return;
  state.mailChatId = id;
  const log = $('mailChatLog');
  log.textContent = '';
  setMailStreaming(false);
  (r.chat.messages || []).forEach((m) =>
    addMsgIn(log, m.role === 'user' ? 'user' : 'assistant', m.text, m.ctx));
  log.scrollTop = log.scrollHeight;
  if (r.chat.cost) {
    $('mailSideMeta').textContent = `这段约 $${Number(r.chat.cost).toFixed(3)}`;
  }
}

/* 邮件对话的关联对象(和课业那套各自一份,不会串) */
function addMailCtx(item) {
  const same = (a, b) => a.kind === b.kind && a.title === b.title;
  if (state.mailCtx.some((x) => same(x, item))) return;
  state.mailCtx.push(item);
  if (state.mailCtx.length > CTX_MAX) state.mailCtx.shift();
  state.mailCtxSent = false;
  renderMailCtxBar();
  switchMailTab('chat');
  $('mailChatInput').focus();
}

function renderMailCtxBar() {
  const bar = $('mailCtxBar');
  bar.textContent = '';
  if (!state.mailCtx.length) { bar.hidden = true; return; }
  bar.hidden = false;
  bar.appendChild(el('span', 'ctx-label', '针对'));
  state.mailCtx.forEach((it, i) => {
    const chip = el('span', 'ctx-chip');
    chip.appendChild(el('span', 'ctx-icon', '✉'));
    chip.appendChild(el('span', 'ctx-text', it.title || ''));
    const x = el('button', 'ctx-x', '×');
    x.type = 'button';
    x.title = '取消关联';
    x.addEventListener('click', () => {
      state.mailCtx.splice(i, 1);
      state.mailCtxSent = false;
      renderMailCtxBar();
    });
    chip.appendChild(x);
    bar.appendChild(chip);
  });
}

function switchMailTab(which) {
  const prev = state.mailTab;      // 同上
  state.mailTab = which;
  if (which === 'brief' && prev !== 'brief') {
    state.mailBriefPinned = false;
    loadMailBriefIndex();
  }
  $('mailTabBrief').classList.toggle('is-active', which === 'brief');
  $('mailTabChat').classList.toggle('is-active', which === 'chat');
  $('mailTabMemo').classList.toggle('is-active', which === 'memo');
  $('mailPanelBrief').hidden = which !== 'brief';
  $('mailPanelChat').hidden = which !== 'chat';
  showMemoPanel(which === 'memo', $('paneMailSide'));
  if (which === 'chat') $('mailChatInput').focus();
}

function wireMail() {
  document.querySelectorAll('#pageSeg .seg-btn').forEach((b) => {
    b.addEventListener('click', () => setPage(b.dataset.v));
  });

  $('mailAccount').addEventListener('change', (e) => {
    state.mailAccount = e.target.value;
    loadMail();
  });
  $('mailLevel').addEventListener('change', (e) => {
    state.mailMinLevel = Number(e.target.value) || 0;
    state.mailPage = 1;
    loadMail();
  });
  $('btnMailBox').addEventListener('click', toggleMailBox);
  $('mailPerson').addEventListener('change', (e) => {
    state.mailPerson = e.target.value;
    state.mailPage = 1;
    loadMail();
  });
  $('mailSort').addEventListener('change', (e) => {
    state.mailSort = e.target.value;
    savePrefs({ mailSort: state.mailSort });
    loadMail();
  });
  $('mailDay').addEventListener('change', (e) => {
    state.mailDay = e.target.value;
    state.mailPage = 1;
    loadMail();
  });
  $('btnMailUnread').addEventListener('click', () => {
    state.mailUnreadOnly = !state.mailUnreadOnly;
    loadMail();
  });
  $('btnMailFetch').addEventListener('click', async () => {
    try {
      const r = await apiPost('/api/mail/fetch', {});
      if (r && r.state) renderMailState(r.state);
    } catch (e) { /* 状态会顺着 SSE 回来 */ }
  });

  $('mailTabMemo').addEventListener('click', () => switchMailTab('memo'));
  $('mailTabBrief').addEventListener('click', () => switchMailTab('brief'));
  $('mailTabChat').addEventListener('click', () => switchMailTab('chat'));
  $('mailBriefDate').addEventListener('change', (e) => {
    state.mailBriefPinned = true;
    showMailBrief(e.target.value);
  });
  $('btnMailGenBrief').addEventListener('click', async () => {
    const already = state.mailBriefIndex.some((x) => x.date === state.mailBriefToday);
    const r = await apiPost('/api/mail/briefings/generate', { force: already });
    if (!r.started) setMailBriefStatus(r.reason);
  });

  $('mailComposer').addEventListener('submit', (e) => {
    e.preventDefault();
    sendMailChat($('mailChatInput').value);
  });
  $('mailChatInput').addEventListener('input', () => autoGrowEl($('mailChatInput')));
  $('mailChatInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMailChat($('mailChatInput').value);
    }
  });

  $('mailChatPick').addEventListener('change', (e) => openMailChat(e.target.value));
  $('btnMailNewChat').addEventListener('click', async () => {
    state.mailCtx = [];
    state.mailCtxSent = false;
    renderMailCtxBar();
    await apiPost('/api/mailchat/reset');
    $('mailChatLog').textContent = '';
    $('mailSideMeta').textContent = '';
    setMailStreaming(false);
    await loadMailChatIndex();
  });
  const del = $('btnMailDelChat');
  let armed = 0;
  del.addEventListener('click', async () => {
    if (Date.now() - armed > 3000) {
      armed = Date.now();
      del.textContent = '确认删除?';
      del.classList.add('is-armed');
      setTimeout(() => {
        if (Date.now() - armed >= 3000) {
          del.textContent = '删除';
          del.classList.remove('is-armed');
        }
      }, 3100);
      return;
    }
    armed = 0;
    del.textContent = '删除';
    del.classList.remove('is-armed');
    const r = await apiPost('/api/mailchats/delete', { id: state.mailChatId });
    await loadMailChatIndex();
    if (r && r.active) await openMailChat(r.active);
  });

  // 邮件也能拖到右栏
  const zone = $('paneMailSide');
  let depth = 0;
  zone.addEventListener('dragenter', (e) => {
    if (!e.dataTransfer.types.includes('application/x-canvas-item')) return;
    e.preventDefault();
    depth += 1;
    zone.classList.add('is-drop');
  });
  zone.addEventListener('dragover', (e) => {
    if (!e.dataTransfer.types.includes('application/x-canvas-item')) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
  });
  zone.addEventListener('dragleave', () => {
    depth = Math.max(0, depth - 1);
    if (!depth) zone.classList.remove('is-drop');
  });
  zone.addEventListener('drop', (e) => {
    e.preventDefault();
    depth = 0;
    zone.classList.remove('is-drop');
    const raw = e.dataTransfer.getData('application/x-canvas-item');
    if (!raw) return;
    try {
      addMailCtx(JSON.parse(raw));
    } catch (err) { /* 拖进来的不是我们的东西 */ }
  });
}

/* ── 设置面板里的邮箱账号 ── */

function renderMailAccountsPrefs(accs) {
  const box = $('mailAccList');
  box.textContent = '';
  if (!accs || !accs.length) {
    box.appendChild(el('div', 'muted', '还没有账号。'));
    return;
  }
  accs.forEach((a) => {
    const row = el('div', 'mail-acc-row');
    const main = el('div', 'mail-acc-main');
    main.appendChild(el('div', 'mail-acc-name', a.label || a.email));
    main.appendChild(el('div', 'mail-acc-sub muted',
      [a.email, a.ready ? '已就绪' : '未就绪',
       a.last_sync ? '上次 ' + a.last_sync : '',
       a.last_error || ''].filter(Boolean).join(' · ')));
    row.appendChild(main);
    const rm = el('button', 'link-btn link-danger', '移除');
    rm.type = 'button';
    rm.addEventListener('click', async () => {
      await apiPost('/api/mail/accounts/remove', { id: a.id });
      refreshMailPrefs();
    });
    row.appendChild(rm);
    box.appendChild(row);
  });
}

async function refreshMailPrefs() {
  try {
    const d = await apiGet('/api/mail', { limit: '1' });
    renderMailAccountsPrefs(d.accounts || []);
  } catch (e) { /* 面板没开就算了 */ }
}

function wireMailPrefs() {
  $('mailPreset').addEventListener('change', (e) => {
    $('mailHost').hidden = e.target.value !== 'other';
  });
  $('btnMailAddImap').addEventListener('click', async () => {
    const st = $('mailAddState');
    st.textContent = '验证中…';
    let r;
    try {
      r = await apiPost('/api/mail/accounts/imap', {
        email: $('mailEmail').value.trim(),
        password: $('mailPassword').value.trim(),
        preset: $('mailPreset').value,
        host: $('mailHost').value.trim(),
      });
    } catch (e) {
      st.textContent = '出错:' + e.message;
      return;
    }
    st.textContent = r.ok ? '加好了,正在收邮件' : ('登不上:' + (r.error || '未知原因'));
    if (r.ok) {
      $('mailPassword').value = '';
      $('foldQQ').open = false;
      refreshMailPrefs();
    }
  });

  $('btnFactAdd').addEventListener('click', async () => {
    const facts = (state.prefs.mailFacts || []).concat([{ k: '', v: '' }]);
    // 必须等它画完再聚焦。反过来的话焦点会落在旧的最后一行上,而 renderFacts
    // 一看"焦点在里面"就不重绘了 —— 新行画不出来,接着打的字还进了上一行
    await saveFacts(facts, false, true);
    const rows = document.querySelectorAll('#factList .fact-k');
    if (rows.length) rows[rows.length - 1].focus();
  });

  toggle('swMailAi', 'mailAiOn');
  toggle('swMarkRead', 'mailMarkRead');
  $('selMailModel').addEventListener('change', (e) => {
    savePrefs({ mailModel: e.target.value });
  });
  $('btnMailFill').addEventListener('click', async () => {
    $('fillState').textContent = '开始取…';
    try {
      const r = await apiPost('/api/mail/backfill', {});
      state.mailFill = r.fill || null;
      $('fillState').textContent = r.started
        ? `正在取 ${(r.fill || {}).total || ''}…` : '都取过了';
      renderAiBar();
    } catch (e) {
      $('fillState').textContent = '出错:' + e.message;
    }
  });
  $('btnMailAnalyze').addEventListener('click', () => runAnalyze(false));
  $('btnMailRedo').addEventListener('click', () => runAnalyze(true));
  $('btnMailRelink').addEventListener('click', () => runRelink());

  $('btnTagAdd').addEventListener('click', addTag);
  $('btnGroupAdd').addEventListener('click', addGroup);
  $('groupNew').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      addGroup();
    }
  });
  $('tagNew').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      addTag();
    }
  });
  $('btnTagReset').addEventListener('click', async () => {
    const r = await apiPost('/api/mail/tags', { reset: true });
    applyPrefs(Object.assign({}, state.prefs, { mailTags: r.tags }));
    renderTagCatalog();
  });

}

/* ─────────────────────── 拖拽关联 ───────────────────────

   左栏的作业/公告/文件拖到右栏,就成为这次对话的"关联对象":发问题时会在前面
   附一段说明(标题 + 截止 + 分值 + course_id/assignment_id),所以直接说
   「看看这个作业是干啥的」也知道是哪个。

   只在**关联对象变化后的第一条消息**附那段说明:后续追问靠 CLI 的会话上下文
   接着就行,每轮都重发是白烧额度。 */

const CTX_MAX = 4;

function startCtxDrag(e, payload) {
  try {
    e.dataTransfer.setData('application/x-canvas-item', JSON.stringify(payload));
    // 给个纯文本兜底:万一拖到外面别的程序,至少是个标题
    e.dataTransfer.setData('text/plain', payload.title || '');
    e.dataTransfer.effectAllowed = 'copy';
  } catch (err) {
    reportError('dragstart', (err && err.message) || String(err), '', 0, err && err.stack);
  }
}

async function addCtx(item) {
  // 文件必须有本地副本,Claude 才读得到。后台同步器平时就把课件都拉下来了,
  // 万一这个还没同步到(刚上传的),就地补下一次
  if (item.kind === 'file' && !item.path && item.file_id) {
    state.ctx.push({ ...item, pending: true });
    renderCtxBar();
    let r = null;
    try {
      r = await apiPost('/api/files/download',
        { course_id: item.course_id, file_id: item.file_id });
    } catch (e) {
      r = { ok: false, error: e.message };
    }
    state.ctx = state.ctx.filter((x) => !x.pending);
    if (!r || !r.ok) {
      renderCtxBar();
      showBanner((r && r.error) || '这个文件下不下来');
      return;
    }
    item = { ...item, path: r.path };
    if (state.courseId) openCourse(state.courseId);
  }
  // 同一个东西别重复加
  const same = (a, b) =>
    a.kind === b.kind && a.title === b.title && a.course === b.course;
  if (state.ctx.some((x) => same(x, item))) return;
  state.ctx.push(item);
  if (state.ctx.length > CTX_MAX) state.ctx.shift();
  state.ctxSent = false;
  renderCtxBar();
  switchTab('chat');
  $('chatInput').focus();
}

function removeCtx(i) {
  state.ctx.splice(i, 1);
  state.ctxSent = false;
  renderCtxBar();
}

function renderCtxBar() {
  const bar = $('ctxBar');
  bar.textContent = '';
  if (!state.ctx.length) {
    bar.hidden = true;
    return;
  }
  bar.hidden = false;
  bar.appendChild(el('span', 'ctx-label', '针对'));
  state.ctx.forEach((it, i) => {
    const chip = el('span', 'ctx-chip' + (it.pending ? ' is-pending' : ''));
    const icon = { assignment: '✎', announcement: '▣', file: '⎘' }[it.kind] || '·';
    chip.appendChild(el('span', 'ctx-icon', icon));
    chip.appendChild(el('span', 'ctx-text',
      (it.course ? it.course + ' · ' : '') + (it.title || '')));
    const x = el('button', 'ctx-x', '×');
    x.type = 'button';
    x.title = '取消关联';
    x.addEventListener('click', () => removeCtx(i));
    chip.appendChild(x);
    bar.appendChild(chip);
  });
}

function wireDropZone() {
  const zone = $('paneSide');
  let depth = 0;      // dragenter/dragleave 会在子元素之间来回触发,得计数

  zone.addEventListener('dragenter', (e) => {
    if (!e.dataTransfer.types.includes('application/x-canvas-item')) return;
    e.preventDefault();
    depth += 1;
    zone.classList.add('is-drop');
  });
  zone.addEventListener('dragover', (e) => {
    if (!e.dataTransfer.types.includes('application/x-canvas-item')) return;
    e.preventDefault();                       // 不拦就不会触发 drop
    e.dataTransfer.dropEffect = 'copy';
  });
  zone.addEventListener('dragleave', () => {
    depth = Math.max(0, depth - 1);
    if (!depth) zone.classList.remove('is-drop');
  });
  zone.addEventListener('drop', (e) => {
    e.preventDefault();
    depth = 0;
    zone.classList.remove('is-drop');
    const raw = e.dataTransfer.getData('application/x-canvas-item');
    if (!raw) return;
    try {
      addCtx(JSON.parse(raw));
    } catch (err) {
      reportError('drop', (err && err.message) || String(err), '', 0, err && err.stack);
    }
  });
}

/* ─────────────────────────── 对话历史 ───────────────────────────

   消息存在后端的 data/chats.json。上下文本身是 claude CLI 那边的 session
   (--resume),这里存原文是为了两件事:能回看,以及 session 没了之后还能把
   最近 10 轮拼回去接着聊(见 chats.py 的 context_block)。 */

async function loadChatIndex() {
  let d;
  try {
    d = await apiGet('/api/chats');
  } catch (e) {
    return;
  }
  state.chats = d.index || [];
  state.chatId = d.active || '';
  state.contextTurns = d.context_turns || 10;

  const sel = $('chatPick');
  sel.textContent = '';
  state.chats.forEach((c) => {
    const o = el('option', null, `${c.title}${c.turns ? ` · ${c.turns} 轮` : ''}`);
    o.value = c.id;
    sel.appendChild(o);
  });
  if (state.chatId) sel.value = state.chatId;
  const cur = state.chats.find((c) => c.id === state.chatId);
  sel.title = cur
    ? `${cur.title} · ${cur.turns} 轮 · 最近 ${cur.updated}`
    : '新对话';
}

async function openChat(id) {
  if (!id) return;
  let r;
  try {
    r = await apiPost('/api/chats/open', { id: id });
  } catch (e) {
    reportError('openChat', (e && e.message) || String(e), '', 0, e && e.stack);
    return;
  }
  if (!r.ok || !r.chat) return;
  state.chatId = id;
  renderChatLog(r.chat.messages || []);
  if (r.chat.cost) {
    const m = $('sideMeta');
    m.textContent = `这段约 $${Number(r.chat.cost).toFixed(3)}`;
    m.title = '按 API 标价折算的估值。订阅席位不按此扣费,但会消耗用量额度。';
  }
}

function renderChatLog(messages) {
  const log = $('chatLog');
  log.textContent = '';
  setStreaming(false);
  if (!messages.length) {
    renderSuggests();
    return;
  }
  messages.forEach((m) => addMsg(m.role === 'user' ? 'user' : 'assistant', m.text, m.ctx));
  log.scrollTop = log.scrollHeight;
}

function wireChatHistory() {
  $('chatPick').addEventListener('change', (e) => openChat(e.target.value));

  $('btnNewChat').addEventListener('click', async () => {
    state.ctx = [];
    state.ctxSent = false;
    renderCtxBar();
    await apiPost('/api/chat/reset');
    $('chatLog').textContent = '';
    $('sideMeta').textContent = '';
    setStreaming(false);
    renderSuggests();
    await loadChatIndex();
  });

  // 删除要点两下:第一下变成"确认删除",3 秒内再点才真删。
  // 不用 confirm() —— 无边框窗口里弹原生对话框很突兀。
  const del = $('btnDelChat');
  let armed = 0;
  del.addEventListener('click', async () => {
    if (Date.now() - armed > 3000) {
      armed = Date.now();
      del.textContent = '确认删除?';
      del.classList.add('is-armed');
      setTimeout(() => {
        if (Date.now() - armed >= 3000) {
          del.textContent = '删除';
          del.classList.remove('is-armed');
        }
      }, 3100);
      return;
    }
    armed = 0;
    del.textContent = '删除';
    del.classList.remove('is-armed');
    const r = await apiPost('/api/chats/delete', { id: state.chatId });
    await loadChatIndex();
    if (r && r.active) await openChat(r.active);
    else renderChatLog([]);
  });
}

/* ─────────────────────────── 简报栏 ───────────────────────────
   每天一份,09:00 由后端调度触发(9 点后才开机则开窗时补报)。
   这里只负责展示和手动触发。 */

async function loadBriefIndex() {
  let d;
  try {
    d = await apiGet('/api/briefings');
  } catch (e) {
    return;
  }
  state.briefIndex = d.index || [];
  state.briefToday = d.today;
  state.briefGenerating = d.generating;

  const sel = $('briefDate');
  const keep = sel.value;
  sel.textContent = '';
  state.briefIndex.forEach((it) => {
    const o = el('option', null, it.date === d.today ? `${it.date}(今天)` : it.date);
    o.value = it.date;
    sel.appendChild(o);
  });

  if (!state.briefIndex.length) {
    showBriefEmpty(d);
    return;
  }
  // **默认永远是最新那一份。** 只有用户正在这一栏里自己翻别的日期时才保留
  // 他的选择(briefPinned 由那个下拉的 change 事件置上)—— 切走再切回来
  // 想看到的是最新的,不是上次翻到哪儿
  const target =
    state.briefPinned && state.briefIndex.some((x) => x.date === keep)
      ? keep : state.briefIndex[0].date;
  sel.value = target;
  showBrief(target);
}

function showBriefEmpty(d) {
  const box = $('briefBody');
  box.textContent = '';
  const wrap = el('div', 'brief-empty');
  if (d && d.generating) {
    wrap.appendChild(el('p', null, '正在生成今天的简报…'));
  } else if (d && !d.has_today) {
    wrap.appendChild(el('p', null, `还没有简报。每天 ${d.brief_hour}:00 会自动生成一份。`));
    wrap.appendChild(el('p', 'muted', '不想等就点右上角「生成今日」。'));
  } else {
    wrap.appendChild(el('p', null, '还没有存档的简报。'));
  }
  box.appendChild(wrap);
}

async function showBrief(date) {
  const box = $('briefBody');
  let d;
  try {
    d = await apiGet('/api/briefings/' + date);
  } catch (e) {
    box.textContent = '';
    box.appendChild(el('div', 'brief-empty', '读取这一天的简报失败。'));
    return;
  }
  box.textContent = '';
  const meta = el('div', 'brief-meta');
  meta.appendChild(
    document.createTextNode(`${d.date} · 生成于 ${d.generated_at}`)
  );
  box.appendChild(meta);
  const body = el('div');
  renderMarkdown(d.text || '', body);
  box.appendChild(body);
  box.scrollTop = 0;   // 流式时是贴着底部走的,切到存档要回到开头
}

function setBriefStatus(text) {
  const bar = $('briefStatus');
  if (!text) { bar.hidden = true; return; }
  bar.textContent = '';
  bar.appendChild(el('span', 'pulse'));
  bar.appendChild(el('span', null, text));
  bar.hidden = false;
}

function markBriefUnread(on) {
  const tab = $('tabBrief');
  const existing = tab.querySelector('.badge');
  if (on && !existing && state.tab !== 'brief') {
    tab.appendChild(el('span', 'badge'));
  } else if (!on && existing) {
    existing.remove();
  }
}

/* 简报通道的流式事件 */
function handleBriefEvent(ev) {
  switch (ev.kind) {
    case 'start':
      state.briefGenerating = true;
      state.briefLive = '';
      setBriefStatus('正在生成今天的简报…');
      break;
    case 'tool':
      setBriefStatus(ev.text + '…');
      break;
    case 'delta':
      setBriefStatus(null);
      state.briefLive += ev.text;
      scheduleRender('brief');
      break;
    case 'error': {
      const box = $('briefBody');
      const err = el('div', 'msg msg-error');
      err.appendChild(el('div', 'msg-body', ev.text));
      box.appendChild(err);
      break;
    }
    case 'done':
      state.briefGenerating = false;
      setBriefStatus(null);
      $('briefBody').classList.remove('streaming');
      markBriefUnread(true);
      // 后端在 on_done 里落盘,重载目录就能拿到正式存档
      setTimeout(loadBriefIndex, 400);
      break;
  }
}

/* ─────────────────────── 三态窗口 ───────────────────────

   orb  悬浮球     -> 点一下开 chat(球是原生窗口,见 orb_window.py;
                      收成球的时候这个 WebView 窗口整个隐藏)
   chat 只有对话框 -> 点「展开」进 full,双击标题栏也行
   full 完整面板   -> 双击标题栏最大化 / 还原

   窗口操作走 HTTP,后端用纯 Win32 执行。**不用 pywebview 的 js_api 桥** ——
   实测挂上它这个页面就会把窗口卡死成「未响应」,详见 native_window.py。 */

async function setMode(mode) {
  applyModeClass(mode);
  // 让浏览器先把淡出这一帧画出去,再让原生窗口开始形变
  await new Promise((r) => requestAnimationFrame(() => r()));
  try {
    await apiPost('/api/window/mode', { mode: mode });
  } catch (e) {
    reportError('setMode', (e && e.message) || String(e), '', 0, e && e.stack);
  }
  if (mode === 'chat') switchTab('chat');
  if (mode !== 'orb' && state.tab === 'chat') $('chatInput').focus();
}

/* 只改页面这一侧的形态。窗口事件(点原生悬浮球)也会走到这里 ——
   那种情况下窗口已经由后端摆好了,不能再 POST 回去,否则来回打转。

   is-morphing 的作用是把内容整个淡掉:原生窗口形变是逐帧 SetWindowPos,
   每一帧 WebView2 都要重排一次,不遮住的话看起来就是"抽了一下"。
   收成球之后**不摘**这个 class —— 窗口是隐藏的,内容留在淡掉的状态,
   下次展开时先长出一个空壳、动画结束再淡入,中间不会闪一帧错位的布局。 */
let morphTimer = 0;

function applyModeClass(mode) {
  const changing = state.mode !== mode;
  const wasOrb = state.mode === 'orb';
  state.mode = mode;
  // 放在"形态没变就返回"**前面**:启动时那次 setMode 是同态的,
  // 但按钮的图标还没摆对
  syncSizeBtn(mode);
  // 收成球时页面提前排成 chat 布局:窗口此刻是隐藏的,等下点球弹出来就是现成的
  document.body.dataset.mode = mode === 'orb' ? 'chat' : mode;
  // 形态没变就什么都不用做 —— 启动时那次 setMode 也走这里,
  // 不拦住的话界面会先白一下再回来
  if (!changing) return;
  document.body.classList.add('is-morphing');
  // 收球 / 展开两个方向上页面都要画成球的样子(那 130ms 是两个窗口交叉淡化,
  // 画得越像越看不出换了个窗口),但时机相反:
  //   收 → 延迟 300ms 淡入(先缩小,小了才变成球)
  //   展 → 立刻挂上,再随着窗口长大淡出
  const toOrb = mode === 'orb';
  document.body.classList.toggle('is-orbing', toOrb);
  document.body.classList.toggle('is-unorbing', !toOrb && wasOrb);
  if (toOrb || wasOrb) driveBallFade(toOrb);
  clearTimeout(morphTimer);
  // 和后端的时长对齐:收起 110+340+130ms;展开 90+130+340ms
  morphTimer = setTimeout(() => {
    document.body.classList.remove('is-morphing');
    document.body.classList.remove('is-unorbing');
  }, toOrb ? 8000 : 580);
}

/* 展开/收起那个按钮的两副面孔。收成球的时候按 chat 算 —— 球里面的页面
   就是按 chat 排的,点球回来看到的也该是那一套。 */
function syncSizeBtn(mode) {
  const narrow = mode !== 'full';
  const btn = $('btnSize');
  if (!btn) return;
  const label = narrow ? '展开完整面板' : '收成对话框';
  $('btnSizeIcon').textContent = narrow ? '⤢' : '▭';
  $('btnSizeLabel').textContent = label;
  btn.title = label;
}

/* 球的样子淡入淡出:**按窗口宽度**,不按时间。

   页面就在窗口里面,`window.innerWidth` 就是窗口有多宽,每帧读一下即可 ——
   既不用后端推进度,也不用两边对时长。窗口宽到 orbSize 的四倍以上完全看不到球
   (那时候还是个面板,正在缩);窄到球本身那么大时完全是球,接着和真正的
   悬浮球交叉淡化,两张图一模一样。展开是同一条曲线反着走。 */
function driveBallFade(toOrb) {
  const d = Number(state.prefs.orbSize) || 36;
  // 球那个窗口比球本身大一圈(软阴影的余量,实测 11 CSS px 上下),
  // 所以收到底时窗口宽是 orbSize + 十几,不是 orbSize —— 阈值按 d*1.15 算
  // 末态只能到 0.95,交接那一下会差一点。留够余量。
  const lo = d + 14;                    // 窄到这个宽度 = 完全是球
  const hi = Math.max(220, d * 6);      // 宽过这个 = 完全看不到球
  const tick = () => {
    const body = document.body;
    if (!body.classList.contains('is-orbing')
        && !body.classList.contains('is-unorbing')) {
      body.style.removeProperty('--ball-a');
      return;
    }
    const w = window.innerWidth;
    const a = w <= lo ? 1 : w >= hi ? 0 : (hi - w) / (hi - lo);
    body.style.setProperty('--ball-a', a.toFixed(3));
    requestAnimationFrame(tick);
  };
  // 展开时窗口此刻还是球那么大,先把球画满再让它随尺寸淡出;
  // 收起时反过来,先确保是 0,别闪一下
  document.body.style.setProperty('--ball-a', toOrb ? '0' : '1');
  requestAnimationFrame(tick);
}

/* 大头针的按下状态。完整面板默认不置顶(它那么大,压着别人没法干活),
   钉住之后所有形态都置顶。 */
function setPin(on) {
  const b = $('btnPin');
  b.setAttribute('aria-pressed', String(!!on));
  b.classList.toggle('is-on', !!on);
  b.title = on ? '取消置顶' : '钉在最前面';
}

/* 页面一恢复可见就跟后端对一次形态。
   收成球时这个窗口是隐藏的,Chromium 会把文档标成 hidden;再显示出来时
   如果那条 SSE 事件恰好丢了(比如 EventSource 正在重连),就靠这里兜住。 */
/* SSE 那条 window 通道里的 memos 事件:别处改了备忘录,这边角标要跟上 */
function onMemoEvent(ev) {
  state.memoPending = ev.pending || 0;
  renderMemoBadge();
  // 刚才是自己改的:本地已经画过一遍了,别再拉一次重画(那就是闪屏)
  if (Date.now() - (state.memoSelfEdit || 0) < 2500) return;
  const panel = document.getElementById('panelMemo');
  if (panel && !panel.hidden) loadMemos();
}

function wireVisibilitySync() {
  document.addEventListener('visibilitychange', async () => {
    if (document.visibilityState !== 'visible') return;
    try {
      const st = await apiGet('/api/window/state');
      if (st && st.mode && st.mode !== state.mode) applyModeClass(st.mode);
      // 兜底:窗口都看得见了,内容不能还是淡掉的
      clearTimeout(morphTimer);
      document.body.classList.remove('is-morphing');
    } catch (e) { /* 对不上账就算了,下次再说 */ }
  });
}

function handleWindowEvent(ev) {
  if (ev.kind === 'sync') {
    renderSyncState(ev.sync || {});
    return;
  }
  if (ev.kind === 'mail') {
    renderMailState(ev.mail || {});
    // 有新邮件进来就把列表刷一下(在邮箱页的时候)
    if (state.page === 'mail' && (ev.mail || {}).added) loadMail();
    return;
  }
  if (ev.kind === 'memos') {
    onMemoEvent(ev);
    return;
  }
  if (ev.kind === 'peek') {
    setMemoPeek(!!ev.on);
    return;
  }
  if (ev.kind === 'prefs') {
    applyPrefs(ev.prefs);
    if (!$('prefsBackdrop').hidden) syncPrefsUI();
    return;
  }
  if (ev.kind === 'mode') {
    if (ev.mode === state.mode) return;
    applyModeClass(ev.mode);
    if (ev.mode === 'chat') switchTab('chat');
  }
}

/* ── 拖窗口 ──
   曾经想省事:让后端发一条 WM_NCLBUTTONDOWN + HTCAPTION,Windows 自己进移动
   循环。实测一下都拖不动 —— 那个循环起手要 SetCapture,而捕获此刻在 Chromium
   的渲染子窗口手里(另一个进程,ReleaseCapture 也管不着),抢不到就立刻退出。

   所以改成自己跟:pointerdown 记起点,pointermove 每帧发一条,后端自己
   GetCursorPos 算位移。坐标一个都不传 —— screenX 是 CSS 像素、窗口要物理
   像素,换算在多显示器 + 175% 缩放下很容易算错。 */

function wireDragRegions() {
  document.querySelectorAll('.drag-region').forEach((node) => {
    let dragging = false;
    let armed = false;      // 按下了但还没超过阈值
    let sx = 0;
    let sy = 0;
    let ticking = false;

    node.addEventListener('pointerdown', (e) => {
      if (e.button !== 0) return;
      armed = true;
      dragging = false;
      sx = e.clientX;
      sy = e.clientY;
      // 捕获指针:拖出这个元素之后 pointermove 仍然送到这里
      try { node.setPointerCapture(e.pointerId); } catch (err) { /* 老 WebView 没有 */ }
    });

    node.addEventListener('pointermove', (e) => {
      if (!armed) return;
      // 3px 的余量:双击时手抖一下不该把窗口挪走
      if (!dragging && Math.abs(e.clientX - sx) + Math.abs(e.clientY - sy) <= 3) return;
      if (!dragging) {
        dragging = true;
        apiPost('/api/window/drag/start').catch(() => {});
        return;
      }
      // 每帧最多一条请求,鼠标再快也不会把本地服务打满
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(() => {
        ticking = false;
        apiPost('/api/window/drag/move').catch(() => {});
      });
    });

    const stop = (e) => {
      if (!armed) return;
      armed = false;
      if (dragging) apiPost('/api/window/drag/end').catch(() => {});
      dragging = false;
      try { node.releasePointerCapture(e.pointerId); } catch (err) { /* 忽略 */ }
    };
    node.addEventListener('pointerup', stop);
    node.addEventListener('pointercancel', stop);
  });
}

/* 双击标题栏:没展开就展开,已经是完整面板就最大化 / 还原 */
function wireTitlebarDblClick() {
  $('tbDrag').addEventListener('dblclick', async () => {
    if (state.mode !== 'full') {
      await setMode('full');
      return;
    }
    try {
      await apiPost('/api/window/maximize');
    } catch (e) {
      reportError('maximize', (e && e.message) || String(e), '', 0, e && e.stack);
    }
  });
}

/* ═══════════════════════ 备忘录(第三列)═══════════════════════

   四种备忘:纯文字 / 某一天 / 每周 / 每月。**倒计时的数字是后端算的** ——
   "下一次什么时候""这是第几周期"必须有一处权威答案,前端只负责每秒重画。 */

async function loadMemos() {
  let d;
  try {
    d = await apiGet('/api/memos');
  } catch (e) {
    return;
  }
  state.memos = d.items || [];
  state.memoPending = d.pending || 0;
  renderMemos();
}

function renderMemoBadge() {
  const n = state.memoPending || 0;
  ['tabMemoN', 'mailTabMemoN'].forEach((id) => {
    const b = $(id);
    if (!b) return;
    b.textContent = n > 99 ? '99+' : String(n);
    b.hidden = !n;
  });
  const c = $('memoCount');
  if (c) c.textContent = n ? `${n} 条未完成` : '都完成了';
}

function renderMemos() {
  renderMemoBadge();
  const list = $('memoList');
  if (!list) return;
  list.textContent = '';
  if (!state.memos.length) {
    list.appendChild(el('div', 'empty',
      '还没有备忘。点「新增」,或者把左边的作业、公告、邮件拖到这儿。'));
    return;
  }
  state.memos.forEach((m) => list.appendChild(memoCard(m)));
  startMemoTick();
}

function memoCard(m) {
  const info = m.info || {};
  const soon = info.seconds !== undefined && info.seconds >= 0
    && info.seconds < 86400;
  const row = el('div', 'memo-item'
    + (m.done ? ' is-done' : info.overdue ? ' is-over' : soon ? ' is-soon' : '')
    + (info.when ? ' has-time' : ''));
  row.dataset.id = m.id;
  // 有时间的备忘:**时间当主角**,大号在上,文字在下。没时间的就只有文字
  if (info.when) {
    const w = el('div', 'memo-when');
    w.appendChild(el('span', 'mw-lead', info.overdue ? '已过' : '还剩'));
    w.appendChild(el('span', 'mw-big', info.when
      .replace(/^(还剩|已过)\s*/, '').replace(/\(此为第.*$/, '')));
    if (info.cycle) {
      w.appendChild(el('span', 'mw-cycle', `第 ${info.cycle} 周期`));
    }
    w.title = '到点时间:' + (info.at || '');
    row.appendChild(w);
  }
  row.appendChild(el('div', 'memo-text', m.text));
  if (m.link && m.link.title) {
    const meta = el('div', 'memo-meta',
      '来自' + ({ assignment: '作业', announcement: '公告', mail: '邮件',
        file: '课件' }[m.link.kind] || '') + ' · ' + m.link.title);
    row.appendChild(meta);
  }

  const act = el('div', 'memo-act');
  const done = el('button', 'link-btn', m.done ? '取消完成' : '完成');
  done.type = 'button';
  done.addEventListener('click', async () => {
    // 自己发的请求会让后端 push 一条 SSE 回来,那边也会重画 —— 打个标记,
    // 让它别再画一次。重复重画就是"点一下闪几下"的来源
    state.memoSelfEdit = Date.now();
    const r = await apiPost('/api/memos/update', { id: m.id, done: !m.done });
    state.memos = r.items || state.memos;
    state.memoPending = r.pending || 0;
    renderMemos();
  });
  act.appendChild(done);
  if (m.link && m.link.url) {
    const go = el('button', 'link-btn', '打开');
    go.type = 'button';
    go.addEventListener('click', () => openExternal(m.link.url));
    act.appendChild(go);
  }
  act.appendChild(el('span', 'memo-act-spacer'));
  const del = el('button', 'link-btn link-danger', '删');
  del.type = 'button';
  del.addEventListener('click', async () => {
    state.memoSelfEdit = Date.now();
    const r = await apiPost('/api/memos/delete', { id: m.id });
    state.memos = r.items || state.memos;
    state.memoPending = r.pending || 0;
    renderMemos();
  });
  act.appendChild(del);
  row.appendChild(act);
  return row;
}

/* 悬浮球停一秒弹出来的那一态。窗口的位置和尺寸是后端摆的,
   页面这边只负责"只显示备忘录"。 */
function setMemoPeek(on) {
  document.body.classList.toggle('is-memopeek', !!on);
  const p = $('panelMemo');
  if (!p) return;
  if (on) {
    // 浮窗里备忘录就是整扇窗:把面板搬到 body 底下,不受侧栏的布局管
    document.body.appendChild(p);
    p.hidden = false;
    loadMemos();
  } else {
    // 退出浮窗:面板搬回当前那个侧栏,显隐交回标签状态
    const pane = state.page === 'mail' ? $('paneMailSide') : $('paneSide');
    if (pane) pane.appendChild(p);
    const tab = state.page === 'mail' ? state.mailTab : state.tab;
    p.hidden = tab !== 'memo';
  }
}

/* 倒计时每 30 秒重画一次。只动那一行文字,不重建卡片 —— 重建会把动画重放,
   而且滚动位置会跳。 */
function startMemoTick() {
  if (state.memoTick) return;
  state.memoTick = setInterval(() => {
    const panel = $('panelMemo');
    const peek = document.body.classList.contains('is-memopeek');
    if ((!panel || panel.hidden) && !peek) return;
    if (!state.memos.some((m) => (m.info || {}).at)) return;
    loadMemos();
  }, 30000);
}

/* ── 新增表单。按类型只露出用得上的那几个控件 ── */

function syncMemoForm() {
  const k = $('memoKind').value;
  $('memoAt').hidden = k !== 'once';
  $('memoWeekday').hidden = k !== 'weekly';
  $('memoDay').hidden = k !== 'monthly';
  $('memoTime').hidden = k === 'plain' || k === 'once';
  $('memoFormHint').textContent = {
    plain: '只是一条文字',
    once: '到点只提醒这一次',
    weekly: '每周这一天',
    monthly: '每月这一号',
  }[k] || '';
}

function openMemoForm(on) {
  $('memoForm').hidden = !on;
  // 悬浮球浮窗那一态:表单开着就把窗口钉住。系统的日期选择器是另一个弹出
  // 窗口,指针会离开浮窗 —— 不钉住的话选个时间窗口就关了
  if (document.body.classList.contains('is-memopeek')) {
    apiPost('/api/window/peek/pin', { on: !!on }).catch(() => {});
  }
  if (on) {
    syncMemoForm();
    $('memoText').focus();
  }
}

async function submitMemo(e) {
  if (e) e.preventDefault();
  const text = $('memoText').value.trim();
  if (!text) {
    $('memoText').focus();
    return;
  }
  const kind = $('memoKind').value;
  const body = { text, kind };
  if (kind === 'once') {
    if (!$('memoAt').value) {
      $('memoFormHint').textContent = '得选个时间';
      return;
    }
    body.at = $('memoAt').value;
  }
  if (kind === 'weekly' || kind === 'monthly') {
    const [h, mi] = ($('memoTime').value || '09:00').split(':');
    body.hour = Number(h);
    body.minute = Number(mi);
    if (kind === 'weekly') body.weekday = Number($('memoWeekday').value);
    else body.day = Number($('memoDay').value);
  }
  try {
    const r = await apiPost('/api/memos', body);
    state.memos = r.items || [];
    state.memoPending = r.pending || 0;
    $('memoText').value = '';
    state.memoSelfEdit = Date.now();
    openMemoForm(false);            // 这里会顺手放开钉住
    renderMemos();
  } catch (err) {
    $('memoFormHint').textContent = '没记上:' + ((err && err.message) || err);
  }
}

/* ── 把作业 / 公告 / 邮件拖过来直接建一条 ── */

function wireMemo() {
  $('tabMemo').addEventListener('click', () => switchTab('memo'));
  $('btnMemoAdd').addEventListener('click',
    () => openMemoForm($('memoForm').hidden));
  $('btnMemoCancel').addEventListener('click', () => openMemoForm(false));
  $('memoForm').addEventListener('submit', submitMemo);
  $('memoKind').addEventListener('change', syncMemoForm);

  // 拖到**标签本身**上就能建一条 —— 不用先切过去。两个页面的标签都接
  const has = (e) => e.dataTransfer
    && Array.from(e.dataTransfer.types).includes('application/x-canvas-item');
  const zones = ['tabMemo', 'mailTabMemo', 'panelMemo']
    .map((id) => $(id)).filter(Boolean);
  const over = (e) => {
    if (!has(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
    e.currentTarget.classList.add('is-over');
  };
  const off = (e) => {
    (e && e.currentTarget ? [e.currentTarget] : zones)
      .forEach((z) => z.classList.remove('is-over'));
  };
  zones.forEach((z) => {
    z.addEventListener('dragover', over);
    z.addEventListener('dragenter', over);
    z.addEventListener('dragleave', off);
  });
  const onDrop = async (e) => {
    off();
    if (!has(e)) return;
    e.preventDefault();
    let item;
    try {
      item = JSON.parse(e.dataTransfer.getData('application/x-canvas-item'));
    } catch (err) {
      return;
    }
    try {
      state.memoSelfEdit = Date.now();
      const r = await apiPost('/api/memos/from-item', { item });
      state.memos = r.items || [];
      state.memoPending = r.pending || 0;
      // 建完直接切到备忘录那一栏,让人看见它真的记上了
      if (state.page === 'mail') switchMailTab('memo');
      else switchTab('memo');
      renderMemos();
    } catch (err) {
      reportError('memo', (err && err.message) || String(err), '', 0);
    }
  };
  zones.forEach((z) => z.addEventListener('drop', onDrop));
}

/* ─────────────────────────── 标签切换 ─────────────────────────── */

function switchTab(which) {
  const prev = state.tab;          // 必须先记下来:下一行就把它覆盖了
  state.tab = which;
  const on = (id, yes) => {
    $(id).classList.toggle('is-active', yes);
    $(id).setAttribute('aria-selected', String(yes));
  };
  if (which === 'brief' && prev !== 'brief') {
    // 回到简报那一栏:解钉 + 重拉,看到的就是最新那一份
    state.briefPinned = false;
    loadBriefIndex();
  }
  on('tabBrief', which === 'brief');
  on('tabChat', which === 'chat');
  on('tabMemo', which === 'memo');
  $('panelBrief').hidden = which !== 'brief';
  $('panelChat').hidden = which !== 'chat';
  showMemoPanel(which === 'memo', $('paneSide'));
  if (which === 'brief') markBriefUnread(false);
  else if (which === 'chat') $('chatInput').focus();
}

/* 备忘录面板只有一份 DOM,两个侧栏轮流用它 —— appendChild 是"搬",不是"拷",
   所以不会出现两份状态不同步的问题。 */
function showMemoPanel(on, pane) {
  const p = $('panelMemo');
  if (!p) return;
  if (on && pane && p.parentElement !== pane) pane.appendChild(p);
  p.hidden = !on;
  if (on) loadMemos();
}

/* ─────────────────────────── 主题 ─────────────────────────── */

/* 偏好存在后端,不用 localStorage:端口每次启动都是随机的,而 localStorage
   按 origin 隔离 —— 存在浏览器里的话主题和窗口形态一重启就丢。 */

async function loadPrefs() {
  try {
    return (await apiGet('/api/prefs')) || {};
  } catch (e) {
    return {};
  }
}

/* 偏好的写入统一走设置面板里的 savePrefs():它会先本地应用、再落盘、
   最后用后端返回的完整对象校正一次。 */

/* 把偏好落到界面上。只碰 CSS 变量和两个 data/class 开关 ——
   所有玻璃色都是从 --glass-a 算出来的,所以拖滑块能即时预览,不用重渲染。 */
// 后端返回的偏好总是「默认值 + 存档」的完整对象;这份兜底只在
// /api/prefs 请求失败时起作用,免得界面上出现 NaN 和 undefined
const PREF_FALLBACK = {
  theme: 'auto', mode: 'full', blur: 26, glass: 0.55, anim: true,
  orbSize: 36, briefHour: 9, topmost: true, showDismissed: true, opacity: 1,
  briefHistory: 3, upcomingDays: 28, soonDays: 3, annDays: 10,
  toastOn: true, toastSecs: 9,
  focusCourses: '', prefsTab: 'general',
  mailFacts: [], mailTags: [], mailAiOn: true, mailModel: 'sonnet',
  mailMarkRead: true,
  mailSort: 'date_desc',
};

function applyPrefs(prefs) {
  state.prefs = Object.assign({}, PREF_FALLBACK, prefs || {});
  const root = document.documentElement;
  const p = state.prefs;

  root.dataset.theme = p.theme === 'light' || p.theme === 'dark' ? p.theme : 'auto';
  root.style.setProperty('--glass-a', String(p.glass));
  root.style.setProperty('--blur', (p.blur || 0) + 'px');
  document.body.dataset.blur = Number(p.blur) > 0 ? 'on' : '0';
  document.body.classList.toggle('no-anim', p.anim === false);
  state.showDismissed = p.showDismissed !== false;
  if (p.mailSort) state.mailSort = p.mailSort;
  setPin(!!p.pinned);
}

/* ─────────────────────────── 设置面板 ───────────────────────────
   每一项都直接写进 data/prefs.json。滑块的 input 事件只做即时预览,
   change 事件才落盘 —— 拖一次滑块不该写几十遍文件。 */

function openPrefs(tab) {
  setPrefsTab(tab || state.prefsTab || state.prefs.prefsTab || 'general');
  syncPrefsUI();
  $('prefsBackdrop').hidden = false;
}

function closePrefs() {
  // 有没写完的 textarea 就趁现在落盘 —— 关面板时丢掉刚打的字是最气人的
  state.lazyFlush.forEach((f) => f());
  factCommits.forEach((f) => f());
  $('prefsBackdrop').hidden = true;
}

function syncPrefsUI() {
  const p = state.prefs;
  const theme = p.theme || 'auto';
  document.querySelectorAll('#segTheme .seg-btn').forEach((b) => {
    b.classList.toggle('is-on', b.dataset.v === theme);
    b.setAttribute('aria-checked', String(b.dataset.v === theme));
  });
  $('rngBlur').value = p.blur;
  $('outBlur').textContent = Number(p.blur) > 0 ? p.blur + ' px' : '关';
  $('rngGlass').value = Math.round(p.glass * 100);
  $('outGlass').textContent = Math.round(p.glass * 100) + '%';
  $('rngOpacity').value = Math.round(p.opacity * 100);
  $('outOpacity').textContent = Math.round(p.opacity * 100) === 100
    ? '不透明' : Math.round(p.opacity * 100) + '%';
  $('rngOrb').value = p.orbSize;
  $('outOrb').textContent = p.orbSize + ' px';
  $('numHour').value = p.briefHour;
  $('numMaxMB').value = p.maxSyncMB;
  $('numBriefHistory').value = p.briefHistory;
  $('numUpcoming').value = p.upcomingDays;
  $('numSoon').value = p.soonDays;
  $('numAnnDays').value = p.annDays;
  // textarea 只在没焦点的时候回填 —— 正在打字时被覆盖会丢输入
  setTextIfIdle('txtFocus', p.focusCourses);
  setSwitch('swMailAi', p.mailAiOn !== false);
  setSwitch('swMarkRead', p.mailMarkRead !== false);
  $('selMailModel').value = p.mailModel || 'sonnet';
  $('mailSort').value = p.mailSort || 'date_desc';
  renderFacts();
  renderTagCatalog();
  renderPeople();
  renderAiState();
  const f = state.mailFill;
  if ($('fillState')) {
    $('fillState').textContent = !f ? ''
      : f.running ? `取正文中 ${f.done}/${f.total}`
        : f.pending ? `还有 ${f.pending} 封没取` : '都取过了';
  }
  setSwitch('swAnim', p.anim !== false);
  setSwitch('swToast', p.toastOn !== false);
  $('numToastSecs').value = p.toastSecs;
  setSwitch('swDismissed', p.showDismissed !== false);
  setSwitch('swTopmost', p.topmost !== false);
  setSwitch('swSync', p.autoSync !== false);
  setSwitch('swMail', p.mailOn !== false);
  $('numMailHour').value = p.mailHour;
  const nWatch = (state.mail && state.mail.watching) || state.watching.length || 0;
  const wb = $('prefsMailBadge');
  if (nWatch > 0) {
    wb.textContent = String(nWatch);
    wb.hidden = false;
  } else {
    wb.hidden = true;
  }
  const st = state.sync;
  $('syncState').textContent = !st
    ? ''
    : st.running
      ? `同步中 ${st.done}/${st.total}`
      : (st.last ? `上次 ${st.last} · 新增 ${st.added} 更新 ${st.updated}` : '还没同步过');
  $('dismissCount').textContent = state.dismissedCount
    ? '当前划掉了 ' + state.dismissedCount + ' 项'
    : '当前没有划掉的条目';
}

/* ── 我的情况:一行一条键值对 ──

   刻意做成结构化的两列而不是一大段自述:填一行就多一条判断依据,
   写起来没负担;分析 prompt 里也是一条一行,模型不会把几件事揉成一团。 */

let factCommits = [];    // 「还没落盘的那一笔」,每次重绘重置

/* 从 DOM 现读整个列表。有这个函数就不需要"第 i 行"这种说法了 ——
   下标一旦和 DOM 错位(比如删了一行却没重绘),改一行会写到别一行上。 */
function collectFacts() {
  return Array.from(document.querySelectorAll('#factList .fact-row')).map((r) => ({
    k: (r.querySelector('.fact-k') || {}).value || '',
    v: (r.querySelector('.fact-v') || {}).value || '',
  }));
}

/* force = 用户自己点的增删,必须重绘。
   不带 force 的是顺带发生的重绘(SSE 推回来 / syncPrefsUI),那种情况下
   焦点在里面就别动 DOM —— 会把光标顶走。 */
function renderFacts(force) {
  const box = $('factList');
  if (!box) return;
  const facts = state.prefs.mailFacts || [];
  if (!force && box.contains(document.activeElement)) return;
  factCommits = [];
  box.textContent = '';
  if (!facts.length) {
    box.appendChild(el('div', 'pref-hint', '一条都没填。点「加一条」开始。'));
    return;
  }
  facts.forEach((f) => {
    const row = el('div', 'fact-row');
    const k = el('input', 'num fact-k');
    k.type = 'text';
    k.value = f.k || '';
    k.placeholder = '名目';
    const v = el('input', 'num num-wide fact-v');
    v.type = 'text';
    v.value = f.v || '';
    v.placeholder = '内容';
    // 打字防抖落盘 + 失焦立刻落。不只听 change —— 那个事件什么时候来
    // 取决于浏览器和输入法,赌它等于把用户刚打的字丢掉
    let t = 0;
    const commit = () => {
      clearTimeout(t);
      t = 0;
      saveFacts(collectFacts(), true);
    };
    [k, v].forEach((inp) => {
      inp.addEventListener('input', () => {
        clearTimeout(t);
        t = setTimeout(commit, 800);
      });
      inp.addEventListener('change', commit);
      inp.addEventListener('blur', commit);
    });
    factCommits.push(() => { if (t) commit(); });
    const x = el('button', 'icon-btn fact-x', '✕');
    x.type = 'button';
    x.title = '删掉这一条';
    x.addEventListener('click', async () => {
      clearTimeout(t);          // 这一行待落盘的那笔作废,别让它把自己写回来
      t = 0;
      row.remove();             // 先把这一行从 DOM 上摘掉
      x.blur();                 // 焦点别留在已经不存在的按钮上
      await saveFacts(collectFacts(), true);
      renderFacts(true);        // 强制重绘:空状态、后面几行的闭包都要重建
    });
    row.appendChild(k);
    row.appendChild(v);
    row.appendChild(x);
    box.appendChild(row);
  });
}

/* quiet = 只落盘,不重绘(正在里面打字,重绘会把光标顶走) */
async function saveFacts(facts, quiet, force) {
  state.prefs = Object.assign({}, state.prefs, { mailFacts: facts });
  try {
    await apiPost('/api/prefs', { mailFacts: facts });
  } catch (e) {
    reportError('prefs', (e && e.message) || String(e), '', 0, e && e.stack);
  }
  // quiet = 正在里面打字,重绘会把光标顶走
  if (!quiet) renderFacts(force);
}

/* ── 标签目录 ── */

function renderTagCatalog() {
  const box = $('tagCatalog');
  const bad = $('tagBad');
  if (!box || !bad) return;
  const tags = state.prefs.mailTags || [];
  const trash = state.mailTrashTags || [];
  const counts = state.mailTagCounts || {};
  box.textContent = '';
  bad.textContent = '';
  if (!tags.length) {
    box.appendChild(el('div', 'drop-empty', '目录是空的 —— 分析时它会自己造标签。'));
  }
  tags.forEach((t) => {
    const chip = tagChip(t, counts[t], trash.includes(t));
    (trash.includes(t) ? bad : box).appendChild(chip);
  });
  if (!bad.children.length) {
    bad.appendChild(el('div', 'drop-empty', '把标签拖到这儿'));
  }
  wireTagDrop(box, false);
  wireTagDrop(bad, true);
}

function tagChip(t, n, isBad) {
  const chip = el('span', 'tag-chip tag-cat' + (isBad ? ' is-trash' : ''));
  chip.draggable = true;
  chip.dataset.tag = t;
  chip.appendChild(el('span', null, t));
  if (n) chip.appendChild(el('span', 'tag-n', String(n)));
  chip.addEventListener('dragstart', (e) => {
    e.dataTransfer.setData('application/x-mail-tag', t);
    e.dataTransfer.setData('text/plain', t);
    e.dataTransfer.effectAllowed = 'move';
  });
  const x = el('button', 'tag-x', '✕');
  x.type = 'button';
  x.title = `把「${t}」从目录里删掉(已经打过这个标签的邮件不受影响)`;
  x.addEventListener('click', async (ev) => {
    ev.stopPropagation();
    const r = await apiPost('/api/mail/tags', { drop: t });
    applyPrefs(Object.assign({}, state.prefs, { mailTags: r.tags }));
    renderTagCatalog();
  });
  chip.appendChild(x);
  return chip;
}

/* 两个框都是投放区。往「坏标签」里拖 = 设成坏标签,拖回去 = 收回来。 */
function wireTagDrop(zone, toBad) {
  if (zone.dataset.wired) return;
  zone.dataset.wired = '1';
  const has = (e) => e.dataTransfer
    && Array.from(e.dataTransfer.types).includes('application/x-mail-tag');
  zone.addEventListener('dragover', (e) => {
    if (!has(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    zone.classList.add('is-over');
  });
  zone.addEventListener('dragleave', () => zone.classList.remove('is-over'));
  zone.addEventListener('drop', async (e) => {
    zone.classList.remove('is-over');
    if (!has(e)) return;
    e.preventDefault();
    const t = e.dataTransfer.getData('application/x-mail-tag');
    if (!t) return;
    const already = (state.mailTrashTags || []).includes(t);
    if (already === toBad) return;          // 拖回原处,什么都不用做
    const r = await apiPost('/api/mail/trashtag', { tag: t, on: toBad });
    state.mailTrashTags = r.trash_tags || [];
    state.mailBoxes = r.boxes || state.mailBoxes;
    renderTagCatalog();
    if (state.page === 'mail') loadMail();
  });
}

/* 设置里的分组管理:每组一行,后面挂着这一组的人 */
async function renderPeople() {
  const box = $('peopleGroups');
  if (!box) return;
  let d;
  try {
    d = await apiGet('/api/mail/people');
  } catch (e) {
    return;
  }
  state.mailGroups = d.groups || [];
  box.textContent = '';
  (d.groups || []).forEach((g) => {
    const row = el('div', 'pg-row');
    row.appendChild(el('span', 'pg-name', g));
    const mem = (d.members || []).filter((m) => m.group === g);
    if (!mem.length) {
      row.appendChild(el('span', 'muted', '还没有人'));
    }
    mem.forEach((m) => {
      const chip = el('span', 'pg-member');
      chip.appendChild(el('span', null, m.addr));
      chip.title = m.name || m.addr;
      const x = el('button', 'pg-x', '✕');
      x.type = 'button';
      x.title = '把这个人移出分组';
      x.addEventListener('click', async () => {
        await apiPost('/api/mail/person', { addr: m.addr, group: '' });
        renderPeople();
        if (state.page === 'mail') loadMail();
      });
      chip.appendChild(x);
      row.appendChild(chip);
    });
    const del = el('button', 'pg-del', '删掉这一组');
    del.type = 'button';
    del.addEventListener('click', async () => {
      if (mem.length && !window.confirm(
        `删掉「${g}」,里面 ${mem.length} 个人会变成未分组。继续?`)) return;
      await apiPost('/api/mail/groups', { drop: g });
      renderPeople();
      if (state.page === 'mail') loadMail();
    });
    row.appendChild(del);
    box.appendChild(row);
  });
}

async function addGroup() {
  const box = $('groupNew');
  const v = box.value.trim();
  if (!v) return;
  await apiPost('/api/mail/groups', { add: v });
  box.value = '';
  renderPeople();
}

async function addTag() {
  const box = $('tagNew');
  const v = box.value.trim();
  if (!v) return;
  const r = await apiPost('/api/mail/tags', { add: v });
  box.value = '';
  applyPrefs(Object.assign({}, state.prefs, { mailTags: r.tags }));
  renderTagCatalog();
}

/* ── 催一轮过目 ── */

/* 只补链接挑选。**和「全部重判」分开**:那个会把标签、级别、摘要一起
   重付一次钱并推翻,而这里要补的只是"哪几条链接值得点"。 */
async function runRelink() {
  if (!window.confirm(
    '给「带链接但还没挑过」的邮件补一次链接挑选。\n'
    + '这会调一次模型,要花钱(通常几毛)。标签和摘要不受影响。\n\n继续吗?')) return;
  $('aiState').textContent = '正在重挑链接…';
  try {
    const r = await apiPost('/api/mail/analyze', { relink: true });
    if (!r.dropped) {
      $('aiState').textContent = '没有需要补的 —— 带链接的信都挑过了';
      return;
    }
    renderAiState(r.ai);
  } catch (e) {
    $('aiState').textContent = '没跑起来:' + ((e && e.message) || e);
  }
}

async function runAnalyze(redo) {
  if (redo && !window.confirm(
    '把所有邮件的标签和级别全部作废,按现在的个人信息重新判一遍。\n'
    + '这要重新花一次钱(上次 83 封约 $0.33)。继续?')) return;
  $('aiState').textContent = redo ? '正在重判…' : '开始过目…';
  try {
    const r = await apiPost('/api/mail/analyze', { redo: !!redo });
    state.mailAi = r.ai || null;
    renderAiState();
    if (!r.started) $('aiState').textContent = '没有需要过目的了';
  } catch (e) {
    $('aiState').textContent = '出错:' + e.message;
  }
}

function renderAiState() {
  const box = $('aiState');
  if (!box) return;
  const a = state.mailAi;
  if (!a) {
    box.textContent = '';
    return;
  }
  box.textContent = a.running
    ? `过目中 ${a.done}/${a.total} · $${a.cost || 0}`
    : `已过目 ${a.analyzed} 封` + (a.pending ? ` · 还差 ${a.pending}` : '')
      + (a.cost ? ` · 本轮 $${a.cost}` : '');
}

function setTextIfIdle(id, v) {
  const box = $(id);
  if (box && document.activeElement !== box) box.value = v || '';
}

function setSwitch(id, on) {
  $(id).setAttribute('aria-checked', String(!!on));
}

/* 开关接线。模块作用域,因为 wirePrefs 和 wireMailPrefs 都要用 */
function toggle(id, key, after) {
  $(id).addEventListener('click', () => {
    const next = $(id).getAttribute('aria-checked') !== 'true';
    const patch = {};
    patch[key] = next;
    savePrefs(patch);
    if (after) after(next);
  });
}

/* 多行文本框:打字不落盘,停手 1.2 秒或者失焦才写。
   写盘本身很便宜,但每次 savePrefs 都会 applyPrefs + syncPrefsUI 重刷面板,
   正在打字的时候那是会打断输入法的。 */
function lazyText(id, key, after) {
  const box = $(id);
  if (!box) return;
  let t = 0;
  const flush = () => {
    clearTimeout(t);
    t = 0;
    const v = box.value;
    if (v === (state.prefs[key] || '')) return;
    const patch = {};
    patch[key] = v;
    state.prefs = Object.assign({}, state.prefs, patch);   // 先本地生效,免得回写把光标顶走
    apiPost('/api/prefs', patch)
      .then(() => { if (after) after(); })
      .catch((e) => reportError('prefs', (e && e.message) || String(e), '', 0, e && e.stack));
  };
  box.addEventListener('input', () => {
    clearTimeout(t);
    t = setTimeout(flush, 1200);
  });
  box.addEventListener('blur', flush);
  state.lazyFlush.push(flush);
}

/* ── 三栏 */

function setPrefsTab(tab) {
  const ok = ['general', 'canvas', 'mail'].includes(tab) ? tab : 'general';
  state.prefsTab = ok;
  document.querySelectorAll('#prefsSeg .seg-btn').forEach((b) => {
    b.classList.toggle('is-on', b.dataset.v === ok);
    b.setAttribute('aria-selected', String(b.dataset.v === ok));
  });
  document.querySelectorAll('.prefs-tab').forEach((pane) => {
    pane.hidden = pane.dataset.tab !== ok;
  });
  if (ok === 'mail') {
    refreshMailPrefs();
    loadWatching();
  }
}

/* 还在盯的邮件。这一栏是"标了重点就一直提醒到完成"在设置里的那一面 ——
   让你随时看得到自己压了几件事、压了多久。 */
async function loadWatching() {
  const box = $('watchList');
  if (!box) return;
  let d;
  try {
    d = await apiGet('/api/mail/watching');
  } catch (e) {
    return;
  }
  state.watching = d.watching || [];
  box.textContent = '';
  if (!state.watching.length) {
    box.appendChild(el('div', 'watch-empty', '现在一封都没盯着。'));
    return;
  }
  state.watching.forEach((w) => {
    const m = w.meta || {};
    const d0 = w.days;
    const row = el('div', 'watch-item' + (d0 >= 3 ? ' is-stale' : ''));
    const main = el('div', 'watch-main');
    main.appendChild(el('div', 'watch-subj', m.subject || '(无主题)'));
    main.appendChild(el('div', 'watch-sub',
      [m.from, m.date_local, w.note].filter(Boolean).join(' · ')));
    row.appendChild(main);
    row.appendChild(el('span', 'watch-age',
      d0 === null || d0 === undefined ? '刚标的' : d0 + ' 天'));
    const done = el('button', 'link-btn', '完成');
    done.type = 'button';
    done.addEventListener('click', async () => {
      await apiPost('/api/mail/flag', { id: w.id, done: true });
      loadWatching();
      if (state.page === 'mail') loadMail();
    });
    row.appendChild(done);
    box.appendChild(row);
  });
}

async function savePrefs(patch) {
  applyPrefs(Object.assign({}, state.prefs, patch));
  syncPrefsUI();
  try {
    const r = await apiPost('/api/prefs', patch);
    if (r && r.prefs) applyPrefs(r.prefs);
  } catch (e) {
    reportError('prefs', (e && e.message) || String(e), '', 0, e && e.stack);
  }
}

function wirePrefs() {
  $('btnSettings').addEventListener('click', openPrefs);
  $('btnClosePrefs').addEventListener('click', closePrefs);
  $('prefsBackdrop').addEventListener('click', (e) => {
    if (e.target === $('prefsBackdrop')) closePrefs();
  });

  document.querySelectorAll('#prefsSeg .seg-btn').forEach((b) => {
    b.addEventListener('click', () => {
      setPrefsTab(b.dataset.v);
      savePrefs({ prefsTab: b.dataset.v });
    });
  });

  document.querySelectorAll('#segTheme .seg-btn').forEach((b) => {
    b.addEventListener('click', () => savePrefs({ theme: b.dataset.v }));
  });

  // 玻璃两根滑块:拖的时候即时预览(只改 CSS 变量),松手才写文件
  const live = (id, key, conv) => {
    $(id).addEventListener('input', (e) => {
      const patch = {};
      patch[key] = conv(e.target.value);
      applyPrefs(Object.assign({}, state.prefs, patch));
      syncPrefsUI();
    });
    $(id).addEventListener('change', (e) => {
      const patch = {};
      patch[key] = conv(e.target.value);
      savePrefs(patch);
    });
  };
  live('rngBlur', 'blur', (v) => Number(v));
  live('rngGlass', 'glass', (v) => Number(v) / 100);

  // 整窗透明度是 DWM 干的活,页面这边没法预览,拖的时候只更新读数;
  // 松手才发给后端(每一次 SetLayeredWindowAttributes 都是一次整窗重合成)
  $('rngOpacity').addEventListener('input', (e) => {
    const v = Number(e.target.value);
    $('outOpacity').textContent = v === 100 ? '不透明' : v + '%';
  });
  $('rngOpacity').addEventListener('change', (e) => {
    savePrefs({ opacity: Number(e.target.value) / 100 });
  });

  // 球是后端画的,拖动中没法预览,松手再重画
  $('rngOrb').addEventListener('input', (e) => {
    $('outOrb').textContent = e.target.value + ' px';
  });
  $('rngOrb').addEventListener('change', (e) => savePrefs({ orbSize: Number(e.target.value) }));

  $('numMaxMB').addEventListener('change', (e) => {
    let v = parseInt(e.target.value, 10);
    if (isNaN(v)) v = 80;
    savePrefs({ maxSyncMB: Math.max(1, Math.min(2000, v)) });
  });
  $('numHour').addEventListener('change', (e) => {
    let h = parseInt(e.target.value, 10);
    if (isNaN(h)) h = 9;
    savePrefs({ briefHour: Math.max(0, Math.min(23, h)) });
  });

  // 口径这几项改完要重拉仪表盘:紧急阈值和展望天数直接决定卡片上的状态
  const clampNum = (id, key, lo, hi, dflt, reload) => {
    $(id).addEventListener('change', (e) => {
      let v = parseInt(e.target.value, 10);
      if (isNaN(v)) v = dflt;
      savePrefs({ [key]: Math.max(lo, Math.min(hi, v)) });
      if (reload) loadDashboard(true);
    });
  };
  clampNum('numBriefHistory', 'briefHistory', 0, 7, 3);
  clampNum('numUpcoming', 'upcomingDays', 7, 120, 28, true);
  clampNum('numSoon', 'soonDays', 1, 14, 3, true);
  clampNum('numAnnDays', 'annDays', 3, 60, 10, true);
  lazyText('txtFocus', 'focusCourses');

  toggle('swAnim', 'anim');
  toggle('swToast', 'toastOn');
  clampNum('numToastSecs', 'toastSecs', 3, 60, 9);
  $('btnToastTest').addEventListener('click', () => {
    apiPost('/api/toast/test', {}).catch(() => {});
  });
  toggle('swDismissed', 'showDismissed', () => {
    if (state.data && !state.data.error) renderTodo(state.data.todo);
  });
  toggle('swSync', 'autoSync');
  toggle('swMail', 'mailOn');
  $('numMailHour').addEventListener('change', (e) => {
    let h = parseInt(e.target.value, 10);
    if (isNaN(h)) h = 9;
    savePrefs({ mailHour: Math.max(0, Math.min(23, h)) });
  });
  $('btnSyncNow').addEventListener('click', async () => {
    try {
      const r = await apiPost('/api/sync', {});
      if (r && r.state) renderSyncState(r.state);
      syncPrefsUI();
    } catch (e) {
      reportError('sync', (e && e.message) || String(e), '', 0, e && e.stack);
    }
  });
  $('btnOpenDownloads').addEventListener('click', () => {
    apiPost('/api/reveal', { what: 'downloads' }).catch(() => {});
  });

  toggle('swTopmost', 'topmost', () => {
    if (state.mode === 'chat') setMode('chat');   // 立刻生效
  });

  $('btnResetOrbPos').addEventListener('click', () => savePrefs({ orbX: null, orbY: null }));
  $('btnCollapseNow').addEventListener('click', () => {
    closePrefs();
    setMode('orb');
  });
  $('btnGenNow').addEventListener('click', async () => {
    closePrefs();
    switchTab('brief');
    const r = await apiPost('/api/briefings/generate', { force: true });
    if (!r.started) setBriefStatus(r.reason);
  });
  $('btnRestoreAll').addEventListener('click', async () => {
    await apiPost('/api/dismiss/clear');
    await loadDashboard(true);
    syncPrefsUI();
  });
  $('btnOpenData').addEventListener('click', () => {
    apiPost('/api/reveal', { what: 'data' }).catch(() => {});
  });
  $('btnOpenStartup').addEventListener('click', () => {
    apiPost('/api/reveal', { what: 'startup' }).catch(() => {});
  });
}

/* ─────────────────────────── 启动 ─────────────────────────── */

function wireEvents() {
  $('btnRefresh').addEventListener('click', () => loadDashboard(true));

  $('btnToggleDone').addEventListener('click', () => {
    state.showDone = !state.showDone;
    if (state.data && !state.data.error) renderTodo(state.data.todo);
  });

  $('composer').addEventListener('submit', (e) => {
    e.preventDefault();
    sendChat($('chatInput').value);
  });

  $('chatInput').addEventListener('input', autoGrow);
  $('chatInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendChat($('chatInput').value);
    }
  });

  wireDragRegions();
  wireTitlebarDblClick();
  wirePrefs();
  wireVisibilitySync();
  wireChatHistory();
  wireDropZone();
  wireCourseView();
  wireMail();
  wireMailPrefs();
  wireMemo();
  $('btnPin').addEventListener('click', async () => {
    const next = $('btnPin').getAttribute('aria-pressed') !== 'true';
    setPin(next);
    try {
      await apiPost('/api/window/pin', { on: next });
    } catch (e) {
      setPin(!next);
      reportError('pin', (e && e.message) || String(e), '', 0, e && e.stack);
    }
  });

  // 一个按钮,两个方向:完整面板时收成对话框,对话框时展开回完整面板
  $('btnSize').addEventListener('click', () => {
    setMode(document.body.dataset.mode === 'chat' ? 'full' : 'chat');
  });
  $('btnToOrb').addEventListener('click', () => setMode('orb'));
  $('btnMin').addEventListener('click', () => {
    apiPost('/api/window/minimize').catch(() => {});
  });
  $('btnClose').addEventListener('click', () => {
    apiPost('/api/window/close').catch(() => {});
  });

  $('tabBrief').addEventListener('click', () => switchTab('brief'));
  $('tabChat').addEventListener('click', () => switchTab('chat'));

  $('briefDate').addEventListener('change', (e) => {
    // 自己翻了日期才算"钉住":之后的刷新保留这个选择。
    // 切走再切回来会重置成最新那一份(见 loadBriefIndex)
    state.briefPinned = true;
    showBrief(e.target.value);
  });

  $('btnGenBrief').addEventListener('click', async () => {
    if (state.briefGenerating) return;
    // force:今天已经有简报了也重新生成一份(覆盖存档)
    const already = state.briefIndex.some((x) => x.date === state.briefToday);
    const r = await apiPost('/api/briefings/generate', { force: already });
    if (!r.started) setBriefStatus(r.reason);
  });

  $('btnCloseSheet').addEventListener('click', closeSheet);
  $('sheetBackdrop').addEventListener('click', (e) => {
    if (e.target === $('sheetBackdrop')) closeSheet();
  });
  document.addEventListener('keydown', (e) => {
    // Ctrl+, 是各家软件通用的"打开设置"
    if (e.ctrlKey && e.key === ',') {
      e.preventDefault();
      if ($('prefsBackdrop').hidden) openPrefs(); else closePrefs();
      return;
    }
    if (e.key !== 'Escape') return;
    if (!$('sheetBackdrop').hidden) closeSheet();
    else if (!$('prefsBackdrop').hidden) closePrefs();
    else if (state.mailOne) closeMailOne();
  });

  $('btnSheetAsk').addEventListener('click', () => {
    const a = state.activeAssignment;
    if (!a) return;
    closeSheet();
    sendChat(`讲一下 ${a.course_short} 的「${a.name}」:要求是什么、该怎么下手、有什么坑。`);
  });

  $('btnSheetOpen').addEventListener('click', () => {
    const a = state.activeAssignment;
    if (a) openExternal(a.url);
  });
}

async function boot() {
  const prefs = await loadPrefs();
  applyPrefs(prefs);
  connectStream();
  wireEvents();
  renderSuggests();
  switchTab('brief');
  // 形态优先级:URL 参数(排障用) > 上次记住的 > full
  let start = new URLSearchParams(location.search).get('mode');
  if (!['orb', 'chat', 'full'].includes(start)) start = prefs.mode;
  if (!['orb', 'chat', 'full'].includes(start)) start = 'full';
  await loadDashboard(false);
  // 对话历史:启动就把上次那段读回来,不是空白开始
  await loadChatIndex();
  if (state.chatId) await openChat(state.chatId);
  // 简报由后端调度(每天 09:00,一天一次),前端只负责取来展示。
  // 如果此刻后端正在生成,SSE 的 delta 会直接把内容画进来。
  await loadBriefIndex();
  // 备忘录:角标要有数,展开过的话列表也一起拉回来
  await loadMemos();
  // 上次在哪个子页面就回哪个
  setPage(new URLSearchParams(location.search).get('page') || prefs.page || 'study');
  // 排障:URL 上带 &panel=prefs 就直接把设置面板打开(env CANVAS_HELPER_PANEL)
  const q = new URLSearchParams(location.search);
  if (q.get('panel') === 'prefs') openPrefs();
  // &course=<id>[&seg=hw|mat|ann] 启动就切到课程单页,方便直接看这一屏
  if (q.get('course')) {
    state.courseSeg = ['hw', 'mat', 'ann'].includes(q.get('seg')) ? q.get('seg') : 'hw';
    openCourse(Number(q.get('course')));
  }
  // 放在最后:数据都就位了再决定以哪个形态出现,避免收起来时还在加载
  await setMode(start);
}

/* 前端报错必须捞回后端 —— 开机是 pythonw 启动的,没有控制台也没有 devtools,
   不上报的话 boot() 中途抛异常会表现成「界面莫名停在某个状态」。 */
function reportError(kind, message, source, line, stack) {
  try {
    fetch(apiUrl('/api/clientlog'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind, message, source, line, stack }),
    }).catch(() => {});
  } catch (e) { /* 连上报都失败就算了 */ }
}

window.addEventListener('error', (e) => {
  reportError('error', e.message, e.filename, e.lineno, e.error && e.error.stack);
});
window.addEventListener('unhandledrejection', (e) => {
  const r = e.reason || {};
  reportError('unhandledrejection', r.message || String(r), '', 0, r.stack);
});

// 前端是普通网页了,DOM 就绪即可启动
function safeBoot() {
  boot().catch((err) => {
    reportError('boot', err && err.message, '', 0, err && err.stack);
  });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', safeBoot);
} else {
  safeBoot();
}
