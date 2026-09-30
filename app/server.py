"""HTTP 服务：纯标准库 http.server 实现的 JSON API + 静态文件服务。

鉴权：请求头 X-User-Id（演示用身份切换），服务端按角色鉴权。
"""
import json
import os
import re
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .services import ServiceError

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "static")


class Router:
    def __init__(self):
        self.routes = []

    def add(self, method, pattern, handler, auth=True):
        self.routes.append((method, re.compile(f"^{pattern}$"), handler, auth))

    def match(self, method, path):
        for m, rx, h, auth in self.routes:
            if m != method:
                continue
            mo = rx.match(path)
            if mo:
                return h, auth, mo.groups()
        return None, None, None


def build_router(svc, tasks):
    r = Router()

    def GET(p, h, auth=True): r.add("GET", p, h, auth)
    def POST(p, h, auth=True): r.add("POST", p, h, auth)
    # 所有处理器签名统一为 (actor, *path_groups, params)
    # GET → params 为查询字符串 dict；POST → params 为 JSON body

    GET(r"/api/bootstrap", lambda a, _p: {
        "stores": svc.db.query("SELECT * FROM stores"),
        "booths": svc.db.query("SELECT * FROM booths ORDER BY store_id,grid_y,grid_x"),
        "users": svc.db.query("SELECT id,name,role,store_id FROM users"),
        "tasks": tasks.status() if tasks else None,
    }, auth=False)
    GET(r"/api/stores/(\d+)/plan", lambda a, sid, _p: svc.plan(int(sid)))
    GET(r"/api/devices/(\d+)", lambda a, did, _p: svc.device_detail(int(did)))
    GET(r"/api/devices/(\d+)/verify", lambda a, did, _p: svc.verify_device(int(did)))
    POST(r"/api/devices", lambda a, b: svc.register_device(a, b))
    POST(r"/api/devices/(\d+)/relocate",
         lambda a, did, b: svc.relocate_device(a, int(did), b["booth_id"], b["version"]))

    POST(r"/api/inventories", lambda a, b: svc.start_inventory(
        a, b["store_id"], b["type"], b.get("biz_date")))
    GET(r"/api/inventories/(\d+)", lambda a, iid, _p: svc.get_inventory(int(iid)))
    GET(r"/api/inventories", lambda a, _p: svc.db.query(
        "SELECT * FROM inventories ORDER BY id DESC LIMIT 20"))
    POST(r"/api/inventories/(\d+)/items/(\d+)",
         lambda a, iid, did, b: svc.submit_item(a, int(iid), int(did), b))
    POST(r"/api/inventories/(\d+)/complete",
         lambda a, iid, b: svc.complete_inventory(a, int(iid)))

    GET(r"/api/anomalies", lambda a, q: svc.list_anomalies(
        store_id=int(q["store_id"]) if q.get("store_id") else None,
        status=q.get("status")))
    POST(r"/api/anomalies", lambda a, b: svc.report_anomaly(
        a, b["device_id"], b["type"], b.get("detail", "")))
    POST(r"/api/anomalies/(\d+)/evidence",
         lambda a, aid, b: svc.add_evidence(a, int(aid), b["filename"], b["data_b64"]))
    POST(r"/api/anomalies/(\d+)/resolve",
         lambda a, aid, b: svc.resolve_anomaly(
             a, int(aid), b.get("resolution", ""), b.get("payload")))
    POST(r"/api/evidence/(\d+)/delete",
         lambda a, eid, b: svc.delete_evidence(a, int(eid)))

    GET(r"/api/restore-queue", lambda a, _p: svc.restore_queue())
    POST(r"/api/restore-tasks/(\d+)/execute",
         lambda a, tid, b: svc.execute_restore(a, int(tid)))

    GET(r"/api/transfers", lambda a, _p: svc.list_transfers())
    POST(r"/api/transfers", lambda a, b: svc.create_transfer(
        a, b["device_id"], b["to_store_id"], b["to_booth_id"]))
    POST(r"/api/transfers/(\d+)/approve", lambda a, tid, b: svc.approve_transfer(a, int(tid)))
    POST(r"/api/transfers/(\d+)/receive", lambda a, tid, b: svc.receive_transfer(a, int(tid)))
    POST(r"/api/transfers/(\d+)/reject",
         lambda a, tid, b: svc.reject_transfer(a, int(tid), b.get("reason", "")))

    GET(r"/api/returns", lambda a, _p: svc.list_returns())
    POST(r"/api/returns", lambda a, b: svc.create_return(a, b["device_id"], b.get("reason", "")))
    POST(r"/api/returns/(\d+)/review",
         lambda a, rid, b: svc.review_return(a, int(rid), b.get("checklist", {}),
                                             b.get("approve", False)))

    GET(r"/api/notifications", lambda a, _p: svc.notifications())
    GET(r"/api/audit", lambda a, _p: svc.db.query(
        "SELECT * FROM audit_log ORDER BY id DESC LIMIT 50"))
    GET(r"/api/devices", lambda a, _p: svc.db.query(
        "SELECT d.*, b.code AS booth_code FROM devices d"
        " LEFT JOIN booths b ON b.id=d.booth_id WHERE d.status != 'returned'"
        " ORDER BY d.store_id, b.grid_y, b.grid_x"))
    return r


def make_handler(router, svc):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass  # 静默

        # ---------- 工具 ----------
        def _send(self, status, obj, ctype="application/json; charset=utf-8"):
            body = obj if isinstance(obj, bytes) else json.dumps(
                obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status, code, message):
            self._send(status, {"error": {"code": code, "message": message}})

        def _actor(self):
            uid = self.headers.get("X-User-Id")
            if not uid:
                raise ServiceError(401, "unauthorized", "缺少 X-User-Id 请求头")
            return svc.get_user(int(uid))

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8") or "{}")

        def _static(self, path):
            if path == "/":
                path = "/index.html"
            safe = os.path.normpath(path).lstrip("/")
            full = os.path.join(STATIC_DIR, safe)
            if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
                self._error(404, "not_found", "页面不存在")
                return
            ctype = {".html": "text/html; charset=utf-8",
                     ".css": "text/css; charset=utf-8",
                     ".js": "application/javascript; charset=utf-8",
                     ".svg": "image/svg+xml"}.get(
                         os.path.splitext(full)[1], "application/octet-stream")
            with open(full, "rb") as f:
                self._send(200, f.read(), ctype)

        # ---------- 分发 ----------
        def _dispatch(self, method):
            path = self.path.split("?")[0]
            if not path.startswith("/api/"):
                if method == "GET":
                    return self._static(path)
                return self._error(405, "method", "方法不允许")
            handler, need_auth, groups = router.match(method, path)
            if not handler:
                return self._error(404, "not_found", "接口不存在")
            try:
                actor = self._actor() if need_auth else None
                if method == "POST":
                    params = self._body()
                else:
                    from urllib.parse import urlparse, parse_qs
                    qs = parse_qs(urlparse(self.path).query)
                    params = {k: v[0] for k, v in qs.items()}
                result = handler(actor, *groups, params)
                self._send(200, result)
            except ServiceError as e:
                self._error(e.status, e.code, e.message)
            except (KeyError, ValueError, TypeError) as e:
                self._error(400, "bad_request", f"请求参数错误：{e}")
            except Exception as e:
                traceback.print_exc()
                self._error(500, "internal", f"服务器内部错误：{e}")

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

    return Handler


def serve(svc, tasks, host="127.0.0.1", port=8000):
    router = build_router(svc, tasks)
    httpd = ThreadingHTTPServer((host, port), make_handler(router, svc))
    print(f"演示机盘点与系统还原平台已启动： http://{host}:{port}")
    print("按 Ctrl+C 停止。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
