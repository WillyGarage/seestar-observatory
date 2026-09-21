# public-site - the public gallery

Dumb inbox + gallery for a Seestar S50 Pro. Flask + waitress, SQLite, Pillow for
thumbnails. No node, no build step. `SPEC.md` is the source of truth; this file is
the operator's quick reference.

Security posture (SPEC section 0): prompt text from the request form is never
interpreted, executed, or sent to an LLM. It is stored as-is and rendered escaped.
Raw requests never appear on a public page. `/queue` shows only accepted requests and
only their approved `display_prompt`; the API refuses to accept a request without one.
All `/api/*` routes need `Authorization: Bearer <SITE_TOKEN>`.

## Run locally

```
cd public-site
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env      # edit SITE_TOKEN
.venv/bin/python app.py   # waitress on :8090 (or $PORT)
```

Data lives in `./data` (SQLite at `data/site.db`, images under `data/full/` and
`data/thumb/`), created on first start. The schema is created and migrated once at
process start.

## Getting pictures onto the site

From the private machine, with the `seestar` skill:

```
seestar gallery add --featured --credit Owner <file>  # shown on the gallery page
seestar gallery add <file>                            # stored, not shown
seestar gallery publish                               # uploads everything not yet pushed
seestar gallery sync                                  # pushes metadata edits for items already up
```

The site stores every picture (`photos.featured` = 0/1) and displays only featured
ones. Credit is who asked for the picture: a request's submitter, the owner, or "AI".
There is no separate seeding step any more.

## Routes

Public: `/` (featured gallery), `/gallery.json`, `/state` (banner status + change counter),
`POST /like/<filename>`, `/request` (GET/POST form), `/queue` (visitor requests, then the
telescope's reorderable picks, then done), `POST /queue/move`, `/r/<token>`, `/healthz`,
and `/full/<filename>` + `/thumb/<filename>` for every stored image.

Admin (password, not linked): `/admin`, `/admin/login`, `POST /admin/request/<id>`,
`POST /admin/photo/<id>`.

Private API (bearer token):
- `GET /api/requests?status=new|...|all`  includes raw `prompt_text` (the only place).
- `PATCH /api/requests/<id>`  `{status, display_prompt, triage_note, photo_id}`;
  accepted/scheduled/done require `display_prompt`; `photo_id` must exist.
- `GET /api/photos`  every stored photo with like counts.
- `POST /api/photos`  multipart `file` + `meta` JSON (`featured`, `credit`,
  `request_id` optional; `request_id` must exist and have wording).
- `PATCH /api/photos/<id>`  metadata, `featured`, `request_id`.
- `POST /api/status`  the telescope's current state for the banner.
- `POST /api/requests`  file an AI/owner pick so it shows on the queue.

## Deploy

Any small Linux box with Docker. The container listens on 127.0.0.1:8090 and nginx +
certbot sit in front of it; `deploy/nginx.conf.example` is the vhost to copy.

```
deploy/deploy.py                 # upload, back up the DB, docker compose up -d --build, wait for /healthz
deploy/deploy.py --no-build      # upload only
deploy/deploy.py run 'docker logs --tail 50 sky'
```

Credentials live in `~/.config/seestar/deploy.env` on the private machine
(`WG_HOST`, `WG_USER`, `WG_PASSWORD`, `WG_APP_DIR`). `.env` and `data/` never leave the
box and are excluded from the image by `.dockerignore`.

Backups: `deploy.py` copies `data/site.db` to `data/site.db.bak-<stamp>` before every
build. Images are in `data/full/`; copy that directory too for a full backup.

## Config (env)

| var | default | notes |
|---|---|---|
| `SITE_TOKEN` | - | required; the one bearer token for `/api/*` |
| `DATA_DIR` | `./data` | DB + image storage |
| `PORT` | `8090` | waitress listen port |
| `SITE_TITLE` | `Sky` | page `<title>` and the name in the header |
| `SITE_SUBTITLE` | `Pictures from a Seestar S50 Pro.` | the line under the name |
| `OWNER_NAME` | `Owner` | credited on pictures you asked for yourself |
| `TIMEZONE` | `UTC` | IANA name; every displayed time is local to this |
| `ADMIN_PASSWORD` | - | password for `/admin` (owner only) |
| `SECRET_KEY` | random per start | signs the admin session cookie; set it so logins survive restarts |
