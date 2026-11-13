import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

from services.otc_ml.preview import PreviewJournal
from web.mini_app import create_app, read_dashboard, verify_init_data


def make_init_data(token, user_id, now):
    fields = {"auth_date": str(now), "user": json.dumps({"id": user_id})}
    payload = "\n".join(k + "=" + v for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class AuthTests(unittest.TestCase):
    def test_valid_owner_and_rejects_tampering_or_other_users(self):
        token = "dummy-token"
        raw = make_init_data(token, 100, 2000)
        self.assertTrue(verify_init_data(raw, token, 100, now=2001))
        self.assertFalse(verify_init_data(raw, token, 101, now=2001))
        self.assertFalse(verify_init_data(raw, token, 100, now=6002))
        self.assertFalse(verify_init_data(raw.replace("100", "101"), token, 100, now=2001))
        self.assertFalse(verify_init_data(raw + "&user=x", token, 100, now=2001))
        self.assertFalse(verify_init_data("", token, 100, now=2001))


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        journal = PreviewJournal(Path(self.tmp.name) / "ready.db", label="Основной")
        self.service = SimpleNamespace(
            main_ready=SimpleNamespace(journal=journal), ml_preview=None,
            groq_preview=None, phase="остановлен", monitored=0, last_tick=None,
            running=lambda: False,
        )
        self.app = create_app(self.service, "dummy-token", 100)
        self.server = TestServer(self.app)
        self.client = TestClient(self.server)
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def test_private_api_rejects_unsigned_and_foreign_user(self):
        response = await self.client.get("/api/dashboard")
        self.assertEqual(response.status, 401)
        await response.release()
        raw = make_init_data("dummy-token", 101, int(__import__("time").time()))
        response = await self.client.get("/api/dashboard", headers={"Authorization": "tma " + raw})
        self.assertEqual(response.status, 401)
        await response.release()

    async def test_signed_owner_receives_only_safe_summary(self):
        raw = make_init_data("dummy-token", 100, int(__import__("time").time()))
        response = await self.client.get("/api/dashboard", headers={"Authorization": "tma " + raw})
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertEqual(data["strategies"]["Основной"]["total"], 0)
        self.assertFalse(data["platform_prices_verified"])
        self.assertNotIn("token", json.dumps(data))

    async def test_html_contains_no_private_data(self):
        response = await self.client.get("/")
        self.assertEqual(response.status, 200)
        html = await response.text()
        self.assertIn("Trading", html)
        self.assertNotIn("dummy-token", html)
