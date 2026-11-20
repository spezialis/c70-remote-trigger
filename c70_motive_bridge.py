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

MOTIVE_PORT = int(os.environ.get("C70_MOTIVE_PORT", "1512"))  # Motive Command Port + 2; tests use another port
DEBOUNCE_S = 0.5                   # ignore duplicate packets within this window
KEEPALIVE_S = 15                   # session seems to expire after ~45-60 s idle
HERE = Path(__file__).resolve().parent
LOG_PATH = Path(os.environ.get("C70_LOG", HERE / "takes.csv"))
MOTIVE_RAW_LOG = HERE / "motive_packets.log"   # every raw UDP packet from Motive
DISCOVER_DIR = HERE / "discover"
# -------------------------------------------------------------------------

BASE = f"http://{CAMERA_IP}"
LOGIN_URL = f"{BASE}/brapi/login"  # verified: Digest realm "BrowserRemote"
LOGOUT_URL = f"{BASE}/brapi/logout"
URL = f"{BASE}/ccapi/{API_VERSION}/shooting/control/recbutton"
# From the camera's GET /ccapi list and shoot_script.js (2026-11-20, fw 1.1.4.1).
# Paths and request bodies are copied from the web UI; not yet exercised by this script.
SHOTMARK_URL = f"{BASE}/ccapi/{API_VERSION}/functions/shotmark"    # POST {"type":"shot1mark"}
CLIPMARK_URL = f"{BASE}/ccapi/{API_VERSION}/functions/clipmark"    # POST {"type":"okmark"|"checkmark"}
STORAGE_URL = f"{BASE}/ccapi/{API_VERSION}/devicestatus/storage"   # GET
POWER_URL = f"{BASE}/ccapi/{API_VERSION}/devicestatus/powersource" # GET
# From meta_script.js (2026-11-20): user memo = clip metadata (title etc.).
MEMO_SETTING_URL = f"{BASE}/ccapi/{API_VERSION}/usermemo/setting"  # PUT {"value":"remote"} = "Activate"
MEMO_DATA_URL = f"{BASE}/ccapi/{API_VERSION}/usermemo/data"        # PUT {"editclip":..., "clipmeta":{"title":...}}
UIINFO_URL = f"{BASE}/brapi/metadata/uiinfo"                       # GET: uiinfo_usermemo.value, capability bits
MEMO_CURRENT, MEMO_NEXT, MEMO_PREVIOUS = 1, 2, 4                   # capability bits -> editclip current/next/previous
SHOT_MARK_AT_START = os.environ.get("C70_SHOTMARK", "1") != "0"
# Verified 2026-11-20: the camera is busy for ~1 s after REC start, and every
# rejected (503) shot mark shows an "invalid operation" pop-up on the camera.
# So wait first instead of retrying fast.
SHOT_MARK_DELAY_S = float(os.environ.get("C70_SHOTMARK_DELAY", "1.5"))
CLIP_TITLE = os.environ.get("C70_TITLE", "1") != "0"               # write the Motive take name as clip title
LOW_CARD_MIN = 10                  # warn below this many minutes of card time

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
    print("[camera] logging out ...", flush=True)
    try:
        session.get(LOGOUT_URL, timeout=2, allow_redirects=False)
        print("[camera] logged out")
    except requests.RequestException:
        print("[camera] logout failed (camera not reachable); the session expires in ~1 min")
    except KeyboardInterrupt:
        print("[camera] logout skipped; the session expires in ~1 min")
    session.cookies.clear()


def _request(method: str, body: dict | None = None, url: str = URL) -> requests.Response | None:
    kw = {"timeout": 2}
    if body is not None:
        kw["json"] = body
    with lock:
        for attempt in (1, 2):
            try:
                r = session.request(method, url, **kw)
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


def _json(r: requests.Response | None) -> dict | None:
    if r is None or not r.ok:
        return None
    try:
        return r.json()
    except ValueError:
        return None


def _unwrap(obj: dict | None, key: str) -> dict:
    """The web UI reads these as data.value[...]; accept the dict with or without that wrapper."""
    if not isinstance(obj, dict):
        return {}
    if isinstance(obj.get("value"), dict):
        obj = obj["value"]
    return obj if key in obj else {}


# Last recording format the camera reported ('xf-avc', 'mp4', ...).
camera_format = {"value": ""}


def marks_supported() -> bool:
    """Observed 2026-11-20 (fw 1.1.4.1): in MP4 the camera rejects shot/clip marks
    with 503 "Device busy" (+ "invalid operation" pop-up) and allows no clip title
    (capability 0). In XF-AVC they work. Unknown format -> try anyway."""
    return camera_format["value"] in ("", "xf-avc")


def camera_info(verbose: bool = False) -> dict:
    """Card time and battery, read-only. Field names come from shoot_script.js
    (set_storage / set_powersource)."""
    info = {"card": "", "card_min": "", "card_format": "", "power": "", "battery": ""}
    st_raw = _json(_request("GET", url=STORAGE_URL))
    pw_raw = _json(_request("GET", url=POWER_URL))
    if verbose:
        print(f"[camera] storage raw: {st_raw}")
        print(f"[camera] powersource raw: {pw_raw}")
    st = _unwrap(st_raw, "storageinfo")
    for s in st.get("storageinfo", []):
        if s.get("name") == st.get("currentstorage"):
            info.update(card=s.get("name", ""), card_min=s.get("recordabletime", ""),
                        card_format=s.get("recordingformat", ""))
    if info["card_format"]:
        camera_format["value"] = info["card_format"]
    pw = _unwrap(pw_raw, "sourceinfo")
    info["power"] = pw.get("currentsource", "")
    for s in pw.get("sourceinfo", []):
        if s.get("name") == info["power"]:
            if info["power"] == "battery":
                parts = [f"{s['level_percent']}%" if s.get("level_percent") not in (None, "") else "",
                         f"{s['remaintime']} min" if s.get("remaintime") not in (None, "") else "",
                         s.get("status", "")]
                info["battery"] = " ".join(p for p in parts if p)
            elif s.get("level_volt") not in (None, ""):
                info["battery"] = f"DC {s['level_volt']} V"
    return info


def print_camera_info(info: dict) -> None:
    print(f"[camera] card {info['card'] or '?'}: {info['card_min'] or '?'} min left, "
          f"format {info['card_format'] or '?'} | power {info['power'] or '?'} {info['battery']}")
    try:
        if info["card_min"] != "" and int(info["card_min"]) < LOW_CARD_MIN:
            print(f"[camera] WARNING: less than {LOW_CARD_MIN} min left on the card")
    except (TypeError, ValueError):
        pass


def memo_info() -> tuple[str, int]:
    """(usermemo setting 'remote'/'sdcard'/'', capability bits) from /brapi/metadata/uiinfo."""
    j = _json(_request("GET", url=UIINFO_URL)) or {}
    setting = (j.get("uiinfo_usermemo") or {}).get("value", "")
    try:
        cap = int(j.get("capability", 0)) if setting == "remote" else 0
    except (TypeError, ValueError):
        cap = 0
    return setting, cap


def memo_activate() -> bool:
    """Same as the 'Activate' button on the Browser Remote metadata page."""
    setting, _ = memo_info()
    if setting == "remote":
        print("[camera] metadata: remote (clip title can be set)")
        return True
    r = _request("PUT", {"value": "remote"}, url=MEMO_SETTING_URL)
    ok = r is not None and r.ok
    print(f"[camera] metadata was {setting or '?'!r}, activate remote -> "
          f"{'OK' if ok else ('no response' if r is None else f'HTTP {r.status_code} {r.text.strip()[:200]}')}")
    return ok


def set_clip_title(title: str, editclip: str) -> bool:
    """editclip: 'current' (clip being recorded), 'next' or 'previous'. XF-AVC only."""
    r = _request("PUT", {"editclip": editclip, "clipmeta": {"title": title}}, url=MEMO_DATA_URL)
    ok = r is not None and r.ok
    detail = "no response" if r is None else f"HTTP {r.status_code} {r.text.strip()[:200]}"
    print(f"[camera] clip title {title!r} -> {editclip} clip: {detail}")
    return ok


def title_after(kind: str, take: str, pending: dict) -> str:
    """Write the take name as clip title. Called after REC was pressed.

    At start: the current clip if the camera allows it (capability bit 1).
    Otherwise it's remembered and written to the previous clip after stop (bit 4).
    Returns what to log in the clip_title column.
    """
    if not (CLIP_TITLE and take):
        return ""
    if not marks_supported():
        return f"skipped ({camera_format['value']})"
    _, cap = memo_info()
    if kind == "start":
        if cap & MEMO_CURRENT:
            pending.pop("title", None)
            return "current" if set_clip_title(take, "current") else "error"
        pending["title"] = take
        return f"pending (cap={cap})"
    title = pending.pop("title", None)
    if title is None:
        return ""
    if cap & MEMO_PREVIOUS:
        return "previous" if set_clip_title(title, "previous") else "error"
    print(f"[camera] clip title not set: camera doesn't allow it (capability={cap}; "
          f"needs XF-AVC and metadata = remote)")
    return f"unavailable (cap={cap})"


def post_mark(url: str, kind: str, retries: int = 0, wait_s: float = 0.5) -> bool:
    """POST a shot/clip mark. Verified 2026-11-20: right after REC start the camera
    answers 503 {"message":"Device busy"}, so the shot mark retries on 503."""
    for attempt in range(retries + 1):
        r = _request("POST", {"type": kind}, url=url)
        ok = r is not None and r.ok
        detail = "no response" if r is None else f"HTTP {r.status_code} {r.text.strip()[:200]}"
        print(f"[camera] {kind} -> {detail}" + (f" (try {attempt + 1})" if retries else ""))
        if ok or r is None or r.status_code != 503:
            return ok
        time.sleep(wait_s)
    return False


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
FIELD_RE = re.compile(r"<(\w+)\s+VALUE\s*=\s*\"([^\"]*)\"", re.I)


def parse_motive(msg: str) -> tuple[str, dict] | None:
    """Return (root tag, {child tag lowercased: VALUE attr}) for Motive's XML broadcast.

    Field names (Name, TimeCode, ...) follow OptiTrack's documentation and are
    not yet verified on this Motive install; every raw packet is appended to
    MOTIVE_RAW_LOG so they can be checked. Keys are lowercased so the lookup
    doesn't depend on capitalisation. Motive may pad the packet with NUL bytes,
    which break the XML parser; they are stripped, and if parsing still fails,
    the fields are pulled out with a regex.
    """
    clean = msg.replace("\x00", "").strip().lstrip("﻿")
    try:
        root = ET.fromstring(clean)
        fields = {}
        for child in root:
            attrs = {k.lower(): v for k, v in child.attrib.items()}
            fields[child.tag.lower()] = attrs.get("value", (child.text or "").strip())
        return root.tag, fields
    except ET.ParseError:
        for tag in ("CaptureStart", "CaptureStop"):
            if tag in clean:
                return tag, {k.lower(): v for k, v in FIELD_RE.findall(clean)}
        return None


def log_raw(data: bytes, addr) -> None:
    """Append one raw Motive packet (repr, so NULs/BOMs are visible) for debugging."""
    try:
        with MOTIVE_RAW_LOG.open("a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now().isoformat(timespec='milliseconds')} {addr[0]} {data!r}\n")
    except OSError as e:
        print(f"[log] could not write {MOTIVE_RAW_LOG}: {e}")


LOG_FIELDS = ["host_time", "event", "take", "motive_timecode", "motive_packet_id",
              "motive_from", "post_http", "post_ms", "state", "confirmed", "confirm_ms",
              "shotmark", "clip_title", "card_min", "card_format", "battery"]


log_lock = threading.Lock()


def log_take(row: dict) -> None:
    with log_lock:
        _log_take(row)


def _log_take(row: dict) -> None:
    if LOG_PATH.exists():
        # Columns changed since the file was started: keep the old file, start a new one.
        with LOG_PATH.open(encoding="utf-8") as f:
            header = f.readline().strip()
        if header and header != ",".join(LOG_FIELDS):
            old = LOG_PATH.with_name(f"{LOG_PATH.stem}_{dt.datetime.now():%Y%m%d_%H%M%S}{LOG_PATH.suffix}")
            LOG_PATH.rename(old)
            print(f"[log] take log columns changed; old log kept as {old.name}")
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
    # meta.shtml = metadata page (user memo); it's only linked from script code.
    queue, seen = ["/wpd/shoot.shtml", "/wpd/meta.shtml"], set()
    # Links inside a script are relative to the page that loaded it, not to the
    # script's own folder (/wpd/js/meta.shtml -> 500, /wpd/meta.shtml is right).
    page_of: dict[str, str] = {}
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
        page = page_of.get(path, path) if path.endswith(".js") else path
        for link in LINK_RE.findall(text):
            u = urlparse(urljoin(BASE + page, link))
            if u.netloc == host and u.path not in seen:
                page_of.setdefault(u.path, page)
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


KEYS_HELP = "Keys (type + Enter): o = OK mark, c = check mark, i = card/battery info"


def keyboard(state: dict) -> None:
    """Clip marks / info from the bridge terminal: the camera allows only one
    session, so these can't come from a second process while the bridge runs."""
    while True:
        try:
            line = input().strip().lower()
        except (EOFError, OSError):
            return  # no console (e.g. started in the background)
        if line in ("o", "ok", "c", "check") and not marks_supported():
            print(f"[camera] clip marks need XF-AVC; camera records {camera_format['value']}")
            continue
        if line in ("o", "ok"):
            ok = post_mark(CLIPMARK_URL, "okmark")
        elif line in ("c", "check"):
            ok = post_mark(CLIPMARK_URL, "checkmark")
        elif line in ("i", "info"):
            print_camera_info(camera_info())
            continue
        else:
            if line:
                print(KEYS_HELP)
            continue
        log_take({"host_time": dt.datetime.now().isoformat(timespec="milliseconds"),
                  "event": "okmark" if line.startswith("o") else "checkmark",
                  "take": state["take"], "post_http": "ok" if ok else "error"})


def listen():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", MOTIVE_PORT))
    # On Windows a blocking recvfrom() never sees Ctrl+C; wake up regularly instead.
    sock.settimeout(0.5)
    print(f"Listening for Motive triggers on UDP {MOTIVE_PORT} -> {URL}")
    print(f"Logging takes to {LOG_PATH}")
    login()
    print(f"[camera] state: {rec_status()}")
    print_camera_info(camera_info(verbose=True))
    if not marks_supported():
        print(f"[camera] records {camera_format['value']}: shot mark, OK/check marks and clip title "
              f"are skipped (XF-AVC only)")
    print(f"Shot mark at take start: {'on' if SHOT_MARK_AT_START else 'off'} (C70_SHOTMARK=0 turns it off)")
    print(f"Clip title = take name: {'on' if CLIP_TITLE else 'off'} (C70_TITLE=0 turns it off)")
    if CLIP_TITLE:
        memo_activate()
    pending: dict = {}
    print(KEYS_HELP)
    threading.Thread(target=keepalive, daemon=True).start()
    state = {"take": ""}
    threading.Thread(target=keyboard, args=(state,), daemon=True).start()

    last = {"start": 0.0, "stop": 0.0}
    current_take = ""
    while True:
        try:
            data, addr = sock.recvfrom(65535)
        except socket.timeout:
            continue
        t_rx = time.perf_counter()
        host_time = dt.datetime.now().isoformat(timespec="milliseconds")
        log_raw(data, addr)
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

        take = fields.get("name", "")
        if kind == "start":
            current_take = take
        take = take or current_take  # in case CaptureStop has no name
        tc = fields.get("timecode", "")
        print(f"[motive] {kind} take={take!r} tc={tc!r} from {addr[0]}")
        print(f"[motive] fields: {fields}")
        state["take"] = take
        res = press_rec(body, t_rx)
        # Everything below runs after REC was pressed, so it can't delay it.
        extra = {}
        if res["confirmed"]:
            extra["clip_title"] = title_after(kind, take, pending)
        if kind == "start" and SHOT_MARK_AT_START and res["confirmed"] and not marks_supported():
            extra["shotmark"] = f"skipped ({camera_format['value']})"
        elif kind == "start" and SHOT_MARK_AT_START and res["confirmed"]:
            # In the background, so a quick stop isn't held up by the delay.
            def delayed_shotmark(take=take):
                time.sleep(SHOT_MARK_DELAY_S)
                if rec_status() != "start":
                    print("[camera] shot mark skipped: no longer recording")
                    return
                ok = post_mark(SHOTMARK_URL, "shot1mark", retries=1, wait_s=1.0)
                log_take({"host_time": dt.datetime.now().isoformat(timespec="milliseconds"),
                          "event": "shotmark", "take": take, "shotmark": "ok" if ok else "error"})
            threading.Thread(target=delayed_shotmark, daemon=True).start()
            extra["shotmark"] = f"in {SHOT_MARK_DELAY_S:g} s"
        info = camera_info()
        print_camera_info(info)
        extra.update(card_min=info["card_min"], card_format=info["card_format"],
                     battery=info["battery"])
        log_take({"host_time": host_time, "event": kind, "take": take,
                  "motive_timecode": tc, "motive_packet_id": fields.get("packetid", ""),
                  "motive_from": addr[0], **res, **extra})


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--test", choices=["start", "stop"], help="send one command and exit")
    p.add_argument("--status", action="store_true", help="login + GET recbutton (read-only)")
    p.add_argument("--discover", action="store_true",
                   help="crawl the Browser Remote UI (GET only), list API paths, save to ./discover")
    p.add_argument("--get", metavar="PATH", help="login + read-only GET of PATH, e.g. /ccapi")
    p.add_argument("--info", action="store_true", help="login + card time and battery (read-only)")
    a = p.parse_args()
    atexit.register(logout)  # free the single camera session (also on Ctrl+C)
    if a.discover:
        discover()
    elif a.info:
        if login():
            print_camera_info(camera_info(verbose=True))
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
        try:
            listen()
        except KeyboardInterrupt:
            print("\nStopping (Ctrl+C)")
