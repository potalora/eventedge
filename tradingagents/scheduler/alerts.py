import apprise

class AlertManager:
    def __init__(self, config: dict):
        alerts_config = config.get("alerts", {})
        self.enabled = alerts_config.get("enabled", False)
        self.notify_on = set(alerts_config.get("notify_on", []))
        self.channels = alerts_config.get("channels", [])
        self._apprise = None
        if self.enabled and self.channels:
            self._apprise = apprise.Apprise()
            for channel in self.channels:
                self._apprise.add(channel)

    def send(self, alert_type: str, message: str, title: str = "TradingAgents"):
        if not self.enabled:
            return
        if alert_type not in self.notify_on:
            return
        if self._apprise is None:
            return
        self._apprise.notify(title=f"{title}: {alert_type}", body=message)
