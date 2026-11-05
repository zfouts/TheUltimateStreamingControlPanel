"""core.safefs.write_atomic: private, atomic, never through a symlink, no litter on failure."""
import os
import stat
import unittest
from unittest import mock

from tests.helpers import tmpdir

from core import safefs  # noqa: E402


class TestWriteAtomic(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdir()
        self.path = os.path.join(self.dir, "state.json")

    def leftovers(self):
        return [n for n in os.listdir(self.dir) if n.endswith(".tmp")]

    def test_new_file_is_0600(self):
        safefs.write_atomic(self.path, b"one")
        self.assertEqual(open(self.path, "rb").read(), b"one")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(self.leftovers(), [])

    def test_mode_argument(self):
        safefs.write_atomic(self.path, b"x", mode=0o640)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode) & 0o750, 0o640 & 0o750)

    def test_replaces_existing_file(self):
        safefs.write_atomic(self.path, b"one")
        safefs.write_atomic(self.path, b"two")
        self.assertEqual(open(self.path, "rb").read(), b"two")
        self.assertEqual(self.leftovers(), [])

    def test_symlink_at_target_is_replaced_not_followed(self):
        victim = os.path.join(self.dir, "victim")
        with open(victim, "wb") as f:
            f.write(b"keep")
        os.symlink(victim, self.path)
        safefs.write_atomic(self.path, b"new")
        self.assertEqual(open(victim, "rb").read(), b"keep")
        self.assertFalse(os.path.islink(self.path))
        self.assertEqual(open(self.path, "rb").read(), b"new")

    def test_symlink_at_temp_path_is_refused(self):
        victim = os.path.join(self.dir, "victim")
        with open(victim, "wb") as f:
            f.write(b"keep")
        tmp = os.path.join(self.dir, ".state.json.fixed.tmp")
        os.symlink(victim, tmp)
        with mock.patch.object(safefs.secrets, "token_hex", return_value="fixed"):
            with self.assertRaises(OSError):
                safefs.write_atomic(self.path, b"new")
        self.assertEqual(open(victim, "rb").read(), b"keep")
        self.assertFalse(os.path.exists(self.path))
        self.assertTrue(os.path.islink(tmp))      # not ours to remove

    def test_failed_replace_removes_temp_and_keeps_old(self):
        safefs.write_atomic(self.path, b"old")
        with mock.patch.object(safefs.os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                safefs.write_atomic(self.path, b"new")
        self.assertEqual(open(self.path, "rb").read(), b"old")
        self.assertEqual(self.leftovers(), [])

    def test_failed_write_removes_temp(self):
        with mock.patch.object(safefs.os, "fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                safefs.write_atomic(self.path, b"new")
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(self.leftovers(), [])


if __name__ == "__main__":
    unittest.main()
