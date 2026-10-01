"""演示数据：门店、展台、设备、账号基线、用户，以及离线/错位/调店样例。

一条本地命令生成全部样例：
    python3 -m app.seed --reset
"""
import argparse
import json
import os

from .db import DB, now
from .services import StoreService

DEMO_USERS = [
    ("张导购", "staff", "1001"),
    ("李店长", "manager", "2002"),
    ("王技术", "tech", "3003"),
]

# 机型 -> 演示账号/应用/壁纸/设置基线
PROFILES = {
    "Aurora X12": {
        "account_version": "demo-acct-3.2",
        "apps": ["演示中心", "相机实验室", "健康样板", "零售助手", "游戏空间"],
        "wallpaper": "wallpaper-aurora-blue.jpg",
        "settings": {"亮度": 70, "自动锁屏": "永不", "蓝牙": True, "定位": "使用期间", "音量": 40},
    },
    "Aurora X12 Pro": {
        "account_version": "demo-acct-3.2",
        "apps": ["演示中心", "相机实验室", "健康样板", "零售助手", "游戏空间", "Pro 影像"],
        "wallpaper": "wallpaper-aurora-pro.jpg",
        "settings": {"亮度": 75, "自动锁屏": "永不", "蓝牙": True, "定位": "使用期间", "音量": 40},
    },
    "Nova Flip": {
        "account_version": "demo-acct-2.9",
        "apps": ["演示中心", "折叠分屏", "相机实验室", "零售助手"],
        "wallpaper": "wallpaper-nova-sand.jpg",
        "settings": {"亮度": 65, "自动锁屏": "永不", "蓝牙": True, "定位": "使用期间", "音量": 35},
    },
}

# 展台：(code, 名称, x, y, w, h) —— 平面图严格按柜面位置排布
BOOTHS_S1 = [
    ("A1", "A 柜·左一", 0.5, 0.8, 1.6, 2.2),
    ("A2", "A 柜·左二", 2.3, 0.8, 1.6, 2.2),
    ("A3", "A 柜·左三", 4.1, 0.8, 1.6, 2.2),
    ("B1", "中岛·南侧", 1.2, 3.6, 1.8, 1.6),
    ("B2", "中岛·北侧", 3.4, 3.6, 1.8, 1.6),
    ("C1", "C 柜·体验位", 0.8, 5.8, 2.2, 1.5),
    ("C2", "C 柜·配件位", 3.3, 5.8, 2.2, 1.5),
]
BOOTHS_S2 = [
    ("D1", "旗舰柜·左", 0.6, 1.0, 1.8, 2.4),
    ("D2", "旗舰柜·右", 2.8, 1.0, 1.8, 2.4),
    ("E1", "中岛", 1.4, 4.0, 2.4, 1.8),
]

# (serial, model, color, booth_code, dock, cable, runtime_override)
DEVICES_S1 = [
    ("SN-AX12-0001", "Aurora X12", "曜石黑", "A1", "DK-A1-01", "CB-A1-01",
     {"battery": 92}),
    ("SN-AX12-0002", "Aurora X12", "云屿白", "A2", "DK-A2-01", "CB-A2-01",
     {"battery": 88}),
    # 离线样例：设备不在线
    ("SN-AX12P-001", "Aurora X12 Pro", "冰川蓝", "A3", "DK-A3-01", "CB-A3-01",
     {"online": False, "network_state": "offline", "battery": 54}),
    ("SN-NF-0007", "Nova Flip", "暮光紫", "B1", "DK-B1-01", "CB-B1-01",
     {"battery": 76}),
    # 错位样例：物理放在 B2，但系统登记在 C1（盘点时会发现 expected != actual）
    ("SN-NF-0008", "Nova Flip", "樱粉", "C1", "DK-C1-01", "CB-C1-01",
     {"battery": 81}),
    # 设置漂移样例：顾客试用后改了壁纸/设置并登出 → 进入待还原
    ("SN-AX12P-002", "Aurora X12 Pro", "曜石黑", "C2", "DK-C2-01", "CB-C2-01",
     {"account_version": "personal-account",
      "apps": ["演示中心", "微信", "抖音"],
      "wallpaper": "customer-photo.jpg",
      "settings": {"亮度": 100, "自动锁屏": "30秒", "蓝牙": False, "定位": "永不", "音量": 90},
      "logged_in": False, "content_flag": "顾客自拍与短视频",
      "appearance": "scratch", "battery": 47}),
]


def seed(db_path, data_dir, reset=False):
    if reset and os.path.exists(db_path):
        for suffix in ("", "-wal", "-shm"):
            p = db_path + suffix
            if os.path.exists(p):
                os.remove(p)
    db = DB(db_path)
    evidence_dir = os.path.join(data_dir, "evidence")
    package_dir = os.path.join(data_dir, "packages")
    svc = StoreService(db, evidence_dir, package_dir)

    if svc.list_stores():
        return svc, db  # 已有数据，幂等返回

    for name, role, pin in DEMO_USERS:
        db.execute("INSERT INTO users(name, role, pin) VALUES(?,?,?)", (name, role, pin))
    for model, p in PROFILES.items():
        db.execute(
            "INSERT INTO demo_profiles(model, account_version, apps, wallpaper, settings) "
            "VALUES(?,?,?,?,?)",
            (model, p["account_version"], json.dumps(p["apps"], ensure_ascii=False),
             p["wallpaper"], json.dumps(p["settings"], ensure_ascii=False)))
    db.commit()

    manager = db.one("SELECT * FROM users WHERE role='manager'")
    s1 = svc.create_store(manager, "万象城旗舰店", "09:00", "21:30")
    s2 = svc.create_store(manager, "滨江万达店", "10:00", "22:00")

    booth_map = {}
    for code, name, x, y, w, h in BOOTHS_S1:
        b = svc.create_booth(manager, s1["id"], code, name, x, y, w, h)
        booth_map[code] = b["id"]
    booth_map2 = {}
    for code, name, x, y, w, h in BOOTHS_S2:
        b = svc.create_booth(manager, s2["id"], code, name, x, y, w, h)
        booth_map2[code] = b["id"]

    for serial, model, color, bcode, dock, cable, rt in DEVICES_S1:
        svc.register_device(
            manager, serial, model, color, s1["id"], booth_map[bcode], dock, cable,
            runtime=rt)

    # 二号店两台正常机
    svc.register_device(
        manager, "SN-AX12-1001", "Aurora X12", "云屿白", s2["id"], booth_map2["D1"],
        "DK-D1-01", "CB-D1-01", runtime={"battery": 95})
    svc.register_device(
        manager, "SN-NF-1002", "Nova Flip", "冰川蓝", s2["id"], booth_map2["D2"],
        "DK-D2-01", "CB-D2-01", runtime={"battery": 83})

    # 为每个机型生成离线配置包
    for model in PROFILES:
        svc.build_offline_package(model)

    # 写一份外观证据样例（划伤设备）
    ev_path = os.path.join(evidence_dir, "sample-scratch-evidence.txt")
    os.makedirs(evidence_dir, exist_ok=True)
    with open(ev_path, "w", encoding="utf-8") as f:
        f.write("样例证据：SN-AX12P-002 右侧边框 3mm 划伤，开店盘点拍照留档。\n")

    # ---- 预置业务样例 ----
    staff = db.one("SELECT * FROM users WHERE role='staff'")
    # 1) 设置漂移设备直接进入待还原队列
    svc.enqueue_restore(manager, "SN-AX12P-002", "顾客试用后账号登出、壁纸与设置被更改")

    # 2) 防盗线报警异常（导购上报）
    svc.report_exception(staff, "SN-AX12-0001", "cable_alarm",
                         "14:22 防盗底座短暂报警，现场复位，外观无异常")
    dock = db.one("SELECT * FROM docks WHERE code='DK-A1-01'")
    db.execute("UPDATE docks SET alarm=0 WHERE code='DK-A1-01'")
    db.commit()

    # 3) 调店样例：一号店 SN-NF-0007 → 二号店 E1（店长已批准，等待接收；原展位 B1 冻结）
    t = svc.request_transfer(staff, "SN-NF-0007", s2["id"], booth_map2["E1"],
                             note="滨江店新品体验周支援")
    svc.approve_transfer(manager, t["id"])

    return svc, db


def main():
    parser = argparse.ArgumentParser(description="生成演示数据（含离线、错位、调店样例）")
    parser.add_argument("--reset", action="store_true", help="清空后重建")
    parser.add_argument("--db", default=os.path.join("data", "demo.db"))
    parser.add_argument("--data-dir", default="data")
    args = parser.parse_args()
    svc, db = seed(args.db, args.data_dir, reset=args.reset)
    print(f"已生成演示数据：{args.db}")
    for s in svc.list_stores():
        print(f"  门店 {s['id']} - {s['name']}，展台 {len(svc.list_booths(s['id']))} 个")
    print(f"  设备 {len(svc.list_devices())} 台，还原任务 {len(svc.list_restore_jobs())} 个，"
          f"调店单 {len(svc.list_transfers())} 张")
    print("  登录 PIN：导购 1001 / 店长 2002 / 技术 3003")


if __name__ == "__main__":
    main()
