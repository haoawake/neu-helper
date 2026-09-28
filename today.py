"""Local action list assembled from existing sources; never calls a model."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path

import memos


def timestamp(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).astimezone()
    except (TypeError, ValueError):
        return None


class ActionStore:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.data = {}
        self.error = ''
        if self.path.exists():
            # A damaged file must not be silently replaced, losing user decisions.
            try:
                self.data = json.loads(self.path.read_text(encoding='utf-8-sig'))
                if not isinstance(self.data, dict) or any(not isinstance(v, dict) for v in self.data.values()):
                    raise ValueError('Invalid action state')
            except (OSError, ValueError) as exc:
                self.data = {}
                self.error = f'Cannot read saved actions: {exc}'

    def all(self):
        with self.lock:
            return {k: dict(v) for k, v in self.data.items()}

    def set(self, key, status, until=None, item=None):
        if self.error:
            raise ValueError(self.error)
        if status not in ('open', 'done', 'ignored', 'snoozed'):
            raise ValueError('Invalid action')
        when = timestamp(until)
        if status == 'snoozed' and (not when or when <= datetime.now().astimezone()):
            raise ValueError('Choose a future reminder time')
        with self.lock:
            updated = dict(self.data)
            if status == 'open':
                updated.pop(key, None)
            else:
                updated[key] = {'status': status, 'until': when.isoformat() if when else None}
                if item:
                    updated[key]['item'] = dict(item)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix('.tmp')
            tmp.write_text(json.dumps(updated, ensure_ascii=False), encoding='utf-8')
            tmp.replace(self.path)
            self.data = updated


def build(*, canvas, messages, flags, memo_items, schedule, events, marks, now=None):
    now = now or datetime.now().astimezone()
    end = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    horizon = end + timedelta(days=6)
    rows = []

    def add(key, title, source, due, reason, target, *, info=False, native_done=False, allday=False):
        dt = timestamp(due)
        # Include handled rows even if they have moved outside the usual horizon.
        mark = marks.get(key, {})
        if dt and dt >= horizon and not mark:
            return
        status = mark.get('status', 'open')
        until = timestamp(mark.get('until'))
        if status == 'snoozed' and (not until or until <= now):
            status = 'open'
        if native_done:
            status = 'source_done'
        group = ('handled' if status in ('done', 'ignored', 'source_done') else
                 'snoozed' if status == 'snoozed' else
                 'info' if info else 'overdue' if dt and dt < now and source in ('canvas', 'memo') else
                 'today' if dt and dt < end else 'upcoming' if dt else 'unscheduled')
        rows.append(dict(id=key, title=title or '—', source=source,
                         due=dt.isoformat() if dt else '', reason=reason, target=target,
                         status=status, group=group, until=mark.get('until'), allday=allday))

    for it in canvas.get('todo', []):
        if it.get('submitted') or it.get('dismissed'):
            continue
        add('canvas:' + str(it['key']), it.get('title'), 'canvas', it.get('due_utc'),
            'canvas_pending', {'type': 'url', 'url': it.get('url', '')})

    for it in memo_items:
        if it.get('kind') in ('weekly', 'monthly'):
            # Keep today's occurrence visible after its clock time has passed.
            when, _ = memos.next_time(it, now.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None))
            due = when.isoformat() if when else ''
            suffix = ':' + due[:10]
        else:
            due, suffix = (it.get('info') or {}).get('at', ''), ''
        add('memo:' + it['id'] + suffix, it.get('text'), 'memo', due, 'memo',
            {'type': 'memo', 'id': it['id']}, native_done=bool(it.get('done')))

    # Only explicitly watched mail becomes an action. AI importance alone is informational.
    mail_by_id = {m['id']: m for m in messages}
    for mid, flag in flags.items():
        if flag.get('star') and mid not in mail_by_id:
            mail_by_id[mid] = {'id': mid, **flag.get('meta', {}), 'archived': True}
    for mid, it in mail_by_id.items():
        flag = flags.get(mid, {})
        rank = it.get('rank') or {}
        if not flag.get('star'):
            received = timestamp(it.get('ts'))
            if not it.get('unread') or rank.get('level', 0) < 2 or not received or received < now - timedelta(days=7):
                continue
        target = {'type': 'mail', 'id': mid, 'url': it.get('web_url', ''), 'archived': bool(it.get('archived'))}
        add('mail:' + mid, it.get('subject'), 'mail', '', 'watched' if flag.get('star') else 'mail_info',
            target, info=not flag.get('star'), native_done=bool(flag.get('done')))

    # Course schedule uses Sunday=0. Occurrence IDs prevent completing every future week.
    for offset in range(7):
        day = now.date() + timedelta(days=offset)
        for it in schedule.get('items', []):
            if it.get('weekday') != (day.weekday() + 1) % 7:
                continue
            due = f"{day.isoformat()}T{it.get('start') or '00:00'}"
            add('schedule:' + str(it['id']) + ':' + day.isoformat(),
                ' · '.join(filter(None, [it.get('course'), it.get('title')])), 'schedule', due,
                'schedule', {'type': 'schedule', 'date': day.isoformat(), 'id': it['id']})
    for it in events:
        day = it.get('date', '')
        if not day or day < now.date().isoformat():
            continue
        # Existing mail event layer already merges reminders and retains original sources.
        add('event:' + str(it['id']), it.get('title'), 'schedule',
            day + 'T' + (it.get('start') or '00:00'), 'mail_event',
            {'type': 'mail', 'id': it.get('mid'), 'date': day},
            allday=bool(it.get('allday') or not it.get('start')))

    # A snoozed occurrence must survive midnight even when its calendar day has passed.
    present = {r['id'] for r in rows}
    for key, mark in marks.items():
        saved = mark.get('item') or {}
        if key not in present and mark.get('status') == 'snoozed' and saved.get('source') == 'schedule':
            add(key, saved.get('title'), saved['source'], saved.get('due'), saved.get('reason'),
                saved.get('target', {}), allday=saved.get('allday', False))

    order = {k: i for i, k in enumerate(('overdue', 'today', 'unscheduled', 'upcoming', 'info', 'snoozed', 'handled'))}
    rows.sort(key=lambda r: (order[r['group']], r['due'] or '9999', r['id']))
    return {'date': now.date().isoformat(), 'items': rows,
            'counts': {g: sum(r['group'] == g for r in rows) for g in order}}
