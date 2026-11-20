# CLAUDE.md

Motive (OptiTrack) take start/stop → Canon EOS C70 REC via `c70_motive_bridge.py`. It runs on the Motive PC (Windows, Python 3.11).
Setup, usage, take-log columns and the verified API are in [README.md](README.md). This file holds what the README doesn't: environment facts, test status, the reasons behind decisions, and open work.

## Working style
- Direct and concise. Be honest about uncertainty and flag assumptions.
- **Don't invent API details.** Every camera endpoint must come from the device (its web UI scripts in `discover/`, or DevTools) and be verified on the real camera.
- Development happens both on a Mac (camera not reachable) and directly on the Motive PC (`D:\04_projects\2623_LimitsVR_SimonDediesbach\RemoteTrigger_C70`). Test against a fake camera and fake Motive packets first, then ask the user to run it on the device. The user runs the bridge in their own terminal, because it needs the password.
- **Local tests on the Motive PC: never send fake packets to UDP 1512.** A running bridge receives them and records on the real camera; this happened on 2026-11-20 and recorded 2 junk clips. Use `C70_MOTIVE_PORT=15999` plus `C70_IP=127.0.0.1:<fake port>`, and first check that no bridge is running.
- Canon manual: `CAC70RF.pdf` (on the Mac only, gitignored; online at gdlp01.c-wss.com `eosc70-im12-en.pdf`). Browser Remote: pp. 156–162; metadata: p. 98; messages: ~p. 190.

## Environment (verified on device)
- C70 firmware **1.1.4.1** (all facts here were verified on it). Network uses an [LMP USB-C Gigabit Ethernet](https://lmp-adapter.com/product/lmp-usb-c-to-gigabit-ethernet-adapter/) adapter. The [Delock 81570](https://www.delock.com/produkt/81570/merkmale.html) (RTL8157, 5 GbE) did **not** work; the C70 only supports RTL8153-class chips.
- Camera connection SET2 = Browser Remote, Ethernet, IPv4 DHCP, IPv6 off (SET1 = [XC Protocol](https://www.youtube.com/watch?v=7Emc3WyHPDY), unused). On the school network (/21, gw 10.21.136.1). The DHCP address changes (.42 on 2026-11-17, **10.21.137.75** on 2026-11-20), so the user sets `C70_IP` per terminal.
- Browser Remote user: One User (Full), `ias`. **Never store the password**; ask the user.
- One session per user: login redirects to `/wpd/already_login.shtml` while another session is alive, and a stale session expires after ~45–60 s. While the bridge runs, Browser Remote as `ias` is blocked in a browser, and so are the bridge's own `--status`/`--info`/`--discover`.
- Motive: Remote Trigger on, XML broadcast port 1512. Motive binds `10.21.136.113:1512` itself; the bridge on `0.0.0.0:1512` (SO_REUSEADDR) still receives the packets. Packets are **NUL-terminated**. `CaptureStart` carries `Name`, `SessionName`, `TimeCode` (`HH:MM:SS:FF.s`, with subframe), `PacketID`, `Assets`, …; `CaptureStop` has fewer fields (no `SessionName`, no `PacketID`).
- The user makes one Motive session per day (e.g. "2026-11-20 Session"), so the take log is one file per session: `takes_2026-11-20_Session.csv`.
- Windows Firewall: inbound UDP/TCP rules for Python 3.11 exist (Public profile; all NICs are Public).
- **Timecode (since 2026-11-19):** the C70 (BNC TC in) and Motive (eSync 2) are fed shared SMPTE timecode from an external device. Align footage by TC; the network trigger is not frame-accurate.
  - The TC source runs at 25 fps, so mocap is recorded at **100 fps** (4 subframes per TC frame). Video: 25p.
  - The TC reads one hour ahead of the PC clock (e.g. 18:07 at 17:07 PC time). This is expected and not a problem.
  - Motive's FBX export carries per-frame TC as animated `TCHour/TCMinute/TCSecond/TCFrame/TCSubframe` properties on `Root`, and the key times equal the TC time of day. The C70 MP4 has a `tmcd` track with the start TC; XF-AVC clips show start/end TC in Clip Info.
- **Card / format:** SD slot B holds a SanDisk Extreme PRO 128 GB **V30 / UHS-I**; slot A reports as unusable (empty `accesscapability`). XF-AVC 10-bit needs V60/V90 (manual, message list), so the camera shows "card not compatible" in XF-AVC. **Currently recording MP4 H.264 1080p 25p** until a V90 card is available.

## Status (end of 2026-11-20)
Verified on the camera:
- **REC start/stop** end to end. HTTP ~8 ms; the camera confirms `start` ~220 ms and `stop` ~650–1050 ms after the trigger.
- **TC sync:** the camera starts 1–2 video frames after Motive and stops ~0.25–0.5 s after it. Image check (take `new_test_012`): the `IAS_Origin` lift and set-down line up between video and mocap to **±1 video frame**, with no drift. Done with OpenCV in a scratch venv (not a project dependency).
- **Take log:** take name and Motive TC are filled (after the NUL fix), one CSV per Motive session (`final_test_000` in `takes_2026-11-20_Session.csv`). Card time, format and power are read correctly; the responses come **without** the `value` wrapper that the web UI JS suggests.
- **Clip title = take name (XF-AVC):** PUT `usermemo/data` `editclip: current` during REC → the clip sidecar `A002C005_261120A5_CANON.XML` has `<Title>new_test_007</Title>`. The bridge sets the metadata to Remote at startup.
- **Shot mark (XF-AVC):** right after REC start the camera answers 503 `{"message":"Device busy"}` for ~1 s, and **each 503 shows an "invalid operation" pop-up**. It was accepted ~1.2 s after start. A successful mark shows a "Shot Mark" pop-up.
- **MP4:** the camera rejects all marks (503) and allows no clip title (`uiinfo` capability 0). The bridge skips marks/title whenever the storage format isn't `xf-avc` (logged as `skipped (mp4)`). No pop-ups in MP4 since.

Implemented, not yet verified on the camera:
- Shot mark sent once, 1.5 s after the start (`C70_SHOTMARK_DELAY`), in a background thread, skipped if REC already stopped, one retry after 1 s. Should avoid the pop-up in XF-AVC.
- OK/check mark via keys `o`/`c` in the bridge terminal: which clip does it hit (current or last)? (In MP4 it got 503.)
- Clip title fallback to `previous` after the stop when capability bit 1 (current) isn't set.

Not implemented: camera TC readback (only in the live-view info stream; both files already carry TC).

## Open to-dos
1. **Get a V90 SD card** (Canon-verified list), format it in the camera, switch to XF-AVC, then test: shot mark without the pop-up, OK/check mark, clip title. Also check whether "invalid operation" on the camera's own REC button (seen in XF-AVC with the V30 card) goes away.
2. Production network: fixed IP on a direct cable or a dedicated switch (e.g. camera 192.168.1.10/24, PC 192.168.1.20/24), not on the OptiTrack NIC. That also removes the changing DHCP address.
3. Optional: autostart (Task Scheduler), store the password outside the shell (e.g. Windows Credential Manager), retry the POST if the state isn't confirmed.
