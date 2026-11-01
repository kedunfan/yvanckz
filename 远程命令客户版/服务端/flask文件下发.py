#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
远程命令服务端 - Flask 文件下发（token 验证 + 待发文件夹队列逐个下发）

目录结构（都在本文件所在目录）：
  待发/          放入要下发的 bat（队列：服务端一个一个按顺序下发，发完即移走）
  下载过/        已被下发过的 bat 归档（仅作记录，不再重复下发）
  下载记录.json  各客户端当前执行记录（重启不丢失）

接口（除 /health 外均需 token：?token= 或请求头 X-Token）：
  GET /download?client=<客户端ID>
      200 返回 bat  → 待发/ 里有文件，取队头（最旧）一个下发并移入 下载过/
      204           → 待发/ 为空，没有要下发的文件
  GET /delete?name=run.bat   从 待发/ 和 下载过/ 中删除指定 bat
  GET /status                查看文件与各客户端下载情况
  GET /health                健康检查（无需 token）

工作流：
  1. 下发命令：把 bat 放进 待发/，服务端每轮询一次发一个（按修改时间从旧到新）
  2. 重新下发：把 bat 再放进 待发/ 即当作新文件，会一个个发出
  3. 停止下发：删掉 待发/ 中的文件即可；之后返回 204 无内容
"""
import argparse
import hashlib
import hmac
import json
import re
import shutil
import threading
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file

# ================== 配置 ==================
TOKEN = "change-me-please"                    # 必须与 客户端/httpbat.py 中的 TOKEN 一致
BASE_DIR = Path(__file__).parent
PENDING_DIR = BASE_DIR / "待发"               # 待下发
SENT_DIR = BASE_DIR / "下载过"                # 已下发归档
RECORD_FILE = BASE_DIR / "下载记录.json"      # 各客户端下载记录
# ==========================================

app = Flask(__name__)
_records = {}     # client_id -> {"current": md5|None, "current_name": str|None, "history": [md5,...]}
_lock = threading.Lock()

PENDING_DIR.mkdir(parents=True, exist_ok=True)
SENT_DIR.mkdir(parents=True, exist_ok=True)


# ---------- 下载记录读写 ----------
def load_records():
    global _records
    if RECORD_FILE.exists():
        try:
            _records = json.loads(RECORD_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            _records = {}


def save_records():
    try:
        RECORD_FILE.write_text(
            json.dumps(_records, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


# ---------- 工具 ----------
def file_hash(path: Path) -> str:
    try:
        return hashlib.md5(path.read_bytes()).hexdigest()
    except OSError:      # 文件正好被删除等竞态
        return ""


def pending_bats() -> list:
    """待发/ 中的全部 bat，按修改时间从旧到新排序（队头先发，保证一个一个按顺序）"""
    files = [p for p in PENDING_DIR.glob("*")
             if p.is_file() and p.suffix.lower() == ".bat"]
    return sorted(files, key=lambda p: p.stat().st_mtime)


def list_bats(d: Path) -> list:
    return sorted(p.name for p in d.glob("*")
                  if p.is_file() and p.suffix.lower() == ".bat")


def check_token() -> bool:
    """校验 token（常数时间比较，防时序攻击）"""
    token = request.args.get("token") or request.headers.get("X-Token", "")
    return hmac.compare_digest(token, TOKEN)


@app.before_request
def require_token():
    """除 /health 外所有接口都需要 token 验证"""
    if request.path == "/health":
        return None
    if not check_token():
        return jsonify({"error": "unauthorized", "msg": "token 验证失败"}), 401


# ---------- 核心接口 ----------
@app.route("/download")
def download():
    """
    待发文件夹队列式下发：
      - 待发/ 里有 bat           → 取队头（最旧）一个，移入 下载过/ 并 200 下发
                                   客户端每轮询一次拿到一个，实现“一个一个发”
      - 待发/ 为空               → 204 无内容，无需下发
    """
    client = (request.args.get("client") or request.remote_addr or "unknown").strip()[:64]
    with _lock:
        pending = pending_bats()

        # 待发/ 没有东西 → 直接返回 204
        if not pending:
            return "", 204

        # 有东西 → 取一个（最旧的先发）下发，并移入 下载过/
        src = pending[0]
        dest = SENT_DIR / src.name
        try:
            if dest.exists():
                dest.unlink()
            shutil.move(str(src), str(dest))
            send_path = dest
        except OSError:
            send_path = src                       # 移动失败就原地下发

        h = file_hash(send_path)
        rec = _records.setdefault(
            client, {"current": None, "current_name": None, "history": []})
        rec["current"] = h
        rec["current_name"] = send_path.name
        save_records()
        return send_file(send_path, as_attachment=True,
                         download_name=send_path.name)


@app.route("/delete", methods=["GET", "POST"])
def delete():
    """从 待发/ 和 下载过/ 中删除 bat；客户端轮询到 404 后自动停止对应进程"""
    name = request.args.get("name") or ""
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+", name):
        abort(400, description="非法文件名")
    deleted = []
    for d in (PENDING_DIR, SENT_DIR):
        p = d / name
        if p.is_file():
            try:
                p.unlink()
                deleted.append(str(p))
            except OSError as e:
                return jsonify({"error": str(e)}), 500
    if not deleted:
        abort(404, description="文件不存在")
    return jsonify({"msg": f"已删除 {name}", "files": deleted})


@app.route("/status")
def status():
    """查看当前文件与各客户端下载情况"""
    with _lock:
        clients = {c: {"正在执行": r.get("current_name") or "（无）",
                       "已下载内容数": len(r.get("history", []))}
                   for c, r in _records.items()}
    return jsonify({
        "待发": list_bats(PENDING_DIR),
        "下载过": list_bats(SENT_DIR),
        "客户端": clients,
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


load_records()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="远程命令服务端 - 文件下发")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0）")
    parser.add_argument("--port", type=int, default=8000, help="监听端口（默认 8000）")
    parser.add_argument("--token", help="访问 token（默认用文件内 TOKEN）")
    args = parser.parse_args()

    if args.token:
        TOKEN = args.token

    print(f"[服务端] 待发目录: {PENDING_DIR.resolve()}")
    print(f"[服务端] 下载过目录: {SENT_DIR.resolve()}")
    print(f"[服务端] 下载记录: {RECORD_FILE.resolve()}")
    print(f"[服务端] 监听: http://{args.host}:{args.port}")
    if TOKEN == "change-me-please":
        print("[警告] 正在使用默认 TOKEN，请尽快修改（客户端需保持一致）！")
    app.run(host=args.host, port=args.port, threaded=True)
