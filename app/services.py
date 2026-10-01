"""领域服务：门店/展台/设备、开闭店盘点、异常、还原队列、调店、退仓。

角色：
  staff   导购：执行盘点、登记异常；不能批准调店、不能执行还原、不能改还原结论
  manager 店长：批准/接收调店、退仓复核、处理异常、登记设备
  tech    技术人员：执行系统还原；不能修改盘点结论
"""
import json
import os
import shutil

from .db import DB, now, today
from .errors import Conflict, NotFound, PermissionDenied, PreconditionFailed, ValidationError

ROLE_STAFF, ROLE_MANAGER, ROLE_TECH = "staff", "manager", "tech"


def _require(user, *roles):
    if user is None:
        raise PermissionDenied("请先登录")
    if user["role"] not in roles:
        names = {"staff": "导购", "manager": "店长", "tech": "技术人员"}
        raise PermissionDenied(f"需要 {'/'.join(names[r] for r in roles)} 权限，当前为{names[user['role']]}")


class StoreService:
    def __init__(self, db: DB, evidence_dir, package_dir):
        self.db = db
        self.evidence_dir = evidence_dir
        self.package_dir = package_dir
        os.makedirs(evidence_dir, exist_ok=True)
        os.makedirs(package_dir, exist_ok=True)

    # ---------- 基础资料 ----------
    def create_store(self, user, name, open_time="09:00", close_time="21:30"):
        _require(user, ROLE_MANAGER)
        cur = self.db.execute(
            "INSERT INTO stores(name, open_time, close_time, created_at) VALUES(?,?,?,?)",
            (name, open_time, close_time, now()))
        self.db.commit()
        return self.get_store(cur.lastrowid)

    def get_store(self, store_id):
        row = self.db.one("SELECT * FROM stores WHERE id=?", (store_id,))
        if row is None:
            raise NotFound("门店不存在")
        return dict(row)

    def list_stores(self):
        return [dict(r) for r in self.db.query("SELECT * FROM stores ORDER BY id")]

    def create_booth(self, user, store_id, code, name, x, y, w=1.0, h=1.0):
        _require(user, ROLE_MANAGER)
        try:
            self.db.execute(
                "INSERT INTO booths(store_id, code, name, pos_x, pos_y, pos_w, pos_h) "
                "VALUES(?,?,?,?,?,?,?)", (store_id, code, name, x, y, w, h))
            self.db.commit()
        except Exception as e:
            raise Conflict(f"展台编号 {code} 已存在") from e
        return self.get_booth_by_code(store_id, code)

    def get_booth_by_code(self, store_id, code):
        row = self.db.one("SELECT * FROM booths WHERE store_id=? AND code=?", (store_id, code))
        if row is None:
            raise NotFound(f"展台 {code} 不存在")
        return dict(row)

    def list_booths(self, store_id):
        return [dict(r) for r in self.db.query(
            "SELECT * FROM booths WHERE store_id=? ORDER BY code", (store_id,))]

    def register_device(self, user, serial, model, color, store_id=None, booth_id=None,
                        dock_code=None, cable_code=None, runtime=None):
        """登记设备。序列号全局唯一；同一展位不能同时放两台设备。"""
        _require(user, ROLE_MANAGER)
        if self.db.one("SELECT 1 FROM devices WHERE serial=?", (serial,)):
            raise Conflict(f"序列号 {serial} 已登记，同一序列号不能同时属于两个展位")
        if booth_id is not None:
            occ = self.db.one(
                "SELECT serial FROM devices WHERE booth_id=? AND status!='returned'", (booth_id,))
            if occ is not None:
                raise Conflict(f"该展台已放置设备 {occ['serial']}")
        profile = self.db.one("SELECT * FROM demo_profiles WHERE model=?", (model,))
        ts = now()
        self.db.execute(
            "INSERT INTO devices(serial, model, color, store_id, booth_id, dock_code, cable_code, "
            "registered_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (serial, model, color, store_id, booth_id, dock_code, cable_code, ts, ts))
        rt = runtime or {}
        rt = runtime or {}
        apps_val = rt.get("apps")
        if apps_val is not None:
            apps_val = json.dumps(apps_val, ensure_ascii=False)
        else:
            apps_val = profile["apps"] if profile else "[]"
        settings_val = rt.get("settings")
        if settings_val is not None:
            settings_val = json.dumps(settings_val, ensure_ascii=False)
        else:
            settings_val = profile["settings"] if profile else "{}"
        self.db.execute(
            "INSERT INTO device_runtime(serial, online, battery, appearance, network_state, "
            "logged_in, account_version, apps, wallpaper, settings, content_flag, faulty, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (serial, int(rt.get("online", True)), rt.get("battery", 96),
             rt.get("appearance", "ok"), rt.get("network_state", "wifi"),
             int(rt.get("logged_in", True)),
             rt.get("account_version") or (profile["account_version"] if profile else "demo-v1"),
             apps_val,
             rt.get("wallpaper") or (profile["wallpaper"] if profile else "brand-default.jpg"),
             settings_val,
             rt.get("content_flag"), int(rt.get("faulty", False)), ts))
        if dock_code:
            self.db.execute(
                "INSERT OR REPLACE INTO docks(code, device_serial, connected, alarm) VALUES(?,?,1,0)",
                (dock_code, serial))
        self.db.commit()
        return self.get_device(serial)

    def get_device(self, serial):
        row = self.db.one("SELECT * FROM devices WHERE serial=?", (serial,))
        if row is None:
            raise NotFound(f"设备 {serial} 不存在")
        d = dict(row)
        rt = self.db.one("SELECT * FROM device_runtime WHERE serial=?", (serial,))
        d["runtime"] = dict(rt) if rt else None
        if d["runtime"]:
            d["runtime"]["online"] = bool(d["runtime"]["online"])
            d["runtime"]["logged_in"] = bool(d["runtime"]["logged_in"])
            d["runtime"]["apps"] = DB.jloads(d["runtime"]["apps"], [])
            d["runtime"]["settings"] = DB.jloads(d["runtime"]["settings"], {})
        return d

    def list_devices(self, store_id=None):
        sql = "SELECT serial FROM devices"
        args = ()
        if store_id is not None:
            sql += " WHERE store_id=?"
            args = (store_id,)
        return [self.get_device(r["serial"]) for r in self.db.query(sql + " ORDER BY serial", args)]

    def floor_plan(self, store_id):
        """展台平面图：严格按柜面坐标 (pos_x,pos_y,pos_w,pos_h) 排布。"""
        booths = self.list_booths(store_id)
        for b in booths:
            dev = self.db.one(
                "SELECT serial, model, color, status FROM devices WHERE booth_id=? AND status!='returned'",
                (b["id"],))
            b["device"] = dict(dev) if dev else None
            if dev:
                rt = self.db.one("SELECT online, battery FROM device_runtime WHERE serial=?",
                                 (dev["serial"],))
                b["device"]["online"] = bool(rt["online"])
                b["device"]["battery"] = rt["battery"]
        return booths

    def save_evidence(self, serial, src_path_or_bytes, filename=None):
        """保存外观证据到本地目录，返回相对路径。证据长期保留。"""
        ext = os.path.splitext(filename or "")[1] or ".bin"
        safe_serial = "".join(c for c in str(serial) if c.isalnum() or c in "-_")
        base = f"{safe_serial}-{now().replace(':','').replace('-','')}"
        rel = f"{base}{ext}"
        dest = os.path.join(self.evidence_dir, rel)
        n = 1
        while os.path.exists(dest):
            rel = f"{base}-{n}{ext}"
            dest = os.path.join(self.evidence_dir, rel)
            n += 1
        if isinstance(src_path_or_bytes, (bytes, bytearray)):
            with open(dest, "wb") as f:
                f.write(src_path_or_bytes)
        else:
            shutil.copyfile(src_path_or_bytes, dest)
        return os.path.join("evidence", rel) if os.path.basename(
            os.path.dirname(self.evidence_dir)) == "" else os.path.relpath(
            dest, os.path.dirname(self.evidence_dir))

    def evidence_full_path(self, rel):
        return os.path.join(os.path.dirname(self.evidence_dir), rel)

    # ---------- 开闭店盘点 ----------
    def open_run(self, user, store_id, day=None, kind="open"):
        _require(user, ROLE_STAFF, ROLE_MANAGER)
        day = day or today()
        row = self.db.one(
            "SELECT * FROM inventory_runs WHERE store_id=? AND day=? AND kind=?",
            (store_id, day, kind))
        if row:
            if row["status"] == "closed":
                raise Conflict(f"当日{('开店' if kind=='open' else '闭店')}盘点已关闭")
            return dict(row)
        cur = self.db.execute(
            "INSERT INTO inventory_runs(store_id, day, kind, opened_by, opened_at) "
            "VALUES(?,?,?,?,?)", (store_id, day, kind, user["name"], now()))
        self.db.commit()
        run_id = cur.lastrowid
        # 预生成应盘设备清单（开/闭店时在店且非调店/退仓中的设备）
        devices = self.db.query(
            "SELECT serial, booth_id FROM devices WHERE store_id=? AND status IN ('on_display','pending_restore')",
            (store_id,))
        for d in devices:
            self.db.execute(
                "INSERT INTO inventory_items(run_id, serial, expected_booth_id, version) "
                "VALUES(?,?,?,1)", (run_id, d["serial"], d["booth_id"]))
        self.db.commit()
        return dict(self.db.one("SELECT * FROM inventory_runs WHERE id=?", (run_id,)))

    def get_run(self, run_id):
        row = self.db.one("SELECT * FROM inventory_runs WHERE id=?", (run_id,))
        if row is None:
            raise NotFound("盘点单不存在")
        run = dict(row)
        run["items"] = [dict(r) for r in self.db.query(
            "SELECT * FROM inventory_items WHERE run_id=? ORDER BY id", (run_id,))]
        return run

    def today_runs(self, store_id, day=None):
        day = day or today()
        out = []
        for r in self.db.query(
                "SELECT id FROM inventory_runs WHERE store_id=? AND day=? ORDER BY kind",
                (store_id, day)):
            out.append(self.get_run(r["id"]))
        return out

    def check_item(self, user, run_id, serial, base_version, **fields):
        """提交单台设备盘点结果（逐台聚焦）。base_version 为乐观锁，防止并发覆盖。"""
        _require(user, ROLE_STAFF, ROLE_MANAGER)  # 技术人员不能改盘点结论
        run = self.db.one("SELECT * FROM inventory_runs WHERE id=?", (run_id,))
        if run is None:
            raise NotFound("盘点单不存在")
        if run["status"] == "closed":
            raise Conflict("盘点单已关闭，不能修改")
        item = self.db.one(
            "SELECT * FROM inventory_items WHERE run_id=? AND serial=?", (run_id, serial))
        if item is None:
            raise NotFound(f"设备 {serial} 不在本盘点单内")
        if item["version"] != base_version:
            cur = self.get_run(run_id)
            cur_item = next(i for i in cur["items"] if i["serial"] == serial)
            raise Conflict("盘点数据已被其他人更新，请刷新后重试",
                           extra={"current": cur_item})
        # 位置核对：实际展位与期望展位不一致 → 展位错放
        actual_booth = fields.get("actual_booth_id", item["actual_booth_id"] or item["expected_booth_id"])
        missing = int(fields.get("missing", item["missing"]))
        appearance = fields.get("appearance", item["appearance"] or "ok")
        network = fields.get("network_state", item["network_state"] or "wifi")
        dock_ok = fields.get("dock_ok", item["dock_ok"])
        cable_ok = fields.get("cable_ok", item["cable_ok"])
        battery = fields.get("battery", item["battery"])
        note = fields.get("note", item["note"])
        evidence_path = fields.get("evidence_path", item["evidence_path"])

        abnormal, reasons = False, []
        if missing:
            abnormal = True
            reasons.append("设备缺失")
        if actual_booth != item["expected_booth_id"]:
            abnormal = True
            reasons.append("展位错放")
        if appearance == "scratch":
            abnormal = True
            reasons.append("外观划伤")
        if network == "offline":
            abnormal = True
            reasons.append("设备离线")
        if cable_ok is False or cable_ok == 0:
            abnormal = True
            reasons.append("防盗线异常")
        if dock_ok is False or dock_ok == 0:
            abnormal = True
            reasons.append("充电底座异常")
        rt = self.db.one("SELECT * FROM device_runtime WHERE serial=?", (serial,))
        if rt and not rt["logged_in"]:
            abnormal = True
            reasons.append("演示账号登出")
        if rt and rt["content_flag"]:
            abnormal = True
            reasons.append(f"异常内容：{rt['content_flag']}")

        result = "abnormal" if abnormal else "ok"
        self.db.execute(
            "UPDATE inventory_items SET actual_booth_id=?, battery=?, appearance=?, network_state=?, "
            "dock_ok=?, cable_ok=?, missing=?, result=?, note=?, evidence_path=?, checked_by=?, "
            "checked_at=?, version=version+1 WHERE id=?",
            (actual_booth, battery, appearance, network,
             None if dock_ok is None else int(bool(dock_ok)),
             None if cable_ok is None else int(bool(cable_ok)),
             missing, result, note, evidence_path, user["name"], now(), item["id"]))
        self.db.commit()

        # 异常联动登记（同一 run+serial+kind 去重）
        if missing:
            kind = "closing_missing" if run["kind"] == "close" else "closing_missing"
            self._raise_exception(serial, kind, "盘点发现设备缺失：" + (note or ""), user,
                                  run_id, [evidence_path] if evidence_path else None)
        if actual_booth != item["expected_booth_id"]:
            self._raise_exception(serial, "misplaced",
                                  f"实际展位与登记位置不一致，期望展台 {item['expected_booth_id']}",
                                  user, run_id)
        if appearance == "scratch":
            self._raise_exception(serial, "scratch", "外观划伤：" + (note or ""), user,
                                  run_id, [evidence_path] if evidence_path else None)
        if network == "offline" or (rt and not rt["online"]):
            self._raise_exception(serial, "offline", "设备离线 / 无法联网", user, run_id)
        if rt and not rt["logged_in"]:
            self._raise_exception(serial, "account_logout", "演示账号已登出", user, run_id)
        if rt and rt["content_flag"]:
            self._enqueue_restore(serial, f"异常内容：{rt['content_flag']}", user["name"])
        if rt and self._settings_drift(serial):
            self._enqueue_restore(serial, "设备设置/演示配置发生变更", user["name"])
        return self.get_run(run_id)

    def close_run(self, user, run_id, force_note=None):
        """闭店：核对序列号及配件，所有设备必须逐项盘点完毕。"""
        _require(user, ROLE_STAFF, ROLE_MANAGER)
        run = self.db.one("SELECT * FROM inventory_runs WHERE id=?", (run_id,))
        if run is None:
            raise NotFound("盘点单不存在")
        if run["status"] == "closed":
            raise Conflict("盘点单已关闭")
        items = self.db.query("SELECT * FROM inventory_items WHERE run_id=?", (run_id,))
        pending = [i for i in items if i["result"] is None]
        if pending:
            raise PreconditionFailed(
                f"还有 {len(pending)} 台设备未盘点（{', '.join(i['serial'] for i in pending[:5])}…）")
        missing = [i for i in items if i["missing"]]
        if missing and not force_note:
            raise PreconditionFailed(
                f"闭店少机 {len(missing)} 台，需店长填写少机说明后才能关单",
                extra={"missing_serials": [i["serial"] for i in missing]})
        if missing:
            _require(user, ROLE_MANAGER)
        self.db.execute(
            "UPDATE inventory_runs SET status='closed', closed_by=?, closed_at=?, version=version+1 "
            "WHERE id=?", (user["name"], now(), run_id))
        self.db.commit()
        return self.get_run(run_id)

    # ---------- 异常 ----------
    def report_exception(self, user, serial, kind, detail, evidence_paths=None):
        """导购只能报异常（以及店长）。"""
        _require(user, ROLE_STAFF, ROLE_MANAGER)
        valid = {"offline", "cable_alarm", "scratch", "account_logout", "settings_drift",
                 "restore_failed", "misplaced", "closing_missing", "transfer_rejected"}
        if kind not in valid:
            raise ValidationError(f"未知异常类型 {kind}")
        return self._raise_exception(serial, kind, detail, user, None, evidence_paths)

    def _raise_exception(self, serial, kind, detail, user, run_id, evidence_paths=None):
        exist = self.db.one(
            "SELECT id FROM exceptions WHERE serial=? AND kind=? AND status='open' "
            "AND IFNULL(run_id,-1)=IFNULL(?,-1)", (serial, kind, run_id))
        if exist:
            return dict(self.db.one("SELECT * FROM exceptions WHERE id=?", (exist["id"],)))
        cur = self.db.execute(
            "INSERT INTO exceptions(serial, kind, detail, reporter, reporter_role, run_id, "
            "evidence_paths, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (serial, kind, detail, user["name"], user["role"], run_id,
             json.dumps(evidence_paths or [], ensure_ascii=False), now()))
        self.db.commit()
        return dict(self.db.one("SELECT * FROM exceptions WHERE id=?", (cur.lastrowid,)))

    def list_exceptions(self, status=None, store_id=None):
        sql = ("SELECT e.* FROM exceptions e LEFT JOIN devices d ON d.serial=e.serial WHERE 1=1")
        args = []
        if status:
            sql += " AND e.status=?"
            args.append(status)
        if store_id is not None:
            sql += " AND (d.store_id=? OR e.serial IS NULL)"
            args.append(store_id)
        sql += " ORDER BY e.id DESC"
        rows = []
        for r in self.db.query(sql, tuple(args)):
            d = dict(r)
            d["evidence_paths"] = DB.jloads(d["evidence_paths"], [])
            rows.append(d)
        return rows

    def resolve_exception(self, user, exc_id, resolution):
        _require(user, ROLE_MANAGER)
        row = self.db.one("SELECT * FROM exceptions WHERE id=?", (exc_id,))
        if row is None:
            raise NotFound("异常不存在")
        if row["status"] == "resolved":
            raise Conflict("异常已处理")
        self.db.execute(
            "UPDATE exceptions SET status='resolved', resolved_at=?, resolved_by=?, resolution=? "
            "WHERE id=?", (now(), user["name"], resolution, exc_id))
        self.db.commit()
        return dict(self.db.one("SELECT * FROM exceptions WHERE id=?", (exc_id,)))

    # ---------- 还原 ----------
    def expected_config(self, model):
        row = self.db.one("SELECT * FROM demo_profiles WHERE model=?", (model,))
        if row is None:
            raise NotFound(f"机型 {model} 缺少演示配置基线")
        return {"account_version": row["account_version"],
                "apps": DB.jloads(row["apps"], []),
                "wallpaper": row["wallpaper"],
                "settings": DB.jloads(row["settings"], {})}

    def _settings_drift(self, serial):
        d = self.get_device(serial)
        rt = d["runtime"]
        try:
            exp = self.expected_config(d["model"])
        except NotFound:
            return False
        return (rt["account_version"] != exp["account_version"]
                or sorted(rt["apps"]) != sorted(exp["apps"])
                or rt["wallpaper"] != exp["wallpaper"]
                or rt["settings"] != exp["settings"])

    def _enqueue_restore(self, serial, reason, by):
        """设置变更或异常内容 → 进入待还原。"""
        d = self.get_device(serial)
        if d["status"] in ("in_transfer", "returned"):
            return None
        active = self.db.one(
            "SELECT id FROM restore_jobs WHERE serial=? AND status IN ('queued','running','failed')",
            (serial,))
        if active:
            return dict(self.db.one("SELECT * FROM restore_jobs WHERE id=?", (active["id"],)))
        expected = self.expected_config(d["model"])
        cur = self.db.execute(
            "INSERT INTO restore_jobs(serial, status, reason, expected, attempts, created_by, "
            "created_at, updated_at) VALUES(?, 'queued', ?, ?, 0, ?, ?, ?)",
            (serial, reason, json.dumps(expected, ensure_ascii=False),
             by, now(), now()))
        self.db.execute(
            "UPDATE devices SET status='pending_restore', updated_at=? WHERE serial=?",
            (now(), serial))
        self.db.commit()
        return dict(self.db.one("SELECT * FROM restore_jobs WHERE id=?", (cur.lastrowid,)))

    def enqueue_restore(self, user, serial, reason):
        _require(user, ROLE_MANAGER, ROLE_TECH)
        return self._enqueue_restore(serial, reason, user["name"])

    def list_restore_jobs(self, status=None):
        sql = "SELECT * FROM restore_jobs"
        args = ()
        if status:
            sql += " WHERE status=?"
            args = (status,)
        sql += " ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 WHEN 'failed' THEN 2 ELSE 3 END, id"
        out = []
        for r in self.db.query(sql, args):
            j = dict(r)
            for k in ("expected", "actual", "diff"):
                j[k] = DB.jloads(j[k], None)
            out.append(j)
        return out

    def _diff_config(self, expected, actual):
        """逐字段对比期望与实际配置，只校验演示账号、应用与壁纸（设置项同时展示）。"""
        diff = {}
        for field in ("account_version", "wallpaper", "settings"):
            if expected.get(field) != actual.get(field):
                diff[field] = {"expected": expected.get(field), "actual": actual.get(field)}
        if sorted(expected.get("apps", [])) != sorted(actual.get("apps", [])):
            diff["apps"] = {"expected": expected.get("apps", []), "actual": actual.get("apps", [])}
        return diff

    def verify_job(self, job_id):
        """版本校验：拉取设备当前配置并与期望配置逐字段对比。"""
        from .adapters import DeviceManagementAdapter
        job = self.db.one("SELECT * FROM restore_jobs WHERE id=?", (job_id,))
        if job is None:
            raise NotFound("还原任务不存在")
        mdm = DeviceManagementAdapter(self.db)
        snap = mdm.snapshot(job["serial"])
        expected = DB.jloads(job["expected"], {})
        actual = {k: snap[k] for k in ("account_version", "apps", "wallpaper", "settings")}
        diff = self._diff_config(expected, actual)
        self.db.execute("UPDATE restore_jobs SET actual=?, diff=?, updated_at=? WHERE id=?",
                        (json.dumps(actual, ensure_ascii=False),
                         json.dumps(diff, ensure_ascii=False), now(), job_id))
        self.db.commit()
        return {"job_id": job_id, "passed": not diff, "diff": diff,
                "expected": expected, "actual": actual}

    def execute_restore(self, user, job_id, adapter=None, simulate_failure=False):
        """技术人员执行还原。导购/店长不能执行；技术人员不能改盘点结论。"""
        _require(user, ROLE_TECH)
        from .adapters import DeviceManagementAdapter
        job = self.db.one("SELECT * FROM restore_jobs WHERE id=?", (job_id,))
        if job is None:
            raise NotFound("还原任务不存在")
        if job["status"] == "success":
            raise Conflict("该任务已还原成功")
        d = self.get_device(job["serial"])
        if d["status"] == "returned":
            raise PreconditionFailed("设备已退仓，不能还原")
        expected = DB.jloads(job["expected"], {})
        self.db.execute(
            "UPDATE restore_jobs SET status='running', attempts=attempts+1, updated_at=? WHERE id=?",
            (now(), job_id))
        self.db.execute("UPDATE devices SET status='restoring', updated_at=? WHERE serial=?",
                        (now(), job["serial"]))
        self.db.commit()

        mdm = adapter or DeviceManagementAdapter(self.db)
        ok, actual_snap, error = mdm.push_restore(job["serial"], expected, simulate_failure)
        if ok:
            actual = {k: actual_snap[k] for k in
                      ("account_version", "apps", "wallpaper", "settings")}
            diff = self._diff_config(expected, actual)
            passed = not diff
            self.db.execute(
                "UPDATE restore_jobs SET status='success', actual=?, diff=?, last_error=NULL, "
                "executed_at=?, updated_at=? WHERE id=?",
                (json.dumps(actual, ensure_ascii=False),
                 json.dumps(diff, ensure_ascii=False), now(), now(), job_id))
            if passed:
                # 演示账号、应用、壁纸校验通过后才能回到可展示
                self.db.execute(
                    "UPDATE devices SET status='on_display', updated_at=? WHERE serial=?",
                    (now(), job["serial"]))
                self.db.execute(
                    "UPDATE exceptions SET status='resolved', resolved_at=?, resolved_by=?, "
                    "resolution='系统还原后校验通过' WHERE serial=? AND kind IN "
                    "('settings_drift','restore_failed','account_logout') AND status='open'",
                    (now(), user["name"], job["serial"]))
            self.db.commit()
            return {"job_id": job_id, "ok": True, "passed": passed, "diff": diff}
        # 失败：保留待还原状态，登记异常，等待进程内重试
        actual_d = {k: actual_snap[k] for k in
                    ("account_version", "apps", "wallpaper", "settings")} if actual_snap else None
        self.db.execute(
            "UPDATE restore_jobs SET status='failed', actual=?, last_error=?, updated_at=? WHERE id=?",
            (json.dumps(actual_d, ensure_ascii=False) if actual_d else None, error, now(), job_id))
        self.db.execute(
            "UPDATE devices SET status='pending_restore', updated_at=? WHERE serial=?",
            (now(), job["serial"]))
        self.db.commit()
        self._raise_exception(
            job["serial"], "restore_failed", f"还原失败（第 {job['attempts'] + 1} 次）：{error}",
            {"name": user["name"], "role": user["role"]}, None)
        return {"job_id": job_id, "ok": False, "passed": False, "error": error}

    def retry_failed_restores(self, adapter=None):
        """进程内定时任务调用：自动重试失败的还原任务。"""
        results = []
        rows = self.db.query(
            "SELECT * FROM restore_jobs WHERE status='failed' AND attempts < max_attempts")
        tech = self.db.one("SELECT * FROM users WHERE role='tech'")
        for r in rows:
            try:
                res = self.execute_restore(dict(tech), r["id"], adapter=adapter)
                results.append({"job_id": r["id"], **res})
            except Exception as e:  # 单台失败不影响其它重试
                results.append({"job_id": r["id"], "ok": False, "error": str(e)})
        return results

    # ---------- 离线配置包 ----------
    def build_offline_package(self, model):
        """在本地目录生成离线还原配置包，供设备离线时 U 盘/内网分发。"""
        expected = self.expected_config(model)
        fname = f"offline-package-{model.replace(' ', '_')}.json"
        path = os.path.join(self.package_dir, fname)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"model": model, "built_at": now(), "config": expected}, f,
                      ensure_ascii=False, indent=2)
        return {"file": os.path.relpath(path, os.path.dirname(self.package_dir)),
                "config": expected}

    def list_packages(self):
        if not os.path.isdir(self.package_dir):
            return []
        return sorted(os.listdir(self.package_dir))

    # ---------- 调店 ----------
    def request_transfer(self, user, serial, to_store_id, to_booth_id=None, note=""):
        """发起调店：导购可申请、店长也可；调店立即冻结原展位。"""
        _require(user, ROLE_STAFF, ROLE_MANAGER)
        d = self.get_device(serial)
        if d["status"] in ("in_transfer", "returned"):
            raise Conflict(f"设备当前状态 {d['status']}，不能调店")
        if d["booth_id"] is None:
            raise ValidationError("设备不在展台上，无法发起调店")
        if to_store_id == d["store_id"] and to_booth_id == d["booth_id"]:
            raise ValidationError("源展位与目标展位相同")
        if to_booth_id is not None:
            occ = self.db.one(
                "SELECT serial FROM devices WHERE booth_id=? AND status!='returned' AND serial!=?",
                (to_booth_id, serial))
            if occ is not None:
                raise Conflict(f"目标展台已放置设备 {occ['serial']}")
        cur = self.db.execute(
            "INSERT INTO transfers(serial, from_store_id, from_booth_id, to_store_id, to_booth_id, "
            "status, requested_by, note, created_at) VALUES(?,?,?,?,?, 'requested', ?,?,?)",
            (serial, d["store_id"], d["booth_id"], to_store_id, to_booth_id,
             user["name"], note, now()))
        self.db.execute("UPDATE booths SET frozen=1 WHERE id=?", (d["booth_id"],))
        self.db.commit()
        return self.get_transfer(cur.lastrowid)

    def approve_transfer(self, user, transfer_id):
        """店长批准调店，并生成交接记录。"""
        _require(user, ROLE_MANAGER)
        t = self.db.one("SELECT * FROM transfers WHERE id=?", (transfer_id,))
        if t is None:
            raise NotFound("调店单不存在")
        if t["status"] != "requested":
            raise Conflict(f"调店单状态为 {t['status']}，不能批准")
        d = self.get_device(t["serial"])
        handover = {
            "serial": t["serial"], "model": d["model"], "color": d["color"],
            "dock_code": d["dock_code"], "cable_code": d["cable_code"],
            "from_store_id": t["from_store_id"], "from_booth_id": t["from_booth_id"],
            "to_store_id": t["to_store_id"], "to_booth_id": t["to_booth_id"],
            "approved_by": user["name"], "approved_at": now(),
            "accessories": {"充电底座": d["dock_code"], "防盗线": d["cable_code"]},
        }
        self.db.execute(
            "UPDATE transfers SET status='approved', approved_by=?, approved_at=?, handover=? "
            "WHERE id=?",
            (user["name"], now(), json.dumps(handover, ensure_ascii=False), transfer_id))
        self.db.execute("UPDATE devices SET status='in_transfer', updated_at=? WHERE serial=?",
                        (now(), t["serial"]))
        self.db.commit()
        return self.get_transfer(transfer_id)

    def receive_transfer(self, user, transfer_id, accepted, to_booth_id=None, reason=""):
        """目标门店店长接收/拒收。拒收回到原展位并解冻。"""
        _require(user, ROLE_MANAGER)
        t = self.db.one("SELECT * FROM transfers WHERE id=?", (transfer_id,))
        if t is None:
            raise NotFound("调店单不存在")
        if t["status"] != "approved":
            raise Conflict(f"调店单状态为 {t['status']}，不能收货")
        if accepted:
            booth_id = to_booth_id or t["to_booth_id"]
            if booth_id is None:
                raise ValidationError("请选择目标展台")
            occ = self.db.one(
                "SELECT serial FROM devices WHERE booth_id=? AND status!='returned' AND serial!=?",
                (booth_id, t["serial"]))
            if occ is not None:
                raise Conflict(f"目标展台已放置设备 {occ['serial']}")
            self.db.execute(
                "UPDATE devices SET store_id=?, booth_id=?, status='on_display', updated_at=? "
                "WHERE serial=?", (t["to_store_id"], booth_id, now(), t["serial"]))
            self.db.execute(
                "UPDATE transfers SET status='received', to_booth_id=?, received_by=?, received_at=? "
                "WHERE id=?", (booth_id, user["name"], now(), transfer_id))
            self.db.execute("UPDATE booths SET frozen=0 WHERE id=?", (t["from_booth_id"],))
        else:
            # 调店拒收：设备回到原展位，原展位解冻
            if not reason:
                raise ValidationError("拒收必须填写原因")
            self.db.execute(
                "UPDATE devices SET status='on_display', updated_at=? WHERE serial=?",
                (now(), t["serial"]))
            self.db.execute(
                "UPDATE transfers SET status='rejected', reject_reason=?, received_by=?, received_at=? "
                "WHERE id=?", (reason, user["name"], now(), transfer_id))
            self.db.execute("UPDATE booths SET frozen=0 WHERE id=?", (t["from_booth_id"],))
            self._raise_exception(
                t["serial"], "transfer_rejected", f"调店拒收：{reason}",
                {"name": user["name"], "role": user["role"]}, None)
        self.db.commit()
        return self.get_transfer(transfer_id)

    def recall_transfer(self, user, transfer_id):
        _require(user, ROLE_MANAGER)
        t = self.db.one("SELECT * FROM transfers WHERE id=?", (transfer_id,))
        if t is None:
            raise NotFound("调店单不存在")
        if t["status"] not in ("requested", "approved"):
            raise Conflict("仅待批准/已批准未收货的调店单可以撤回")
        self.db.execute(
            "UPDATE transfers SET status='recalled', received_at=? WHERE id=?", (now(), transfer_id))
        self.db.execute(
            "UPDATE devices SET status='on_display', updated_at=? WHERE serial=?",
            (now(), t["serial"]))
        self.db.execute("UPDATE booths SET frozen=0 WHERE id=?", (t["from_booth_id"],))
        self.db.commit()
        return self.get_transfer(transfer_id)

    def get_transfer(self, transfer_id):
        row = self.db.one("SELECT * FROM transfers WHERE id=?", (transfer_id,))
        if row is None:
            raise NotFound("调店单不存在")
        t = dict(row)
        t["handover"] = DB.jloads(t["handover"], None)
        return t

    def list_transfers(self, status=None):
        sql = "SELECT * FROM transfers"
        args = ()
        if status:
            sql += " WHERE status=?"
            args = (status,)
        sql += " ORDER BY id DESC"
        rows = []
        for r in self.db.query(sql, args):
            t = dict(r)
            t["handover"] = DB.jloads(t["handover"], None)
            rows.append(t)
        return rows

    # ---------- 退仓 ----------
    def request_return(self, user, serial, reason, evidence_paths=None):
        _require(user, ROLE_STAFF, ROLE_MANAGER)
        d = self.get_device(serial)
        if d["status"] == "returned":
            raise Conflict("设备已退仓")
        if d["status"] == "in_transfer":
            raise Conflict("调店流程中的设备不能退仓")
        cur = self.db.execute(
            "INSERT INTO returns(serial, reason, status, requested_by, evidence_paths, created_at) "
            "VALUES(?,?, 'pending', ?,?,?)",
            (serial, reason, user["name"],
             json.dumps(evidence_paths or [], ensure_ascii=False), now()))
        self.db.commit()
        return dict(self.db.one("SELECT * FROM returns WHERE id=?", (cur.lastrowid,)))

    def review_return(self, user, return_id, approved, note=""):
        """退仓复核：店长批准后设备退仓、展位解冻；证据保留。"""
        _require(user, ROLE_MANAGER)
        row = self.db.one("SELECT * FROM returns WHERE id=?", (return_id,))
        if row is None:
            raise NotFound("退仓单不存在")
        if row["status"] != "pending":
            raise Conflict("退仓单已复核")
        if approved:
            d = self.get_device(row["serial"])
            self.db.execute(
                "UPDATE devices SET status='returned', booth_id=NULL, dock_code=NULL, cable_code=NULL, "
                "updated_at=? WHERE serial=?", (now(), row["serial"]))
            if d["booth_id"]:
                self.db.execute("UPDATE booths SET frozen=0 WHERE id=?", (d["booth_id"],))
            self.db.execute(
                "UPDATE docks SET device_serial=NULL, connected=0 WHERE device_serial=?",
                (row["serial"],))
            self.db.execute(
                "UPDATE returns SET status='approved', reviewed_by=?, note=?, reviewed_at=? WHERE id=?",
                (user["name"], note, now(), return_id))
        else:
            self.db.execute(
                "UPDATE returns SET status='rejected', reviewed_by=?, note=?, reviewed_at=? WHERE id=?",
                (user["name"], note, now(), return_id))
        self.db.commit()
        return dict(self.db.one("SELECT * FROM returns WHERE id=?", (return_id,)))

    def list_returns(self, status=None):
        sql = "SELECT * FROM returns"
        args = ()
        if status:
            sql += " WHERE status=?"
            args = (status,)
        out = []
        for r in self.db.query(sql + " ORDER BY id DESC", args):
            d = dict(r)
            d["evidence_paths"] = DB.jloads(d["evidence_paths"], [])
            out.append(d)
        return out

    # ---------- 提醒 ----------
    def add_reminder(self, kind, message, store_id=None):
        self.db.execute(
            "INSERT INTO reminders(kind, message, store_id, created_at) VALUES(?,?,?,?)",
            (kind, message, store_id, now()))
        self.db.commit()

    def list_reminders(self, unread_only=False):
        sql = "SELECT * FROM reminders"
        if unread_only:
            sql += " WHERE read=0"
        return [dict(r) for r in self.db.query(sql + " ORDER BY id DESC LIMIT 50")]

    def mark_reminder_read(self, reminder_id):
        self.db.execute("UPDATE reminders SET read=1 WHERE id=?", (reminder_id,))
        self.db.commit()
