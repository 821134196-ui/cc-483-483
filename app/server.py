"""HTTP 服务：JSON API + 静态前端。仅用标准库。"""
import json
import mimetypes
import os
import secrets
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .adapters import SecurityDockAdapter
from .db import DB
from .errors import AppError
from .scheduler import Scheduler
from .seed import seed
from .services import StoreService

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


class AppContext:
    def __init__(self, db_path="data/demo.db", data_dir="data"):
        self.db = DB(db_path)
        self.evidence_dir = os.path.join(data_dir, "evidence")
        self.package_dir = os.path.join(data_dir, "packages")
        self.svc = StoreService(self.db, self.evidence_dir, self.package_dir)
        self.sessions = {}
        self.scheduler = Scheduler(self.svc, interval=30)

    def user_from_cookie(self, cookie_header):
        if not cookie_header:
            return None
        jar = SimpleCookie()
        jar.load(cookie_header)
        sid = jar.get("sid")
        if not sid:
            return None
        uid = self.sessions.get(sid.value)
        if uid is None:
            return None
        row = self.db.one("SELECT * FROM users WHERE id=?", (uid,))
        return dict(row) if row else None


def make_handler(ctx: AppContext):
    svc = ctx.svc
    db = ctx.db

    class Handler(BaseHTTPRequestHandler):
        server_version = "DemoInventory/1.0"

        def log_message(self, fmt, *args):
            pass

        # ---------- 基础工具 ----------
        def _send_json(self, obj, status=200, extra_headers=()):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for k, v in extra_headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            raw = self.rfile.read(n)
            try:
                return json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise AppError("请求体不是合法 JSON", status=400)

        def _current_user(self):
            return ctx.user_from_cookie(self.headers.get("Cookie"))

        def _error(self, e: AppError):
            self._send_json({"error": str(e), **e.extra}, status=e.status)

        def _serve_file(self, path):
            if not os.path.isfile(path):
                self.send_error(404)
                return
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            with open(path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # ---------- 路由 ----------
        def do_GET(self):
            try:
                parsed = urlparse(self.path)
                path = parsed.path
                qs = {}
                if parsed.query:
                    for kv in parsed.query.split("&"):
                        if "=" in kv:
                            k, v = kv.split("=", 1)
                            qs[k] = v
                # 静态资源
                if path.startswith("/evidence/"):
                    rel = os.path.normpath(path.lstrip("/"))
                    full = os.path.join(os.path.dirname(ctx.evidence_dir), rel)
                    if not os.path.abspath(full).startswith(os.path.abspath(
                            os.path.dirname(ctx.evidence_dir))):
                        return self.send_error(403)
                    return self._serve_file(full)
                if path == "/" or path == "/index.html":
                    return self._serve_file(os.path.join(STATIC_DIR, "index.html"))
                if path.startswith("/static/"):
                    rel = os.path.normpath(path[len("/static/"):])
                    full = os.path.join(STATIC_DIR, rel)
                    if not os.path.abspath(full).startswith(STATIC_DIR):
                        return self.send_error(403)
                    return self._serve_file(full)
                self._api_get(path, qs)
            except AppError as e:
                self._error(e)
            except Exception as e:  # noqa: BLE001
                self._send_json({"error": f"服务器错误: {e}"}, status=500)

        def do_POST(self):
            self._mutate("POST")

        def do_PATCH(self):
            self._mutate("PATCH")

        def _mutate(self, method):
            try:
                parsed = urlparse(self.path)
                path = parsed.path
                body = self._read_json()
                self._api_write(method, path, body)
            except AppError as e:
                self._error(e)
            except Exception as e:  # noqa: BLE001
                self._send_json({"error": f"服务器错误: {e}"}, status=500)

        # ---------- GET API ----------
        def _api_get(self, path, qs):
            if path == "/api/me":
                u = self._current_user()
                return self._send_json({"user": self._user_public(u)})
            if path == "/api/stores":
                return self._send_json({"stores": svc.list_stores()})
            if path == "/api/booths":
                return self._send_json({"booths": svc.floor_plan(int(qs["store_id"]))})
            if path == "/api/devices":
                store_id = qs.get("store_id")
                return self._send_json(
                    {"devices": svc.list_devices(int(store_id) if store_id else None)})
            if path.startswith("/api/devices/"):
                return self._send_json({"device": svc.get_device(path.rsplit("/", 1)[1])})
            if path == "/api/runs":
                return self._send_json(
                    {"runs": svc.today_runs(int(qs["store_id"]), qs.get("day"))})
            if path.startswith("/api/runs/"):
                return self._send_json({"run": svc.get_run(int(path.rsplit("/", 1)[1]))})
            if path == "/api/exceptions":
                return self._send_json({"exceptions": svc.list_exceptions(
                    qs.get("status"), int(qs["store_id"]) if qs.get("store_id") else None)})
            if path == "/api/restores":
                return self._send_json({"jobs": svc.list_restore_jobs(qs.get("status"))})
            if path.startswith("/api/restores/") and path.endswith("/verify"):
                job_id = int(path.split("/")[-2])
                return self._send_json({"verify": svc.verify_job(job_id)})
            if path == "/api/transfers":
                return self._send_json({"transfers": svc.list_transfers(qs.get("status"))})
            if path.startswith("/api/transfers/"):
                return self._send_json(
                    {"transfer": svc.get_transfer(int(path.rsplit("/", 1)[1]))})
            if path == "/api/returns":
                return self._send_json({"returns": svc.list_returns(qs.get("status"))})
            if path == "/api/reminders":
                return self._send_json(
                    {"reminders": svc.list_reminders(qs.get("unread") == "1")})
            if path == "/api/packages":
                return self._send_json({"packages": svc.list_packages()})
            if path.startswith("/api/docks/"):
                code = path.rsplit("/", 1)[1]
                return self._send_json({"dock": SecurityDockAdapter(db).dock_status(code)})
            self.send_error(404)

        @staticmethod
        def _user_public(u):
            if not u:
                return None
            return {"id": u["id"], "name": u["name"], "role": u["role"]}

        # ---------- 写 API ----------
        def _api_write(self, method, path, body):
            u = self._current_user()

            if path == "/api/login":
                row = db.one("SELECT * FROM users WHERE pin=?", (str(body.get("pin", "")),))
                if row is None:
                    raise AppError("PIN 错误", status=401)
                sid = secrets.token_hex(16)
                ctx.sessions[sid] = row["id"]
                return self._send_json(
                    {"user": self._user_public(dict(row))},
                    extra_headers=[("Set-Cookie",
                                    f"sid={sid}; Path=/; HttpOnly; SameSite=Lax")])
            if path == "/api/logout":
                jar = SimpleCookie(self.headers.get("Cookie"))
                if "sid" in jar:
                    ctx.sessions.pop(jar["sid"].value, None)
                return self._send_json(
                    {"ok": True},
                    extra_headers=[("Set-Cookie", "sid=; Path=/; Max-Age=0")])

            if not u:
                raise AppError("请先登录", status=401)

            if path == "/api/devices/register":
                d = svc.register_device(
                    u, body["serial"], body["model"], body["color"],
                    body.get("store_id"), body.get("booth_id"),
                    body.get("dock_code"), body.get("cable_code"))
                return self._send_json({"device": d}, status=201)

            if path == "/api/runs/open":
                run = svc.open_run(u, int(body["store_id"]), body.get("day"),
                                   body.get("kind", "open"))
                return self._send_json({"run": run}, status=201)

            if path.startswith("/api/runs/") and path.endswith("/check"):
                run_id = int(path.split("/")[-2])
                run = svc.check_item(
                    u, run_id, body["serial"], int(body["base_version"]),
                    actual_booth_id=body.get("actual_booth_id"),
                    battery=body.get("battery"),
                    appearance=body.get("appearance"),
                    network_state=body.get("network_state"),
                    dock_ok=body.get("dock_ok"),
                    cable_ok=body.get("cable_ok"),
                    missing=body.get("missing", False),
                    note=body.get("note"),
                    evidence_path=body.get("evidence_path"))
                return self._send_json({"run": run})

            if path.startswith("/api/runs/") and path.endswith("/close"):
                run_id = int(path.split("/")[-2])
                run = svc.close_run(u, run_id, body.get("force_note"))
                return self._send_json({"run": run})

            if path == "/api/exceptions":
                e = svc.report_exception(
                    u, body.get("serial"), body["kind"], body["detail"],
                    body.get("evidence_paths"))
                return self._send_json({"exception": e}, status=201)

            if path.startswith("/api/exceptions/") and path.endswith("/resolve"):
                exc_id = int(path.split("/")[-2])
                e = svc.resolve_exception(u, exc_id, body.get("resolution", ""))
                return self._send_json({"exception": e})

            if path == "/api/restores/enqueue":
                job = svc.enqueue_restore(u, body["serial"], body.get("reason", "手动发起"))
                return self._send_json({"job": job}, status=201)

            if path.startswith("/api/restores/") and path.endswith("/execute"):
                job_id = int(path.split("/")[-2])
                res = svc.execute_restore(
                    u, job_id, simulate_failure=bool(body.get("simulate_failure")))
                return self._send_json({"result": res})

            if path.startswith("/api/restores/") and path.endswith("/retry"):
                job_id = int(path.split("/")[-2])
                # 单任务重试：直接把 attempts 复位后走自动重试入口
                db.execute("UPDATE restore_jobs SET attempts=0 WHERE id=? AND status='failed'",
                           (job_id,))
                db.commit()
                results = svc.retry_failed_restores()
                return self._send_json({"results": results})

            if path == "/api/packages":
                pkg = svc.build_offline_package(body["model"])
                return self._send_json({"package": pkg}, status=201)

            if path == "/api/transfers":
                t = svc.request_transfer(
                    u, body["serial"], int(body["to_store_id"]),
                    body.get("to_booth_id"), body.get("note", ""))
                return self._send_json({"transfer": t}, status=201)

            if path.startswith("/api/transfers/") and path.endswith("/approve"):
                t = svc.approve_transfer(u, int(path.split("/")[-2]))
                return self._send_json({"transfer": t})

            if path.startswith("/api/transfers/") and path.endswith("/receive"):
                t = svc.receive_transfer(
                    u, int(path.split("/")[-2]), bool(body["accepted"]),
                    body.get("to_booth_id"), body.get("reason", ""))
                return self._send_json({"transfer": t})

            if path.startswith("/api/transfers/") and path.endswith("/recall"):
                t = svc.recall_transfer(u, int(path.split("/")[-2]))
                return self._send_json({"transfer": t})

            if path == "/api/returns":
                r = svc.request_return(
                    u, body["serial"], body["reason"], body.get("evidence_paths"))
                return self._send_json({"return": r}, status=201)

            if path.startswith("/api/returns/") and path.endswith("/review"):
                r = svc.review_return(
                    u, int(path.split("/")[-2]), bool(body["approved"]), body.get("note", ""))
                return self._send_json({"return": r})

            if path.startswith("/api/reminders/") and path.endswith("/read"):
                svc.mark_reminder_read(int(path.split("/")[-2]))
                return self._send_json({"ok": True})

            if path.startswith("/api/devices/") and path.endswith("/evidence"):
                serial = path.split("/")[-2]
                fname = body.get("filename") or "evidence.bin"
                content = body.get("content_base64")
                if content:
                    import base64
                    raw = base64.b64decode(content)
                else:
                    raw = body.get("content", "").encode("utf-8")
                rel = svc.save_evidence(serial, raw, fname)
                return self._send_json({"evidence_path": rel}, status=201)

            self.send_error(404)

    return Handler


def run(host="127.0.0.1", port=8000, db_path="data/demo.db", data_dir="data",
        reset=False, with_scheduler=True):
    if reset or not os.path.exists(db_path):
        seed(db_path, data_dir, reset=reset)
    ctx = AppContext(db_path, data_dir)
    if with_scheduler:
        ctx.scheduler.start()
    httpd = ThreadingHTTPServer((host, port), make_handler(ctx))
    print(f"演示机盘点平台已启动：http://{host}:{port}", flush=True)
    print("登录 PIN：导购 1001 / 店长 2002 / 技术 3003", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        ctx.scheduler.stop()
        print("\n已停止")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="演示机盘点与系统还原平台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default="data/demo.db")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--reset", action="store_true", help="重置并生成离线/错位/调店样例")
    parser.add_argument("--no-scheduler", action="store_true")
    args = parser.parse_args()
    run(args.host, args.port, args.db, args.data_dir, args.reset,
        with_scheduler=not args.no_scheduler)
