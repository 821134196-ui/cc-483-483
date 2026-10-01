"""进程内定时任务：开店/闭店盘点提醒、还原失败自动重试。"""
import threading
import time
from datetime import datetime

from .db import today


class Scheduler:
    def __init__(self, svc, interval=30):
        self.svc = svc
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None
        self._last_open_key = None
        self._last_close_key = None
        self._last_retry = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="demo-scheduler", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def tick(self, now_dt=None):
        """执行一次检查，可被测试直接调用。now_dt 为 (datetime, minute_str)。"""
        now_dt = now_dt or datetime.now()
        hm = now_dt.strftime("%H:%M")
        day = today()
        for store in self.svc.list_stores():
            runs = {r["kind"]: r for r in self.svc.today_runs(store["id"], day)}
            open_run = runs.get("open")
            close_run = runs.get("close")
            okey = f"{store['id']}:{day}:open"
            ckey = f"{store['id']}:{day}:close"
            if hm >= store["open_time"] and (not open_run or open_run["status"] != "closed"):
                if self._last_open_key != okey:
                    self.svc.add_reminder(
                        "open_inventory", f"门店「{store['name']}」已到开店时间 {store['open_time']}，请完成开店盘点",
                        store["id"])
                    self._last_open_key = okey
            if hm >= store["close_time"] and (not close_run or close_run["status"] != "closed"):
                if self._last_close_key != ckey:
                    self.svc.add_reminder(
                        "close_inventory", f"门店「{store['name']}」已到闭店时间 {store['close_time']}，请核对序列号及配件后闭店",
                        store["id"])
                    self._last_close_key = ckey
        # 失败重试：每分钟最多一轮
        retry_key = now_dt.strftime("%Y-%m-%dT%H:%M")
        if self._last_retry != retry_key:
            results = self.svc.retry_failed_restores()
            if results:
                failed = [r for r in results if not r.get("ok")]
                ok = [r for r in results if r.get("ok")]
                if ok:
                    self.svc.add_reminder(
                        "restore_retry", f"自动重试：{len(ok)} 台设备还原成功")
                if failed:
                    self.svc.add_reminder(
                        "restore_retry",
                        f"自动重试：仍有 {len(failed)} 台设备还原失败，需技术人员处理")
            self._last_retry = retry_key

    def _loop(self):
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception as e:  # 调度线程不能因单次异常退出
                print(f"[scheduler] tick error: {e}")
