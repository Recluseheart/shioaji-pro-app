#!/usr/bin/env python3
"""Start an isolated Gateway + Web dev app; default PAPER never uses a sidecar."""

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("start", "status"), nargs="?", default="start"
    )
    parser.add_argument("--mode", choices=("paper", "observe", "sim"), default="paper")
    parser.add_argument("--web-port", type=int, default=4174)
    parser.add_argument(
        "--strategy",
        default="price-threshold",
        help="price-threshold, or the id of a private strategy in --private-strategy-dir",
    )
    parser.add_argument(
        "--private-strategy-dir",
        help="directory outside every git work tree holding private strategy modules",
    )
    parser.add_argument(
        "--cb-symbols", default="", help="comma-separated convertible bond codes allowed to trade"
    )
    args = parser.parse_args()
    if args.strategy != "price-threshold" and not args.private_strategy_dir:
        parser.error("--private-strategy-dir is required for a private strategy")
    if args.command == "status":
        for port, path in (
            (18082, "/api/v1/autotrade/runtime"),
            (18082, "/api/v1/autotrade/health"),
            (args.web_port, "/"),
        ):
            try:
                with urlopen(f"http://127.0.0.1:{port}{path}", timeout=2) as response:
                    print(
                        f"{port}{path}: {response.read().decode() if path != '/' else response.status}"
                    )
            except OSError:
                print(f"{port}{path}: unavailable")
        return
    for port in (18082, args.web_port):
        with socket.socket() as probe:
            # Like the servers themselves, ignore TIME_WAIT left by a stack
            # that just stopped; a live listener still refuses the bind.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                parser.error(
                    f"port {port} is occupied; leave its owner untouched and stop it explicitly if appropriate"
                )
    python = ROOT / ".venv/bin/python"
    node = shutil.which("node")
    homebrew_node = Path("/opt/homebrew/opt/node@22/bin/node")
    if homebrew_node.exists():
        node = str(homebrew_node)
    if (
        not python.exists()
        or not node
        or not (ROOT / "node_modules/vite/bin/vite.js").exists()
    ):
        parser.error(
            "run uv sync --frozen --group dev and pnpm install --frozen-lockfile first (Node 22+)"
        )
    # Do not inherit credentials, DSNs, old fixture switches, production flags,
    # or account selection into this isolated preview. No .env is loaded here.
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "HOME", "TMPDIR", "LANG", "USER", "SHELL", "SYSTEMROOT"}
    }
    directory = ROOT / ".runtime" / args.mode
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    environment.update(
        AUTOTRADE_RUNTIME_MODE=args.mode.upper(),
        AUTOTRADE_GATEWAY_MODE="OPERATOR",
        AUTOTRADE_GATEWAY_DB=str(directory / "journal.db"),
        PORT=str(args.web_port),
        SHIOAJI_HTTP_ORIGIN="http://127.0.0.1:21322",
        PYTHONUNBUFFERED="1",
        AUTOTRADE_STRATEGY=args.strategy,
        AUTOTRADE_CB_SYMBOLS=args.cb_symbols,
    )
    if args.private_strategy_dir:
        environment["AUTOTRADE_PRIVATE_STRATEGY_DIR"] = str(
            Path(args.private_strategy_dir).expanduser().resolve()
        )
    # SIM starts with execution disabled and cannot silently inherit an opt-in.
    # Explicit broker execution uses the documented operator service setup.
    children = []

    def stop(_signal=None, _frame=None):
        for child in children:
            if child.poll() is None:
                child.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        children.append(
            subprocess.Popen(
                [str(python), "-m", "services.autotrade_gateway.server"],
                cwd=ROOT,
                env=environment,
            )
        )
        for _ in range(50):
            if children[0].poll() is not None:
                raise RuntimeError("Gateway failed to start; see error above")
            try:
                with urlopen(
                    "http://127.0.0.1:18082/api/v1/autotrade/runtime", timeout=1
                ) as response:
                    if json.load(response)["mode"] == args.mode.upper():
                        break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("Gateway readiness timed out")
        children.append(
            subprocess.Popen(
                [node, "node_modules/vite/bin/vite.js", "--strictPort"],
                cwd=ROOT,
                env=environment,
            )
        )
        print(
            f"Autotrade {args.mode.upper()} · http://127.0.0.1:{args.web_port} · Gateway 18082 · DISARMED",
            flush=True,
        )
        while all(child.poll() is None for child in children):
            time.sleep(0.5)
    finally:
        stop()
        for child in children:
            try:
                child.wait(timeout=35)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    main()
