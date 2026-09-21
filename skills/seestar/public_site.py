"""Client for the public gallery / photo-request inbox site (public-site/).

Talks to the HTTP API described in public-site/SPEC.md section 3. stdlib only
(urllib) - no new deps for the skill's venv. Config lives in
~/.config/seestar/public-site.env (PUBLIC_SITE_URL, PUBLIC_SITE_TOKEN); if
that file is missing or incomplete, load_config() returns None and callers
should skip the network step rather than crash.
"""
import json, mimetypes, os, urllib.error, urllib.request, uuid
from pathlib import Path

CONFIG_PATH = Path.home() / ".config/seestar/public-site.env"


class NotConfigured(Exception):
    """Raised by the request/patch/post helpers if called without a config."""


def load_config():
    """Return {"url", "token"} or None if the config file is missing/incomplete."""
    if not CONFIG_PATH.exists():
        return None
    cfg = {}
    for line in CONFIG_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip()
    url, token = cfg.get("PUBLIC_SITE_URL"), cfg.get("PUBLIC_SITE_TOKEN")
    if not url or not token:
        return None
    return {"url": url.rstrip("/"), "token": token}


def _call(cfg, method, path, data=None, headers=None):
    req = urllib.request.Request(cfg["url"] + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {cfg['token']}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as ex:
        body = ex.read().decode(errors="replace")
        raise RuntimeError(f"{method} {path} -> HTTP {ex.code}: {body[:300]}") from ex


def get_requests(cfg, status="new"):
    """GET /api/requests?status=... -> list of {id, created_at, submitter_name, prompt_text, status}."""
    if cfg is None:
        raise NotConfigured
    q = f"?status={status}" if status else ""
    return _call(cfg, "GET", f"/api/requests{q}")


def patch_request(cfg, request_id, **fields):
    """PATCH /api/requests/<id> with any subset of status/display_prompt/triage_note/photo_id."""
    if cfg is None:
        raise NotConfigured
    body = json.dumps({k: v for k, v in fields.items() if v is not None}).encode()
    return _call(cfg, "PATCH", f"/api/requests/{request_id}", data=body,
                 headers={"Content-Type": "application/json"})


def post_photo(cfg, file_path, meta):
    """POST /api/photos multipart: file=<image>, meta=<json>. Returns {ok, id, filename}."""
    if cfg is None:
        raise NotConfigured
    file_path = Path(file_path)
    boundary = uuid.uuid4().hex
    ctype = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    parts = []
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{file_path.name}"\r\n'
        f"Content-Type: {ctype}\r\n\r\n".encode()
        + file_path.read_bytes() + b"\r\n"
    )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="meta"\r\n\r\n'
        f"{json.dumps(meta)}\r\n".encode()
    )
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    return _call(cfg, "POST", "/api/photos", data=body,
                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})


def get_photos(cfg):
    """GET /api/photos -> every stored photo (featured or not) with like counts."""
    if cfg is None:
        raise NotConfigured
    return _call(cfg, "GET", "/api/photos")


def patch_photo(cfg, photo_id, **fields):
    """PATCH /api/photos/<id>: title/object/kind/taken/frames/sub/total_exp/filter/gain/notes/credit,
    featured (bool), request_id (int or None)."""
    if cfg is None:
        raise NotConfigured
    body = json.dumps(fields).encode()
    return _call(cfg, "PATCH", f"/api/photos/{photo_id}", data=body,
                 headers={"Content-Type": "application/json"})


def put_photo_file(cfg, photo_id, file_path):
    """PUT /api/photos/<id>/file: replace the stored image, keep id/filename/metadata."""
    if cfg is None:
        raise NotConfigured
    file_path = Path(file_path)
    boundary = uuid.uuid4().hex
    ctype = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{file_path.name}\"\r\n"
            f"Content-Type: {ctype}\r\n\r\n").encode() + file_path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    return _call(cfg, "PUT", f"/api/photos/{photo_id}/file", data=body,
                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})


def set_notice(cfg, heading="", body="", cls="warn"):
    """POST /api/notice: pin a standing notice above everything on the public site.

    Unlike the status banner this never expires - it stays until cleared. Pass
    empty heading and body to clear it.
    """
    if cfg is None:
        raise NotConfigured
    payload = json.dumps({"heading": heading, "body": body, "cls": cls}).encode()
    return _call(cfg, "POST", "/api/notice", data=payload,
                 headers={"Content-Type": "application/json"})
