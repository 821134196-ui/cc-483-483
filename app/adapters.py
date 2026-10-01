"""模拟适配器：设备管理（MDM）与防盗底座。

真实环境中这些接口由 MDM 平台与防盗器硬件提供；此处返回样例数据，
数据以本地 SQLite 中的 device_runtime / docks 为"真实设备"状态来源，
适配器负责模拟下发还原指令后的状态变化与随机失败。
"""
import json
import random

from .errors import NotFound
from .db import now


class DeviceManagementAdapter:
    """模拟 MDM：读取设备状态、下发系统还原配置。"""

    def __init__(self, db, rng=None):
        self.db = db
        self.rng = rng or random.Random(91027)

    def snapshot(self, serial):
        row = self.db.one(
            "SELECT rt.*, d.model FROM device_runtime rt JOIN devices d ON d.serial = rt.serial "
            "WHERE rt.serial = ?", (serial,))
        if row is None:
            raise NotFound(f"设备 {serial} 不在线或未登记")
        return {
            "serial": serial,
            "model": row["model"],
            "online": bool(row["online"]),
            "battery": row["battery"],
            "network_state": row["network_state"],
            "logged_in": bool(row["logged_in"]),
            "account_version": row["account_version"],
            "apps": self.db.jloads(row["apps"], []),
            "wallpaper": row["wallpaper"],
            "settings": self.db.jloads(row["settings"], {}),
            "content_flag": row["content_flag"],
        }

    def push_restore(self, serial, expected, simulate_failure=False):
        """下发还原包。返回 (ok, actual, error)。

        设备离线 / faulty 标记会导致失败；simulate_failure 用于演示还原失败重试。
        """
        rt = self.db.one("SELECT * FROM device_runtime WHERE serial = ?", (serial,))
        if rt is None:
            return False, None, "设备未注册"
        if not rt["online"]:
            return False, self.snapshot(serial), "设备离线，无法下发还原包"
        if simulate_failure or rt["faulty"]:
            actual = self.snapshot(serial)
            return False, actual, "还原写入中断（线缆接触不良），已加入重试"
        # 还原成功：运行时状态重置为期望配置
        self.db.execute(
            "UPDATE device_runtime SET logged_in=1, account_version=?, apps=?, wallpaper=?, "
            "settings=?, content_flag=NULL, appearance='ok', network_state='wifi', "
            "faulty=0, updated_at=? WHERE serial=?",
            (expected["account_version"], json.dumps(expected["apps"], ensure_ascii=False),
             expected["wallpaper"], json.dumps(expected["settings"], ensure_ascii=False),
             now(), serial))
        self.db.commit()
        return True, self.snapshot(serial), None


class SecurityDockAdapter:
    """模拟防盗充电底座：充电、连接、防盗线报警。"""

    def __init__(self, db):
        self.db = db

    def dock_status(self, code):
        row = self.db.one("SELECT * FROM docks WHERE code = ?", (code,))
        if row is None:
            raise NotFound(f"底座 {code} 不存在")
        return {"code": code, "device_serial": row["device_serial"],
                "connected": bool(row["connected"]), "alarm": bool(row["alarm"]),
                "charging": bool(row["connected"] and not row["alarm"])}

    def trigger_alarm(self, code, on=True):
        self.db.execute("UPDATE docks SET alarm=? WHERE code=?", (1 if on else 0, code))
        self.db.commit()
        return self.dock_status(code)
