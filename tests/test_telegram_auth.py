import hashlib
import hmac
import io
import json
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlencode

import api.index as vercel_api
import web as local_api
from telegram_auth import authenticate_init_data


TOKEN = "test-token"
NOW = 1_700_000_000


def signed_init_data(**overrides):
    values = {
        "auth_date": str(NOW),
        "query_id": "query",
        "user": json.dumps({"id": 42, "first_name": "Test"}, separators=(",", ":")),
    }
    values.update(overrides)
    check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


class TelegramInitDataTests(unittest.TestCase):
    def test_valid_payload_returns_verified_user_id(self):
        self.assertEqual((42, None), authenticate_init_data(signed_init_data(), now=NOW, bot_token=TOKEN))

    def test_tampered_payload_is_rejected(self):
        payload = signed_init_data().replace("query_id=query", "query_id=changed")
        self.assertEqual((None, "Autentikasi Telegram tidak valid."),
                         authenticate_init_data(payload, now=NOW, bot_token=TOKEN))

    def test_expired_future_and_malformed_payloads_are_rejected(self):
        self.assertEqual((None, "Autentikasi Telegram telah kedaluwarsa."),
                         authenticate_init_data(signed_init_data(auth_date=str(NOW - 3601)), now=NOW, bot_token=TOKEN))
        self.assertEqual((None, "Autentikasi Telegram telah kedaluwarsa."),
                         authenticate_init_data(signed_init_data(auth_date=str(NOW + 1)), now=NOW, bot_token=TOKEN))
        self.assertEqual((None, "Autentikasi Telegram tidak valid."),
                         authenticate_init_data("auth_date=1&hash=x", now=NOW, bot_token=TOKEN))

    def test_invalid_hash_and_user_id_are_rejected(self):
        payload = signed_init_data().replace("hash=", "hash=nonascii%F0%9F%98%80")
        self.assertEqual((None, "Autentikasi Telegram tidak valid."),
                         authenticate_init_data(payload, now=NOW, bot_token=TOKEN))
        self.assertEqual((None, "Autentikasi Telegram tidak valid."),
                         authenticate_init_data(signed_init_data(user='{"id":0}'), now=NOW, bot_token=TOKEN))

    def test_missing_token_duplicate_keys_and_wrong_bot_are_rejected(self):
        for payload, token in ((signed_init_data(), ""), (signed_init_data(), "different-bot"),
                               (signed_init_data() + "&auth_date=" + str(NOW), TOKEN)):
            with self.subTest(token=token):
                user_id, error = authenticate_init_data(payload, now=NOW, bot_token=token)
                self.assertIsNone(user_id)
                self.assertIsNotNone(error)


class LocalAuthenticationTests(unittest.TestCase):
    def handler(self, payload):
        handler = object.__new__(local_api.Handler)
        handler._json_body = Mock(return_value=(payload, None))
        handler._send = Mock()
        return handler

    def test_all_endpoints_reject_unsigned_user_id_without_database_access(self):
        for endpoint in ("register", "products", "add_product", "update_stock", "delete_product", "stats"):
            with self.subTest(endpoint=endpoint), patch.object(local_api, "get_db") as get_db:
                handler = self.handler({"user_id": 42})
                getattr(handler, "_handle_" + endpoint)()
                self.assertEqual(handler._send.call_args.args[0], 401)
                get_db.assert_not_called()

    @patch("telegram_auth.time.time", return_value=NOW)
    @patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": TOKEN})
    def test_stats_uses_signed_identity_and_rejects_nonreseller(self, clock):
        for role_error in (None, "Hanya reseller yang dapat mengakses."):
            with self.subTest(role_error=role_error), \
                    patch.object(local_api, "verify_user", return_value=({}, role_error)) as verify, \
                    patch.object(local_api, "get_reseller_stats", return_value={"total_stock": 7}) as stats:
                handler = self.handler({"init_data": signed_init_data(), "user_id": 999})
                handler._handle_stats()
                verify.assert_called_once_with(42)
                if role_error:
                    stats.assert_not_called()
                    self.assertEqual(handler._send.call_args.args[0], 403)
                else:
                    stats.assert_called_once_with(42)
                    self.assertEqual(handler._send.call_args.args[0], 200)


class ResellerStatsTests(unittest.TestCase):
    def test_shipped_orders_are_included_in_revenue_for_both_servers(self):
        for module in (local_api, vercel_api):
            with self.subTest(module=module.__name__), patch.object(module, "get_db") as get_db:
                cursor = get_db.return_value.cursor.return_value.__enter__.return_value
                cursor.fetchone.side_effect = [
                    {"cnt": 1, "total_stock": 7}, {"cnt": 3, "revenue": 50},
                ]
                self.assertEqual(50, module.get_reseller_stats(42)["total_revenue"])
                self.assertIn("'shipped'", cursor.execute.call_args_list[1].args[0])


class VercelAuthenticationTests(unittest.TestCase):
    def call_api(self, payload):
        raw = json.dumps(payload).encode()
        captured = {}

        def start_response(status, headers):
            captured["status"] = status
            captured["headers"] = headers

        output = vercel_api.application({
            "REQUEST_METHOD": "POST", "PATH_INFO": "/api/stats",
            "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw),
        }, start_response)
        return captured["status"], json.loads(b"".join(output))

    def test_raw_user_id_without_init_data_is_rejected(self):
        status, response = self.call_api({"user_id": 999})
        self.assertEqual("401 UNAUTHORIZED", status)
        self.assertFalse(response["success"])

    @patch("api.index.get_reseller_stats", return_value={"total_stock": 7, "total_orders": 2, "total_revenue": 50})
    @patch("api.index.verify_user", return_value=({}, None))
    @patch("api.index.authenticate_init_data", return_value=(42, None))
    def test_stats_uses_verified_identity_not_raw_user_id(self, authenticate, verify, stats):
        status, response = self.call_api({"init_data": "verified", "user_id": 999})
        self.assertEqual("200 OK", status)
        self.assertTrue(response["success"])
        verify.assert_called_once_with(42)
        stats.assert_called_once_with(42)

