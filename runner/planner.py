"""Planner: keeps runner/plan.json full and in step with the public queue.

- sync_requests(plan): pull scheduled/accepted requests from the public site; add visitor
  requests (with a target) to the plan at the front; copy the site's manual order
  (position) and planned_for onto AI items.
- refill(plan): when nothing is pending, choose more targets from catalog.json for tonight
  and the next night: visible above the roof obstruction table, below 75 deg, away from the
  Moon, not already in the gallery. Files each pick as a request from "AI".
- mark_planned_for(plan): decide which pending picks fit before dawn (tonight) and which
  roll over (next), and tell the site so the queue page draws the cutoff line.
"""
import json, math, re
from datetime import datetime, timedelta, timezone

import night as N
from skyfield.api import Star

CATALOG_F = N.RUN / "catalog.json"
STEP_MIN = 10
SETTLE_MIN = 6            # goto + plate solve + settle between targets


def catalog():
    return N.jload(CATALOG_F, {"targets": []})["targets"]


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def imaged_objects(plan):
    meta = N.jload(N.GAL / "meta.json", {})
    done = {norm(m.get("object")) for m in meta.values() if m.get("object") and m.get("kind") != "Site survey"}
    done |= {norm(it.get("object")) for it in plan["items"] if it.get("status") in ("done", "running", "thin")}
    return done


def dark_periods(hours=40):
    """[(start, end)] datetimes when the sun is below the nautical limit, next `hours` hours."""
    t0 = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    steps = int(hours * 60 / STEP_MIN)
    dark = []
    for i in range(steps + 1):
        t = t0 + timedelta(minutes=i * STEP_MIN)
        alt = N.SITE.at(N.TS.from_datetime(t)).observe(N.EPH["sun"]).apparent().altaz()[0].degrees
        dark.append((t, alt < N.DUSK_SUN_ALT))
    periods, start = [], None
    for t, d in dark:
        if d and start is None:
            start = t
        if not d and start is not None:
            periods.append((start, t)); start = None
    if start is not None:
        periods.append((start, dark[-1][0]))
    return [(s.astimezone(), e.astimezone()) for s, e in periods]


_MOON = {}


def moon_state(t):
    key = t.replace(second=0, microsecond=0).timestamp()
    if key in _MOON:
        return _MOON[key]
    ts = N.TS.from_datetime(t.astimezone(timezone.utc))
    ap = N.SITE.at(ts)
    m = ap.observe(N.EPH["moon"]).apparent(); s = ap.observe(N.EPH["sun"]).apparent()
    alt, az, _ = m.altaz()
    phase = m.separation_from(s).degrees          # 0 new .. 180 full
    lit = (1 - math.cos(math.radians(phase))) / 2
    ra, dec, _ = m.radec()
    _MOON[key] = (alt.degrees, ra.hours, dec.degrees, lit)
    return _MOON[key]


def windows(target, period):
    """Contiguous stretches inside `period` when the target is usable. Returns [(start, end)]."""
    start, end = period; out = []; cur = None
    t = start
    while t <= end:
        alt, az = N.altaz(target["ra"], target["dec"], N.TS.from_datetime(t.astimezone(timezone.utc)))
        ok = N.visible(alt, az)
        if ok:
            malt, mra, mdec, lit = moon_state(t)
            if malt > 0:
                sep = N.sep_deg(target["ra"], target["dec"], mra, mdec)
                need = 20 if target["type"] == "cluster" else (60 if lit > 0.6 else 35)
                ok = sep > need
        if ok and cur is None:
            cur = t
        if not ok and cur is not None:
            out.append((cur, t)); cur = None
        t += timedelta(minutes=STEP_MIN)
    if cur is not None:
        out.append((cur, end))
    return out


def recipe(target):
    typ = target.get("type", "other")
    base = {"cluster": {"lp": False, "a": 0.004, "crop": "square1050"}, "galaxy": {"lp": False, "a": 0.0015, "crop": "square1050"},
            "nebula": {"lp": True, "a": 0.001, "crop": "wide1400"}}.get(typ, {"lp": False, "a": 0.0015, "crop": "square1050"})
    return {"lp": target.get("lp", base["lp"]), "a": target.get("a", base["a"]), "crop": target.get("crop", base["crop"])}


def refill(plan, nights=2):
    """Add picks for tonight (from now) and the next night. Returns the number added."""
    periods = dark_periods()[:nights]
    if not periods:
        return 0
    now = N.now()
    periods = [(s_, e_ - timedelta(minutes=20)) for s_, e_ in periods]
    periods[0] = (max(periods[0][0], now), periods[0][1])
    have = {norm(it.get("object")) for it in plan["items"] if it.get("status") in ("pending", "running")}
    done = imaged_objects(plan)
    cands = [t for t in catalog() if t.get("pipeline", "stack") == "stack" and norm(t["object"]) not in have and (t.get("repeatable") or norm(t["object"]) not in done)]
    added = 0
    for n, period in enumerate(periods):
        label = "tonight" if n == 0 else "next"
        # busy time already committed to pending items this night
        cursor = period[0]
        scored = []
        for t in cands:
            for ws, we in windows(t, period):
                mins = (we - ws).total_seconds() / 60
                if mins < 25:
                    continue
                use = min(t["minutes"], int(mins) - 5)
                score = t.get("interest", 2) * min(1.0, mins / 90) * (use / t["minutes"])
                scored.append((score, ws, we, use, t))
        scored.sort(key=lambda x: -x[0])
        chosen = []
        # first-fit by score into a single timeline (one telescope), around what is already planned
        busy = []
        for it in plan["items"]:
            if it.get("status") not in ("pending", "running"):
                continue
            nb, na = N.item_window(it)
            if it.get("night") and it["night"] != period[0].strftime("%Y-%m-%d") and not (nb and period[0] <= nb <= period[1]):
                continue
            st = nb if nb and period[0] <= nb <= period[1] else (cursor if n == 0 else period[0])
            if it.get("status") == "running" and it.get("started"):
                st = datetime.fromisoformat(it["started"])
            if period[0] - timedelta(hours=1) <= st <= period[1]:
                busy.append((st, st + timedelta(minutes=it["minutes"] + SETTLE_MIN)))
        for score, ws, we, use, t in scored:
            if any(c[4]["id"] == t["id"] for c in chosen):
                continue
            s = max(ws, cursor if not busy else ws)
            # find a free slot inside the window
            slot = None; probe = ws
            while probe + timedelta(minutes=use) <= we:
                if all(not (probe < b_e and probe + timedelta(minutes=use) > b_s) for b_s, b_e in busy):
                    slot = probe; break
                probe += timedelta(minutes=STEP_MIN)
            if slot is None:
                continue
            busy.append((slot, slot + timedelta(minutes=use + SETTLE_MIN)))
            chosen.append((score, slot, min(we, slot + timedelta(minutes=use + 60)), use, t))
        chosen.sort(key=lambda x: x[1])
        for score, s, e, use, t in chosen:
            r = recipe(t)
            item = {"id": f"{t['id']}-{s.strftime('%m%d')}", "name": t["name"], "scope_name": t["scope_name"], "title": t["title"], "object": t["object"],
                    "kind": "Deep sky", "type": t["type"], "display": t["display"], "why": t.get("why", ""), "minutes": use, "lp": r["lp"], "a": r["a"], "crop": r["crop"],
                    "credit": "AI", "source": "ai", "not_before": s.strftime("%H:%M"), "not_after": e.strftime("%H:%M"), "night": period[0].strftime("%Y-%m-%d"),
                    "planned_for": label, "status": "pending", "ra": t["ra"], "dec": t["dec"]}
            plan["items"].append(item); added += 1
            cands = [c for c in cands if c["id"] != t["id"]]
            N.log(f"planner: {label} {s.strftime('%H:%M')}-{e.strftime('%H:%M')} {t['name']} {use} min (score {score:.1f})")
    return added


def sync_requests(plan):
    """Mirror the public queue into the plan: new visitor requests become items at the front;
    AI items pick up the site's manual order and planned_for."""
    c = N.cfg()
    if c is None:
        return
    try:
        rows = N.public_site.get_requests(c, status="scheduled") + N.public_site.get_requests(c, status="accepted")
    except Exception as ex:
        N.log("sync_requests failed:", str(ex).splitlines()[0][:120]); return
    by_rid = {it.get("request_id"): it for it in plan["items"] if it.get("request_id")}
    for r in rows:
        it = by_rid.get(r["id"])
        if it:
            it["position"] = r.get("position"); it["site_planned_for"] = r.get("planned_for")
            continue
        if r.get("source", "visitor") != "visitor" or not r.get("target"):
            continue
        # a visitor request accepted on /admin: add it to the front
        target = r["target"].strip()
        cat = next((t for t in catalog() if norm(t["name"]) == norm(target) or norm(t["object"]) == norm(target) or norm(t["title"]) == norm(target)), None)
        try:
            ra, dec = (cat["ra"], cat["dec"]) if cat and "ra" in cat else N.resolve(target)
        except Exception as ex:
            N.log(f"request #{r['id']}: cannot resolve target {target!r}: {ex}")
            N.patch_request(r["id"], triage_note=f"could not find '{target}' in the sky catalogue")
            continue
        if cat and cat.get("pipeline", "stack") != "stack":
            N.log(f"request #{r['id']}: {target} needs the {cat['pipeline']} pipeline, not built yet; left scheduled")
            continue
        rc = recipe(cat or {"type": "other"})
        item = {"id": f"req{r['id']}", "name": cat["name"] if cat else target, "scope_name": cat["scope_name"] if cat else re.sub(r"\s+", "", target),
                "title": cat["title"] if cat else target, "object": cat["object"] if cat else target, "kind": "Deep sky", "type": cat["type"] if cat else "other",
                "display": r.get("display_prompt") or target, "why": "visitor request", "minutes": cat["minutes"] if cat else 45,
                "lp": rc["lp"], "a": rc["a"], "crop": rc["crop"], "credit": "AI", "source": "visitor", "request_id": r["id"],
                "position": r.get("position"), "status": "pending", "ra": ra, "dec": dec}
        plan["items"].insert(0, item)
        N.log(f"request #{r['id']} from {r.get('submitter_name') or 'anonymous'}: {target} added to the plan")


def battery_horizon(now):
    """When the battery will give out, if the scope is unplugged: ~9 h from full without the heater
    (ZWO's figure for the S50 Pro), ~6.5 h with it. None when on mains or unknown."""
    pct, charging = N.battery()
    if pct is None or charging:
        return None
    hours_full = 6.5 if N.LAST.get("heater") else 9.0
    return now + timedelta(hours=hours_full * max(0, pct - N.BATTERY_STOP) / 100)


def mark_planned_for(plan):
    """Walk pending items in pick order with a time cursor; those that fit before dawn (or before the
    battery gives out) are 'tonight'."""
    periods = dark_periods(hours=14)
    if not periods:
        return
    now = N.now()
    dawn = periods[0][1] if periods[0][0] <= now + timedelta(hours=6) else periods[0][0]
    bh = battery_horizon(now)
    if bh and bh < dawn:
        N.log(f"planner: battery limits tonight to about {bh.strftime('%-I:%M %p')}"); dawn = bh
    cursor = max(now, periods[0][0])
    running = [it for it in plan["items"] if it.get("status") == "running"]
    if running:
        it = running[0]
        try:
            started = datetime.fromisoformat(it["started"])
            cursor = max(cursor, started + timedelta(minutes=it["minutes"] + SETTLE_MIN))
        except Exception:
            pass
    pend = [it for it in plan["items"] if it.get("status") == "pending"]
    for it in pend:
        if not it.get("request_id") and it.get("source", "ai") != "visitor":
            N.ensure_request(it)
    tonight = periods[0][0].strftime("%Y-%m-%d") if periods[0][0] <= now + timedelta(hours=6) else None
    def start_key(it):
        nb, _ = N.item_window(it)
        return (0 if it.get("source") == "visitor" else 1, it.get("position") if it.get("position") is not None else 1e9, (nb or now).timestamp())
    pend.sort(key=start_key)
    for it in pend:
        nb, na = N.item_window(it)
        start = max(cursor, nb) if nb else cursor
        fits = start + timedelta(minutes=it["minutes"]) <= dawn and (na is None or start + timedelta(minutes=10) < na)
        if it.get("night") and tonight and it["night"] > tonight:
            fits = False
        label = "tonight" if fits else "next"
        if fits:
            cursor = start + timedelta(minutes=it["minutes"] + SETTLE_MIN)
        if it.get("planned_for") != label or it.get("site_planned_for") != label:
            it["planned_for"] = label; it["site_planned_for"] = label
            if it.get("request_id"):
                N.patch_request(it["request_id"], planned_for=label)
