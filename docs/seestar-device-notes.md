# Seestar S50 Pro - setup and control notes

Researched 2026-09-18. Primary source: ZWO's official S50 Pro manual (V1, Aug 2026):
https://i.zwoastro.com/wp-content/uploads/2026/08/9cdcf7965665984d64c1d1c4c61eef11.pdf

## Phase 1: activate indoors (needs internet on the phone)

1. Charge via USB-C (5V/3A or 12V/3A charger). Firmware updates refuse below 30% battery.
2. Install the **Seestar** app (publisher Suzhou ZWO). Create / sign in to a ZWO account.
   Grant Bluetooth, nearby-devices, location, and local-network permissions.
3. Mount on the tripod (3/8" screw, turn clockwise).
4. First-ever power-on: short press 1s, then hold 2s. Voice says "Powering on, ready for connection."
   Afterwards: hold 2s to power on/off. Hold 6s = forced shutdown (avoid).
5. App -> Connect. It finds the scope over Bluetooth. Pick it, tap Connect, then press the
   reset button on the underside of the scope to confirm pairing.
6. App switches to the scope's own Wi-Fi hotspot: SSID `S50 Pro_xxxxxx`, password `12345678`.
   iPhone joins automatically. Android must join manually in Wi-Fi settings.
7. Online activation runs (mandatory, once). Don't close the app during it. If it fails,
   background the app, get the phone back on real internet, come back, Try Again.
8. Accept the firmware update prompt. Yellow light blinks fast while flashing, then it reboots.

Light meanings: red fast-breathing = not activated yet. Red slow-breathing = idle, no phone.
Solid yellow = connected (alt-az). Solid green = connected (EQ mode).

## Phase 2: put it on home Wi-Fi ("station mode") so the PC can reach it

Menu path (documented for the S50, labels may differ slightly on the Pro):
App -> device settings -> Wi-Fi settings -> Station Mode -> pick home network -> password -> confirm.
Then move the phone back to the home network.

Gotchas:
- Prefer the 2.4 GHz network. 5 GHz works but is the flaky path; DFS channels (52-144) often don't show up.
- Mesh routers with client isolation block PC-to-scope traffic. Check router settings.
- Find its IP in the router's client list. Give it a DHCP reservation so it never changes.
- Wi-Fi range from the scope is short (~10 m open air). Backyard-to-indoors through walls is the real limit.
- Hotspot mode saying "no internet" is normal.

## Phase 3: first night (alt-az, no extra hardware)

- Flat, level surface. 45 degrees of open sky. Don't walk around the tripod while stacking.
- If the app asks for horizontal or compass calibration: open sky, shade the lens from streetlights
  and the Moon, stay away from metal.
- Focus is automatic on both cameras.
- Stargazing -> Tonight's Best -> pick something high -> GOTO. It slews, plate solves, initializes once,
  then stacks 10s frames. Keeps going if the phone disconnects.
- Turn on "Save each frame" in Advanced Settings if you ever want raw frames later. Off by default.
- Dew heater is in settings; costs battery.
- Sun: solar filter is mandatory. Solar System -> Sun -> GOTO, attach filter when prompted.
  Slew the Sun out of view BEFORE removing the filter.

EQ mode (60s exposures) needs the ZWO TH10 wedge + S50 Pro quick-release plate (~$109, not in the box),
a leg pointed true north, and real stars for polar alignment. Skip on night one.

## Controlling it from this PC

Two ways in. Try the first; it's official and needs no hacks.

### A. Built-in ASCOM Alpaca (HTTP REST, port 32323) - recommended
ZWO ships an Alpaca server in the firmware; the S50 Pro markets it as a feature. Standard, public API.

    curl "http://<scope-ip>:32323/management/v1/configureddevices"
    curl "http://<scope-ip>:32323/api/v1/telescope/0/rightascension?ClientID=1&ClientTransactionID=1"
    curl -X PUT "http://<scope-ip>:32323/api/v1/telescope/0/slewtocoordinatesasync" \
      -d "RightAscension=5.5876&Declination=-5.3911&ClientID=1&ClientTransactionID=2"

Devices: Telescope, Camera 0 (telephoto) + Camera 1 (wide), Focuser(s), FilterWheel, Switch (dew heater).
Quirks reported on other models: clock-sync warning + failed slew until the phone app has connected once;
camera subframe/ROI errors out; filter wheel state slow to update.
Reference client (Python, built against an S30 Pro): https://github.com/MBoerlage/SeeStarDirect
INDI driver (alpha): https://github.com/gordtulloch/indi-seestar

### B. Native JSON-RPC protocol (TCP 4700) - legacy, reverse engineered
Ports: 4700 control, 4800 image frames, 4801 logs, 4554 RTSP (`rtsp://<ip>:4554/stream`), UDP 4720 discovery.
Since firmware 7.18 it needs an RSA challenge-response signed with a key extracted from ZWO's Android APK.
Without it the socket connects but every command silently times out.
Tools: seestarpy (pip, handles auth) https://github.com/astronomyk/seestarpy
       seestar_alp (scheduler + web UI on :5432) https://github.com/smart-underworld/seestar_alp
       - seestar_alp has NO S50 Pro code or reports as of 2026-09-18. Expect to debug.
Key extraction is a legal grey area (DMCA 1201(f) interop). Path A avoids it entirely.

### Getting images off
SMB share, guest login:
    sudo mount -t cifs "//<scope-ip>/EMMC Images" /mnt/seestar -o guest,vers=3.0,uid=$(id -u)
    # files under MyWorks/<target>/
Or plug in USB-C: mounts as storage.

### WSL2 note
This machine's WSL2 is in NAT mode. Direct TCP/HTTP to the scope's LAN IP works. UDP broadcast
discovery (Alpaca 32227, Seestar 4720) and `seestar.local` mDNS do NOT. Hardcode the IP.

## Unknowns to verify on the real unit
- Does port 32323 answer on the S50 Pro? (Only confirmed on S30 Pro and S50.)
- Does port 4700 still exist on the Pro, and does the APK key still authenticate?
- Exact station-mode menu labels in the current app.

## Sources
- Product page: https://www.seestar.com/products/seestar-s50-pro-smart-telescope
- Station mode tutorial: https://www.seestar.com/blogs/tutorial/access-the-internet-while-filming
- FAQ: https://www.seestar.com/blogs/faq/s50
- Review: https://astrobackyard.com/seestar-s50-pro-review/
- Cloudy Nights S50 Pro thread: https://www.cloudynights.com/forums/topic/960987-seestar-s50-pro/
- Alpaca on Seestar technical reference: https://www.cloudynights.com/forums/topic/996392-ascom-alpaca-on-the-zwo-seestar-s50-a-technical-reference/
- EQ mode guide: https://agenaastro.com/articles/imaging/how-to-set-up-and-use-eq-mode-on-the-zwo-seestar-s50-s30
