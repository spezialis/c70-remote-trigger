# CLAUDE.md

Motive (OptiTrack) take start/stop → Canon EOS C70 REC via `c70_motive_bridge.py`. It runs on the Motive PC (Windows, Python 3.11).
Setup, usage, take-log columns and the verified API are in [README.md](README.md). This file holds what the README doesn't: environment facts, test status, the reasons behind decisions, and open work.

## Working style
- Direct and concise. Be honest about uncertainty and flag assumptions.
- **Don't invent API details.** Every camera endpoint must come from the device (its web UI scripts / DevTools) and be verified on the real camera.
- The camera is only reachable from the Motive PC's network, not from the dev Mac. Test locally against a fake camera and fake Motive packets, then ask the user to run it on the device.
- Canon manual: `CAC70RF.pdf` (local only, gitignored). Browser Remote: pp. 156–162; metadata: p. 98.

## Environment (verified on device)
- C70 firmware 1.1.2.1. Network uses an LMP USB-C Gigabit Ethernet adapter. The Delock 81570 (RTL8157, 5 GbE) did **not** work; the C70 only supports RTL8153-class chips.
- Camera connection SET2 = Browser Remote, Ethernet, IPv4 DHCP, IPv6 off (SET1 = XC Protocol, unused). Currently on the school network at **10.21.137.42** (/21, gw 10.21.136.1), which may change.
- Browser Remote user: One User (Full), `ias`. **Never store the password**; ask the user.
- One session per user: login redirects to `/wpd/already_login.shtml` while another session is alive, and a stale session expires after ~45–60 s. While the bridge runs, Browser Remote as `ias` is blocked in a browser.
- Motive: Remote Trigger on, XML broadcast port 1512. Motive binds `10.21.136.113:1512` itself; the bridge on `0.0.0.0:1512` (SO_REUSEADDR) still receives the packets.
- Windows Firewall: inbound UDP/TCP rules for Python 3.11 exist (Public profile; all NICs are Public).
- **Timecode (since 2026-11-19):** the C70 (BNC TC in) and Motive (eSync 2) are fed shared SMPTE timecode from an external device. Align footage by TC; the network trigger is not frame-accurate.

## Status
- REC start/stop: **works end to end** (2026-11-17). HTTP ~8 ms; camera reports `start` ~220 ms and `stop` ~1050 ms after the trigger.
- Take log (`takes.csv`) and `--discover` / `--get`: added 2026-11-19, **tested only against a fake camera**. The Motive XML field names `Name`, `TimeCode` and `PacketID` come from OptiTrack docs and are not yet verified on this Motive install. The bridge prints the raw XML so it can be checked.

## Open to-dos
1. **Run `--discover` on the camera**, then wire up these Browser Remote features (no endpoints known yet):
   - clip title = Motive take name via the user memo, "Apply from Next" (XF-AVC only; the camera needs Metadata Setting = Remote)
   - shot mark at take start; OK/NG mark on the last clip
   - pre-take check: remaining card time, battery, camera TC (log it next to Motive's TC)
2. Verify on a real take, with a clap or slate, that the C70 clip TC matches Motive's take TC and `motive_timecode` in `takes.csv`.
   Reference take from before shared TC: `video_test/Take 2026-11-17 08.02.59 PM` (mocap 523 frames @120 fps = 4.358 s; video 240 frames @50p = 4.800 s).
3. Production network: fixed IP on a direct cable or a dedicated switch (e.g. camera 192.168.1.10/24, PC 192.168.1.20/24), not on the OptiTrack NIC.
4. Optional: autostart (Task Scheduler), store the password outside the shell history, retry the POST if the state isn't confirmed.
