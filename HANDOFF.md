# Handoff: Motive (OptiTrack) → Canon EOS C70 record trigger

## Goal
When a take starts/stops in **Motive (OptiTrack)**, the **Canon EOS C70** should start/stop recording automatically.
Machine: the Motive PC (Windows). Bridge script: `c70_motive_bridge.py` (in this folder).

## Architecture
```
Motive  --UDP XML broadcast (port 1512, CaptureStart/CaptureStop)-->  c70_motive_bridge.py
c70_motive_bridge.py  --HTTP POST (Canon CCAPI)-->  C70 Browser Remote  -->  REC
```

## Camera state (verified on device)
- Canon **EOS C70**, firmware **1.1.2.1**
- Network via USB-C Ethernet adapter **LMP USB-C to Gigabit Ethernet** (works).
  - Note: Delock 81570 (Realtek RTL8157, 5GbE) did NOT work, because the C70 only has drivers for older chips (RTL8153-class). Canon-tested: TP-Link UE300, StarTech US1GC30W.
- Connection setting **SET2** = **Browser Remote**, Ethernet, **IPv4 Automatic (DHCP)**, IPv6 disabled
  - IP at time of setup: **10.21.137.42** (school network, /21, gw 10.21.136.1). May change, because it's DHCP.
  - SET1 = XC Protocol (not used).
- Browser Remote auth: **One User (Full)**, username `ias` (password: ask the user; it's not stored here)
- Browser Remote URL: `http://<camera-ip>/`, HTTP port 80
- REC endpoint found by the user in DevTools:
  `http://10.21.137.42/ccapi/ver111/shooting/control/recbutton`
  This is Canon **CCAPI** (Camera Control API, HTTP/JSON).

## Status: WORKING end-to-end (tested 2026-11-17)
Motive take start/stop → C70 REC start/stop, confirmed via camera state readback.
HTTP command ~8 ms; camera reports `start` ~220 ms after the trigger and `stop` ~1050 ms after it.

## Verified API details (on device)
- **Auth = session, not plain Digest.** `GET /brapi/login` with HTTP Digest (realm `BrowserRemote`) → 303 to `/wpd/shoot.shtml` + cookie `brsessionid`. Without it, `/ccapi/*` → 403 `{"message":"Not access"}`.
- **Logout:** `GET /brapi/logout` frees the session immediately.
- **One session per user.** While another is alive (an open Browser Remote tab, or a script that didn't log out), login → 303 `/wpd/already_login.shtml`. A stale session expires after ~45–60 s idle. **While the bridge runs, Browser Remote as `ias` is blocked.**
- **REC:** `POST /ccapi/ver111/shooting/control/recbutton` body `{"action":"start"|"stop"}` (from the camera's `shoot_script.js`) → 200 `{}`.
- **State:** `GET` same URL → `{"recbutton":{"status":"start"|"stop"}}`.
- **Motive:** Settings → Streaming → **Remote Trigger = on**, XML Broadcast Port 1512. Motive binds `10.21.136.113:1512` itself; the bridge on `0.0.0.0:1512` (SO_REUSEADDR) still receives `CaptureStart`/`CaptureStop`.
- Windows Firewall: inbound UDP/TCP rules for Python 3.11 exist (Public profile; all NICs are Public).

## How to run
```powershell
$env:C70_PASSWORD = "<password>"     # optional: $env:C70_IP, $env:C70_USER
python c70_motive_bridge.py --status       # login + read REC state (no recording)
python c70_motive_bridge.py --test start   # / --test stop
python c70_motive_bridge.py                # bridge; Ctrl+C logs out
```
The bridge logs in at startup, sends a keepalive every 15 s, logs in again on 401/403, and confirms the REC state after each press.

## Remaining to-dos
1. Production network: **fixed IP** on a direct cable or a dedicated switch (e.g. camera 192.168.1.10/24, PC NIC 192.168.1.20/24), NOT on the OptiTrack NIC. Camera is on school DHCP right now (10.21.137.42); set `C70_IP` accordingly.
2. Sync: test take `video_test/Take 2026-11-17 08.02.59 PM`: mocap 523 frames @120 fps (4.358 s) vs video 240 frames @50p (4.800 s). Start offset not measured (needs a visible sync event). A hardware Motive/camera sync device is under consideration for later.
3. Optional: autostart the bridge (e.g. Task Scheduler) and store the password outside the shell history.

## Known limitations / design notes
- Network trigger latency is roughly tens to hundreds of ms and not deterministic. For frame-accurate alignment, feed **shared SMPTE timecode** into the OptiTrack **eSync 2** and the C70 **BNC TC input**, and align in post.
- C70 has a single USB-C port, so the Ethernet adapter and a DJI gimbal control cable can't be used at the same time.
- Possible improvements: use the take name from Motive's XML in the log; retry the POST if the state isn't confirmed.

## User preferences
Direct, concise, honest about uncertainty; flag assumptions; don't invent API details. Verify against the real device.
