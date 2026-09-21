"""Everything that is specific to one telescope, one back garden and one machine.

Nothing personal is hard-coded anywhere else in this project. Settings are read from,
in order of precedence:

  1. the process environment
  2. ~/.config/seestar/seestar.env   (or the file named by $SEESTAR_CONFIG)
  3. the defaults below

That means you can override any single value for one command without editing a file:

    SEESTAR_IP=192.168.1.50 seestar status

Copy `seestar.env.example` to ~/.config/seestar/seestar.env and fill it in once.
"""
import os
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("SEESTAR_CONFIG", Path.home() / ".config/seestar/seestar.env"))

_file = {}
if CONFIG_PATH.exists():
    for _line in CONFIG_PATH.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            _file[_k.strip()] = _v.strip().strip('"').strip("'")


def get(key, default=None):
    """Environment beats the config file beats the default."""
    v = os.environ.get(key)
    if v is None:
        v = _file.get(key)
    return default if v is None else v


def getf(key, default):
    try:
        return float(get(key, default))
    except (TypeError, ValueError):
        return float(default)


HERE = Path(__file__).resolve().parent

# --- the telescope -----------------------------------------------------------
# Give the scope a DHCP reservation on your router so this never changes. WSL2 and
# most container setups cannot see UDP discovery or mDNS, so an IP is required.
SCOPE_IP = get("SEESTAR_IP", "")

# --- where you are -----------------------------------------------------------
# Decimal degrees, north and east positive. Used for Sun/Moon/planet positions and
# for deciding what is above the horizon tonight. Set it to the same place you told
# the Seestar app, or the mount's idea of the sky will not match the software's.
LAT = getf("SEESTAR_LAT", 0.0)
LON = getf("SEESTAR_LON", 0.0)
ELEV_M = getf("SEESTAR_ELEV_M", 0.0)

# IANA name, e.g. "America/Los_Angeles", "Europe/London". Local clock for logs,
# session folder names and "tonight" arithmetic.
TZ = get("SEESTAR_TZ", "UTC")

# --- where things land -------------------------------------------------------
# Every frame, stack and finished picture goes under here. Expect tens of GB.
CAPTURES = Path(get("SEESTAR_OUT", Path.home() / "seestar/captures")).expanduser()

# JPL ephemeris used by skyfield. Downloaded once by `make ephemeris`; roughly 17 MB,
# too big to keep in git.
EPHEMERIS = Path(get("SEESTAR_EPHEMERIS", HERE / "data/de421.bsp")).expanduser()

# --- optional: nearest airport weather station -------------------------------
# A four-letter ICAO/METAR identifier used for cloud cover before opening the arm.
# api.weather.gov is US-only; leave this blank outside the US and the runner falls
# back to the forecast grid, or to no weather check at all.
METAR_STATION = get("SEESTAR_METAR_STATION", "")
# api.weather.gov asks for a contact address in the User-Agent header.
WEATHER_CONTACT = get("SEESTAR_WEATHER_CONTACT", "seestar-observatory (set SEESTAR_WEATHER_CONTACT)")

# --- optional: scratch host for looking at images on a phone ------------------
# `seestar publish` copies files to PUBLISH_HOST:PUBLISH_DIR over ssh and prints
# PUBLISH_URL/<name>. Leave PUBLISH_HOST blank to turn the command off.
PUBLISH_HOST = get("SEESTAR_PUBLISH_HOST", "")
PUBLISH_DIR = get("SEESTAR_PUBLISH_DIR", "~/mockups")
PUBLISH_URL = get("SEESTAR_PUBLISH_URL", "").rstrip("/")


def require_ip():
    """Fail with something readable instead of a socket timeout."""
    if not SCOPE_IP:
        raise SystemExit(
            "No telescope address. Set SEESTAR_IP in %s (find the scope's IP in your "
            "router's client list and give it a DHCP reservation)." % CONFIG_PATH)
    return SCOPE_IP


def require_site():
    if LAT == 0.0 and LON == 0.0:
        raise SystemExit(
            "No observing site. Set SEESTAR_LAT and SEESTAR_LON in %s." % CONFIG_PATH)
    return LAT, LON
