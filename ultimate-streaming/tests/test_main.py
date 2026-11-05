"""Command-line behavior: --version and only one daemon at a time."""
import os
import subprocess
import sys
import time
import unittest

from tests.helpers import ROOT, tmpdir

HOLD = """
import sys, time
sys.path.insert(0, {root!r})
import main
main.single_instance()
print("locked", flush=True)
time.sleep(30)
"""


class Cli(unittest.TestCase):
    def test_version(self):
        out = subprocess.run([sys.executable, os.path.join(ROOT, "main.py"), "--version"],
                             capture_output=True, text=True, check=True).stdout
        self.assertRegex(out, r"ultimate-streaming \d+\.\d+")

    def test_second_daemon_refused(self):
        env = dict(os.environ, DECK_STATE_DIR=tmpdir())
        first = subprocess.Popen([sys.executable, "-c", HOLD.format(root=ROOT)], env=env,
                                 stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(first.stdout.readline().strip(), "locked")
            second = subprocess.run([sys.executable, "-c", HOLD.format(root=ROOT)], env=env,
                                    capture_output=True, text=True, timeout=10)
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("already running", second.stderr)
            self.assertIn(str(first.pid), second.stderr)
        finally:
            first.kill()
            first.wait()
            first.stdout.close()
        time.sleep(0.1)
        third = subprocess.Popen([sys.executable, "-c", HOLD.format(root=ROOT)], env=env,
                                 stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(third.stdout.readline().strip(), "locked", "lock should free when the daemon dies")
        finally:
            third.kill()
            third.wait()
            third.stdout.close()


if __name__ == "__main__":
    unittest.main()
