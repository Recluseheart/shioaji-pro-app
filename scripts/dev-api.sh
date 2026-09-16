#!/bin/sh
# Dev API server — runs the BUNDLED sidecar binary（與 CI 打包同版本），
# so the UI always talks to a matching API version. Never touches the
# user's own CLI server on 8080.
#
#   port:    21322（app 預設），override with SJ_DEV_PORT
#   binary:  src-tauri/binaries/shioaji-<target-triple>（gitignored；
#            下載對應版本：github.com/sinotrade/shioaji releases）
#   keys:    專案根目錄 .env（SJ_API_KEY / SJ_SEC_KEY）

set -eu
cd "$(dirname "$0")/.."

# This launcher is solely for the local simulation sidecar.  A user may have
# production or CA variables in their login shell; never inherit them into a
# process launched for the Gateway's read-only development path.
case "${SJ_PRODUCTION:-}" in
    1|true|TRUE|yes|YES)
        echo "refusing to start dev api with SJ_PRODUCTION enabled" >&2
        exit 1
        ;;
esac
if [ -n "${SJ_CA_PATH:-}" ] || [ -n "${SJ_CA_PASSWD:-}" ]; then
    echo "refusing to start dev api with CA configuration" >&2
    exit 1
fi

case "$(uname -sm)" in
    "Darwin arm64") TRIPLE=aarch64-apple-darwin ;;
    "Darwin x86_64") TRIPLE=x86_64-apple-darwin ;;
    "Linux x86_64") TRIPLE=x86_64-unknown-linux-gnu ;;
    *) echo "unsupported platform: $(uname -sm)" >&2; exit 1 ;;
esac

BIN="src-tauri/binaries/shioaji-$TRIPLE"
if [ ! -x "$BIN" ]; then
    echo "missing $BIN — download the sidecar binary first:" >&2
    echo "  https://github.com/sinotrade/shioaji/releases" >&2
    exit 1
fi

PORT="${SJ_DEV_PORT:-21322}"
EXPECTED="$(tr -d 'v[:space:]' < SHIOAJI_VERSION)"
BIN_VER="$("$BIN" --version 2>/dev/null | tail -1 | awk '{print $2}')"
if [ "$BIN_VER" != "$EXPECTED" ]; then
    echo "binary version $BIN_VER != SHIOAJI_VERSION $EXPECTED — 換掉 $BIN" >&2
    exit 1
fi

# 版本預檢：port 上已有 server 就確認版本 — 相符才沿用，不符直接失敗
# （絕不默默沿用版本不明的 server）
RUNNING="$(curl -sf -m 2 "http://127.0.0.1:$PORT/api/v1/info" 2>/dev/null \
    | sed -n 's/.*"version":"\([^"]*\)".*/\1/p')"
if [ -n "$RUNNING" ]; then
    if [ "$RUNNING" = "$EXPECTED" ]; then
        echo "dev api already running on :$PORT (v$RUNNING, version OK) — reusing"
        exec tail -f /dev/null # keep the launcher's process alive
    fi
    echo ":$PORT 已被 v$RUNNING 佔用（需 v$EXPECTED）— 停掉它或改 SJ_DEV_PORT" >&2
    exit 1
fi

echo "dev api: v$BIN_VER on 127.0.0.1:$PORT"
exec env SJ_HTTP_ADDR="127.0.0.1:$PORT" \
    "$BIN" server start --no-open
