# CLAUDE.md

Motive (OptiTrack) take start/stop → Canon EOS C70 REC via `c70_motive_bridge.py`. It runs on the Motive PC (Windows, Python 3.11).
Setup, usage, take-log columns and the verified API are in [README.md](README.md). This file holds what the README doesn't: environment facts, test status, the reasons behind decisions, and open work.

## Working style
- Direct and concise. Be honest about uncertainty and flag assumptions.
- **Don't invent API details.** Every camera endpoint must come from the device (its web UI scripts / DevTools) and be verified on the real camera.
- The camera is only reachable from the Motive PC's network, not from the dev Mac. Test locally against a fake camera and fake Motive packets, then ask the user to run it on the device.
- **Local tests on the Motive PC: never send fake packets to UDP 1512.** A running bridge receives them and records on the real camera; this happened on 2026-11-20 and recorded 2 junk clips. Use `C70_MOTIVE_PORT=15999` plus `C70_IP=127.0.0.1:<fake port>`, and first check that no bridge is running.
- Canon manual: `CAC70RF.pdf` (local only, gitignored). Browser Remote: pp. 156–162; metadata: p. 98.

## Environment (verified on device)
- C70 firmware **1.1.4.1** (all facts here were verified on it). Network uses an [LMP USB-C Gigabit Ethernet](https://lmp-adapter.com/product/lmp-usb-c-to-gigabit-ethernet-adapter/) adapter. The [Delock 81570](https://www.delock.com/produkt/81570/merkmale.html) (RTL8157, 5 GbE) did **not** work; the C70 only supports RTL8153-class chips.
- Camera connection SET2 = Browser Remote, Ethernet, IPv4 DHCP, IPv6 off (SET1 = [XC Protocol](https://www.youtube.com/watch?v=7Emc3WyHPDY), unused). On the school network (/21, gw 10.21.136.1). The DHCP address changes (was 10.21.137.42 and no longer answered on 2026-11-20), so set `C70_IP` per session.
- Browser Remote user: One User (Full), `ias`. **Never store the password**; ask the user.
- One session per user: login redirects to `/wpd/already_login.shtml` while another session is alive, and a stale session expires after ~45–60 s. While the bridge runs, Browser Remote as `ias` is blocked in a browser.
- Motive: Remote Trigger on, XML broadcast port 1512. Motive binds `10.21.136.113:1512` itself; the bridge on `0.0.0.0:1512` (SO_REUSEADDR) still receives the packets.
- Windows Firewall: inbound UDP/TCP rules for Python 3.11 exist (Public profile; all NICs are Public).
- **Timecode (since 2026-11-19):** the C70 (BNC TC in) and Motive (eSync 2) are fed shared SMPTE timecode from an external device. Align footage by TC; the network trigger is not frame-accurate.
  - The TC source runs at 25 fps, so mocap is recorded at **100 fps** (4 subframes per TC frame). Video: 25p MP4.
  - The TC reads one hour ahead of the PC clock (e.g. 18:07 at 17:07 PC time). This is expected and not a problem.
  - Motive's FBX export carries per-frame TC as animated `TCHour/TCMinute/TCSecond/TCFrame/TCSubframe` properties on `Root`, and the key times equal the TC time of day. The C70 MP4 has a `tmcd` track with the start TC.

## Status
- REC start/stop: **works end to end** (2026-11-17, firmware 1.1.4.1). HTTP ~8 ms; camera reports `start` ~220 ms and `stop` ~1050 ms after the trigger.
- Take log (`takes.csv`): works on the device for REC timing, but `take` and `motive_timecode` stayed **empty** in the 2026-11-20 takes. Suspected cause: Motive NUL-pads the packet, so the XML parse failed and only the substring fallback matched. Fixed 2026-11-20 (strips NULs, regex fallback, case-insensitive keys), **not yet verified on Motive**. Every raw packet now goes to `motive_packets.log` (gitignored); check it after the next take.
- `--discover` ran on the camera 2026-11-20 (files in `discover/`, gitignored), including `meta.shtml` / `meta_script.js` after the crawler fix. `GET /ccapi` lists all endpoints.
- **First real take with all features (2026-11-20, `new_test_007`, XF-AVC):** NUL fix works, so take name and TC arrive (Motive TC format `18:49:23:04.00` = with subframe; the stop packet has fewer fields). Storage/powersource responses are **unwrapped** (no `value`). Clip title PUT `current` -> 200. Shot mark 0.2 s after REC start -> **503 `{"message":"Device busy"}`**, and the camera showed an **"invalid operation"** pop-up (probably from the shot mark, not confirmed). Changed: title first, shot mark retries on 503 (3x, 0.5 s).
- **Clip title = take name: verified on the camera 2026-11-20.** PUT `usermemo/data` `editclip: current` during REC -> clip `A002C005` sidecar `A002C005_261120A5_CANON.XML` has `<Title>new_test_007</Title>`. Clip TC 18:49:23:06–18:49:30:22 vs Motive 18:49:23:04 / 18:49:30:10: the camera starts 2 frames later and stops 12 frames later.
- Shot mark with retry (take `new_test_008`, 2026-11-20): 503 busy twice, then **200 on try 3** (~1.2 s after REC start). **Every 503 shows an "invalid operation" pop-up on the camera**, and a successful mark shows a "Shot Mark" pop-up. Now the bridge sends the shot mark once, `C70_SHOTMARK_DELAY` (default 1.5 s) after the start, in a background thread, skips it if REC already stopped, and retries once after 1 s. Not yet tested on the camera in XF-AVC.
- **In MP4 the camera rejects all marks** (take `new_test_012`, 2026-11-20: shot mark at 1.5 s and 2.5 s, and the OK mark after the stop, all got 503 "Device busy"), and `uiinfo` capability = 0, so no clip title either. Observed behaviour, not from the manual. The bridge now skips marks and title when the storage format isn't `xf-avc`.
- **Card:** the SanDisk Extreme PRO 128 GB in slot B is **V30 / UHS-I**. XF-AVC 10-bit needs V60/V90 (C70 manual, message list), which triggers the "card not compatible" message. The user went back to MP4 H.264 1080p 25p until a V90 card is available. Slot A is reported as unusable (empty `accesscapability`). "Invalid operation" also appeared when pressing REC on the camera in XF-AVC with this card; cause unconfirmed.
- Clip title = take name (`usermemo`): added 2026-11-20 from `meta_script.js`. At start it writes to `current` if capability bit 1 is set, otherwise to `previous` after stop (bit 4).
- Added 2026-11-20 from `shoot_script.js`, **tested only against a fake camera**: shot mark at start, OK/check marks via keys `o`/`c` in the bridge terminal, card/battery via `devicestatus/storage|powersource`. The response shapes are inferred from the JS (`data.value[...]`), and the code accepts them with or without the `value` wrapper. The bridge prints the raw responses at startup.
- Camera TC isn't available through a simple GET; the web UI only gets it from the live-view info stream. Not implemented, because both files already carry TC.
- **TC sync verified 2026-11-20** (take `test_002`, `video_mocap_test/`): mocap starts at TC 18:07:06:11.0 and the video at 18:07:06:12, so the camera starts 1 video frame (40 ms) after Motive and stops ~0.25 s after it. The `IAS_Origin` rigid body lifts off the floor at mocap frame 577–579 = TC **18:07:12:05**. Still to do: confirm in the video that the lift appears at 18:07:12:05–06 (clip frame ~143–144).

## Open to-dos
1. Verify on the camera: shot mark, OK/check mark (does it hit the current or the last clip?), card/battery parsing, and the clip title (which `capability` bits appear while recording / stopped, and whether the title shows up in the XF-AVC clip). The clip title needs XF-AVC; the camera currently records **MP4**.
2. ~~Confirm sync in the image~~ **Done 2026-11-20** (take `new_test_012`, `video_mocap_test/MVI_0008.MP4` + `new_test_012/IAS_Origin.fbx`): video TC start 19:15:56:22 (1 frame after mocap 19:15:56:21). The `IAS_Origin` lift (mocap TC 19:16:04:00–03 → video frames 178–181) shows in the video at frames 180–181; the set-down (mocap 19:16:06:00–01 → 228–229) at 227–228. So video and mocap agree to **±1 video frame** at both points, with no drift. Checked with OpenCV in a scratch venv (not a project dependency). `motive_timecode` in `takes.csv` is filled since the NUL fix.
3. Production network: fixed IP on a direct cable or a dedicated switch (e.g. camera 192.168.1.10/24, PC 192.168.1.20/24), not on the OptiTrack NIC.
4. Optional: autostart (Task Scheduler), store the password outside the shell history, retry the POST if the state isn't confirmed.
