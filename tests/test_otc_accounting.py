import csv
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from services.otc_ml.preview import PreviewJournal
from services.otc_platform_audit import compare, export
from services.otc_signal_report import journal_stats, read_journal, report
from tests.test_otc_result_cooldown import signal


class AccountingTests(unittest.TestCase):
    def test_source_win_rates_are_separate_and_pending_not_in_denominator(self):
        from bot import otc_commands

        def preview(name):
            return SimpleNamespace(
                journal=name,
                enabled=True,
                runtime=SimpleNamespace(preview_ready=lambda: True),
                client=SimpleNamespace(health=lambda: (True, "")),
            )

        current = SimpleNamespace(
            running=lambda: True,
            phase="поток активен",
            main_ready=preview("main"),
            ml_preview=preview("ml"),
        )
        outcomes = {
            "main": ["WIN", "LOSS"],
            "ml": ["WIN", "WIN", "LOSS", "PENDING"],
        }
        with patch.object(
            otc_commands,
            "read_journal",
            side_effect=lambda name, *args: [{"outcome": s} for s in outcomes[name]],
        ):
            text = otc_commands.strategy_status(current)
        self.assertIn("XGBoost · 4 · WIN 2 / LOSS 1 · 66.7%", text)
        self.assertNotIn("Groq ·", text)
        with patch.object(otc_commands, "service", None):
            buttons = [
                b.callback_data
                for row in otc_commands.menu(1, expanded=True).inline_keyboard
                for b in row
            ]
        self.assertIn("otc:mlstats:all:today", buttons)
        self.assertIn("otc:groqrecovery", buttons)

    def test_cycles_remain_and_period_survives_switching(self):
        from bot import otc_commands

        with patch.object(
            otc_commands, "settings", SimpleNamespace(primary_mode="ready")
        ):
            buttons = [
                b for row in otc_commands.stats_menu("7d").inline_keyboard for b in row
            ]
            self.assertIn("otc:stats:all:7d", [b.callback_data for b in buttons])
            self.assertIn("otc:cycles:1:7d", [b.callback_data for b in buttons])
            self.assertTrue(any("Циклы ×4" in b.text for b in buttons))
            buttons = [
                b
                for row in otc_commands.stats_menu("7d", cycles=True).inline_keyboard
                for b in row
            ]
            self.assertIn("otc:stats:1:7d", [b.callback_data for b in buttons])

    def test_same_sent_population_boundary_versions_and_draw(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = PreviewJournal(
                Path(folder) / "primary-ready.db", label="Основной v7"
            )
            # Announced before the reporting boundary but entered after it.
            old = journal.reserve(signal(at=1200), 1180)
            journal.sent(old, 1180)
            journal.finish(old, "WIN", 1, 2, resolved_at=1300)
            draw = journal.reserve(dict(signal(at=1500), model_sha256="v2"), 1480)
            journal.sent(draw, 1480)
            journal.finish(draw, "DRAW", 2, 2, resolved_at=1600)
            pending = journal.reserve(signal(at=1800), 1780)
            journal.sent(pending, 1780)
            rows = read_journal(journal, 1190, 1800)
            self.assertEqual(len(rows), 2)
            text = journal_stats(journal, since=1190, until=1800)
            self.assertIn("Отправлено вовремя: 2", text)
            self.assertIn("Вин 0 (+) · Лос 0", text)
            self.assertIn("Ничьи 1 · Ожидают 1 · Нет данных 0", text)
            self.assertIn("v2", text)
            self.assertIn("fixture", text)
            self.assertLess(len(text), 3500)
            detailed = "\n".join(report({"Основной": journal}, 0, 1800))
            self.assertIn("v2", detailed)

    def test_late_and_failed_delivery_do_not_inflate_counter(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = PreviewJournal(Path(folder) / "primary-ready.db")
            late = journal.reserve(signal(), 1180)
            journal.sent(late, 1190, late=True)
            failed = journal.reserve(signal(at=1500), 1480)
            journal.finish(failed, "NOT_DELIVERED", resolved_at=1480)
            text = journal_stats(journal, until=1800)
            self.assertIn("Отправлено вовремя: 0", text)
            self.assertIn("не доставлено 1, опоздало 1", text)

    def test_platform_evidence_never_changes_journal(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = PreviewJournal(Path(folder) / "primary-ready.db")
            identifier = journal.reserve(signal(), 1180)
            journal.sent(identifier, 1180)
            journal.finish(identifier, "WIN", 1, 2, resolved_at=1300)
            path = Path(folder) / "audit.csv"
            self.assertEqual(export(folder, path), 1)
            with self.assertRaises(FileExistsError):
                export(folder, path)
            self.assertEqual(compare(path), {"total": 1, "unverified": 1})
            with path.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            rows[0].update(
                platform_entry="2", platform_expiry="1", platform_outcome="LOSS"
            )
            with path.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=rows[0].keys())
                writer.writeheader()
                writer.writerows(rows)
            self.assertEqual(compare(path), {"total": 1, "mismatched": 1})
            self.assertEqual(read_journal(journal, 0, 1800)[0]["outcome"], "WIN")
