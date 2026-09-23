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
  week: null,          // 课表数据(/api/schedule 的返回)
  weekStart: '',       // 当前显示这一周的周日 YYYY-MM-DD
  weekEdit: null,      // 正在改的那一条(null = 在加新的)
  weekTick: 0,         // 「现在」那条线的定时器
  sched: null,         // 课表解析的进度
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
  briefRaw: '',        // 当前这份简报的 markdown 原文(「复制」按钮用)
  mailBriefRaw: '',
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

/* ─────────────── 复制到剪贴板 ───────────────
   页面是 http://127.0.0.1 起的,算「安全上下文」,所以 navigator.clipboard
   在 WebView2 里是能用的。但它要求文档有焦点 —— 窗口刚从悬浮球展开、或者
   焦点在原生控件上的时候会被拒。所以留一条老路(隐藏 textarea +
   execCommand)兜底,两条都不成才报"复制不了"。 */
async function copyText(text) {
  text = String(text == null ? '' : text);
  if (!text) return false;
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (e) { /* 没焦点或者没权限,走下面那条 */ }
  try {
    // 这条路要靠"选中再 copy",而那会把用户**页面上**的选区顶掉 ——
    // 按了个 Ctrl+C 结果高亮没了,看着像出了错。先记下来,完事放回去
    const sel = window.getSelection();
    const saved = [];
    for (let i = 0; i < sel.rangeCount; i += 1) saved.push(sel.getRangeAt(i));
    const back = document.activeElement;

    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    // 不能用 display:none —— 选不中就复制不了。挪到屏幕外
    ta.style.cssText = 'position:fixed;top:-1000px;left:0;opacity:0';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    ta.remove();

    sel.removeAllRanges();
    saved.forEach((r) => sel.addRange(r));
    if (back && back.focus) { try { back.focus(); } catch (err) { /* 没了就算 */ } }
    return ok;
  } catch (e) {
    return false;
  }
}

/* 按钮文字临时换成一句反馈,1.4 秒后换回来。
   不弹 toast:对话里一条一条复制的时候,满屏飘提示很吵。 */
function flashBtn(btn, label) {
  if (!btn) return;
  if (btn.dataset.label === undefined) btn.dataset.label = btn.textContent;
  btn.textContent = label;
  btn.classList.add('is-done');
  clearTimeout(btn._flash);
  btn._flash = setTimeout(() => {
    btn.textContent = btn.dataset.label;
    btn.classList.remove('is-done');
  }, 1400);
}

async function copyInto(btn, text) {
  if (!String(text || '').trim()) { flashBtn(btn, '这儿是空的'); return; }
  flashBtn(btn, (await copyText(text)) ? '已复制' : '复制不了');
}

/* 屏幕下方那个一闪而过的小提示。按钮上能改字的场合用 flashBtn,
   右键菜单这种点完就消失的场合没有按钮可改,用它。 */
let hintTimer = 0;

function hint(text) {
  let box = $('hintPill');
  if (!box) {
    box = el('div', 'hint-pill');
    box.id = 'hintPill';
    document.body.appendChild(box);
  }
  box.textContent = text;
  box.classList.add('is-on');
  clearTimeout(hintTimer);
  hintTimer = setTimeout(() => box.classList.remove('is-on'), 1600);
}

/* ─────────────── 选中一段就能复制 ───────────────

   **为什么这些要自己做。** pywebview 起 WebView2 的时候,把
   `AreDefaultContextMenusEnabled` 和 `AreBrowserAcceleratorKeysEnabled`
   一起绑在 debug 上,而我们是 debug=False —— 于是**右键根本不出菜单**,
   选中一段话之后没有任何"复制"的入口。按 WebView2 的文档,Ctrl+C/V/X/A
   这类编辑快捷键不在被关掉的范围里,但不能只押在这一条上。

   所以这里补两样:一个自己画的右键菜单(中文、跟着应用的玻璃样式),
   和一条 Ctrl/Cmd+C 的兜底。 */

// 认得出"这是一块正文"的地方 —— 右键在这些里面才给菜单,
// 在按钮和卡片上乱弹一个只有灰项的菜单比不弹更烦
const TEXT_ZONES = '.chat-log, .brief-body, .mail-one, .sheet-body';

/* 当前选中的文字。**输入框里的选区不在 document selection 里**
   (Chromium 就是这么定的),得单独问它自己。 */
function selText() {
  const a = document.activeElement;
  try {
    if (a && (a.tagName === 'TEXTAREA' || a.tagName === 'INPUT')
        && typeof a.selectionStart === 'number'
        && a.selectionStart !== a.selectionEnd) {
      return String(a.value).slice(a.selectionStart, a.selectionEnd);
    }
  } catch (e) { /* type=time 之类没有 selectionStart,问了会抛 */ }
  return String(window.getSelection() || '');
}

function selectNode(node) {
  const r = document.createRange();
  r.selectNodeContents(node);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(r);
}

async function menuCopy(text) {
  if (!String(text || '')) return;
  hint((await copyText(text)) ? '已复制' : '复制不了');
}

async function cutIn(node) {
  const a = node.selectionStart;
  const b = node.selectionEnd;
  const txt = String(node.value).slice(a, b);
  if (!txt || !(await copyText(txt))) { hint('剪不动'); return; }
  node.setRangeText('', a, b, 'end');
  // 输入框靠 input 事件长高(autoGrowEl),不补一条它就不会收回去
  node.dispatchEvent(new Event('input', { bubbles: true }));
  hint('已剪切');
}

async function pasteIn(node) {
  let txt = '';
  try {
    txt = await navigator.clipboard.readText();
  } catch (e) {
    // 读剪贴板要的权限比写严,被拒是正常的 —— 原生的 Ctrl+V 仍然好使
    hint('读不到剪贴板,用 Ctrl+V');
    return;
  }
  if (!txt) { hint('剪贴板是空的'); return; }
  const a = node.selectionStart;
  const b = node.selectionEnd;
  node.setRangeText(txt, a, b, 'end');
  node.dispatchEvent(new Event('input', { bubbles: true }));
}

let ctxMenu = null;

function closeCtxMenu() {
  if (ctxMenu) { ctxMenu.remove(); ctxMenu = null; }
}

function showCtxMenu(x, y, items) {
  closeCtxMenu();
  const m = el('div', 'ctxmenu');
  items.forEach((it) => {
    const b = el('button', 'ctxmenu-i', it.label);
    b.type = 'button';
    b.disabled = !!it.off;
    b.addEventListener('click', () => { closeCtxMenu(); it.run(); });
    m.appendChild(b);
  });
  // 按下去不让焦点跑到菜单上 —— 输入框一旦失焦,「剪切」「全选」就找不到
  // 原来那个选区了。文字选区同理
  m.addEventListener('mousedown', (e) => e.preventDefault());
  // 先摆上去量尺寸,再决定放哪儿 —— 贴着右边/底边弹出的话要翻到另一侧,
  // 不然菜单有一半在窗口外面
  m.style.visibility = 'hidden';
  document.body.appendChild(m);
  m.style.left = Math.max(4, Math.min(x, window.innerWidth - m.offsetWidth - 4)) + 'px';
  m.style.top = Math.max(4, Math.min(y, window.innerHeight - m.offsetHeight - 4)) + 'px';
  m.style.visibility = '';
  ctxMenu = m;
}

function wireCopy() {
  document.addEventListener('keydown', (e) => {
    if (!(e.ctrlKey || e.metaKey) || (e.key || '').toLowerCase() !== 'c') return;
    const t = selText();
    // **刻意不 preventDefault**:WebView2 自己那份 Ctrl+C 要是好用,
    // 两边写进去的是同一段文字,重复一次没有代价;它要是被关掉了,
    // 这条就顶上。拦下来反而可能把好用的那条也弄坏
    if (t) copyText(t);
  });

  document.addEventListener('contextmenu', (e) => {
    // 已经有人接管了(比如附件那个"打开所在文件夹"),别抢
    if (e.defaultPrevented) return;
    const t = e.target;
    if (!t || !t.closest) return;
    const edit = t.tagName === 'TEXTAREA'
      || (t.tagName === 'INPUT'
          && /^(text|search|url|email|tel|number|password)$/i.test(t.type || 'text'));
    const zone = t.closest(TEXT_ZONES);
    const sel = selText();
    if (!edit && !zone && !sel) return;     // 没什么可给的,就当没这回事
    e.preventDefault();

    const items = [{ label: '复制', off: !sel, run: () => menuCopy(sel) }];
    if (edit) {
      items.push({ label: '剪切', off: !sel, run: () => cutIn(t) });
      items.push({ label: '粘贴', run: () => pasteIn(t) });
      items.push({ label: '全选', run: () => t.select() });
    } else {
      const msg = t.closest('.msg');
      const brief = t.closest('.brief-body');
      if (msg && msg._raw) {
        items.push({ label: '复制整条', run: () => menuCopy(msg._raw) });
      } else if (brief) {
        const raw = brief.id === 'mailBriefBody' ? state.mailBriefRaw : state.briefRaw;
        items.push({ label: '复制整份简报', off: !raw, run: () => menuCopy(raw) });
      }
      if (msg || zone) {
        items.push({ label: '全选这块', run: () => selectNode(msg || zone) });
      }
    }
    showCtxMenu(e.clientX, e.clientY, items);
  });

  // 点别处、滚动、Esc、窗口失焦 —— 都收起来
  document.addEventListener('mousedown', (e) => {
    if (ctxMenu && !ctxMenu.contains(e.target)) closeCtxMenu();
  }, true);
  // **听滚轮而不是 scroll 事件。** 页面自己会滚:流式输出每来一段就
  // `log.scrollTop = log.scrollHeight`,切页面也会把滚动位置归零 —— 那些都会
  // 派发 scroll。挂在 scroll 上的话,Claude 一边写、你一边右键,菜单会被自己
  // 的滚动关掉。wheel 和 touchmove 才是"人在滚"
  document.addEventListener('wheel', closeCtxMenu, true);
  document.addEventListener('touchmove', closeCtxMenu, true);
  window.addEventListener('resize', closeCtxMenu);
  // **捕获阶段**:Esc 的第一优先级是收起这个菜单。不抢的话同一下还会
  // 顺手把底下的作业详情 / 设置面板一起关掉(它们的 Esc 挂在冒泡阶段)
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && ctxMenu) {
      closeCtxMenu();
      e.stopPropagation();
    }
  }, true);
  window.addEventListener('blur', closeCtxMenu);
}

// 应用内没有地址栏和登录态,外链一律交给系统浏览器
function openExternal(url) {
  if (url) apiPost('/api/open', { url: url }).catch(() => {});
}

/* ───────────────────── 极简 markdown 渲染 ─────────────────────
   只建 DOM 节点、只用 textContent —— 模型输出同样当不可信内容处理,
   绝不走 innerHTML。支持:标题、无序/有序列表、围栏代码、粗体、斜体、
   行内代码、链接。 */

/* ─────────────────────────── 公式 ───────────────────────────

   **为什么不上 KaTeX。** 这是个本地应用,没有 CDN 可用(联网渲染公式还会把
   你在看什么告诉别人),而把 KaTeX 整个塞进仓库是 300KB 的字体加脚本 ——
   为了偶尔一个 Θ(n log n) 不值得。

   所以这里认的是**一个子集**:上下标、分式、根号、希腊字母和常用符号。
   算法课和数据课的对话里出现的基本就是这些(O(n^2)、\sum_{i=1}^{n}、
   \Theta(n \log n))。认不出来的命令**原样显示**,不猜 ——
   显示成 `oobar` 你一眼知道是没支持,猜错了反而看不出来。 */

const MATH_SYM = {
  alpha: 'α', beta: 'β', gamma: 'γ', delta: 'δ', epsilon: 'ε', zeta: 'ζ',
  eta: 'η', theta: 'θ', iota: 'ι', kappa: 'κ', lambda: 'λ', mu: 'μ',
  nu: 'ν', xi: 'ξ', pi: 'π', rho: 'ρ', sigma: 'σ', tau: 'τ',
  phi: 'φ', chi: 'χ', psi: 'ψ', omega: 'ω',
  Gamma: 'Γ', Delta: 'Δ', Theta: 'Θ', Lambda: 'Λ', Xi: 'Ξ', Pi: 'Π',
  Sigma: 'Σ', Phi: 'Φ', Psi: 'Ψ', Omega: 'Ω',
  sum: '∑', prod: '∏', int: '∫', infty: '∞', partial: '∂', nabla: '∇',
  le: '≤', leq: '≤', ge: '≥', geq: '≥', ne: '≠', neq: '≠', equiv: '≡',
  approx: '≈', sim: '∼', propto: '∝', pm: '±', mp: '∓',
  times: '×', div: '÷', cdot: '·', ast: '∗', star: '⋆',
  in: '∈', notin: '∉', subset: '⊂', subseteq: '⊆', supset: '⊃',
  cup: '∪', cap: '∩', emptyset: '∅', forall: '∀', exists: '∃',
  land: '∧', lor: '∨', neg: '¬', oplus: '⊕', otimes: '⊗',
  to: '→', rightarrow: '→', Rightarrow: '⇒', leftarrow: '←',
  Leftarrow: '⇐', leftrightarrow: '↔', Leftrightarrow: '⇔', mapsto: '↦',
  ldots: '…', cdots: '⋯', dots: '…', vdots: '⋮', ddots: '⋱',
  angle: '∠', perp: '⊥', parallel: '∥', degree: '°', prime: '′',
  aleph: 'ℵ', hbar: 'ℏ', ell: 'ℓ', Re: 'ℜ', Im: 'ℑ',
  lceil: '⌈', rceil: '⌉', lfloor: '⌊', rfloor: '⌋',
  langle: '⟨', rangle: '⟩', vert: '|', Vert: '‖',
};

// 这些是函数名,数学排版里要立体(正体)而不是斜体
const MATH_OP = ['log', 'ln', 'lg', 'exp', 'max', 'min', 'arg', 'gcd', 'lcm',
  'mod', 'sin', 'cos', 'tan', 'det', 'dim', 'deg', 'lim', 'sup', 'inf',
  'Pr', 'Theta', 'Omega'];
// 只管间距、不出字的
const MATH_SKIP = ['left', 'right', 'big', 'Big', 'bigg', 'Bigg', 'displaystyle',
  'limits', 'nolimits', 'quad', 'qquad'];
// 内容当普通文字排的
const MATH_TEXT = ['text', 'mathrm', 'mathbf', 'mathit', 'mathsf', 'mathtt',
  'operatorname', 'textbf', 'textit', 'boldsymbol'];

/* 从 i 处读一个"组":`{...}`(括号配平)或者紧跟的一个字符。
   返回 [内容, 下一个位置]。 */
function mathGroup(tex, i) {
  if (tex[i] !== '{') {
    if (tex[i] === undefined) return ['', i];
    // lpha^eta 这种:上标本身是个命令,整条读走
    if (tex[i] === '\\') {
      const m = /^\\([A-Za-z]+)/.exec(tex.slice(i));
      if (m) return [m[0], i + m[0].length];
    }
    return [tex[i], i + 1];
  }
  let depth = 0;
  for (let j = i; j < tex.length; j += 1) {
    if (tex[j] === '{') depth += 1;
    else if (tex[j] === '}') {
      depth -= 1;
      if (depth === 0) return [tex.slice(i + 1, j), j + 1];
    }
  }
  return [tex.slice(i + 1), tex.length];     // 没配上就当读到末尾
}

/* 把一段 TeX 画进 parent。递归 —— 分式的分子本身可以是分式。 */
function mathInto(tex, parent) {
  let buf = '';
  const flush = () => {
    if (buf) parent.appendChild(document.createTextNode(buf));
    buf = '';
  };
  let i = 0;
  let guard = 0;
  while (i < tex.length && guard++ < 4000) {
    const c = tex[i];

    if (c === '\\') {
      const m = /^\\([A-Za-z]+|.)/.exec(tex.slice(i));
      if (!m) { buf += c; i += 1; continue; }
      const name = m[1];
      i += m[0].length;
      if (name === 'frac' || name === 'dfrac' || name === 'tfrac') {
        const [num, i2] = mathGroup(tex, i);
        const [den, i3] = mathGroup(tex, i2);
        i = i3;
        flush();
        const f = el('span', 'md-frac');
        const n = el('span', 'md-frac-n');
        mathInto(num, n);
        const d = el('span', 'md-frac-d');
        mathInto(den, d);
        f.appendChild(n);
        f.appendChild(d);
        parent.appendChild(f);
        continue;
      }
      if (name === 'sqrt') {
        const [inner, i2] = mathGroup(tex, i);
        i = i2;
        flush();
        parent.appendChild(document.createTextNode('√'));
        const r = el('span', 'md-sqrt');
        mathInto(inner, r);
        parent.appendChild(r);
        continue;
      }
      if (MATH_TEXT.includes(name)) {
        const [inner, i2] = mathGroup(tex, i);
        i = i2;
        flush();
        parent.appendChild(el('span', 'md-math-up', inner));
        continue;
      }
      if (MATH_OP.includes(name)) {
        flush();
        parent.appendChild(el('span', 'md-math-up', MATH_SYM[name] || name));
        continue;
      }
      if (MATH_SKIP.includes(name)) {
        if (name === 'quad' || name === 'qquad') buf += '  ';
        continue;
      }
      if (MATH_SYM[name] !== undefined) { buf += MATH_SYM[name]; continue; }
      if (name.length === 1) { buf += name; continue; }   // \{ \} \_ \%
      // 不认识的命令原样留着 —— 猜错比看得出没支持更糟。
      // **连大括号一起留** —— 只留命令名会变成 `\\foobarx`,
      // 看着像一个词,反而不像"这儿有个没支持的命令"
      buf += '\\' + name;
      if (tex[i] === '{') {
        const [inner, i2] = mathGroup(tex, i);
        buf += '{' + inner + '}';
        i = i2;
      }
      continue;
    }

    if (c === '^' || c === '_') {
      const [inner, i2] = mathGroup(tex, i + 1);
      i = i2;
      flush();
      const n = el(c === '^' ? 'sup' : 'sub');
      mathInto(inner, n);
      parent.appendChild(n);
      continue;
    }

    if (c === '{' || c === '}') { i += 1; continue; }   // 纯分组括号不出字
    buf += c;
    i += 1;
  }
  flush();
}

/* 一段公式 -> 一个节点。display=true 是独占一行的那种($$…$$)。 */
function mathNode(tex, display) {
  const box = el(display ? 'div' : 'span',
    'md-math' + (display ? ' md-math-block' : ''));
  box.title = tex;                 // 认错了也能看到原文
  mathInto(tex, box);
  return box;
}

/* `$…$` 里到底是公式还是钱。**这一关不能省** —— 对话里「这次花了 $0.10」
   很常见,把它当公式渲染出来是一坨乱码。
   规矩:两头不能是空格、里面不能再有 $、而且得有个"像数学"的东西
   (反斜杠命令、上下标、花括号、或者字母)。纯数字一律当钱。 */
function looksMath(inner) {
  if (!inner || /^\s|\s$/.test(inner) || inner.includes('$')) return false;
  if (/^[\d.,]+$/.test(inner)) return false;
  return /[\\^_{}]/.test(inner) || /[A-Za-z]/.test(inner);
}

function mdInline(text, parent) {
  // 公式紧跟在行内代码后面 —— 不然 \$a * b\$ 里那个星号会先被当成强调标记
  const RE = /(`[^`\n]+`)|(\$[^$\n]+\$)|(\\\([^)]*\\\))|(\*\*[^*\n]+\*\*)|(\[[^\]\n]+\]\([^)\s]+\))|(\*[^*\n]+\*)/;
  let rest = text;
  let guard = 0;
  while (rest && guard++ < 2000) {
    const m = rest.match(RE);
    if (!m) { parent.appendChild(document.createTextNode(rest)); return; }
    if (m.index > 0) parent.appendChild(document.createTextNode(rest.slice(0, m.index)));
    const tok = m[0];
    if (tok.startsWith('`')) {
      parent.appendChild(el('code', 'md-code', tok.slice(1, -1)));
    } else if (tok.startsWith('$') || tok.startsWith('\\(')) {
      // 看着像钱就别当公式(「这次花了 $0.10」很常见),见 looksMath
      const inner = tok.startsWith('$') ? tok.slice(1, -1) : tok.slice(2, -2);
      if (tok.startsWith('$') && !looksMath(inner)) {
        parent.appendChild(document.createTextNode(tok));
      } else {
        parent.appendChild(mathNode(inner, false));
      }
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

const MD_BLOCK_START = /^(#{1,6}\s|```|\s*[-*+]\s|\s*\d+\.\s|\s*(?:\$\$|\\\[))/;

function renderMarkdown(raw, container) {
  container.textContent = '';
  const lines = raw.split('\n');
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];

    // 独占一行的公式。**必须排在段落之前** —— 不然整段会被当成普通
    // 文字,一屏的 $$ 和反斜杠
    const dm = line.match(/^\s*(?:\$\$|\\\[)\s*(.*?)\s*(?:\$\$|\\\])?\s*$/);
    if (dm && dm[1]) {
      container.appendChild(mathNode(dm[1], true));
      i++;
      continue;
    }

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
        // 脚本名分平台 —— 写死 install.ps1 的话,Mac 用户会去找一个
        // 不存在的 PowerShell 脚本(后端在 /api/update 里给了正确的那个)
        ? `找不到或读不到 Canvas token,跑一次 ${state.setupScript || 'install 脚本'} 重新写入。`
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

/* 往指定的对话框里加一条消息。课业和邮箱两个页面共用。

   **data-i 是这条消息在存档里的下标**(data/chats.json 那个数组)。「改一句
   重发」就是拿它去砍历史的,所以这里的编号必须和后端一条一条对得上:
   只有真会落盘的(自己说的 + Claude 答的)才编号,出错气泡和"试试问"那块
   都不编 —— 它们不进存档。 */
function addMsgIn(log, role, text, ctx) {
  const msg = el('div', `msg msg-${role}`);
  if (role === 'user' || role === 'assistant') {
    msg.dataset.i = String(log.querySelectorAll('.msg[data-i]').length);
  }
  msg.appendChild(el('div', 'msg-role', role === 'user' ? '我' : role === 'error' ? '出错' : 'Claude'));
  // **Claude 那一侧要过一遍 markdown。** 流式那条气泡一直是渲染过的
  // (flushChatBubble),但读回历史走的是这儿 —— 原来直接塞纯文本,于是
  // 重开一段对话满屏都是星号和 ``` ,公式更是没法看。
  // 自己说的话保持原样:那是他打的字,不该被解释成格式
  const body = el('div', 'msg-body');
  if (role === 'assistant') renderMarkdown(text || '', body);
  else body.textContent = text || '';
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
  // 复制的是 markdown 原文,不是渲染后的文字 —— 粘到别处才还是那份格式
  msg._raw = text || '';
  // 流式那条气泡是空着建出来的,等写完了再挂按钮(见 finishBubble)
  if (msg._raw) attachMsgActions(msg);
  log.appendChild(msg);
  log.scrollTop = log.scrollHeight;
  return body;
}

/* ── 每条消息底下那行小动作 ──
   平时淡着,鼠标停到这条上才显出来。自己说的话多一个「编辑」:
   改完重发会把这条之后的都作废,和 ChatGPT 那套一样。 */
function attachMsgActions(msg) {
  if (!msg || msg.querySelector(':scope > .msg-acts')) return;
  const row = el('div', 'msg-acts');
  row.appendChild(msgActBtn('复制', '复制这条的原文', (b) => copyInto(b, msg._raw)));
  if (msg.classList.contains('msg-user') && msg.dataset.i !== undefined) {
    row.appendChild(msgActBtn('编辑', '改一改,重新发一次', () => startEditMsg(msg)));
  }
  msg.appendChild(row);
}

function msgActBtn(label, title, fn) {
  const b = el('button', 'msg-act', label);
  b.type = 'button';
  b.title = title;
  b.addEventListener('click', () => fn(b));
  return b;
}

/* 流式写完了:把攒下来的原文记到气泡上,顺手把按钮挂上去。 */
function finishBubble(bubble, raw) {
  if (!bubble) return;
  const msg = bubble.closest ? bubble.closest('.msg') : null;
  if (!msg) return;
  msg._raw = raw || '';
  if (msg._raw) attachMsgActions(msg);
}

/* ── 改一句重发 ──
   气泡就地变成输入框(不是把文字丢回底下的输入栏)—— 这样看得见改的是
   哪一条。Enter 发送、Shift+Enter 换行、Esc 放弃,和主输入框一个手感。 */
function startEditMsg(msg) {
  const log = msg.parentElement;
  if (!log || msg.querySelector('.msg-edit')) return;
  const isMail = log.id === 'mailChatLog';
  const body = msg.querySelector('.msg-body');
  const acts = msg.querySelector(':scope > .msg-acts');
  const box = el('div', 'msg-edit');
  const ta = el('textarea', 'msg-edit-ta');
  ta.value = msg._raw || (body ? body.textContent : '');
  const row = el('div', 'msg-edit-row');
  const tip = el('span', 'msg-edit-tip', '发出去之后,这条以下的都不作数了');
  const cancel = el('button', 'link-btn', '取消');
  cancel.type = 'button';
  const send = el('button', 'msg-edit-send', '发送');
  send.type = 'button';
  row.appendChild(tip);
  row.appendChild(cancel);
  row.appendChild(send);
  box.appendChild(ta);
  box.appendChild(row);

  const close = () => {
    box.remove();
    if (body) body.hidden = false;
    if (acts) acts.hidden = false;
  };
  cancel.addEventListener('click', close);
  send.addEventListener('click', () => submitEdit(msg, ta.value, isMail, close, send));
  ta.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { e.preventDefault(); close(); return; }
    // isComposing:中文输入法选字时的那个回车不算发送
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      submitEdit(msg, ta.value, isMail, close, send);
    }
  });
  ta.addEventListener('input', () => autoGrowEl(ta));

  if (body) body.hidden = true;
  if (acts) acts.hidden = true;
  msg.insertBefore(box, acts || null);
  autoGrowEl(ta);
  ta.focus();
  // 光标摆到末尾:多半是想接着补两句,而不是从头重写
  ta.setSelectionRange(ta.value.length, ta.value.length);
}

function submitEdit(msg, text, isMail, close, btn) {
  text = (text || '').trim();
  if (!text) return;
  // 上一轮还在写就先不动:后端一次只跑一个问题,砍了历史又发不出去最难受。
  // 编辑框留在原地,等答完再点一次就行
  if (isMail ? state.mailStreaming : state.streaming) {
    flashBtn(btn, '等这轮答完');
    return;
  }
  const idx = Number(msg.dataset.i);
  const log = msg.parentElement;
  close();
  if (Number.isInteger(idx) && log) {
    // 这条和它后面的全部作废 —— 后端那边也会砍到同一个位置
    let n = log.lastElementChild;
    while (n && n !== msg) { const prev = n.previousElementSibling; n.remove(); n = prev; }
    msg.remove();
  }
  if (isMail) sendMailChat(text, { editIndex: idx });
  else sendChat(text, { editIndex: idx });
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
  // 边写边更新原文,这样生成到一半也能整份复制走
  state.briefRaw = state.briefLive;
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
      finishBubble(state.bubble, state.bubbleRaw);
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
  // editIndex >= 0 就是「改一句重发」:走另一个接口,后端会先把存档砍到
  // 这一条,再换一条会话把前文补回去(见 server.resend_edited)
  const edit = opts && Number.isInteger(opts.editIndex) ? opts.editIndex : -1;
  // 关联对象只在"这组对象变化后的第一条消息"附过去:后续追问靠会话上下文
  // 接着就行,每轮都重发是白烧额度。**重发是例外** —— 那边的会话是新起的,
  // 不重新附一遍它就不知道在问哪个作业了
  const ctx = (edit >= 0 || !state.ctxSent) ? state.ctx.slice() : [];
  // 开机简报是自动发的,显示成用户说的话会很怪
  if (!(opts && opts.silent)) addMsg('user', text, state.ctx.slice());
  $('chatInput').value = '';
  autoGrow();
  setStreaming(true);          // 先锁住输入,别等后端第一个事件
  setChatStatus('连接中…');
  let r;
  try {
    r = await apiPost(edit >= 0 ? '/api/chat/edit' : '/api/chat',
                      { message: text, context: ctx, index: edit });
    if (ctx.length) state.ctxSent = true;
  } catch (e) {
    addMsg('error', '后端没响应:' + e.message);
    setStreaming(false);
    if (edit >= 0) openChat(state.chatId);   // 界面已经砍了、后端没砍,重新对齐
    return;
  }
  if (!r.ok) {
    addMsg('error', r.message);
    setStreaming(false);
    if (edit >= 0) openChat(state.chatId);
  }
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

/* ═════════════════════════ 每周课程表 ═════════════════════════

   横轴周日→周六,纵轴时刻。格子里三类东西:

     上课          从课程首页 / syllabus / 公告的**正文**里抽出来的
     office hour   同上
     备忘录        data/memos.json 里「每周」和「某一天」那两种

   前两类没有结构化接口可查 —— Canvas 的 /appointment_groups 和
   /calendar_events 实测都是空的,时间全是老师写在网页上的散文。所以后端
   过一次模型把散文变成条目(timetable.py),这里只负责画,以及让每一格都能
   手改:抽错了点一下就能纠,改过的不会被下一次解析冲掉。

   纵轴默认**只画有内容的时段**(现在是 10:00–18:00 左右)。0–24 全画的话
   所有课挤在中间三分之一,看不清 —— 但「全天」按钮随时能切回去。 */

const WK_CN = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
const WK_HOUR_PX = 46;          // 一小时画多高
const WK_KIND_CN = { lecture: '上课', office: 'OH', memo: '备忘',
  mail: '邮件', other: '' };
// 全天条一格里最多摆几条,多的折叠成 +N。摆太多会把表头顶起来
const WK_ALLDAY_MAX = 3;

function wkSunday(d) {
  const x = new Date(d);
  x.setHours(0, 0, 0, 0);
  x.setDate(x.getDate() - x.getDay());
  return x;
}

function wkYmd(d) {
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

// "14:20" -> 860
function wkMins(s) {
  const m = /^(\d{1,2}):(\d{2})$/.exec(String(s || ''));
  return m ? Number(m[1]) * 60 + Number(m[2]) : 0;
}

function wkClock(min) {
  const p = (n) => String(n).padStart(2, '0');
  return `${p(Math.floor(min / 60) % 24)}:${p(min % 60)}`;
}

/* 日程从"左栏里的一个子视图"升成了顶层页,所以这两个函数现在就是切页。
   加载和定时重画都在 setPage 里做,见那儿的注释。 */
function openWeek() {
  setPage('week');
}

function closeWeek() {
  setPage('study');
}

function wkTick() {
  if ($('weekView').hidden) return;
  renderWeek();
}

function wkShift(days) {
  const d = new Date(state.weekStart + 'T00:00:00');
  d.setDate(d.getDate() + days);
  state.weekStart = wkYmd(d);
  loadWeek();
}

async function loadWeek() {
  try {
    state.week = await apiGet('/api/schedule', { week: state.weekStart });
  } catch (e) {
    $('weekBanner').hidden = false;
    $('weekBanner').textContent = '读不到课表:' + ((e && e.message) || e);
    return;
  }
  $('weekBanner').hidden = true;
  renderWeek();
  renderWeekEntry();
}

/* 纵轴范围。默认贴着内容走,上下各留一小时余量;「全天」是 0–24。
   一条都没有的时候给个 8–20 的空架子 —— 比一片 0–24 的空白好认。 */
function wkRange(items) {
  if (state.prefs.schedFull) return [0, 24];
  if (!items.length) return [8, 20];
  let lo = 24 * 60, hi = 0;
  items.forEach((it) => {
    lo = Math.min(lo, wkMins(it.start));
    hi = Math.max(hi, wkMins(it.end));
  });
  return [Math.max(0, Math.floor(lo / 60) - 1), Math.min(24, Math.ceil(hi / 60) + 1)];
}

/* 同一天里时间重叠的条目分列排开,不然后面那条会被前面那条整个盖住。
   贪心:按开始时间扫,能塞进已有某一列就塞,塞不下才开新列。 */
function wkLayout(evs) {
  let cluster = [];
  let clusterEnd = -1;
  const flush = () => {
    if (cluster.length) {
      const colEnd = [];
      cluster.forEach((e) => {
        let i = 0;
        while (i < colEnd.length && colEnd[i] > e.s) i += 1;
        if (i === colEnd.length) colEnd.push(0);
        colEnd[i] = e.e;
        e.col = i;
      });
      cluster.forEach((e) => { e.ncols = colEnd.length; });
    }
    cluster = [];
    clusterEnd = -1;
  };
  evs.slice().sort((a, b) => a.s - b.s).forEach((e) => {
    if (cluster.length && e.s >= clusterEnd) flush();
    cluster.push(e);
    clusterEnd = Math.max(clusterEnd, e.e);
  });
  flush();
}

function renderWeek() {
  const d = state.week;
  const grid = $('weekGrid');
  grid.textContent = '';
  if (!d) return;

  const items = (d.items || []).filter(
    (it) => (state.prefs.schedMemos !== false || !it.memo)
      && (state.prefs.schedMail !== false || !it.mail));
  const [lo, hi] = wkRange(items);
  const top = lo * 60;
  const total = (hi - lo) * 60;
  const px = (min) => ((min - top) / 60) * WK_HOUR_PX;

  const start = new Date(state.weekStart + 'T00:00:00');
  const today = wkYmd(new Date());
  const nowMin = new Date().getHours() * 60 + new Date().getMinutes();

  // ── 表头:星期 + 日期
  const head = el('div', 'wk-head');
  head.appendChild(el('div', 'wk-corner'));
  for (let i = 0; i < 7; i += 1) {
    const day = new Date(start);
    day.setDate(day.getDate() + i);
    const h = el('div', 'wk-dayh');
    if (wkYmd(day) === today) h.classList.add('is-today');
    h.appendChild(el('span', 'wk-dayh-n', WK_CN[i]));
    h.appendChild(el('span', 'wk-dayh-d', `${day.getMonth() + 1}/${day.getDate()}`));
    head.appendChild(h);
  }
  grid.appendChild(head);

  // ── 全天条。只在真有全天条目的时候才建节点 —— 空着一条横线在那儿是
  // 白占地方。**不能把它们当成 00:00–23:59 的块塞进主体**:纵轴范围是按
  // 最早/最晚条目算的,那样会把它撑成 0–24,两门真课挤成一条缝
  const ad = (d.allday || []).filter(
    (x) => state.prefs.schedMail !== false);
  if (ad.length) {
    const row = el('div', 'wk-allday');
    row.appendChild(el('div', 'wk-allday-k', '全天'));
    for (let i = 0; i < 7; i += 1) {
      const day = new Date(start);
      day.setDate(day.getDate() + i);
      const cell = el('div', 'wk-allday-c');
      if (wkYmd(day) === today) cell.classList.add('is-today');
      const mine = ad.filter((x) => x.weekday === i);
      mine.slice(0, WK_ALLDAY_MAX).forEach((it) => {
        const b = el('button', 'wk-ad' + (it.mkind === 'due' ? ' is-due' : ''),
          (it.mkind === 'due' ? '截止 · ' : '') + (it.title || ''));
        b.type = 'button';
        b.title = [it.title, it.note].filter(Boolean).join('\n');
        b.addEventListener('click', () => openWkSheet(it));
        cell.appendChild(b);
      });
      if (mine.length > WK_ALLDAY_MAX) {
        const more = el('div', 'wk-ad is-more',
          `+${mine.length - WK_ALLDAY_MAX}`);
        more.title = mine.slice(WK_ALLDAY_MAX).map((x) => x.title).join('\n');
        cell.appendChild(more);
      }
      row.appendChild(cell);
    }
    grid.appendChild(row);
  }

  // ── 主体:左边时刻尺,右边七列
  const body = el('div', 'wk-body');
  body.style.height = ((hi - lo) * WK_HOUR_PX) + 'px';
  // 网格线是画在背景上的(repeating gradient),不是一堆 div —— 24 小时 × 7 列
  // 真建出来是 168 个节点,每分钟重画一次太浪费
  body.style.backgroundSize = `100% ${WK_HOUR_PX}px`;

  const ruler = el('div', 'wk-ruler');
  for (let h = lo; h <= hi; h += 1) {
    const t = el('div', 'wk-tick', `${String(h % 24).padStart(2, '0')}:00`);
    t.style.top = px(h * 60) + 'px';
    ruler.appendChild(t);
  }
  body.appendChild(ruler);

  for (let i = 0; i < 7; i += 1) {
    const day = new Date(start);
    day.setDate(day.getDate() + i);
    const isToday = wkYmd(day) === today;
    const col = el('div', 'wk-col');
    if (isToday) col.classList.add('is-today');

    const evs = items.filter((it) => it.weekday === i).map((it) => ({
      it, s: wkMins(it.start), e: wkMins(it.end), col: 0, ncols: 1,
    }));
    wkLayout(evs);
    evs.forEach((e) => {
      col.appendChild(wkBlock(e, px, isToday, nowMin));
    });

    // 当前时刻那条线。只画在今天那一列上 —— 横贯七列的话,看一眼分不清
    // 说的是"现在"还是某种分隔
    if (isToday && nowMin >= top && nowMin <= top + total) {
      const line = el('div', 'wk-now');
      line.style.top = px(nowMin) + 'px';
      line.title = '现在 ' + wkClock(nowMin);
      col.appendChild(line);
    }
    body.appendChild(col);
  }
  grid.appendChild(body);

  // ── 头上那行状态
  const range = new Date(start);
  const end = new Date(start);
  end.setDate(end.getDate() + 6);
  $('weekRange').textContent =
    `${range.getMonth() + 1}月${range.getDate()}日 – ${end.getMonth() + 1}月${end.getDate()}日`;
  $('btnWeekNow').hidden = state.weekStart === wkYmd(wkSunday(new Date()));
  $('btnWeekFull').classList.toggle('is-on', !!state.prefs.schedFull);

  const nAuto = items.filter((x) => x.auto).length;
  const nHand = items.filter((x) => !x.auto && !x.memo && !x.mail).length;
  const nMemo = items.filter((x) => x.memo).length;
  const nMail = items.filter((x) => x.mail).length + ad.length;
  const bits = [`${nAuto} 条抽自课程页面`];
  if (nHand) bits.push(`${nHand} 条手加`);
  if (nMemo) bits.push(`${nMemo} 条备忘`);
  if (nMail) bits.push(`${nMail} 条来自邮件`);
  $('weekSub').textContent = bits.join(' · ');

  renderWeekNotes(d);
  renderWeekBadge();
  // 页脚只说有货那几门课解析于何时 —— 五门课全列出来会绕两行,而没抽出
  // 东西的那几门上面 quiet 那一行已经交代过了
  const parsed = (d.parsed || []).filter((x) => x.n);
  const when = (d.parsed || []).map((x) => x.at).filter(Boolean).sort().pop();
  $('weekFoot').textContent = (d.parsed || []).length
    ? `上次解析 ${(when || '').slice(5, 16)}`
      + (parsed.length ? ` · ${parsed.map((x) => x.course).join('、')}` : '')
      + (d.cost ? ` · 累计 $${d.cost}` : '')
    : '还没解析过。点「重新解析」让它去读课程首页和 syllabus。';
}

function wkBlock(e, px, isToday, nowMin) {
  const it = e.it;
  const b = el('div', 'wk-ev is-' + (it.kind || 'other'));
  b.style.top = px(e.s) + 'px';
  b.style.height = Math.max(18, px(e.e) - px(e.s) - 2) + 'px';
  b.style.left = `calc(${(e.col / e.ncols) * 100}% + 2px)`;
  b.style.width = `calc(${(1 / e.ncols) * 100}% - 4px)`;
  if (isToday && nowMin >= e.s && nowMin < e.e) b.classList.add('is-live');

  // 颜色不是唯一的区分通道:每块自己带类型字样和时间
  const kindCn = WK_KIND_CN[it.kind] || '';
  const head = el('div', 'wk-ev-h');
  if (kindCn) head.appendChild(el('span', 'wk-ev-k', kindCn));
  // 和别人分列的时候块只有半格宽,"上课 11:00–14:20" 会折成两行、把名字挤没。
  // 时间在纵轴上本来就读得出来,悬停也有 —— 窄的时候让位给名字
  if (e.ncols < 2) head.appendChild(el('span', 'wk-ev-t', `${it.start}–${it.end}`));
  b.appendChild(head);
  b.appendChild(el('div', 'wk-ev-name', it.title || ''));
  const sub = [it.course, it.place].filter(Boolean).join(' · ');
  if (sub) b.appendChild(el('div', 'wk-ev-sub', sub));
  if (it.edited) b.appendChild(el('span', 'wk-ev-flag', '改'));

  b.title = [it.title, it.who, it.place, it.note,
    `${WK_CN[it.weekday]} ${it.start}–${it.end}`].filter(Boolean).join('\n');

  if (it.memo) {
    b.classList.add('is-memo');
  } else {
    b.addEventListener('click', () => openWkSheet(it));
  }
  return b;
}

/* 模型看见了、但排不进格子的话。**这一段不能省** —— 空着的 office hour 列
   看不出是"老师没有"还是"没抓到",这里那句"TA office hour 还没公布"才说清。 */
/* 顶层导航上那个角标:今天还剩几件事。

   **只数"还没过去的"** —— 一整天的课都上完了还挂个 3 在那儿,那个数字就
   只是噪音。全天的事只要是今天就算(它没有时刻,过不过去无从判断)。 */
function renderWeekBadge() {
  const b = $('weekBadge');
  if (!b) return;
  const d = state.week;
  const today = wkYmd(new Date());
  const nowMin = new Date().getHours() * 60 + new Date().getMinutes();
  const wd = new Date().getDay();
  let n = 0;
  if (d) {
    // 角标只在"本周"这一屏有意义:翻到下一周的时候格子里根本没有今天
    const thisWeek = state.weekStart === wkYmd(wkSunday(new Date()));
    if (thisWeek) {
      n = (d.items || []).filter(
        (x) => x.weekday === wd && !x.memo
          && (state.prefs.schedMail !== false || !x.mail)
          && wkMins(x.end) > nowMin).length
        + (d.allday || []).filter(
          (x) => x.date === today && state.prefs.schedMail !== false).length;
    }
  }
  b.textContent = String(n);
  b.hidden = n <= 0;
}

function renderWeekNotes(d) {
  const box = $('weekNotes');
  box.textContent = '';
  (d.notes || []).forEach((n) => {
    const row = el('div', 'wk-note');
    row.appendChild(el('span', 'wk-note-c', n.course || ''));
    row.appendChild(el('span', 'wk-note-t', n.text || ''));
    box.appendChild(row);
  });
  // 一条都没抽出来的课(培训模块、orientation)一行带过 —— 它们的 note
  // 全是"这门课没有每周固定安排",逐条列出来会把上面两门真课的说明淹掉
  const quiet = d.quiet || [];
  if (quiet.length) {
    const row = el('div', 'wk-note');
    row.appendChild(el('span', 'wk-note-c', '其他'));
    row.appendChild(el('span', 'wk-note-t',
      `${quiet.join('、')} 没有每周固定安排`));
    box.appendChild(row);
  }
  // 被删掉的条目。**这一段不能省** —— 藏起来的条目不画格子,于是它从界面上
  // 彻底消失;而 item_id 是按内容算的,重新解析回来的还是同一个 id,照样被
  // 压住。不说出来的话,人看到的就是"点了重新解析,格子还是空的",
  // 会以为解析坏了。所以这里既要说清是谁干的,也要给一条回头路。
  const gone = d.hidden || [];
  if (gone.length) {
    const row = el('div', 'wk-note wk-note-gone');
    row.appendChild(el('span', 'wk-note-c', '删掉的'));
    const t = el('span', 'wk-note-t');
    t.appendChild(document.createTextNode(
      `${gone.length} 条被你删掉了,所以格子里看不到 —— 重新解析也不会把它们请回来。`));
    const all = el('button', 'link-btn wk-restore-all', '全部恢复');
    all.type = 'button';
    all.addEventListener('click', () => restoreWk(gone.map((x) => x.id)));
    t.appendChild(all);
    row.appendChild(t);
    box.appendChild(row);
    gone.forEach((g) => {
      const r = el('div', 'wk-note wk-note-gone');
      r.appendChild(el('span', 'wk-note-c', ''));
      const b = el('span', 'wk-note-t');
      b.appendChild(el('span', 'wk-gone-t',
        `${g.course} · ${g.title} · ${WK_CN[g.weekday] || ''} ${g.start}–${g.end}`));
      const btn = el('button', 'link-btn', '恢复');
      btn.type = 'button';
      btn.addEventListener('click', () => restoreWk([g.id]));
      b.appendChild(btn);
      r.appendChild(b);
      box.appendChild(r);
    });
  }
}

// 课表面板那个「删除」的复位。wireWeek 里赋真身,在那之前调到也不出事
let wfDisarm = () => {};

/* 把删掉的条目放回来。revert = 丢掉这条 id 上的覆盖层,hidden 跟着没了。 */
async function restoreWk(ids) {
  for (const id of ids) {
    try {
      await apiPost('/api/schedule/item', { action: 'revert', id: id });
    } catch (e) {
      reportError('restoreWk', (e && e.message) || String(e), '', 0, e && e.stack);
    }
  }
  await loadWeek();
}

/* 仪表盘上那一行:今天接下来还有什么。**不点进去也有用**,所以它不只是个
   按钮 —— 今天没课就说明天,明天也没有就说这周几条。 */
function renderWeekEntry() {
  const box = $('weekEntryText');
  if (!box) return;
  const d = state.week;
  if (!d) { box.textContent = '还没读取'; return; }
  // 备忘不算(那是另一列的事),但**邮件日程要算** —— 今天下午三点的面试
  // 正是这一行该说的东西
  const items = (d.items || []).filter(
    (x) => !x.memo && (state.prefs.schedMail !== false || !x.mail));
  const today = wkYmd(new Date());
  const adToday = (d.allday || []).filter(
    (x) => x.date === today && state.prefs.schedMail !== false);
  if (!items.length && !adToday.length) {
    box.textContent = '还没解析过 —— 点进去抽一次';
    return;
  }
  // 今天有全天的事(交表截止之类)就挂在后面一句 —— 它没有时刻,排不进
  // "接下来",但漏掉它这行就在说谎
  const tail = adToday.length
    ? ` · 今天还有「${adToday[0].title}」${adToday.length > 1 ? ` 等 ${adToday.length} 件` : ''}`
    : '';
  const say = (t) => { box.textContent = t + tail; };
  const now = new Date();
  const wd = now.getDay();
  const nowMin = now.getHours() * 60 + now.getMinutes();
  const live = items.find(
    (x) => x.weekday === wd && wkMins(x.start) <= nowMin && nowMin < wkMins(x.end));
  if (live) {
    say(`正在进行 · ${live.title} 到 ${live.end}`
      + (live.place ? ` · ${live.place}` : ''));
    return;
  }
  const next = items
    .filter((x) => x.weekday === wd && wkMins(x.start) > nowMin)
    .sort((a, b) => wkMins(a.start) - wkMins(b.start))[0];
  if (next) {
    say(`今天 ${next.start} ${next.title}`
      + (next.place ? ` · ${next.place}` : ''));
    return;
  }
  // 今天没有了 —— 往后找最近的一天
  for (let k = 1; k <= 7; k += 1) {
    const day = (wd + k) % 7;
    const list = items.filter((x) => x.weekday === day)
      .sort((a, b) => wkMins(a.start) - wkMins(b.start));
    if (list.length) {
      say(`${k === 1 ? '明天' : WK_CN[day]} ${list[0].start} ${list[0].title}`);
      return;
    }
  }
  say(items.length ? `本周 ${items.length} 项` : '今天没有固定日程');
}

/* ── 改一条 / 加一条 ──
   自动抽出来的条目改的是覆盖层(后端 edits),重新解析不会冲掉;
   手加的条目是真改真删。前端只看 id 前缀:u 开头是手加的。 */

function openWkSheet(it) {
  state.weekEdit = it || null;
  const mk = !it;
  // 邮件来的日程只有"什么事、哪天、几点"这几样能改 —— 类型、课程、谁、
  // 地点、链接对它没有意义,整行藏掉比留着几个空框好
  const isMail = !!(it && it.mail);
  ['wfRowKind', 'wfRowWho', 'wfRowPlace', 'wfRowUrl'].forEach((id) => {
    if ($(id)) $(id).hidden = isMail;
  });
  // 来源邮件。合并过的(几封信说同一件事)列成一份清单,点哪封开哪封;
  // 只有一封就还是底下那个「看这封邮件」按钮,不值得为它撑出一块区域
  const mails = (isMail && it.mails) || [];
  renderWfMails(mails.length > 1 ? mails : []);
  $('btnWfMail').hidden = !(isMail && it.mid) || mails.length > 1;
  // 星期对邮件日程是**算出来的**(日期决定),不是能改的。留着能看清是哪天,
  // 但禁掉 —— 不然改了它以为生效了,而 wkMailForm 根本不发这个字段
  $('wfWeekday').disabled = isMail;
  $('weekSheetTitle').textContent = mk ? '加一条'
    : (isMail ? '这条来自邮件' : '改这一条');
  $('wfTitle').value = it ? (it.title || '') : '';
  $('wfKind').value = it ? (it.kind || 'other') : 'other';
  $('wfCourse').value = it ? (it.course || '') : '';
  $('wfWeekday').value = String(it ? it.weekday : new Date().getDay());
  $('wfStart').value = it ? (it.start || '') : '09:00';
  $('wfEnd').value = it ? (it.end || '') : '10:00';
  $('wfWho').value = it ? (it.who || '') : '';
  $('wfPlace').value = it ? (it.place || '') : '';
  $('wfUrl').value = it ? (it.url || '') : '';
  $('wfNote').value = it ? (it.note || '') : '';
  $('wfHint').textContent = isMail
    ? `这条是从邮件里抽出来的${it.date ? `(${it.date})` : ''}。`
      + '时间留空 = 全天,摆到顶上那条全天条里;删掉不会再回来。'
    : (it && it.auto
      ? '这条是从课程页面里抽出来的。改了之后,重新解析不会把你的改动冲掉。'
      : '');
  $('btnWfRevert').hidden = !(it && (it.auto || it.mail) && it.edited);
  $('btnWfDel').hidden = mk;
  wfDisarm();                    // 上次点了一下没删就关掉的,复位
  $('weekSheet').hidden = false;
  $('wfTitle').focus();
}

/* 把合并进这一条日程的几封信列出来。空列表 = 整块藏掉。 */
function renderWfMails(mails) {
  const box = $('wfMails');
  box.textContent = '';
  if (!mails.length) { box.hidden = true; return; }
  box.hidden = false;
  box.appendChild(el('div', 'wf-mails-h', `${mails.length} 封信说的是这件事`));
  mails.forEach((m) => {
    const b = el('button', 'wf-mail');
    b.type = 'button';
    b.disabled = !m.mid;
    b.appendChild(el('span', 'wf-mail-d', (m.day || '').slice(5) || '—'));
    b.appendChild(el('span', 'wf-mail-s', m.subject || '(没有主题)'));
    b.title = m.mid ? '打开这封信' : '这封信已经不在本地索引里了';
    if (m.mid) {
      b.addEventListener('click', () => { closeWkSheet(); openMailById(m.mid); });
    }
    box.appendChild(b);
  });
}

function closeWkSheet() {
  $('weekSheet').hidden = true;
  state.weekEdit = null;
}

async function wkSend(body) {
  try {
    const r = await apiPost('/api/schedule/item', body);
    if (r && r.ok === false) {
      $('wfHint').textContent = r.error || '没存上';
      return false;
    }
  } catch (e) {
    $('wfHint').textContent = (e && e.message) || String(e);
    return false;
  }
  closeWkSheet();
  await loadWeek();
  return true;
}

function wkForm() {
  return {
    title: $('wfTitle').value.trim(),
    kind: $('wfKind').value,
    course: $('wfCourse').value.trim(),
    weekday: Number($('wfWeekday').value),
    start: $('wfStart').value,
    end: $('wfEnd').value,
    who: $('wfWho').value.trim(),
    place: $('wfPlace').value.trim(),
    url: $('wfUrl').value.trim(),
    note: $('wfNote').value.trim(),
  };
}

/* 邮件日程能改的就这几样。**不发 kind** —— 那个字段在邮件日程里是
   meet/due/other,和表单里 lecture/office 那三个不是一回事,发过去会把
   截止条目变成"上课"。 */
function wkMailForm() {
  return {
    title: $('wfTitle').value.trim(),
    start: $('wfStart').value,
    end: $('wfEnd').value,
    note: $('wfNote').value.trim(),
  };
}

function renderSchedState(st) {
  state.sched = st || {};
  const box = $('weekState');
  if (!box) return;
  if (st && st.running) {
    box.textContent = `解析中 ${st.done || 0}/${st.total || 0}`
      + (st.course ? ` · ${st.course}` : '');
  } else if (st && (st.errors || []).length) {
    box.textContent = '解析出错:' + st.errors.join(' / ');
  } else {
    // **跑完了要说一句。** 原来这儿是直接清空 —— 于是"源文没变所以跳过了"
    // 和"真的重抽了一遍"长得一模一样,都是点完什么都不动,看着像按钮坏了
    const bits = [];
    if (st && st.restored) bits.push(`放回了 ${st.restored} 条你删掉的`);
    if (st && st.skipped) bits.push(`${st.skipped} 门课源文没变,沿用上次的`);
    box.textContent = st && st.last && bits.length
      ? bits.join(' · ') : '';
    // 刚跑完:把结果读回来
    if (st && st.last && !$('weekView').hidden) loadWeek();
  }
  $('btnWeekParse').disabled = !!(st && st.running);
}

function wireWeek() {
  $('btnWeek').addEventListener('click', openWeek);
  $('btnWeekBack').addEventListener('click', closeWeek);
  $('btnWeekPrev').addEventListener('click', () => wkShift(-7));
  $('btnWeekNext').addEventListener('click', () => wkShift(7));
  $('btnWeekNow').addEventListener('click', () => {
    state.weekStart = wkYmd(wkSunday(new Date()));
    loadWeek();
  });
  $('btnWeekFull').addEventListener('click', () => {
    savePrefs({ schedFull: !state.prefs.schedFull });
    renderWeek();
  });
  $('btnWeekAdd').addEventListener('click', () => openWkSheet(null));
  $('btnWeekParse').addEventListener('click', async () => {
    $('weekState').textContent = '解析中…';
    try {
      // manual:这是人点的,不是每小时那次自动保鲜 —— 后端据此把删掉的
      // 条目放回来(自动那次不会,见 server.parse_schedule)
      const r = await apiPost('/api/schedule/parse', { manual: true });
      if (r && r.state) renderSchedState(r.state);
    } catch (e) {
      $('weekState').textContent = (e && e.message) || String(e);
    }
  });

  $('btnWeekSheetX').addEventListener('click', closeWkSheet);
  $('weekSheet').addEventListener('click', (e) => {
    if (e.target === $('weekSheet')) closeWkSheet();
  });
  $('weekForm').addEventListener('submit', (e) => e.preventDefault());
  $('btnWfSave').addEventListener('click', () => {
    const cur = state.weekEdit;
    const isMail = !!(cur && cur.mail);
    const f = isMail ? wkMailForm() : wkForm();
    // 邮件日程:两个时刻都空是合法的,那就是"全天"
    const blank = isMail && !f.start && !f.end;
    if (!blank && wkMins(f.end) <= wkMins(f.start)) {
      $('wfHint').textContent = '结束时间要晚于开始时间';
      return;
    }
    wkSend(cur ? Object.assign({ action: 'edit', id: cur.id }, f)
               : Object.assign({ action: 'add' }, f));
  });
  $('btnWfMail').addEventListener('click', () => {
    const cur = state.weekEdit;
    if (!(cur && cur.mid)) return;
    closeWkSheet();
    openMailById(cur.mid);
  });
  $('btnWfRevert').addEventListener('click', () => {
    if (state.weekEdit) wkSend({ action: 'revert', id: state.weekEdit.id });
  });
  // 删除要点两下,和对话那边一个路子(wireChatHistory)。**这里更要拦一下**:
  // 这个按钮就贴在「保存」旁边,而自动抽出来的条目删掉之后是从格子里彻底
  // 消失的 —— 以前连个找回的入口都没有,一次误点就等于这门课的上课时间没了。
  const wfDel = $('btnWfDel');
  let wfArmed = 0;
  wfDisarm = () => {
    wfArmed = 0;
    wfDel.textContent = '删除';
    wfDel.classList.remove('is-armed');
  };
  wfDel.addEventListener('click', () => {
    if (!state.weekEdit) return;
    if (Date.now() - wfArmed > 3000) {
      wfArmed = Date.now();
      wfDel.textContent = '确认删除?';
      wfDel.classList.add('is-armed');
      setTimeout(() => { if (Date.now() - wfArmed >= 3000) wfDisarm(); }, 3100);
      return;
    }
    const id = state.weekEdit.id;
    wfDisarm();
    wkSend({ action: 'delete', id: id });
  });
}

/* ═════════════════════════ 邮箱子页面 ═════════════════════════

   左边收件箱(3 分钟自动收一次),右边邮件简报 / 邮件对话。
   **和课业那套完全分开**:存档、CLI 通道、历史记录都是独立的两份,
   所以课业简报在生成的时候你还能问邮件的事,互不打断。 */

function setPage(page) {
  state.page = ['mail', 'week'].includes(page) ? page : 'study';
  document.body.dataset.page = state.page;
  // 日程页:#weekView 上那个 hidden 留着不动 —— 别处有好几处拿
  // `!$('weekView').hidden` 当"现在看得见吗"用(定时重画、改了偏好要不要
  // 重渲染),去掉它那些判断就全失效了
  const onWeek = state.page === 'week';
  $('weekView').hidden = !onWeek;
  if (onWeek) {
    if (!state.weekStart) state.weekStart = wkYmd(wkSunday(new Date()));
    loadWeek();
    // 「实时」就体现在这儿:红线和「进行中」的高亮每 30 秒自己往下走,
    // 不用重新拉数据
    if (!state.weekTick) state.weekTick = setInterval(wkTick, 30000);
  } else if (state.weekTick) {
    clearInterval(state.weekTick);
    state.weekTick = 0;
  }
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
  // 标签的定义(和进 prompt 的是同一份)—— 设置里那排标签靠它显示提示
  state.mailTagDefs = d.tag_defs || state.mailTagDefs || {};
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

  // ③ 分组 + 级别 + 标签 + 日程。分组排最前 —— 它比标签高一级
  const tagrow = el('div', 'mail-tagrow');
  // 这封信里的事进了日程表。**点一下直接跳到那一周** —— 看见「已进日程」
  // 第一个念头就是"进到哪天了",不给入口反而添堵
  if (m.dated) {
    const cal = el('button', 'dated-chip', '已进日程');
    cal.type = 'button';
    cal.title = '这封信里有带日期的事,已经画进日程表了 —— 点这里去看';
    cal.addEventListener('click', (ev) => {
      ev.stopPropagation();
      openWeek();
    });
    tagrow.appendChild(cal);
  }
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
    if (l.ai) {
      /* AI 挑中的:**一句话在上、地址在下**。
         一句话("填这个表报名 Research Rush,9/21 前截止")塞不进原来那个
         130px 的胶囊,而它才是你真正要读的东西 —— 所以让它占一整行,
         地址退成下面一行小字。地址仍然露出来:点之前得知道要去哪儿。 */
      const item = el('div', 'mail-linkai');
      const why = el('button', 'la-why');
      why.type = 'button';
      why.appendChild(el('span', 'la-icon', '🔗'));
      why.appendChild(el('span', null, l.ai));
      why.title = '打开 ' + l.url;
      why.addEventListener('click', () => openExternal(l.url));
      item.appendChild(why);
      const addr = el('div', 'la-url', l.url.replace(/^https?:\/\//, ''));
      addr.title = l.url;
      item.appendChild(addr);
      box.appendChild(item);
      return;
    }
    // 没挑过的(老结论):保持原来那一行的样子
    const line = el('div', 'mail-linkrow');
    line.appendChild(el('span', 'll-icon', '🔗'));
    line.appendChild(el('span', 'll-label', linkLabel(l)));
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
    // 弱化只在"上面已经有挑中的链接"时才成立 —— 一条都没挑中的时候,
    // 这一行是**唯一**的入口,再弱化就等于把链接藏了
    const onlyWay = picked && show.length === 0;
    const more = el('button', 'mail-linkmore'
      + (picked && !onlyWay ? ' is-filtered' : ''),
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

/* ── 安装完整性 ──

   装机脚本半路失败过(PowerShell 把一句正常提示当成致命错误,脚本在第 2 步
   终止)。那之后应用能跑,但 MCP 没注册、快捷方式没建 —— 而让人"再装一遍"
   是个很差的答复。所以应用自己查、自己补。 */

async function loadSetup() {
  try {
    const d = await apiGet('/api/setup');
    state.setup = d;
    renderSetup();
  } catch (e) { /* 查不了就算了 */ }
}

function renderSetup() {
  const d = state.setup || {};
  const miss = d.missing || [];
  const bar = $('fixBar');
  const show = miss.length && !state.setupDismissed;
  if (bar) {
    bar.hidden = !show;
    if (show) {
      $('fixText').textContent = '安装没做完:' + miss.join('、');
    }
  }
  const box = $('setupState');
  if (box) box.textContent = miss.length ? miss.join('、') : '安装是完整的';
}

async function fixSetup() {
  $('setupState').textContent = '正在补…';
  const bar = $('fixBar');
  if (bar && !bar.hidden) $('fixText').textContent = '正在补…';
  try {
    const r = await apiPost('/api/setup/fix', {});
    state.setup = { state: r.state, missing: [] };
    // **照实说**:哪些补上了、哪些没补上。只说一句"好了"是不负责任的
    const lines = (r.done || []).map((x) => '✓ ' + x)
      .concat((r.failed || []).map((x) => '✗ ' + x));
    $('setupState').textContent = lines.join(' · ') || '没什么要补的';
    if (r.failed && r.failed.length) {
      if (bar) { bar.hidden = false; $('fixText').textContent = '还差:' + r.failed.join('、'); }
    } else {
      state.setupDismissed = true;
      if (bar) bar.hidden = true;
    }
    await loadSetup();
    if (!(state.setup.missing || []).length) {
      $('setupState').textContent = lines.join(' · ') + ' —— 现在是完整的了';
      if (bar) bar.hidden = true;
    }
  } catch (e) {
    $('setupState').textContent = '没补成:' + ((e && e.message) || e);
  }
}

/* ── 更新 ──

   这条横幅**不复用 #banner**:那个是仪表盘报错的位置、每次刷新会被清掉,
   而"有新版本"应该一直挂着直到你处理它。 */

/* 版本号比大小。"3.0.10" 要排在 "3.0.9" 后面,所以不能按字符串比。 */
function cmpVer(a, b) {
  const pa = String(a).replace(/^v/, '').split('.').map(Number);
  const pb = String(b).replace(/^v/, '').split('.').map(Number);
  for (let i = 0; i < 3; i += 1) {
    const d = (pa[i] || 0) - (pb[i] || 0);
    if (d) return d;
  }
  return 0;
}

function renderUpdate(info, job) {
  // **空对象不许覆盖已经拿到的那份。** `{}` 在 JS 里是真值,原来写成
  // `info || state.upd` 就会被它冲掉 —— 而 push_update 在第一次成功检查
  // 之前送的正是 `{}`(backend.update_info 的初值),于是版本号那一行变空白
  if (info && Object.keys(info).length) state.upd = info;
  else if (!state.upd) state.upd = {};
  if (job && Object.keys(job).length) state.updJob = job;
  else if (!state.updJob) state.updJob = {};
  const i = state.upd;
  const j = state.updJob;
  const bar = $('updBar');
  const txt = $('updText');
  if (!bar) return;

  // 源码版快进完发现本来就是最新的 —— 说一声就收起来,不留在横幅上
  if (j.phase === 'done') {
    bar.classList.remove('is-working');
    bar.hidden = true;
    setUpdState(j.msg || '已经是最新的');
    return;
  }
  // 正在下载/安装:横幅原地变成进度,不另弹框
  if (j.phase && j.phase !== 'idle' && j.phase !== 'error') {
    bar.hidden = false;
    bar.classList.add('is-working');
    txt.textContent = (j.msg || '正在更新…')
      + (j.phase === 'downloading' && j.pct ? ` ${j.pct}%` : '');
    setUpdState(txt.textContent);
    return;
  }
  bar.classList.remove('is-working');
  if (j.phase === 'error' && j.error) {
    bar.hidden = false;
    txt.textContent = '更新没成功:' + j.error;
    setUpdState(txt.textContent);
    return;
  }

  // 「有更新」有两种。**源码版的更新不经过 Release** —— 代码一推上 main
  // 就能快进拿到,所以那种情况报的是提交数,不是版本号。
  const gitN = Number(i.git_behind || 0);
  // 源码版特有:代码已经是新的了,只是跑着的进程还是旧的(见后端 stale_process)
  const stale = !!i.stale_process;
  const relNew = !!(i.newer && i.latest) && !stale;
  const key = stale ? `stale:${i.latest}`
    : relNew ? i.latest : (gitN ? `git:${gitN}` : '');
  const show = !!key && skipKey() !== key;
  bar.hidden = !show;
  if (!show && $('updNotes')) $('updNotes').hidden = true;
  if (show) {
    txt.textContent = stale
      ? `代码已经更新到 v${i.disk_version || i.latest} —— 你这个窗口还跑着 v${i.current},重启一下生效`
      : relNew
        ? `有新版本 v${i.latest}`
          + (i.published ? `(${i.published})` : '')
          + ` —— 你现在是 v${i.current}`
        : `源码有 ${gitN} 个新提交 —— 点「更新并重启」快进到最新`;
    // 源码版没有 Release notes 可看,提交标题就是"改了什么"
    bar.title = relNew ? '' : (i.git_subjects || []).join('\n');
  }
  // **两个版本号永远都摆出来:我现在是多少、最新是多少。**
  //
  // 原来是按状态拼不同的话,结果 stale 那一支写成「当前 vX · 代码已是 vY」,
  // 而 Y 取的是 **Release** 的版本号 —— 可"代码已是"说的明明是**磁盘上**那份。
  // 两个数一样的时候(刚发完版、Release 和磁盘同步了)就成了
  // 「当前 v3.0.2 · 代码已是 v3.0.2」,同一个号说两遍,等于没说。
  //
  // 现在:当前取内存里的,最新取 **Release 和磁盘里更大的那个**(源码版的磁盘
  // 可能比任何 Release 都新),后面才跟一句"该怎么办"。
  const cur = i.current || state.updVersion || '';
  const newest = [i.latest, i.disk_version].filter(Boolean)
    .sort(cmpVer).pop() || '';
  const bits = [];
  if (cur) bits.push(`当前 v${cur}`);
  if (newest) bits.push(`最新 v${newest}`);
  const tail = stale ? '重启生效'
    : relNew ? '可以更新'
      : gitN ? `源码落后 ${gitN} 个提交`
        : (newest && cur && newest === cur ? '已是最新' : '');
  setUpdState(bits.join(' · ') + (tail ? ` —— ${tail}` : ''));
  // **一个按钮,文案随状态变。** 用户不该先判断"我属于哪种情况"再选按钮:
  //   有新版本 / 源码落后 -> 更新并重启(先换代码,再重起,顺序就是这个)
  //   代码已新、进程旧     -> 重启生效(没东西可换,只差重起)
  const label = stale ? '重启生效' : '更新并重启';
  ['updGo', 'btnUpdGo'].forEach((id) => {
    const b = $(id);
    if (!b) return;
    b.hidden = !(relNew || gitN || stale);
    b.textContent = label;
    b.title = stale
      ? '代码已经是新的了,只是这个窗口还跑着旧的 —— 点一下重起一份'
      : '换成新代码并自动重启,你的邮件/对话/课件/设置都不动';
  });
  // 「改了什么」只在有 Release 页可看的时候给 —— 源码版点开会落到
  // 上一个 Release 的页面,那是在说谎
  // 「改了什么」两种都给:Release 有 notes,源码版有提交标题
  const what = $('updWhat');
  if (what) what.hidden = !(relNew ? (i.notes || (i.history || []).length)
    : (i.git_subjects || []).length);
}

/* 「跳过这个版本」记在 prefs 里,重开还算数。源码版的提交数每次 fetch
   都在变,记不住也不该记 —— 那种只在这一次会话里收起来。 */
function skipKey() {
  const i = state.upd || {};
  return (i.newer && i.latest) ? (state.prefs.skipVersion || '') : state.updDismissed;
}

/* 点了右下角那条更新弹窗之后落到横幅上。

   只把窗口叫回来是不够的:横幅在学业页的仪表盘里,而你可能正停在邮箱页
   或者课程表上 —— 那条弹窗就白弹了。所以这里把路铺完:切回学业页、退出
   子视图、把横幅滚进视野、闪一下。 */
function gotoWhere(where) {
  // 新邮件那条弹窗:点一下直接开那封信。`mail:<id>` = 开这一封,
  // 光是 `mail` = 回邮箱列表(一次进来好几封时,点开某一封不一定是他要的那封)
  if (where === 'mail' || where.indexOf('mail:') === 0) {
    setPage('mail');
    const id = where.slice(5);
    if (id) openMailById(id);
    return;
  }
  if (where !== 'update') return;
  setPage('study');
  if (!$('weekView').hidden) closeWeek();
  $('courseView').hidden = true;
  $('dashView').hidden = false;
  const bar = $('updBar');
  if (!bar || bar.hidden) return;
  try {
    bar.scrollIntoView({ block: 'nearest' });
  } catch (e) { /* 老 webview 没有这个参数,滚不动就算了 */ }
  bar.classList.remove('is-flash');
  // 重排一次再加回来,连点两下才会重新播动画
  void bar.offsetWidth;
  bar.classList.add('is-flash');
  setTimeout(() => bar.classList.remove('is-flash'), 1600);
}

/* 把「你这个版本到最新版之间」每一版的说明画出来。

   history 是后端拼好的(updater._notes_since),新的在前。老版本的 info
   里没有这个字段 —— 那就退回只显示最新那一份。 */
function renderUpdNotes() {
  const box = $('updNotes');
  const i = state.upd || {};
  box.textContent = '';
  const hist = (i.history || []).filter((h) => h && (h.notes || h.version));
  // 源码版没有 Release notes,提交标题就是"改了什么"
  if (!hist.length && (i.git_subjects || []).length) {
    box.appendChild(el('div', 'md-p',
      `源码有 ${i.git_behind} 个新提交,最近几条:`));
    const ul = el('ul', 'md-ul');
    i.git_subjects.forEach((t) => ul.appendChild(el('li', null, t)));
    box.appendChild(ul);
    return;
  }
  if (!hist.length && i.notes) {
    hist.push({ version: i.latest || '', date: i.published || '', notes: i.notes });
  }
  if (!hist.length) {
    box.appendChild(el('div', 'muted', '这一版没写更新说明。'));
    return;
  }
  box.appendChild(el('div', 'md-p',
    hist.length > 1 ? `检测到更新,${hist.length} 个版本的更新内容如下:`
      : '检测到更新,更新内容如下:'));
  hist.forEach((h) => {
    box.appendChild(el('div', 'upd-notes-v',
      `v${h.version}${h.date ? ' · ' + h.date : ''}`));
    const body = el('div');
    renderMarkdown(h.notes || '(没写说明)', body);
    box.appendChild(body);
  });
  if (i.page) {
    const a = el('button', 'link-btn', '在 GitHub 上看这个 Release');
    a.type = 'button';
    a.addEventListener('click', () => openExternal(i.page));
    box.appendChild(a);
  }
}

function setUpdState(t) {
  const box = $('updState');
  if (box) box.textContent = t || '';
}

async function loadUpdate() {
  try {
    const d = await apiGet('/api/update');
    // 装机脚本叫什么(分平台)—— 报错文案里要念它
    if (d.setup_script) state.setupScript = d.setup_script;
    // 后端每次都报当前版本 —— 存下来,info 里没有 current 时用它兜底
    if (d.version) state.updVersion = d.version;
    state.updKind = d.kind;
    renderUpdate(d.info || {}, d.job || {});
    if (!(d.info || {}).latest) setUpdState(`当前 v${d.version}`);

  } catch (e) { /* 查不到就算了,不是错误 */ }
}

async function checkUpdate() {
  setUpdState('正在问 GitHub…');
  try {
    const d = await apiPost('/api/update/check', {});
    state.updKind = d.kind;
    // 手动点了「现在检查」就别再被"这次先不提"挡着
    state.updDismissed = '';
    // 手点了检查 = 他现在就想知道,把之前跳过的那个版本解开
    if (state.prefs.skipVersion) savePrefs({ skipVersion: '' });
    renderUpdate(d.info || {}, {});
    if (!(d.info || {}).ok && (d.info || {}).error) {
      setUpdState('查不到:' + d.info.error);
    }
  } catch (e) {
    setUpdState('查不到:' + ((e && e.message) || e));
  }
}

async function applyUpdate() {
  const i = state.upd || {};
  const kind = state.updKind;
  // 源码版不下 zip,走 git 快进 —— 所以别拿包的大小吓唬人,也别提"替换"
  const ask = kind === 'git'
    ? `更新到 v${i.latest}:取最新代码、快进、自动重启。\n`
      + '你的邮件、对话、课件、CLAUDE.md 都不动(它们不在 git 里)。\n'
      + '本地改过的文件会被拦下来,不会覆盖。\n\n继续吗?'
    : `下载 v${i.latest} 并替换当前版本(约 ${Math.round((i.size || 0) / 1048576)} MB)。\n`
      + '装好会自动重启。你的邮件、对话、课件、设置都不动。\n\n继续吗?';
  if (!window.confirm(ask)) return;
  try {
    const r = await apiPost('/api/update/apply', {});
    if (!r.ok && r.error) setUpdState(r.error);
    renderUpdate(state.upd, r.job || {});
  } catch (e) {
    setUpdState('没跑起来:' + ((e && e.message) || e));
  }
}

/* ── 单封视图 ──

   占满左栏,右栏的对话不受影响:只是把 #paneMail 里列表那几块藏掉
   (靠 .is-one 这个类),不动 grid 布局。退出回列表,滚动位置也还原。 */

/* 从日程表跳回原信。只有邮件 id —— 列表是分页的,那封可能不在当前这页,
   所以直接按 id 问后端要一封。 */
async function openMailById(mid) {
  setPage('mail');
  try {
    const r = await apiGet('/api/mail/one', { id: mid });
    if (r && r.message) {
      openMailOne(r.message);
      return;
    }
  } catch (e) {
    showMailBanner('打不开那封信:' + ((e && e.message) || e));
    return;
  }
  showMailBanner('那封信已经不在本地索引里了');
}

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

  // 级别 + 标签 + 日程
  const tags = el('div', 'one-tags');
  if (m.dated) {
    const cal = el('button', 'dated-chip', '已进日程');
    cal.type = 'button';
    cal.title = '这封信里有带日期的事,已经画进日程表了 —— 点这里去看';
    cal.addEventListener('click', () => openWeek());
    tags.appendChild(cal);
  }
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
      + (a.cost ? ` · 累计 $${a.cost}` : '');
    bar.hidden = false;
  } else if (err) {
    // errors 每一轮开始时会清空,所以这条一定是**最近那一轮**的。
    // 还有待办就提一句可以再催 —— 偶发的解析失败重试一次多半就过了。
    bar.textContent = '过目失败:' + err
      + (a.pending ? ` —— 还有 ${a.pending} 封没过目,设置 → 邮箱可以再催一次` : '');
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
  state.mailBriefRaw = '';
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
  state.mailBriefRaw = d.text || '';
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
  state.mailBriefRaw = state.mailBriefLive;
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
      finishBubble(state.mailBubble, state.mailBubbleRaw);
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

async function sendMailChat(text, opts) {
  text = (text || '').trim();
  if (!text || state.mailStreaming) return;
  const edit = opts && Number.isInteger(opts.editIndex) ? opts.editIndex : -1;
  const ctx = (edit >= 0 || !state.mailCtxSent) ? state.mailCtx.slice() : [];
  addMsgIn($('mailChatLog'), 'user', text, state.mailCtx.slice());
  $('mailChatInput').value = '';
  autoGrowEl($('mailChatInput'));
  setMailStreaming(true);
  setMailChatStatus('连接中…');
  let r;
  try {
    r = await apiPost(edit >= 0 ? '/api/mailchat/edit' : '/api/mailchat',
                      { message: text, context: ctx, index: edit });
    if (ctx.length) state.mailCtxSent = true;
  } catch (e) {
    addMsgIn($('mailChatLog'), 'error', '后端没响应:' + e.message);
    setMailStreaming(false);
    if (edit >= 0) openMailChat(state.mailChatId);
    return;
  }
  if (!r.ok) {
    addMsgIn($('mailChatLog'), 'error', r.message);
    setMailStreaming(false);
    if (edit >= 0) openMailChat(state.mailChatId);
  }
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
  $('btnCopyMailBrief').addEventListener('click', (e) =>
    copyInto(e.currentTarget, state.mailBriefRaw));
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
  $('btnMailRedate').addEventListener('click', () => runRedate());

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
  state.briefRaw = '';
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
    state.briefRaw = '';
    box.textContent = '';
    box.appendChild(el('div', 'brief-empty', '读取这一天的简报失败。'));
    return;
  }
  state.briefRaw = d.text || '';
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
  if (ev.kind === 'sched') {
    renderSchedState(ev.sched || {});
    return;
  }
  if (ev.kind === 'update') {
    renderUpdate(ev.info || {}, ev.job || {});
    return;
  }
  if (ev.kind === 'goto') {
    gotoWhere(ev.where || '');
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

   所以改成自己跟:pointerdown 记起点,后端自己 GetCursorPos 算位移。坐标一个
   都不传 —— screenX 是 CSS 像素、窗口要物理像素,换算在多显示器 + 175% 缩放
   下很容易算错。

   **中间那一段现在由后端自己转。** 原来是 pointermove 里用 rAF 合并到
   ~60/s、每帧发一条 POST。那条路会一顿一顿地追手:werkzeug 的开发服务器是
   HTTP/1.0,每个响应都 Connection: close,于是每帧都要新建一条 loopback TCP
   连接 —— 而这台机器上新建连接的 p90 是 **509ms**(建好的连接上跑一个来回
   只要 0.047ms)。实测 2 秒里 60 次请求只有 27 次跟得上。
   所以 drag/start 会回一个 follow:后端自己跟的话,前端整个拖动过程只发两条
   请求。macOS 那边 AppKit 不是线程安全的,后台线程挪窗口要崩,所以那边
   follow 是 false,仍然走每帧一条的老路。 */

function wireDragRegions() {
  document.querySelectorAll('.drag-region').forEach((node) => {
    let dragging = false;
    let armed = false;      // 按下了但还没超过阈值
    let sx = 0;
    let sy = 0;
    let ticking = false;
    // 后端自己跟吗。null = start 还没回话 —— 那段时间**什么都不发**:
    // Windows 上后端已经在跟了,再发是白撞那条慢路;macOS 上顶多晚一帧
    let follow = null;

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
        follow = null;
        apiPost('/api/window/drag/start')
          .then((r) => { follow = !!(r && r.follow); })
          .catch(() => { follow = false; });
        return;
      }
      // 后端自己跟着光标走(或者还不知道)—— 一条请求都不用再发
      if (follow !== false) return;
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
      follow = null;
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
  theme: 'auto', lang: 'zh', mode: 'full', blur: 26, glass: 0.55, anim: true,
  orbSize: 36, briefHour: 9, topmost: true, showDismissed: true, opacity: 1,
  briefHistory: 3, upcomingDays: 28, soonDays: 3, annDays: 10,
  toastOn: true, toastSecs: 9, updateCheck: true, updateToast: true,
  skipVersion: '',
  focusCourses: '', prefsTab: 'general',
  mailFacts: [], mailTags: [], mailAiOn: true, mailModel: 'sonnet',
  schedModel: 'sonnet', schedFull: false, schedMemos: true, schedMail: true,
  mailMarkRead: true,
  mailSort: 'date_desc',
};

function applyPrefs(prefs) {
  state.prefs = Object.assign({}, PREF_FALLBACK, prefs || {});
  const root = document.documentElement;
  const p = state.prefs;

  // neu = 校徽那三色(黑白红)的深色皮,和 light/dark 并列摆在设置里
  root.dataset.theme = ['light', 'dark', 'neu'].includes(p.theme) ? p.theme : 'auto';
  // 语言只在启动时接一次。切语言走的是「存偏好 + 刷新页面」那条路
  // (见 wirePrefs),所以这里不用管从英文切回中文
  if (!state.i18nOn) { state.i18nOn = true; startI18n(p.lang); }
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

/* 开机自启的开关。**每次都现问后端**,不缓存在 prefs 里 —— 这件事的真相在
   操作系统那边(启动文件夹里的那个快捷方式),用户可能自己去删掉。 */
async function syncAutostart() {
  const b = $('swAutostart');
  if (!b) return;
  try {
    const d = await apiGet('/api/autostart');
    b.setAttribute('aria-checked', String(!!d.on));
    b.title = d.path || '';
  } catch (e) { /* 拿不到就保持现状,别把开关摆成误导性的状态 */ }
}

function syncPrefsUI() {
  const p = state.prefs;
  syncAutostart();
  const theme = p.theme || 'auto';
  document.querySelectorAll('#segTheme .seg-btn').forEach((b) => {
    b.classList.toggle('is-on', b.dataset.v === theme);
    b.setAttribute('aria-checked', String(b.dataset.v === theme));
  });
  document.querySelectorAll('#segLang .seg-btn').forEach((b) => {
    const on = b.dataset.v === (p.lang === 'en' ? 'en' : 'zh');
    b.classList.toggle('is-on', on);
    b.setAttribute('aria-checked', String(on));
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
  // 这两个原来一个都没回填 —— HTML 里写死 aria-checked="true",于是把
  // 「自动检查更新」关掉、重开面板它还显示是开的
  setSwitch('swUpdateCheck', p.updateCheck !== false);
  setSwitch('swUpdateToast', p.updateToast !== false);
  setSwitch('swToast', p.toastOn !== false);
  $('numToastSecs').value = p.toastSecs;
  setSwitch('swDismissed', p.showDismissed !== false);
  setSwitch('swTopmost', p.topmost !== false);
  setSwitch('swSync', p.autoSync !== false);
  $('selSchedModel').value = p.schedModel || 'sonnet';
  setSwitch('swSchedMemos', p.schedMemos !== false);
  setSwitch('swSchedMail', p.schedMail !== false);
  if ($('schedPrefState')) {
    const sc = state.sched;
    $('schedPrefState').textContent = !sc ? ''
      : sc.running ? `解析中 ${sc.done}/${sc.total}`
        : sc.last ? `上次 ${sc.last}` : '';
  }
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
  // 悬停看定义。**和进 prompt 的是同一份** —— 模型按这句话判,你也按这句话
  // 理解,不会两头对不上。模型自己造的标签没有定义,那就说清它是造出来的
  const def = (state.mailTagDefs || {})[t];
  chip.title = def ? `${t} —— ${def}` : `${t}(分析时自己造的标签,没有内置定义)`;
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

/* 补抽日程。**和「全部重判」分开**:只动最近 80 封里还没抽过日程的那些,
   老存量不重付一次钱。日程和标签、摘要是同一次调用的产出,所以这几封的
   标签会顺带按新规则更新 —— 那不是浪费。 */
async function runRedate() {
  if (!window.confirm(
    '给最近 80 封里还没抽过日程的邮件补一次。\n'
    + '面试、讲座、交表截止会被画进日程表。\n'
    + '这会调一次模型,要花钱(通常几毛)。\n'
    + '这几封的标签和摘要会顺带按新规则重判。\n\n继续吗?')) return;
  $('aiState').textContent = '正在补抽日程…';
  try {
    const r = await apiPost('/api/mail/analyze', { redate: true });
    if (!r.dropped) {
      $('aiState').textContent = '没有需要补的 —— 最近这些信都抽过日程了';
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
      + (a.cost ? ` · 累计 $${a.cost}` : '');
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

  // 切语言:存下来,然后**整页重来一遍**。
  // 表是单向的(中文 -> 英文),往回切需要反查,而反查在"两句中文翻成同一句
  // 英文"时是二义的。刷新则拿到一份干净的中文 DOM,动态内容本来就是从后端
  // 重新取的,什么都不丢 —— 详见 i18n.js 开头。
  document.querySelectorAll('#segLang .seg-btn').forEach((b) => {
    b.addEventListener('click', async () => {
      if (b.dataset.v === (state.prefs.lang === 'en' ? 'en' : 'zh')) return;
      $('langHint').textContent = '切换中…';
      await savePrefs({ lang: b.dataset.v });
      location.reload();
    });
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
  $('selSchedModel').addEventListener('change', (e) => {
    savePrefs({ schedModel: e.target.value });
  });
  // 备忘录进不进格子是纯显示问题,不用重新解析,原地重画就行
  toggle('swSchedMemos', 'schedMemos', () => {
    if (!$('weekView').hidden) renderWeek();
  });
  // 画不画是纯显示问题,原地重画就行 —— 日程本身早抽好了
  toggle('swSchedMail', 'schedMail', () => {
    if (!$('weekView').hidden) renderWeek();
    renderWeekEntry();
  });
  $('btnSchedParse').addEventListener('click', async () => {
    $('schedPrefState').textContent = '解析中…';
    try {
      const r = await apiPost('/api/schedule/parse', { manual: true });
      if (r && r.state) renderSchedState(r.state);
    } catch (e) {
      $('schedPrefState').textContent = (e && e.message) || String(e);
    }
  });
  // 清空连手改和手加的一起没,所以按两下才算数(和删对话那个按钮一个套路)
  const resetBtn = $('btnSchedReset');
  let schedArmed = 0;
  resetBtn.addEventListener('click', async () => {
    if (Date.now() - schedArmed > 3000) {
      schedArmed = Date.now();
      resetBtn.textContent = '连手改的一起清?';
      setTimeout(() => {
        if (Date.now() - schedArmed >= 3000) resetBtn.textContent = '清空课表';
      }, 3100);
      return;
    }
    schedArmed = 0;
    resetBtn.textContent = '清空课表';
    await apiPost('/api/schedule/item', { action: 'reset' });
    await loadWeek();
    $('schedPrefState').textContent = '已清空';
  });
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

  $('btnSetupFix').addEventListener('click', () => fixSetup());
  $('fixGo').addEventListener('click', () => fixSetup());
  $('fixLater').addEventListener('click', () => {
    state.setupDismissed = true;
    $('fixBar').hidden = true;
  });
  $('btnUpdCheck').addEventListener('click', () => checkUpdate());
  $('btnUpdGo').addEventListener('click', () => applyUpdate());
  $('updGo').addEventListener('click', () => applyUpdate());
  $('updWhat').addEventListener('click', () => {
    const box = $('updNotes');
    if (!box) return;
    // 就地展开。原来是直接开浏览器 —— 但"更新内容"这种东西就该在应用里
    // 看得到,跳出去看还得自己找回来
    if (!box.hidden) { box.hidden = true; return; }
    renderUpdNotes();
    box.hidden = false;
  });
  $('updLater').addEventListener('click', () => {
    // 「跳过这个版本」。**只跳过这一个版本** —— 下一个版本还会再来,
    // 所以它不是那种"设完就忘、再也收不到更新"的全局开关。
    // Release 的记进 prefs(重开还算数);源码那种报的是提交数,每次
    // fetch 都在变,记住没意义,只收起这一次。
    const i = state.upd || {};
    if (i.newer && i.latest) savePrefs({ skipVersion: i.latest });
    else state.updDismissed = `git:${Number(i.git_behind || 0)}`;
    $('updBar').hidden = true;
    $('updNotes').hidden = true;
  });
  toggle('swUpdateCheck', 'updateCheck');
  toggle('swUpdateToast', 'updateToast');
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

  // 开机自启。**按后端回的实际状态摆**,不是按点击意图摆 —— 建快捷方式可能
  // 失败(启动文件夹被组策略锁了之类),那时候开关就该弹回去,而不是显示成
  // "开着"骗人
  $('swAutostart').addEventListener('click', async () => {
    const b = $('swAutostart');
    const want = b.getAttribute('aria-checked') !== 'true';
    b.disabled = true;
    try {
      const r = await apiPost('/api/autostart', { on: want });
      b.setAttribute('aria-checked', String(!!r.on));
      b.title = r.path || '';
      if (r.ok === false) hint(want ? '没能加上开机项' : '没能去掉开机项');
    } catch (e) {
      hint('改不了开机项');
    } finally {
      b.disabled = false;
    }
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

  wireCopy();
  wireDragRegions();
  wireTitlebarDblClick();
  wirePrefs();
  wireVisibilitySync();
  wireChatHistory();
  wireDropZone();
  wireCourseView();
  wireWeek();
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

  // 复制的是 markdown 原文,不是渲染后的样子 —— 粘进备忘录或者微信都还能看
  $('btnCopyBrief').addEventListener('click', (e) =>
    copyInto(e.currentTarget, state.briefRaw));

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
  // 有没有新版本。**只读后端存着的那份结果,不触发联网** ——
  // 真正去问 GitHub 是后台每 6 小时一次的事
  loadUpdate();
  // 安装完整吗(要跑一次 claude mcp list,慢一点,所以放在后面)
  loadSetup();
  // 对话历史:启动就把上次那段读回来,不是空白开始
  await loadChatIndex();
  if (state.chatId) await openChat(state.chatId);
  // 简报由后端调度(每天 09:00,一天一次),前端只负责取来展示。
  // 如果此刻后端正在生成,SSE 的 delta 会直接把内容画进来。
  await loadBriefIndex();
  // 备忘录:角标要有数,展开过的话列表也一起拉回来
  await loadMemos();
  // 课表:仪表盘上那行「今天接下来有什么」要有内容,所以启动就读一次。
  // 只读存档,不跑模型 —— 解析是你点「重新解析」才发生的事
  state.weekStart = wkYmd(wkSunday(new Date()));
  loadWeek();
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
