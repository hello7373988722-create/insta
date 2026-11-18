"""
Reel Saver
Share an Instagram reel to this installed web app and it downloads automatically.

Run on Render:
    build:  pip install -r requirements.txt
    start:  gunicorn app:app --workers 1 --threads 8 --timeout 120

Optional environment variables:
    SYNAX_API    override the primary downloader API
    IG_COOKIES   Netscape-format cookies text, used only by the yt-dlp fallback
"""
import json
import logging
import math
import os
import re
import struct
import tempfile
import threading
import time
import zlib
from urllib.parse import unquote

import requests
from flask import Flask, Response, jsonify, request

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("reelsaver")

SYNAX_API = os.environ.get("SYNAX_API", "https://synax-family-insta.synaxbots.workers.dev/")
UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
)
CACHE_TTL = 600  # seconds a resolved reel stays usable
RATE_LIMIT = 15  # lookups per minute per IP

COOKIE_FILE = None
if os.environ.get("IG_COOKIES"):
    COOKIE_FILE = os.path.join(tempfile.gettempdir(), "ig_cookies.txt")
    with open(COOKIE_FILE, "w") as fh:
        fh.write(os.environ["IG_COOKIES"])


# --------------------------------------------------------------------------
# Resolving a reel
# --------------------------------------------------------------------------
class ReelError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


POST_RE = re.compile(
    r"https?://(?:www\.|m\.)?(?:instagram\.com|instagr\.am)/(?:[A-Za-z0-9_.]+/)?(reels?|p|tv)/([A-Za-z0-9_-]+)",
    re.I,
)
SHARE_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/share/(?:(?:reel|p)/)?[A-Za-z0-9_-]+", re.I
)
CODE_RE = re.compile(r"[A-Za-z0-9_-]{5,40}")


def _canon(match):
    kind = "reel" if match.group(1).lower().startswith("reel") else match.group(1).lower()
    code = match.group(2)
    return code, f"https://www.instagram.com/{kind}/{code}/"


def find_post(raw):
    """Pull a (shortcode, clean url) out of whatever text Instagram shared."""
    raw = (raw or "").strip()
    share = SHARE_RE.search(raw)
    if share:  # short "share" links redirect to the real post
        try:
            final = unquote(
                requests.get(
                    share.group(0), headers={"User-Agent": UA}, timeout=10, allow_redirects=True
                ).url
            )
        except requests.RequestException:
            raise ReelError("Share link khul nahi paaya. Reel ka normal link try karo.", 502)
        found = POST_RE.search(final)
        if found:
            return _canon(found)
    found = POST_RE.search(raw)
    if found:
        return _canon(found)
    raise ReelError("Ye Instagram reel ka link nahi lag raha.", 400)


def via_synax(url):
    r = requests.get(SYNAX_API, params={"synaxurl": url}, headers={"User-Agent": UA}, timeout=25)
    r.raise_for_status()
    data = r.json()
    items = data.get("items") or []
    videos = [i for i in items if i.get("type") == "video" and i.get("download")]
    if not videos:
        if items:
            raise ReelError("Is post me video nahi hai.", 422)
        raise ValueError("empty response")
    return {
        "video": videos[0]["download"],
        "thumb": data.get("thumb"),
        "title": (data.get("title") or "").strip(),
        "headers": {},
        "source": "synax",
    }


def via_ytdlp(url):
    import yt_dlp  # imported late so the app still starts without it

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "best[ext=mp4]/best",
        "socket_timeout": 15,
    }
    if COOKIE_FILE:
        opts["cookiefile"] = COOKIE_FILE
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("entries"):
        info = next((e for e in info["entries"] if e), info)
    direct = info.get("url")
    if not direct and info.get("requested_formats"):
        direct = info["requested_formats"][0].get("url")
    if not direct:
        raise RuntimeError("no direct url")
    return {
        "video": direct,
        "thumb": info.get("thumbnail"),
        "title": (info.get("description") or info.get("title") or "").strip(),
        "headers": info.get("http_headers") or {},
        "source": "ytdlp",
    }


_cache, _hits, _lock = {}, {}, threading.Lock()


def cache_get(code):
    with _lock:
        entry = _cache.get(code)
        if entry and entry[0] > time.time():
            return entry[1]
        _cache.pop(code, None)
    return None


def cache_put(code, data):
    with _lock:
        if len(_cache) > 300:
            for key in sorted(_cache, key=lambda k: _cache[k][0])[:100]:
                _cache.pop(key, None)
        _cache[code] = (time.time() + CACHE_TTL, data)


def rate_limited(ip):
    now = time.time()
    with _lock:
        if len(_hits) > 2000:
            _hits.clear()
        recent = [t for t in _hits.get(ip, []) if now - t < 60]
        recent.append(now)
        _hits[ip] = recent
        return len(recent) > RATE_LIMIT


def resolve(code, url):
    data = cache_get(code)
    if data:
        return data
    try:
        data = via_synax(url)
    except ReelError:
        raise
    except Exception as exc:  # network error, bad JSON, empty answer
        log.warning("primary API failed (%s), trying yt-dlp", exc)
        try:
            data = via_ytdlp(url)
        except Exception as exc2:
            log.warning("yt-dlp failed: %s", exc2)
            raise ReelError(
                "Abhi reel nahi mil paayi. Reel private ho sakti hai, ya thodi der baad dobara try karo.",
                502,
            )
    cache_put(code, data)
    return data


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------
def fail(message, status):
    return jsonify(ok=False, error=message), status


def client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    return forwarded.split(",")[0].strip() or request.remote_addr or "?"


def stream(url, headers, mimetype, filename=None, keep_type=False):
    if not url or not url.startswith("https://"):
        raise ReelError("Video ka link sahi nahi hai.", 502)
    try:
        up = requests.get(url, headers=headers or {"User-Agent": UA}, stream=True, timeout=(10, 30))
        up.raise_for_status()
    except requests.RequestException as exc:
        log.warning("upstream fetch failed: %s", exc)
        raise ReelError("Video server se download nahi ho paaya. Dobara try karo.", 502)

    def chunks():
        try:
            for part in up.iter_content(65536):
                if part:
                    yield part
        finally:
            up.close()

    kind = up.headers.get("Content-Type", mimetype).split(";")[0] if keep_type else mimetype
    resp = Response(chunks(), mimetype=kind)
    if up.headers.get("Content-Length") and not up.headers.get("Content-Encoding"):
        resp.headers["Content-Length"] = up.headers["Content-Length"]
    if filename:
        resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        resp.headers["Cache-Control"] = "no-store"
    else:
        resp.headers["Cache-Control"] = "private, max-age=600"
    return resp


@app.get("/api/info")
def api_info():
    if rate_limited(client_ip()):
        return fail("Bahut zyada requests ho gayi. Ek minute ruko.", 429)
    try:
        code, url = find_post(request.args.get("link", ""))
        data = resolve(code, url)
    except ReelError as exc:
        return fail(str(exc), exc.status)
    first_line = data["title"].split("\n")[0].strip()[:140]
    return jsonify(
        ok=True,
        code=code,
        title=first_line,
        thumb=f"/api/thumb/{code}" if data.get("thumb") else None,
        file=f"/api/file/{code}",
        direct=(
            data["video"]
            if data.get("source") == "synax" and str(data["video"]).startswith("https://")
            else None
        ),
        filename=f"reel_{code}.mp4",
    )


@app.get("/api/file/<code>")
def api_file(code):
    data = cache_get(code) if CODE_RE.fullmatch(code) else None
    if not data:
        return fail("Link expire ho gaya. Reel dobara bhejo.", 410)
    try:
        return stream(data["video"], data["headers"], "video/mp4", filename=f"reel_{code}.mp4")
    except ReelError as exc:
        return fail(str(exc), exc.status)


@app.get("/api/thumb/<code>")
def api_thumb(code):
    data = cache_get(code) if CODE_RE.fullmatch(code) else None
    if not data or not data.get("thumb"):
        return Response(status=404)
    try:
        return stream(data["thumb"], {"User-Agent": UA}, "image/jpeg", keep_type=True)
    except ReelError:
        return Response(status=404)


@app.get("/healthz")
def healthz():
    return "ok"


# --------------------------------------------------------------------------
# PWA plumbing: manifest with share target, service worker, icons
# --------------------------------------------------------------------------
@app.get("/manifest.webmanifest")
def manifest():
    icons = []
    for size in (192, 512):
        for purpose in ("any", "maskable"):
            icons.append(
                {
                    "src": f"/icon-{size}.png",
                    "sizes": f"{size}x{size}",
                    "type": "image/png",
                    "purpose": purpose,
                }
            )
    body = {
        "name": "Reel Saver",
        "short_name": "Reel Saver",
        "description": "Instagram reel share karo, download apne aap.",
        "id": "/",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#ECEBFF",
        "theme_color": "#ECEBFF",
        "icons": icons,
        "share_target": {
            "action": "/share",
            "method": "GET",
            "params": {"title": "title", "text": "text", "url": "url"},
        },
    }
    return Response(json.dumps(body), mimetype="application/manifest+json")


SW_JS = """self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {});
"""


@app.get("/sw.js")
def service_worker():
    resp = Response(SW_JS, mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


_icons = {}


def make_icon(size):
    """Cobalt tile with a 9:16 frame that is filled to about half in sun yellow."""
    cobalt, white, sun, ink = (59, 47, 224), (255, 255, 255), (255, 200, 69), (20, 18, 59)
    hw, hh, rad = 0.17, 0.32, 0.07
    rows = bytearray()
    for py in range(size):
        rows.append(0)
        y = (py + 0.5) / size
        for px in range(size):
            x = (px + 0.5) / size
            qx = abs(x - 0.5) - (hw - rad)
            qy = abs(y - 0.5) - (hh - rad)
            dist = math.hypot(max(qx, 0), max(qy, 0)) + min(max(qx, qy), 0) - rad
            cover = min(1.0, max(0.0, 0.5 - dist * size))
            if cover <= 0:
                pixel = cobalt
            else:
                inner = white if y < 0.51 else (ink if y < 0.53 else sun)
                pixel = tuple(round(cobalt[i] * (1 - cover) + inner[i] * cover) for i in range(3))
            rows.extend(pixel)

    def chunk(tag, payload):
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(rows), 6))
        + chunk(b"IEND", b"")
    )


@app.get("/icon-<int:size>.png")
def icon(size):
    if size not in (192, 512):
        return Response(status=404)
    if size not in _icons:
        _icons[size] = make_icon(size)
    resp = Response(_icons[size], mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------
CSS = r"""
:root{
  --ink:#14123B; --cobalt:#3B2FE0; --mist:#ECEBFF; --lilac:#D8D5FF;
  --sun:#FFC845; --coral:#E8445F; --muted:#4A4770;
  color-scheme:light;
}
*{box-sizing:border-box;margin:0}
[hidden]{display:none!important}
html{-webkit-text-size-adjust:100%}
body{
  font-family:"Bricolage Grotesque",system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  background:var(--mist);color:var(--ink);min-height:100vh;min-height:100dvh;
  line-height:1.45;-webkit-tap-highlight-color:transparent;
}
a{color:inherit}
:focus-visible{outline:3px solid var(--cobalt);outline-offset:3px}
.wrap{max-width:520px;margin:0 auto;padding:20px 20px calc(32px + env(safe-area-inset-bottom))}
.brand{display:inline-flex;align-items:center;gap:10px;font-weight:800;font-size:19px;letter-spacing:-.02em;text-decoration:none}
.brand svg{width:20px;height:28px;display:block}
h1,h2{font-weight:800;letter-spacing:-.035em;line-height:1.04;font-variation-settings:"wdth" 86}

/* the 9:16 frame: the one signature element */
.frame{
  position:relative;width:min(62vw,250px);aspect-ratio:9/16;border-radius:28px;overflow:hidden;
  border:3px solid var(--ink);background:var(--lilac);isolation:isolate;--p:0%;
}
.layer{position:absolute;inset:0;background-size:cover;background-position:center}
.base{background-color:var(--lilac)}
.sharp{
  background-image:linear-gradient(165deg,var(--cobalt),var(--coral));
  clip-path:inset(calc(100% - var(--p)) 0 0 0);transition:clip-path .3s linear;
}
.frame.has-thumb .base{background-image:var(--thumb);filter:blur(16px) saturate(.15) brightness(1.08);transform:scale(1.15)}
.frame.has-thumb .sharp{background-image:var(--thumb)}
.edge{position:absolute;left:0;right:0;bottom:var(--p);height:4px;background:var(--sun);opacity:0;transition:bottom .3s linear}
[data-state=loading] .edge,.frame.static .edge{opacity:1}
[data-state=loading] .base:not(.x){animation:breathe 1.6s ease-in-out infinite}
.frame.has-thumb .base{animation:none!important}
@keyframes breathe{50%{opacity:.7}}
.play{position:absolute;left:50%;top:50%;width:44px;height:44px;margin:-22px 0 0 -22px;color:var(--ink);opacity:.35;display:none}
[data-state=idle] .play,.frame.static .play{display:block}
.frame.static .play{color:#fff;opacity:.9}
.mark{
  position:absolute;left:50%;top:50%;width:72px;height:72px;margin:-36px 0 0 -36px;border-radius:50%;
  display:none;place-items:center;border:3px solid var(--ink);
}
.mark svg{width:34px;height:34px}
.mark.ok{background:var(--sun)}
.mark.bad{background:var(--coral);color:#fff}
[data-state=done] .mark.ok,[data-state=error] .mark.bad{display:grid;animation:pop .35s cubic-bezier(.2,1.4,.4,1)}
[data-state=error] .frame{background:#FFD9DF}
@keyframes pop{from{transform:scale(.4);opacity:0}to{transform:scale(1);opacity:1}}

/* controls */
.btn{
  display:inline-flex;align-items:center;justify-content:center;gap:8px;min-height:52px;padding:0 28px;
  border-radius:999px;border:2px solid var(--ink);background:transparent;color:var(--ink);
  font-family:inherit;font-weight:700;font-size:17px;line-height:1;cursor:pointer;text-decoration:none;
}
.btn.primary{background:var(--cobalt);color:#fff;box-shadow:0 4px 0 var(--ink);transition:transform .08s,box-shadow .08s}
.btn.primary:active{transform:translateY(3px);box-shadow:0 1px 0 var(--ink)}
.btn.block{width:100%}
.link{display:inline-block;background:none;border:0;font-family:inherit;font-weight:600;font-size:16px;line-height:1;color:var(--muted);text-decoration:underline;text-underline-offset:4px;padding:12px;cursor:pointer}
.field{
  display:flex;align-items:center;gap:6px;background:#fff;border:2px solid var(--ink);
  border-radius:999px;padding:5px 5px 5px 20px;
}
.field input{flex:1;min-width:0;border:0;outline:0;background:transparent;font-family:inherit;font-weight:500;font-size:16px;line-height:1.2;color:var(--ink);min-height:44px}
.field input::placeholder{color:#7A77A3}
.field:focus-within{outline:3px solid var(--cobalt);outline-offset:3px}
.field .btn{min-height:44px;padding:0 18px;font-size:15px;background:var(--lilac)}
.go{margin-top:14px}
.fine{font-size:13.5px;color:var(--muted);max-width:42ch}

/* share screen */
.stage{
  min-height:calc(100vh - 90px);min-height:calc(100dvh - 90px);display:flex;flex-direction:column;
  align-items:center;justify-content:center;text-align:center;padding:12px 0 8px;
}
.pct{font-size:15px;font-weight:700;font-variant-numeric:tabular-nums;color:var(--cobalt);height:20px;margin-top:16px}
.stage h1{font-size:clamp(28px,8vw,36px)}
.sub{
  margin-top:10px;max-width:32ch;color:var(--muted);display:-webkit-box;-webkit-line-clamp:3;
  -webkit-box-orient:vertical;overflow:hidden;
}
.note{margin-top:8px;font-size:14px;color:var(--muted);min-height:20px}
.actions{display:flex;flex-direction:column;align-items:center;gap:6px;margin-top:18px;width:100%}
#btn-back,#btn-stay,#btn-retry,#paste-form{display:none}
[data-state=done] #btn-back,[data-state=done] #btn-stay,[data-state=error] #btn-retry{display:inline-flex}
[data-state=idle] #paste-form,[data-state=error] #paste-form{display:block}
#paste-form{width:100%;margin-top:18px;text-align:left}

/* home */
.hero{display:flex;gap:20px;align-items:flex-start;justify-content:space-between;padding:36px 0 26px}
.hero h1{font-size:clamp(36px,10.5vw,54px)}
.hero p{margin-top:14px;color:var(--muted);max-width:28ch}
.hero .frame{width:104px;flex:none;border-radius:22px;margin-top:6px}
.install{padding:34px 0 8px;border-top:2px solid var(--ink);margin-top:34px}
.install h2{font-size:26px}
.install .lead{margin-top:8px;color:var(--muted)}
.install .btn{margin-top:16px}
.track{list-style:none;padding:0;margin:22px 0 0}
.track li{position:relative;padding:0 0 22px 34px}
.track li::before{
  content:"";position:absolute;left:0;top:3px;width:16px;height:16px;border-radius:50%;
  border:3px solid var(--ink);background:var(--sun);
}
.track li:not(:last-child)::after{content:"";position:absolute;left:7.5px;top:22px;bottom:2px;width:3px;background:var(--ink)}
.track b{display:block;font-size:18px;letter-spacing:-.01em}
.track span{color:var(--muted)}
.tick{margin-top:16px;font-weight:700}
footer.fine{margin-top:26px}

@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{animation:none!important;transition:none!important}
}
"""

HEAD = r"""<!doctype html>
<html lang="hi-Latn">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#ECEBFF">
<title>__TITLE__</title>
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/icon-192.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wdth,wght@12..96,75..100,400..800&display=swap" rel="stylesheet">
<style>__CSS__</style>
</head>
"""

BRAND = r"""<a class="brand" href="/" aria-label="Reel Saver home">
<svg viewBox="0 0 20 28" aria-hidden="true"><rect x="1.5" y="1.5" width="17" height="25" rx="5" fill="#fff" stroke="#14123B" stroke-width="3"/><path d="M3 17h14v5a3 3 0 0 1-3 3H6a3 3 0 0 1-3-3z" fill="#FFC845"/></svg>
Reel Saver</a>"""

PLAY = r"""<svg class="play" viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5.5v13l11-6.5z" fill="currentColor"/></svg>"""

HOME_HTML = (
    HEAD
    + r"""<body>
<div class="wrap">
  <header>__BRAND__</header>

  <section class="hero">
    <div>
      <h1>Reel share karo, download apne aap.</h1>
      <p>Instagram me reel pe Share dabao, Reel Saver chuno. Video seedha phone me save.</p>
    </div>
    <div class="frame static" style="--p:62%" aria-hidden="true">
      <div class="layer base"></div><div class="layer sharp"></div><div class="edge"></div>__PLAY__
    </div>
  </section>

  <form id="go" autocomplete="off">
    <div class="field">
      <input id="link" type="url" inputmode="url" placeholder="Reel ka link paste karo" aria-label="Reel ka link" required>
      <button type="button" class="btn" id="paste">Paste</button>
    </div>
    <button class="btn primary block go" type="submit">Download karo</button>
  </form>

  <section class="install">
    <h2>Share menu me jodo</h2>
    <p class="lead">Ek baar install karna padta hai, uske baad reel se seedha share.</p>
    <button class="btn primary" id="install" hidden>Reel Saver install karo</button>
    <p class="tick" id="installed" hidden>Install ho chuka hai.</p>
    <ol class="track">
      <li><b>Install karo</b><span>Chrome ke menu me "Install app" chuno.</span></li>
      <li><b>Instagram me reel kholo</b><span>Share button dabao.</span></li>
      <li><b>Reel Saver chuno</b><span>Download shuru hota hai, phir Instagram pe wapas.</span></li>
    </ol>
  </section>

  <footer class="fine">Sirf apne ya permission wale reels download karo. Is app ka Instagram se koi sambandh nahi hai.</footer>
</div>
<script>
const $=s=>document.querySelector(s);
const inp=$('#link');
$('#go').addEventListener('submit',e=>{
  e.preventDefault();
  const v=inp.value.trim();
  if(v) location.href='/share?url='+encodeURIComponent(v);
});
$('#paste').addEventListener('click',async()=>{
  try{const t=await navigator.clipboard.readText(); if(t){inp.value=t;}}catch(_){}
  inp.focus();
});
let deferred=null;
addEventListener('beforeinstallprompt',e=>{e.preventDefault();deferred=e;$('#install').hidden=false;});
$('#install').addEventListener('click',async()=>{
  if(!deferred) return;
  deferred.prompt(); await deferred.userChoice; deferred=null; $('#install').hidden=true;
});
addEventListener('appinstalled',()=>{$('#install').hidden=true;$('#installed').hidden=false;});
if(matchMedia('(display-mode: standalone)').matches){$('#installed').hidden=false;}
if('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js');
</script>
</body>
</html>
"""
)

SHARE_HTML = (
    HEAD
    + r"""<body>
<main class="wrap" id="app" data-state="idle">
  <header>__BRAND__</header>
  <section class="stage">
    <div class="frame" id="frame">
      <div class="layer base"></div><div class="layer sharp"></div><div class="edge"></div>__PLAY__
      <div class="mark ok" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="#14123B" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg></div>
      <div class="mark bad" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg></div>
    </div>
    <p class="pct" id="pct" aria-hidden="true"></p>
    <h1 id="status" aria-live="polite">Reel ka link chahiye</h1>
    <p class="sub" id="sub">Instagram me reel pe Share dabao aur Reel Saver chuno. Ya link yahin paste karo.</p>
    <p class="note" id="note" aria-live="polite"></p>

    <div class="actions">
      <button class="btn primary" id="btn-back" type="button">Instagram pe wapas</button>
      <button class="link" id="btn-stay" type="button">Yahin ruko</button>
      <a class="link" id="btn-redo" target="_blank" rel="noopener" hidden>Nahi aaya? Yahan dabao</a>
      <button class="btn primary" id="btn-retry" type="button">Dobara try karo</button>
    </div>

    <form id="paste-form" autocomplete="off">
      <div class="field">
        <input id="link" type="url" inputmode="url" placeholder="Reel ka link paste karo" aria-label="Reel ka link" required>
        <button type="button" class="btn" id="paste">Paste</button>
      </div>
      <button class="btn primary block go" type="submit">Download karo</button>
    </form>
  </section>
</main>
<script>
const $=s=>document.querySelector(s);
const app=$('#app'), frame=$('#frame'), statusEl=$('#status'), sub=$('#sub'), note=$('#note'), pct=$('#pct'), inp=$('#link');
const INTENT='intent://#Intent;package=com.instagram.android;action=android.intent.action.MAIN;category=android.intent.category.LAUNCHER;end';
const redo=$('#btn-redo'), stay=$('#btn-stay');
let lastRaw='', backTimer=null, fakeTimer=null, current=0;

const setState=s=>{app.dataset.state=s;};
const progress=p=>{current=p;frame.style.setProperty('--p',p+'%');};
const say=(h,s)=>{statusEl.textContent=h;sub.textContent=s||'';};

function startFake(){
  stopFake();
  fakeTimer=setInterval(()=>{progress(current+(92-current)*0.04);},120);
}
function stopFake(){clearInterval(fakeTimer);fakeTimer=null;}

function startDirect(info){
  startFake();
  const f=document.createElement('iframe');
  f.style.display='none';
  f.src=info.direct;
  document.body.appendChild(f);
  setTimeout(()=>f.remove(),60000);
}

function goBack(){
  clearTimeout(backTimer);
  location.href=INTENT;
  setTimeout(()=>{try{window.close();}catch(_){}} ,1200);
}

function save(blob,name){
  const a=document.createElement('a');
  a.href=URL.createObjectURL(blob);
  a.download=name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(()=>URL.revokeObjectURL(a.href),60000);
}

async function fetchFile(info){
  const res=await fetch(info.file);
  if(!res.ok){
    let m='Video download nahi ho paayi.';
    try{m=(await res.json()).error||m;}catch(_){}
    throw new Error(m);
  }
  const total=+res.headers.get('Content-Length')||0;
  const reader=res.body.getReader();
  const chunks=[]; let got=0;
  if(!total) startFake();
  for(;;){
    const {done,value}=await reader.read();
    if(done) break;
    chunks.push(value); got+=value.length;
    if(total){
      const p=Math.min(99,got/total*100);
      progress(p); pct.textContent=Math.round(p)+'%';
    }
  }
  stopFake();
  return new Blob(chunks,{type:'video/mp4'});
}

async function run(raw){
  lastRaw=raw;
  clearTimeout(backTimer); stopFake();
  frame.classList.remove('has-thumb'); frame.style.removeProperty('--thumb');
  progress(0); pct.textContent=''; note.textContent='';
  redo.hidden=true; stay.hidden=false;
  setState('loading');
  say('Reel dhoondh rahe hain','');
  try{
    const r=await fetch('/api/info?link='+encodeURIComponent(raw));
    const info=await r.json();
    if(!r.ok||!info.ok) throw new Error(info.error||'Reel nahi mil paayi.');
    if(info.thumb){
      frame.style.setProperty('--thumb','url("'+info.thumb+'")');
      frame.classList.add('has-thumb');
    }
    if(info.direct){
      say('Download shuru ho raha hai',info.title||'Bas kuch second.');
      startDirect(info);
      await new Promise(r=>setTimeout(r,1400));
      stopFake(); progress(100); pct.textContent='';
      setState('done');
      say('Download shuru ho gaya','Chrome ke notification me progress dikhega. Video Downloads folder me aayegi.');
      redo.href=info.direct; redo.hidden=false; stay.hidden=true;
      return;
    }
    say('Download ho raha hai',info.title||'Bas kuch second.');
    const blob=await fetchFile(info);
    save(blob,info.filename);
    progress(100); pct.textContent='';
    setState('done');
    say('Ho gaya','Video Downloads folder me save ho gayi.');
    note.textContent='Instagram pe wapas ja rahe hain…';
    backTimer=setTimeout(goBack,2600);
  }catch(err){
    stopFake(); progress(0); pct.textContent='';
    setState('error');
    say('Download nahi ho paaya',err.message||'Kuch gadbad ho gayi. Dobara try karo.');
  }
}

function begin(raw){
  raw=(raw||'').trim();
  if(!/instagram\.com|instagr\.am/i.test(raw)){
    setState('error');
    say('Ye reel ka link nahi lag raha','Instagram reel ka link paste karo.');
    return;
  }
  run(raw);
}

$('#btn-back').addEventListener('click',goBack);
$('#btn-stay').addEventListener('click',()=>{clearTimeout(backTimer);note.textContent='';$('#btn-stay').hidden=true;});
$('#btn-retry').addEventListener('click',()=>begin(lastRaw));
$('#paste-form').addEventListener('submit',e=>{e.preventDefault();begin(inp.value);});
$('#paste').addEventListener('click',async()=>{
  try{const t=await navigator.clipboard.readText(); if(t){inp.value=t;}}catch(_){}
  inp.focus();
});

const q=new URLSearchParams(location.search);
const shared=['url','text','title'].map(k=>q.get(k)||'').join(' ').trim();
if(shared){begin(shared);} 
if('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js');
</script>
</body>
</html>
"""
)


def render(page, title):
    html = page.replace("__CSS__", CSS).replace("__TITLE__", title)
    html = html.replace("__BRAND__", BRAND).replace("__PLAY__", PLAY)
    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/")
def home():
    return render(HOME_HTML, "Reel Saver")


@app.get("/share")
def share():
    return render(SHARE_HTML, "Reel Saver")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
