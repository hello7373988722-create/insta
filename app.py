"""
Reel Saver
Share an Instagram reel to this installed web app and it downloads automatically.

Run on Render:
    build:  pip install -r requirements.txt
    start:  gunicorn app:app --workers 1 --threads 8 --timeout 120

Optional environment variables:
    USE_SYNAX    set to 1 to try the Synax API after yt-dlp (its video links only work on grabfy.com)
    SYNAX_API    override the Synax API address
    IG_COOKIES   Netscape-format cookies text, used by yt-dlp and gallery-dl
"""
import json
import logging
import math
import os
import re
import struct
import subprocess
import sys
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
            raise ReelError("Could not open the share link. Try the normal reel link.", 502)
        found = POST_RE.search(final)
        if found:
            return _canon(found)
    found = POST_RE.search(raw)
    if found:
        return _canon(found)
    raise ReelError("This does not look like an Instagram reel link.", 400)


VIDEO_EXT = {"mp4", "webm", "mov", "m4v"}
EXT_TYPES = {
    "mp4": "video/mp4", "webm": "video/webm", "mov": "video/quicktime", "m4v": "video/mp4",
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp",
    "heic": "image/heic",
}


def _ext_of(url, default):
    name = url.split("?", 1)[0].rsplit("/", 1)[-1]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return ext if ext in EXT_TYPES else default


def _item(url, kind=None):
    """One downloadable file: {"type": video|image, "url": ..., "ext": ...}."""
    if kind is None:
        kind = "video" if _ext_of(url, "") in VIDEO_EXT else "image"
    ext = _ext_of(url, "mp4" if kind == "video" else "jpg")
    if kind == "video" and ext not in VIDEO_EXT:
        ext = "mp4"
    if kind == "image" and ext in VIDEO_EXT:
        ext = "jpg"
    return {"type": kind, "url": url, "ext": ext}


def via_synax(url):
    r = requests.get(SYNAX_API, params={"synaxurl": url}, headers={"User-Agent": UA}, timeout=25)
    r.raise_for_status()
    data = r.json()
    items = [
        _item(i["download"], "video" if i.get("type") == "video" else "image")
        for i in (data.get("items") or [])
        if i.get("download")
    ]
    if not items:
        raise ValueError("empty response")
    return {
        "items": items,
        "thumb": data.get("thumb"),
        "title": (data.get("title") or "").strip(),
        "headers": {},
        "source": "synax",
    }


def _ydl_item(entry):
    direct = entry.get("url")
    if not direct and entry.get("requested_formats"):
        direct = entry["requested_formats"][0].get("url")
    if not direct:
        return None
    is_video = entry.get("ext") in VIDEO_EXT or entry.get("vcodec") not in (None, "none")
    item = _item(direct, "video" if is_video else None)
    item["thumb"] = entry.get("thumbnail")
    return item


def via_ytdlp(url):
    import yt_dlp  # imported late so the app still starts without it

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": False,  # carousels come back as a playlist, we want every item
        "format": "best[ext=mp4]/best",
        "socket_timeout": 15,
    }
    if COOKIE_FILE:
        opts["cookiefile"] = COOKIE_FILE
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    entries = [e for e in (info.get("entries") or []) if e] or [info]
    items = [i for i in (_ydl_item(e) for e in entries) if i]
    if not items:
        raise RuntimeError("no direct url")
    return {
        "items": items,
        "thumb": info.get("thumbnail") or next((e.get("thumbnail") for e in entries if e.get("thumbnail")), None),
        "title": (info.get("description") or info.get("title") or "").strip(),
        "headers": info.get("http_headers") or entries[0].get("http_headers") or {},
        "source": "ytdlp",
    }


def via_gallerydl(url):
    """Photos and carousels: ask gallery-dl for the direct file links."""
    cmd = [sys.executable, "-m", "gallery_dl", "-g"]
    if COOKIE_FILE:
        cmd += ["--cookies", COOKIE_FILE]
    cmd.append(url)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
    urls = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("| "):  # links gallery-dl hands over to yt-dlp
            line = line[2:].strip()
        if line.startswith("https://") and line not in urls:
            urls.append(line)
    if not urls:
        raise RuntimeError((proc.stderr or "gallery-dl found nothing").strip()[-200:])
    items = [_item(u) for u in urls]
    for it in items:
        it["thumb"] = it["url"] if it["type"] == "image" else None
    return {
        "items": items,
        "thumb": next((i["url"] for i in items if i["type"] == "image"), None),
        "title": "",
        "headers": {"User-Agent": UA},
        "source": "gallerydl",
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


USE_SYNAX = os.environ.get("USE_SYNAX", "0") == "1"
NOT_FOUND = "Could not find the post right now. It may be private, or try again in a bit."


def resolve(code, url):
    data = cache_get(code)
    if data:
        return data
    order = [via_ytdlp, via_gallerydl]
    if "/p/" in url:  # photo and carousel posts: gallery-dl first
        order.reverse()
    if USE_SYNAX:
        order.append(via_synax)
    for fn in order:
        try:
            data = fn(url)
            break
        except ReelError:
            raise
        except Exception as exc:
            log.warning("%s failed: %s", getattr(fn, "__name__", "resolver"), exc)
    else:
        raise ReelError(NOT_FOUND, 502)
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
        raise ReelError("The video link is invalid.", 502)
    try:
        up = requests.get(url, headers=headers or {"User-Agent": UA}, stream=True, timeout=(10, 30))
        up.raise_for_status()
    except requests.RequestException as exc:
        log.warning("upstream fetch failed: %s", exc)
        raise ReelError("Could not download from the video server. Please try again.", 502)

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


def _item_thumb(item):
    return item.get("thumb") or (item["url"] if item["type"] == "image" else None)


def _thumb_url(data):
    if data.get("thumb"):
        return data["thumb"]
    return next((i["url"] for i in data["items"] if i["type"] == "image"), None)


@app.get("/api/info")
def api_info():
    if rate_limited(client_ip()):
        return fail("Too many requests. Please wait a minute.", 429)
    try:
        code, url = find_post(request.args.get("link", ""))
        data = resolve(code, url)
    except ReelError as exc:
        return fail(str(exc), exc.status)
    items = data["items"]
    many = len(items) > 1
    out = [
        {
            "type": it["type"],
            "file": f"/api/file/{code}/{i}",
            "thumb": f"/api/thumb/{code}/{i}" if _item_thumb(it) else None,
            "filename": f"insta_{code}{'_' + str(i + 1) if many else ''}.{it['ext']}",
        }
        for i, it in enumerate(items)
    ]
    first_line = data["title"].split("\n")[0].strip()[:140]
    return jsonify(
        ok=True,
        code=code,
        title=first_line,
        thumb=f"/api/thumb/{code}" if _thumb_url(data) else None,
        items=out,
    )


@app.get("/api/file/<code>/<int:idx>")
def api_file(code, idx):
    data = cache_get(code) if CODE_RE.fullmatch(code) else None
    if not data:
        return fail("Link expired. Please send the post again.", 410)
    if not 0 <= idx < len(data["items"]):
        return fail("File not found.", 404)
    it = data["items"][idx]
    name = f"insta_{code}{'_' + str(idx + 1) if len(data['items']) > 1 else ''}.{it['ext']}"
    try:
        return stream(it["url"], data["headers"], EXT_TYPES.get(it["ext"], "application/octet-stream"), filename=name)
    except ReelError as exc:
        return fail(str(exc), exc.status)


@app.get("/api/thumb/<code>")
def api_thumb(code):
    data = cache_get(code) if CODE_RE.fullmatch(code) else None
    thumb = _thumb_url(data) if data else None
    if not thumb:
        return Response(status=404)
    try:
        return stream(thumb, {"User-Agent": UA}, "image/jpeg", keep_type=True)
    except ReelError:
        return Response(status=404)


@app.get("/api/thumb/<code>/<int:idx>")
def api_item_thumb(code, idx):
    data = cache_get(code) if CODE_RE.fullmatch(code) else None
    if not data or not 0 <= idx < len(data["items"]):
        return Response(status=404)
    thumb = _item_thumb(data["items"][idx])
    if not thumb:
        return Response(status=404)
    try:
        return stream(thumb, {"User-Agent": UA}, "image/jpeg", keep_type=True)
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
        "description": "Share an Instagram reel and it downloads automatically.",
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
[data-mode=paste][data-state=done] #btn-back,[data-mode=paste][data-state=done] #btn-stay{display:none}
[data-mode=paste][data-state=done] #paste-form{display:block}

/* picker: choose which files of a carousel to save */
#picker{display:none;width:100%;margin-top:22px}
[data-state=pick] #picker{display:block}
[data-state=pick] #frame,[data-state=pick] #pct{display:none}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:20px}
.tile{position:relative;aspect-ratio:4/5;padding:0;border:2px solid var(--ink);border-radius:14px;overflow:hidden;background:var(--lilac);cursor:pointer}
.tile img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;display:block}
.tile .kind{position:absolute;left:6px;bottom:6px;width:26px;height:26px;border-radius:50%;background:rgba(20,18,59,.72);color:#fff;display:grid;place-items:center}
.tile .kind svg{width:13px;height:13px}
.tile .num{position:absolute;top:6px;right:6px;width:28px;height:28px;border-radius:50%;border:2px solid #fff;background:rgba(20,18,59,.35);color:#fff;font-weight:800;font-size:14px;display:grid;place-items:center;font-variant-numeric:tabular-nums}
.tile[aria-pressed=true]{outline:4px solid var(--cobalt);outline-offset:-4px}
.tile[aria-pressed=true] .num{background:var(--cobalt)}
#picker .btn+.btn{margin-top:12px}
.btn:disabled{opacity:.4;box-shadow:none;cursor:default}
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
<html lang="en">
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
      <h1>Share a reel, download automatically.</h1>
      <p>Tap Share on a reel in Instagram, then choose Reel Saver. The video saves straight to your phone.</p>
    </div>
    <div class="frame static" style="--p:62%" aria-hidden="true">
      <div class="layer base"></div><div class="layer sharp"></div><div class="edge"></div>__PLAY__
    </div>
  </section>

  <form id="go" autocomplete="off">
    <div class="field">
      <input id="link" type="url" inputmode="url" placeholder="Paste the reel link" aria-label="Reel link" required>
      <button type="button" class="btn" id="paste">Paste</button>
    </div>
    <button class="btn primary block go" type="submit">Download</button>
  </form>

  <section class="install">
    <h2>Add to the Share menu</h2>
    <p class="lead">Install it once, then share straight from a reel.</p>
    <button class="btn primary" id="install" hidden>Install Reel Saver</button>
    <p class="tick" id="installed" hidden>Already installed.</p>
    <ol class="track">
      <li><b>Install it</b><span>Choose "Install app" in the Chrome menu.</span></li>
      <li><b>Open a reel in Instagram</b><span>Tap the Share button.</span></li>
      <li><b>Choose Reel Saver</b><span>The download starts, then you go back to Instagram.</span></li>
    </ol>
  </section>

  <footer class="fine">Only download your own reels or ones you have permission for. This app is not affiliated with Instagram.</footer>
</div>
<script>
const $=s=>document.querySelector(s);
const inp=$('#link');
$('#go').addEventListener('submit',e=>{
  e.preventDefault();
  const v=inp.value.trim();
  if(v) location.href='/share?src=paste&url='+encodeURIComponent(v);
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
    <h1 id="status" aria-live="polite">A reel link is needed</h1>
    <p class="sub" id="sub">Tap Share on a reel in Instagram and choose Reel Saver. Or paste the link here.</p>
    <p class="note" id="note" aria-live="polite"></p>

    <div class="actions">
      <button class="btn primary" id="btn-back" type="button">Back to Instagram</button>
      <button class="link" id="btn-stay" type="button">Stay here</button>
      <button class="link" id="btn-more" type="button" hidden>Choose more</button>
      <button class="btn primary" id="btn-retry" type="button">Try again</button>
    </div>

    <div id="picker">
      <div class="grid" id="grid"></div>
      <button class="btn primary block" id="btn-sel" type="button" disabled>Download (0)</button>
      <button class="btn block" id="btn-all" type="button">Download all</button>
    </div>

    <form id="paste-form" autocomplete="off">
      <div class="field">
        <input id="link" type="url" inputmode="url" placeholder="Paste the reel link" aria-label="Reel link" required>
        <button type="button" class="btn" id="paste">Paste</button>
      </div>
      <button class="btn primary block go" type="submit">Download</button>
    </form>
  </section>
</main>
<script>
const $=s=>document.querySelector(s);
const app=$('#app'), frame=$('#frame'), statusEl=$('#status'), sub=$('#sub'), note=$('#note'), pct=$('#pct'), inp=$('#link');
const INTENT='intent://#Intent;package=com.instagram.android;action=android.intent.action.MAIN;category=android.intent.category.LAUNCHER;end';
const stay=$('#btn-stay');
let lastRaw='', backTimer=null, fakeTimer=null, current=0;

const setState=s=>{app.dataset.state=s;};
const progress=p=>{current=p;frame.style.setProperty('--p',p+'%');};
const say=(h,s)=>{statusEl.textContent=h;sub.textContent=s||'';};

function startFake(){
  stopFake();
  fakeTimer=setInterval(()=>{progress(current+(92-current)*0.04);},120);
}
function stopFake(){clearInterval(fakeTimer);fakeTimer=null;}

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

async function fetchFile(item,idx,n){
  const res=await fetch(item.file);
  if(!res.ok){
    let m='Download failed.';
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
      const p=Math.min(99,(idx+got/total)/n*100);
      progress(p);
      pct.textContent=n>1?(idx+1)+' / '+n:Math.round(p)+'%';
    }
  }
  stopFake();
  return new Blob(chunks,{type:res.headers.get('Content-Type')||'application/octet-stream'});
}

const picker=$('#picker'), grid=$('#grid'), btnSel=$('#btn-sel'), btnAll=$('#btn-all'), more=$('#btn-more');
const PLAY='<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5.5v13l11-6.5z" fill="currentColor"/></svg>';
let lastInfo=null, sel=[];

function syncPicker(){
  [...grid.children].forEach((b,i)=>{
    const at=sel.indexOf(i);
    b.setAttribute('aria-pressed',at>=0?'true':'false');
    b.querySelector('.num').textContent=at>=0?String(at+1):'';
  });
  btnSel.textContent='Download ('+sel.length+')';
  btnSel.disabled=sel.length===0;
}

function renderPicker(info){
  sel=[]; grid.textContent='';
  info.items.forEach((it,i)=>{
    const b=document.createElement('button');
    b.type='button'; b.className='tile'; b.setAttribute('aria-pressed','false');
    b.setAttribute('aria-label',(it.type==='video'?'Video ':'Photo ')+(i+1));
    if(it.thumb){
      const im=document.createElement('img');
      im.src=it.thumb; im.alt=''; im.loading='lazy';
      b.appendChild(im);
    }
    if(it.type==='video'){
      const k=document.createElement('span'); k.className='kind'; k.innerHTML=PLAY; b.appendChild(k);
    }
    const nm=document.createElement('span'); nm.className='num'; b.appendChild(nm);
    b.addEventListener('click',()=>{
      const at=sel.indexOf(i);
      if(at>=0) sel.splice(at,1); else sel.push(i);
      syncPicker();
    });
    grid.appendChild(b);
  });
  btnAll.textContent='Download all ('+info.items.length+')';
  syncPicker();
}

function showPicker(){
  clearTimeout(backTimer); note.textContent=''; more.hidden=true;
  sel=[]; syncPicker();
  setState('pick');
  say(lastInfo.items.length+' files found','Tap the ones you want, then press Download.');
}

async function downloadItems(items,title){
  clearTimeout(backTimer); stopFake();
  progress(0); pct.textContent=''; note.textContent='';
  stay.hidden=false; more.hidden=true;
  setState('loading');
  const n=items.length;
  say('Downloading',n>1?n+' files':(title||'Just a few seconds.'));
  try{
    for(let i=0;i<n;i++){
      if(n>1){progress(i/n*100); pct.textContent=(i+1)+' / '+n;}
      const blob=await fetchFile(items[i],i,n);
      save(blob,items[i].filename);
      if(i<n-1) await new Promise(r=>setTimeout(r,600));
    }
    progress(100); pct.textContent='';
    setState('done');
    say('Done',n>1?n+' files saved to your Downloads folder.':(items[0].type==='image'?'Photo saved to your Downloads folder.':'Video saved to your Downloads folder.'));
    if(lastInfo&&lastInfo.items.length>1) more.hidden=false;
    if(app.dataset.mode!=='paste'){
      note.textContent='Going back to Instagram…';
      backTimer=setTimeout(goBack,2600);
    }else{
      inp.value='';
    }
  }catch(err){
    stopFake(); progress(0); pct.textContent='';
    setState('error');
    say('Download failed',err.message||'Something went wrong. Please try again.');
  }
}

async function run(raw){
  lastRaw=raw; lastInfo=null;
  clearTimeout(backTimer); stopFake();
  frame.classList.remove('has-thumb'); frame.style.removeProperty('--thumb');
  progress(0); pct.textContent=''; note.textContent='';
  stay.hidden=false; more.hidden=true;
  setState('loading');
  say('Looking for the post','');
  try{
    const r=await fetch('/api/info?link='+encodeURIComponent(raw));
    const info=await r.json();
    if(!r.ok||!info.ok) throw new Error(info.error||'Could not find the post.');
    const items=info.items||[];
    if(!items.length) throw new Error('Nothing downloadable was found in this post.');
    lastInfo=info;
    if(items.length>1){ renderPicker(info); showPicker(); return; }
    if(info.thumb){
      frame.style.setProperty('--thumb','url("'+info.thumb+'")');
      frame.classList.add('has-thumb');
    }
    await downloadItems(items,info.title);
  }catch(err){
    stopFake(); progress(0); pct.textContent='';
    setState('error');
    say('Download failed',err.message||'Something went wrong. Please try again.');
  }
}

function begin(raw){
  raw=(raw||'').trim();
  if(!/instagram\.com|instagr\.am/i.test(raw)){
    setState('error');
    say('This does not look like a reel link','Paste an Instagram reel link.');
    return;
  }
  run(raw);
}

$('#btn-back').addEventListener('click',goBack);
btnSel.addEventListener('click',()=>{ if(lastInfo&&sel.length) downloadItems(sel.map(i=>lastInfo.items[i]),lastInfo.title); });
btnAll.addEventListener('click',()=>{ if(lastInfo) downloadItems(lastInfo.items,lastInfo.title); });
more.addEventListener('click',()=>{ if(lastInfo) showPicker(); });
$('#btn-stay').addEventListener('click',()=>{clearTimeout(backTimer);note.textContent='';$('#btn-stay').hidden=true;});
$('#btn-retry').addEventListener('click',()=>begin(lastRaw));
$('#paste-form').addEventListener('submit',e=>{e.preventDefault();app.dataset.mode='paste';begin(inp.value);});
$('#paste').addEventListener('click',async()=>{
  try{const t=await navigator.clipboard.readText(); if(t){inp.value=t;}}catch(_){}
  inp.focus();
});

const q=new URLSearchParams(location.search);
if(q.get('src')==='paste') app.dataset.mode='paste';
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
