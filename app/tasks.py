"""进程内任务：盘点提醒 + 还原失败自动重试。

后台守护线程，每 interval 秒执行一轮：
- 开店/闭店盘点未完成 → 写入通知提醒（当日去重）
- 进行中的盘点超过 30 分钟 → 提醒
- 失败的还原任务 → 自动重试（不超过 max_retry）
"""
import threading
from datetime import datetime


class TaskRunner:
    def __init__(self, svc, interval=20):
        self.svc = svc
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None
        self.last_run = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="task-runner")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # 任务异常不拖垮主进程
                self.svc.notify("task", "error", f"后台任务异常：{exc}")
            self._stop.wait(self.interval)

    def _notified_today(self, kind, prefix):
        today = datetime.now().date().isoformat()
        rows = self.svc.db.query(
            "SELECT id FROM notifications WHERE kind=? AND message LIKE ?"
            " AND created_at >= ?", (kind, prefix + "%", today))
        return bool(rows)

    def tick(self):
        self.last_run = datetime.now().isoformat(timespec="seconds")
        hour = datetime.now().hour
        today = datetime.now().date().isoformat()
        stores = self.svc.db.query("SELECT * FROM stores")
        for s in stores:
            if hour >= 9:
                done = self.svc.db.one(
                    "SELECT id FROM inventories WHERE store_id=? AND type='open'"
                    " AND biz_date=? AND status='completed'", (s["id"], today))
                msg = f"【{s['name']}】今日开店盘点尚未完成"
                if not done and not self._notified_today("remind", msg):
                    self.svc.notify("remind", "warn", msg)
            if hour >= 21:
                done = self.svc.db.one(
                    "SELECT id FROM inventories WHERE store_id=? AND type='close'"
                    " AND biz_date=? AND status='completed'", (s["id"], today))
                msg = f"【{s['name']}】今日闭店盘点尚未完成"
                if not done and not self._notified_today("remind", msg):
                    self.svc.notify("remind", "warn", msg)
            stale = self.svc.db.query(
                "SELECT * FROM inventories WHERE store_id=? AND status='in_progress'"
                " AND started_at < datetime('now','localtime','-30 minutes')",
                (s["id"],))
            for inv in stale:
                msg = f"【{s['name']}】盘点单 #{inv['id']} 已进行超过 30 分钟，请尽快完成"
                if not self._notified_today("remind", msg):
                    self.svc.notify("remind", "warn", msg)
        # 失败重试
        retried = self.svc.retry_failed_restores()
        for r in retried:
            self.svc.notify("restore", "info",
                            f"自动重试还原任务 #{r['task_id']} → {r['status']}")

    def status(self):
        return {"running": bool(self._thread and self._thread.is_alive()),
                "interval": self.interval, "last_run": self.last_run}
