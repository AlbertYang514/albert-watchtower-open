import json
import unittest
from unittest import mock

import watchtower


class ReportTypeTests(unittest.TestCase):
    def test_boundaries(self):
        self.assertEqual(watchtower.get_report_type(5), "morning")
        self.assertEqual(watchtower.get_report_type(11), "morning")
        self.assertEqual(watchtower.get_report_type(12), "daily")
        self.assertEqual(watchtower.get_report_type(18), "evening")
        self.assertEqual(watchtower.get_report_type(23), "evening")
        self.assertEqual(watchtower.get_report_type(0), "daily")


class ConfigAndTitleTests(unittest.TestCase):
    def test_default_title(self):
        self.assertEqual(
            watchtower.report_title({}, "morning"),
            "小鸡值班室早报🐣",
        )

    def test_custom_title(self):
        config = {"report": {"titles": {"morning": "Plain morning"}}}
        self.assertEqual(watchtower.report_title(config, "morning"), "Plain morning")

    def test_example_config_has_only_placeholders(self):
        with open("config.example.json", "r", encoding="utf-8") as f:
            raw = f.read()
        config = json.loads(raw)
        self.assertIn("YOUR_", config["email"]["username"])
        self.assertIn("YOUR_", config["email"]["password"])
        self.assertIn("YOUR_", config["llm"]["api_key"])
        self.assertEqual(config["llm"]["enabled"], False)


class ReportBuilderTests(unittest.TestCase):
    def _config(self):
        return {
            "report": {
                "server_label": "值班机",
                "sites_heading": "站点",
                "titles": {"daily": "Status report"},
            },
            "locations": [
                {"name": "地点 A", "lat": 1.0, "lon": 2.0},
                {"name": "地点 B", "lat": 3.0, "lon": 4.0},
            ],
            "sites": [
                {"name": "主站", "url": "https://example.com"},
                {"name": "备用站", "url": "https://example.org"},
            ],
        }

    def test_build_report_with_mocks(self):
        calls = []

        def fake_weather(lat, lon, timezone="auto"):
            calls.append((lat, lon, timezone))
            return "晴，当前20°C"

        def fake_check_url(url, timeout=10):
            if url == "https://example.com":
                return True, "OK"
            return False, "HTTP 503"

        vps = {
            "status": "OK",
            "uptime_str": "1天2小时",
            "disk_pct": 30,
            "mem_pct": 40,
        }

        with mock.patch.object(watchtower, "fetch_weather", side_effect=fake_weather), \
             mock.patch.object(watchtower, "check_vps", return_value=vps), \
             mock.patch.object(watchtower, "check_url", side_effect=fake_check_url):
            body = watchtower.build_report(self._config(), "daily")

        self.assertIn("Status report", body)
        self.assertIn("地点 A：晴，当前20°C", body)
        self.assertIn("地点 B：晴，当前20°C", body)
        self.assertIn("值班机", body)
        self.assertIn("磁盘使用：30%", body)
        self.assertIn("主站：OK", body)
        self.assertIn("备用站：FAIL（HTTP 503）", body)
        self.assertEqual(calls, [(1.0, 2.0, "auto"), (3.0, 4.0, "auto")])

    def test_subject_ok_and_alert(self):
        ok = watchtower._build_subject("一切正常\n主站：OK", "日报")
        alert = watchtower._build_subject("主站：FAIL（HTTP 500）", "日报")
        self.assertTrue(ok.startswith("[Watchtower OK]"))
        self.assertTrue(alert.startswith("[Watchtower ALERT]"))
        self.assertIn("主站", alert)


class EmailTests(unittest.TestCase):
    def test_send_email_uses_mock_smtp(self):
        config = {
            "email": {
                "smtp_host": "smtp.example.com",
                "smtp_port": 465,
                "smtp_ssl": True,
                "username": "sender@example.com",
                "password": "fake-authorization-code",
                "from": "sender@example.com",
                "to": "receiver@example.com",
            }
        }
        server = mock.MagicMock()
        with mock.patch.object(watchtower.smtplib, "SMTP_SSL", return_value=server):
            ok, err = watchtower.send_email(config, "subject", "body")

        self.assertTrue(ok)
        self.assertIsNone(err)
        server.login.assert_called_once_with("sender@example.com", "fake-authorization-code")
        server.send_message.assert_called_once()
        server.quit.assert_called_once()

    def test_send_email_reports_auth_failure(self):
        config = {
            "email": {
                "smtp_host": "smtp.example.com",
                "smtp_port": 465,
                "smtp_ssl": True,
                "username": "sender@example.com",
                "password": "bad",
                "from": "sender@example.com",
                "to": "receiver@example.com",
            }
        }
        server = mock.MagicMock()
        server.login.side_effect = watchtower.smtplib.SMTPAuthenticationError(535, b"bad auth")
        with mock.patch.object(watchtower.smtplib, "SMTP_SSL", return_value=server):
            ok, err = watchtower.send_email(config, "subject", "body")

        self.assertFalse(ok)
        self.assertIn("SMTP", err)


class LLMCleanerTests(unittest.TestCase):
    def test_clean_observation_rejects_fragment(self):
        self.assertIsNone(watchtower._clean_observation_output("好"))

    def test_clean_observation_adds_period(self):
        result = watchtower._clean_observation_output("系统状态正常，值班室已经到岗")
        self.assertTrue(result.endswith("。"))

    def test_clean_llm_output_strips_markdown(self):
        result = watchtower._clean_llm_output("**今日观察：** 一切正常。第二句")
        self.assertEqual(result, "一切正常。")


if __name__ == "__main__":
    unittest.main()
