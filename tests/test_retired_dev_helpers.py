"""Legacy entrypoints must fail before app initialization or database access."""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class RetiredDevHelpersTest(unittest.TestCase):
    def check_helper(self, filename, entrypoint):
        script = Path(__file__).resolve().parents[1] / filename
        for invoke_imported_function in (False, True):
            with self.subTest(imported=invoke_imported_function):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    database = root / "existing.db"
                    original = b"Existing database must remain untouched."
                    database.write_bytes(original)
                    # -S excludes app dependencies. Either invocation must
                    # stop intentionally before importing application modules.
                    command = [sys.executable, "-S", str(script)]
                    if invoke_imported_function:
                        command = [sys.executable, "-S", "-c",
                                   f"import runpy; runpy.run_path({str(script)!r})"
                                   f"[{entrypoint!r}]()"]
                    result = subprocess.run(
                        command,
                        cwd=root,
                        env={"DATABASE_URL": f"sqlite:///{database}",
                             "APP_ENV": "production", "RUNTIME_ROLE": "worker"},
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    self.assertEqual(result.returncode, 2)
                    self.assertIn("retired", result.stderr)
                    self.assertNotIn("Traceback", result.stderr)
                    self.assertEqual(database.read_bytes(), original)
                    self.assertEqual(sorted(p.name for p in root.iterdir()),
                                     ["existing.db"])

    def test_fixed_password_helper_is_retired(self):
        self.check_helper("create_test_user.py", "create_admin")

    def test_fixed_product_deletion_is_retired(self):
        self.check_helper("cleanup_db.py", "delete_product")
