# RemoteTrigger_c70

Starts and stops recording on a **Canon EOS C70** when a take starts or stops in **OptiTrack Motive**.

```
Motive ──UDP XML broadcast (port 1512, CaptureStart / CaptureStop)──▶ c70_motive_bridge.py
c70_motive_bridge.py ──HTTP (Canon Browser Remote / CCAPI)──▶ C70 ──▶ REC
```

A single Python script runs on the Motive PC. It listens for Motive's remote-trigger broadcast and presses REC on the camera through its Browser Remote web API. It then reads the camera's REC state back to confirm the command worked, and logs every take to a CSV file.

**Status:** tested end to end on 2026-11-17 with C70 firmware 1.1.4.1. Measured on that setup, the HTTP command took ~8 ms. The camera reported recording ~220 ms after a start trigger and stopped ~1 s after a stop trigger.

## Requirements

- Python 3.10+ and `requests` (`pip install requests`)
- Canon EOS C70 on the network through a USB-C Ethernet adapter with an RTL8153-class chip. We tested the [LMP USB-C to Gigabit Ethernet adapter](https://lmp-adapter.com/product/lmp-usb-c-to-gigabit-ethernet-adapter/); Canon also lists the [TP-Link UE300](https://www.tp-link.com/us/home-networking/usb-converter/ue300/) and the [StarTech US1GC30W](https://www.startech.com/en-us/networking-io/US1GC30W). 2.5/5 GbE adapters do **not** work, e.g. the [Delock 81570](https://www.delock.com/produkt/81570/merkmale.html) (RTL8157).
- Motive with **Settings → Streaming → Remote Trigger** turned on (XML broadcast port 1512)
- The Motive PC's firewall must allow inbound UDP on port 1512 for Python

## Camera setup

1. Set up a network connection on the C70 with the function **Browser Remote** (Ethernet, IPv4).
2. Under Browser Remote users, choose **One User (Full)** and set a user name and password.
3. Activate the network and note the camera's IP address from the Network Settings status screen.

The camera allows **one Browser Remote session per user**. While the bridge is running, logging in to Browser Remote in a web browser with the same user is refused, and the other way round. A stale session expires after about a minute.

## Usage

Set the camera credentials as environment variables (PowerShell):

```powershell
$env:C70_IP = "192.168.1.10"        # camera IP
$env:C70_USER = "<user>"
$env:C70_PASSWORD = "<password>"
```

```powershell
python c70_motive_bridge.py --status       # log in and read the REC state (doesn't record)
python c70_motive_bridge.py --test start   # press REC once
python c70_motive_bridge.py --test stop
python c70_motive_bridge.py                # run the bridge; Ctrl+C stops it and logs out
```

While running, the bridge:
- logs in at startup and keeps the session alive every 15 s
- logs in again automatically if the camera rejects the session
- ignores duplicate Motive packets within 0.5 s
- reads the REC state back after each press and prints `CONFIRMED` or `NOT CONFIRMED`

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `C70_IP` | `10.21.137.42` | Camera IP (optionally `ip:port`) |
| `C70_USER` | `ias` | Browser Remote user |
| `C70_PASSWORD` | – | Browser Remote password (required) |
| `C70_LOG` | `takes.csv` next to the script | Take log file |

## Take log

Every start and stop adds one row to `takes.csv`:

| Column | Meaning |
|---|---|
| `host_time` | PC time when the Motive packet arrived (ms resolution) |
| `event` | `start` / `stop` |
| `take` | Take name from Motive's XML |
| `motive_timecode` | Timecode from Motive's XML |
| `motive_packet_id`, `motive_from` | Motive packet ID and sender IP |
| `post_http`, `post_ms` | Camera response code and time until it answered |
| `state`, `confirmed`, `confirm_ms` | REC state read back, whether it matches, and when |

All times in ms are measured from when the Motive packet arrived. The field names `Name`, `TimeCode` and `PacketID` come from OptiTrack's documentation. The bridge prints the raw XML of every trigger so you can check them on your Motive version.

## Sync

Network triggering is **not frame-accurate**: the delay is tens to hundreds of ms and varies between takes. Use it to start and stop the camera, not to align the footage.

For alignment, the C70 (BNC TC input) and Motive (eSync 2) are fed the **same SMPTE timecode** from an external timecode device. Align video and mocap in post by timecode.

## Finding more camera endpoints

The C70's Browser Remote can also set the clip title (user memo), add shot marks and OK/NG marks, and show timecode, battery and remaining card time. The bridge doesn't use these yet. Their API endpoints aren't documented; to find them:

```powershell
python c70_motive_bridge.py --discover     # download the Browser Remote web UI (read-only)
python c70_motive_bridge.py --get /ccapi   # read-only GET of any single camera path
```

`--discover` saves the camera's web pages and scripts to `discover/` and lists the API paths and functions it finds in `discover/endpoints.txt`. It only reads, and it never opens the logout link.

## API notes (verified on the C70)

- **Login:** `GET /brapi/login` with HTTP Digest (realm `BrowserRemote`) → `303` + cookie `brsessionid`. Without it, `/ccapi/*` returns `403 {"message":"Not access"}`.
- **Logout:** `GET /brapi/logout`
- **REC:** `POST /ccapi/ver111/shooting/control/recbutton` with `{"action":"start"}` or `{"action":"stop"}` → `200 {}`
- **REC state:** `GET` on the same URL → `{"recbutton":{"status":"start"|"stop"}}`

## Limitations

- The C70 has a single USB-C port, so the Ethernet adapter and a gimbal control cable can't be connected at the same time.
- Put the camera on a fixed IP on a dedicated network (direct cable or its own switch), not on the OptiTrack camera network.

See [CLAUDE.md](CLAUDE.md) for environment details, project status and open to-dos.
