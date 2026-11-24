#!/usr/bin/env bash
set -e

# Nếu không có DISPLAY (chạy tay, image cũ, compose thiếu env...) thì tự đặt.
: "${DISPLAY:=:0}"
export DISPLAY

if ! xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
    echo "Starting Xvfb on $DISPLAY ..."
    Xvfb "$DISPLAY" -screen 0 1366x900x24 -nolisten tcp &
    XVFB_PID=$!

    # chờ display sẵn sàng; nếu Xvfb chết giữa chừng thì báo lỗi rõ ràng
    for i in $(seq 1 50); do
        if xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
            break
        fi
        if ! kill -0 "$XVFB_PID" 2>/dev/null; then
            echo "Xvfb thoát sớm — xem log phía trên" >&2
            exit 1
        fi
        sleep 0.1
    done

    if ! xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
        echo "Xvfb không sẵn sàng sau 5s trên $DISPLAY" >&2
        exit 1
    fi
    echo "Xvfb ready on $DISPLAY (pid $XVFB_PID)"
fi

# tắt Xvfb khi container dừng
trap 'kill "$XVFB_PID" 2>/dev/null || true' EXIT

exec waitress-serve --listen=0.0.0.0:8000 --threads=8 api_v2:app