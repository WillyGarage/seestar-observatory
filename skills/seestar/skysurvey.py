#!/usr/bin/env python
"""Horizon survey: point the wide camera round the sky, save each patch."""
import sys, time, json, struct, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import numpy as np, requests, contextlib, io
with contextlib.redirect_stdout(io.StringIO()):
    from seestarpy import connection as conn, raw
import config
conn.DEFAULT_IP = config.require_ip()

from skyfield.api import load, wgs84, Star
import cv2
from PIL import Image

LAT, LON = config.require_site()
OUT = Path(os.environ.get("SURVEY_OUT", config.CAPTURES / "skysurvey"))
OUT.mkdir(parents=True, exist_ok=True)
ALPACA = f"http://{config.SCOPE_IP}:32323/api/v1"
P = {"ClientID": 7, "ClientTransactionID": 1}

ts = load.timescale()
eph = load(str(config.EPHEMERIS))
site = eph["earth"] + wgs84.latlon(LAT, LON)

def altaz_to_radec(alt_deg, az_deg, t=None):
    t = t or ts.now()
    pos = site.at(t).from_altaz(alt_degrees=alt_deg, az_degrees=az_deg)
    ra, dec, _ = pos.radec()
    return ra.hours, dec.degrees

def alp_put(path, data, timeout=30):
    return requests.put(f"{ALPACA}/{path}", data={**P, **data}, timeout=timeout).json()
def alp_get(path, timeout=15):
    return requests.get(f"{ALPACA}/{path}", params=P, timeout=timeout).json()

def shot(cam, dur):
    B = f"camera/{cam}"
    r = alp_put(B + "/startexposure", {"Duration": str(dur), "Light": "true"})
    if r["ErrorNumber"]: raise RuntimeError(r["ErrorMessage"])
    for _ in range(200):
        time.sleep(0.25)
        if alp_get(B + "/imageready").get("Value"): break
    b = requests.get(f"{ALPACA}/{B}/imagearray", params=P,
                     headers={"Accept": "application/imagebytes"}, timeout=180).content
    h = struct.unpack("<11i", b[:44])
    if h[1]: raise RuntimeError(f"imagearray error {h[1]}")
    return np.frombuffer(b[h[4]:], dtype=np.uint16).reshape((h[8], h[9])).T.astype(np.float32)

def slew(alt, az, tol=2.0, timeout=70):
    ra, dec = altaz_to_radec(alt, az)
    raw.scope_goto(ra, dec)
    t0 = time.time(); last = None
    while time.time() - t0 < timeout:
        time.sleep(1.5)
        h = raw.scope_get_horiz_coord().get("result")
        if not h: continue
        last = h
        dalt = abs(h[0] - alt)
        daz = abs((h[1] - az + 180) % 360 - 180)
        if dalt < tol and daz < tol: return h, time.time() - t0
    return last, time.time() - t0

def save(arr, name, gamma=2.2):
    rgb = cv2.cvtColor(np.clip(arr,0,65535).astype(np.uint16), cv2.COLOR_BayerGB2RGB).astype(np.float32)
    lo, hi = np.percentile(rgb, 1), np.percentile(rgb, 99.8)
    out = np.clip((rgb-lo)/(hi-lo+1e-6), 0, 1)
    img = (out**(1/gamma)*255).astype(np.uint8)
    Image.fromarray(img).save(OUT/f"{name}.jpg", quality=88)
    np.save(OUT/f"{name}.npy", arr.astype(np.uint16))
    return img.shape, float(np.median(rgb)), float(np.percentile(rgb,99.9))

# ----------------------------------------------------------------------------- contact sheets
def sheet(frames_dir, alt, out, title=None, cols=4):
    """Compose the frames of one altitude ring into a labelled contact sheet with every frame
    UPRIGHT (the wide camera is portrait: sky at the top, ground at the bottom). the standing rule
    2026-09-20: horizon pictures must be displayed the way you would see them standing there."""
    from PIL import ImageDraw, ImageFont
    frames_dir = Path(frames_dir)
    names = ["N", "NNE", "ENE", "E", "ESE", "SSE", "S", "SSW", "WSW", "W", "WNW", "NNW"]
    files = sorted(frames_dir.glob(f"alt{int(alt):02d}_az*.jpg"), key=lambda p: int(p.stem.split("az")[1]))
    if not files:
        raise SystemExit(f"no alt{int(alt):02d}_az*.jpg in {frames_dir}")
    cw, ch, pad, label_h = 300, 533, 12, 26
    rows = (len(files) + cols - 1) // cols
    W = cols * (cw + pad) + pad; H = 44 + rows * (ch + label_h + pad) + pad
    img = Image.new("RGB", (W, H), (246, 247, 249)); d = ImageDraw.Draw(img)
    try:
        f_title = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
        f_lab = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 15)
    except Exception:
        f_title = f_lab = ImageFont.load_default()
    d.text((pad, 10), title or f"Looking {int(alt)} degrees above the horizon", fill=(27, 29, 33), font=f_title)
    for i, p in enumerate(files):
        az = int(p.stem.split("az")[1]); name = names[(az // 30) % 12] if az % 30 == 0 else ""
        fr = Image.open(p).convert("RGB")
        if fr.width > fr.height:            # a frame that was saved landscape: stand it up
            fr = fr.rotate(90, expand=True)
        fr.thumbnail((cw, ch))
        x = pad + (i % cols) * (cw + pad); y = 44 + (i // cols) * (ch + label_h + pad)
        d.text((x, y), f"{name}  ({az} deg)", fill=(27, 29, 33), font=f_lab)
        img.paste(fr, (x + (cw - fr.width) // 2, y + label_h))
    img.save(out, quality=88)
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet", help="no scope: build a contact sheet from FRAMES_DIR for --alt (one value), write to --out")
    ap.add_argument("--out"); ap.add_argument("--title")
    ap.add_argument("--alt", type=float, nargs="+", required=True)
    ap.add_argument("--az", type=float, nargs="*", default=None)
    ap.add_argument("--exp", type=float, default=3.0)
    ap.add_argument("--cam", type=int, default=1)
    ap.add_argument("--auto", action="store_true", help="auto-adjust exposure per point (twilight)")
    a = ap.parse_args()
    if a.sheet:
        print(sheet(a.sheet, a.alt[0], a.out or f"sheet_alt{int(a.alt[0]):02d}.jpg", a.title)); raise SystemExit
    if not a.az:
        raise SystemExit("--az is required for a survey")
    alp_put(f"camera/{a.cam}/connected", {"Connected":"true"})
    meta = []
    for alt in a.alt:
        for az in a.az:
            t0 = time.time()
            pos, st = slew(alt, az)
            exp = a.exp
            f = shot(a.cam, exp)
            if a.auto:
                # Twilight changes fast and every direction differs. Aim for a median
                # around a quarter scale so solids stay dark and sky stays bright.
                for _ in range(5):
                    med = float(np.median(f))
                    if 4000 < med < 50000: break
                    exp = max(0.0005, min(8.0, exp * (16000.0 / max(med, 200.0))))
                    f = shot(a.cam, exp)
                print(f"   exposure {exp*1000:.1f} ms (median {float(np.median(f)):.0f})", flush=True)
            nm = f"alt{int(alt):02d}_az{int(az):03d}"
            shape, med, p999 = save(f, nm)
            dt = time.time()-t0
            print(f"{nm}: actual alt={pos[0]:.1f} az={pos[1]:.1f} slew={st:.0f}s total={dt:.0f}s "
                  f"shape={shape} median={med:.0f} p99.9={p999:.0f}", flush=True)
            meta.append(dict(name=nm, alt=alt, az=az, actual=pos, median=med, p999=p999, exp=exp))
    json.dump(meta, open(OUT/"meta.json","w"), indent=1, default=str)
