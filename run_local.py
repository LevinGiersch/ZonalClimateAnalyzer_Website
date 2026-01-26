#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import socket
import signal
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"


def _check_cmd(name: str) -> None:
    if shutil.which(name) is None:
        raise SystemExit(f"Missing required command: {name}")


def _run(cmd: list[str], cwd: Path) -> subprocess.Popen:
    return subprocess.Popen(cmd, cwd=str(cwd))

def _wait_for_port(port: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.25)
    return False


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _pids_on_port(port: int) -> list[int]:
    try:
        result = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}"],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return []
    pids = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return pids


def _stop_ports(ports: list[int]) -> int:
    stopped = 0
    for port in ports:
        for pid in _pids_on_port(port):
            try:
                subprocess.run(["kill", str(pid)], check=False)
                stopped += 1
            except Exception:
                continue
    return stopped

def main() -> int:
    parser = argparse.ArgumentParser(description="Run ZonalClimateAnalyzer locally.")
    parser.add_argument(
        "command",
        nargs="?",
        default="start",
        choices=["start", "stop"],
        help="start (default) or stop local dev servers",
    )
    args = parser.parse_args()

    if args.command == "stop":
        stopped = _stop_ports([8000, 5173])
        if stopped:
            print(f"Stopped {stopped} process(es) on ports 8000/5173.")
        else:
            print("No processes found on ports 8000/5173.")
        return 0

    _check_cmd(sys.executable)
    _check_cmd("node")
    _check_cmd("npm")

    api = None
    if _port_open(8000):
        print("API already running on http://localhost:8000")
    else:
        print("Starting API on http://localhost:8000 ...")
        api = _run(
            [sys.executable, "-m", "uvicorn", "api.server:app", "--reload", "--port", "8000"],
            ROOT,
        )

    try:
        if api and not _wait_for_port(8000, timeout=12.0):
            print("Warning: API did not start within 12s. Check the API logs above.")

        vite_bin = WEB_DIR / "node_modules" / ".bin" / "vite"
        if not vite_bin.exists():
            print("Installing web dependencies...")
            subprocess.run(["npm", "install"], cwd=str(WEB_DIR), check=True)
        print("Starting web app on http://localhost:5173 ...")
        web = _run(["npm", "run", "dev", "--", "--host", "127.0.0.1"], WEB_DIR)

        def _shutdown(*_args) -> None:
            for proc in (web, api):
                try:
                    proc.terminate()
                except Exception:
                    pass

        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)

        web.wait()
        return web.returncode or 0
    finally:
        if api:
            try:
                api.terminate()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
