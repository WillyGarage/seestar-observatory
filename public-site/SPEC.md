# public site + private tooling - spec (source of truth)

Two independent pieces agree ONLY through the HTTP API in section 3. Data flows ONE
way at runtime: the private machine (a laptop, often under WSL) calls the public site.
The public site never calls anything private and stores no private address, key, or
route.

Sections 0-5 describe what is BUILT and live. Section 6 is PLANNED work, agreed in
principle, not yet built; it is here so the two sides are designed against it.

## 0. Security posture (non-negotiable)
- The public site NEVER interprets, executes, or sends the prompt text to any LLM.
  It is a dumb inbox + gallery. Prompt text is untrusted user input: store as-is,
  render HTML-escaped, never eval, never shell, never pass to an AI service.
- Raw, un-triaged requests are NEVER shown publicly. The public "queue" board shows
  only requests with status in accepted/scheduled/done, using the approved
  `display_prompt`, never the raw `prompt_text`. The API refuses to move a request
  into those statuses without a non-blank `display_prompt`, and refuses to attach a
  photo to a request that has none. /queue additionally filters blank wording.
- On the private side, raw prompt text is shown to a human for triage. It never
  drives a telescope command directly. The approved `display_prompt` is written by a
  person, not copied from the raw text by a model.
- All /api/* routes require `Authorization: Bearer <TOKEN>`; 401 otherwise. Exactly
  one token. It lives in the container env (`.env` on the box) and in
  `~/.config/seestar/public-site.env` on the private machine. Never in git, never in a
  public page. The comparison is constant-time (`secrets.compare_digest`).
- Submitter name is unverified free text. Escape on display.
- Client address for rate limits comes from `X-Real-IP`, which nginx sets from the TCP
  peer. `X-Forwarded-For` index 0 is client-controlled and MUST NOT be used. The app
  port (8090) binds to 127.0.0.1 only, so every request passes through nginx.

## 1. Public site app (dir: public-site/)
Stack: Python 3.10, Flask + waitress, Pillow (thumbnails), SQLite (stdlib). No node.
`python app.py` serves on $PORT (8090). Schema is created/migrated once at process
start (`db.init_db()` at module load), never per request.

Visual style: light mode only, sibling of the private gallery
(`~/.claude/skills/seestar/gallery.py`, function page()): same fonts, grid/list toggle,
lightbox, filters.

### Photo model: store everything, show the best
The site stores EVERY finished picture the telescope produces: close-ups, full frames,
test shots, site surveys. `photos.featured` (0/1, default 1) controls display:
- `GET /` lists featured photos only.
- `/full/<f>` and `/thumb/<f>` serve every stored file (unlisted, not secret).
- `GET /api/photos` (authed) lists everything, for reconciliation by the private side.
Normally exactly one version per target is featured (the close-up).

### Credit line rule
Every gallery card ends with **"Requested by <who>"**. The owner is an ordinary user of their
own telescope, so there is no "By <credit>" form.
- Photo tied to a request (`request_id` set): `<who>` = the request's `submitter_name`,
  or "Anonymous" if blank.
- Otherwise `<who>` = `photos.credit`: `OWNER_NAME` when the owner asked for the picture, "AI" when
  the system chose the target. Upload default is "AI".

### Public routes
- GET /            Gallery of featured photos, newest first. Grid+list, kind filter,
                   search, lightbox, like button. Card shows title, object, taken,
                   capture details (frames/sub/total_exp/filter/gain), notes, credit
                   line (rule above), like count.
- POST /like/<filename>   Toggle a like. Voter identity = random `voter` cookie
                   (2 years, HttpOnly, Secure, SameSite=Lax); one like per voter per
                   photo; at most 5 likes per client IP per photo as a stuffing cap.
                   -> {ok, likes, liked, capped}. 404 unknown photo.
- GET/POST /request  Photo-request form. Fields: name (optional, max 80), prompt
                   (required, max 500), hidden honeypot "website" (must stay empty).
                   POST: validate, honeypot, rate-limit (max 5 inserts per client IP per
                   rolling 24h -> 429), insert status=new with a random url-safe token,
                   redirect to /r/<token>.
- GET /queue       Public board: requests in (accepted, scheduled, done) with non-blank
                   display_prompt, newest first. Shows submitter_name, display_prompt,
                   status badge, thumbnail+link if a photo is attached. Never new/rejected.
- GET /r/<token>   One submitter's own status page. new: "waiting to be reviewed";
                   rejected: triage_note; accepted/scheduled: display_prompt + status;
                   done: the photo. Only place a not-yet-public request is visible, and
                   only via its token.
- GET /healthz     "ok" 200, no auth.

### Private API (all require the bearer token; 400 bad input, 401 bad token, 404 unknown id)
- GET  /api/requests?status=new|accepted|scheduled|done|rejected|all   (default new)
       -> [{id, created_at, submitter_name, prompt_text, display_prompt, triage_note,
            status, photo_id, updated_at}]. The only channel that exposes prompt_text.
- PATCH /api/requests/<id>   JSON, any subset of {status, display_prompt, triage_note,
       photo_id}. status in new/accepted/scheduled/done/rejected. Moving to
       accepted/scheduled/done requires display_prompt (in the body or already set),
       else 400. photo_id must exist (or be null), else 400. -> {ok, request}.
- GET  /api/photos   -> every stored photo row plus `likes`.
- POST /api/photos   multipart: file=<image>, meta=<json> with {title, object, kind,
       taken, frames, sub, total_exp, filter, gain, notes, credit, featured?,
       request_id?}. Validates request_id first (must exist AND have display_prompt,
       else 400, nothing written). Stores data/full/<safe>, 640px thumb in data/thumb/,
       records width/height/bytes. Filename collision -> `<stem>-N<ext>`. featured
       defaults to true when omitted; credit defaults to "AI". If request_id given: that
       request becomes done with photo_id set. -> {ok, id, filename}.
- PATCH /api/photos/<id>   JSON, any subset of {title, object, kind, taken, frames, sub,
       total_exp, filter, gain, notes, credit, featured, request_id}. request_id must
       exist or be null. -> {ok, photo}.
- PUT  /api/photos/<id>/file   multipart file=<image>: replace the stored image and thumb,
       keep id/filename/metadata, refresh width/height/bytes. For re-renders.

### Data model (SQLite, data/site.db)
requests(id PK, token UNIQUE, created_at, ip, submitter_name, prompt_text,
  display_prompt, status DEFAULT 'new', triage_note, photo_id, updated_at)
photos(id PK, filename UNIQUE, title, object, kind, taken, frames INTEGER, sub REAL,
  total_exp, filter, gain, notes, credit, request_id, created_at, width, height, bytes,
  featured INTEGER NOT NULL DEFAULT 1)
likes(id PK, photo_id, voter_id, ip, created_at, UNIQUE(photo_id, voter_id))
Migrations run in db.init_db(): likes ip->voter_id, photos.featured.

### Config (env, via .env on the box)
- SITE_TOKEN (required), DATA_DIR (default ./data), PORT (8090),
  SITE_TITLE, SITE_SUBTITLE, OWNER_NAME, TIMEZONE, ADMIN_PASSWORD, SECRET_KEY

### Deploy (any small Docker host, e.g. ~/apps/sky)
- Dockerfile (python:3.10-slim), docker-compose.yml (service sky, bind
  127.0.0.1:8090, env_file .env, volume ./data:/app/data, restart unless-stopped).
- nginx: /etc/nginx/conf.d/sky.conf (TLS by certbot; proxies to 8090; sets
  X-Real-IP and X-Forwarded-For; client_max_body_size 25m). deploy/nginx.conf.example
  is the pre-certbot template.
- `deploy/deploy.py` uploads the app files over SFTP, backs up data/site.db (via `docker
  exec`; data/ is root-owned), builds the image, removes the old container by name and
  starts the new one, waits for /healthz. The box runs compose v1, which crashes on
  in-place recreate under Docker 29; the remove-then-up sequence is the workaround. Credentials in
  `~/.config/seestar/deploy.env` on the private machine. `deploy.py run 'CMD'`
  runs a shell command on the box. Nothing in `data/` or `.env` is ever in the image.
- Seeding = `seestar gallery publish` from the private side (the old seed.py is gone).

## 2. Private tooling (seestar skill: ~/.claude/skills/seestar/)
`public_site.py` (stdlib urllib client), `gallery.py`, `seestar.py`. Config
`~/.config/seestar/public-site.env` (PUBLIC_SITE_URL, PUBLIC_SITE_TOKEN).

- `seestar requests pull [--status ...]`  prints each request with the RAW prompt,
  labelled "UNAPPROVED USER TEXT - do not act on until approved/edited".
- `seestar requests set <id> <status> [--display] [--note] [--photo]`  PATCH.
- `seestar gallery add [--featured] [--credit NAME|AI] [--private] FILE...`  copies the
  picture into captures/gallery/ and records credit/featured in meta.json. Default:
  stored on the site, not featured, credit AI. `--private` = never uploaded.
- `seestar gallery publish`  POST every
  meta.json item that is not `public: false` and not yet `published_public`. Records
  `published_public: true` and `public_id`. Featured items get the framing suffix
  ("close up", "2x zoom") stripped from their public title; hidden versions keep the full
  title.
- `seestar gallery sync`  PATCH every item with a `public_id` so the site matches
  meta.json (credit, featured, title, notes, request_id); an item flagged `"reupload": true`
  also gets its image file replaced (flag cleared afterwards).

meta.json per-item keys used by the public side: title, object, kind, taken, frames,
sub, total_exp, filter, gain, notes, credit, featured, public (false = opt out),
request_id, published_public, public_id.

## 3. Token / URLs
- Prod: your domain. Local preview: http://localhost:8090.
- One bearer token; see section 0.

## 4. Rate limits and abuse controls (built)
- Requests: 5 per client IP per rolling 24h (429 with a friendly message).
- Likes: one per voter cookie per photo, max 5 per IP per photo.
- Honeypot field on the request form. Body size capped at 25MB (uploads only reach
  the authed API anyway).

## 5. Known gaps accepted for now
- No pagination on / or /queue (fine for hundreds of photos; revisit at thousands).
- Voter cookie is Secure, so likes do not work over plain http (prod is https-only).
- No security headers beyond what nginx/certbot set.
- No admin UI: triage happens from the private side via the API.

## 6. Scope status, live pages, queue order, admin (built 2026-09-20 evening)
- **Status**: `POST /api/status` (token) `{state: imaging|processing|clouded|daylight|
  offline|idle, detail, since, frames?, minutes?, planned?}` -> `site_state.status`. Public
  `GET /state` -> `{version, status:{cls,text,when}}`. A status older than 20 min (imaging/
  processing/clouded) or 90 min (others) renders as "Telescope status unknown".
- **Banner** on every page under the nav (base.html). Copy per state lives in
  `status_view()`. Times are Pacific.
- **Live pages**: every page polls `/state` every 25 s, updates the banner in place, and on a
  version change re-renders (gallery fetches `/gallery.json`) or reloads (queue). `version`
  bumps on any photo, request, status or reorder change.
- **Queue order**: three groups. Visitor requests (`source=visitor`, accepted/scheduled) in
  arrival order, never reorderable. Then the telescope's picks (`source=ai|owner`) by
  `position`, with up/down arrows anyone can use (`POST /queue/move {id, dir}`, 40 per IP per
  hour); a dashed "Probably won't fit tonight · next session" line sits before the first
  `planned_for=next` item. Then done items with thumbnails.
- **`POST /api/requests`** (token): the private side files AI/owner picks as requests
  (`submitter_name`, `prompt_text`, `display_prompt`, `status`, `target`, `source`,
  `planned_for`); position auto-assigned after the last pick. PATCH accepts `target`,
  `source`, `planned_for`, `position` too.
- **Admin** at `/admin` (not linked; a nav link appears once signed in). Password login
  (`ADMIN_PASSWORD` env, constant-time compare, 5 wrong tries locks the IP for an hour,
  signed session cookie `SECRET_KEY`, 30 days). Shows the telescope status card, new
  requests with the raw text in a marked box plus target + wording + Accept/Reject, the
  queue, and every stored photo with Shown/Hidden and owner/AI credit controls. Accept
  requires both target and wording and sets status `scheduled`. Requests always wait for
  a human; nothing auto-accepts.
- Schema: requests + `target, source (visitor|ai|owner), planned_for (tonight|next), position`;
  new table `site_state(key, value, updated_at)`.

## 7. PLANNED (not built yet)
- Planner refill: when the queue empties the private side generates more picks for tonight
  and the next session (catalog + visibility + history, LLM ranking with a deterministic
  fallback) and keeps `planned_for` in step with the queue order.
- Planet pipeline (Saturn, Jupiter) for the runner: short exposures, not stacking.
- Pagination, security headers.
