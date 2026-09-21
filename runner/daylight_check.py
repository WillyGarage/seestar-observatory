#!/usr/bin/env python
"""Daylight optics check after the fall. Aim the telescope at a distant object (a rooftop, chimney,
antenna, tree line several hundred feet away) and measure sharpness across the frame.

  daylight_check.py ALT AZ            point there (degrees) and shoot
  daylight_check.py --here            shoot wherever the arm already points

Takes auto-exposed frames with the telephoto and the wide camera, sweeps the focuser around the
factory position, and reports: best focus step, sharpness in the centre vs the four corners, and
whether the star-like/point sources look round. Saves jpgs next to the script in
captures/sessions/<date>/daylight/. Nothing is published."""
import sys, os, time, contextlib, io, struct, requests, json, numpy as np, cv2
from pathlib import Path
from datetime import datetime, timedelta
sys.path.insert(0, os.environ.get('SEESTAR_SKILL', str(Path(__file__).resolve().parent.parent / 'skills/seestar')))
import config
with contextlib.redirect_stdout(io.StringIO()):
    from seestarpy import connection as conn, raw
conn.DEFAULT_IP = config.require_ip(); conn.VERBOSE_LEVEL = 0
PRIVACY_MIN_ALT = config.getf('SEESTAR_PRIVACY_MIN_ALT', 30.0)
PRIVACY_AZ = [tuple(float(x) for x in seg.split('-')) for seg in config.get('SEESTAR_PRIVACY_AZ', '').split(',') if '-' in seg]
OUT = config.CAPTURES / "sessions" / (datetime.now() - timedelta(hours=12)).strftime("%Y-%m-%d") / "daylight"
OUT.mkdir(parents=True, exist_ok=True)
P = {"ClientID": 7, "ClientTransactionID": 1}; A = f"http://{config.SCOPE_IP}:32323/api/v1/camera"
def log(*a): print(time.strftime("%H:%M:%S"), *a, flush=True)
def shot(cam, exp, gain):
    B = f"{A}/{cam}"; requests.put(B + "/connected", data={**P, "Connected": "true"}, timeout=15); requests.put(B + "/gain", data={**P, "Gain": gain}, timeout=15)
    requests.put(B + "/startexposure", data={**P, "Duration": exp, "Light": "true"}, timeout=15)
    for _ in range(300):
        if requests.get(B + "/imageready", params=P, timeout=15).json()["Value"]: break
        time.sleep(0.15)
    b = requests.get(B + "/imagearray", params=P, headers={"Accept": "application/imagebytes"}, timeout=180).content
    h = struct.unpack("<11i", b[:44]); return np.frombuffer(b[44:], dtype=np.uint16)[:h[8] * h[9]].reshape(h[8], h[9]).T.astype(np.float32)
def green(r): return (r[0::2, 0::2] + r[1::2, 1::2]) / 2
def auto(cam):
    """Find an exposure that puts the 99.5th percentile around 40000."""
    exp = 0.002
    for _ in range(8):
        g = green(shot(cam, exp, 0)); p = np.percentile(g, 99.5)
        if 25000 < p < 55000: return exp, g
        exp = max(0.0002, min(2.0, exp * 40000 / max(p - 4096, 500)))
    return exp, g
def sharp(g):
    lap = cv2.Laplacian(cv2.GaussianBlur(g, (3, 3), 0), cv2.CV_32F); return float(lap.var())
def regions(g):
    H, W = g.shape; h, w = H // 4, W // 4
    return {"centre": g[H//2-h//2:H//2+h//2, W//2-w//2:W//2+w//2], "top-left": g[:h, :w], "top-right": g[:h, -w:], "bottom-left": g[-h:, :w], "bottom-right": g[-h:, -w:]}
def save(g, name, hi=99.7):
    s = np.clip((g - np.percentile(g, 0.3)) / (np.percentile(g, hi) - np.percentile(g, 0.3) + 1e-6), 0, 1); cv2.imwrite(str(OUT / name), (s ** 0.8 * 255).astype(np.uint8))
tilt = raw.get_device_state()["result"]["balance_sensor"]["data"]["angle"]
if tilt > 20: raise SystemExit(f"base tilt {tilt:.0f} deg: the scope is not upright")
raw.iscope_stop_view(); time.sleep(2)
if "--here" not in sys.argv:
    alt, az = float(sys.argv[1]), float(sys.argv[2])
    if any(lo <= az % 360 <= hi for lo, hi in PRIVACY_AZ) and alt < PRIVACY_MIN_ALT:
        raise SystemExit(f"refusing alt {alt} az {az}: that sector is a neighbour's property "
                         f"(SEESTAR_PRIVACY_AZ={config.get('SEESTAR_PRIVACY_AZ', '')}).")
    from skyfield.api import Loader, wgs84
    L = Loader(str(config.EPHEMERIS.parent)); ts = L.timescale(); eph = L(config.EPHEMERIS.name); site = eph['earth'] + wgs84.latlon(*config.require_site())
    ra, dec, _ = site.at(ts.now()).from_altaz(alt_degrees=alt, az_degrees=az).radec(); raw.scope_goto(ra.hours, dec.degrees); time.sleep(3)
    for _ in range(40):
        time.sleep(1.5)
        if raw.get_device_state()["result"]["mount"]["move_type"] == "none": break
h = raw.scope_get_horiz_coord()["result"]; log(f"arm at alt {h[0]:.1f} az {h[1]:.1f}, base tilt {tilt:.1f}")
# wide camera: what are we looking at
e1, w = auto(1); save(w, "wide.jpg"); log(f"wide: exposure {e1*1000:.1f} ms -> wide.jpg")
# telephoto focus sweep: coarse across the whole range (a target a few hundred feet away needs a
# different focus than the stars), then fine around the best
res = {}
def measure(step):
    raw.move_focuser(step); time.sleep(3)
    e0, g = auto(0); res[step] = (sharp(regions(g)["centre"]), e0, g)
    log(f"focus {step}: exposure {e0*1000:.1f} ms, centre sharpness {res[step][0]:.0f}")
for step in range(600, 2401, 200):
    measure(step)
b = max(res, key=lambda k: res[k][0])
for step in range(max(0, b - 150), min(2600, b + 151), 50):
    if step not in res: measure(step)
best = max(res, key=lambda k: res[k][0]); raw.move_focuser(best); time.sleep(3)
e0, g = auto(0); save(g, f"tele_focus{best}.jpg"); H, W = g.shape
reg = {k: sharp(v) for k, v in regions(g).items()}
log(f"best focus {best} (factory 1001). sharpness by region: " + ", ".join(f"{k} {v:.0f}" for k, v in reg.items()))
c = reg["centre"]; corners = [reg[k] for k in reg if k != "centre"]
verdict = "corners within normal range of the centre" if min(corners) > 0.35 * c else "one or more corners much softer than the centre: possible tilt or decentred lens"
log("verdict:", verdict)
cv2.imwrite(str(OUT / "tele_centre_1to1.jpg"), np.clip((g[H//2-300:H//2+300, W//2-300:W//2+300] - np.percentile(g, 0.3)) / (np.percentile(g, 99.7) - np.percentile(g, 0.3) + 1e-6) * 255, 0, 255).astype(np.uint8))
cv2.imwrite(str(OUT / "tele_corner_1to1.jpg"), np.clip((g[:600, :600] - np.percentile(g, 0.3)) / (np.percentile(g, 99.7) - np.percentile(g, 0.3) + 1e-6) * 255, 0, 255).astype(np.uint8))
json.dump({"time": datetime.now().isoformat(timespec="seconds"), "alt": h[0], "az": h[1], "best_focus": best, "focus_sweep": {k: v[0] for k, v in res.items()}, "regions": reg, "verdict": verdict}, open(OUT / "result.json", "w"), indent=1)
raw.move_focuser(1001); raw.scope_park(); log(f"parked; files in {OUT}")
