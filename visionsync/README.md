# VisionSync dashboard and integration hub

The mirror's screen, and the service that joins the voice assistant, the
camera/security backend, the phone app and the cloud APIs into one system.

| Folder | What it is |
| :--- | :--- |
| `hub/` | Python service that collects everything and feeds the screen. Standard library only. |
| `magicmirror/modules/MMM-VisionSync/` | MagicMirror2 module: clock, weather, calendar, status, assistant, modes, alerts |
| `magicmirror/config/` | `config.js` and `custom.css` for MagicMirror2 |
| `hub/web/` | The same dashboard as a plain page, served by the hub (needs no Node.js) |
| `deploy/` | Installer, systemd services, full-screen launcher |
| `INTEGRATION.md` | What each teammate's part has to send or expose |

## Try it on any laptop (2 minutes, no keys, no hardware)

```bash
python3 hub/visionsync_hub.py --demo
# open http://127.0.0.1:8765/ in Chrome and press F11
```

The demo walks through: idle, a voice question and answer, internet loss with
old data marked, Security Mode and a person alert, Home Alone with the
"Are you okay?" countdown, recovery, a confirmed fall, camera offline, and
overheating.

## Install on the Jetson

```bash
cd ~ && unzip visionsync-dashboard.zip && cd visionsync
bash deploy/install.sh
nano .env                       # add the keys, see below
sudo systemctl restart visionsync-hub
bash deploy/kiosk.sh            # dashboard full screen
```

Keys in `.env`:

1. `OPENWEATHER_API_KEY`: free key from openweathermap.org.
2. `GOOGLE_CALENDAR_ICS_URL`: Google Calendar, Settings, your calendar,
   "Integrate calendar", "Secret address in iCal format".
3. `MIRROR_SECRET_KEY`: the same value the camera backend and the phone app use.

Put the camera backend in `~/camera_backend` before running the installer and
it gets a service too, started from the same `.env`.

### Hardware this is set up for

NVIDIA Jetson Orin Nano Developer Kit (8GB), A4Tech HD-1085 USB webcam (camera
and microphone in one), LCD behind a two-way mirror in a wooden frame.

- **Display cable.** The Orin Nano kit has a DisplayPort output and no HDMI
  port. For an HDMI or VGA monitor use an *active* DisplayPort adapter.
- **Sound.** The kit has no headphone jack. Use USB speakers.
- **Power.** Use the DC supply that came in the box (the SRS line about a
  5V/4A adapter is for the old Jetson Nano).
- **Microphone.** The webcam's microphone shows up as a normal USB audio
  input; the voice pipeline can use it.

### MagicMirror2 on the Orin Nano

JetPack 5 is Ubuntu 20.04 and JetPack 6 is Ubuntu 22.04 (check with
`cat /etc/os-release`). Both can run current Node.js, so the normal
MagicMirror2 install applies:

```bash
# Node.js 22 (see nodesource.com for the current command), then:
cd ~ && git clone https://github.com/MagicMirrorOrg/MagicMirror
cd MagicMirror && npm run install-mm
cd ~/visionsync && bash deploy/install.sh     # copies the module and config
```

`deploy/magicmirror.service` runs MagicMirror2 in server mode and
`deploy/kiosk.sh` shows it full screen in Chromium. If MagicMirror2 is not
installed or not running, `kiosk.sh` opens the hub's own page instead
(`http://127.0.0.1:8765/`): same widgets, same layout, no Node.js.

### Wi-Fi setup without a keyboard

`wifi/visionsync_wifi.py` (service `visionsync-wifi`, runs as root). When the
mirror has no network for 60 seconds it hosts its own Wi-Fi,
`VisionSync-Setup`, and shows a QR code. Scan it with a phone camera, the
setup page opens by itself, pick the home Wi-Fi and type its password. A wrong
password shows on the mirror and setup starts again. If nobody sets it up in
10 minutes it checks the known networks again.

- Start it on purpose (demo): `sudo python3 wifi/visionsync_wifi.py --setup-now`
- The voice assistant can start it too: `vs.wifi_setup()` (see INTEGRATION.md).
- The Wi-Fi card must support hosting a network. The installer checks.
- Tests: `python3 -m unittest discover -s wifi/tests` (12 tests, with a
  pretend NetworkManager; not yet run on the Jetson's real Wi-Fi card).

### Glowing background

`AMBIENT` in `.env`: `off` (pure black), `subtle` (default) or `vivid`.
On a two-way mirror black areas reflect and lit areas show the screen, so a
brighter background means a weaker reflection. Try all three on the real
glass. The glow turns blue and rises while the assistant listens, and
switches off during a fall or security alert. To compare quickly, open
`http://127.0.0.1:8765/?ambient=vivid`.

## What is on the screen

| Position | Widget | Source |
| :--- | :--- | :--- |
| top centre | Clock and date | Mirror's own clock |
| top right | Weather: temperature, icon, condition, humidity, next 12 hours | OpenWeatherMap |
| bottom left | Next events, the first two larger | Google Calendar |
| top left | Assistant mode (cloud / on this mirror), Wi-Fi, temperature, camera | Hub |
| lower third | Voice assistant: listening, thinking, the spoken reply | Voice pipeline |
| bottom right | Security mode / Home alone mode when on | Camera backend |
| behind everything | Slow glowing background (off / subtle / vivid) | Hub setting |
| full screen | Wi-Fi setup with QR code, only when there is no network | Wi-Fi service |
| full screen | "Are you okay?" countdown, fall detected, person detected, camera offline | Camera backend |

## SRS coverage of this part

| Requirement | How |
| :--- | :--- |
| UC-1 time, weather, humidity, next events | clock, weather, calendar widgets |
| UC-1 alt 1: network lost, cached data with staleness mark | data cached, dimmed, amber "Offline. Showing data from 3:04 PM"; clock keeps running |
| UC-1 alt 2: "Calendar unavailable, check connection." | shown word for word, other widgets unaffected |
| NFR-P2 weather 10 min, calendar 5 min | `WEATHER_INTERVAL`, `CALENDAR_INTERVAL`; early retry when the internet returns |
| UC-2 voice states visible on the mirror | assistant widget + `visionsync_client.py` |
| UC-3 modes, countdown, emergencies, camera offline on the mirror | guard and alerts widgets, read from the camera backend |
| UC-3 A4 local alarm when the internet is down | alerts widget sounds a tone on a confirmed emergency while offline |
| UC-4 / NFR-P4 notice internet loss within 2 s | checked every 0.5 s with a 1 s limit; mode switches to "on this mirror"; voice code reads `vs.online()` |
| SRS 4.1 app Status tab shows AI mode | hub writes the mode to the camera backend |
| SRS 4.4 Unix socket between MagicMirror and Python | `/tmp/visionsync.sock`, JSON lines |
| NFR-S1 throttle above 85 C | animations stop, stream drops to 480p, back to normal under 75 C |
| NFR-S3 clean shutdown | SIGTERM handled, socket removed |
| NFR-SEC1 keys outside the code | `.env`, mode 600, git-ignored |
| NFR-SEC2 local only | hub and MagicMirror listen on 127.0.0.1 |
| NFR-SEC3 no stored conversations | text kept in memory only, cleared from screen after 20 s |
| NFR-Q1 / Q2 / Q5 | each part restarts alone; hub workers restart after an error; widgets keep last data if the hub stops |

The SRS states two different refresh times (30 s in UC-1 step 3, 10 and 5
minutes in NFR-P2). The NFR-P2 values are the defaults because 30 s would use
up the free OpenWeatherMap quota; both are one line in `.env`.

## Tests

```bash
python3 -m unittest discover -s hub/tests -v     # 33 tests, no network needed
```

## Useful commands

```bash
journalctl -u visionsync-hub -f              # hub log
curl -s http://127.0.0.1:8765/api/state      # everything the screen knows
python3 hub/visionsync_client.py             # sends a test conversation to the mirror
```

## Licences

Roboto and Roboto Condensed fonts: Apache 2.0 (`fonts/LICENSE-roboto.txt`).
MagicMirror2: MIT.
