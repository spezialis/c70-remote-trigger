# Cannon C70 Remote Trigger

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
python c70_motive_bridge.py --info         # card time left and battery (read-only)
python c70_motive_bridge.py --test start   # press REC once
python c70_motive_bridge.py --test stop
python c70_motive_bridge.py                # run the bridge; Ctrl+C stops it and logs out
```

While running, the bridge:
- logs in at startup and keeps the session alive every 15 s
- logs in again automatically if the camera rejects the session
- ignores duplicate Motive packets within 0.5 s
- reads the REC state back after each press and prints `CONFIRMED` or `NOT CONFIRMED`
- sets a **shot mark** 1.5 s after each confirmed start (turn off with `C70_SHOTMARK=0`). Right after REC starts, the camera is busy for about a second and rejects marks with an "invalid operation" pop-up.
- writes the Motive **take name as clip title** (turn off with `C70_TITLE=0`). At startup it switches the camera's metadata to Remote ("Activate"). It writes to the clip being recorded if the camera allows it, otherwise to the previous clip after the stop. The camera supports this for **XF-AVC only**.
- shows card time left and battery at startup and after every start/stop, and warns below 10 min of card time
- takes keys in its terminal (type + Enter): `o` = **OK mark**, `c` = **check mark**, `i` = card/battery info. The camera allows only one session, so marks can't be sent from a second terminal while the bridge runs.

Everything after the REC press runs after it, so it doesn't delay recording.

**Shot mark, OK/check marks and clip title only work in XF-AVC.** In MP4 the camera rejects them ("Device busy" / "invalid operation" pop-up), so the bridge skips them when the camera reports MP4. XF-AVC 10-bit needs a V60 or V90 SD card.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `C70_IP` | `10.21.137.42` | Camera IP (optionally `ip:port`) |
| `C70_USER` | `ias` | Browser Remote user |
| `C70_PASSWORD` | – | Browser Remote password (required) |
| `C70_LOG` | `takes.csv` next to the script | Take log file |
| `C70_SHOTMARK` | `1` | `0` = no shot mark at take start |
| `C70_SHOTMARK_DELAY` | `1.5` | Seconds after REC start before the shot mark is sent |
| `C70_TITLE` | `1` | `0` = don't write the take name as clip title |
| `C70_MOTIVE_PORT` | `1512` | UDP port to listen on (use another port for local tests) |

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
| `shotmark` | `ok` / `error` for the shot mark at take start |
| `clip_title` | Where the take name went: `current`, `previous`, `pending (cap=…)` (written at stop), `unavailable (cap=…)` or `error` |
| `card_min`, `card_format`, `battery` | Card time left, recording format and battery, read after the REC press |

Rows with `event` = `okmark` / `checkmark` come from the `o` / `c` keys. If the columns change between versions, the old file is kept as `takes_<date>_<time>.csv`.

All times in ms are measured from when the Motive packet arrived. The field names `Name`, `TimeCode` and `PacketID` come from OptiTrack's documentation. The bridge prints the parsed fields of every trigger and appends each raw packet to `motive_packets.log` next to the script, so you can check them on your Motive version.

## Sync

Network triggering is **not frame-accurate**: the delay is tens to hundreds of ms and varies between takes. Use it to start and stop the camera, not to align the footage.

For alignment, the C70 (BNC TC input) and Motive (eSync 2) are fed the **same SMPTE timecode** from an external timecode device. Align video and mocap in post by timecode.

## Finding more camera endpoints

The C70's API isn't documented. To see what the camera's web UI uses:

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
- **Endpoint list:** `GET /ccapi` returns every endpoint with its allowed methods.

From the camera's web UI (`shoot_script.js`), used by the bridge but **not yet confirmed on the camera**:
- **Shot mark:** `POST /ccapi/ver111/functions/shotmark` `{"type":"shot1mark"}`
- **Clip mark:** `POST /ccapi/ver111/functions/clipmark` `{"type":"okmark"|"checkmark"}`
- **Card:** `GET /ccapi/ver111/devicestatus/storage` → `currentstorage`, `storageinfo[].recordabletime` (min), `recordingformat`
- **Power:** `GET /ccapi/ver111/devicestatus/powersource` → `currentsource`, `sourceinfo[].level_percent`, `remaintime`
From `meta_script.js`, also not yet confirmed on the camera:
- **Metadata to Remote** ("Activate"): `PUT /ccapi/ver111/usermemo/setting` `{"value":"remote"}` (the other value is `"sdcard"`)
- **Metadata state:** `GET /brapi/metadata/uiinfo` → `uiinfo_usermemo.value`, `capability` bits: 1 = current clip, 2 = apply from next, 4 = overwrite previous. It also returns `uiinfo_storage`, `uiinfo_powersource`, `uiinfo_lens`, `uiinfo_recordingmode`.
- **Clip title:** `PUT /ccapi/ver111/usermemo/data` `{"editclip":"current"|"next"|"previous","clipmeta":{"title":"…"}}`. Other `clipmeta` keys: `creator`, `location`, `description`, `altitude`, `latitude`, `longitude`.

## Limitations

- The C70 has a single USB-C port, so the Ethernet adapter and a gimbal control cable can't be connected at the same time.
- Put the camera on a fixed IP on a dedicated network (direct cable or its own switch), not on the OptiTrack camera network.

See [CLAUDE.md](CLAUDE.md) for environment details, project status and open to-dos.
