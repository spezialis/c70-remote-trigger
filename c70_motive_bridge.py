#!/usr/bin/env python3
"""
Motive (OptiTrack) -> Canon EOS C70 record bridge.

Listens for Motive's XML start/stop broadcast (UDP, default port 1512)
and presses REC on the C70 through its Browser Remote HTTP API.

Requirements:  pip install requests

Test the camera link first, without Motive:
    python c70_motive_bridge.py --test start
    python c70_motive_bridge.py --test stop

Run the bridge:
    python c70_motive_bridge.py
"""

import argparse
import json
import os
import atexit
import socket
import threading
import time

import requests
from requests.auth import HTTPDigestAuth

# ---------------------------------------------------------------- settings
CAMERA_IP = os.environ.get("C70_IP", "10.21.137.42")  # later: fixed IP, e.g. 192.168.1.10
API_VERSION = "ver111"             # as seen in your captured request
USERNAME = os.environ.get("C70_USER", "ias")
PASSWORD = os.environ.get("C70_PASSWORD", "")  # set env var C70_PASSWORD

# Verified from the camera's shoot_script.js (post_shooting_control_recbutton):
START_BODY = {"action": "start"}
STOP_BODY = {"action": "stop"}

MOTIVE_PORT = 1512                 # Motive Command Port + 2
DEBOUNCE_S = 0.5                   # ignore duplicate packets within this window
KEEPALIVE_S = 15                   # session seems to expire after ~45-60 s idle
# -------------------------------------------------------------------------

BASE = f"http://{CAMERA_IP}"
LOGIN_URL = f"{BASE}/brapi/login"  # verified: Digest realm "BrowserRemote"
LOGOUT_URL = f"{BASE}/brapi/logout"
URL = f"{BASE}/ccapi/{API_VERSION}/shooting/control/recbutton"

# Verified on device:
# - /ccapi/* returns 403 {"message":"Not access"} without a session.
# - Digest auth on /brapi/login -> 303 to /wpd/shoot.shtml + cookie brsessionid.
# - Only ONE session per user: while another is alive (browser tab, or a
#   previous run that didn't log out), login -> 303 /wpd/already_login.shtml.
# - GET recbutton -> {"recbutton":{"status":"stop"}}  (read-only)
session = requests.Session()
lock = threading.Lock()


def login() -> bool:
    if not PASSWORD:
        print("[camera] C70_PASSWORD env var not set")
        return False
    try:
        r = session.get(LOGIN_URL, auth=HTTPDigestAuth(USERNAME, PASSWORD),
                        timeout=3, allow_redirects=False)
    except requests.RequestException as e:
        print(f"[camera] login failed: {e}")
        return False
    loc = r.headers.get("Location", "")
    if "already_login" in loc:
        print("[camera] login refused: another session is active (close Browser "
              "Remote tabs, or wait ~1 min for a stale session to expire)")
        return False
    ok = r.status_code < 400 and "brsessionid" in session.cookies
    print(f"[camera] login -> HTTP {r.status_code} {loc} {'OK' if ok else 'FAILED'}")
    return ok


def logout() -> None:
    if "brsessionid" not in session.cookies:
        return
    try:
        session.get(LOGOUT_URL, timeout=2, allow_redirects=False)
        print("[camera] logged out")
    except requests.RequestException:
        pass
    session.cookies.clear()


def _request(method: str, body: dict | None = None) -> requests.Response | None:
    kw = {"timeout": 2}
    if body is not None:
        kw["json"] = body
    with lock:
        for attempt in (1, 2):
            try:
                r = session.request(method, URL, **kw)
            except requests.RequestException as e:
                print(f"[camera] request failed: {e}")
                return None
            if r.status_code in (401, 403) and attempt == 1:
                print(f"[camera] HTTP {r.status_code}, (re)logging in")
                if not login():
                    return r
                continue
            return r
    return r


def rec_status() -> str | None:
    r = _request("GET")
    if r is None or not r.ok:
        return None
    try:
        return r.json()["recbutton"]["status"]
    except (ValueError, KeyError, TypeError):
        return None


def press_rec(body: dict) -> bool:
    t0 = time.perf_counter()
    r = _request("POST", body)
    if r is None:
        return False
    ms = (time.perf_counter() - t0) * 1000
    print(f"[camera] {body} -> HTTP {r.status_code} ({ms:.0f} ms) {r.text.strip()[:200]}")
    if r.ok:
        # Verified: status reads "start"/"stop"; the stop transition can take >0.3 s.
        want, state = body["action"], None
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = rec_status()
            if state == want:
                break
            time.sleep(0.2)
        ms = (time.perf_counter() - t0) * 1000
        tag = "CONFIRMED" if state == want else "NOT CONFIRMED"
        print(f"[camera] state {state!r} {tag} after {ms:.0f} ms")
    return r.ok


def status() -> None:
    r = _request("GET")
    if r is not None:
        print(f"[camera] GET recbutton -> HTTP {r.status_code} {r.text.strip()[:300]}")


def keepalive() -> None:
    while True:
        time.sleep(KEEPALIVE_S)
        if rec_status() is None:
            print("[camera] keepalive failed")


def listen():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", MOTIVE_PORT))
    print(f"Listening for Motive triggers on UDP {MOTIVE_PORT} -> {URL}")
    login()
    print(f"[camera] state: {rec_status()}")
    threading.Thread(target=keepalive, daemon=True).start()

    last = {"start": 0.0, "stop": 0.0}
    while True:
        data, addr = sock.recvfrom(65535)
        msg = data.decode("utf-8", errors="ignore")
        now = time.monotonic()
        if "CaptureStart" in msg:
            kind, body = "start", START_BODY
        elif "CaptureStop" in msg:
            kind, body = "stop", STOP_BODY
        else:
            continue
        if now - last[kind] < DEBOUNCE_S:
            continue
        last[kind] = now
        print(f"[motive] {kind} from {addr[0]}")
        press_rec(body)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--test", choices=["start", "stop"], help="send one command and exit")
    p.add_argument("--status", action="store_true", help="login + GET recbutton (read-only)")
    a = p.parse_args()
    atexit.register(logout)  # free the single camera session (also on Ctrl+C)
    if a.status:
        login()
        status()
    elif a.test:
        login()
        press_rec(START_BODY if a.test == "start" else STOP_BODY)
    else:
        listen()
