"""业务服务层：盘点、异常、还原、调店、退仓的全部规则与权限。

角色：sales 导购（只能报异常）/ manager 店长（批准调店、改盘点结论）/
      technician 技术员（执行还原，不能改盘点结论）。
并发：devices / inventory_items / transfers 均带 version 乐观锁。
"""
import base64
import json
import os
import re
import sqlite3
from datetime import datetime

DEVICE_STATUS = {
    "displayable": "可展示",
    "pending_restore": "待还原",
    "in_transit": "调店在途",
    "returned": "已退仓",
}
ANOMALY_TYPES = {
    "offline": "设备离线",
    "anti_theft_alarm": "防盗线报警",
    "scratch": "外观划伤",
    "account_logout": "账号登出",
    "restore_failed": "还原失败",
    "misplaced": "展位错放",
    "missing_at_close": "闭店少机",
    "transfer_rejected": "调店拒收",
}
ROLES = {"sales": "导购", "manager": "店长", "technician": "技术员"}
# 盘点中参与流转的设备状态
COUNTABLE = ("displayable", "pending_restore")


def now():
    return datetime.now().isoformat(timespec="seconds")


class ServiceError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def bad_request(msg):
    return ServiceError(400, "bad_request", msg)


def forbidden(msg="当前角色无权执行该操作"):
    return ServiceError(403, "forbidden", msg)


def not_found(msg="记录不存在"):
    return ServiceError(404, "not_found", msg)


def conflict(msg):
    return ServiceError(409, "conflict", msg)


class Service:
    def __init__(self, db, mdm, dock, data_dir):
        self.db = db
        self.mdm = mdm
        self.dock = dock
        self.data_dir = data_dir
        self.evidence_dir = os.path.join(data_dir, "evidence")
        self.package_dir = os.path.join(data_dir, "packages")
        os.makedirs(self.evidence_dir, exist_ok=True)
        os.makedirs(self.package_dir, exist_ok=True)

    # ---------------------------------------------------------------- 基础
    def audit(self, actor, action, entity, entity_id, detail=""):
        self.db.execute(
            "INSERT INTO audit_log(actor,action,entity,entity_id,detail,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (actor["name"] if isinstance(actor, dict) else str(actor),
             action, entity, entity_id, detail, now()))

    def notify(self, kind, level, message):
        self.db.execute(
            "INSERT INTO notifications(kind,level,message,created_at) VALUES(?,?,?,?)",
            (kind, level, message, now()))

    def get_user(self, user_id):
        u = self.db.one("SELECT * FROM users WHERE id=?", (user_id,))
        if not u:
            raise ServiceError(401, "unauthorized", "未知用户，请重新选择身份")
        return u

    def get_device(self, device_id):
        d = self.db.one("SELECT * FROM devices WHERE id=?", (device_id,))
        if not d:
            raise not_found("设备不存在")
        return d

    def get_device_by_serial(self, serial):
        return self.db.one("SELECT * FROM devices WHERE serial=?", (serial,))

    def _require_role(self, actor, *roles):
        if actor["role"] not in roles:
            raise forbidden(f"需要{'或'.join(ROLES[r] for r in roles)}角色")

    def _booth_occupant(self, booth_id):
        return self.db.one(
            "SELECT * FROM devices WHERE booth_id=? AND status IN ('displayable','pending_restore','in_transit')",
            (booth_id,))

    # ------------------------------------------------------------ 设备登记
    def register_device(self, actor, data):
        self._require_role(actor, "manager")
        serial = (data.get("serial") or "").strip()
        if not serial:
            raise bad_request("序列号不能为空")
        if self.get_device_by_serial(serial):
            raise conflict(f"序列号 {serial} 已登记，同一序列号不能重复入库")
        booth_id = data.get("booth_id")
        if booth_id:
            booth = self.db.one("SELECT * FROM booths WHERE id=?", (booth_id,))
            if not booth:
                raise not_found("展台不存在")
            occ = self._booth_occupant(booth_id)
            if occ:
                raise conflict(f"展台 {booth['code']} 已放置设备 {occ['serial']}，同一序列号不能同时属于两个展位")
        pkg = self.latest_package(data.get("model", ""))
        account_version = pkg["account_version"] if pkg else "3.2.0"
        try:
            cur = self.db.execute(
                "INSERT INTO devices(serial,model,color,store_id,booth_id,status,"
                "dock_serial,cable_serial,account_name,account_version,version,created_at,updated_at)"
                " VALUES(?,?,?,?,?,'displayable',?,?,?,?,1,?,?)",
                (serial, data.get("model"), data.get("color"), data.get("store_id"),
                 booth_id, data.get("dock_serial"), data.get("cable_serial"),
                 data.get("account_name") or f"demo-{serial[-4:].lower()}@sample.local",
                 account_version, now(), now()))
        except sqlite3.IntegrityError:
            raise conflict(f"序列号 {serial} 已登记")
        device_id = cur.lastrowid
        # 适配器同步登记
        booth = self.db.one("SELECT * FROM booths WHERE id=?", (booth_id,)) if booth_id else None
        apps = pkg["apps"] if pkg else {}
        self.mdm.enroll(serial, booth_code=booth["code"] if booth else None,
                        account_version=account_version, apps=apps,
                        wallpaper_id=pkg["wallpaper_id"] if pkg else "wp-default")
        self.dock.enroll(serial)
        self.audit(actor, "register", "device", device_id, f"登记设备 {serial}")
        return self.get_device(device_id)

    def relocate_device(self, actor, device_id, booth_id, version):
        """移位/上架：乐观锁防并发，展台占用校验。"""
        self._require_role(actor, "manager")
        device = self.get_device(device_id)
        if device["status"] not in COUNTABLE:
            raise conflict(f"设备当前状态为{DEVICE_STATUS[device['status']]}，不能移位")
        booth = self.db.one("SELECT * FROM booths WHERE id=?", (booth_id,))
        if not booth:
            raise not_found("展台不存在")
        if booth["status"] == "frozen":
            raise conflict(f"展台 {booth['code']} 已冻结（调店中）")
        occ = self._booth_occupant(booth_id)
        if occ and occ["id"] != device_id:
            raise conflict(f"展台 {booth['code']} 已被设备 {occ['serial']} 占用")
        cur = self.db.execute(
            "UPDATE devices SET booth_id=?, store_id=?, version=version+1, updated_at=?"
            " WHERE id=? AND version=?",
            (booth_id, booth["store_id"], now(), device_id, version))
        if cur.rowcount == 0:
            raise conflict("设备版本已变化（并发修改），请刷新后重试")
        self.mdm.tamper(device["serial"], booth_hint=booth["code"])
        self.audit(actor, "relocate", "device", device_id, f"移至展台 {booth['code']}")
        return self.get_device(device_id)

    # ------------------------------------------------------------ 配置包
    def latest_package(self, model):
        row = self.db.one(
            "SELECT * FROM config_packages WHERE model=? ORDER BY id DESC", (model,))
        if not row:
            return None
        return {
            "model": row["model"], "version": row["version"],
            "account_version": row["account_version"],
            "wallpaper_id": row["wallpaper_id"],
            "apps": json.loads(row["apps_json"]),
        }

    def save_package(self, model, version, account_version, wallpaper_id, apps):
        """写入离线配置包（本地目录 + 数据库索引）。"""
        pkg_dir = os.path.join(self.package_dir, model)
        os.makedirs(pkg_dir, exist_ok=True)
        path = os.path.join(pkg_dir, f"config-{version}.json")
        payload = {"model": model, "version": version,
                   "account_version": account_version,
                   "wallpaper_id": wallpaper_id, "apps": apps}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        self.db.execute(
            "INSERT OR REPLACE INTO config_packages(model,version,account_version,"
            "wallpaper_id,apps_json,path) VALUES(?,?,?,?,?,?)",
            (model, version, account_version, wallpaper_id,
             json.dumps(apps, ensure_ascii=False), path))
        return payload

    # ------------------------------------------------------------ 版本校验
    def verify_device(self, device_id):
        """期望配置（配置包） vs 实际配置（MDM 适配器），逐字段比对。"""
        d = self.get_device(device_id)
        pkg = self.latest_package(d["model"])
        if not pkg:
            raise not_found(f"机型 {d['model']} 缺少离线配置包")
        actual = self.mdm.fetch_config(d["serial"])
        fields = []

        def add(field, label, expected, actual):
            fields.append({"field": field, "label": label, "expected": expected,
                           "actual": actual, "ok": expected == actual})

        add("account_version", "演示账号版本", pkg["account_version"], actual["account_version"])
        add("wallpaper_id", "壁纸", pkg["wallpaper_id"], actual["wallpaper_id"])
        for app, ver in pkg["apps"].items():
            add(f"app:{app}", f"应用·{app}", ver, actual["apps"].get(app))
        for app, ver in actual["apps"].items():
            if app not in pkg["apps"]:
                add(f"app:{app}", f"应用·{app}", None, ver)  # 多出的应用
        ok = all(f["ok"] for f in fields) and actual.get("logged_in", True)
        if not actual.get("logged_in", True):
            fields.append({"field": "logged_in", "label": "账号登录态",
                           "expected": True, "actual": False, "ok": False})
        return {"device_id": device_id, "serial": d["serial"], "ok": ok,
                "expected": pkg, "actual": actual, "fields": fields}

    def config_drifted(self, device):
        try:
            return not self.verify_device(device["id"])["ok"]
        except (KeyError, ServiceError):
            return True

    # ------------------------------------------------------------ 盘点
    def start_inventory(self, actor, store_id, inv_type, biz_date=None):
        self._require_role(actor, "manager", "technician")
        if inv_type not in ("open", "close"):
            raise bad_request("盘点类型须为 open 或 close")
        biz_date = biz_date or datetime.now().date().isoformat()
        existing = self.db.one(
            "SELECT * FROM inventories WHERE store_id=? AND type=? AND biz_date=?"
            " AND status='in_progress'", (store_id, inv_type, biz_date))
        if existing:
            raise conflict("该门店今日同类型盘点已在进行中")
        cur = self.db.execute(
            "INSERT INTO inventories(store_id,type,biz_date,status,started_by,started_at)"
            " VALUES(?,?,?,'in_progress',?,?)",
            (store_id, inv_type, biz_date, actor["id"], now()))
        self.audit(actor, "start", "inventory", cur.lastrowid,
                   f"{'开店' if inv_type == 'open' else '闭店'}盘点")
        return self.get_inventory(cur.lastrowid)

    def get_inventory(self, inv_id):
        inv = self.db.one("SELECT * FROM inventories WHERE id=?", (inv_id,))
        if not inv:
            raise not_found("盘点单不存在")
        devices = self.db.query(
            "SELECT d.*, b.code AS booth_code FROM devices d"
            " LEFT JOIN booths b ON b.id=d.booth_id"
            " WHERE d.store_id=? AND d.status IN ('displayable','pending_restore')"
            " ORDER BY b.grid_y, b.grid_x", (inv["store_id"],))
        items = {i["device_id"]: i for i in self.db.query(
            "SELECT * FROM inventory_items WHERE inventory_id=?", (inv_id,))}
        dev_list = []
        for d in devices:
            st = self.mdm.fetch_status(d["serial"])
            dock = self.dock.status(d["serial"])
            dev_list.append({
                **d, "adapter": st, "dock": dock,
                "item": items.get(d["id"]),
                "position_match": (st["booth_hint"] == (d["booth_code"] or "")),
            })
        inv["devices"] = dev_list
        inv["done"] = sum(1 for d in dev_list if d["item"])
        inv["total"] = len(dev_list)
        return inv

    def submit_item(self, actor, inv_id, device_id, checks):
        """提交单台盘点结果。version 乐观锁防并发覆盖；
        结论由系统计算，仅店长可手工改结论。"""
        self._require_role(actor, "manager", "technician")
        inv = self.db.one("SELECT * FROM inventories WHERE id=?", (inv_id,))
        if not inv:
            raise not_found("盘点单不存在")
        if inv["status"] != "in_progress":
            raise conflict("盘点已结束，不能再提交")
        device = self.get_device(device_id)
        manual_conclusion = checks.get("conclusion")
        if manual_conclusion is not None and actor["role"] != "manager":
            raise forbidden("技术人员不能修改盘点结论，仅店长可改")

        def b(key):
            v = checks.get(key)
            return None if v is None else (1 if v else 0)

        if inv["type"] == "open":
            auto = "ok" if all([b("position_ok"), b("appearance_ok"), b("online")]) else "issue"
        else:
            auto = "ok" if all([b("serial_verified"), b("accessories_ok")]) else "issue"
        conclusion = manual_conclusion if (manual_conclusion and actor["role"] == "manager") else auto

        existing = self.db.one(
            "SELECT * FROM inventory_items WHERE inventory_id=? AND device_id=?",
            (inv_id, device_id))
        if existing:
            expect = checks.get("version")
            if expect is None or int(expect) != existing["version"]:
                raise conflict("该设备盘点记录已被他人更新（版本冲突），请刷新后重试")
            cur = self.db.execute(
                "UPDATE inventory_items SET position_ok=?,battery=?,appearance_ok=?,"
                "online=?,serial_verified=?,accessories_ok=?,conclusion=?,note=?,"
                "version=version+1,checked_by=?,checked_at=?"
                " WHERE id=? AND version=?",
                (b("position_ok"), checks.get("battery"), b("appearance_ok"),
                 b("online"), b("serial_verified"), b("accessories_ok"),
                 conclusion, checks.get("note"), actor["id"], now(),
                 existing["id"], existing["version"]))
            if cur.rowcount == 0:
                raise conflict("该设备盘点记录已被他人更新（版本冲突），请刷新后重试")
            item_id = existing["id"]
        else:
            cur = self.db.execute(
                "INSERT INTO inventory_items(inventory_id,device_id,position_ok,battery,"
                "appearance_ok,online,serial_verified,accessories_ok,conclusion,note,"
                "version,checked_by,checked_at) VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?)",
                (inv_id, device_id, b("position_ok"), checks.get("battery"),
                 b("appearance_ok"), b("online"), b("serial_verified"),
                 b("accessories_ok"), conclusion, checks.get("note"),
                 actor["id"], now()))
            item_id = cur.lastrowid
        return self.db.one("SELECT * FROM inventory_items WHERE id=?", (item_id,))

    def complete_inventory(self, actor, inv_id):
        """完成盘点：开店→配置漂移设备进待还原并生成还原任务；
        闭店→序列号未核对/配件缺失生成异常。"""
        self._require_role(actor, "manager")
        inv = self.db.one("SELECT * FROM inventories WHERE id=?", (inv_id,))
        if not inv:
            raise not_found("盘点单不存在")
        if inv["status"] != "in_progress":
            raise conflict("盘点已结束")
        devices = self.db.query(
            "SELECT * FROM devices WHERE store_id=? AND status IN ('displayable','pending_restore')",
            (inv["store_id"],))
        items = {i["device_id"]: i for i in self.db.query(
            "SELECT * FROM inventory_items WHERE inventory_id=?", (inv_id,))}
        missing = [d for d in devices if d["id"] not in items]
        if missing:
            raise bad_request(
                "还有设备未盘点：" + "、".join(d["serial"] for d in missing))

        results = {"to_pending_restore": [], "anomalies": []}
        for d in devices:
            item = items[d["id"]]
            if inv["type"] == "open":
                if item["conclusion"] == "issue" or self.config_drifted(d):
                    self._enter_pending_restore(d, actor)
                    results["to_pending_restore"].append(d["serial"])
            else:  # close
                if not item["serial_verified"]:
                    a = self._system_anomaly(
                        d, "missing_at_close",
                        f"闭店盘点序列号未核对到 {d['serial']}", actor)
                    results["anomalies"].append(a["id"])
                if not item["accessories_ok"]:
                    a = self._system_anomaly(
                        d, "anti_theft_alarm",
                        f"闭店盘点配件缺失（底座/防盗线）：{d['serial']}", actor)
                    results["anomalies"].append(a["id"])
        self.db.execute(
            "UPDATE inventories SET status='completed', completed_at=? WHERE id=?",
            (now(), inv_id))
        self.audit(actor, "complete", "inventory", inv_id,
                   f"完成{inv['type']}盘点：{len(results['to_pending_restore'])}台待还原，"
                   f"{len(results['anomalies'])}条异常")
        return {"inventory_id": inv_id, **results}

    # ------------------------------------------------------------ 异常
    def _system_anomaly(self, device, type_, detail, actor=None):
        cur = self.db.execute(
            "INSERT INTO anomalies(device_id,store_id,type,status,title,detail,"
            "reported_by,created_at) VALUES(?,?,?,'open',?,?,?,?)",
            (device["id"], device["store_id"], type_,
             ANOMALY_TYPES[type_], detail,
             (actor or {}).get("id"), now()))
        return self.db.one("SELECT * FROM anomalies WHERE id=?", (cur.lastrowid,))

    def report_anomaly(self, actor, device_id, type_, detail=""):
        """任何角色可报异常（导购的主要入口）。"""
        if type_ not in ANOMALY_TYPES:
            raise bad_request(f"未知异常类型 {type_}")
        device = self.get_device(device_id)
        a = self._system_anomaly(device, type_, detail or ANOMALY_TYPES[type_], actor)
        # 账号登出：设备进入待还原，需重新校验账号/应用/壁纸
        if type_ == "account_logout":
            self.mdm.tamper(device["serial"], logged_in=False)
            self._enter_pending_restore(device, actor)
        if type_ == "anti_theft_alarm":
            self.dock.set(device["serial"], alarm=True, connected=False)
        self.audit(actor, "report", "anomaly", a["id"],
                   f"{ANOMALY_TYPES[type_]}：{device['serial']}")
        return a

    def add_evidence(self, actor, anomaly_id, filename, data_b64):
        a = self.db.one("SELECT * FROM anomalies WHERE id=?", (anomaly_id,))
        if not a:
            raise not_found("异常不存在")
        safe = re.sub(r"[^\w.\-]", "_", filename or "evidence.bin")
        folder = os.path.join(self.evidence_dir, str(anomaly_id))
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, safe)
        with open(path, "wb") as f:
            f.write(base64.b64decode(data_b64))
        cur = self.db.execute(
            "INSERT INTO evidence(anomaly_id,filename,path,uploaded_by,created_at)"
            " VALUES(?,?,?,?,?)",
            (anomaly_id, safe, path, actor["id"], now()))
        return self.db.one("SELECT * FROM evidence WHERE id=?", (cur.lastrowid,))

    def list_evidence(self, anomaly_id):
        return self.db.query(
            "SELECT id,anomaly_id,filename,uploaded_by,created_at FROM evidence"
            " WHERE anomaly_id=?", (anomaly_id,))

    def delete_evidence(self, actor, evidence_id):
        """证据保留策略：异常关闭前禁止删除；关闭后同样留档不可删。"""
        ev = self.db.one("SELECT * FROM evidence WHERE id=?", (evidence_id,))
        if not ev:
            raise not_found("证据不存在")
        raise conflict("外观证据按留档策略不可删除")

    def resolve_anomaly(self, actor, anomaly_id, resolution, payload=None):
        """八类异常分别处理。"""
        self._require_role(actor, "manager", "technician")
        payload = payload or {}
        a = self.db.one("SELECT * FROM anomalies WHERE id=?", (anomaly_id,))
        if not a:
            raise not_found("异常不存在")
        if a["status"] == "resolved":
            raise conflict("异常已处理完毕")
        device = self.get_device(a["device_id"]) if a["device_id"] else None
        t = a["type"]

        if t == "scratch":
            ev = self.list_evidence(anomaly_id)
            if not ev:
                raise bad_request("外观划伤须先上传外观证据照片才能办结")
        elif t == "misplaced":
            target = payload.get("target_booth_id")
            if not target:
                raise bad_request("展位错放须指定纠正后的目标展台")
            self.relocate_device(
                {"role": "manager", "id": actor["id"], "name": actor["name"]},
                device["id"], int(target), device["version"])
        elif t == "offline":
            if not self.mdm.fetch_status(device["serial"])["online"]:
                raise bad_request("设备仍离线，请先恢复供电/网络后再办结")
        elif t == "anti_theft_alarm":
            dock = self.dock.status(device["serial"])
            if dock["alarm"] or not dock["connected"]:
                if not payload.get("cable_reconnected"):
                    raise bad_request("防盗线未恢复连接，请现场确认后再办结")
                self.dock.set(device["serial"], alarm=False, connected=True)
        elif t == "account_logout":
            if device["status"] != "displayable":
                raise bad_request("账号尚未恢复：需完成还原并校验通过后才能办结")
        elif t == "restore_failed":
            task = self.db.one(
                "SELECT * FROM restore_tasks WHERE device_id=? ORDER BY id DESC",
                (device["id"],))
            if task and task["status"] != "success":
                raise bad_request("还原任务尚未成功，不能办结还原失败异常")
        elif t == "missing_at_close":
            self._require_role(actor, "manager")
            if not payload.get("found"):
                raise bad_request("闭店少机须店长确认找回（found=true）后才能办结")
        elif t == "transfer_rejected":
            pass  # 调店拒收：退回流程已在 reject_transfer 完成，此处仅归档

        self.db.execute(
            "UPDATE anomalies SET status='resolved', resolved_by=?, resolved_at=?,"
            " resolution=? WHERE id=?",
            (actor["id"], now(), resolution or "已处理", anomaly_id))
        self.audit(actor, "resolve", "anomaly", anomaly_id,
                   f"{ANOMALY_TYPES[t]}：{resolution or '已处理'}")
        return self.db.one("SELECT * FROM anomalies WHERE id=?", (anomaly_id,))

    # ------------------------------------------------------------ 还原
    def _enter_pending_restore(self, device, actor=None):
        self.db.execute(
            "UPDATE devices SET status='pending_restore', version=version+1,"
            " updated_at=? WHERE id=?", (now(), device["id"]))
        self.create_restore_task(device["id"], actor)

    def create_restore_task(self, device_id, actor=None):
        d = self.get_device(device_id)
        if d["status"] != "pending_restore":
            raise conflict("还原前置不满足：设备不在待还原状态")
        dup = self.db.one(
            "SELECT * FROM restore_tasks WHERE device_id=? AND status='pending'",
            (device_id,))
        if dup:
            return dup
        pkg = self.latest_package(d["model"])
        if not pkg:
            raise not_found(f"机型 {d['model']} 缺少离线配置包，无法还原")
        actual = self.mdm.fetch_config(d["serial"])
        diff = self.verify_device(device_id)["fields"]
        cur = self.db.execute(
            "INSERT INTO restore_tasks(device_id,status,expected_json,actual_json,"
            "diff_json,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
            (device_id, "pending", json.dumps(pkg, ensure_ascii=False),
             json.dumps(actual, ensure_ascii=False),
             json.dumps(diff, ensure_ascii=False),
             (actor or {}).get("id"), now()))
        return self.db.one("SELECT * FROM restore_tasks WHERE id=?", (cur.lastrowid,))

    def execute_restore(self, actor, task_id, _system=False):
        """执行还原：仅技术员（或系统重试）。刷机→校验→通过才回可展示。"""
        if not _system:
            self._require_role(actor, "technician")
        task = self.db.one("SELECT * FROM restore_tasks WHERE id=?", (task_id,))
        if not task:
            raise not_found("还原任务不存在")
        if task["status"] == "success":
            raise conflict("该还原任务已成功，无需重复执行")
        device = self.get_device(task["device_id"])
        if device["status"] != "pending_restore":
            raise conflict("还原前置不满足：设备不在待还原状态")
        pkg = json.loads(task["expected_json"])
        ok = self.mdm.apply_package(device["serial"], pkg)
        if not ok:
            retry = task["retry_count"] + 1
            self.db.execute(
                "UPDATE restore_tasks SET status='failed', retry_count=?,"
                " last_error=?, executed_by=?, executed_at=? WHERE id=?",
                (retry, "刷机失败：设备未确认写入（适配器返回失败）",
                 actor.get("id"), now(), task_id))
            self._system_anomaly(device, "restore_failed",
                                 f"{device['serial']} 第{retry}次还原失败", actor)
            self.notify("restore", "warn",
                        f"设备 {device['serial']} 还原失败（第{retry}次），将进入自动重试")
            return self.db.one("SELECT * FROM restore_tasks WHERE id=?", (task_id,))
        # 刷机成功 → 校验演示账号/应用/壁纸
        verify = self.verify_device(device["id"])
        if verify["ok"]:
            self.db.execute(
                "UPDATE restore_tasks SET status='success', actual_json=?, diff_json=?,"
                " executed_by=?, executed_at=? WHERE id=?",
                (json.dumps(verify["actual"], ensure_ascii=False),
                 json.dumps(verify["fields"], ensure_ascii=False),
                 actor.get("id"), now(), task_id))
            self.db.execute(
                "UPDATE devices SET status='displayable', account_version=?,"
                " version=version+1, updated_at=? WHERE id=?",
                (pkg["account_version"], now(), device["id"]))
            self.notify("restore", "info",
                        f"设备 {device['serial']} 还原校验通过，恢复可展示")
        else:
            retry = task["retry_count"] + 1
            self.db.execute(
                "UPDATE restore_tasks SET status='failed', retry_count=?,"
                " actual_json=?, diff_json=?, last_error=?, executed_by=?,"
                " executed_at=? WHERE id=?",
                (retry, json.dumps(verify["actual"], ensure_ascii=False),
                 json.dumps(verify["fields"], ensure_ascii=False),
                 "刷入后校验未通过：演示账号/应用/壁纸存在不一致",
                 actor.get("id"), now(), task_id))
            self._system_anomaly(device, "restore_failed",
                                 f"{device['serial']} 还原后校验未通过", actor)
        self.audit(actor, "execute", "restore_task", task_id,
                   f"结果：{'成功' if verify['ok'] else '失败'}")
        return self.db.one("SELECT * FROM restore_tasks WHERE id=?", (task_id,))

    def retry_failed_restores(self):
        """进程内任务：自动重试失败的还原（不超过 max_retry）。"""
        tasks = self.db.query(
            "SELECT * FROM restore_tasks WHERE status='failed' AND retry_count < max_retry")
        done = []
        for t in tasks:
            device = self.get_device(t["device_id"])
            if device["status"] != "pending_restore":
                continue
            r = self.execute_restore({"id": None, "name": "系统重试", "role": "technician"},
                                     t["id"], _system=True)
            done.append({"task_id": t["id"], "status": r["status"]})
        return done

    # ------------------------------------------------------------ 调店
    def create_transfer(self, actor, device_id, to_store_id, to_booth_id):
        self._require_role(actor, "manager")
        device = self.get_device(device_id)
        if device["status"] != "displayable":
            raise conflict(f"仅可展示设备可发起调店，当前：{DEVICE_STATUS[device['status']]}")
        dup = self.db.one(
            "SELECT * FROM transfers WHERE device_id=? AND status IN ('requested','in_transit')",
            (device_id,))
        if dup:
            raise conflict("该设备已有进行中的调店单")
        booth = self.db.one("SELECT * FROM booths WHERE id=?", (to_booth_id,))
        if not booth or booth["store_id"] != to_store_id:
            raise bad_request("目标展台与目标门店不符")
        if booth["status"] != "active":
            raise conflict("目标展台已冻结")
        if self._booth_occupant(to_booth_id):
            raise conflict("目标展台已被占用")
        cur = self.db.execute(
            "INSERT INTO transfers(device_id,from_store_id,from_booth_id,to_store_id,"
            "to_booth_id,status,requested_by,requested_at,version)"
            " VALUES(?,?,?,?,?,'requested',?,?,1)",
            (device_id, device["store_id"], device["booth_id"],
             to_store_id, to_booth_id, actor["id"], now()))
        self.audit(actor, "create", "transfer", cur.lastrowid,
                   f"申请调店 {device['serial']}")
        return self.db.one("SELECT * FROM transfers WHERE id=?", (cur.lastrowid,))

    def _transfer(self, tid):
        t = self.db.one("SELECT * FROM transfers WHERE id=?", (tid,))
        if not t:
            raise not_found("调店单不存在")
        return t

    def approve_transfer(self, actor, tid):
        """店长批准：冻结原展位、设备转在途、生成交接记录。"""
        self._require_role(actor, "manager")
        t = self._transfer(tid)
        if t["status"] != "requested":
            raise conflict("调店单状态不允许批准")
        device = self.get_device(t["device_id"])
        handover = {
            "serial": device["serial"], "model": device["model"],
            "color": device["color"],
            "from_store": t["from_store_id"], "to_store": t["to_store_id"],
            "dock_serial": device["dock_serial"],
            "cable_serial": device["cable_serial"],
            "account_name": device["account_name"],
            "account_version": device["account_version"],
            "device_version": device["version"],
            "approved_by": actor["name"], "approved_at": now(),
        }
        if t["from_booth_id"]:
            self.db.execute("UPDATE booths SET status='frozen' WHERE id=?",
                            (t["from_booth_id"],))
        self.db.execute(
            "UPDATE devices SET status='in_transit', version=version+1, updated_at=?"
            " WHERE id=?", (now(), device["id"]))
        self.db.execute(
            "UPDATE transfers SET status='in_transit', handover_json=?,"
            " approved_by=?, approved_at=?, version=version+1 WHERE id=? AND status='requested'",
            (json.dumps(handover, ensure_ascii=False), actor["id"], now(), tid))
        self.audit(actor, "approve", "transfer", tid,
                   f"批准调店 {device['serial']}，原展位已冻结")
        return self._transfer(tid)

    def receive_transfer(self, actor, tid):
        self._require_role(actor, "manager")
        t = self._transfer(tid)
        if t["status"] != "in_transit":
            raise conflict("调店单不在在途状态")
        device = self.get_device(t["device_id"])
        self.db.execute(
            "UPDATE devices SET store_id=?, booth_id=?, status='displayable',"
            " version=version+1, updated_at=? WHERE id=?",
            (t["to_store_id"], t["to_booth_id"], now(), device["id"]))
        if t["from_booth_id"]:
            self.db.execute("UPDATE booths SET status='active' WHERE id=?",
                            (t["from_booth_id"],))
        booth = self.db.one("SELECT * FROM booths WHERE id=?", (t["to_booth_id"],))
        self.mdm.tamper(device["serial"], booth_hint=booth["code"] if booth else None)
        self.db.execute(
            "UPDATE transfers SET status='received', received_by=?, received_at=?,"
            " version=version+1 WHERE id=?", (actor["id"], now(), tid))
        self.audit(actor, "receive", "transfer", tid,
                   f"{device['serial']} 已收货上架")
        return self._transfer(tid)

    def reject_transfer(self, actor, tid, reason):
        """拒收：设备退回原店原展位，解冻，并生成调店拒收异常。"""
        self._require_role(actor, "manager")
        t = self._transfer(tid)
        if t["status"] != "in_transit":
            raise conflict("调店单不在在途状态")
        device = self.get_device(t["device_id"])
        self.db.execute(
            "UPDATE devices SET store_id=?, booth_id=?, status='displayable',"
            " version=version+1, updated_at=? WHERE id=?",
            (t["from_store_id"], t["from_booth_id"], now(), device["id"]))
        if t["from_booth_id"]:
            self.db.execute("UPDATE booths SET status='active' WHERE id=?",
                            (t["from_booth_id"],))
        booth = self.db.one("SELECT * FROM booths WHERE id=?", (t["from_booth_id"],))
        self.mdm.tamper(device["serial"], booth_hint=booth["code"] if booth else None)
        self.db.execute(
            "UPDATE transfers SET status='rejected', rejected_reason=?,"
            " version=version+1 WHERE id=?", (reason or "目标门店拒收", tid))
        self._system_anomaly(device, "transfer_rejected",
                             f"调店被拒收退回：{reason or '目标门店拒收'}", actor)
        self.audit(actor, "reject", "transfer", tid, reason or "")
        return self._transfer(tid)

    # ------------------------------------------------------------ 退仓
    def create_return(self, actor, device_id, reason):
        self._require_role(actor, "manager")
        device = self.get_device(device_id)
        if device["status"] not in COUNTABLE:
            raise conflict(f"当前状态{DEVICE_STATUS[device['status']]}不可退仓")
        cur = self.db.execute(
            "INSERT INTO warehouse_returns(device_id,store_id,reason,status,"
            "requested_by,created_at) VALUES(?,?,?,'requested',?,?)",
            (device_id, device["store_id"], reason or "", actor["id"], now()))
        self.audit(actor, "create", "return", cur.lastrowid,
                   f"申请退仓 {device['serial']}")
        return self.db.one("SELECT * FROM warehouse_returns WHERE id=?", (cur.lastrowid,))

    def review_return(self, actor, return_id, checklist, approve):
        """退仓复核：配件齐全 / 证据留存 / 账号已清，三项齐备方可通过。"""
        self._require_role(actor, "manager")
        r = self.db.one("SELECT * FROM warehouse_returns WHERE id=?", (return_id,))
        if not r:
            raise not_found("退仓单不存在")
        if r["status"] != "requested":
            raise conflict("退仓单已复核")
        required = ("accessories_complete", "evidence_retained", "account_wiped")
        if approve and not all(checklist.get(k) for k in required):
            raise bad_request("复核未通过：配件齐全、证据留存、账号已清三项必须全部确认")
        device = self.get_device(r["device_id"])
        if approve:
            self.db.execute(
                "UPDATE devices SET status='returned', booth_id=NULL,"
                " version=version+1, updated_at=? WHERE id=?", (now(), device["id"]))
            self.mdm.tamper(device["serial"], booth_hint=None, present=False)
        self.db.execute(
            "UPDATE warehouse_returns SET status=?, checklist_json=?, reviewed_by=?,"
            " reviewed_at=? WHERE id=?",
            ("completed" if approve else "rejected",
             json.dumps(checklist, ensure_ascii=False), actor["id"], now(), return_id))
        self.audit(actor, "review", "return", return_id,
                   "复核通过退仓" if approve else "复核驳回")
        return self.db.one("SELECT * FROM warehouse_returns WHERE id=?", (return_id,))

    # ------------------------------------------------------------ 查询
    def plan(self, store_id):
        booths = self.db.query(
            "SELECT * FROM booths WHERE store_id=? ORDER BY grid_y, grid_x", (store_id,))
        devices = self.db.query(
            "SELECT * FROM devices WHERE store_id=? AND status != 'returned'", (store_id,))
        by_booth = {}
        for d in devices:
            by_booth.setdefault(d["booth_id"], []).append(d)
        out = []
        for b in booths:
            devs = by_booth.get(b["id"], [])
            cell = {**b, "devices": []}
            for d in devs:
                st = self.mdm.fetch_status(d["serial"])
                cell["devices"].append({**d, "adapter": st,
                                        "dock": self.dock.status(d["serial"])})
            out.append(cell)
        stray = [d for d in devices if not d["booth_id"]]
        return {"booths": out, "unplaced": stray}

    def device_detail(self, device_id):
        d = self.get_device(device_id)
        booth = self.db.one("SELECT * FROM booths WHERE id=?", (d["booth_id"],)) if d["booth_id"] else None
        anomalies = self.db.query(
            "SELECT * FROM anomalies WHERE device_id=? AND status != 'resolved'"
            " ORDER BY id DESC", (device_id,))
        verify = None
        try:
            verify = self.verify_device(device_id)
        except ServiceError:
            pass
        return {**d, "booth": booth, "adapter": self.mdm.fetch_status(d["serial"]),
                "dock": self.dock.status(d["serial"]),
                "open_anomalies": anomalies, "verify": verify}

    def list_anomalies(self, store_id=None, status=None):
        sql = ("SELECT a.*, d.serial, d.model, u.name AS reporter FROM anomalies a"
               " LEFT JOIN devices d ON d.id=a.device_id"
               " LEFT JOIN users u ON u.id=a.reported_by")
        cond, params = [], []
        if store_id:
            cond.append("a.store_id=?"); params.append(store_id)
        if status:
            cond.append("a.status=?"); params.append(status)
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY a.id DESC"
        rows = self.db.query(sql, params)
        for r in rows:
            r["evidence"] = self.list_evidence(r["id"])
        return rows

    def restore_queue(self):
        rows = self.db.query(
            "SELECT t.*, d.serial, d.model, d.color FROM restore_tasks t"
            " JOIN devices d ON d.id=t.device_id ORDER BY t.id DESC")
        for r in rows:
            r["expected"] = json.loads(r["expected_json"] or "null")
            r["actual"] = json.loads(r["actual_json"] or "null")
            r["diff"] = json.loads(r["diff_json"] or "null")
        return rows

    def list_transfers(self):
        rows = self.db.query("SELECT * FROM transfers ORDER BY id DESC")
        for r in rows:
            r["handover"] = json.loads(r["handover_json"] or "null")
            d = self.get_device(r["device_id"])
            r["serial"], r["model"] = d["serial"], d["model"]
        return rows

    def list_returns(self):
        rows = self.db.query(
            "SELECT r.*, d.serial, d.model FROM warehouse_returns r"
            " JOIN devices d ON d.id=r.device_id ORDER BY r.id DESC")
        for r in rows:
            r["checklist"] = json.loads(r["checklist_json"] or "null")
        return rows

    def notifications(self, limit=50):
        return self.db.query(
            "SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,))
