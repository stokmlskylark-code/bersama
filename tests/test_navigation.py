import os
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from bot import Config, ShopBot, TelegramError


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.config = Config("123:test", frozenset({1}), "Toko Test", "Bank", "@support",
                             "unused", 24, channel_username="test_channel")
        self.store = Mock()
        self.store.user.return_value = {
            "id": 2, "name": "Pembeli", "role": "customer", "reseller_request": 0,
        }
        self.store.user_stats.return_value = {"completed_orders": 0, "total_spending": 0}
        self.api = Mock()
        self.bot = ShopBot(self.config, self.store, self.api)

    def start(self, status):
        self.api.call.return_value = status
        self.bot.command(2, "/start welcome", 123)

    def texts(self):
        return "\n".join(call.kwargs.get("text", "") for call in self.api.call.call_args_list)

    def test_members_get_menu(self):
        for status in ("member", "administrator", "creator"):
            with self.subTest(status=status):
                self.api.reset_mock()
                self.start({"status": status})
                self.api.call.assert_any_call("getChatMember", chat_id="@test_channel", user_id=2)
                self.assertIn("Halo Pembeli", self.texts())

    def test_nonmembers_get_join_link_and_retry_without_menu(self):
        for status in ("left", "kicked", "unknown", "restricted"):
            with self.subTest(status=status):
                self.api.reset_mock()
                self.start({"status": status, "is_member": False})
                self.assertNotIn("Halo Pembeli", self.texts())
                markup = self.api.call.call_args.kwargs["reply_markup"]["inline_keyboard"]
                self.assertEqual(markup[0][0]["url"], "https://t.me/test_channel")
                self.assertEqual(markup[1][0]["callback_data"], "check_join")

    def test_restricted_current_member_gets_menu(self):
        self.start({"status": "restricted", "is_member": True})
        self.assertIn("Halo Pembeli", self.texts())

    def test_malformed_membership_response_does_not_grant_access(self):
        self.start(None)
        self.assertNotIn("Halo Pembeli", self.texts())
        self.assertIn("belum dapat diverifikasi", self.texts())

    def test_telegram_failure_does_not_grant_access_or_claim_nonmembership(self):
        def call(method, **kwargs):
            if method == "getChatMember":
                raise TelegramError(403)
            return True

        self.api.call.side_effect = call
        with self.assertLogs("shopbot", level="WARNING"):
            self.bot.command(2, "/start", 123)
        self.assertNotIn("Halo Pembeli", self.texts())
        self.assertIn("belum dapat diverifikasi", self.texts())
        self.assertIn("@support", self.texts())

    def test_retry_checks_fresh_membership(self):
        self.start({"status": "left"})
        self.api.reset_mock()
        self.api.call.return_value = {"status": "member"}
        self.bot.callback(2, "check_join")
        self.api.call.assert_any_call("getChatMember", chat_id="@test_channel", user_id=2)
        self.assertIn("Halo Pembeli", self.texts())

    def test_unconfigured_channel_keeps_menu_available(self):
        self.bot.config = replace(self.config, channel_username="")
        self.bot.command(2, "/start", 123)
        self.assertTrue(all(call.args[0] != "getChatMember" for call in self.api.call.call_args_list))
        self.assertIn("Halo Pembeli", self.texts())

    def test_home_checks_channel_again(self):
        self.api.call.return_value = {"status": "left"}
        self.bot.callback(2, "home")
        self.assertNotIn("Halo Pembeli", self.texts())

    def test_reseller_dashboard_has_summary_and_navigation_without_web(self):
        self.store.user.return_value["role"] = "reseller"
        self.store.stats.return_value = {
            "paid": {"count": 2, "total": 16000, "saving": 4000},
            "pending_payment": {"count": 1, "total": 8000, "saving": 2000},
        }
        self.bot.command(2, "/reseller", 123)
        self.store.stats.assert_called_once_with(2, False)
        self.assertIn("DASHBOARD RESELLER", self.texts())
        self.assertIn("Total belanja terkonfirmasi: Rp16.000", self.texts())
        self.assertIn("Penghematan harga reseller: Rp4.000", self.texts())
        markup = self.api.call.call_args.kwargs["reply_markup"]["inline_keyboard"]
        actions = [button.get("callback_data") for row in markup for button in row]
        self.assertTrue({"my_products:0", "orders:0", "home"}.issubset(actions))

    def test_legacy_dashboard_button_sends_valid_web_app_markup(self):
        self.store.user.return_value["role"] = "reseller"
        self.store.stats.return_value = {}
        self.bot.config = replace(self.config, webapp_url="https://shop.example")
        self.bot.callback(2, "dashboard_web")
        call = self.api.call.call_args
        self.assertEqual(call.args, ("sendMessage",))
        self.assertEqual(call.kwargs["reply_markup"]["inline_keyboard"][0][0]["web_app"],
                         {"url": "https://shop.example/dashboard"})

    def test_customer_cannot_open_reseller_dashboard(self):
        self.bot.callback(2, "dashboard_web")
        self.store.stats.assert_not_called()
        self.assertIn("Ajukan melalui", self.texts())

    def test_channel_configuration(self):
        env = {"TELEGRAM_BOT_TOKEN": "123:test", "ADMIN_IDS": "1", "SUPPORT_CONTACT": "@support",
               "PAYMENT_PROVIDER": "manual", "PAYMENT_INSTRUCTIONS": "Bank"}
        with patch.dict(os.environ, env, clear=True):
            for value in ("@test_channel", "test_channel", ""):
                with patch.dict(os.environ, {"CHANNEL_USERNAME": value}):
                    self.assertEqual(Config.from_env().channel_username, value.lstrip("@"))
            for value in ("https://t.me/test_channel", "-100123456", "invalid channel"):
                with patch.dict(os.environ, {"CHANNEL_USERNAME": value}):
                    with self.assertRaisesRegex(ValueError, "CHANNEL_USERNAME"):
                        Config.from_env()


if __name__ == "__main__":
    unittest.main()
