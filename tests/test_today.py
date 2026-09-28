import ast
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock
from flask import Flask, jsonify, request
import today

ROOT = Path(__file__).resolve().parents[1]

class TodayTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
        self.args = dict(canvas={}, messages=[], flags={}, memo_items=[], schedule={}, events=[], marks={}, now=self.now)

    def build(self, **kwargs):
        return today.build(**{**self.args, **kwargs})

    def task(self, key, delta, **extra):
        return dict(key=key, title='Assignment', due_utc=(self.now + delta).isoformat(), url='https://example.edu/assignment', **extra)

    def test_date_buckets_and_source_submission(self):
        tasks = [self.task('over', timedelta(hours=-1)), self.task('today', timedelta(hours=4)),
                 self.task('next', timedelta(days=2)), self.task('far', timedelta(days=9)),
                 self.task('sent', timedelta(), submitted=True), self.task('ignored', timedelta(), dismissed=True)]
        rows = self.build(canvas={'todo': tasks})['items']
        self.assertEqual([r['group'] for r in rows], ['overdue', 'today', 'upcoming'])
        self.assertEqual(rows[0]['target']['url'], 'https://example.edu/assignment')

    def test_timezone_offsets_represent_same_deadline(self):
        due = self.now + timedelta(hours=1)
        from datetime import timezone
        task = self.task('one', timedelta(hours=1))
        task['due_utc'] = due.astimezone(timezone.utc).isoformat()
        self.assertEqual(self.build(canvas={'todo': [task]})['items'][0]['group'], 'today')

    def test_same_titles_have_independent_stable_ids(self):
        tasks = [self.task('a', timedelta(hours=1)), self.task('b', timedelta(hours=1))]
        rows = self.build(canvas={'todo': tasks}, marks={'canvas:a': {'status': 'done'}})['items']
        self.assertEqual(len(rows), 2)
        self.assertEqual({r['id']: r['group'] for r in rows}, {'canvas:a':'handled','canvas:b':'today'})

    def test_snooze_expires_without_losing_task(self):
        tasks = [self.task('a', timedelta(hours=1))]
        for delta, expected in [(timedelta(hours=1), 'snoozed'), (timedelta(hours=-1), 'today')]:
            out = self.build(canvas={'todo': tasks}, marks={'canvas:a': {'status':'snoozed','until':(self.now+delta).isoformat()}})
            self.assertEqual(out['items'][0]['group'], expected)

    def test_important_mail_is_information_not_an_action(self):
        messages = [{'id':'a', 'subject':'Notice', 'unread':True, 'ts':self.now.isoformat(), 'rank':{'level':3}},
                    {'id':'b', 'subject':'Old', 'unread':True, 'ts':(self.now-timedelta(days=30)).isoformat(), 'rank':{'level':3}}]
        self.assertEqual([r['group'] for r in self.build(messages=messages)['items']], ['info'])
        self.assertEqual(self.build(messages=messages, flags={'a':{'star':True}})['items'][0]['group'], 'unscheduled')

    def test_watched_mail_survives_cache_eviction(self):
        out = self.build(flags={'gone': {'star': True, 'meta': {'subject':'Follow up'}}})
        self.assertTrue(out['items'][0]['target']['archived'])
        self.assertEqual(out['items'][0]['title'], 'Follow up')

    def test_weekly_memo_keeps_today_occurrence_after_clock_time(self):
        memo = dict(id='m', text='Review', kind='weekly', weekday=self.now.weekday(), hour=9, minute=0,
                    created=(self.now-timedelta(days=20)).strftime('%Y-%m-%d %H:%M:%S'))
        rows = self.build(memo_items=[memo])['items']
        self.assertTrue(rows[0]['id'].endswith(self.now.date().isoformat()))
        self.assertEqual(rows[0]['group'], 'overdue')
        future = self.build(memo_items=[memo], now=self.now+timedelta(days=7), marks={rows[0]['id']:{'status':'done'}})
        self.assertNotEqual(future['items'][0]['id'], rows[0]['id'])
        self.assertNotEqual(future['items'][0]['status'], 'done')

    def test_schedule_occurrences_use_sunday_zero(self):
        it = dict(id='s', title='Class', weekday=(self.now.weekday()+1)%7, start='15:00')
        out = self.build(schedule={'items':[it]})
        self.assertEqual(len(out['items']), 1)
        self.assertEqual(out['items'][0]['group'], 'today')
        self.assertEqual(out['items'][0]['target']['date'], self.now.date().isoformat())

    def test_persistence_undo_and_invalid_snooze(self):
        with tempfile.TemporaryDirectory(prefix='today-test-', dir=ROOT) as folder:
            path = Path(folder)/'actions.json'
            store = today.ActionStore(path)
            store.set('a','done')
            self.assertEqual(today.ActionStore(path).all()['a']['status'], 'done')
            with self.assertRaises(ValueError): store.set('a','snoozed','2000-01-01')
            self.assertEqual(store.all()['a']['status'], 'done')
            store.set('a','open')
            self.assertEqual(today.ActionStore(path).all(), {})

    def test_routes_validate_and_write_without_remote_side_effects(self):
        tree = ast.parse((ROOT/'server.py').read_text(encoding='utf-8'))
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ('api_today','api_today_action','api_today_refresh')]
        app = Flask(__name__)
        with tempfile.TemporaryDirectory(prefix='today-test-', dir=ROOT) as folder:
            backend = MagicMock()
            backend.today_actions = today.ActionStore(Path(folder)/'actions.json')
            backend.today_view.return_value = {'items':[{'id':'canvas:a','status':'open'}]}
            ns = dict(app=app,backend=backend,jsonify=jsonify,request=request)
            exec(compile(ast.Module(body=nodes,type_ignores=[]),'server.py','exec'),ns)
            client = app.test_client()
            self.assertEqual(client.post('/api/today/action',json={'id':'missing','status':'done'}).status_code,404)
            self.assertEqual(client.post('/api/today/action',json={'id':'canvas:a','status':'bad'}).status_code,400)
            self.assertEqual(client.post('/api/today/action',json={'id':'canvas:a','status':'done'}).status_code,200)
            self.assertEqual(backend.today_actions.all()['canvas:a']['status'],'done')
            self.assertEqual(client.post('/api/today/action',json=['bad']).status_code,400)
            backend.mail_flags.set.assert_not_called()
            backend.client.assert_not_called()

    def test_corrupt_marks_do_not_get_overwritten(self):
        with tempfile.TemporaryDirectory(prefix='today-test-', dir=ROOT) as folder:
            path = Path(folder)/'actions.json'
            path.write_text('{broken',encoding='utf-8')
            store = today.ActionStore(path)
            self.assertTrue(store.error)
            with self.assertRaises(ValueError): store.set('a','done')
            self.assertEqual(path.read_text(encoding='utf-8'),'{broken')

    def test_snoozed_calendar_occurrence_survives_midnight(self):
        key = 'schedule:class:yesterday'
        saved = dict(title='Class follow-up',source='schedule',due=(self.now-timedelta(days=1)).isoformat(),
                     reason='schedule',target={'type':'schedule'})
        marks = {key:{'status':'snoozed','until':(self.now+timedelta(hours=1)).isoformat(),'item':saved}}
        self.assertEqual(self.build(marks=marks)['items'][0]['group'],'snoozed')
        self.assertEqual(self.build(marks=marks,now=self.now+timedelta(hours=2))['items'][0]['status'],'open')

    def test_canvas_failure_retains_cached_actions_and_failure_status(self):
        from types import SimpleNamespace, MethodType
        tree = ast.parse((ROOT/'server.py').read_text(encoding='utf-8'))
        cls = next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Backend')
        nodes = [n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name in ('dashboard','today_view')]
        ns = dict(today=today,read_prefs=lambda:{},mailmod=SimpleNamespace(accounts_public=lambda:[]),
                  CanvasConfigError=type('CanvasConfigError',(Exception,),{}))
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'server.py','exec'),ns)
        backend = SimpleNamespace(_cache=None,_today_canvas={'todo':[self.task('cached',timedelta(hours=1))],'courses':[]},
            canvas_status={'state':'idle','last_success':'2026-09-22T12:00:00'},client=MagicMock(side_effect=OSError('offline')),
            schedule=MagicMock(),mail=MagicMock(),mail_flags=MagicMock(),memos=MagicMock(),today_actions=MagicMock(),
            mail_fetcher=MagicMock(),rate_messages=lambda x:x,is_trash=lambda m,t:False,mail_event_list=lambda:[],sched_state={})
        backend.schedule.view.return_value={'items':[]}
        backend.mail.all.return_value=[]
        backend.mail_flags.all.return_value={}
        backend.memos.all.return_value=[]
        backend.today_actions.all.return_value={}
        backend.today_actions.error=''
        backend.mail_fetcher.snapshot.return_value={}
        self.assertEqual(MethodType(ns['dashboard'],backend)(True)['error'],'network')
        result = MethodType(ns['today_view'],backend)()
        self.assertEqual(result['items'][0]['id'],'canvas:cached')
        self.assertEqual(result['sources']['canvas']['state'],'error')
        self.assertEqual(result['sources']['canvas']['last_success'],'2026-09-22T12:00:00')

if __name__ == '__main__': unittest.main()


