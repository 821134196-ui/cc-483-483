"""HTTP 端到端测试：会话、角色门禁、盘点冲突、还原、调店全流程。"""
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from http.cookiejar import CookieJar

from app.server import AppContext, make_handler
from http.server import ThreadingHTTPServer

from app.seed import seed


class Client:
    def __init__(self, base):
        self.base = base
        self.cj = CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cj))

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(req) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def login(self, pin):
        return self.call("POST", "/api/login", {"pin": pin})


class HttpIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls.tmp.name, "demo.db")
        seed(cls.db_path, cls.tmp.name, reset=True)
        cls.ctx = AppContext(cls.db_path, cls.tmp.name)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.ctx))
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.tmp.cleanup()

    def setUp(self):
        self.staff = Client(self.base)
        self.manager = Client(self.base)
        self.tech = Client(self.base)
        self.anon = Client(self.base)
        self.staff.login("1001")
        self.manager.login("2002")
        self.tech.login("3003")

    def test_01_login_and_session(self):
        st, body = self.anon.call("GET", "/api/me")
        self.assertIsNone(body["user"])
        st, body = self.staff.call("GET", "/api/me")
        self.assertEqual(body["user"]["role"], "staff")
        st, body = self.anon.call("POST", "/api/runs/open", {"store_id": 1})
        self.assertEqual(st, 401)
        st, body = self.staff.call("POST", "/api/login", {"pin": "0000"})
        self.assertEqual(st, 401)

    def test_02_role_gating(self):
        # 导购不能执行还原
        st, _ = self.staff.call("POST", "/api/restores/1/execute", {})
        self.assertEqual(st, 403)
        # 技术不能改盘点结论（需要一个 open run）
        _, runs = self.manager.call("GET", "/api/runs?store_id=1")
        run_id = None
        for r in runs["runs"]:
            if r["kind"] == "open" and r["status"] == "open":
                run_id = r["id"]
        if run_id is None:
            _, body = self.staff.call("POST", "/api/runs/open", {"store_id": 1, "kind": "open"})
            run_id = body["run"]["id"]
        st, body = self.tech.call("POST", f"/api/runs/{run_id}/check", {
            "serial": "SN-AX12-0001", "base_version": 1, "battery": 50,
            "appearance": "ok", "network_state": "wifi",
            "dock_ok": True, "cable_ok": True})
        self.assertEqual(st, 403)
        # 导购不能批准调店
        _, ts = self.staff.call("GET", "/api/transfers")
        pending = next((t for t in ts["transfers"] if t["status"] == "requested"), None)
        if pending:
            st, _ = self.staff.call("POST", f"/api/transfers/{pending['id']}/approve", {})
            self.assertEqual(st, 403)

    def test_03_full_restore_flow(self):
        # SN-AX12P-002 预置在还原队列；技术执行 → 成功并校验通过 → 回到可展示
        _, jobs = self.tech.call("GET", "/api/restores?status=queued")
        job = next(j for j in jobs["jobs"] if j["serial"] == "SN-AX12P-002")
        st, body = self.tech.call("POST", f"/api/restores/{job['id']}/execute", {})
        self.assertEqual(st, 200)
        self.assertTrue(body["result"]["passed"])
        _, dev = self.tech.call("GET", "/api/devices/SN-AX12P-002")
        self.assertEqual(dev["device"]["status"], "on_display")
        st, vbody = self.tech.call("GET", f"/api/restores/{job['id']}/verify")
        self.assertTrue(vbody["verify"]["passed"])

    def test_04_restore_failure_and_retry(self):
        # 离线设备加入还原 → 执行失败 → 任务 failed，设备保持待还原
        serial = "SN-AX12P-001"
        st, body = self.manager.call("POST", "/api/restores/enqueue",
                                     {"serial": serial, "reason": "测试离线失败"})
        self.assertEqual(st, 201)
        job_id = body["job"]["id"]
        st, body = self.tech.call("POST", f"/api/restores/{job_id}/execute", {})
        self.assertFalse(body["result"]["ok"])
        _, j = self.tech.call("GET", "/api/restores?status=failed")
        self.assertTrue(any(x["id"] == job_id for x in j["jobs"]))
        _, dev = self.tech.call("GET", f"/api/devices/{serial}")
        self.assertEqual(dev["device"]["status"], "pending_restore")

    def test_05_concurrent_inventory_conflict(self):
        # 开一张新的当日盘点单（此前可能已被 02 部分使用，这里取同一单）
        _, runs = self.staff.call("GET", "/api/runs?store_id=1")
        run = next(r for r in runs["runs"] if r["kind"] == "open")
        item = next(i for i in run["items"] if i["serial"] == "SN-AX12-0002")
        base = item["version"]
        st, body = self.staff.call("POST", f"/api/runs/{run['id']}/check", {
            "serial": "SN-AX12-0002", "base_version": base, "battery": 71,
            "appearance": "ok", "network_state": "wifi",
            "dock_ok": True, "cable_ok": True})
        self.assertEqual(st, 200)
        # 导购乙（同一角色新会话）拿旧版本提交 → 409 并返回当前记录
        staff2 = Client(self.base)
        staff2.login("1001")
        st, body = staff2.call("POST", f"/api/runs/{run['id']}/check", {
            "serial": "SN-AX12-0002", "base_version": base, "battery": 9,
            "appearance": "ok", "network_state": "wifi",
            "dock_ok": True, "cable_ok": True})
        self.assertEqual(st, 409)
        self.assertEqual(body["current"]["battery"], 71)

    def test_06_transfer_receive_flow(self):
        # 预置调店单 SN-NF-0007 已批准待接收；二号店店长接收至 E1
        _, ts = self.manager.call("GET", "/api/transfers?status=approved")
        t = next(x for x in ts["transfers"] if x["serial"] == "SN-NF-0007")
        _, booths = self.manager.call("GET", "/api/booths?store_id=2")
        e1 = next(b for b in booths["booths"] if b["code"] == "E1")
        st, body = self.manager.call("POST", f"/api/transfers/{t['id']}/receive", {
            "accepted": True, "to_booth_id": e1["id"]})
        self.assertEqual(st, 200)
        self.assertEqual(body["transfer"]["status"], "received")
        _, dev = self.manager.call("GET", "/api/devices/SN-NF-0007")
        self.assertEqual(dev["device"]["store_id"], 2)
        self.assertEqual(dev["device"]["booth_id"], e1["id"])

    def test_07_evidence_uploaded_and_served(self):
        rel = None
        st, body = self.staff.call("POST", "/api/devices/SN-AX12-0001/evidence", {
            "filename": "note.txt", "content": "顾客试用后底座报警复核记录"})
        self.assertEqual(st, 201)
        rel = body["evidence_path"]
        with urllib.request.urlopen(self.base + "/" + rel) as r:
            text = r.read().decode()
        self.assertIn("底座报警", text)

    def test_08_static_index(self):
        with urllib.request.urlopen(self.base + "/") as r:
            html = r.read().decode()
        self.assertIn("演示机盘点", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
