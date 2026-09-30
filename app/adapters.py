"""模拟适配器：设备管理（MDM）与防盗充电底座。

- DeviceManagementAdapter：上报在线/电量/位置/配置（账号、应用、壁纸），
  支持刷入离线配置包；flaky 设备前 N 次刷机会失败，用于演示失败重试。
- AntiTheftAdapter：上报防盗线连接/报警/充电状态。

适配器状态持久化到 data/adapter_state.json，重启后保持一致。
"""
import json
import os
import threading


class _JsonStore:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                self.state = json.load(f)
        else:
            self.state = {"devices": {}, "docks": {}}
            self._save()

    def _save(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def save(self):
        with self._lock:
            self._save()


class DeviceManagementAdapter:
    """模拟 MDM：设备侧真实状态的唯一来源（盘点时用来比对）。"""

    def __init__(self, store: _JsonStore):
        self._s = store

    def enroll(self, serial, *, online=True, battery=100, booth_code=None,
               account_version="3.2.0", apps=None, wallpaper_id="wp-default",
               logged_in=True, fail_flashes=0):
        self._s.state["devices"][serial] = {
            "online": online,
            "battery": battery,
            "booth_hint": booth_code,       # 设备物理所在展台（可能错位）
            "present": True,
            "config": {
                "account_version": account_version,
                "apps": apps or {},
                "wallpaper_id": wallpaper_id,
            },
            "logged_in": logged_in,
            "flash_attempts": 0,
            "fail_flashes": fail_flashes,   # 前 N 次刷机失败（演示重试）
        }
        self._s.save()

    def _dev(self, serial):
        d = self._s.state["devices"].get(serial)
        if d is None:
            raise KeyError(f"适配器未登记设备 {serial}")
        return d

    def fetch_status(self, serial):
        d = self._dev(serial)
        return {
            "online": d["online"],
            "battery": d["battery"],
            "present": d["present"],
            "booth_hint": d["booth_hint"],
            "logged_in": d["logged_in"],
        }

    def fetch_config(self, serial):
        d = self._dev(serial)
        cfg = d["config"]
        return {
            "account_version": cfg["account_version"],
            "apps": dict(cfg["apps"]),
            "wallpaper_id": cfg["wallpaper_id"],
            "logged_in": d["logged_in"],
        }

    def apply_package(self, serial, package):
        """刷入离线配置包。flaky 设备前 fail_flashes 次返回 False。"""
        d = self._dev(serial)
        d["flash_attempts"] += 1
        if d["flash_attempts"] <= d.get("fail_flashes", 0):
            self._s.save()
            return False
        d["config"] = {
            "account_version": package["account_version"],
            "apps": dict(package["apps"]),
            "wallpaper_id": package["wallpaper_id"],
        }
        d["logged_in"] = True
        self._s.save()
        return True

    # ---- 演示/种子用：篡改设备侧状态 ----
    def tamper(self, serial, **changes):
        d = self._dev(serial)
        cfg_keys = {"account_version", "apps", "wallpaper_id"}
        for k, v in changes.items():
            if k in cfg_keys:
                d["config"][k] = v
            else:
                d[k] = v
        self._s.save()


class AntiTheftAdapter:
    """模拟防盗充电底座。"""

    def __init__(self, store: _JsonStore):
        self._s = store

    def enroll(self, serial, *, connected=True, alarm=False, charging=True):
        self._s.state["docks"][serial] = {
            "connected": connected, "alarm": alarm, "charging": charging,
        }
        self._s.save()

    def status(self, serial):
        return dict(self._s.state["docks"].get(
            serial, {"connected": False, "alarm": False, "charging": False}))

    def set(self, serial, **changes):
        dock = self._s.state["docks"].setdefault(
            serial, {"connected": True, "alarm": False, "charging": True})
        dock.update(changes)
        self._s.save()


def build_adapters(data_dir):
    store = _JsonStore(os.path.join(data_dir, "adapter_state.json"))
    return DeviceManagementAdapter(store), AntiTheftAdapter(store)
