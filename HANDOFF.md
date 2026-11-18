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

### Take log (added 2026-11-19, tested against a fake camera only)
Each start/stop is appended to `takes.csv` (override with `$env:C70_LOG`):
`host_time, event, take, motive_timecode, motive_packet_id, motive_from, post_http, post_ms, state, confirmed, confirm_ms`.
`post_ms`/`confirm_ms` are measured from the moment the Motive packet was received.
The Motive XML is parsed generically (`<Tag VALUE="..."/>` children). The field names `Name`, `TimeCode` and `PacketID` come from OptiTrack docs and are **not yet verified** on this Motive install. The bridge prints the raw XML of every trigger, so check it on the first real take. Non-XML packets fall back to substring matching, as before.

### Endpoint discovery (read-only)
```powershell
python c70_motive_bridge.py --discover      # crawls /wpd/shoot.shtml + linked pages/scripts (GET only, skips logout)
python c70_motive_bridge.py --get /ccapi    # any single read-only GET with the session
```
`--discover` saves the camera's web UI files to `discover/` and writes `discover/endpoints.txt` (API paths + `get_/post_/put_` wrapper names).

## Remaining to-dos
0. **Run `--discover` on the camera** (log in as Full user `ias`), then wire up the features the manual (CAC70RF.pdf pp. 98, 156–162) says Browser Remote supports. None of their endpoints are known yet:
   - clip title = Motive take name via the user memo, "Apply from Next" (XF-AVC only; the camera needs Metadata Setting = Remote / [Activate])
   - shot mark at take start; OK/NG mark on the last clip
   - pre-take check: remaining card time, battery, camera timecode (log it next to the Motive TC)
1. Production network: **fixed IP** on a direct cable or a dedicated switch (e.g. camera 192.168.1.10/24, PC NIC 192.168.1.20/24), NOT on the OptiTrack NIC. Camera is on school DHCP right now (10.21.137.42); set `C70_IP` accordingly.
2. Sync: **since 2026-11-19 the C70 and Motive share timecode from an external timecode device**, so align in post by timecode, not by trigger timing. Still to verify: that the C70 clip TC and Motive's take TC (and the `TimeCode` field in Motive's XML / `takes.csv`) match on a real take, e.g. with a clap or slate. Earlier test take `video_test/Take 2026-11-17 08.02.59 PM` (before shared TC): mocap 523 frames @120 fps (4.358 s) vs video 240 frames @50p (4.800 s).
3. Optional: autostart the bridge (e.g. Task Scheduler) and store the password outside the shell history.

## Known limitations / design notes
- Network trigger latency is roughly tens to hundreds of ms and not deterministic. For frame-accurate alignment, feed **shared SMPTE timecode** into the OptiTrack **eSync 2** and the C70 **BNC TC input**, and align in post.
- C70 has a single USB-C port, so the Ethernet adapter and a DJI gimbal control cable can't be used at the same time.
- Possible improvement: retry the POST if the state isn't confirmed.

## User preferences
Direct, concise, honest about uncertainty; flag assumptions; don't invent API details. Verify against the real device.
