"""种子数据：门店/展台/用户/设备/配置包，以及离线、错位、调店样例。

所有设备序列号与演示账号均为样例。
"""
import json
import os
from datetime import datetime

from .services import now

APPS_DEMO = {"演示相册": "1.4.0", "体验相机": "2.1.0", "门店导购": "3.0.2"}
MODELS = [("星耀X1", "曜石黑"), ("星耀X1 Pro", "珍珠白"), ("云舟M5", "蔚蓝")]


def seed_if_empty(svc):
    if svc.db.one("SELECT id FROM stores LIMIT 1"):
        return False
    db = svc.db

    # 门店
    db.execute("INSERT INTO stores(code,name) VALUES('S001','星城旗舰店')")
    db.execute("INSERT INTO stores(code,name) VALUES('S002','城西体验店')")
    s1 = db.one("SELECT id FROM stores WHERE code='S001'")["id"]
    s2 = db.one("SELECT id FROM stores WHERE code='S002'")["id"]

    # 展台（严格柜面坐标）
    booths = {}
    layout1 = [("A1", "旗舰体验台-1", 1, 1), ("A2", "旗舰体验台-2", 2, 1),
               ("A3", "旗舰体验台-3", 3, 1), ("B1", "影像专区-1", 1, 2),
               ("B2", "影像专区-2", 2, 2), ("B3", "影像专区-3", 3, 2)]
    for code, name, x, y in layout1:
        cur = db.execute(
            "INSERT INTO booths(store_id,code,name,grid_x,grid_y) VALUES(?,?,?,?,?)",
            (s1, code, name, x, y))
        booths[code] = cur.lastrowid
    layout2 = [("C1", "体验台-1", 1, 1), ("C2", "体验台-2", 2, 1),
               ("C3", "体验台-3", 3, 1)]
    for code, name, x, y in layout2:
        cur = db.execute(
            "INSERT INTO booths(store_id,code,name,grid_x,grid_y) VALUES(?,?,?,?,?)",
            (s2, code, name, x, y))
        booths[code] = cur.lastrowid

    # 用户（样例）
    users = {}
    for name, role, store in [
            ("导购小周", "sales", s1), ("店长林岚", "manager", s1),
            ("技术老白", "technician", s1), ("店长高远", "manager", s2),
            ("导购阿黎", "sales", s2)]:
        cur = db.execute(
            "INSERT INTO users(name,role,store_id) VALUES(?,?,?)", (name, role, store))
        users[name] = cur.lastrowid
    manager1 = db.one("SELECT * FROM users WHERE id=?", (users["店长林岚"],))

    # 离线配置包（本地目录 data/packages/）
    for model, _ in MODELS:
        svc.save_package(model, "2026.09", "3.2.0", "wp-aurora-09", dict(APPS_DEMO))

    # 设备（样例序列号）
    devices = [
        # serial, model_idx, booth, store
        ("DEMO-X1-0001", 0, "A1", s1), ("DEMO-X1-0002", 0, "A2", s1),
        ("DEMO-X1P-0003", 1, "A3", s1), ("DEMO-X1P-0004", 1, "B1", s1),
        ("DEMO-M5-0005", 2, "B2", s1), ("DEMO-M5-0006", 2, "B3", s1),
        ("DEMO-X1-0007", 0, "C1", s2), ("DEMO-X1P-0008", 1, "C2", s2),
    ]
    ids = {}
    for serial, mi, booth_code, store_id in devices:
        model, color = MODELS[mi]
        d = svc.register_device(manager1, {
            "serial": serial, "model": model, "color": color,
            "store_id": store_id, "booth_id": booths[booth_code],
            "dock_serial": f"DOCK-{serial[-4:]}",
            "cable_serial": f"CABLE-{serial[-4:]}",
            "account_name": f"demo-{serial[-4:].lower()}@sample.local",
        })
        ids[serial] = d["id"]

    # ---- 样例 1：设备离线（A2 的 DEMO-X1-0002）----
    svc.mdm.tamper("DEMO-X1-0002", online=False, battery=12)
    svc.report_anomaly(
        db.one("SELECT * FROM users WHERE id=?", (users["导购小周"],)),
        ids["DEMO-X1-0002"], "offline", "开店前巡检发现设备离线，电量低")

    # ---- 样例 2：展位错放（DEMO-M5-0005 登记在 B2，实际被放到 B3 台面）----
    svc.mdm.tamper("DEMO-M5-0005", booth_hint="B3")
    svc.report_anomaly(
        db.one("SELECT * FROM users WHERE id=?", (users["导购小周"],)),
        ids["DEMO-M5-0005"], "misplaced",
        "设备实际在 B3 台面，系统登记在 B2")

    # ---- 样例 3：配置漂移 → 待还原（DEMO-X1P-0003 被顾客改动）----
    svc.mdm.tamper("DEMO-X1P-0003", account_version="2.8.1",
                   wallpaper_id="wp-customer-photo",
                   apps={**APPS_DEMO, "第三方游戏": "9.9.9"})
    svc.report_anomaly(
        db.one("SELECT * FROM users WHERE id=?", (users["导购小周"],)),
        ids["DEMO-X1P-0003"], "account_logout", "顾客试用后演示账号被登出并改动设置")

    # ---- 样例 4：外观划伤 + 证据文件（DEMO-X1P-0004）----
    a = svc.report_anomaly(
        db.one("SELECT * FROM users WHERE id=?", (users["导购小周"],)),
        ids["DEMO-X1P-0004"], "scratch", "边框右侧约 2cm 划痕")
    # 生成一个占位“照片”证据（文本内容模拟）
    import base64
    svc.add_evidence(
        db.one("SELECT * FROM users WHERE id=?", (users["导购小周"],)),
        a["id"], "scratch-photo.txt",
        base64.b64encode("样例证据：边框右侧 2cm 划痕照片".encode()).decode())

    # ---- 样例 5：调店在途（DEMO-M5-0006：S001 → S002/C3，已批准）----
    t = svc.create_transfer(manager1, ids["DEMO-M5-0006"], s2, booths["C3"])
    svc.approve_transfer(manager1, t["id"])

    # ---- 样例 6： flaky 设备（DEMO-X1-0007 首次刷机必失败 → 演示自动重试）----
    svc.mdm.tamper("DEMO-X1-0007", fail_flashes=1, account_version="3.0.0")
    svc._enter_pending_restore(svc.get_device(ids["DEMO-X1-0007"]), manager1)

    db.execute(
        "INSERT INTO notifications(kind,level,message,created_at) VALUES(?,?,?,?)",
        ("seed", "info", "样例数据已生成：含离线、错位、调店在途、待还原设备", now()))
    return True
