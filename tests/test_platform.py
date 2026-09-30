"""关键用例测试（标准库 unittest，无第三方依赖）：

1. 序列号唯一：重复序列号拒绝；同一设备不能同时占两个展位
2. 盘点冲突：同一设备盘点记录按版本防并发覆盖
3. 还原前置：非待还原设备不能执行还原；校验通过才回可展示
4. 调店权限：导购/技术员不能批准调店；批准冻结原展位
5. 证据保留：划伤异常无证据不能办结；证据留档不可删除

运行：python3 -m unittest discover -s tests -v
"""
import base64
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import Database
from app.adapters import build_adapters
from app.services import Service, ServiceError
from app.seed import seed_if_empty
from app.tasks import TaskRunner


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="demo-test-")
        self.db = Database(os.path.join(self.dir, "app.db"))
        mdm, dock = build_adapters(self.dir)
        self.mdm, self.dock = mdm, dock
        self.svc = Service(self.db, mdm, dock, self.dir)
        seed_if_empty(self.svc)
        q = self.db.one
        self.sales = q("SELECT * FROM users WHERE role='sales' LIMIT 1")
        self.manager = q("SELECT * FROM users WHERE role='manager' AND store_id=1")
        self.manager2 = q("SELECT * FROM users WHERE role='manager' AND store_id=2")
        self.tech = q("SELECT * FROM users WHERE role='technician' LIMIT 1")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def assertSvcErr(self, status, fn, *args, **kw):
        with self.assertRaises(ServiceError) as cm:
            fn(*args, **kw)
        self.assertEqual(cm.exception.status, status, cm.exception.message)
        return cm.exception

    def dev(self, serial):
        return self.db.one("SELECT * FROM devices WHERE serial=?", (serial,))


class TestSerialUnique(Base):
    def test_duplicate_serial_rejected(self):
        self.assertEqual(self.dev("DEMO-X1-0001")["serial"], "DEMO-X1-0001")
        e = self.assertSvcErr(409, self.svc.register_device, self.manager, {
            "serial": "DEMO-X1-0001", "model": "星耀X1", "color": "曜石黑",
            "store_id": 1, "booth_id": None})
        self.assertIn("序列号", e.message)

    def test_serial_cannot_occupy_two_booths(self):
        # 已占用的展台不能再放别的设备
        occ_booth = self.dev("DEMO-X1-0001")["booth_id"]
        e = self.assertSvcErr(409, self.svc.register_device, self.manager, {
            "serial": "DEMO-NEW-9001", "model": "星耀X1", "color": "曜石黑",
            "store_id": 1, "booth_id": occ_booth})
        self.assertIn("已放置设备", e.message)
        # 已上架设备不能直接移到被占用展台（须先腾出）
        d2 = self.dev("DEMO-X1-0002")
        self.assertSvcErr(409, self.svc.relocate_device,
                          self.manager, d2["id"], occ_booth, d2["version"])
        # 移到空展台成功
        empty = self.db.one("SELECT * FROM booths WHERE code='C3'")
        moved = self.svc.relocate_device(
            self.manager, d2["id"], empty["id"], d2["version"])
        self.assertEqual(moved["booth_id"], empty["id"])


class TestInventoryConflict(Base):
    def _start_open(self):
        return self.svc.start_inventory(self.manager, 1, "open")

    def test_second_start_conflict(self):
        self._start_open()
        self.assertSvcErr(409, self.svc.start_inventory, self.manager, 1, "open")

    def test_version_conflict_on_item(self):
        inv = self._start_open()
        d = self.dev("DEMO-X1-0001")
        item = self.svc.submit_item(self.manager, inv["id"], d["id"], {
            "position_ok": True, "appearance_ok": True, "online": True, "battery": 90})
        self.assertEqual(item["version"], 1)
        # 正常更新 → version 2
        item2 = self.svc.submit_item(self.tech, inv["id"], d["id"], {
            "position_ok": True, "appearance_ok": False, "online": True,
            "battery": 90, "version": 1})
        self.assertEqual(item2["version"], 2)
        # 用旧版本提交 → 409 防覆盖
        self.assertSvcErr(409, self.svc.submit_item, self.manager,
                          inv["id"], d["id"],
                          {"position_ok": True, "appearance_ok": True,
                           "online": True, "battery": 90, "version": 1})

    def test_technician_cannot_set_conclusion(self):
        inv = self._start_open()
        d = self.dev("DEMO-X1-0001")
        self.assertSvcErr(403, self.svc.submit_item, self.tech,
                          inv["id"], d["id"],
                          {"position_ok": False, "appearance_ok": False,
                           "online": False, "conclusion": "ok"})
        # 导购不能盘点
        self.assertSvcErr(403, self.svc.submit_item, self.sales,
                          inv["id"], d["id"], {"position_ok": True})


class TestRestorePrerequisite(Base):
    def test_restore_requires_pending_status(self):
        # 可展示设备直接建还原任务 → 409
        d = self.dev("DEMO-X1-0001")
        self.assertEqual(d["status"], "displayable")
        self.assertSvcErr(409, self.svc.create_restore_task, d["id"], self.tech)
        # 手工把任务插到可展示设备上也不允许执行（前置校验）
        drifted = self.dev("DEMO-X1P-0003")
        self.assertEqual(drifted["status"], "pending_restore")
        task = self.svc.db.one(
            "SELECT * FROM restore_tasks WHERE device_id=? AND status='pending'",
            (drifted["id"],))
        self.assertIsNotNone(task)

    def test_restore_permission_and_flow(self):
        drifted = self.dev("DEMO-X1P-0003")
        task = self.svc.db.one(
            "SELECT * FROM restore_tasks WHERE device_id=? AND status='pending'",
            (drifted["id"],))
        # 店长不能执行还原（仅技术员）
        self.assertSvcErr(403, self.svc.execute_restore, self.manager, task["id"])
        # 技术员执行 → 校验通过 → 回到可展示
        done = self.svc.execute_restore(self.tech, task["id"])
        self.assertEqual(done["status"], "success")
        self.assertEqual(self.dev("DEMO-X1P-0003")["status"], "displayable")
        self.assertTrue(self.svc.verify_device(drifted["id"])["ok"])

    def test_flaky_restore_retry_by_task_runner(self):
        flaky = self.dev("DEMO-X1-0007")
        self.assertEqual(flaky["status"], "pending_restore")
        task = self.svc.db.one(
            "SELECT * FROM restore_tasks WHERE device_id=? AND status='pending'",
            (flaky["id"],))
        # 第一次执行失败（适配器设定首次刷机失败）
        r1 = self.svc.execute_restore(self.tech, task["id"])
        self.assertEqual(r1["status"], "failed")
        self.assertEqual(self.dev("DEMO-X1-0007")["status"], "pending_restore")
        # 进程内任务自动重试 → 成功
        runner = TaskRunner(self.svc, interval=9999)
        runner.tick()
        r2 = self.svc.db.one("SELECT * FROM restore_tasks WHERE id=?", (task["id"],))
        self.assertEqual(r2["status"], "success")
        self.assertEqual(self.dev("DEMO-X1-0007")["status"], "displayable")


class TestTransferPermission(Base):
    def _create(self):
        d = self.dev("DEMO-X1-0001")
        booth_c3 = self.db.one("SELECT * FROM booths WHERE code='C3'")
        return self.svc.create_transfer(self.manager, d["id"], 2, booth_c3["id"])

    def test_approve_requires_manager(self):
        t = self._create()
        self.assertSvcErr(403, self.svc.approve_transfer, self.sales, t["id"])
        self.assertSvcErr(403, self.svc.approve_transfer, self.tech, t["id"])
        ok = self.svc.approve_transfer(self.manager, t["id"])
        self.assertEqual(ok["status"], "in_transit")

    def test_approve_freezes_booth_and_handover(self):
        t = self._create()
        d = self.dev("DEMO-X1-0001")
        booth_id = d["booth_id"]
        self.svc.approve_transfer(self.manager, t["id"])
        booth = self.db.one("SELECT * FROM booths WHERE id=?", (booth_id,))
        self.assertEqual(booth["status"], "frozen")
        self.assertEqual(self.dev("DEMO-X1-0001")["status"], "in_transit")
        handover = self.svc.list_transfers()[0]["handover"]
        self.assertEqual(handover["serial"], "DEMO-X1-0001")
        self.assertIn("dock_serial", handover)
        # 收货后解冻并落到目标展台
        self.svc.receive_transfer(self.manager2, t["id"])
        self.assertEqual(self.db.one(
            "SELECT status FROM booths WHERE id=?", (booth_id,))["status"], "active")
        moved = self.dev("DEMO-X1-0001")
        self.assertEqual((moved["store_id"], moved["status"]), (2, "displayable"))

    def test_reject_returns_device(self):
        t = self._create()
        self.svc.approve_transfer(self.manager, t["id"])
        self.svc.reject_transfer(self.manager2, t["id"], "包装破损")
        d = self.dev("DEMO-X1-0001")
        self.assertEqual((d["store_id"], d["status"]), (1, "displayable"))
        anomalies = [a for a in self.svc.list_anomalies()
                     if a["type"] == "transfer_rejected"]
        self.assertTrue(anomalies)


class TestEvidenceRetention(Base):
    def _scratch(self):
        d = self.dev("DEMO-X1-0001")
        return self.svc.report_anomaly(self.sales, d["id"], "scratch", "边框划痕")

    def test_scratch_requires_evidence(self):
        a = self._scratch()
        e = self.assertSvcErr(400, self.svc.resolve_anomaly,
                              self.manager, a["id"], "已贴膜", {})
        self.assertIn("证据", e.message)

    def test_evidence_saved_on_disk_and_kept(self):
        a = self._scratch()
        ev = self.svc.add_evidence(self.sales, a["id"], "photo.txt",
                                   base64.b64encode(b"evidence-bytes").decode())
        self.assertTrue(os.path.isfile(ev["path"]))
        self.assertIn(str(a["id"]), ev["path"])
        # 证据不可删除（留档策略）
        self.assertSvcErr(409, self.svc.delete_evidence, self.manager, ev["id"])
        # 有证据后可办结，办结后证据仍保留
        done = self.svc.resolve_anomaly(self.manager, a["id"], "已记录并贴膜", {})
        self.assertEqual(done["status"], "resolved")
        self.assertSvcErr(409, self.svc.delete_evidence, self.manager, ev["id"])
        self.assertTrue(os.path.isfile(ev["path"]))


class TestHttpApi(unittest.TestCase):
    """端到端：真实 HTTP 服务冒烟。"""

    def test_http_smoke(self):
        import json
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        from app.server import build_router, make_handler
        from app.tasks import TaskRunner

        d = tempfile.mkdtemp(prefix="demo-http-")
        try:
            db = Database(os.path.join(d, "app.db"))
            mdm, dock = build_adapters(d)
            svc = Service(db, mdm, dock, d)
            seed_if_empty(svc)
            tasks = TaskRunner(svc, interval=9999)
            router = build_router(svc, tasks)
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(router, svc))
            port = httpd.server_address[1]
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            try:
                def get(path, uid=2):
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{port}{path}",
                        headers={"X-User-Id": str(uid)})
                    with urllib.request.urlopen(req) as r:
                        return json.loads(r.read())
                boot = get("/api/bootstrap", uid=None) if False else get("/api/bootstrap")
                self.assertEqual(len(boot["stores"]), 2)
                plan = get("/api/stores/1/plan")
                self.assertEqual(len(plan["booths"]), 6)
                # 未授权：导购执行还原 → 403
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/restore-tasks/1/execute",
                    method="POST", data=b"{}",
                    headers={"X-User-Id": "1", "Content-Type": "application/json"})
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    urllib.request.urlopen(req)
                self.assertEqual(cm.exception.code, 403)
            finally:
                httpd.shutdown()
                httpd.server_close()
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
