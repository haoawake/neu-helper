from datetime import datetime
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import updater

ROOT = Path(__file__).resolve().parents[1]


def ts(day, hour, minute=0):
    return datetime(2026, 9, day, hour, minute).timestamp()


class CheckDueTests(unittest.TestCase):
    def test_first_check_runs_immediately(self):
        self.assertTrue(updater.check_due(ts(29, 14), 0, 0))

    def test_checks_again_after_crossing_nine(self):
        ok = ts(29, 8)
        self.assertFalse(updater.check_due(ts(29, 8, 59), ok, ok))
        self.assertTrue(updater.check_due(ts(29, 9, 1), ok, ok))

    def test_morning_check_waits_for_the_next_day(self):
        ok = ts(29, 10)
        self.assertFalse(updater.check_due(ts(29, 23), ok, ok))
        self.assertFalse(updater.check_due(ts(30, 8, 59), ok, ok))
        self.assertTrue(updater.check_due(ts(30, 9, 1), ok, ok))

    def test_sleeping_through_nine_is_caught_up_on_wake(self):
        ok = ts(27, 10)
        self.assertTrue(updater.check_due(ts(29, 14), ok, ok))

    def test_failures_retry_after_half_an_hour(self):
        self.assertFalse(updater.check_due(ts(29, 10, 20), 0, ts(29, 10)))
        self.assertTrue(updater.check_due(ts(29, 10, 31), 0, ts(29, 10)))
        ok, failed = ts(28, 10), ts(29, 9)
        self.assertFalse(updater.check_due(ts(29, 9, 10), ok, failed))
        self.assertTrue(updater.check_due(ts(29, 9, 31), ok, failed))


class StageMacBundleTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.raw = self.root / 'raw'
        self.staged = self.root / 'staged'

    def tearDown(self):
        self.dir.cleanup()

    def bundle(self, rel):
        exe = self.raw / rel / 'Contents' / 'MacOS' / 'NEU Helper'
        exe.parent.mkdir(parents=True)
        exe.write_text('')
        return exe

    def test_bundle_keeps_its_app_name(self):
        self.bundle('NEU Helper.app')
        (self.raw / '__MACOSX' / '._NEU Helper.app').mkdir(parents=True)
        (self.raw / 'NEU Helper.app' / 'Contents' / 'Frameworks' / 'Inner.app').mkdir(parents=True)
        app = updater.stage_mac_bundle(self.raw, self.staged)
        self.assertEqual(app, self.staged / 'NEU Helper.app')
        self.assertTrue((app / 'Contents' / 'MacOS' / 'NEU Helper').is_file())

    def test_bundle_inside_a_folder_is_found(self):
        self.bundle('NEU Helper/NEU Helper.app')
        app = updater.stage_mac_bundle(self.raw, self.staged)
        self.assertEqual(app.name, 'NEU Helper.app')
        self.assertTrue((app / 'Contents' / 'MacOS' / 'NEU Helper').is_file())

    def test_missing_bundle_is_refused(self):
        (self.raw / 'NEU Helper').mkdir(parents=True)
        with self.assertRaisesRegex(RuntimeError, '没有 NEU Helper.app'):
            updater.stage_mac_bundle(self.raw, self.staged)


class MacHandshakeTests(unittest.TestCase):
    def test_new_bundle_writes_the_flag_where_the_old_process_waits(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            cfg = tmp / 'cfg'
            old_app = tmp / 'Applications' / 'NEU Helper.app'
            (old_app / 'Contents' / 'MacOS').mkdir(parents=True)
            new_exe = cfg / 'update' / 'staged' / 'NEU Helper.app' / 'Contents' / 'MacOS' / 'NEU Helper'
            new_exe.parent.mkdir(parents=True)
            new_exe.write_text('')
            with patch.object(updater.platform_id, 'IS_MAC', True), \
                    patch.object(updater.platform_id, 'config_dir', return_value=cfg), \
                    patch.object(sys, 'executable', str(new_exe)), \
                    patch.object(updater, '_apply_update_mac', return_value=0) as swap, \
                    patch('sys.stderr'):
                waits_on = updater.stage_root(old_app / 'Contents' / 'MacOS') / updater.STARTED
                self.assertEqual(updater.apply_update(old_app, 999999), 0)
            self.assertTrue(waits_on.exists())
            self.assertFalse((new_exe.parents[1] / updater.STARTED).exists())
            self.assertEqual(swap.call_args.args[1], new_exe.parent.resolve())


class ApplyUpdateEntryTests(unittest.TestCase):
    def test_apply_update_mode_skips_the_heavy_imports(self):
        probe = (
            "import runpy, sys\n"
            "sys.argv = ['app.py', '--apply-update']\n"
            "try:\n"
            "    runpy.run_path('app.py', run_name='__main__')\n"
            "except SystemExit as e:\n"
            "    print('exit', e.code, 'webview' in sys.modules, 'server' in sys.modules)\n"
        )
        r = subprocess.run([sys.executable, '-c', probe], cwd=str(ROOT), capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(r.stdout.strip(), 'exit 2 False False', r.stderr[-2000:])


if __name__ == '__main__':
    unittest.main()
