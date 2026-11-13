import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from services.otc_report_sync import GitHubError, export, publish


class PublicReportTests(unittest.TestCase):
    def test_allowlist_excludes_secrets_raw_evidence_and_personal_ids(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "data").mkdir()
            (root / "logs").mkdir()
            (root / ".env").write_text("TOKEN=do-not-export", encoding="utf-8")
            (root / "logs/runtime-health.json").write_text(
                json.dumps(
                    dict(
                        at=1000,
                        running=True,
                        quote_age=1,
                        error="secret URL do-not-export",
                        pid=12345,
                        run_id="do-not-export",
                    )
                ),
                encoding="utf-8",
            )
            with closing(sqlite3.connect(root / "data/primary-ready.db")) as db:
                db.execute(
                    "CREATE TABLE predictions(entry_at,expires_at,sent_at,outcome,model_sha,evidence,created_at)"
                )
                db.execute(
                    "INSERT INTO predictions VALUES(?,?,?,?,?,?,?)",
                    (
                        900,
                        960,
                        880,
                        "WIN",
                        "primary-ready-v6-one-confirmation",
                        '{"chat_id":123456789,"key":"do-not-export"}',
                        870,
                    ),
                )
                db.commit()
            result = export(root, now=1100)
            text = json.dumps(result)
            self.assertNotIn("do-not-export", text)
            self.assertNotIn("123456789", text)
            self.assertNotIn('"pid"', text)
            self.assertEqual(result["sources"]["primary"]["groups"][0]["win_rate"], 1)
            self.assertTrue(result["runtime"]["running"])
            self.assertFalse(export(root, now=1201)["runtime"]["running"])
            self.assertFalse((root / "data/groq-preview.db").exists())

    def test_public_upload_requires_explicit_flag(self):
        with patch(
            "services.otc_report_sync.api",
            return_value={"private": False, "default_branch": "main"},
        ) as api:
            with self.assertRaises(ValueError):
                publish({}, "owner/repo", "termux")
            self.assertEqual(api.call_count, 1)

    def test_upload_uses_report_branch_device_path_and_existing_sha(self):
        answers = [{"private": False, "default_branch": "main"}, {}, {"sha": "old"}, {}]
        with patch("services.otc_report_sync.api", side_effect=answers) as api:
            url = publish({"safe": True}, "owner/repo", "termux", allow_public=True)
        self.assertIn("/blob/reports/reports/termux/latest.json", url)
        method, path, payload = api.call_args.args
        self.assertEqual(method, "PUT")
        self.assertEqual(path, "repos/owner/repo/contents/reports/termux/latest.json")
        self.assertEqual(payload["branch"], "reports")
        self.assertEqual(payload["sha"], "old")

    def test_conflict_refetches_sha_and_missing_branch_is_created(self):
        answers = [
            {"private": True, "default_branch": "main"},
            GitHubError(404),
            {"object": {"sha": "base"}},
            {},
            GitHubError(404),
            GitHubError(409),
            {"sha": "new"},
            {},
        ]
        with patch("services.otc_report_sync.api", side_effect=answers) as api:
            publish({}, "owner/repo", "termux")
        self.assertEqual(api.call_args.args[2]["sha"], "new")

    def test_invalid_device_cannot_escape_report_folder(self):
        with patch("services.otc_report_sync.api") as api:
            with self.assertRaises(ValueError):
                publish({}, "owner/repo", "../main")
            api.assert_not_called()
