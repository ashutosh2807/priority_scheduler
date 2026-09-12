"""Exercise the Windows launcher in temporary fixtures, never the real worker."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import venv
from pathlib import Path


@unittest.skipUnless(os.name == "nt", "Windows batch integration tests")
class WorkerServiceLauncherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="worker launcher test ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.engine = self.root / "scheduler project with spaces"
        self.engine.mkdir()
        self.launcher = self.engine / "run_scheduler_service.bat"
        shutil.copyfile(Path(__file__).with_name("run_scheduler_service.bat"), self.launcher)
        self.report = self.root / "mock worker report.json"
        # The validation import and the worker are deliberate fixtures. No
        # installed scheduler modules, Oracle drivers or actual settings load.
        (self.engine / "dotenv.py").write_text("# Isolated validation fixture.\n", encoding="utf-8")
        (self.engine / "main.py").write_text(
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "Path(os.environ['MOCK_WORKER_REPORT']).write_text(json.dumps({\n"
            " 'args': sys.argv[1:], 'executable': sys.executable, 'cwd': os.getcwd(),\n"
            " 'master': os.getenv('SCHEDULER_MASTER_SOURCE'),\n"
            " 'calendar': os.getenv('SCHEDULER_CALENDAR_SOURCE'),\n"
            " 'api': os.getenv('SCHEDULER_API_ENABLED')}), encoding='utf-8')\n"
            "print(os.getenv('MOCK_WORKER_MESSAGE', 'fixture worker stdout'), flush=True)\n"
            "print('fixture worker stderr', file=sys.stderr, flush=True)\n"
            "raise SystemExit(int(os.getenv('MOCK_WORKER_EXIT', '0')))\n",
            encoding="utf-8",
        )
        self.environment = dict(os.environ)
        for name in ("SCHEDULER_PYTHON", "VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH"):
            self.environment.pop(name, None)
        self.environment.update(MOCK_WORKER_REPORT=str(self.report),
                                SCHEDULER_MASTER_SOURCE="file", SCHEDULER_CALENDAR_SOURCE="file",
                                SCHEDULER_API_ENABLED="0")

    def interpreter(self, relative):
        target = self.root / relative
        # A fresh interpreter is used only to run our fake main.py. with_pip=False
        # keeps this fixture offline and independent of the user's packages.
        venv.EnvBuilder(with_pip=False, system_site_packages=False, symlinks=False).create(target)
        return target / "Scripts" / "python.exe"

    def run_launcher(self, *args, **environment):
        command = subprocess.list2cmdline([os.environ.get("COMSPEC", "cmd.exe")])
        # /s /c removes the outer quote pair; the batch path keeps its own
        # quotes so copied project directories containing spaces still work.
        command += ' /d /s /c ""' + str(self.launcher) + '"'
        if args:
            command += " " + subprocess.list2cmdline(list(args))
        command += '"'
        return subprocess.run(command, cwd=self.root, env={**self.environment, **environment},
                              capture_output=True, text=True, timeout=20, check=False)

    def worker_report(self):
        return json.loads(self.report.read_text(encoding="utf-8"))

    def test_explicit_interpreter_spaced_paths_flags_logs_and_exit_code(self):
        explicit = self.interpreter("explicit python with spaces")
        self.interpreter(".scheduler-venv")
        result = self.run_launcher("--monitor", "--help", SCHEDULER_PYTHON=str(explicit), MOCK_WORKER_EXIT="37")
        self.assertEqual(result.returncode, 37, result.stderr)
        report = self.worker_report()
        self.assertEqual(Path(report["executable"]).resolve(), explicit.resolve())
        self.assertEqual(Path(report["cwd"]).resolve(), self.engine.resolve())
        self.assertEqual(report["args"], ["--calendar-source", "oracle", "--monitor", "--help"])
        self.assertEqual((report["master"], report["calendar"], report["api"]), ("oracle", "oracle", "1"))
        log = (self.engine / "logs" / "scheduler_service.log").read_text()
        self.assertIn("fixture worker stdout", log)
        self.assertIn("fixture worker stderr", log)
        self.assertIn("exit code 37", log)
        self.assertIn("ITRP scheduler service launcher.", result.stdout)
        self.assertIn(str(self.engine / "logs" / "scheduler_service.log"), result.stdout)
        self.assertIn("fixture worker stdout", result.stdout)
        self.assertIn("fixture worker stderr", result.stdout)
        self.assertIn("exit code 37", result.stdout)

    def test_shared_environment_precedes_legacy_and_activated_environments(self):
        shared = self.interpreter(".scheduler-venv")
        self.interpreter("scheduler project with spaces/venv")
        activated = self.interpreter("activated python")
        result = self.run_launcher(VIRTUAL_ENV=str(activated.parent.parent))
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.worker_report()
        self.assertEqual(Path(report["executable"]).resolve(), shared.resolve())
        self.assertEqual(report["args"], ["--calendar-source", "oracle"])

    def test_unavailable_shared_falls_back_to_legacy_before_activated(self):
        legacy = self.interpreter("scheduler project with spaces/venv")
        activated = self.interpreter("activated python")
        result = self.run_launcher("--help", VIRTUAL_ENV=str(activated.parent.parent))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(self.worker_report()["executable"]).resolve(), legacy.resolve())

    def test_activated_environment_is_last_supported_fallback(self):
        activated = self.interpreter("activated python")
        result = self.run_launcher("--monitor", VIRTUAL_ENV=str(activated.parent.parent))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(self.worker_report()["executable"]).resolve(), activated.resolve())

    def test_invalid_explicit_interpreter_does_not_fall_back(self):
        self.interpreter(".scheduler-venv")
        result = self.run_launcher(SCHEDULER_PYTHON=str(self.root / "missing python.exe"))
        self.assertEqual(result.returncode, 9009, result.stderr)
        self.assertFalse(self.report.exists())
        self.assertIn("SCHEDULER_PYTHON must point to Python", result.stderr)

    def test_python_without_dotenv_is_rejected_without_starting_worker(self):
        self.interpreter(".scheduler-venv")
        (self.engine / "dotenv.py").unlink()
        result = self.run_launcher()
        self.assertEqual(result.returncode, 9009, result.stderr)
        self.assertFalse(self.report.exists())
        self.assertIn("No compatible project Python environment found", result.stderr)

    def test_service_disabling_and_unknown_flags_are_rejected_before_python(self):
        for args in (("--once",), ("--no-control-api",), ("--calendar-source", "file"), ("--json",), ("", "--once")):
            with self.subTest(args=args):
                result = self.run_launcher(*args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse(self.report.exists())
                self.assertIn("Only --monitor and --help are supported", result.stderr)

    def test_duplicate_worker_reason_is_visible_without_replaying_previous_invocation(self):
        self.interpreter(".scheduler-venv")
        previous = self.run_launcher(MOCK_WORKER_MESSAGE="old invocation output")
        self.assertEqual(previous.returncode, 0, previous.stderr)
        message = "Scheduler worker already running; this trigger was skipped."
        result = self.run_launcher(MOCK_WORKER_MESSAGE=message)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(message, result.stdout)
        self.assertNotIn("old invocation output", result.stdout)
        log = (self.engine / "logs" / "scheduler_service.log").read_text()
        self.assertIn("old invocation output", log)
        self.assertIn(message, log)
        self.assertEqual(log.count("] Scheduler service launcher invoked."), 2)

    def test_current_invocation_keeps_complete_help_longer_than_twelve_lines(self):
        self.interpreter(".scheduler-venv")
        help_lines = [f"Help fixture line {number}" for number in range(30)]
        result = self.run_launcher("--help", MOCK_WORKER_MESSAGE="\n".join(help_lines))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.worker_report()["args"], ["--calendar-source", "oracle", "--help"])
        for line in help_lines:
            self.assertIn(line, result.stdout)

    def test_large_current_invocation_shows_bounded_recent_output(self):
        self.interpreter(".scheduler-venv")
        message = "\n".join(f"Large fixture line {number:05d}" for number in range(3500))
        result = self.run_launcher(MOCK_WORKER_MESSAGE=message, MOCK_WORKER_EXIT="19")
        self.assertEqual(result.returncode, 19, result.stderr)
        self.assertIn("Large fixture line 03499", result.stdout)
        self.assertNotIn("Large fixture line 00000", result.stdout)
        self.assertIn("exit code 19", result.stdout)
        self.assertLess(len(result.stdout), 3000)


if __name__ == "__main__":
    unittest.main()
