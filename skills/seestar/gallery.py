#!/usr/bin/env python
"""Gallery bookkeeping and publishing to the public site

  gallery.py build            # (optional) local thumbnails + index.html into captures/gallery/_site
  gallery.py publish          # push every not-yet-pushed picture to the public site
  gallery.py add FILE ... [--featured] [--credit NAME|AI] [--private]
                              # copy a finished picture into the gallery and stub its metadata
  gallery.py sync             # push metadata changes (credit/featured/title/notes) for items already on the public site

Layout (see SKILL.md):
  captures/gallery/*.jpg      finished pictures, named <date>-<slug>.jpg
  captures/gallery/meta.json  per-file title/object/taken/frames/... (hand-edited)
  captures/gallery/_site/     generated; this is what gets copied to tao
"""
import json, os, re, shutil, subprocess, sys, html
from datetime import datetime
from pathlib import Path

import public_site

# Framing wording ("close up", "2x zoom") only makes sense next to a paired shot. The
# featured (displayed) version of a pair gets the clean name on the public site; the
# stored-but-hidden versions keep their full title so the archive stays unambiguous.
_FRAMING_RE = re.compile(r",?\s*(?:close[\s-]?up|(?:\d+\s*x\s*)?zoom(?:ed)?)\s*$", re.I)


def public_title(title):
    """Title as it should appear on the public site: no 'close up' / 'zoom' framing."""
    return _FRAMING_RE.sub("", title or "").strip()


def public_info(m, f):
    """The meta record as the public site wants it. Credit is WHO ASKED for the picture:
    the owner's name for their own requests, "AI" for the ones the system picked, or the request's
    submitter (via request_id). Never "By"."""
    featured = bool(m.get("featured"))
    title = m.get("title") or f.stem
    info = {
        "title": public_title(title) if featured else title, "object": m.get("object", ""), "kind": m.get("kind", ""),
        "taken": m.get("taken"), "frames": m.get("frames"), "sub": m.get("sub"),
        "total_exp": m.get("total_exp"), "filter": m.get("filter", ""), "gain": m.get("gain"),
        "notes": m.get("notes", ""), "credit": m.get("credit") or "AI", "featured": featured,
    }
    if m.get("request_id"):
        info["request_id"] = m["request_id"]
    return info

import config
CAP = config.CAPTURES
GAL = CAP / "gallery"
SITE = GAL / "_site"
TAO = config.PUBLISH_HOST
REMOTE = config.get("SEESTAR_GALLERY_REMOTE_DIR", "~/sky")
URL = config.get("SEESTAR_GALLERY_REMOTE_URL", "")


def load_meta():
    p = GAL / "meta.json"
    return json.loads(p.read_text()) if p.exists() else {}


def thumb(src: Path, dst: Path, width: int):
    import cv2
    img = cv2.imread(str(src))
    h, w = img.shape[:2]
    if w > width:
        img = cv2.resize(img, (width, max(1, round(h * width / w))), interpolation=cv2.INTER_AREA)
    dst.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(dst), img, [cv2.IMWRITE_JPEG_QUALITY, 86])
    return img.shape[1], img.shape[0]


def build():
    import cv2
    meta = load_meta()
    if SITE.exists():
        shutil.rmtree(SITE)
    (SITE / "full").mkdir(parents=True)
    (SITE / "thumb").mkdir(parents=True)

    items = []
    for f in sorted(GAL.glob("*.jpg")):
        m = meta.get(f.name, {})
        img = cv2.imread(str(f))
        if img is None:
            print(f"  skip (unreadable) {f.name}")
            continue
        h, w = img.shape[:2]
        taken = m.get("taken") or datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds")
        shutil.copy2(f, SITE / "full" / f.name)
        thumb(f, SITE / "thumb" / f.name, 640)
        items.append({
            "file": f.name,
            "title": m.get("title", f.stem),
            "object": m.get("object", ""),
            "kind": m.get("kind", ""),
            "taken": taken,
            "width": w, "height": h,
            "bytes": f.stat().st_size,
            "frames": m.get("frames"), "sub": m.get("sub"), "total_exp": m.get("total_exp"),
            "filter": m.get("filter", ""), "gain": m.get("gain"),
            "notes": m.get("notes", ""),
        })
        print(f"  {f.name}  {w}x{h}")

    items.sort(key=lambda i: i["taken"], reverse=True)
    (SITE / "data.json").write_text(json.dumps(items, indent=1))
    (SITE / "index.html").write_text(page(items))
    print(f"built {len(items)} pictures -> {SITE}")
    return items


def page(items):
    data = json.dumps(items)
    return """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sky</title>
<style>
:root{color-scheme:light}
*{box-sizing:border-box}
body{margin:0;font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
     background:#f6f7f9;color:#1b1d21}
header{padding:20px 20px 12px;max-width:1200px;margin:0 auto}
h1{margin:0 0 4px;font-size:22px;font-weight:650;letter-spacing:-.01em}
.sub{color:#697080;font-size:13px}
.bar{position:sticky;top:0;z-index:20;background:rgba(246,247,249,.94);
     backdrop-filter:blur(8px);border-bottom:1px solid #e3e6ea;padding:10px 20px}
.bar .in{max-width:1200px;margin:0 auto;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
label{font-size:13px;color:#697080}
select,input,button{font:inherit;font-size:13px;padding:6px 9px;border:1px solid #d3d8de;
     border-radius:7px;background:#fff;color:#1b1d21}
button{cursor:pointer}
button.on{background:#1b1d21;color:#fff;border-color:#1b1d21}
.grid{max-width:1200px;margin:0 auto;padding:18px 20px 60px;
      columns:4 260px;column-gap:18px}
@media(max-width:700px){.grid{columns:1}}
.card{background:#fff;border:1px solid #e3e6ea;border-radius:12px;overflow:hidden;
      box-shadow:0 1px 2px rgba(20,24,31,.05);break-inside:avoid;margin:0 0 18px}
.card img{width:100%;display:block;background:#0b0d10;cursor:zoom-in}
.meta{padding:11px 13px 13px}
.t{font-weight:600;font-size:14px;margin-bottom:2px}
.o{font-size:12px;color:#697080}
.tags{margin-top:8px;display:flex;gap:6px;flex-wrap:wrap}
.tag{font-size:11px;background:#eef1f5;color:#4a5160;border-radius:5px;padding:2px 6px}
.notes{margin-top:8px;font-size:12.5px;color:#4a5160}
table{max-width:1200px;margin:0 auto;padding:0 20px 60px;border-collapse:collapse;width:100%}
td,th{padding:7px 10px;border-bottom:1px solid #e3e6ea;text-align:left;font-size:13px;white-space:nowrap}
th{cursor:pointer;color:#697080;font-weight:600;user-select:none}
th:hover{color:#1b1d21}
td.n{white-space:normal}
tbody tr:hover{background:#fff}
#lb{position:fixed;inset:0;background:rgba(12,14,18,.94);display:none;z-index:50;
    align-items:center;justify-content:center;flex-direction:column;padding:16px}
#lb.open{display:flex}
#lb img{max-width:100%;max-height:calc(100% - 52px);object-fit:contain}
#lb .cap{color:#dfe3e9;font-size:13px;margin-top:10px;text-align:center}
#lb .cap a{color:#8fb8ff}
.empty{max-width:1200px;margin:40px auto;padding:0 20px;color:#697080}
</style>
<header>
  <h1>Sky</h1>
  <div class="sub">Pictures from a Seestar S50 Pro.</div>
</header>
<div class="bar"><div class="in">
  <label for="sort">Sort</label>
  <select id="sort">
    <option value="taken-desc">Newest first</option>
    <option value="taken-asc">Oldest first</option>
    <option value="title-asc">Name A-Z</option>
    <option value="title-desc">Name Z-A</option>
    <option value="object-asc">Object A-Z</option>
    <option value="total_exp-desc">Longest exposure</option>
    <option value="bytes-desc">Largest file</option>
  </select>
  <select id="kind"><option value="">All types</option></select>
  <input id="q" type="search" placeholder="Search" style="flex:1;min-width:120px">
  <button id="bgrid" class="on">Grid</button>
  <button id="blist">List</button>
</div></div>
<div class="grid" id="grid"></div>
<table id="list" hidden><thead><tr>
  <th data-k="title">Name</th><th data-k="object">Object</th><th data-k="kind">Type</th>
  <th data-k="taken">Taken</th><th data-k="total_exp">Exposure</th>
  <th data-k="frames">Frames</th><th data-k="width">Size</th><th data-k="bytes">File</th>
</tr></thead><tbody></tbody></table>
<div id="lb"><img><div class="cap"></div></div>
<script>
const ITEMS = __DATA__;
const $ = s => document.querySelector(s);
const fdate = s => new Date(s).toLocaleString([], {year:'numeric',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
const fexp = v => v == null ? '' : v >= 60 ? (v/60).toFixed(v>=600?0:1)+' min' : v >= 1 ? v+' s' : v*1000+' ms';
const fsize = b => b > 1e6 ? (b/1048576).toFixed(1)+' MB' : Math.round(b/1024)+' KB';

[...new Set(ITEMS.map(i=>i.kind).filter(Boolean))].sort().forEach(k=>{
  const o=document.createElement('option'); o.value=o.textContent=k; $('#kind').append(o);
});

function sorted(){
  const [k,dir] = $('#sort').value.split('-');
  const q = $('#q').value.trim().toLowerCase();
  const kind = $('#kind').value;
  let a = ITEMS.filter(i => (!kind || i.kind===kind) &&
    (!q || (i.title+' '+i.object+' '+i.kind+' '+i.notes).toLowerCase().includes(q)));
  a.sort((x,y)=>{
    let p=x[k], r=y[k];
    if(p==null)p=''; if(r==null)r='';
    const c = typeof p==='number'&&typeof r==='number' ? p-r : String(p).localeCompare(String(r));
    return dir==='desc' ? -c : c;
  });
  return a;
}

function render(){
  const a = sorted();
  $('#grid').innerHTML = a.map(i=>`
    <div class="card">
      <img loading="lazy" src="thumb/${i.file}" data-f="${i.file}" alt="${i.title}">
      <div class="meta">
        <div class="t">${i.title}</div>
        <div class="o">${i.object} &middot; ${fdate(i.taken)}</div>
        <div class="tags">
          ${i.total_exp?`<span class="tag">${fexp(i.total_exp)} total</span>`:''}
          ${i.frames>1?`<span class="tag">${i.frames} frames</span>`:''}
          ${i.filter&&i.filter!=='None'?`<span class="tag">${i.filter} filter</span>`:''}
          <span class="tag">${i.width}&times;${i.height}</span>
        </div>
        ${i.notes?`<div class="notes">${i.notes}</div>`:''}
      </div>
    </div>`).join('') || '<div class="empty">Nothing matches.</div>';
  $('#list tbody').innerHTML = a.map(i=>`
    <tr><td class="n"><a href="full/${i.file}">${i.title}</a></td><td>${i.object}</td><td>${i.kind}</td>
    <td>${fdate(i.taken)}</td><td>${fexp(i.total_exp)}</td><td>${i.frames??''}</td>
    <td>${i.width}&times;${i.height}</td><td>${fsize(i.bytes)}</td></tr>`).join('');
}

$('#sort').onchange = $('#kind').onchange = render;
$('#q').oninput = render;
document.querySelectorAll('#list th').forEach(th=>th.onclick=()=>{
  const k = th.dataset.k, cur = $('#sort').value;
  $('#sort').value = cur === k+'-asc' ? k+'-desc' : k+'-asc';
  if(![...$('#sort').options].some(o=>o.value===$('#sort').value)){
    const o=document.createElement('option'); o.value=o.textContent=$('#sort').value; $('#sort').append(o);
    $('#sort').value=o.value;
  }
  render();
});
function view(v){
  const l = v==='list';
  $('#grid').hidden=l; $('#list').hidden=!l;
  $('#blist').classList.toggle('on',l); $('#bgrid').classList.toggle('on',!l);
  history.replaceState(null,'',l?'#list':'#');
}
$('#bgrid').onclick=()=>view('grid');
$('#blist').onclick=()=>view('list');
if(location.hash==='#list') view('list');
$('#grid').onclick = e => {
  const f = e.target.dataset?.f; if(!f) return;
  const i = ITEMS.find(x=>x.file===f);
  $('#lb img').src = 'full/'+f;
  $('#lb .cap').innerHTML = `${i.title} &middot; ${fdate(i.taken)} &middot; <a href="full/${f}">full size ${i.width}&times;${i.height}</a>`;
  $('#lb').classList.add('open');
};
$('#lb').onclick = e => { if(e.target.tagName!=='A') $('#lb').classList.remove('open') };
addEventListener('keydown', e => e.key==='Escape' && $('#lb').classList.remove('open'));
render();
</script>
""".replace("__DATA__", data)


def publish():
    """Push to the public site. The optional rsync-to-a-static-host path is legacy; the supported target is at
    Every picture lives on the public site, hidden ones included, and the
    /admin page there is the all-photos view. `gallery.py build` still makes a local page if wanted."""
    push_public()


def push_public():
    """Upload every gallery item to the public site. The site STORES everything (tests,
    full-frame versions, site surveys included) and DISPLAYS only featured items. An item
    opts out entirely with "public": false. Already-pushed items (published_public) are
    skipped; use sync() to update their metadata."""
    cfg = public_site.load_config()
    if cfg is None:
        print("public-site not configured (~/.config/seestar/public-site.env missing) — skipping public push")
        return
    meta = load_meta()
    pushed = 0
    for name, m in meta.items():
        if m.get("public") is False or m.get("published_public"):
            continue
        f = GAL / name
        if not f.exists():
            print(f"  skip (missing file) {name}")
            continue
        result = public_site.post_photo(cfg, f, public_info(m, f))
        m["published_public"] = True
        m["public_id"] = result.get("id")
        pushed += 1
        print(f"  pushed {name} -> id {result.get('id')}" + ("  [featured]" if m.get("featured") else ""))
        (GAL / "meta.json").write_text(json.dumps(meta, indent=2))
    total = sum(1 for m in meta.values() if m.get("published_public"))
    shown = sum(1 for m in meta.values() if m.get("published_public") and m.get("featured"))
    print(f"public push: {pushed} pushed; {total} stored on site, {shown} featured")


def sync_public():
    """PATCH already-pushed items so the site matches meta.json (credit, featured, title, notes...)."""
    cfg = public_site.load_config()
    if cfg is None:
        print("public-site not configured — nothing to sync")
        return
    meta = load_meta()
    n = 0
    for name, m in meta.items():
        pid = m.get("public_id")
        if not pid or m.get("public") is False:
            continue
        public_site.patch_photo(cfg, pid, **public_info(m, GAL / name))
        n += 1
        if m.pop("reupload", False):
            public_site.put_photo_file(cfg, pid, GAL / name)
            print(f"  re-uploaded file for {name} (id {pid})")
            (GAL / "meta.json").write_text(json.dumps(meta, indent=2))
        print(f"  synced {name} (id {pid})" + ("  [featured]" if m.get("featured") else ""))
    print(f"public sync: {n} updated")


def add(paths, public=True, featured=False, credit="AI"):
    meta = load_meta()
    for p in paths:
        p = Path(p)
        dst = GAL / p.name
        shutil.copy2(p, dst)
        m = meta.setdefault(p.name, {
            "title": p.stem, "object": "", "kind": "",
            "taken": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
            "frames": None, "sub": None, "total_exp": None, "filter": "", "gain": None, "notes": "",
        })
        m["credit"] = credit
        m["featured"] = bool(featured)
        if not public:
            m["public"] = False
        else:
            m.pop("public", None)
        print(f"  + {p.name}  credit={credit}" + ("  [featured]" if featured else "") + ("" if public else "  [private]"))
    (GAL / "meta.json").write_text(json.dumps(meta, indent=2))
    print("edit captures/gallery/meta.json, then: gallery.py publish")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", nargs="?", default="build", choices=["add", "build", "publish", "sync"])
    ap.add_argument("files", nargs="*")
    ap.add_argument("--public", action="store_true", help="add: store on the public site (this is the default)")
    ap.add_argument("--private", action="store_true", help="add: keep the file off the public site entirely")
    ap.add_argument("--featured", action="store_true", help="add: show it on the public gallery page (default: stored but hidden)")
    ap.add_argument("--credit", default="AI", help="add: who asked for the picture - a person's name or 'AI' (default AI)")
    a = ap.parse_args()
    if a.action == "add":
        add(a.files, public=not a.private, featured=a.featured, credit=a.credit)
    elif a.action == "sync":
        sync_public()
    else:
        {"build": build, "publish": publish}[a.action]()
