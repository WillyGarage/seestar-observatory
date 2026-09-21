#!/usr/bin/env python
"""Live tilt readout for levelling the scope base.

The balance sensor returns a gravity vector (x, y, z) in the scope's own body
frame plus the total tilt angle. z is up, so (x, y) points toward the LOW side.
Poll it and print a bar so someone standing at the scope can shim by feel.
"""
import sys
from pathlib import Path, time, math, contextlib, io, argparse
sys.path.insert(0, str(Path(__file__).resolve().parent))
with contextlib.redirect_stdout(io.StringIO()):
    from seestarpy import connection as conn, raw
conn.DEFAULT_IP = config.require_ip()

GOOD, OK = 1.0, 2.5

def read():
    with contextlib.redirect_stdout(io.StringIO()):
        st = raw.get_device_state()["result"]
    b = st["balance_sensor"]["data"]
    c = st["compass_sensor"]["data"]
    return b["x"], b["y"], b["z"], b["angle"], c["direction"]

def bar(a, width=40, full=10.0):
    n = min(width, int(round(a / full * width)))
    mark = int(round(GOOD / full * width))
    s = ["-"] * width
    for i in range(n): s[i] = "#"
    if mark < width: s[mark] = "|"
    return "".join(s)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", type=float, default=600, help="how long to keep reading")
    ap.add_argument("--every", type=float, default=1.5)
    ap.add_argument("--onchange", type=float, default=0.0,
                    help="only print when the tilt moves at least this many degrees")
    ap.add_argument("--heartbeat", type=float, default=30.0,
                    help="with --onchange, still print this often so it's clear we're alive")
    a = ap.parse_args()
    t0 = time.time(); best = 999.0; prev = None; shown = None; last_print = 0.0
    while time.time() - t0 < a.secs:
        try:
            x, y, z, ang, comp = read()
        except Exception as e:
            print("  (read failed, retrying)", e, flush=True); time.sleep(2); continue
        # direction of the low side, in the scope's body frame
        body = (math.degrees(math.atan2(y, x)) + 360) % 360
        trend = "" if prev is None else ("  BETTER" if ang < prev - 0.05 else
                                         ("  worse" if ang > prev + 0.05 else "  (no change)"))
        best = min(best, ang)
        state = "LEVEL" if ang <= GOOD else ("close" if ang <= OK else "off")
        now = time.time()
        quiet = a.onchange > 0 and shown is not None and abs(ang - shown) < a.onchange \
                and now - last_print < a.heartbeat
        if not quiet:
            print(f"{time.strftime('%H:%M:%S')}  tilt {ang:5.2f}deg  [{bar(ang)}]  {state:5}"
                  f"  low-side {body:5.1f} (body)  best {best:4.2f}{trend}", flush=True)
            shown, last_print = ang, now
        prev = ang
        time.sleep(a.every)
