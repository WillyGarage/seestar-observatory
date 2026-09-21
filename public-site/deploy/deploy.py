#!/usr/bin/env python3
"""Upload public-site/ to your server over ssh and rebuild the container.

  deploy/deploy.py              upload files, back up the DB, docker compose up --build, wait for /healthz
  deploy/deploy.py --no-build   upload only
  deploy/deploy.py run 'CMD'    run one shell command on the box and print its output

Credentials come from ~/.config/seestar/deploy.env:

    WG_HOST=sky.example.com
    WG_USER=you
    WG_PASSWORD=...          # or set up an ssh key and leave this out
    WG_APP_DIR=/home/you/apps/sky

Nothing here is copied into the container image: `.env` and `data/` live on the box and
are never uploaded or overwritten. Needs paramiko (`pip install paramiko`).
"""
import os
import posixpath
import sys
import time
from pathlib import Path

import paramiko

HERE = Path(__file__).resolve().parent.parent          # public-site/
CFG_PATH = Path(os.environ.get("SEESTAR_DEPLOY_CONFIG", Path.home() / ".config/seestar/deploy.env"))
FILES = [
    "app.py", "db.py", "requirements.txt", "Dockerfile", "docker-compose.yml", ".dockerignore",
    "README.md", "SPEC.md", "deploy/nginx.conf.example", "deploy/deploy.py",
]
GLOBS = ["templates/*.html", "static/*"]
REMOVE = ["seed.py"]   # files that used to ship and must not linger on the box


def cfg():
    out = {}
    for line in CFG_PATH.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    for k in ("WG_HOST", "WG_USER", "WG_PASSWORD", "WG_APP_DIR"):
        if not out.get(k):
            raise SystemExit(f"{CFG_PATH} is missing {k}")
    return out


def connect(c):
    cl = paramiko.SSHClient()
    cl.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cl.connect(c["WG_HOST"], username=c["WG_USER"], password=c["WG_PASSWORD"], timeout=20,
               look_for_keys=False, allow_agent=False)
    return cl


def run(cl, cmd, timeout=600):
    _, o, e = cl.exec_command(cmd, timeout=timeout)
    out, err = o.read().decode(errors="replace"), e.read().decode(errors="replace")
    rc = o.channel.recv_exit_status()
    if out.strip():
        print(out.rstrip())
    if err.strip():
        print(err.rstrip(), file=sys.stderr)
    return rc


def upload(cl, app_dir):
    sftp = cl.open_sftp()
    files = list(FILES)
    for g in GLOBS:
        files += [str(p.relative_to(HERE)) for p in HERE.glob(g)]
    for rel in files:
        src = HERE / rel
        if not src.exists():
            print(f"  skip (missing locally) {rel}")
            continue
        dst = posixpath.join(app_dir, rel)
        run(cl, f"mkdir -p {posixpath.dirname(dst)}")
        sftp.put(str(src), dst)
        print(f"  up {rel}")
    for rel in REMOVE:
        run(cl, f"rm -f {posixpath.join(app_dir, rel)}")
    sftp.close()


def deploy(build=True):
    c = cfg()
    cl = connect(c)
    app_dir = c["WG_APP_DIR"]
    print(f"== uploading to {c['WG_HOST']}:{app_dir}")
    upload(cl, app_dir)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    # data/ is owned by root (created by the container), so back up from inside the container.
    run(cl, f"docker exec sky sh -c 'cp /app/data/site.db /app/data/site.db.bak-{stamp} && echo db backed up to data/site.db.bak-{stamp}' 2>/dev/null || echo 'no running container; skipping db backup'")
    if build:
        # The box has compose v1 (`docker-compose`); newer boxes have the v2 plugin. Use whichever exists.
        compose = "docker compose" if run(cl, "docker compose version >/dev/null 2>&1") == 0 else "docker-compose"
        print(f"== {compose} up -d --build")
        # Build first so the swap is seconds. compose v1 crashes with KeyError 'ContainerConfig'
        # when it tries to *recreate* a container on Docker >= 25, so stop+rm+up instead of
        # letting it recreate in place.
        rc = run(cl, f"cd {app_dir} && {compose} build > /tmp/sky-build.log 2>&1; rc=$?; tail -3 /tmp/sky-build.log; exit $rc")
        if rc != 0:
            raise SystemExit(f"image build failed rc={rc}; see /tmp/sky-build.log on the box")
        # Remove the running container (and any "<hash>_sky" leftovers from a failed
        # recreate) by name, then start fresh. data/ is a bind mount, so nothing is lost.
        rc = run(cl, f"cd {app_dir} && (docker ps -aq --filter name=sky | xargs -r docker rm -f >/dev/null); {compose} up -d 2>&1 | tail -3")
        if rc != 0:
            raise SystemExit(f"container swap failed rc={rc}")
        for _ in range(30):
            time.sleep(1)
            _, o, _ = cl.exec_command("curl -s -m 3 http://127.0.0.1:8090/healthz")
            if o.read().decode().strip() == "ok":
                print("== healthz ok")
                break
        else:
            raise SystemExit("container did not become healthy; check `docker logs sky`")
    cl.close()


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["run"]:
        cl = connect(cfg())
        sys.exit(run(cl, " ".join(args[1:])))
    deploy(build="--no-build" not in args)
