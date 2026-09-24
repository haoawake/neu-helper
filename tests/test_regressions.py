import ast
import io
import json
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

import chat_bridge
import mailbox
from briefings import BriefingRunner

ROOT = Path(__file__).resolve().parents[1]


class ChatRegressionTests(unittest.TestCase):
    def test_long_unicode_prompt_and_large_stderr(self):
        session = chat_bridge.ChatSession(ROOT)
        original = subprocess.Popen
        prompt = '课程邮件上下文' * 20000
        script = ("import sys,json; sys.stderr.write('x'*200000); sys.stderr.flush(); "
                  "s=sys.stdin.buffer.read().decode('utf-8'); "
                  "print(json.dumps({'type':'stream_event','event':{'type':"
                  "'content_block_delta','delta':{'type':'text_delta','text':str(len(s))}}})); "
                  "print(json.dumps({'type':'result','subtype':'success','session_id':'new'}))")

        def launch(argv, **kwargs):
            self.assertNotIn(prompt, argv)
            return original([sys.executable, '-c', script], **kwargs)

        with patch.object(chat_bridge, 'find_claude', return_value='claude'), \
                patch.object(chat_bridge.subprocess, 'Popen', side_effect=launch):
            self.assertTrue(session._stream(prompt))
        self.assertEqual(session.last_text, str(len(prompt)))
        self.assertIsNone(session._proc)

    def test_partial_failure_is_reported_without_retry(self):
        session = chat_bridge.ChatSession(ROOT)
        session.session_id = 'old'

        def stream(_):
            session.last_text = 'unfinished answer'
            session._last_error = 'connection lost'
            return False

        with patch.object(session, '_stream', side_effect=stream) as run:
            session._run('question')
        self.assertEqual(run.call_count, 1)
        self.assertFalse(session.succeeded)
        self.assertTrue(any(e['kind'] == 'error' for e in session.drain()))

    def test_nonzero_exit_after_text_is_failure(self):
        session = chat_bridge.ChatSession(ROOT)
        proc = MagicMock()
        proc.stdout = io.StringIO(json.dumps({'type': 'stream_event', 'event': {
            'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'partial'}}}) + '\n')
        proc.returncode = 1
        proc.poll.return_value = 1
        with patch.object(chat_bridge, 'find_claude', return_value='claude'), \
                patch.object(chat_bridge.subprocess, 'Popen', return_value=proc):
            self.assertFalse(session._stream('question'))

    def test_cancelled_resume_does_not_restart(self):
        session = chat_bridge.ChatSession(ROOT)
        session.session_id = 'old'

        def stream(_):
            session.cancel()
            return False

        with patch.object(session, '_stream', side_effect=stream) as run:
            session._run('question')
        self.assertEqual(run.call_count, 1)
        self.assertFalse(session.succeeded)
        self.assertFalse(any(e['kind'] == 'error' for e in session.drain()))

    def test_session_id_refreshes(self):
        session = chat_bridge.ChatSession(ROOT)
        session.session_id = 'old'
        session._handle({'type': 'result', 'subtype': 'success', 'session_id': 'new'})
        self.assertEqual(session.session_id, 'new')

    def test_result_error_after_partial_text_is_reported(self):
        session = chat_bridge.ChatSession(ROOT)

        def stream(_):
            session.last_text = 'partial'
            session._handle({'type': 'result', 'subtype': 'error_max_turns'})
            return True

        with patch.object(session, '_stream', side_effect=stream):
            session._run('question')
        self.assertFalse(session.succeeded)
        self.assertTrue(any(e['kind'] == 'error' for e in session.drain()))

    def test_successful_briefing_still_archived(self):
        runner = object.__new__(BriefingRunner)
        runner.pending_date = '2026-09-23'
        runner.store = MagicMock()
        session = chat_bridge.ChatSession(ROOT)
        session.last_text = 'complete briefing'
        session.succeeded = True
        runner.on_session_done(session)
        runner.store.put.assert_called_once_with('2026-09-23', 'complete briefing', 0.0)

    def test_callback_finishes_before_done_and_unlock(self):
        def done(session):
            self.assertTrue(session.is_busy())
            self.assertFalse(any(e['kind'] == 'done' for e in session.drain()))
        session = chat_bridge.ChatSession(ROOT, on_done=done)
        with patch.object(session, '_stream', return_value=True):
            session._run('question')
        self.assertFalse(session.is_busy())
        self.assertEqual(session.drain()[-1]['kind'], 'done')

    def test_busy_reserved_before_worker_starts(self):
        session = chat_bridge.ChatSession(ROOT)
        with patch.object(chat_bridge.threading, 'Thread'):
            session.send_async('question')
            self.assertTrue(session.is_busy())
            session.send_async('duplicate')
        self.assertEqual(session.drain()[0]['kind'], 'error')
        session._busy.release()

    def test_failed_partial_briefing_not_archived(self):
        runner = object.__new__(BriefingRunner)
        runner.pending_date = '2026-09-23'
        runner.store = MagicMock()
        session = chat_bridge.ChatSession(ROOT)
        session.last_text = 'partial briefing'
        runner.on_session_done(session)
        runner.store.put.assert_not_called()
        self.assertIsNone(runner.pending_date)


class MailRegressionTests(unittest.TestCase):
    acc = {'id': 'test', 'host': 'example.invalid', 'email': 'fake', 'password': 'fake'}

    def test_missing_flags_do_not_turn_known_messages_unread(self):
        for response in [('NO', [b'failed']), ('OK', [b'1 (UID 1 FLAGS (\\Seen))'])]:
            with self.subTest(response=response):
                client = MagicMock()
                client.__enter__.return_value = client
                client.uid.side_effect = [('OK', [b'1 2']), response]
                with patch.object(mailbox.imaplib, 'IMAP4_SSL', return_value=client):
                    msgs, err = mailbox.imap_fetch(self.acc, known={'test#1', 'test#2'})
                self.assertIsNone(err)
                self.assertFalse(any(m['unread'] for m in msgs))
                self.assertFalse(any(m['id'] == 'test#2' for m in msgs))

    def test_partial_batch_progress_survives_disconnect(self):
        client = MagicMock()
        client.__enter__.return_value = client
        client.select.return_value = ('OK', [])
        client.uid.side_effect = [('OK', []), OSError('disconnected')]
        with patch.object(mailbox.imaplib, 'IMAP4_SSL', return_value=client):
            n, err = mailbox.imap_set_seen_many(self.acc, [str(i) for i in range(1, 902)])
        self.assertEqual(n, 900)
        self.assertIn('disconnected', err)

    def test_seen_endpoint_keeps_successful_prefix_on_error(self):
        # Load just this route: importing server would start real background services.
        tree = ast.parse((ROOT / 'server.py').read_text(encoding='utf-8'))
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'api_mail_seen_all')
        fn.decorator_list = []
        backend = MagicMock()
        backend.rate_messages.return_value = [{'id': 'test#1', 'unread': True}, {'id': 'test#2', 'unread': True}]
        backend.is_trash.return_value = False
        backend.mail.patch_many.return_value = ['test#1']
        mailmod = MagicMock()
        mailmod.get_account.return_value = self.acc
        mailmod.imap_set_seen_many.return_value = (1, 'disconnected')
        request = MagicMock()
        request.get_json.return_value = {}
        ns = dict(backend=backend, mailmod=mailmod, request=request, read_prefs=lambda: {}, jsonify=lambda x: x)
        exec(compile(ast.Module(body=[fn], type_ignores=[]), 'server.py', 'exec'), ns)
        result = ns['api_mail_seen_all']()
        backend.mail.patch_many.assert_called_once_with(['test#1'], {'unread': False})
        self.assertEqual(result['done'], 1)
        self.assertFalse(result['ok'])


if __name__ == '__main__':
    unittest.main()
