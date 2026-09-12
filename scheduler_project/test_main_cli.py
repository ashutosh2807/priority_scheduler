import subprocess
import sys
import unittest

from config.settings import BASE_DIR
from instance_lock import SchedulerInstanceLock


class MainCliTests(unittest.TestCase):
    def test_once_mode_skips_cleanly_when_another_worker_owns_the_mutex(self):
        lock = SchedulerInstanceLock(BASE_DIR)
        self.assertTrue(lock.acquire())
        try:
            result = subprocess.run(
                [sys.executable, "-B", str(BASE_DIR / "main.py"), "--once"],
                cwd=BASE_DIR,
                capture_output=True,
                text=True,
                check=False,
            )
        finally:
            lock.release()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already running", result.stdout.lower())


if __name__ == "__main__":
    unittest.main()

