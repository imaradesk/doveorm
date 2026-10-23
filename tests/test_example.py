"""
tests/test_example.py
The example project (example/) stays runnable and in sync with its migrations.
"""
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
EXAMPLE = os.path.join(ROOT, "example")


class TestExample(unittest.TestCase):
    """Runs in subprocesses: the example's models must not mix with the test models."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="doveorm_example_")
        self.env = dict(os.environ, BLOG_DB=os.path.join(self.tmp.name, "blog.db"),
                        PYTHONPATH=ROOT)

    def tearDown(self):
        self.tmp.cleanup()

    def run_py(self, *args, cwd):
        proc = subprocess.run([sys.executable, *args], cwd=cwd, env=self.env,
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc.stdout

    def test_migrations_cover_the_models(self):
        out = self.run_py("-m", "doveorm", "verify", cwd=EXAMPLE)
        self.assertIn("in sync", out)

    def test_demo_runs_and_is_repeatable(self):
        first = self.run_py(os.path.join(EXAMPLE, "demo.py"), cwd=self.tmp.name)
        second = self.run_py(os.path.join(EXAMPLE, "demo.py"), cwd=self.tmp.name)
        self.assertIn("['0001_initial']", first.splitlines()[0])
        self.assertIn("none", second.splitlines()[0])
        self.assertEqual(first.splitlines()[1:], second.splitlines()[1:])
        self.assertIn("['python', 'orm']", first)
        self.assertTrue(first.rstrip().endswith("All done! ✓"))
        out =self.run_py("-m", "doveorm", "doctor", cwd=EXAMPLE)
        self.assertIn("0 failed", out)


if __name__ == "__main__":
    unittest.main()
