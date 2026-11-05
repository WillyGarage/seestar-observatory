// Photo viewer: fit-to-window by default, every-pixel (1:1) on demand, pan, pinch, wheel, swipe.
// Usage: Viewer.open(list, index) where list items are {full, thumb, title, sub, w, h}.
(function () {
  const css = `
#pv{position:fixed;inset:0;z-index:100;background:#0b0c0f;display:none;touch-action:none;user-select:none;-webkit-user-select:none;overflow:hidden}
#pv.open{display:block}
#pv .pvstage{position:absolute;inset:0;overflow:hidden;cursor:default}
#pv img{cursor:zoom-in}#pv.zoomed img,#pv.zoomed .pvstage{cursor:grab}#pv.drag img,#pv.drag .pvstage{cursor:grabbing}
#pv img{position:absolute;left:0;top:0;transform-origin:0 0;max-width:none;max-height:none;will-change:transform;image-rendering:auto}
#pv img.pvlo{filter:blur(2px)}
#pv .pvbar{position:absolute;left:0;right:0;display:flex;align-items:center;gap:8px;padding:10px 12px;color:#e8ebf0;
  font:14px/1.3 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;pointer-events:none;transition:opacity .2s}
#pv .pvtop{top:0;background:linear-gradient(#000a,#0000)}
#pv .pvbot{bottom:0;background:linear-gradient(#0000,#000a);justify-content:center;padding-bottom:max(12px,env(safe-area-inset-bottom))}
#pv .pvttl{flex:1;min-width:0}#pv .pvttl b{display:block;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#pv .pvttl span{color:#aab1bc;font-size:12px}
#pv button{backdrop-filter:none;pointer-events:auto;border:0;background:#ffffff1f;color:#fff;height:38px;min-width:38px;padding:0 12px;border-radius:19px;
  font:600 14px system-ui,-apple-system,sans-serif;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;gap:6px}
#pv button:hover{background:#ffffff33}#pv button[hidden]{display:none}
#pv .pvpill{pointer-events:none;background:#0009;border-radius:12px;padding:3px 10px;font-size:12px;color:#cfd5de}
#pv .pvnav{position:absolute;top:50%;transform:translateY(-50%);width:44px;height:64px;border-radius:12px;font-size:28px;transition:opacity .2s}
#pv .pvprev{left:10px}#pv .pvnext{right:10px}
#pv .pvhint{position:absolute;left:50%;bottom:64px;transform:translateX(-50%);background:#000b;color:#e8ebf0;border-radius:16px;
  padding:7px 14px;font:13px system-ui,sans-serif;pointer-events:none;transition:opacity .6s;white-space:nowrap}
#pv .pvnav{background:#0008;border:1px solid #ffffff33}#pv .pvnav:hover{background:#000c}
#pv .xb{gap:6px}
`;
  const st = document.createElement('style'); st.textContent = css; document.head.appendChild(st);
  const el = document.createElement('div'); el.id = 'pv';
  el.innerHTML = `<div class="pvstage"><img alt="" draggable="false"></div>
<div class="pvbar pvtop"><div class="pvttl"><b></b><span></span></div>
 <button class="zb" title="Show every pixel (or click the picture)"></button><button class="xb" title="Back to the gallery (Esc)">&#10005; Close</button></div>
<button class="pvnav pvprev" aria-label="Previous">&#8249;</button><button class="pvnav pvnext" aria-label="Next">&#8250;</button>
<div class="pvbar pvbot"><span class="pvpill"></span></div><div class="pvhint"></div>`;
  document.body.appendChild(el);
  const stage = el.querySelector('.pvstage'), img = el.querySelector('img'), zb = el.querySelector('.zb'),
        pill = el.querySelector('.pvpill'), hint = el.querySelector('.pvhint');
  let list = [], idx = 0, W = 0, H = 0, iw = 1, ih = 1, s = 1, x = 0, y = 0, fit = 1, anim = 0;

  function size() { W = stage.clientWidth; H = stage.clientHeight; fit = Math.min(W / iw, H / ih); }
  const one = () => 1 / (window.devicePixelRatio || 1);       // 1 picture pixel = 1 screen pixel
  const maxS = () => Math.max(one() * 4, fit * 2);
  function clamp() {
    const w = iw * s, h = ih * s;
    x = w <= W ? (W - w) / 2 : Math.min(0, Math.max(W - w, x));
    y = h <= H ? (H - h) / 2 : Math.min(0, Math.max(H - h, y));
  }
  function draw(smooth) {
    clamp();
    img.style.transition = smooth ? 'transform .25s ease' : 'none';
    img.style.transform = `translate(${x}px,${y}px) scale(${s})`;
    const atFit = Math.abs(s - fit) < 1e-3, big = fit < one() * 0.999, pct = Math.round(s / one() * 100);
    el.classList.toggle('zoomed', !atFit);
    zb.hidden = !big && atFit;
    zb.textContent = atFit ? '1:1  Every pixel' : 'Fit to screen';
    pill.textContent = atFit ? (big ? `Whole picture · ${pct}%` : 'Whole picture') : `${Math.abs(pct - 100) < 2 ? 'Every pixel · 100%' : pct + '%'} · drag, scroll or arrow keys to look around`;
  }
  function zoomTo(ns, cx, cy, smooth) {          // keep the image point under (cx, cy) fixed
    ns = Math.max(fit, Math.min(maxS(), ns));
    x = cx - (cx - x) * ns / s; y = cy - (cy - y) * ns / s; s = ns; draw(smooth);
  }
  const toFit = () => { s = fit; x = 0; y = 0; draw(true); };
  function toggle(cx = W / 2, cy = H / 2) { Math.abs(s - fit) < 1e-3 ? zoomTo(Math.max(one(), fit * 2), cx, cy, true) : toFit(); }

  function show(i) {
    idx = (i + list.length) % list.length; const it = list[idx];
    el.querySelector('.pvttl b').textContent = it.title || ''; el.querySelector('.pvttl span').textContent = it.sub || '';
    el.querySelector('.pvprev').hidden = el.querySelector('.pvnext').hidden = list.length < 2;
    iw = it.w || 1000; ih = it.h || 1000; size(); s = fit; x = 0; y = 0;
    img.style.width = iw + 'px'; img.style.height = ih + 'px';
    img.classList.add('pvlo'); img.src = it.thumb || it.full; draw(false);
    const full = new Image(); const my = ++anim;
    full.onload = () => { if (my !== anim) return; if (!it.w) { iw = full.naturalWidth; ih = full.naturalHeight; img.style.width = iw + 'px'; img.style.height = ih + 'px'; size(); s = fit; } img.src = it.full; img.classList.remove('pvlo'); draw(false); };
    full.src = it.full;
  }
  let hintTimer;
  function open(l, i) {
    list = l; el.classList.add('open'); document.documentElement.style.overflow = 'hidden'; show(i);
    let seen = 0; try { seen = +localStorage.getItem('pv-hint') || 0; } catch (e) {}
    if (seen < 3) {
      hint.textContent = matchMedia('(hover:none)').matches ? 'Tap the picture or pinch to see every pixel' : 'Click the picture or scroll to see every pixel';
      hint.style.opacity = 1; clearTimeout(hintTimer); hintTimer = setTimeout(() => hint.style.opacity = 0, 2600);
      try { localStorage.setItem('pv-hint', seen + 1); } catch (e) {}
    } else hint.style.opacity = 0;
  }
  function close() { el.classList.remove('open'); document.documentElement.style.overflow = ''; anim++; img.removeAttribute('src'); }

  // pointers: drag to pan, pinch to zoom, swipe to change/close, tap to toggle the bars, double-tap to zoom
  const pts = new Map(); let start = null, pinch = null, moved = false;
  stage.addEventListener('dragstart', e => e.preventDefault());       // the browser's own image drag was stealing the mouse
  stage.addEventListener('pointerdown', e => {
    if (e.button > 0) return; e.preventDefault(); stage.setPointerCapture(e.pointerId); pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pts.size === 1) { start = { px: e.clientX, py: e.clientY, x, y, t: Date.now() }; moved = false; }
    if (pts.size === 2) { const [a, b] = [...pts.values()]; pinch = { d: Math.hypot(a.x - b.x, a.y - b.y), s, mx: (a.x + b.x) / 2, my: (a.y + b.y) / 2, x, y }; }
  });
  stage.addEventListener('pointermove', e => {
    if (!pts.has(e.pointerId)) return; pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    const r = stage.getBoundingClientRect();
    if (pts.size === 2 && pinch) {
      const [a, b] = [...pts.values()]; const d = Math.hypot(a.x - b.x, a.y - b.y), mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
      const ns = Math.max(fit, Math.min(maxS(), pinch.s * d / pinch.d));
      x = mx - r.left - (pinch.mx - r.left - pinch.x) * ns / pinch.s; y = my - r.top - (pinch.my - r.top - pinch.y) * ns / pinch.s; s = ns;
      moved = true; draw(false); return;
    }
    if (!start) return;
    const dx = e.clientX - start.px, dy = e.clientY - start.py;
    if (Math.hypot(dx, dy) > 6) moved = true;
    if (Math.abs(s - fit) > 1e-3) { el.classList.add('drag'); x = start.x + dx; y = start.y + dy; draw(false); }
    else { img.style.transition = 'none'; img.style.transform = `translate(${x + dx}px,${y + Math.max(0, dy)}px) scale(${s})`; }
  });
  function up(e) {
    if (!pts.has(e.pointerId)) return; pts.delete(e.pointerId); el.classList.remove('drag');
    if (pts.size === 1) { const [p] = [...pts.values()]; start = { px: p.x, py: p.y, x, y, t: Date.now() }; pinch = null; return; }
    if (pts.size) return;
    const wasPinch = !!pinch; pinch = null; if (!start) return;
    const dx = e.clientX - start.px, dy = e.clientY - start.py, fast = Date.now() - start.t < 600; start = null;
    if (wasPinch) { if (s < fit * 1.05) toFit(); return; }
    if (Math.abs(s - fit) < 1e-3 && moved) {               // swipes only at "whole picture"
      if (fast && Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) && list.length > 1) return show(idx + (dx < 0 ? 1 : -1));
      if (dy > 110 && Math.abs(dy) > Math.abs(dx)) return close();
      return draw(true);
    }
    if (moved) return;
    const r = stage.getBoundingClientRect(), cx = e.clientX - r.left, cy = e.clientY - r.top;
    const onImg = cx >= x && cx <= x + iw * s && cy >= y && cy <= y + ih * s;
    if (Math.abs(s - fit) < 1e-3) { if (onImg) toggle(cx, cy); else close(); }      // whole picture: click it = every pixel, click outside = back to gallery
    else toFit();                                                                    // zoomed: click = whole picture again
  }
  stage.addEventListener('pointerup', up); stage.addEventListener('pointercancel', up);
  stage.addEventListener('wheel', e => {
    e.preventDefault(); const r = stage.getBoundingClientRect(), zoomed = Math.abs(s - fit) > 1e-3;
    if (zoomed && !e.ctrlKey) { const k = e.deltaMode === 1 ? 40 : 1; x -= (e.shiftKey ? e.deltaY : e.deltaX) * k; y -= (e.shiftKey ? 0 : e.deltaY) * k; return draw(false); }
    zoomTo(s * Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0022)), e.clientX - r.left, e.clientY - r.top, false);
  }, { passive: false });
  zb.onclick = () => toggle();
  el.querySelector('.xb').onclick = close;
  el.querySelector('.pvprev').onclick = () => show(idx - 1);
  el.querySelector('.pvnext').onclick = () => show(idx + 1);
  addEventListener('keydown', e => {
    if (!el.classList.contains('open')) return;
    if (e.key === 'Escape') close();
    else if (e.key.startsWith('Arrow') && Math.abs(s - fit) > 1e-3) {
      e.preventDefault(); const st = (e.shiftKey ? 0.5 : 0.15) * Math.min(W, H);
      if (e.key === 'ArrowLeft') x += st; if (e.key === 'ArrowRight') x -= st; if (e.key === 'ArrowUp') y += st; if (e.key === 'ArrowDown') y -= st; draw(true);
    }
    else if (e.key === 'ArrowRight') show(idx + 1); else if (e.key === 'ArrowLeft') show(idx - 1);
    else if (e.key === '+' || e.key === '=') zoomTo(s * 1.4, W / 2, H / 2, true);
    else if (e.key === '-') zoomTo(s / 1.4, W / 2, H / 2, true);
    else if (e.key === '0' || e.key === ' ') { e.preventDefault(); toggle(); }
  });
  addEventListener('resize', () => { if (!el.classList.contains('open')) return; const wasFit = Math.abs(s - fit) < 1e-3; size(); if (wasFit) s = fit; draw(false); });
  window.Viewer = { open, close };
})();
