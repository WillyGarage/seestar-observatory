"""
sunguard - keep an unfiltered scope from pointing at (or near) the Sun.

Without the solar filter, sunlight focused by the telephoto (and the wide camera beside it)
can cook the sensors in seconds. The scope's own firmware does not refuse a goto or a manual
move toward the Sun, so this module does.

install() wraps the seestarpy calls that move the scope:
  * raw.scope_goto(ra, dec)             refused if the target is within MARGIN of the Sun
  * raw.iscope_start_view(...)          mode="sun" refused unless the filter is declared on;
                                        with ra/dec, refused near the Sun; without a target,
                                        refused if the scope already points near the Sun
  * raw.scope_speed_move(...)           refused in daylight if the scope points within
                                        MARGIN + NUDGE_PAD of the Sun (a nudge goes a few deg)

The margin is wide on purpose: in daylight there is no plate solve, so pointing is only as good
as the compass (it has been several degrees off, see SKILL.md). Default 30 deg, override with
SEESTAR_SUN_MARGIN.

Solar work declares the filter with SEESTAR_FILTER_ON=1 (env) or sunguard.FILTER_ON = True, set
only after the black-sky filter check passes. seestar.py calls install() on import, so the CLI and
every script that does `import seestar` get the guard. Scripts that import seestarpy directly do
not; call sunguard.install() in them.

Backstop for long daytime runs (the Sun moves ~15 deg/hour): call check_pointing() before every
exposure, and bright_frame() on frames to stop if the image is mostly blown out.
"""
import math, os

MARGIN = float(os.environ.get("SEESTAR_SUN_MARGIN", "30"))
NUDGE_PAD = 10.0
FILTER_ON = os.environ.get("SEESTAR_FILTER_ON", "") == "1"


class SunTooClose(RuntimeError):
    pass


_eph = None


def _sky():
    global _eph
    from pathlib import Path
    from skyfield.api import Loader, wgs84
    import config
    if _eph is None:
        load = Loader(str(Path(__file__).resolve().parent / "data"))
        _eph = (load.timescale(), load("de421.bsp"))
    ts, eph = _eph
    lat, lon = config.require_site()
    return ts, eph, eph["earth"] + wgs84.latlon(lat, lon)


def sun_altaz():
    ts, eph, obs = _sky()
    alt, az, _ = obs.at(ts.now()).observe(eph["sun"]).apparent().altaz()
    return alt.degrees, az.degrees


def _sep(alt1, az1, alt2, az2):
    a1, z1, a2, z2 = map(math.radians, (alt1, az1, alt2, az2))
    c = math.sin(a1) * math.sin(a2) + math.cos(a1) * math.cos(a2) * math.cos(z1 - z2)
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def sep_altaz(alt, az):
    salt, saz = sun_altaz()
    return _sep(alt, az, salt, saz)


def sep_radec(ra_hours, dec_deg):
    from skyfield.api import Star
    ts, eph, obs = _sky()
    t = ts.now()
    alt, az, _ = obs.at(t).observe(Star(ra_hours=ra_hours, dec_degrees=dec_deg)).apparent().altaz()
    return sep_altaz(alt.degrees, az.degrees)


def daylight():
    return sun_altaz()[0] > -6  # civil twilight counts: the Sun's glow is still near the horizon


def _refuse(msg):
    raise SunTooClose(msg + f" (margin {MARGIN:.0f} deg; set SEESTAR_FILTER_ON=1 only with the solar filter on)")


def check_radec(ra_hours, dec_deg):
    if FILTER_ON or not daylight():
        return
    s = sep_radec(float(ra_hours), float(dec_deg))
    if s < MARGIN:
        _refuse(f"target is {s:.1f} deg from the Sun")


def check_pointing(raw=None, pad=0.0):
    """Refuse if the scope currently points within MARGIN + pad of the Sun (daylight only)."""
    if FILTER_ON or not daylight():
        return
    if raw is None:
        from seestarpy import raw
    alt, az = raw.scope_get_horiz_coord()["result"][:2]
    s = sep_altaz(alt, az)
    if s < MARGIN + pad:
        _refuse(f"scope points {s:.1f} deg from the Sun (alt {alt:.1f} az {az:.1f})")


def bright_frame(gray, frac=0.5, thr=250):
    """True if more than `frac` of an 8-bit frame is near white: stop and move away."""
    import numpy as np
    g = np.asarray(gray)
    return g.size > 0 and float((g >= thr).mean()) > frac


def install(raw=None):
    if raw is None:
        from seestarpy import raw
    if getattr(raw, "_sunguard", False):
        return
    goto0, view0, move0 = raw.scope_goto, raw.iscope_start_view, raw.scope_speed_move

    def scope_goto(ra, dec, *a, **k):
        check_radec(ra, dec)
        return goto0(ra, dec, *a, **k)

    def iscope_start_view(*a, **k):
        mode = k.get("mode", "star")
        if mode == "sun":
            if not FILTER_ON:
                _refuse("Sun view requested without the filter declared on")
        elif k.get("ra") is not None and k.get("dec") is not None:
            check_radec(k["ra"], k["dec"])
        else:
            check_pointing(raw)
        return view0(*a, **k)

    def scope_speed_move(*a, **k):
        check_pointing(raw, pad=NUDGE_PAD)
        return move0(*a, **k)

    raw.scope_goto, raw.iscope_start_view, raw.scope_speed_move = scope_goto, iscope_start_view, scope_speed_move
    raw._sunguard = True
