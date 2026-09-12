import os
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from instance_lock import SchedulerInstanceLock


class SchedulerInstanceLockTests(unittest.TestCase):
    def test_second_worker_is_rejected_until_first_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            scope = Path(directory) / "scheduler"
            name = f"Local\\SchedulerLockTest_{uuid4().hex}"
            first = SchedulerInstanceLock(scope, name=name)
            second = SchedulerInstanceLock(scope, name=name)
            third = SchedulerInstanceLock(scope, name=name)
            try:
                self.assertTrue(first.acquire())
                self.assertFalse(second.acquire())
                first.release()
                self.assertTrue(third.acquire())
            finally:
                first.release()
                second.release()
                third.release()


if __name__ == "__main__":
    unittest.main()

