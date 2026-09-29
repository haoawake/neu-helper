import ast
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import translate

ROOT = Path(__file__).resolve().parents[1]


class FakeRunner:
    def __init__(self, delay=0.0):
        self.calls = []
        self.delay = delay
        self.lock = threading.Lock()

    def __call__(self, text, model):
        with self.lock:
            self.calls.append((text, model))
        time.sleep(self.delay)
        return '译文:' + text, 0.0012


class TranslatorTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / 'translations.json'

    def tearDown(self):
        self.dir.cleanup()

    def test_cache_hit_skips_model_and_survives_restart(self):
        runner = FakeRunner()
        tr = translate.Translator(self.path, runner=runner)
        self.assertEqual(tr.translate('Read chapter 3.', 'haiku'), ('译文:Read chapter 3.', False))
        self.assertEqual(tr.translate('Read chapter 3.', 'haiku'), ('译文:Read chapter 3.', True))
        again = translate.Translator(self.path, runner=runner)
        self.assertEqual(again.translate('Read chapter 3.', 'sonnet'), ('译文:Read chapter 3.', True))
        self.assertEqual(len(runner.calls), 1)

    def test_edited_text_is_translated_again(self):
        runner = FakeRunner()
        tr = translate.Translator(self.path, runner=runner)
        tr.translate('Due Friday.', 'haiku')
        tr.translate('Due Monday.', 'haiku')
        self.assertEqual([c[0] for c in runner.calls], ['Due Friday.', 'Due Monday.'])

    def test_concurrent_requests_share_one_model_call(self):
        runner = FakeRunner(delay=0.2)
        tr = translate.Translator(self.path, runner=runner)
        out = []
        threads = [threading.Thread(target=lambda: out.append(tr.translate('Same text', 'haiku')))
                   for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual({o[0] for o in out}, {'译文:Same text'})
        self.assertEqual(sorted(o[1] for o in out), [False, True, True])

    def test_oldest_entries_are_evicted(self):
        self.path.write_text(json.dumps({
            'old': {'text': 'a', 'at': '2026-01-01 00:00:00'},
            'mid': {'text': 'b', 'at': '2026-02-01 00:00:00'},
            'bad': 'not a row',
        }), encoding='utf-8')
        tr = translate.Translator(self.path, runner=FakeRunner())
        with patch.object(translate, 'KEEP', 2):
            tr.translate('new text', 'haiku')
        kept = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(set(kept), {'mid', translate.fingerprint('new text')})

    def test_failed_translation_is_not_cached(self):
        runner = MagicMock(side_effect=[RuntimeError('boom'), ('好了', 0.0)])
        tr = translate.Translator(self.path, runner=runner)
        with self.assertRaises(RuntimeError):
            tr.translate('text', 'haiku')
        self.assertEqual(tr.translate('text', 'haiku'), ('好了', False))


class RunTests(unittest.TestCase):
    def completed(self, rc=0, stdout='', stderr=''):
        return subprocess.CompletedProcess([], rc, stdout=stdout, stderr=stderr)

    def test_prompt_goes_on_stdin_with_restricted_flags(self):
        ok = self.completed(stdout=json.dumps({'result': '```\n第一段\n\n- 第二条\n```', 'total_cost_usd': 0.003}))
        with patch.object(translate, 'find_claude', return_value='claude'), \
                patch.object(translate.subprocess, 'run', return_value=ok) as run:
            self.assertEqual(translate.run('First.\n\n- Second', 'haiku'), ('第一段\n\n- 第二条', 0.003))
        argv = run.call_args.args[0]
        for flag in ('-p', '--strict-mcp-config', '--restricted', '--system-prompt'):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index('--model') + 1], 'haiku')
        self.assertEqual(argv[argv.index('--tools') + 1], '')
        self.assertEqual(argv[argv.index('--effort') + 1], 'low')
        self.assertEqual(run.call_args.kwargs['cwd'], tempfile.gettempdir())
        self.assertNotIn('First.', ' '.join(argv))
        self.assertIn('First.\n\n- Second', run.call_args.kwargs['input'])

    def test_failures_become_short_errors(self):
        cases = [
            (self.completed(rc=1, stderr='auth required'), '退出码 1'),
            (self.completed(stdout=json.dumps({'is_error': True, 'result': 'quota'})), 'quota'),
            (self.completed(stdout='not json'), '读不懂'),
            (self.completed(stdout=json.dumps({'result': '   '})), '没给出译文'),
            (subprocess.TimeoutExpired(['claude', '--system-prompt', 'SECRET'], 180), '超时'),
        ]
        for outcome, expected in cases:
            kw = {'side_effect': outcome} if isinstance(outcome, Exception) else {'return_value': outcome}
            with self.subTest(expected=expected), \
                    patch.object(translate, 'find_claude', return_value='claude'), \
                    patch.object(translate.subprocess, 'run', **kw), \
                    patch('sys.stderr'):
                with self.assertRaises(RuntimeError) as ctx:
                    translate.run('text', 'haiku')
                self.assertIn(expected, str(ctx.exception))
                self.assertNotIn('SECRET', str(ctx.exception))
        with patch.object(translate, 'find_claude', return_value=None):
            with self.assertRaisesRegex(RuntimeError, '找不到'):
                translate.run('text', 'haiku')

    def test_older_cli_without_lean_flags_falls_back(self):
        old_cli = self.completed(rc=1, stderr="error: unknown option '--effort'")
        ok = self.completed(stdout=json.dumps({'result': '好', 'total_cost_usd': 0.05}))
        with patch.object(translate, 'find_claude', return_value='claude'), \
                patch.object(translate.subprocess, 'run', side_effect=[old_cli, ok]) as run:
            self.assertEqual(translate.run('Good.', 'haiku'), ('好', 0.05))
        first, second = (c.args[0] for c in run.call_args_list)
        self.assertIn('--effort', first)
        self.assertNotIn('--effort', second)
        self.assertNotIn('--tools', second)

    def test_clean_only_strips_a_wrapping_fence(self):
        self.assertEqual(translate.clean('<译文>\n好\n</译文>'), '好')
        body = '```\ncode()\n```\n说明\n```\nmore()\n```'
        self.assertEqual(translate.clean(body), body)


class BackendTranslateTests(unittest.TestCase):
    def method(self, prefs):
        # Load just this method: importing server would start real background services.
        tree = ast.parse((ROOT / 'server.py').read_text(encoding='utf-8'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Backend')
        fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'translate_assignment')
        ns = {'read_prefs': lambda: prefs}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), 'server.py', 'exec'), ns)
        return ns['translate_assignment']

    def test_full_description_is_fetched_by_id(self):
        backend = MagicMock()
        backend.assignment.return_value = {'error': None, 'description': '  Full description  '}
        backend.translator.translate.return_value = ('完整译文', False)
        result = self.method({'transModel': 'sonnet'})(backend, 261905, 3457495)
        backend.assignment.assert_called_once_with(261905, 3457495)
        backend.translator.translate.assert_called_once_with('Full description', 'sonnet')
        self.assertEqual(result, {'ok': True, 'text': '完整译文', 'cached': False})

    def test_errors_are_reported_without_calling_the_model(self):
        fn = self.method({})
        backend = MagicMock()
        backend.assignment.return_value = {'error': 'HTTPError: 401'}
        self.assertEqual(fn(backend, 1, 2), {'ok': False, 'error': 'HTTPError: 401'})
        backend.assignment.return_value = {'error': None, 'description': ''}
        self.assertFalse(fn(backend, 1, 2)['ok'])
        backend.translator.translate.assert_not_called()
        backend.assignment.return_value = {'error': None, 'description': 'x'}
        backend.translator.translate.side_effect = RuntimeError('找不到 claude 命令')
        self.assertEqual(fn(backend, 1, 2), {'ok': False, 'error': '找不到 claude 命令'})
        self.assertEqual(backend.translator.translate.call_args.args[1], 'haiku')


if __name__ == '__main__':
    unittest.main()
