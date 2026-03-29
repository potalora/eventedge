import pytest
from unittest.mock import patch, MagicMock
from tradingagents.scheduler.alerts import AlertManager

class TestAlertManager:
    def test_init_with_empty_channels(self):
        config = {"alerts": {"enabled": True, "channels": [], "notify_on": []}}
        am = AlertManager(config)
        assert am.enabled is True

    def test_disabled_does_not_send(self):
        config = {"alerts": {"enabled": False, "channels": [], "notify_on": []}}
        am = AlertManager(config)
        am.send("new_signal", "Test message")

    @patch("tradingagents.scheduler.alerts.apprise.Apprise")
    def test_send_calls_apprise(self, mock_apprise_cls):
        mock_ap = MagicMock()
        mock_ap.notify.return_value = True
        mock_apprise_cls.return_value = mock_ap

        config = {
            "alerts": {
                "enabled": True,
                "channels": ["json://localhost"],
                "notify_on": ["new_signal"],
            }
        }
        am = AlertManager(config)
        am.send("new_signal", "SOFI rated BUY")
        mock_ap.notify.assert_called_once()

    @patch("tradingagents.scheduler.alerts.apprise.Apprise")
    def test_send_skips_unsubscribed_types(self, mock_apprise_cls):
        mock_ap = MagicMock()
        mock_apprise_cls.return_value = mock_ap

        config = {
            "alerts": {
                "enabled": True,
                "channels": ["json://localhost"],
                "notify_on": ["stop_loss"],
            }
        }
        am = AlertManager(config)
        am.send("new_signal", "Test")
        mock_ap.notify.assert_not_called()
