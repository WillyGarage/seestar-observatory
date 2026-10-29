#!/usr/bin/env python
"""Scope agent: feeds the site's /admin/scope page.

The site never reaches into this machine. This agent polls the site's private API for what the
owner asked for on /admin/scope:
  - live_until: while in the future, read the scope about once a second and post the readings
  - polar: measure polar alignment (EQ mode, night only) and post the result
Idle, it only asks the site every few seconds and leaves the scope alone.

Run: skills/seestar/.venv/bin/python runner/scope_agent.py   (systemd unit: seestar-agent.service)
Site + token come from ~/.config/seestar/public-site.env, or SCOPE_AGENT_SITE_URL / SCOPE_AGENT_TOKEN.
"""
import contextlib
import io
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SKILL = Path(os.environ.get("SEESTAR_SKILL", Path(__file__).resolve().parent.parent / "skills/seestar"))
sys.path.insert(0, str(SKILL))
import config  # noqa: E402
import public_site  # noqa: E402
with contextlib.redirect_stdout(io.StringIO()):
    from seestarpy import connection as conn, raw  # noqa: E402
conn.DEFAULT_IP = config.require_ip(); conn.VERBOSE_LEVEL = 0
import numpy as np  # noqa: E402
from skyfield.api import Star, load, wgs84  # noqa: E402

LAT, LON = config.require_site()
TILT_TARGET = round(90 - LAT, 1)       # base lean on the wedge
IDLE_POLL, LIVE_POLL = 3.0, 1.0
PA_SWING_SECS = 2                       # base-motor seconds at speed 1500 (~6 deg/s) between solves

TS = load.timescale()
EPH = load(str(config.EPHEMERIS))
SITE = EPH["earth"] + wgs84.latlon(LAT, LON)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def site_cfg():
    url, tok = os.environ.get("SCOPE_AGENT_SITE_URL"), os.environ.get("SCOPE_AGENT_TOKEN")
    if url and tok:
        return {"url": url.rstrip("/"), "token": tok}
    return public_site.load_config()


CFG = site_cfg()


def api(method, path, body=None, timeout=6):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(CFG["url"] + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {CFG['token']}")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


import threading
SCOPE_LOCK = threading.RLock()
STATE = {"pa_state": None, "pa_hint": None, "live_until": 0, "now_offset": 0.0}


def q(fn, *a, **kw):
    with SCOPE_LOCK, contextlib.redirect_stdout(io.StringIO()):
        return fn(*a, **kw)


# ---------- reading the scope ----------
STAGE_WORDS = {"Stack": "Photographing", "AutoGoto": "Going to the target", "Initialise": "Getting ready to photograph",
               "ContinuousExposure": "Live view", "ScopeGoto": "Moving"}


def read_scope(extra=None):
    out = {"reachable": True}
    try:
        st = q(raw.get_device_state, keys=["pi_status", "balance_sensor", "mount", "station", "storage", "focuser"])["result"]
        pi, mnt = st["pi_status"], st["mount"]
        out.update(battery=pi.get("battery_capacity"), charge_online=pi.get("charge_online"),
                   charger_status=pi.get("charger_status"), battery_temp=pi.get("battery_temp"),
                   temp=round(pi.get("temp", 0), 1), tilt=round(st["balance_sensor"]["data"]["angle"], 2),
                   tilt_target=TILT_TARGET, lat=LAT, eq_mode=mnt.get("equ_mode"), tracking=mnt.get("tracking"),
                   move_type=mnt.get("move_type"), parked=mnt.get("close"),
                   wifi_dbm=(st.get("station") or {}).get("sig_lev"),
                   focus=(st.get("focuser") or {}).get("step"))
        sto = st.get("storage") or {}
        for v in (sto.get("storage_volume") or []):
            if isinstance(v, dict) and v.get("free_mb") is not None:
                out["storage_free_gb"] = round(v["free_mb"] / 1024); break
        try:
            h = q(raw.scope_get_horiz_coord)["result"]; e = q(raw.scope_get_equ_coord)["result"]
            out.update(alt=round(h[0], 1), az=round(h[1], 1), ra=round(e["ra"], 3), dec=round(e["dec"], 2))
        except Exception:
            pass
        v = q(raw.get_view_state).get("result", {}).get("View", {})
        stage = v.get("stage")
        if v.get("state") == "working" and stage:
            out["activity"] = STAGE_WORDS.get(stage, stage); out["target"] = v.get("target_name")
            stk = v.get("Stack") or {}
            out["frames"] = stk.get("stacked_frame"); out["dropped"] = stk.get("dropped_frame")
        else:
            out["activity"] = "Parked" if mnt.get("close") else "Idle"
    except Exception as ex:
        out = {"reachable": False, "error": str(ex).splitlines()[0][:120]}
    if extra:
        out.update(extra)
    return out


# ---------- polar alignment (3 plate solves turning only the polar axis) ----------
def solve(tries=3):
    for _ in range(tries):
        prev = (q(raw.get_solve_result).get("result") or {}).get("ra_dec")
        q(raw.start_solve); res = {}; seen_working = False
        for _ in range(30):
            time.sleep(1.5)
            res = (q(raw.get_solve_result).get("result") or {})
            if res.get("state") == "working":
                seen_working = True
            if res.get("state") in ("complete", "fail") and (seen_working or res.get("ra_dec") != prev):
                break
        if res.get("state") == "complete" and (seen_working or res.get("ra_dec") != prev):
            ra, dec = res["ra_dec"]
            alt, az = altaz_now(ra, dec)
            LAST_RADEC[0] = (ra, dec)
            return alt, az
    return None


LAST_RADEC = [None]


def fast_solve(limit=5.0):
    """One quick plate solve of the current frame; abandons it after `limit` s (a smeared frame
    while the head is moving otherwise costs the scope's full 30 s timeout). Returns (ra, dec)."""
    prev = (q(raw.get_solve_result).get("result") or {}).get("ra_dec")
    q(raw.start_solve); t0 = time.time(); seen = False
    while time.time() - t0 < limit:
        time.sleep(0.3)
        res = q(raw.get_solve_result).get("result") or {}
        if res.get("state") == "working":
            seen = True
        elif res.get("state") == "complete" and (seen or res.get("ra_dec") != prev):
            return tuple(res["ra_dec"])
        elif res.get("state") == "fail" and seen:
            return None
    try:
        q(raw.stop_solve)
    except Exception:
        pass
    return None


def unit(alt, az):
    a, z = math.radians(alt), math.radians(az)
    return np.array([math.cos(a) * math.cos(z), math.cos(a) * math.sin(z), math.sin(a)])   # x=N, y=E, z=up


def altaz_of(v):
    return math.degrees(math.asin(max(-1, min(1, v[2])))), math.degrees(math.atan2(v[1], v[0]))


def turn(v, d_az, d_tilt):
    """The head's two knobs as rotations: tilt about the east-west axis (positive raises a
    north-pointing axis), then azimuth about the vertical (positive turns toward east)."""
    e, d = math.radians(d_tilt), math.radians(d_az)
    x, y, z = v
    x, z = x * math.cos(e) - z * math.sin(e), x * math.sin(e) + z * math.cos(e)
    x, y = x * math.cos(d) - y * math.sin(d), x * math.sin(d) + y * math.cos(d)
    return np.array([x, y, z])


def knob_change(p_ref, p_cur):
    """Find the azimuth and tilt turns that carry the reference star field onto the current one."""
    k = np.zeros(2)
    for _ in range(8):
        f = turn(p_ref, *k) - p_cur
        J = np.zeros((3, 2))
        for i in range(2):
            dk = np.zeros(2); dk[i] = 1e-3
            J[:, i] = (turn(p_ref, *(k + dk)) - turn(p_ref, *k)) / 1e-3
        step, *_ = np.linalg.lstsq(J, -f, rcond=None); k += step
        if np.abs(step).max() < 1e-4:
            break
    return k   # (d_az, d_tilt) in degrees


def altaz_now(ra, dec):
    alt, az, _ = SITE.at(TS.now()).observe(Star(ra_hours=ra, dec_degrees=dec)).apparent().altaz()
    return alt.degrees, az.degrees


def vec(alt, az):
    a, z = math.radians(alt), math.radians(az)
    return np.array([math.cos(a) * math.cos(z), math.cos(a) * math.sin(z), math.sin(a)])


def wait_still(limit=60):
    for _ in range(limit):
        time.sleep(1)
        if q(raw.get_device_state, keys=["mount"])["result"]["mount"]["move_type"] == "none":
            return


def polar(post):
    """post(text) shows progress on the page. Returns the result dict or None."""
    d = q(raw.get_device_state, keys=["mount"])["result"]["mount"]
    if not d.get("equ_mode"):
        post("Not in tilted (EQ) mode, so there is no polar axis to measure. Switch modes first."); return None
    v = q(raw.get_view_state).get("result", {}).get("View", {})
    if v.get("state") == "working" and v.get("stage") == "Stack":
        post("The scope is photographing. Stop that first, then measure."); return None
    sun = SITE.at(TS.now()).observe(EPH["sun"]).apparent().altaz()[0].degrees
    if sun > -11.5:
        post(f"Too light for the star measurement (sun {abs(sun):.0f}° {'down' if sun < 0 else 'up'}). It works once the sun is about 12° down.")
        return None
    if d.get("close"):
        post("Opening the arm and pointing up…")
        q(raw.scope_move_to_horizon); wait_still()
        lst = TS.now().gmst + LON / 15.0
        q(raw.scope_goto, lst % 24, LAT - 15.0); wait_still(90)
    if not (v.get("state") == "working"):
        q(conn.send_command, {"method": "iscope_start_view", "params": {"mode": "star"}}); time.sleep(6)
    q(raw.scope_set_track_state, True)
    # After a park the scope may not know where it points; find open sky by turning the polar
    # axis 45 deg at a time until a star photo solves, then sync to it.
    for k in range(8):
        post("Looking for stars…" if k == 0 else f"No stars there; turning to look elsewhere ({k}/7)…")
        p0 = solve(tries=1)
        if p0 is not None:
            res0 = q(raw.get_solve_result).get("result") or {}
            if res0.get("ra_dec"):
                q(raw.scope_sync, *res0["ra_dec"])
            break
        q(raw.scope_speed_move, 0, 1500, 7); time.sleep(10)
    else:
        post("Couldn't find any stars in 8 directions. Is it dark and clear?"); return None
    pts = []; offset = 0          # base-motor seconds west of the start position
    for i, (angle, secs) in enumerate([(None, 0), (0, PA_SWING_SECS), (180, 2 * PA_SWING_SECS)]):
        if angle is not None:
            post(f"Turning the polar axis for star photo {i + 1} of 3…")
            q(raw.scope_speed_move, angle, 1500, secs); time.sleep(secs + 3)
            offset += secs if angle == 0 else -secs
        post(f"Star photo {i + 1} of 3: matching stars…")
        p = solve()
        if p is None:
            post(f"Couldn't match stars on photo {i + 1}. Is it dark, and is the sky clear above the scope? Point it at open sky and try again.")
            break
        pts.append(p)
    if offset:   # back to where it started
        q(raw.scope_speed_move, 180 if offset > 0 else 0, 1500, abs(offset)); time.sleep(abs(offset) + 1)
    if len(pts) < 3:
        return None
    v0, v1, v2 = (vec(*p) for p in pts)
    n = np.cross(v1 - v0, v2 - v0); n /= np.linalg.norm(n)
    if n[0] < 0:
        n = -n
    alt = math.degrees(math.asin(n[2])); az = math.degrees(math.atan2(n[1], n[0]))
    res = {"axis_alt": round(alt, 2), "axis_az": round(az, 2), "alt_err": round(alt - LAT, 2), "az_err": round(az, 2),
           "total": round(math.hypot(alt - LAT, az * math.cos(math.radians(LAT))), 2), "points": [[round(a, 2), round(b, 2)] for a, b in pts]}
    try:
        t0 = q(raw.get_device_state, keys=["balance_sensor"])["result"]["balance_sensor"]["data"]["angle"]
        res["tilt_at"] = round(t0, 2); res["tilt_c"] = round(res["axis_alt"] - (90 - t0), 2)   # axis_alt = 90 - tilt + c
    except Exception:
        pass
    res["measured_ts"] = time.time(); res["measured_total"] = res["total"]
    post(f"Measured: left/right {abs(res['az_err']):.1f}°, up/down {abs(res['alt_err']):.1f}°. Adjust now; numbers update live.")
    return res


def polar_live(res, keep_going, post):
    """After a measurement, re-solve as fast as the scope allows and report the polar axis live.
    Tracking stays on (with it off, stars smear and matching fails); comparing the current and
    reference star fields at the same instant cancels the sky's motion. Turning a knob rotates the
    whole mount, so the rotation that carries the reference field onto the current one is exactly
    the rotation applied to the polar axis. Up/down is also given instantly by the tilt sensor."""
    q(raw.scope_set_track_state, True); time.sleep(1)
    ref = None
    for _ in range(6):
        ref = fast_solve(limit=8)
        if ref:
            break
    if ref is None:
        post("Live updates paused: couldn't see stars from here."); return
    axis0 = unit(res["axis_alt"], res["axis_az"])
    n = 0; misses = 0
    while keep_going():
        cur = fast_solve()
        if cur is None:
            misses += 1
            if misses > 1:
                STATE["pa_state"], STATE["pa_hint"] = "Hold the head still for a moment: re-finding stars.", "Numbers resume in a few seconds."
            continue
        misses = 0
        p_ref = unit(*altaz_now(*ref)); p_cur = unit(*altaz_now(*cur))
        d_az, d_tilt = knob_change(p_ref, p_cur)
        a_alt, a_az = altaz_of(turn(axis0, d_az, d_tilt))
        live = dict(res, az_err=round(a_az, 2), alt_err=round(a_alt - LAT, 2), axis_alt=round(a_alt, 2), axis_az=round(a_az, 2),
                    live=True, ts=time.time())
        live["total"] = round(math.hypot(live["alt_err"], live["az_err"] * math.cos(math.radians(LAT))), 2)
        try:
            Path("/tmp/seestar_pa_latest.json").write_text(json.dumps(live))
        except Exception:
            pass
        STATE["pa_state"], STATE["pa_hint"] = "Adjust now: numbers update live.", "Small turns, then hold still about 5 s."
        try:
            api("POST", "/api/scope/pa", live, timeout=4)
        except Exception:
            pass
        n += 1
        if n % 10 == 1:
            log(f"live PA: az_err {live['az_err']:+.2f} alt_err {live['alt_err']:+.2f}")


def feeder():
    """Posts a reading about once a second while the live window is open, no matter what else
    the agent is doing (star matching can take several seconds per photo)."""
    fails = 0
    while True:
        if STATE["live_until"] > time.time():
            t0 = time.time()
            try:
                extra = {"pa_state": STATE["pa_state"], "pa_hint": STATE["pa_hint"]} if STATE["pa_state"] else None
                api("POST", "/api/scope/live", read_scope(extra), timeout=5); fails = 0
            except Exception as ex:
                fails += 1
                if fails in (1, 20):
                    log("live post failed:", str(ex)[:100])
            time.sleep(max(0.2, 1.0 - (time.time() - t0)))
        else:
            time.sleep(0.5)


def progress():
    """While the scope stacks, post its frame count every ~20 s for the public site's "now photographing" card.
    Only reads the scope; posts nothing when it isn't stacking."""
    last, exp_of = None, {}
    while True:
        try:
            v = q(raw.get_view_state).get("result", {}).get("View", {})
            stk = v.get("Stack") or {}
            name = v.get("target_name")
            if v.get("state") == "working" and v.get("stage") == "Stack" and name:
                if name not in exp_of:
                    exp_of[name] = (q(raw.get_setting).get("result", {}).get("exp_ms") or {}).get("stack_l", 0) / 1000
                body = {"block": name, "frames": stk.get("stacked_frame") or 0, "dropped": stk.get("dropped_frame") or 0,
                        "exp_s": exp_of[name]}
                if body != last:
                    api("POST", "/api/scope/progress", body, timeout=6); last = body
        except Exception:
            pass
        time.sleep(20)


def main():
    if CFG is None:
        raise SystemExit("no site config (~/.config/seestar/public-site.env)")
    log("scope agent up; site", CFG["url"])
    threading.Thread(target=feeder, daemon=True).start()
    threading.Thread(target=progress, daemon=True).start()
    pa_state = None; site_fail = 0
    while True:
        try:
            cmd = api("GET", "/api/scope/cmd"); site_fail = 0
        except Exception as ex:
            site_fail += 1
            if site_fail in (1, 10) or site_fail % 100 == 0:
                log("site unreachable:", str(ex)[:120])
            time.sleep(min(60, IDLE_POLL * site_fail)); continue
        STATE["live_until"] = (cmd.get("live_until") or 0) - cmd.get("now", time.time()) + time.time()
        live = STATE["live_until"] > time.time()
        if cmd.get("polar"):
            try:
                api("POST", "/api/scope/cmd/polar-taken", {})
            except Exception:
                pass
            def post(text):
                STATE["pa_state"] = text; log("polar:", text)
                STATE["pa_hint"] = "Hands off the scope: it moves by itself while it measures." if text.startswith(("Opening", "Looking", "No stars", "Turning", "Star photo")) else None
            try:
                res = polar(post)
                if res:
                    api("POST", "/api/scope/pa", res)
                    Path("/tmp/seestar_pa_latest.json").write_text(json.dumps(dict(res, ts=time.time())))
                    def keep_going():
                        try:
                            c = api("GET", "/api/scope/cmd", timeout=4)
                            STATE["live_until"] = (c.get("live_until") or 0) - c.get("now", time.time()) + time.time()
                            return STATE["live_until"] > time.time() and not c.get("polar")
                        except Exception:
                            return STATE["live_until"] > time.time()
                    polar_live(res, keep_going, post)
                    post("Live polar updates stopped. Press Measure again for a fresh full measurement.")
            except Exception as ex:
                post(f"Polar measurement failed: {str(ex)[:100]}")
            continue
        if not live:
            STATE["pa_state"] = STATE["pa_hint"] = None
        time.sleep(2.0 if live else IDLE_POLL)


if __name__ == "__main__":
    main()
