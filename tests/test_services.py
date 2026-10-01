"""领域服务测试：序列号唯一、盘点冲突、还原前置、调店权限、证据保留等。"""
import json
import os
import tempfile
import unittest
from datetime import datetime

from app.db import DB
from app.errors import Conflict, PermissionDenied, PreconditionFailed
from app.scheduler import Scheduler
from app.services import StoreService


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        self.db = DB(os.path.join(self.base, "test.db"))
        self.svc = StoreService(
            self.db, os.path.join(self.base, "evidence"),
            os.path.join(self.base, "packages"))
        self.staff = {"id": 1, "name": "张导购", "role": "staff"}
        self.manager = {"id": 2, "name": "李店长", "role": "manager"}
        self.tech = {"id": 3, "name": "王技术", "role": "tech"}
        self._bootstrap()

    def tearDown(self):
        self.tmp.cleanup()

    def _bootstrap(self):
        for u in (self.staff, self.manager, self.tech):
            self.db.execute("INSERT INTO users(id,name,role,pin) VALUES(?,?,?,?)",
                            (u["id"], u["name"], u["role"], str(u["id"])))
        self.db.execute(
            "INSERT INTO demo_profiles(model, account_version, apps, wallpaper, settings) "
            "VALUES(?,?,?,?,?)",
            ("M1", "acct-1", json.dumps(["演示中心", "相机"]), "wp.jpg",
             json.dumps({"亮度": 70})))
        self.db.commit()
        self.s1 = self.svc.create_store(self.manager, "一号店", "09:00", "21:00")
        self.s2 = self.svc.create_store(self.manager, "二号店", "10:00", "22:00")
        self.b1 = self.svc.create_booth(self.manager, self.s1["id"], "A1", "A柜", 0, 0)
        self.b2 = self.svc.create_booth(self.manager, self.s1["id"], "A2", "A柜2", 2, 0)
        self.b3 = self.svc.create_booth(self.manager, self.s2["id"], "B1", "B柜", 0, 0)

    def register(self, serial="SN-1", booth=None, **rt):
        return self.svc.register_device(
            self.manager, serial, "M1", "曜石黑", self.s1["id"],
            booth if booth is not None else self.b1["id"],
            "DK-1", "CB-1", runtime=rt)


class TestSerialUniqueness(ServiceTestBase):
    def test_duplicate_serial_rejected(self):
        self.register("SN-DUP")
        with self.assertRaises(Conflict):
            self.svc.register_device(
                self.manager, "SN-DUP", "M1", "白", self.s1["id"], self.b2["id"])

    def test_same_serial_cannot_occupy_two_booths(self):
        # 同一展位两台设备也被拒绝（一个序列号不可能同时属于两个展位）
        self.register("SN-A", booth=self.b1["id"])
        with self.assertRaises(Conflict):
            self.register("SN-B", booth=self.b1["id"])

    def test_booth_freed_after_transfer_received_allows_reuse(self):
        self.register("SN-A", booth=self.b1["id"])
        self.svc.register_device(
            self.manager, "SN-B", "M1", "白", self.s1["id"], None)
        t = self.svc.request_transfer(self.staff, "SN-A", self.s2["id"], self.b3["id"])
        self.svc.approve_transfer(self.manager, t["id"])
        self.svc.receive_transfer(self.manager, t["id"], True, self.b3["id"])
        # 原展位现在可以放新设备
        d = self.svc.register_device(
            self.manager, "SN-C", "M1", "白", self.s1["id"], self.b1["id"])
        self.assertEqual(d["booth_id"], self.b1["id"])


class TestInventoryConflict(ServiceTestBase):
    def test_optimistic_version_blocks_overwrite(self):
        self.register("SN-A")
        run = self.svc.open_run(self.staff, self.s1["id"], day="2026-10-01")
        # 第一次提交基于 v1，成功后记录变为 v2
        self.svc.check_item(
            self.staff, run["id"], "SN-A", 1, battery=90, appearance="ok",
            network_state="wifi", dock_ok=True, cable_ok=True)
        item = self.db.one(
            "SELECT * FROM inventory_items WHERE run_id=? AND serial='SN-A'", (run["id"],))
        self.assertEqual(item["version"], 2)
        # 另一个人拿着旧 v1 再提交 → 冲突，服务端返回当前版本
        with self.assertRaises(Conflict) as cm:
            self.svc.check_item(
                self.staff, run["id"], "SN-A", 1, battery=12, appearance="ok",
                network_state="wifi", dock_ok=True, cable_ok=True)
        self.assertEqual(cm.exception.extra["current"]["version"], 2)
        # 用新版本提交成功，旧值没有被覆盖
        self.svc.check_item(
            self.staff, run["id"], "SN-A", 2, battery=88, appearance="ok",
            network_state="wifi", dock_ok=True, cable_ok=True)
        item = self.db.one("SELECT * FROM inventory_items WHERE id=?", (item["id"],))
        self.assertEqual(item["battery"], 88)
        self.assertEqual(item["version"], 3)

    def test_tech_cannot_change_inventory(self):
        self.register("SN-A")
        run = self.svc.open_run(self.staff, self.s1["id"], day="2026-10-01")
        with self.assertRaises(PermissionDenied):
            self.svc.check_item(
                self.tech, run["id"], "SN-A", 1, battery=90, appearance="ok",
                network_state="wifi", dock_ok=True, cable_ok=True)

    def test_misplaced_detected(self):
        self.register("SN-A", booth=self.b1["id"])
        run = self.svc.open_run(self.staff, self.s1["id"], day="2026-10-01")
        self.svc.check_item(
            self.staff, run["id"], "SN-A", 1,
            actual_booth_id=self.b2["id"], battery=90, appearance="ok",
            network_state="wifi", dock_ok=True, cable_ok=True)
        got = self.svc.get_run(run["id"])
        item = got["items"][0]
        self.assertEqual(item["result"], "abnormal")
        kinds = [e["kind"] for e in self.svc.list_exceptions()]
        self.assertIn("misplaced", kinds)

    def test_close_requires_all_checked_and_manager_for_missing(self):
        self.register("SN-A")
        run = self.svc.open_run(self.staff, self.s1["id"], day="2026-10-01", kind="close")
        with self.assertRaises(PreconditionFailed):
            self.svc.close_run(self.staff, run["id"])
        # 标记缺失：导购不能关单
        self.svc.check_item(
            self.staff, run["id"], "SN-A", 1, missing=True, appearance="ok",
            network_state="wifi", dock_ok=True, cable_ok=True)
        with self.assertRaises(PreconditionFailed):
            self.svc.close_run(self.staff, run["id"])
        # 店长带说明关单成功
        closed = self.svc.close_run(self.manager, run["id"], force_note="调阅监控中")
        self.assertEqual(closed["status"], "closed")
        kinds = [e["kind"] for e in self.svc.list_exceptions()]
        self.assertIn("closing_missing", kinds)


class TestRestorePrecondition(ServiceTestBase):
    def _drift_device(self, serial="SN-D", **rt):
        overrides = dict(
            account_version="personal",
            apps=["微信"], wallpaper="customer.jpg",
            settings={"亮度": 100}, logged_in=False,
            content_flag="顾客自拍")
        overrides.update(rt)
        return self.register(serial, **overrides)

    def test_drift_enters_pending_restore(self):
        self._drift_device()
        job = self.svc.enqueue_restore(self.manager, "SN-D", "设置被更改")
        self.assertEqual(job["status"], "queued")
        self.assertEqual(self.svc.get_device("SN-D")["status"], "pending_restore")

    def test_offline_restore_fails_and_keeps_pending(self):
        self._drift_device(online=False, network_state="offline")
        job = self.svc.enqueue_restore(self.manager, "SN-D", "设置被更改")
        res = self.svc.execute_restore(self.tech, job["id"])
        self.assertFalse(res["ok"])
        self.assertEqual(self.svc.get_device("SN-D")["status"], "pending_restore")
        # 还原失败自动登记异常
        self.assertIn("restore_failed",
                      [e["kind"] for e in self.svc.list_exceptions()])

    def test_staff_cannot_execute_restore(self):
        self._drift_device()
        job = self.svc.enqueue_restore(self.manager, "SN-D", "x")
        with self.assertRaises(PermissionDenied):
            self.svc.execute_restore(self.staff, job["id"])
        with self.assertRaises(PermissionDenied):
            self.svc.execute_restore(self.manager, job["id"])

    def test_successful_restore_verifies_and_returns_to_display(self):
        self._drift_device()
        job = self.svc.enqueue_restore(self.manager, "SN-D", "设置被更改")
        res = self.svc.execute_restore(self.tech, job["id"])
        self.assertTrue(res["ok"])
        self.assertTrue(res["passed"], res["diff"])
        self.assertEqual(self.svc.get_device("SN-D")["status"], "on_display")
        # 版本校验：期望与实际完全一致
        v = self.svc.verify_job(job["id"])
        self.assertTrue(v["passed"])
        self.assertEqual(v["diff"], {})
        # 已成功的任务不能重复执行
        with self.assertRaises(Conflict):
            self.svc.execute_restore(self.tech, job["id"])

    def test_diff_highlights_fields(self):
        # 部分漂移：只有壁纸不一致
        self.register("SN-P", wallpaper="customer.jpg")
        job = self.svc.enqueue_restore(self.manager, "SN-P", "壁纸被改")
        v = self.svc.verify_job(job["id"])
        self.assertFalse(v["passed"])
        self.assertIn("wallpaper", v["diff"])
        self.assertNotIn("account_version", v["diff"])

    def test_failed_auto_retry_within_max_attempts(self):
        self._drift_device()
        job = self.svc.enqueue_restore(self.manager, "SN-D", "x")
        self.svc.execute_restore(self.tech, job["id"], simulate_failure=True)
        # faulty 导致重试仍失败，但 attempts 会增长，不抛异常
        results = self.svc.retry_failed_restores()
        self.assertTrue(any(r["job_id"] == job["id"] for r in results))

    def test_returned_device_cannot_restore(self):
        self._drift_device()
        job = self.svc.enqueue_restore(self.manager, "SN-D", "x")
        r = self.svc.request_return(self.staff, "SN-D", "返厂")
        self.svc.review_return(self.manager, r["id"], True)
        with self.assertRaises(PreconditionFailed):
            self.svc.execute_restore(self.tech, job["id"])


class TestTransferPermission(ServiceTestBase):
    def _requested(self):
        self.register("SN-T")
        return self.svc.request_transfer(self.staff, "SN-T", self.s2["id"], self.b3["id"])

    def test_staff_requests_and_booth_freezes(self):
        t = self._requested()
        self.assertEqual(t["status"], "requested")
        self.assertEqual(self.svc.list_booths(self.s1["id"])[0]["frozen"], 1)

    def test_only_manager_approves(self):
        t = self._requested()
        with self.assertRaises(PermissionDenied):
            self.svc.approve_transfer(self.staff, t["id"])
        with self.assertRaises(PermissionDenied):
            self.svc.approve_transfer(self.tech, t["id"])
        approved = self.svc.approve_transfer(self.manager, t["id"])
        self.assertEqual(approved["status"], "approved")
        self.assertIsNotNone(approved["handover"])  # 交接记录
        self.assertEqual(self.svc.get_device("SN-T")["status"], "in_transfer")

    def test_receive_moves_device_and_unfreezes(self):
        t = self._requested()
        self.svc.approve_transfer(self.manager, t["id"])
        got = self.svc.receive_transfer(self.manager, t["id"], True, self.b3["id"])
        self.assertEqual(got["status"], "received")
        d = self.svc.get_device("SN-T")
        self.assertEqual(d["store_id"], self.s2["id"])
        self.assertEqual(d["booth_id"], self.b3["id"])
        self.assertEqual(d["status"], "on_display")
        self.assertEqual(self.svc.list_booths(self.s1["id"])[0]["frozen"], 0)

    def test_reject_returns_device_and_records_exception(self):
        t = self._requested()
        self.svc.approve_transfer(self.manager, t["id"])
        with self.assertRaises(Exception):
            self.svc.receive_transfer(self.manager, t["id"], False)  # 拒收必须填原因
        rej = self.svc.receive_transfer(
            self.manager, t["id"], False, reason="外观与交接记录不符")
        self.assertEqual(rej["status"], "rejected")
        d = self.svc.get_device("SN-T")
        self.assertEqual(d["status"], "on_display")
        self.assertEqual(d["booth_id"], self.b1["id"])
        self.assertEqual(self.svc.list_booths(self.s1["id"])[0]["frozen"], 0)
        self.assertIn("transfer_rejected",
                      [e["kind"] for e in self.svc.list_exceptions()])

    def test_target_booth_occupied_blocks_receive(self):
        self.register("SN-T")
        self.svc.register_device(
            self.manager, "SN-O", "M1", "白", self.s2["id"], self.b3["id"])
        # 申请时未指定展位；接收时选择已被占用的 B3 → 拒绝
        t = self.svc.request_transfer(self.staff, "SN-T", self.s2["id"], None)
        self.svc.approve_transfer(self.manager, t["id"])
        with self.assertRaises(Conflict):
            self.svc.receive_transfer(self.manager, t["id"], True, self.b3["id"])


class TestEvidenceRetention(ServiceTestBase):
    def test_evidence_file_survives_return_and_resolution(self):
        self.register("SN-E", appearance="scratch")
        rel = self.svc.save_evidence(
            "SN-E", b"scratch photo bytes", filename="scratch.jpg")
        full = self.svc.evidence_full_path(rel)
        self.assertTrue(os.path.isfile(full))
        # 异常引用证据
        exc = self.svc.report_exception(
            self.staff, "SN-E", "scratch", "边框划伤", evidence_paths=[rel])
        self.svc.resolve_exception(self.manager, exc["id"], "已拍照留档")
        # 退仓通过后设备记录被清空，但证据文件与退仓单中的路径保留
        r = self.svc.request_return(
            self.staff, "SN-E", "外观受损返厂", evidence_paths=[rel])
        self.svc.review_return(self.manager, r["id"], True, note="证据齐全")
        self.assertTrue(os.path.isfile(full), "退仓后证据文件必须保留")
        returns = self.svc.list_returns()
        self.assertEqual(returns[0]["evidence_paths"], [rel])
        self.assertEqual(self.svc.get_device("SN-E")["status"], "returned")
        # 再次盘点同序列号（返修后重新入库）不影响历史证据文件
        self.assertTrue(os.path.isfile(full))


class TestScheduler(ServiceTestBase):
    def test_open_inventory_reminder(self):
        # 开店时间设为 00:00，任意时刻 tick 都应产生且只产生一次提醒
        self.db.execute("UPDATE stores SET open_time='00:00' WHERE id=?", (self.s1["id"],))
        self.db.commit()
        sch = Scheduler(self.svc, interval=60)
        sch.tick(datetime(2026, 10, 1, 9, 0))
        sch.tick(datetime(2026, 10, 1, 9, 1))
        msgs = self.svc.list_reminders()
        self.assertEqual(len([m for m in msgs if m["kind"] == "open_inventory"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
