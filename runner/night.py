#!/usr/bin/env python
"""Night runner: drives the Seestar through a plan of targets with nobody watching.

  night.py run                 the service loop (systemd user unit seestar-night)
  night.py status              what it is doing now
  night.py plan                tonight's plan with live visibility
  night.py skip|pause|resume|stop|park     control the running loop
  night.py add "M31" [--minutes 40] [--credit NAME] [--display "..."]

Runs with the seestar skill's venv. All scope work goes through the `seestar` CLI and
seestarpy, so anything that works by hand works here. State in runner/state.json,
log in runner/night.log, commands in runner/control.json.

Rules baked in (see OPERATING.md): auto_power_off must be false; park is the default
whenever nothing is being photographed; nothing above 75 deg; nothing behind the roof
obstruction table; the Sun is never a target; visitor requests outrank AI picks.
"""
import argparse, contextlib, io, json, math, os, re, shutil, subprocess, sys, time, traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

# The skill lives next to this file in the repo (repo/skills/seestar), or wherever
# SEESTAR_SKILL points if you symlinked it into ~/.claude/skills/.
SKILL = Path(os.environ.get("SEESTAR_SKILL", Path(__file__).resolve().parent.parent / "skills/seestar"))
sys.path.insert(0, str(SKILL))
import config  # noqa: E402
import public_site  # noqa: E402
with contextlib.redirect_stdout(io.StringIO()):
    from seestarpy import connection as conn, raw  # noqa: E402
conn.DEFAULT_IP = config.require_ip(); conn.VERBOSE_LEVEL = 0
from skyfield.api import Loader, wgs84, Star  # noqa: E402

RUN = Path(__file__).resolve().parent
CAP = config.CAPTURES; GAL = CAP / "gallery"
SEESTAR = str(SKILL / "seestar"); VENV_PY = str(SKILL / ".venv/bin/python"); FITPROC = str(SKILL / "fitproc.py")
STATE_F, PLAN_F, CTL_F, LOG_F = RUN / "state.json", RUN / "plan.json", RUN / "control.json", RUN / "night.log"
LAT, LON = config.require_site()
# Name credited on pictures the owner asked for, as opposed to the ones the planner chose.
OWNER = config.get("SEESTAR_OWNER", "Owner")
TICK = 20                      # s between loop iterations
STACK_POLL = 60                # s between stack progress checks
CLOUD_WAIT = 30 * 60           # s to wait after a cloud-out before trying again
STALL_MIN = 6                  # minutes with no new stacked frame = clouded / stuck
MIN_FRAMES_TO_PUBLISH = 15     # 2.5 min of light; less than that is not a picture
DUSK_SUN_ALT, DAWN_SUN_ALT = -15.0, -12.0   # start stacking at -15 (per-frame star alignment fails brighter than that); stop at -12
TWILIGHT_RETRY = 8 * 60                     # s to wait when a stack stalls while the sky is still brightening/darkening
MAX_ALT = 75.0
# Privacy: never aim low toward a neighbour's windows. Below PRIVACY_MIN_ALT degrees of
# altitude, the compass sectors listed in PRIVACY_AZ are refused outright - a telephoto
# pointed over the fence is a camera pointed into someone's house, and the wide camera
# sees a lot more than you think. Set these for YOUR garden before the first night;
# SEESTAR_PRIVACY_AZ takes "225-330,10-40" and SEESTAR_PRIVACY_MIN_ALT a number.
PRIVACY_MIN_ALT = config.getf("SEESTAR_PRIVACY_MIN_ALT", 30.0)
PRIVACY_AZ = [tuple(float(x) for x in seg.split("-"))
              for seg in config.get("SEESTAR_PRIVACY_AZ", "").split(",") if "-" in seg]
TILT_MAX = 20.0            # deg: the base is never this far off level on a tripod; more means it has tipped or is hanging
BATTERY_STOP = 15          # % : park and end the night (the scope shuts down by itself around 5-10)
BATTERY_HEATER_OFF = 35    # % : dew heater is the big draw; drop it first
BATTERY_NO_NEW_TARGET = 22 # % : finish what is running, start nothing new
DEW_SPREAD_C = 3.0         # heater on when air temp - dew point is within this (or frames start dropping)
# Minimum usable altitude by azimuth for YOUR observing spot - trees, roofs, fences -
# linearly interpolated between the points. Measure it once with `seestar survey` (see
# OPERATING.md section 4) and put the result in horizon.json next to this file. The
# default below assumes a clear horizon and will happily point the scope into a tree.
HORIZON = json.loads((RUN / "horizon.json").read_text()) if (RUN / "horizon.json").exists() \
    else [(0, 10), (360, 10)]
FILTER_NAME = {"IRCUT": "Infrared cut", "LP": "Light pollution"}

_load = Loader(str(config.EPHEMERIS.parent)); TS = _load.timescale(); EPH = _load("de421.bsp")
SITE = EPH["earth"] + wgs84.latlon(LAT, LON)


# ----------------------------------------------------------------------------- small helpers
def now():
    return datetime.now().astimezone()


def log(*a):
    line = f"{now().strftime('%Y-%m-%d %H:%M:%S')} " + " ".join(str(x) for x in a)
    print(line, flush=True)
    with open(LOG_F, "a") as f:
        f.write(line + "\n")


def jload(p, default):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return default


def jsave(p, obj):
    tmp = Path(str(p) + ".tmp"); tmp.write_text(json.dumps(obj, indent=2, default=str)); tmp.replace(p)


def night_date():
    """Noon-to-noon: a 1 AM capture belongs to the evening before."""
    return (datetime.now() - timedelta(hours=12)).strftime("%Y-%m-%d")


def hm_today(hm, ref=None):
    """'22:30' -> datetime tonight (noon-to-noon)."""
    ref = ref or now()
    h, m = map(int, hm.split(":"))
    d = ref.replace(hour=h, minute=m, second=0, microsecond=0)
    if h < 12 and ref.hour >= 12:
        d += timedelta(days=1)
    if h >= 12 and ref.hour < 12:
        d -= timedelta(days=1)
    return d


def sun_alt():
    return SITE.at(TS.now()).observe(EPH["sun"]).apparent().altaz()[0].degrees


def horizon_min_alt(az):
    az = az % 360
    for (a0, h0), (a1, h1) in zip(HORIZON, HORIZON[1:]):
        if a0 <= az <= a1:
            return h0 + (h1 - h0) * (az - a0) / (a1 - a0)
    return 65


def altaz(ra_h, dec_d, t=None):
    t = TS.now() if t is None else t
    alt, az, _ = SITE.at(t).observe(Star(ra_hours=ra_h, dec_degrees=dec_d)).apparent().altaz()
    return alt.degrees, az.degrees


def private_direction(alt, az):
    """True when a pointing would look into a neighbour's property (low and westward)."""
    az = az % 360
    return alt < PRIVACY_MIN_ALT and any(a0 <= az <= a1 for a0, a1 in PRIVACY_AZ)


def visible(alt, az, margin=2.0):
    return horizon_min_alt(az) + margin <= alt <= MAX_ALT and not private_direction(alt, az)


def resolve(name):
    """(ra_hours, dec_deg) via the skill's resolver (planets by skyfield, catalog by Sesame)."""
    import importlib
    with contextlib.redirect_stdout(io.StringIO()):
        seestar_mod = importlib.import_module("seestar")
    return seestar_mod.resolve(name)


def sep_deg(ra1, dec1, ra2, dec2):
    a1, a2 = math.radians(ra1 * 15), math.radians(ra2 * 15); d1, d2 = math.radians(dec1), math.radians(dec2)
    return math.degrees(math.acos(max(-1, min(1, math.sin(d1) * math.sin(d2) + math.cos(d1) * math.cos(d2) * math.cos(a1 - a2)))))


def sh(*args, timeout=600, check=False):
    """Run a seestar CLI command, return stdout (RESULT lines and all)."""
    r = subprocess.run([str(a) for a in args], capture_output=True, text=True, timeout=timeout)
    out = (r.stdout or "") + (r.stderr or "")
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(str(a) for a in args)} failed rc={r.returncode}: {out[-800:]}")
    return out


def rpc(name, **kw):
    fn = getattr(raw, name)
    r = fn(**kw) if kw else fn()
    return r.get("result") if isinstance(r, dict) else r


def view_state():
    v = rpc("get_view_state") or {}
    v = v.get("View", {}) if isinstance(v, dict) else {}
    sub = {k: v[k].get("state") for k in v if isinstance(v[k], dict) and "state" in v[k]}
    return v, sub


def mount():
    try:
        return (rpc("get_device_state") or {}).get("mount", {})
    except Exception:
        return {}


def reachable():
    try:
        return bool(rpc("get_device_state"))
    except Exception:
        return False


# ----------------------------------------------------------------------------- site + plan + state
def cfg():
    return public_site.load_config()


def post_status(state, detail="", **extra):
    """Tell the public site what we are doing. Never fatal."""
    body = {"state": state, "detail": detail, "since": extra.pop("since", now().isoformat(timespec="seconds")), **extra}
    st = jload(STATE_F, {}); st["status"] = body; jsave(STATE_F, st)
    c = cfg()
    if c is None:
        return
    try:
        public_site._call(c, "POST", "/api/status", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    except Exception as ex:
        log("status post failed (site not ready?):", str(ex).splitlines()[0][:120])


def load_plan():
    return jload(PLAN_F, {"night": night_date(), "items": []})


def save_plan(plan):
    jsave(PLAN_F, plan)


def set_state(**kw):
    st = jload(STATE_F, {}); st.update(kw); st["updated"] = now().isoformat(timespec="seconds"); jsave(STATE_F, st); return st


def take_control():
    c = jload(CTL_F, None)
    if c:
        try:
            CTL_F.unlink()
        except Exception:
            pass
    return c or {}


def ensure_request(item):
    """AI picks appear on the public queue as requests from 'AI'. Tolerates the endpoint not existing yet."""
    if item.get("request_id") or item.get("credit") not in (None, "AI", OWNER):
        return
    c = cfg()
    if c is None:
        return
    try:
        r = public_site._call(c, "POST", "/api/requests", data=json.dumps({
            "submitter_name": item.get("credit", "AI"), "prompt_text": item.get("why") or item["display"],
            "display_prompt": item["display"], "status": "scheduled", "target": item["name"],
            "source": "ai" if item.get("credit", "AI") == "AI" else "owner", "planned_for": "tonight",
        }).encode(), headers={"Content-Type": "application/json"})
        item["request_id"] = r.get("request", r).get("id")
        log(f"filed request #{item['request_id']} for {item['name']}")
    except Exception as ex:
        log("could not file request (site not ready?):", str(ex).splitlines()[0][:120])


def patch_request(rid, **fields):
    c = cfg()
    if c is None or not rid:
        return
    try:
        public_site.patch_request(c, rid, **fields)
    except Exception as ex:
        log(f"request #{rid} patch failed:", str(ex)[:160])


# ----------------------------------------------------------------------------- picking targets
def item_window(item):
    nb = hm_today(item.get("not_before", "12:00")) if item.get("not_before") else None
    na = hm_today(item.get("not_after", "11:59")) if item.get("not_after") else None
    return nb, na


def item_visible_now(item):
    if "ra" not in item:
        try:
            item["ra"], item["dec"] = resolve(item["name"])
        except Exception as ex:
            log(f"cannot resolve {item['name']}: {ex}"); return False
    alt, az = altaz(item["ra"], item["dec"]); item["alt"], item["az"] = round(alt, 1), round(az, 1)
    return visible(alt, az)


def pick_next(plan):
    """Visitor requests first (arrival order), then the telescope's picks in the order shown on the
    public queue (anyone may reorder those). An item whose window is about to close jumps the queue.
    Only items visible right now."""
    t = now(); cands = []
    for it in plan["items"]:
        if it.get("status") != "pending":
            continue
        nb, na = item_window(it)
        if na and t > na - timedelta(minutes=10):
            it["status"] = "missed"; it["note"] = "window closed"; log(f"{it['name']}: window closed, dropped for tonight"); continue
        if nb and t < nb:
            continue
        if it.get("defer_until") and t < datetime.fromisoformat(it["defer_until"]):
            continue
        if not item_visible_now(it):
            continue
        urgent = bool(na) and na - t < timedelta(minutes=it["minutes"] + 15)
        group = 0 if it.get("source") == "visitor" else (1 if urgent else 2)
        order = it.get("position") if it.get("position") is not None else (it.get("request_id") or 1e9)
        if group == 1:
            order = na.timestamp()
        cands.append((group, order, it.get("not_before", ""), it))
    if not cands:
        return None
    cands.sort(key=lambda c: (c[0], c[1], c[2]))
    return cands[0][3]


def next_pending_time(plan):
    times = []
    for it in plan["items"]:
        if it.get("status") == "pending":
            nb, _ = item_window(it); times.append(nb or now())
    return min(times) if times else None


# ----------------------------------------------------------------------------- scope actions
def scope_setup():
    s = rpc("get_setting") or {}
    if s.get("auto_power_off", True):
        log("auto_power_off was ON - turning it off"); rpc("set_setting", auto_power_off=False)
    pct, charging = battery()
    risk = dew_risk()
    heater(bool(risk) and (charging or (pct or 0) > BATTERY_HEATER_OFF))
    log(f"setup ok: auto_power_off off, battery {pct}% {'on mains' if charging else 'on battery'}, dew risk {risk}, base tilt {base_tilt()} deg")
    ok, detail = lens_check()
    log(f"lens check: {'night sky with stars' if ok else ('BRIGHT - indoors or under a light?' if LAST.get('lens_kind') == 'bright' else 'NO LIGHT - covered or capped?')} ({detail})")
    return ok


LAST = {}


def lens_check(exp=5.0, gain=200):
    """Is light reaching the main camera? Takes one raw frame (no native view may be running) and
    looks for stars. Returns (ok, detail). A capped lens gives a flat frame with no bright pixels."""
    try:
        import requests, struct
        import numpy as np
        P = {"ClientID": 7, "ClientTransactionID": 1}; B = f"http://{config.SCOPE_IP}:32323/api/v1/camera/0"
        requests.put(B + "/connected", data={**P, "Connected": "true"}, timeout=15)
        requests.put(B + "/gain", data={**P, "Gain": gain}, timeout=15)
        requests.put(B + "/startexposure", data={**P, "Duration": exp, "Light": "true"}, timeout=15)
        for _ in range(120):
            if requests.get(B + "/imageready", params=P, timeout=15).json()["Value"]:
                break
            time.sleep(0.3)
        b = requests.get(B + "/imagearray", params=P, headers={"Accept": "application/imagebytes"}, timeout=180).content
        h = struct.unpack("<11i", b[:44])
        raw_ = np.frombuffer(b[44:], dtype=np.uint16)[:h[8] * h[9]].astype(np.float32)
        import cv2
        img = raw_.reshape(h[8], h[9]).T
        g = (img[0::2, 0::2] + img[1::2, 1::2]) / 2            # green superpixels, 1920x1080
        med = float(np.median(g)); mx = float(g.max())
        lit = float((g > med + 2000).mean())                     # fraction of the frame well above the sky level
        hp = g - cv2.GaussianBlur(g, (0, 0), 12)                  # stars survive, gradients do not
        noise = float(np.std(hp[np.abs(hp) < np.percentile(np.abs(hp), 90)])) + 1e-3
        m = (hp > max(6 * noise, 150)).astype(np.uint8)
        n, _, st, _ = cv2.connectedComponentsWithStats(m)
        stars = int(sum(1 for i in range(1, n) if 2 <= st[i, cv2.CC_STAT_AREA] <= 80))
        detail = f"median {med:.0f}, max {mx:.0f}, {lit*100:.1f}% lit, {stars} star-like points"
        if med > 20000 or lit > 0.05:
            LAST["lens_kind"] = "bright"      # broad light across the frame: indoors, a room light, a torch
            return False, detail
        if stars < 25:
            LAST["lens_kind"] = "dark"        # no stars: covered, capped, dark room, or solid cloud
            return False, detail
        LAST["lens_kind"] = "sky"
        return True, detail
    except Exception as ex:
        LAST["lens_kind"] = "unknown"
        return True, f"lens check skipped: {str(ex)[:80]}"   # never block the night on a diagnostic


def base_tilt():
    """Tilt of the mount base from level, degrees, from the balance sensor (it sits in the fork: it turns
    with azimuth but does NOT follow the arm's altitude, so a big number means the whole scope has
    tipped over, not that the arm is pointing somewhere). None if unreadable."""
    try:
        st = rpc("get_device_state") or {}
        LAST["eq_mode"] = bool((st.get("mount") or {}).get("equ_mode"))
        return float(st["balance_sensor"]["data"]["angle"])
    except Exception:
        return None


def tipped(t):
    """True when tilt t means the scope has fallen. On a wedge (EQ mode) the base is meant to lean
    by the co-latitude (~53 deg here), so allow up to that plus the normal margin."""
    limit = TILT_MAX + (90 - LAT if LAST.get("eq_mode") else 0)
    return t > limit


def upright():
    """False when the scope has tipped over or is hanging. Logs and posts once per event."""
    t = base_tilt()
    if t is None:
        return True
    set_state(tilt=round(t, 1))
    if tipped(t):
        if LAST.get("tipped") != True:
            log(f"!!! base tilt {t:.0f} deg: the telescope is NOT standing upright. Freezing - no motor commands until it is standing again")
            post_status("offline", f"the telescope has tipped over (tilt {t:.0f} degrees). Nothing moves until someone stands it up")
            LAST["tipped"] = True
        return False
    if LAST.get("tipped"):
        log(f"base tilt back to {t:.1f} deg; standing again"); LAST["tipped"] = False
    return True


def battery():
    """(percent, charging) from the scope; (None, None) if unreadable."""
    try:
        pi = (rpc("get_device_state") or {}).get("pi_status", {})
        return int(pi.get("battery_capacity")), bool(pi.get("charge_online"))
    except Exception:
        return None, None


def airport_sky():
    """Nearest airport's hourly observation (SEESTAR_METAR_STATION). Returns (clear_enough, text):
    CLR/FEW/SCT count as clear enough to spend battery on a camera check; BKN/OVC mean stay parked.
    None if unknown or if no station is configured. api.weather.gov is US-only."""
    if not config.METAR_STATION:
        return None, "no weather station configured"
    try:
        import urllib.request
        req = urllib.request.Request(f"https://api.weather.gov/stations/{config.METAR_STATION}/observations/latest", headers={"User-Agent": f"seestar-observatory ({config.WEATHER_CONTACT})"})
        p = json.loads(urllib.request.urlopen(req, timeout=20).read())["properties"]
        order = ["CLR", "SKC", "FEW", "SCT", "BKN", "OVC", "VV"]
        layers = [l["amount"] for l in p.get("cloudLayers", [])]
        worst = max((order.index(a) if a in order else 3) for a in layers) if layers else 0
        return worst <= 2, f"{p.get('textDescription')} {layers}"
    except Exception as ex:
        return None, f"weather unavailable: {str(ex)[:60]}"


def dew_risk():
    """True when the air is near its dew point (nearest airport observation). None if unknown."""
    if not config.METAR_STATION:
        return None
    try:
        import urllib.request
        req = urllib.request.Request(f"https://api.weather.gov/stations/{config.METAR_STATION}/observations/latest", headers={"User-Agent": f"seestar-observatory ({config.WEATHER_CONTACT})"})
        p = json.loads(urllib.request.urlopen(req, timeout=15).read())["properties"]
        t, d = p["temperature"]["value"], p["dewpoint"]["value"]
        if t is None or d is None:
            return None
        LAST["dew_spread"] = round(t - d, 1)
        return (t - d) <= DEW_SPREAD_C
    except Exception:
        return None


def heater(on):
    if LAST.get("heater") == on:
        return
    rpc("pi_output_set2", is_dew_on=bool(on), dew_heater_power=70 if on else 0); LAST["heater"] = on
    log(f"dew heater {'ON' if on else 'off'}" + (f" (air-dewpoint spread {LAST['dew_spread']} C)" if "dew_spread" in LAST else ""))


def park():
    try:
        rpc("iscope_stop_view"); time.sleep(2)
        t = base_tilt()
        if t is not None and tipped(t):
            log(f"not parking: base tilt {t:.0f} deg, the scope is not upright"); return
        rpc("scope_park"); log("parked")
    except Exception as ex:
        log("park failed:", ex)


def goto(item):
    """GoTo with plate solve. Returns True when the scope is tracking within 1 deg of the target."""
    if not upright():
        return False
    ra, dec = item["ra"], item["dec"]
    alt_, az_ = altaz(ra, dec)
    if private_direction(alt_, az_):
        log(f"refusing goto {item['name']}: alt {alt_:.0f} az {az_:.0f} looks toward the neighbours"); return False
    rpc("iscope_stop_view"); time.sleep(2)
    r = raw.iscope_start_view(ra=ra, dec=dec, target_name=item["scope_name"], lp_filter=bool(item.get("lp")), mode="star")
    log(f"goto {item['name']} ra={ra:.3f} dec={dec:.2f} lp={item.get('lp')} -> {r.get('result')} {r.get('error') or ''}")
    t0 = time.time(); last = None
    while time.time() - t0 < 240:
        time.sleep(5)
        v, sub = view_state(); m = mount()
        key = (v.get("state"), v.get("stage"), tuple(sorted(sub.items())), m.get("move_type"), m.get("tracking"))
        if key != last:
            log(f"  t={int(time.time()-t0)}s state={v.get('state')} stage={v.get('stage')} mount={m.get('move_type')} tracking={m.get('tracking')} {sub}"); last = key
        if v.get("state") in ("cancel", "fail") or sub.get("AutoGoto") == "fail":
            log("  goto failed (no plate solve - clouds, or target behind something)"); return False
        if time.time() - t0 > 25 and m.get("move_type") == "none" and m.get("tracking") and ("AutoGoto" not in sub or sub.get("AutoGoto") == "complete"):
            try:
                eq = rpc("scope_get_equ_coord")          # {"ra": hours, "dec": degrees}
                era, edec = (eq["ra"], eq["dec"]) if isinstance(eq, dict) else (eq[0], eq[1])
                d = sep_deg(ra, dec, era, edec); log(f"  pointing ra={era:.3f} dec={edec:.2f} off by {d:.2f} deg")
                if d < 1.0:
                    return True
                bad_reads = 0
            except Exception as ex:
                bad_reads = locals().get("bad_reads", 0) + 1
                log("  equ coord read failed:", repr(ex))
                if bad_reads >= 3 and v.get("stage") == "ContinuousExposure":
                    log("  goto finished (live view running, tracking); accepting without the coordinate check"); return True
    log("  goto timed out"); return False


def stack_info():
    try:
        i = rpc("get_stack_info") or {}
    except Exception:
        i = {}
    return i if isinstance(i, dict) else {}


def run_stack(item, minutes, deadline, start_stack=True):
    """Start stacking and babysit it. Returns (frames_stacked, reason) where reason in done|cloud|deadline|skip|stop|fail."""
    if start_stack:
        r = raw.iscope_start_stack(restart=True); log(f"stack start -> {r.get('result')} {r.get('error') or ''}")
    t0 = time.time(); end = t0 + minutes * 60; hist = []; last_progress = time.time(); last_frames = 0
    while True:
        time.sleep(STACK_POLL)
        c = take_control()
        if c.get("cmd") in ("skip", "stop", "park", "pause"):
            log(f"control: {c['cmd']}"); return last_frames, c["cmd"]
        if not upright():
            return last_frames, "tipped"
        info = stack_info(); v, sub = view_state()
        st = v.get("Stack", {}) if isinstance(v.get("Stack"), dict) else {}
        # frame counters live in the view state (View.Stack), not in get_stack_info
        frames = int(st.get("stacked_frame", info.get("stacked_frame", 0)) or 0); dropped = int(st.get("dropped_frame", info.get("dropped_frame", 0)) or 0)
        errcode = st.get("frame_errcode")
        if frames > last_frames:
            last_progress = time.time(); last_frames = frames
        hist.append((time.time(), frames, dropped))
        if len(hist) <= 2:
            log("  view.Stack:", json.dumps(st, default=str)[:300])
        if frames == 0 and dropped >= 5 and len(hist) == 3:
            log(f"  every frame rejected so far (frame_errcode={errcode}, PlateSolve={st.get('PlateSolve', {}).get('state')}): sky too bright or clouded")
        el = int((time.time() - t0) / 60)
        set_state(target=item["name"], frames=frames, dropped=dropped, elapsed_min=el, planned_min=minutes)
        log(f"  {item['name']}: {frames} frames, {dropped} dropped, {el}/{minutes} min, stage={v.get('stage')} err={errcode} {sub}")
        if el % 5 == 0 and el:
            post_status("imaging", f"{item['title']} ({item['name']})", since=item.get("started"), frames=frames, minutes=el, planned=minutes)
            pct_, chg_ = battery()
            if frames >= 20 and dropped / max(1, frames + dropped) > 0.10 and (chg_ or (pct_ or 0) > BATTERY_HEATER_OFF):
                heater(True)   # frames are being lost: dew is the usual cause
            if el % 60 == 0 and not LAST.get("heater") and dew_risk() and (chg_ or (pct_ or 0) > BATTERY_HEATER_OFF):
                heater(True)
        if v.get("state") in ("cancel", "fail") or (v.get("stage") not in ("Stack", None) and time.time() - t0 > 120 and frames == 0):
            log("  stack ended by the scope"); return frames, "fail" if frames < MIN_FRAMES_TO_PUBLISH else "done"
        if time.time() - last_progress > STALL_MIN * 60:
            log(f"  no new frames for {STALL_MIN} min -> clouded"); return frames, "cloud"
        if time.time() >= end:
            return frames, "done"
        if deadline and now() >= deadline:
            log("  window closing"); return frames, "deadline"


def stop_stack():
    try:
        rpc("iscope_stop_view", stage="Stack"); time.sleep(3)
    except Exception as ex:
        log("stop stack:", ex)


# ----------------------------------------------------------------------------- pictures out
CROPS = {"square1050": (1050, 1050), "square1300": (1300, 1300), "square900": (900, 900), "wide1400": (2160, 1400)}


def process_and_publish(item, t_start):
    """Pull the scope's stacked master, render full + close-up, add to the gallery, publish, close the request."""
    sh(SEESTAR, "pull", timeout=900)
    folder = CAP / "scope/MyWorks" / item["scope_name"]
    fits = [p for p in folder.glob("*.fit") if p.stat().st_mtime > t_start - 120] if folder.exists() else []
    if not fits:
        log(f"no new master for {item['name']} in {folder}"); return None
    fit = max(fits, key=lambda p: p.stat().st_mtime)
    mm = re.match(r"Stacked_(\d+)_.*?_([\d.]+)s_(\w+?)_(\d{8}-\d{6})", fit.name)
    frames = int(mm.group(1)) if mm else 0; sub = float(mm.group(2)) if mm else 10.0
    filt = FILTER_NAME.get(mm.group(3), mm.group(3)) if mm else ""
    taken = datetime.strptime(mm.group(4), "%Y%m%d-%H%M%S").isoformat(timespec="seconds") if mm else now().isoformat(timespec="seconds")
    if frames < MIN_FRAMES_TO_PUBLISH:
        log(f"{item['name']}: only {frames} frames, not publishing"); return None
    sess = CAP / "sessions" / night_date(); sess.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", item["title"].lower()).strip("-")
    base = f"{night_date()}-{slug}"
    full = sess / f"{base}.jpg"; close = sess / f"{base}-closeup.jpg"
    a = str(item.get("a", 0.001))
    sh(VENV_PY, FITPROC, fit, full, "full", a, timeout=900, check=True)
    cw, ch = CROPS.get(item.get("crop", "square1050"), (1050, 1050)); H, W = 3840, 2160
    y0 = max(0, (H - ch) // 2); x0 = max(0, (W - cw) // 2)
    sh(VENV_PY, FITPROC, fit, close, f"{y0},{y0+ch},{x0},{x0+cw}", a, timeout=900, check=True)
    credit = item.get("credit", "AI")
    sh(SEESTAR, "gallery", "add", "--credit", credit, str(full), check=True)
    sh(SEESTAR, "gallery", "add", "--featured", "--credit", credit, str(close), check=True)
    meta = jload(GAL / "meta.json", {})
    total = int(frames * sub); mins = total // 60
    common = {"object": item["object"], "kind": item.get("kind", "Deep sky"), "taken": taken, "frames": frames, "sub": sub,
              "total_exp": total, "filter": filt, "gain": 200, "credit": credit}
    if item.get("request_id"):
        common["request_id"] = item["request_id"]
    meta[full.name].update(common, title=f"{item['title']}, full frame", notes=f"The whole telescope field. {mins} minutes of light, {frames} frames of {sub:g} seconds.")
    meta[close.name].update(common, title=f"{item['title']}, close up", notes=f"{item['display']} {mins} minutes of light, {frames} frames of {sub:g} seconds each.")
    jsave(GAL / "meta.json", meta)
    out = sh(SEESTAR, "gallery", "publish", timeout=900)
    log("publish:", " | ".join(l.strip() for l in out.splitlines() if "pushed" in l or "public push" in l))
    meta = jload(GAL / "meta.json", {}); pid = meta.get(close.name, {}).get("public_id")
    if item.get("request_id") and pid:
        patch_request(item["request_id"], status="done", photo_id=pid)
    log(f"{item['name']} published: {frames} frames ({mins} min), photo id {pid}")
    return {"frames": frames, "public_id": pid, "full": str(full), "close": str(close)}


# ----------------------------------------------------------------------------- the loop
def resume_running(plan):
    """After a restart mid-target: if the scope is still stacking the running item, babysit it to the end."""
    for it in plan["items"]:
        if it.get("status") != "running":
            continue
        v, sub = view_state()
        if v.get("stage") == "Stack" or "Stack" in sub:
            started = datetime.fromisoformat(it["started"]); left = max(5, it["minutes"] - int((now() - started).total_seconds() / 60))
            log(f"resuming {it['name']}: scope is still stacking, {left} min left")
            _, na = item_window(it); t_start = started.timestamp()
            frames, reason = run_stack(it, left, na, start_stack=False); stop_stack()
            result = None
            if frames >= MIN_FRAMES_TO_PUBLISH:
                try:
                    result = process_and_publish(it, t_start)
                except Exception:
                    log("processing failed:\n" + traceback.format_exc()[-1500:])
            it["frames"] = frames; it["result"] = result; it["status"] = "done" if result else ("thin" if frames else "pending")
            if not result and frames == 0:
                save_plan(plan); return after_failure(it, plan, "frames rejected")
        else:
            log(f"{it['name']} was running before the restart but the scope is not stacking; back to pending")
            it["status"] = "pending"
    save_plan(plan)
    return None


FAILS = {"streak": 0}


def after_failure(item, plan, why):
    """Bookkeeping after a goto failure or a stack that stacked nothing. Returns a cloud_until datetime
    when it is time to park and wait, or None to move straight on to a different target. One bad field
    is not proof of cloud (the Moon or a tree can do it); two different failures in a row are."""
    FAILS["streak"] += 1
    item["stalls"] = item.get("stalls", 0) + 1
    if item["stalls"] >= 2 and item.get("status") == "pending":
        item["status"] = "failed"; item["note"] = f"{why} twice"; log(f"{item['name']}: {why} twice, dropped for tonight")
    save_plan(plan)
    item["defer_until"] = (now() + timedelta(minutes=25)).isoformat(timespec="seconds"); save_plan(plan)
    others = [it for it in plan["items"] if it.get("status") == "pending" and it is not item and item_visible_now(it)]
    if FAILS["streak"] < 2 and others:
        log(f"{item['name']}: {why}; trying a different part of the sky first ({others[0]['name']})"); return None
    twilight = sun_alt() > -18
    until = now() + timedelta(seconds=TWILIGHT_RETRY if twilight else CLOUD_WAIT)
    park()
    try:
        rpc("iscope_stop_view"); time.sleep(3)
        ok, detail = lens_check()
        if not ok:
            log(f"no usable sky on the main camera ({detail}) - {'indoors or under a light' if LAST.get('lens_kind') == 'bright' else 'covered or capped'}? Checking again in 10 min")
            post_status("offline", "the telescope seems to be indoors or under a light" if LAST.get("lens_kind") == "bright" else "the telescope's cameras see no light. Covered, capped, or indoors?")
            return now() + timedelta(minutes=10)
    except Exception as ex:
        log("lens check failed:", ex)
    post_status("clouded" if not twilight else "idle", "waiting for the sky to clear, checking every half hour" if not twilight else "waiting for full darkness")
    log(f"{'twilight' if twilight else 'cloud'} after {FAILS['streak']} failures in a row; retry at {until.strftime('%H:%M')}")
    return until


def run():
    log("=== night runner starting ===")
    mode = "day"; cloud_until = None; paused = False
    try:
        if sun_alt() < DUSK_SUN_ALT and reachable():
            cloud_until = resume_running(load_plan())
    except Exception:
        log("resume check failed:\n" + traceback.format_exc()[-800:])
    post_status("daylight" if sun_alt() > DUSK_SUN_ALT else "idle", "starting up")
    while True:
        try:
            c = take_control()
            if c.get("cmd") == "stop":
                log("control: stop -> parking and exiting"); park(); post_status("offline", "stopped by operator"); return
            if c.get("cmd") == "pause":
                paused = True; park(); post_status("idle", "paused by operator"); log("paused")
            if c.get("cmd") == "resume":
                paused = False; log("resumed")
            if c.get("cmd") == "park":
                park()
            sa = sun_alt()
            if paused:
                set_state(mode="paused", sun_alt=round(sa, 1)); time.sleep(TICK); continue
            if sa > DUSK_SUN_ALT:
                if mode != "day":
                    log("dawn: wrapping up"); stop_stack(); park(); heater(False)
                    mode = "day"
                nxt = None
                try:
                    from skyfield import almanac
                    t0 = TS.now(); t1 = TS.from_datetime(datetime.now(timezone.utc) + timedelta(hours=30))
                    ts_, ev = almanac.find_discrete(t0, t1, almanac.dark_twilight_day(EPH, wgs84.latlon(LAT, LON)))
                    for t_, e_ in zip(ts_, ev):
                        if e_ == 1 and SITE.at(t_ + 0.001).observe(EPH["sun"]).apparent().altaz()[0].degrees < -11:
                            nxt = t_.astimezone(now().tzinfo); break
                except Exception:
                    pass
                set_state(mode="day", sun_alt=round(sa, 1), next_dusk=nxt.isoformat() if nxt else None)
                if int(time.time()) % 900 < TICK:
                    post_status("daylight", f"next session at dusk, about {nxt.strftime('%-I:%M %p')}" if nxt else "next session at dusk")
                time.sleep(TICK); continue
            # ---- it is dark enough
            if not reachable():
                if mode != "offline":
                    log("scope unreachable"); post_status("offline", "telescope not answering"); mode = "offline"
                set_state(mode="offline"); time.sleep(TICK * 3); continue
            if not upright():
                set_state(mode="tipped"); time.sleep(TICK); continue
            pct, charging = battery()
            if pct is not None:
                set_state(battery=pct, charging=charging)
                if not charging and pct <= BATTERY_STOP and mode not in ("day", "offline", "battery"):
                    log(f"battery {pct}%: ending the night"); stop_stack(); heater(False); park()
                    post_status("idle", f"battery down to {pct}%, done for tonight"); mode = "battery"
                if not charging and pct <= BATTERY_HEATER_OFF and LAST.get("heater"):
                    heater(False)
            if mode == "battery":
                set_state(mode="battery"); time.sleep(TICK * 3); continue
            if mode in ("day", "offline", "covered"):
                if LAST.get("covered_until", 0) > time.time():
                    set_state(mode="covered"); time.sleep(TICK); continue
                if not scope_setup():
                    post_status("offline", "the telescope seems to be indoors or under a light" if LAST.get("lens_kind") == "bright" else "the telescope's cameras see no light. Covered, capped, or indoors?")
                    mode = "covered"; LAST["covered_until"] = time.time() + 600; set_state(mode="covered"); continue
                LAST.pop("covered_until", None)
                mode = "running"; log("night begins")
            if cloud_until and now() < cloud_until:
                set_state(mode="clouded", retry_at=cloud_until.isoformat()); time.sleep(TICK); continue
            if cloud_until and now() >= cloud_until:
                ok, txt = airport_sky()
                if ok is False:
                    cloud_until = now() + timedelta(minutes=15)      # no motion, no battery: check the airport again in 15 min
                    if time.time() - LAST.get("sky_log", 0) > 1800:
                        log(f"still clouded per {config.METAR_STATION} ({txt}); staying parked"); LAST["sky_log"] = time.time()
                    set_state(mode="clouded", retry_at=cloud_until.isoformat(), airport=txt); time.sleep(TICK); continue
                log(f"{config.METAR_STATION} reports {txt}; trying the sky again"); cloud_until = None
            plan = load_plan()
            import planner
            if time.time() - LAST.get("sync", 0) > 60:
                planner.sync_requests(plan); LAST["sync"] = time.time()
            item = pick_next(plan); save_plan(plan)
            if item is None:
                nxt = next_pending_time(plan)
                if nxt is None:
                    added = planner.refill(plan); save_plan(plan)
                    if added:
                        log(f"planner added {added} targets"); planner.mark_planned_for(plan); save_plan(plan); continue
                    if mode != "done":
                        log("nothing left to shoot; parked until dawn"); park(); post_status("idle", "tonight's list is finished; more tomorrow"); mode = "done"
                    set_state(mode="done"); time.sleep(TICK); continue
                if mode != "waiting":
                    log(f"nothing visible right now; next target opens {nxt.strftime('%-I:%M %p')}"); park()
                    post_status("idle", f"waiting for the next target to clear the trees, about {nxt.strftime('%-I:%M %p')}"); mode = "waiting"
                set_state(mode="waiting", next_at=nxt.isoformat()); time.sleep(TICK); continue
            if pct is not None and not charging and pct <= BATTERY_NO_NEW_TARGET:
                if mode != "battery-wait":
                    log(f"battery {pct}%: not starting another target"); park(); post_status("idle", f"battery at {pct}%, no more targets tonight"); mode = "battery-wait"
                time.sleep(TICK * 3); continue
            # ---- run one target
            mode = "running"; cloud_until = None
            ensure_request(item); save_plan(plan)
            try:
                planner.mark_planned_for(plan); save_plan(plan)
            except Exception as ex:
                log("planned_for update failed:", ex)
            item["started"] = now().isoformat(timespec="seconds"); item["status"] = "running"; save_plan(plan)
            set_state(mode="running", target=item["name"], title=item["title"])
            post_status("imaging", f"{item['title']} ({item['name']})", since=item["started"], planned=item["minutes"])
            patch_request(item.get("request_id"), status="accepted")
            ok = goto(item)
            if not ok:
                item["status"] = "pending"; save_plan(plan)
                cloud_until = after_failure(item, plan, "goto failed")
                continue
            _, na = item_window(item)
            t_start = time.time()
            frames, reason = run_stack(item, item["minutes"], na)
            stop_stack()
            log(f"{item['name']}: stack finished, {frames} frames, reason={reason}")
            result = None
            if frames >= MIN_FRAMES_TO_PUBLISH:
                post_status("processing", f"{item['title']}: making the picture")
                try:
                    result = process_and_publish(item, t_start)
                except Exception:
                    log("processing failed:\n" + traceback.format_exc()[-1500:])
            item["frames"] = frames; item["result"] = result
            if reason in ("done", "deadline", "skip"):
                item["status"] = "done" if result else ("thin" if frames else "failed")
                if result:
                    FAILS["streak"] = 0
            elif reason == "cloud":
                if result:
                    item["status"] = "done"; FAILS["streak"] = 0
                    cloud_until = now() + timedelta(seconds=CLOUD_WAIT); park()
                    post_status("clouded", "waiting for the sky to clear, checking every half hour")
                else:
                    item["status"] = "pending"
                    cloud_until = after_failure(item, plan, "frames rejected")
            elif reason == "tipped":
                item["status"] = "pending"      # no park, no motion: the scope is not standing
            elif reason in ("stop", "park", "pause"):
                item["status"] = "done" if result else "pending"; park()
                if reason == "pause": paused = True
            else:
                item["status"] = "done" if result else "failed"
            save_plan(plan)
            if not result and item.get("request_id") and item["status"] != "pending":
                patch_request(item["request_id"], status="scheduled", triage_note="cut short tonight, will retry")
        except KeyboardInterrupt:
            log("interrupted; parking"); park(); return
        except Exception:
            log("loop error:\n" + traceback.format_exc()[-2000:]); time.sleep(TICK)


# ----------------------------------------------------------------------------- CLI
def cli():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["run", "status", "plan", "skip", "pause", "resume", "stop", "park", "add", "refill", "sync"])
    ap.add_argument("target", nargs="?"); ap.add_argument("--minutes", type=int, default=45); ap.add_argument("--credit", default=OWNER)
    ap.add_argument("--display"); ap.add_argument("--lp", action="store_true")
    a = ap.parse_args()
    if a.cmd == "run":
        run()
    elif a.cmd == "status":
        st = jload(STATE_F, {}); print(json.dumps(st, indent=1))
        print("--- last log lines"); print("".join(open(LOG_F).readlines()[-12:]) if LOG_F.exists() else "(no log)")
    elif a.cmd == "plan":
        plan = load_plan(); t = TS.now()
        for it in plan["items"]:
            try:
                if "ra" not in it: it["ra"], it["dec"] = resolve(it["name"])
                alt, az = altaz(it["ra"], it["dec"]); vis = "VISIBLE" if visible(alt, az) else "hidden "
            except Exception as ex:
                alt = az = float("nan"); vis = f"?? {ex}"
            print(f"{it['status']:<8} {vis} {it['name']:<9} alt {alt:5.1f} az {az:5.1f}  {it.get('not_before','')}-{it.get('not_after','')}  {it['minutes']:>3}m  {it.get('source','ai'):<7} {it.get('planned_for') or '':<7} {it['title']}")
        save_plan(plan)
    elif a.cmd in ("refill", "sync"):
        import planner
        plan = load_plan()
        if a.cmd == "sync":
            planner.sync_requests(plan)
        else:
            print("added", planner.refill(plan))
        planner.mark_planned_for(plan); save_plan(plan)
    elif a.cmd == "add":
        plan = load_plan()
        ra, dec = resolve(a.target)
        plan["items"].insert(0, {"id": re.sub(r"\W+", "", a.target.lower()), "name": a.target, "scope_name": re.sub(r"\s+", "", a.target), "title": a.target,
                                 "object": a.target, "kind": "Deep sky", "type": "other", "display": a.display or a.target, "why": "added by operator",
                                 "minutes": a.minutes, "lp": a.lp, "a": 0.0015, "crop": "square1050", "credit": a.credit,
                                 "source": "visitor" if a.credit == OWNER else "ai", "status": "pending", "ra": ra, "dec": dec})
        save_plan(plan); print(f"added {a.target} at ra={ra:.3f} dec={dec:.2f}; runner picks it up on its next boundary")
    else:
        jsave(CTL_F, {"cmd": a.cmd, "at": now().isoformat(timespec="seconds")}); print(f"sent {a.cmd}")


if __name__ == "__main__":
    cli()
