from typing import Any, Dict

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .alerts import AlertManager
from .jobs import daily_scan_job


class TradingScheduler:
    def __init__(self, config: dict):
        self.config = config
        self.sched_config = config.get("scheduler", {})
        self.alert_manager = AlertManager(config)
        self.scheduler = BackgroundScheduler(
            timezone=self.sched_config.get("timezone", "US/Eastern")
        )

    def start(self):
        scan_time = self.sched_config.get("scan_time", "07:00")
        hour, minute = scan_time.split(":")

        self.scheduler.add_job(
            daily_scan_job,
            trigger=CronTrigger(
                day_of_week="mon-fri", hour=int(hour), minute=int(minute),
            ),
            args=[self.config, self.alert_manager],
            id="daily_scan",
            name="Daily Watchlist Scan",
        )

        for check_time in self.sched_config.get("portfolio_check_times", []):
            h, m = check_time.split(":")
            self.scheduler.add_job(
                lambda: None,
                trigger=CronTrigger(
                    day_of_week="mon-fri", hour=int(h), minute=int(m),
                ),
                id=f"portfolio_check_{check_time}",
                name=f"Portfolio Check {check_time}",
            )

        self.scheduler.start()

    def stop(self):
        self.scheduler.shutdown(wait=False)

    def status(self) -> Dict[str, Any]:
        jobs = self.scheduler.get_jobs()
        return {
            "running": self.scheduler.running if hasattr(self.scheduler, "running") else False,
            "jobs": [
                {"id": j.id, "name": j.name, "next_run": str(j.next_run_time)}
                for j in jobs
            ],
        }
