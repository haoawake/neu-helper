/* Today is a local view over existing data. All remote sync is explicit. */
'use strict';
(() => {
  let busy = false, mutation = false, requestId = 0;
  const expanded = new Set();
  const words = {
    title: ['今日行动清单', 'Today’s actions'], refresh: ['同步来源', 'Sync sources'],
    intro: ['先处理今天的事。操作仅影响本清单，不会提交作业或修改邮箱。', 'Plan your day. Actions affect this list only; they do not submit coursework or change your mailbox.'],
    overdue: ['已逾期', 'Overdue'], today: ['今天', 'Today'], unscheduled: ['待安排', 'To plan'],
    upcoming: ['未来 7 天', 'Next 7 days'], info: ['仅供查看', 'For information'],
    snoozed: ['稍后处理', 'Snoozed'], handled: ['已处理', 'Handled'],
    canvas: ['Canvas', 'Canvas'], mail: ['邮件', 'Mail'], memo: ['备忘录', 'Memo'], schedule: ['日程', 'Schedule'],
    canvas_pending: ['Canvas 显示未提交', 'Canvas shows not submitted'],
    watched: ['你标记了重点', 'You marked this for follow-up'],
    mail_info: ['近期重要未读邮件；不自动判定为待办', 'Recent important unread mail; not automatically a task'],
    mail_event: ['从邮件提取；请核对原文时间', 'Extracted from mail; check the original time'],
    open: ['查看来源', 'View source'], done: ['本地完成', 'Done locally'], ignored: ['已忽略', 'Ignored'],
    source_done: ['来源已完成', 'Done in source'], ignore: ['忽略', 'Ignore'], restore: ['撤回', 'Undo'],
    snooze: ['稍后…', 'Snooze…'], hour: ['1 小时后', 'In 1 hour'], tomorrow: ['明天 09:00', 'Tomorrow at 09:00'],
    error: ['清单暂时无法更新，已保留当前内容。请重试。', 'Could not update the list. Previous content is retained. Please retry.'],
    empty: ['当前已知数据中，没有需要处理的事项。请同时查看上方同步状态。', 'No actions in the available data. Check sync status above.'],
    loading: ['同步中', 'Syncing'], success: ['同步成功', 'Synced'], partial: ['部分同步失败', 'Partly failed'],
    failed: ['同步失败 · 保留旧数据', 'Sync failed · previous data retained'], idle: ['尚未同步', 'Not synced yet'],
    cached: ['本地缓存 · 待同步', 'Cached · sync pending'], none: ['未配置', 'Not configured'],
    local: ['本地保存', 'Saved locally'], parsed: ['最近提取', 'Last extracted'], unknown: ['尚未提取课程安排', 'Course schedule not extracted yet'],
    stale: ['数据已超过 30 分钟', 'Data is over 30 minutes old'],
  };
  const en = () => state.prefs?.lang === 'en';
  const t = key => (words[key] || [key, key])[en() ? 1 : 0];
  const node = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text) n.textContent = text; return n; };
  const clock = raw => { if (!raw) return ''; const d = new Date(raw); return Number.isNaN(+d) ? raw : d.toLocaleString(en() ? 'en-US' : 'zh-CN', {month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit'}); };
  const button = (text, fn) => { const b = node('button', 'link-btn', text); b.type = 'button'; b.addEventListener('click', fn); return b; };

  function sourceStatus(label, status, at, message) {
    const box = node('div', 'today-source');
    box.append(node('strong', '', label), node('span', '', status));
    if (at) box.append(node('small', '', clock(at)));
    if (message) { const more = node('details'); more.append(node('summary', '', en() ? 'Details' : '详情'), node('small', '', message)); box.append(more); }
    return box;
  }
  function renderSources(s) {
    const host = $('todaySources');
    const wasOpen = host.querySelector('details')?.open || false;
    host.replaceChildren();
    const details = node('details', 'today-source-details'); details.open = wasOpen;
    const box = node('div', 'today-source-grid');
    const c = s.canvas || {};
    const stale = c.last_success && Date.now() - +new Date(c.last_success) > 30 * 60000;
    const mailBad = (s.accounts || []).some(a => a.last_error) || s.mail?.errors?.length;
    const stateKey = c.state === 'error' ? 'failed' : c.state === 'idle' && c.last_success ? 'cached' : c.state || 'idle';
    details.append(node('summary', '', 'Canvas · ' + t(stateKey) + (stale ? ' · ' + t('stale') : '') + (mailBad ? ' / ' + t('mail') + ' · ' + t('failed') : '')));
    details.append(box); host.append(details);
    box.append(sourceStatus('Canvas', t(c.state === 'error' ? 'failed' : c.state === 'idle' && c.last_success ? 'cached' : c.state || 'idle') + (stale ? ' · ' + t('stale') : ''), c.last_success, c.message));
    const accounts = s.accounts || [];
    if (!accounts.length) box.append(sourceStatus(t('mail'), t('none')));
    accounts.forEach(a => {
      const old = a.last_sync && Date.now() - +new Date(a.last_sync) > 30 * 60000;
      box.append(sourceStatus(a.label, t(s.mail?.running ? 'loading' : a.last_error ? 'failed' : !a.ready ? 'none' : a.last_sync ? 'success' : 'idle') + (old ? ' · ' + t('stale') : ''), a.last_sync, a.last_error));
    });
    if (s.mail?.errors?.length) box.append(sourceStatus(t('mail'), t('failed'), '', s.mail.errors.join('\n')));
    const dates = (s.schedule?.parsed || []).map(p => p.at).filter(Boolean).sort();
    const ss = s.schedule?.state || {};
    box.append(sourceStatus(t('schedule'), ss.running ? t('loading') : dates.length ? t('parsed') : t('unknown'), dates[0], (ss.errors || []).join('\n')));
  }
  async function change(row, status, until) {
    if (mutation) return;
    mutation = true;
    $('todayItems').querySelectorAll('button, select').forEach(b => b.disabled = true);
    try { await apiPost('/api/today/action', {id: row.id, status, until}); await load(); }
    catch (_) { $('todayError').textContent = t('error'); $('todayError').hidden = false; }
    finally { mutation = false; $('todayItems').querySelectorAll('button, select').forEach(b => b.disabled = false); }
  }
  async function source(row) {
    const target = row.target || {};
    if (target.type === 'url') {
      if (/^https?:\/\//i.test(target.url || '')) openExternal(target.url);
      else hint(en() ? 'Source link unavailable' : '来源链接暂不可用');
    } else if (target.type === 'mail') {
      if (target.archived && /^https?:\/\//i.test(target.url || '')) openExternal(target.url);
      else if (target.id) await openMailById(target.id);
      else setPage('week');
    } else if (target.type === 'memo') {
      setPage('study'); switchTab('memo'); await loadMemos();
      const index = state.memos.findIndex(m => m.id === target.id);
      const card = $('memoList').children[index];
      if (card) { card.scrollIntoView({block:'center'}); card.animate([{outline:'2px solid var(--accent)'},{outline:'2px solid transparent'}], {duration:1800}); }
    } else {
      if (target.date) state.weekStart = wkYmd(wkSunday(new Date(target.date + 'T12:00:00')));
      setPage('week');
    }
  }
  function card(row) {
    const box = node('article', 'today-card');
    box.dataset.actionId = row.id;
    const due = row.allday ? new Date(row.due).toLocaleDateString(en() ? 'en-US' : 'zh-CN') + (en() ? ' · All day' : ' · 全天') : clock(row.due);
    box.append(node('div', 'today-card-meta', t(row.source) + (row.due ? ' · ' + due : '')),
               node('h3', '', row.title), node('p', 'today-reason', t(row.reason)));
    const actions = node('div', 'today-actions');
    actions.append(button(t('open'), () => source(row)));
    if (['done','ignored','source_done','snoozed'].includes(row.status)) {
      actions.append(node('span', 'muted', t(row.status) + (row.status === 'snoozed' ? ' · ' + clock(row.until) : '')));
      if (row.status !== 'source_done') actions.append(button(t('restore'), () => change(row, 'open')));
    } else {
      actions.append(button(t('done'), () => change(row, 'done')));
      const select = node('select', 'select today-snooze'); select.setAttribute('aria-label', t('snooze') + row.title);
      [['', 'snooze'],['hour','hour'],['tomorrow','tomorrow']].forEach(([v,k]) => { const o = node('option', '', t(k)); o.value = v; select.append(o); });
      select.addEventListener('change', () => {
        if (!select.value) return;
        const until = new Date();
        if (select.value === 'hour') until.setHours(until.getHours()+1);
        else { until.setDate(until.getDate()+1); until.setHours(9,0,0,0); }
        select.value = ''; change(row, 'snoozed', until.toISOString());
      });
      actions.append(select, button(t('ignore'), () => change(row, 'ignored')));
    }
    box.append(actions); return box;
  }
  function render(data) {
    $('todayPanel').setAttribute('aria-label', t('title'));
    $('todayTitle').textContent = t('title'); $('todayIntro').textContent = t('intro');
    $('todayRefresh').textContent = t('refresh'); renderSources(data.sources || {});
    const counts = data.counts || {};
    $('todaySummary').textContent = `${data.date} · ${t('overdue')} ${counts.overdue || 0} · ${t('today')} ${counts.today || 0} · ${t('unscheduled')} ${counts.unscheduled || 0}`;
    // Nav badge: open items that are overdue or due today, same basis as the summary line.
    const due = (counts.overdue || 0) + (counts.today || 0);
    $('todayBadge').textContent = String(due); $('todayBadge').hidden = due <= 0;
    const list = $('todayItems'); list.replaceChildren();
    if (!data.items.length) list.append(node('p', 'muted', t('empty')));
    ['overdue','today','unscheduled','upcoming','info','snoozed','handled'].forEach(group => {
      const rows = data.items.filter(r => r.group === group); if (!rows.length) return;
      const folded = ['upcoming','info','snoozed','handled'].includes(group);
      const section = node(folded ? 'details' : 'section', 'today-group');
      section.append(node(folded ? 'summary' : 'h3', '', `${t(group)} · ${rows.length}`));
      if (folded) { section.open = expanded.has(group); section.addEventListener('toggle', () => { if (!section.isConnected) return; section.open ? expanded.add(group) : expanded.delete(group); }); }
      rows.forEach(row => section.append(card(row))); list.append(section);
    });
  }
  async function load() {
    const id = ++requestId;
    busy = true;
    try { const data = await apiGet('/api/today'); if (id === requestId) {
      render(data); $('todayError').hidden = !data.state_error;
      if (data.state_error) $('todayError').textContent = en() ? 'Saved actions could not be read. Your existing file has been preserved; changes are unavailable.' : '无法读取已保存的处理记录。原文件已保留，暂时无法修改清单状态。';
    } }
    catch (_) { if (id === requestId) { $('todayError').textContent = t('error'); $('todayError').hidden = false; } }
    finally { if (id === requestId) busy = false; }
  }
  $('todayRefresh').addEventListener('click', async () => {
    const btn = $('todayRefresh'); btn.disabled = true;
    try { await apiPost('/api/today/refresh'); await load(); }
    catch (_) { $('todayError').textContent = t('error'); $('todayError').hidden = false; }
    finally { btn.disabled = false; }
  });
  window.addEventListener('today-data-changed', load);
  setInterval(() => { if (!document.hidden && !busy && !mutation && !document.activeElement?.closest('#todayItems')) load(); }, 15000);
  load();
})();
