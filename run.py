#!/usr/bin/env python3
"""一条本地启动命令：

    python3 run.py            # 初始化数据 + 生成样例 + 启动服务（默认 8000 端口）
    PORT=8080 python3 run.py  # 指定端口
    python3 run.py --reseed   # 清空数据并重新生成样例

自动生成：SQLite 库、外观证据目录、离线配置包、
离线/错位/调店/待还原/失败重试样例，以及进程内盘点提醒任务。
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.db import Database
from app.adapters import build_adapters
from app.services import Service
from app.seed import seed_if_empty
from app.tasks import TaskRunner
from app.server import serve

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE, "data"))
PORT = int(os.environ.get("PORT", "8000"))


def main():
    if "--reseed" in sys.argv and os.path.exists(DATA_DIR):
        shutil.rmtree(DATA_DIR)
        print("已清空旧数据。")
    os.makedirs(DATA_DIR, exist_ok=True)

    db = Database(os.path.join(DATA_DIR, "app.db"))
    mdm, dock = build_adapters(DATA_DIR)
    svc = Service(db, mdm, dock, DATA_DIR)

    if seed_if_empty(svc):
        print("已生成样例数据：门店/展台/设备/演示账号（均为样例），")
        print("  含 离线设备、展位错放、调店在途、待还原与失败重试样例。")

    tasks = TaskRunner(svc, interval=int(os.environ.get("TASK_INTERVAL", "20")))
    tasks.start()
    try:
        serve(svc, tasks, host=os.environ.get("HOST", "127.0.0.1"), port=PORT)
    finally:
        tasks.stop()


if __name__ == "__main__":
    main()
