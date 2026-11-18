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

Each start/stop is appended to a CSV log (C70_LOG, default takes.csv next to
this script) with the Motive take name/timecode and host-side timings.

Find more camera endpoints (read-only; saves the camera's web UI scripts):
    python c70_motive_bridge.py --discover
    python c70_motive_bridge.py --get /ccapi
"""

import argparse
import csv
import datetime as dt
import os
import atexit
import re
import socket
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urljoin, urlparse

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
HERE = Path(__file__).resolve().parent
LOG_PATH = Path(os.environ.get("C70_LOG", HERE / "takes.csv"))
DISCOVER_DIR = HERE / "discover"
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


def press_rec(body: dict, t0: float | None = None) -> dict:
    """Press REC and wait for the state readback.

    t0 (perf_counter) is when the trigger arrived; all ms values are relative
    to it, so they include any time spent before the POST was sent.
    """
    t0 = time.perf_counter() if t0 is None else t0
    res = {"post_http": "", "post_ms": "", "state": "", "confirmed": False, "confirm_ms": ""}
    r = _request("POST", body)
    if r is None:
        res["post_http"] = "error"
        return res
    ms = (time.perf_counter() - t0) * 1000
    res.update(post_http=r.status_code, post_ms=f"{ms:.0f}")
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
        ok = state == want
        res.update(state=state or "", confirmed=ok, confirm_ms=f"{ms:.0f}")
        print(f"[camera] state {state!r} {'CONFIRMED' if ok else 'NOT CONFIRMED'} after {ms:.0f} ms")
    return res


# ------------------------------------------------------------ Motive packets
def parse_motive(msg: str) -> tuple[str, dict] | None:
    """Return (root tag, {child tag: VALUE attr}) for Motive's XML broadcast.

    Field names (Name, TimeCode, ...) follow OptiTrack's documentation and are
    not yet verified on this Motive install; the raw XML is printed so they can
    be checked. Falls back to substring matching if the XML doesn't parse.
    """
    try:
        root = ET.fromstring(msg.strip().lstrip("﻿"))
    except ET.ParseError:
        for tag in ("CaptureStart", "CaptureStop"):
            if tag in msg:
                return tag, {}
        return None
    fields = {child.tag: child.get("VALUE", (child.text or "").strip()) for child in root}
    return root.tag, fields


LOG_FIELDS = ["host_time", "event", "take", "motive_timecode", "motive_packet_id",
              "motive_from", "post_http", "post_ms", "state", "confirmed", "confirm_ms"]


def log_take(row: dict) -> None:
    new = not LOG_PATH.exists()
    try:
        with LOG_PATH.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=LOG_FIELDS, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow(row)
    except OSError as e:
        print(f"[log] could not write {LOG_PATH}: {e}")


def status() -> None:
    r = _request("GET")
    if r is not None:
        print(f"[camera] GET recbutton -> HTTP {r.status_code} {r.text.strip()[:300]}")


def get_path(path: str) -> None:
    """Read-only GET of any camera path with the session (for probing endpoints)."""
    url = urljoin(BASE + "/", path.lstrip("/"))
    if "logout" in url:
        print("refusing to GET a logout URL")
        return
    try:
        r = session.get(url, timeout=3, allow_redirects=False)
    except requests.RequestException as e:
        print(f"[camera] GET {url} failed: {e}")
        return
    print(f"[camera] GET {url} -> HTTP {r.status_code} {r.headers.get('Location', '')}")
    print(r.text[:3000])


# Strings in the web UI that look like API paths / API wrapper functions.
ENDPOINT_RE = re.compile(r"(?:ccapi|brapi)/[A-Za-z0-9_/.{}$+-]*")
FUNC_RE = re.compile(r"\b(?:get|post|put|delete)_[A-Za-z0-9_]{3,}\b")
LINK_RE = re.compile(r"""["']([^"'\s<>]+\.(?:js|shtml|html?))(?:\?[^"']*)?["']""")


def discover(max_files: int = 60) -> None:
    """Crawl the Browser Remote web UI (GET only) and list the API paths it uses.

    Saves every page/script under ./discover/ so the request bodies can be read
    from the source, the same way recbutton was found in shoot_script.js.
    """
    if not login():
        return
    host = urlparse(BASE).netloc
    queue, seen = ["/wpd/shoot.shtml"], set()
    endpoints: dict[str, set[str]] = {}
    funcs: dict[str, set[str]] = {}
    DISCOVER_DIR.mkdir(exist_ok=True)
    while queue and len(seen) < max_files:
        path = queue.pop(0)
        if path in seen or "logout" in path:
            continue
        seen.add(path)
        try:
            r = session.get(BASE + path, timeout=3, allow_redirects=False)
        except requests.RequestException as e:
            print(f"  {path}: {e}")
            continue
        print(f"  GET {path} -> {r.status_code} ({len(r.content)} bytes)")
        if not r.ok:
            continue
        out = DISCOVER_DIR / path.lstrip("/")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(r.content)
        text = r.text
        for m in ENDPOINT_RE.findall(text):
            endpoints.setdefault(m.rstrip("/."), set()).add(path)
        for m in FUNC_RE.findall(text):
            funcs.setdefault(m, set()).add(path)
        for link in LINK_RE.findall(text):
            u = urlparse(urljoin(BASE + path, link))
            if u.netloc == host and u.path not in seen:
                queue.append(u.path)

    # Standard CCAPI exposes its endpoint list at GET /ccapi; may not exist on the C70.
    try:
        r = session.get(BASE + "/ccapi", timeout=3, allow_redirects=False)
        (DISCOVER_DIR / "ccapi_root.txt").write_text(f"HTTP {r.status_code}\n{r.text}", encoding="utf-8")
        print(f"  GET /ccapi -> {r.status_code}")
    except requests.RequestException as e:
        print(f"  GET /ccapi: {e}")

    lines = ["# API paths found in the web UI (path  <- files)"]
    lines += [f"{k}  <- {', '.join(sorted(v))}" for k, v in sorted(endpoints.items())]
    lines += ["", "# API wrapper functions (name  <- files)"]
    lines += [f"{k}  <- {', '.join(sorted(v))}" for k, v in sorted(funcs.items())]
    summary = DISCOVER_DIR / "endpoints.txt"
    summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\nSaved {len(seen)} files + summary to {DISCOVER_DIR}")


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
    print(f"Logging takes to {LOG_PATH}")
    login()
    print(f"[camera] state: {rec_status()}")
    threading.Thread(target=keepalive, daemon=True).start()

    last = {"start": 0.0, "stop": 0.0}
    current_take = ""
    while True:
        data, addr = sock.recvfrom(65535)
        t_rx = time.perf_counter()
        host_time = dt.datetime.now().isoformat(timespec="milliseconds")
        msg = data.decode("utf-8", errors="ignore")
        parsed = parse_motive(msg)
        if parsed is None:
            continue
        tag, fields = parsed
        if tag == "CaptureStart":
            kind, body = "start", START_BODY
        elif tag == "CaptureStop":
            kind, body = "stop", STOP_BODY
        else:
            continue
        now = time.monotonic()
        if now - last[kind] < DEBOUNCE_S:
            continue
        last[kind] = now

        take = fields.get("Name", "")
        if kind == "start":
            current_take = take
        take = take or current_take  # in case CaptureStop has no name
        tc = fields.get("TimeCode", "")
        print(f"[motive] {kind} take={take!r} tc={tc!r} from {addr[0]}")
        print(f"[motive] raw: {msg.strip()[:500]}")
        res = press_rec(body, t_rx)
        log_take({"host_time": host_time, "event": kind, "take": take,
                  "motive_timecode": tc, "motive_packet_id": fields.get("PacketID", ""),
                  "motive_from": addr[0], **res})


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--test", choices=["start", "stop"], help="send one command and exit")
    p.add_argument("--status", action="store_true", help="login + GET recbutton (read-only)")
    p.add_argument("--discover", action="store_true",
                   help="crawl the Browser Remote UI (GET only), list API paths, save to ./discover")
    p.add_argument("--get", metavar="PATH", help="login + read-only GET of PATH, e.g. /ccapi")
    a = p.parse_args()
    atexit.register(logout)  # free the single camera session (also on Ctrl+C)
    if a.discover:
        discover()
    elif a.get:
        if login():
            get_path(a.get)
    elif a.status:
        login()
        status()
    elif a.test:
        login()
        press_rec(START_BODY if a.test == "start" else STOP_BODY)
    else:
        listen()
