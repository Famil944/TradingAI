"""Termux one-command switch only edits the shadow flag."""
import stat
import tempfile
import unittest
from pathlib import Path

from scripts.configure_early_shadow import configure, updated_env


class ConfigureEarlyShadowTests(unittest.TestCase):
    def test_add_toggle_and_idempotence_preserve_secrets(self):
        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / ".env"
            env.write_text(
                "TELEGRAM_BOT_TOKEN=top_secret\n"
                "OTCHARTS_API_KEY=also_secret\nOTC_TEST_MODE=false\n",
                encoding="utf-8",
            )
            env.chmod(0o600)
            original_mode = stat.S_IMODE(env.stat().st_mode)
            self.assertTrue(configure(env, True))
            self.assertFalse(configure(env, True))
            contents = env.read_text(encoding="utf-8")
            self.assertIn("TELEGRAM_BOT_TOKEN=top_secret", contents)
            self.assertIn("OTCHARTS_API_KEY=also_secret", contents)
            self.assertIn("OTC_TEST_MODE=false", contents)
            self.assertEqual(contents.count("OTC_EARLY_SHADOW_ENABLED="), 1)
            self.assertIn("OTC_EARLY_SHADOW_ENABLED=true", contents)
            self.assertEqual(stat.S_IMODE(env.stat().st_mode), original_mode)
            self.assertTrue(configure(env, False))
            self.assertIn("OTC_EARLY_SHADOW_ENABLED=false",
                          env.read_text(encoding="utf-8"))

    def test_duplicate_active_flag_is_reconciled(self):
        env = (
            "# OTC_EARLY_SHADOW_ENABLED=example\n"
            "OTC_EARLY_SHADOW_ENABLED=false\n"
            "export OTC_EARLY_SHADOW_ENABLED=true\n"
            "OTC_PRIMARY_MODE=conditional\n"
        )
        new = updated_env(env, True)
        self.assertEqual(new.count("OTC_EARLY_SHADOW_ENABLED=true"), 1)
        self.assertIn("# OTC_EARLY_SHADOW_ENABLED=example", new)
        self.assertIn("OTC_PRIMARY_MODE=conditional", new)
        self.assertNotIn("OTC_EARLY_SHADOW_ENABLED=false", new)

    def test_crlf_and_file_without_final_newline(self):
        old = "TELEGRAM_BOT_TOKEN=abc\r\nOTCHARTS_API_KEY=xyz"
        after = updated_env(old, True)
        self.assertIn("OTCHARTS_API_KEY=xyz\r\nOTC_EARLY_SHADOW_ENABLED=true\r\n", after)
        self.assertEqual(after, updated_env(after, True))

    def test_no_missing_env_creation_or_symlink_write(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            target = root / ".env"
            with self.assertRaises(FileNotFoundError):
                configure(target, True)
            self.assertFalse(target.exists())
            real = root / "outside"
            real.write_text("DO_NOT_TOUCH=1\n", encoding="utf-8")
            try:
                target.symlink_to(real)
            except OSError as exc:
                if getattr(exc, "winerror", None) == 1314:
                    self.skipTest("Windows requires symlink privileges for this check")
                raise
            with self.assertRaises(ValueError):
                configure(target, True)
            self.assertEqual(real.read_text(encoding="utf-8"), "DO_NOT_TOUCH=1\n")


if __name__ == "__main__":
    unittest.main()
