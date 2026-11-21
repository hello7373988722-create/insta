"""
NirnayDown
Share an Instagram reel to this installed web app and it downloads automatically.

Run on Render:
    build:  pip install -r requirements.txt
    start:  gunicorn app:app --workers 1 --threads 16 --timeout 120 --keep-alive 5

Optional environment variables:
    USE_SYNAX    set to 1 to try the Synax API after yt-dlp (its video links only work on grabfy.com)
    SYNAX_API    override the Synax API address
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
from http.cookiejar import DefaultCookiePolicy
from urllib.parse import unquote

import requests
from requests.adapters import HTTPAdapter
from flask import Flask, Response, jsonify, request

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("nirnaydown")

SYNAX_API = os.environ.get("SYNAX_API", "https://synax-family-insta.synaxbots.workers.dev/")
UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
)
# One shared connection pool: keeps connections to the video CDN open between requests,
# so each download skips the TCP/TLS handshake. Cookies are blocked so nothing leaks between users.
HTTP = requests.Session()
HTTP.mount("https://", HTTPAdapter(pool_connections=20, pool_maxsize=60))
HTTP.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))
CHUNK = 256 * 1024  # bigger chunks = less Python overhead per MB streamed
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
            hop = HTTP.get(
                share.group(0), headers={"User-Agent": UA}, timeout=10,
                allow_redirects=True, stream=True,
            )
            final = unquote(hop.url)
            hop.close()
        except requests.RequestException:
            raise ReelError("Could not open the share link. Try the normal reel link.", 502)
        found = POST_RE.search(final)
        if found:
            return _canon(found)
    found = POST_RE.search(raw)
    if found:
        return _canon(found)
    raise ReelError("This does not look like an Instagram reel link.", 400)


def via_synax(url):
    r = HTTP.get(SYNAX_API, params={"synaxurl": url}, headers={"User-Agent": UA}, timeout=25)
    r.raise_for_status()
    data = r.json()
    items = data.get("items") or []
    videos = [i for i in items if i.get("type") == "video" and i.get("download")]
    if not videos:
        if items:
            raise ReelError("This post has no video.", 422)
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
        "check_formats": False,
        "socket_timeout": 10,
        "retries": 1,
        "extractor_retries": 1,
        "cachedir": False,
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
        return len(recent) > S.get("rate_limit", RATE_LIMIT)


USE_SYNAX = os.environ.get("USE_SYNAX", "0") == "1"
NOT_FOUND = "Could not find the reel right now. It may be private, or try again in a little while."


def via_embed(code):
    """Second chance: Instagram's public embed page often carries the video link for public reels."""
    if not S.get("use_embed", True):
        raise RuntimeError("embed fallback is switched off")
    r = HTTP.get(
        f"https://www.instagram.com/p/{code}/embed/captioned/",
        headers={"User-Agent": UA}, timeout=10,
    )
    r.raise_for_status()
    found = re.search(r'"video_url":"([^"]+)"', r.text)
    if not found:
        raise RuntimeError("embed page has no video link")
    video = json.loads('"' + found.group(1) + '"')
    thumb = re.search(r'"display_url":"([^"]+)"', r.text)
    return {
        "video": video,
        "thumb": json.loads('"' + thumb.group(1) + '"') if thumb else None,
        "title": "",
        "headers": {"User-Agent": UA},
        "source": "embed",
    }


def _why(exc):
    """Short, readable reason for a failed lookup (also helps when reporting a bug)."""
    text = re.sub(r"\x1b\[[0-9;]*m", "", str(exc)).strip()
    low = text.lower()
    if "login" in low or "log in" in low or "cookies" in low or "empty media" in low:
        return "Instagram asks for login"
    if "429" in low or "rate" in low or "too many" in low:
        return "rate limited by Instagram"
    if "private" in low:
        return "private reel"
    if "no video" in low or "there is no video" in low:
        return "post has no video"
    if "timed out" in low or "timeout" in low:
        return "timeout"
    if "no module named" in low:
        return "yt-dlp not installed"
    return text.split("\n")[-1][:90] or "unknown"


def resolve(code, url):
    data = cache_get(code)
    if data:
        return data
    try:
        data = via_ytdlp(url)
    except Exception as exc:
        reason = _why(exc)
        log.warning("yt-dlp failed (%s): %s", code, exc)
        try:
            data = via_embed(code)
        except Exception as exc_embed:
            log.warning("embed failed (%s): %s", code, exc_embed)
            if USE_SYNAX:
                try:
                    data = via_synax(url)
                except ReelError:
                    raise
                except Exception as exc_synax:
                    log.warning("synax failed (%s): %s", code, exc_synax)
            if not data:
                raise ReelError(f"{NOT_FOUND} [{reason}]", 502)
    cache_put(code, data)
    return data


def _warm():
    """Load yt-dlp and its Instagram extractor at startup, so the first share does not pay for it."""
    try:
        import yt_dlp
        from yt_dlp.extractor.instagram import InstagramIE  # noqa: F401

        with yt_dlp.YoutubeDL({"quiet": True, "cachedir": False}):
            pass
    except Exception as exc:  # warming is optional, never fatal
        log.info("warm-up skipped: %s", exc)


def _keepalive(base):
    """Ping our own public URL so a free host does not put the app to sleep."""
    while True:
        time.sleep(540)
        try:
            requests.get(base.rstrip("/") + "/healthz", timeout=10)
        except Exception:
            pass


_warm()  # runs once at startup, before any request, so imports never race
_KEEP = os.environ.get("KEEPALIVE_URL") or os.environ.get("RENDER_EXTERNAL_URL")
if _KEEP:
    threading.Thread(target=_keepalive, args=(_KEEP,), daemon=True).start()


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
        raise ReelError("The video link is not valid.", 502)
    try:
        up = HTTP.get(url, headers=headers or {"User-Agent": UA}, stream=True, timeout=(10, 30))
        up.raise_for_status()
    except requests.RequestException as exc:
        log.warning("upstream fetch failed: %s", exc)
        raise ReelError("Could not download from the video server. Please try again.", 502)

    def chunks():
        try:
            for part in up.iter_content(CHUNK):
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
    t0 = time.time()
    if not is_admin() and maint_active() and S["maint_hard"]:
        track("?", False, 0, "", "maintenance")
        return fail(S["maint_msg"], 503)
    if rate_limited(client_ip()):
        return fail("Too many requests. Please wait a minute.", 429)
    code = "?"
    try:
        code, url = find_post(request.args.get("link", ""))
        data = resolve(code, url)
        data["_ms"] = int((time.time() - t0) * 1000)
    except ReelError as exc:
        found = re.search(r"\[(.+)\]$", str(exc))
        track(code, False, (time.time() - t0) * 1000, "", found.group(1) if found else str(exc)[:80])
        return fail(str(exc), exc.status)
    first_line = data["title"].split("\n")[0].strip()[:140]
    return jsonify(
        ok=True,
        code=code,
        title=first_line,
        thumb=f"/api/thumb/{code}" if data.get("thumb") else None,
        file=f"/api/file/{code}",
        filename=f"reel_{code}.mp4",
    )


@app.get("/api/file/<code>")
def api_file(code):
    token = request.args.get("t", "")
    if not re.fullmatch(r"[A-Za-z0-9]{6,32}", token):
        token = ""

    def flag(resp, value):
        # the page watches this cookie to know the download has started
        if token:
            resp.set_cookie(f"dl_{token}", value, max_age=60, path="/", samesite="Lax")
        return resp

    data = cache_get(code) if CODE_RE.fullmatch(code) else None
    if not data:
        return flag(fail("Link expired. Please send the reel again.", 410)[0], "err"), 410
    try:
        resp = stream(data["video"], data["headers"], "video/mp4", filename=f"reel_{code}.mp4")
        track(code, True, data.get("_ms", 0), data.get("source", "yt-dlp"))
        return flag(resp, "ok")
    except ReelError as exc:
        track(code, False, 0, "", "download failed")
        return flag(fail(str(exc), exc.status)[0], "err"), exc.status


def _flag(resp, token, value):
    # the page watches this cookie to know the download has started
    if token:
        resp.set_cookie(f"dl_{token}", value, max_age=60, path="/", samesite="Lax")
    return resp


@app.get("/api/dl")
def api_dl():
    """One hop: find the reel and start streaming it in the same request."""
    t0 = time.time()
    token = request.args.get("t", "")
    if not re.fullmatch(r"[A-Za-z0-9]{6,32}", token):
        token = ""
    code = "?"

    def bad(msg, status, why, **extra):
        track(code, False, (time.time() - t0) * 1000, "", why)
        return _flag(jsonify(ok=False, error=msg, **extra), token, "err"), status

    if not is_admin():
        if maint_active() and S["maint_hard"]:
            return bad(S["maint_msg"], 503, "maintenance", maint=True)
        if _iph(client_ip()) in S["blocked_ips"] or client_ip() in S["blocked_ips"]:
            return bad("Access denied.", 403, "blocked user")
    if rate_limited(client_ip()):
        return bad("Too many requests. Please wait a minute.", 429, "rate limit")
    try:
        code, url = find_post(request.args.get("link", ""))
        if code in S["blocked_codes"]:
            return bad("This reel is not available.", 403, "blocked reel")
        was_cached = cache_get(code) is not None
        data = resolve(code, url)
        try:
            resp = stream(data["video"], data["headers"], "video/mp4", filename=f"reel_{code}.mp4")
        except ReelError:
            if not was_cached:
                raise
            # a cached link went stale: drop it, resolve fresh and try once more
            with _lock:
                _cache.pop(code, None)
            data = resolve(code, url)
            resp = stream(data["video"], data["headers"], "video/mp4", filename=f"reel_{code}.mp4")
        track(code, True, (time.time() - t0) * 1000, data.get("source", "yt-dlp"))
        return _flag(resp, token, "ok")
    except ReelError as exc:
        found = re.search(r"\[(.+)\]$", str(exc))
        return bad(str(exc), exc.status, found.group(1) if found else str(exc)[:80])
    except Exception:
        log.exception("api_dl crashed")
        return bad("Something went wrong. Please try again.", 500, "server error")


@app.get("/api/thumb/<code>")
def api_thumb(code):
    data = cache_get(code) if CODE_RE.fullmatch(code) else None
    if not data or not data.get("thumb"):
        return Response(status=404)
    try:
        return stream(data["thumb"], {"User-Agent": UA}, "image/jpeg", keep_type=True)
    except ReelError:
        return Response(status=404)


APP_VERSION = "r11-2026-11-21"


@app.get("/healthz")
def healthz():
    """Uptime ping + a quick way to see which version is really running."""
    info = {"ok": True, "version": APP_VERSION}
    try:
        info["db"] = bool(DB_OK)
        info["log_rows"] = _q("SELECT COUNT(*) n FROM logs")[0]["n"] if DB_OK else 0
        info["log_calls"] = LOGSTAT["calls"]
        info["log_saved"] = LOGSTAT["saved"]
        info["log_error"] = LOGSTAT["error"]
    except Exception:
        pass
    resp = jsonify(info)
    resp.headers["Cache-Control"] = "no-store"
    return resp


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
                    "src": f"/icon-{size}.png?v=3",
                    "sizes": f"{size}x{size}",
                    "type": "image/png",
                    "purpose": purpose,
                }
            )
    body = {
        "name": "NirnayDown",
        "short_name": "NirnayDown",
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


SW_JS = """const V = 'nd-shell-v5';
const SHELL = ['/', '/share'];
self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(V).then((c) => Promise.all(SHELL.map((p) => c.add(new Request(p, { cache: 'reload' })).catch(() => {})))).then(() => self.skipWaiting())
  );
});
self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== V).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});
self.addEventListener('fetch', (e) => {
  const u = new URL(e.request.url);
  if (e.request.mode === 'navigate' && u.origin === location.origin && SHELL.includes(u.pathname)) {
    const key = u.pathname;
    e.respondWith((async () => {
      const c = await caches.open(V);
      const net = fetch(key, { cache: 'no-store' }).then((r) => { if (r.ok) c.put(key, r.clone()); return r; }).catch(() => null);
      const slow = new Promise((res) => setTimeout(() => res(null), 1200));
      const first = await Promise.race([net, slow]);
      if (first) return first;
      const hit = await c.match(key);
      if (hit) { e.waitUntil(net); return hit; }
      return (await net) || fetch(e.request);
    })());
  }
});
"""


@app.get("/.well-known/assetlinks.json")
def assetlinks():
    # Lets the Android app (TWA) open this site full-screen with no browser bar.
    # Set ANDROID_PACKAGE (e.g. com.nirnay.down) and ANDROID_SHA256 (comma-separated
    # signing-key fingerprints) as environment variables on your host.
    pkg = os.environ.get("ANDROID_PACKAGE", "").strip()
    fps = [f.strip() for f in os.environ.get("ANDROID_SHA256", "").split(",") if f.strip()]
    if not pkg or not fps:
        return Response("Not configured", status=404)
    body = [{
        "relation": ["delegate_permission/common.handle_all_urls"],
        "target": {"namespace": "android_app", "package_name": pkg, "sha256_cert_fingerprints": fps},
    }]
    return Response(json.dumps(body), mimetype="application/json")


@app.get("/sw.js")
def service_worker():
    resp = Response(SW_JS, mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


_icons = {}


def make_icon(size):
    """iOS-style icon: blue gradient tile, soft-shadow white battery, glossy yellow charge, blue arrow."""

    def mix(a, b, t):
        t = min(1.0, max(0.0, t))
        return tuple(a[k] + (b[k] - a[k]) * t for k in range(3))

    def rrect(x, y, cx, cy, hw, hh, r):
        qx, qy = abs(x - cx) - (hw - r), abs(y - cy) - (hh - r)
        return math.hypot(max(qx, 0), max(qy, 0)) + min(max(qx, qy), 0) - r

    def tri(x, y, a, b, c):
        d = -1e9
        for (x1, y1), (x2, y2) in ((a, b), (b, c), (c, a)):
            ex, ey = x2 - x1, y2 - y1
            d = max(d, ((x - x1) * ey - (y - y1) * ex) / math.hypot(ex, ey))
        return d

    top, bottom = (96, 184, 255), (46, 62, 234)
    body_hi, body_lo = (255, 255, 255), (222, 231, 255)
    sun_hi, sun_lo = (255, 232, 122), (255, 184, 0)
    arrow = (44, 60, 226)
    tip = ((0.395, 0.535), (0.605, 0.535), (0.5, 0.65))
    if (tip[1][0] - tip[0][0]) * (tip[2][1] - tip[0][1]) < 0:
        tip = tip[::-1]
    rows = bytearray()
    for py in range(size):
        rows.append(0)
        y = (py + 0.5) / size
        for px in range(size):
            x = (px + 0.5) / size
            col = mix(top, bottom, y * 0.85 + x * 0.15)
            gloss = 0.18 * max(0.0, 1 - y / 0.5)
            col = mix(col, (255, 255, 255), gloss)

            def paint(dist, color, alpha=1.0, soft=0.0):
                nonlocal col
                cover = min(1.0, max(0.0, 0.5 - dist * size)) if not soft else min(1.0, max(0.0, 0.5 - dist / soft))
                if cover > 0:
                    col = mix(col, color, cover * alpha)

            paint(rrect(x, y - 0.03, 0.5, 0.56, 0.2, 0.28, 0.085), (12, 24, 140), 0.32, soft=0.035)  # soft shadow
            paint(rrect(x, y, 0.5, 0.235, 0.075, 0.035, 0.03), (236, 243, 255))                       # cap
            paint(rrect(x, y, 0.5, 0.56, 0.2, 0.28, 0.085), mix(body_hi, body_lo, (y - 0.28) / 0.56))  # body
            inner = rrect(x, y, 0.5, 0.56, 0.165, 0.245, 0.06)
            paint(max(inner, 0.5 - y), mix(sun_hi, sun_lo, (y - 0.5) / 0.3))                          # charge
            paint(max(inner, abs(y - 0.5) - 0.004), (255, 255, 255), 0.5)                              # surface shine
            paint(rrect(x, y, 0.5, 0.465, 0.033, 0.07, 0.014), arrow)                                  # arrow stem
            paint(tri(x, y, *tip), arrow)                                                              # arrow head
            rows.extend(round(v) for v in col)

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


# draw the icons once in the background so the first install never waits for them
threading.Thread(target=lambda: [_icons.setdefault(n, make_icon(n)) for n in (192, 512)], daemon=True).start()


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
.brand{display:inline-flex;align-items:center;gap:11px;font-weight:800;font-size:19px;letter-spacing:-.02em;text-decoration:none}
.brand svg{width:32px;height:32px;display:block;filter:drop-shadow(0 2px 4px rgba(46,62,234,.28))}
.nbf{transform-box:fill-box;transform-origin:50% 100%;animation:nbfill 2.8s cubic-bezier(.45,0,.2,1) infinite}
@keyframes nbfill{0%{transform:scaleY(.18)}70%{transform:scaleY(1)}85%{transform:scaleY(1)}100%{transform:scaleY(.18)}}
@media (prefers-reduced-motion:reduce){.nbf{animation:none;transform:scaleY(.62)}}
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
  clip-path:inset(calc(100% - var(--p)) 0 0 0);transition:clip-path .12s linear;
}
.frame.has-thumb .base{background-image:var(--thumb);filter:blur(16px) saturate(.15) brightness(1.08);transform:scale(1.15)}
.frame.has-thumb .sharp{background-image:var(--thumb)}
.edge{position:absolute;left:0;right:0;bottom:var(--p);height:4px;background:var(--sun);opacity:0;transition:bottom .12s linear}
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
[data-state=done] .mark.ok,[data-state=error] .mark.bad{display:grid;animation:pop .15s cubic-bezier(.2,1.4,.4,1)}
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
#btn-back,#btn-retry,#btn-another,#paste-form{display:none}
[data-state=done][data-mode=share] #btn-back,[data-state=error] #btn-retry,[data-state=done][data-mode=paste] #btn-another{display:inline-flex}
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
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#ECEBFF">
<title>__TITLE__</title>
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/icon-192.png?v=3">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wdth,wght@12..96,75..100,400..800&display=swap" rel="stylesheet" media="print" onload="this.media='all'">
<noscript><link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wdth,wght@12..96,75..100,400..800&display=swap" rel="stylesheet"></noscript>
<style>__CSS__</style>
</head>
"""

BRAND = r"""<a class="brand" href="/" aria-label="NirnayDown home">
<svg viewBox="0 0 32 32" aria-hidden="true"><defs><linearGradient id="nbg" x1="0" y1="0" x2="0.15" y2="1"><stop offset="0" stop-color="#60B8FF"/><stop offset="1" stop-color="#2E3EEA"/></linearGradient><linearGradient id="nby" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#FFE87A"/><stop offset="1" stop-color="#FFB800"/></linearGradient><clipPath id="nbc"><rect x="9.4" y="9.2" width="13.2" height="17" rx="3.4"/></clipPath></defs><rect width="32" height="32" rx="7.4" fill="url(#nbg)"/><rect x="12.6" y="5.6" width="6.8" height="2.2" rx="1.1" fill="#ECF3FF"/><rect x="8.2" y="7.4" width="15.6" height="20.6" rx="4.6" fill="#fff"/><g clip-path="url(#nbc)"><rect class="nbf" x="9" y="9" width="14" height="17.4" fill="url(#nby)"/></g><path d="M16 13.6v6.6M13.2 17.7L16 20.5l2.8-2.8" fill="none" stroke="#2C3CE2" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/></svg>
NirnayDown</a>"""

PLAY = r"""<svg class="play" viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5.5v13l11-6.5z" fill="currentColor"/></svg>"""

HOME_HTML = (
    HEAD
    + r"""<body>
<div class="wrap">
  <header>__BRAND__</header>

  <section class="hero">
    <div>
      <h1>Share a reel, it downloads automatically.</h1>
      <p>Tap Share on a reel in Instagram and choose NirnayDown. The video saves straight to your phone.</p>
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
    <h2>Add to the share menu</h2>
    <p class="lead">Install it once, then share straight from any reel.</p>
    <button class="btn primary" id="install" hidden>Install NirnayDown</button>
    <p class="tick" id="installed" hidden>Already installed.</p>
    <ol class="track">
      <li><b>Install it</b><span>Choose "Install app" from the Chrome menu.</span></li>
      <li><b>Open a reel in Instagram</b><span>Tap the Share button.</span></li>
      <li><b>Choose NirnayDown</b><span>The download starts, then you go back to Instagram.</span></li>
    </ol>
  </section>

  <footer class="fine">Only download reels that are yours or that you have permission to save. This app is not affiliated with Instagram.</footer>
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
fetch('/api/banner',{cache:'no-store'}).then(r=>r.json()).then(b=>{
  if(!b.on) return;
  const key='nd_b_'+b.id;
  try{ if(b.dismiss && localStorage.getItem(key)) return; }catch(_){}
  const d=document.createElement('div'); d.setAttribute('role','status');
  d.style.cssText='margin:12px 0 0;padding:12px 14px;border:2.5px solid var(--ink);border-radius:16px;color:var(--ink);font-weight:600;line-height:1.35;display:flex;gap:10px;align-items:flex-start;background:'+(b.kind==='warn'?'#ffd34d':b.kind==='ok'?'#8ef0b0':'#d9d6ff');
  const t=document.createElement('div'); t.style.flex='1';
  if(b.title){const s=document.createElement('strong'); s.textContent=b.title; s.style.display='block'; t.appendChild(s);}
  t.appendChild(document.createTextNode(b.text||''));
  if(b.link && /^https?:\/\//i.test(b.link)){const a=document.createElement('a'); a.href=b.link; a.textContent=b.label||'Open'; a.style.cssText='display:block;margin-top:6px;color:var(--ink);text-decoration:underline'; t.appendChild(a);}
  d.appendChild(t);
  if(b.dismiss){const x=document.createElement('button'); x.textContent='\u00d7'; x.setAttribute('aria-label','Dismiss'); x.style.cssText='border:0;background:none;font-size:22px;line-height:1;color:var(--ink);cursor:pointer;padding:0 4px'; x.onclick=()=>{try{localStorage.setItem(key,'1')}catch(_){} d.remove();}; d.appendChild(x);}
  (document.querySelector('.wrap')||document.body).prepend(d);
}).catch(()=>{});
</script>
</body>
</html>
"""
)

SHARE_HTML = (
    HEAD
    + r"""<body>
<main class="wrap" id="app" data-state="idle" data-mode="share">
  <header>__BRAND__</header>
  <section class="stage">
    <div class="frame" id="frame">
      <div class="layer base"></div><div class="layer sharp"></div><div class="edge"></div>__PLAY__
      <div class="mark ok" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="#14123B" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg></div>
      <div class="mark bad" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg></div>
    </div>
    <p class="pct" id="pct" aria-hidden="true"></p>
    <h1 id="status" aria-live="polite">Reel link needed</h1>
    <p class="sub" id="sub">Tap Share on a reel in Instagram and choose NirnayDown. Or paste the link here.</p>
    <p class="note" id="note" aria-live="polite"></p>

    <div class="actions">
      <button class="btn primary" id="btn-back" type="button">Back to Instagram</button>
      <button class="btn primary" id="btn-retry" type="button">Try again</button>
      <button class="btn primary" id="btn-another" type="button">Download another</button>
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
let lastRaw='', lastPaste=false, backTimer=null, fakeTimer=null, current=0;

const setState=s=>{app.dataset.state=s;};
const progress=p=>{current=p;frame.style.setProperty('--p',p+'%');};
const say=(h,s)=>{statusEl.textContent=h;sub.textContent=s||'';};

function startFake(){
  stopFake();
  fakeTimer=setInterval(()=>{progress(current+(92-current)*0.08);},60);
}
function stopFake(){clearInterval(fakeTimer);fakeTimer=null;}

function goBack(){
  clearTimeout(backTimer);
  location.href=INTENT;
  setTimeout(()=>{try{window.close();}catch(_){}} ,250);
}

function startDownload(raw){
  // Hand the link straight to the server; the file starts streaming the moment the reel is found.
  return new Promise((resolve,reject)=>{
    const token=Math.random().toString(36).slice(2,12);
    const key='dl_'+token;
    const f=document.createElement('iframe');
    f.style.display='none';
    f.src='/api/dl?t='+token+'&link='+encodeURIComponent(raw);
    document.body.appendChild(f);
    const t0=Date.now();
    const timer=setInterval(()=>{
      const m=document.cookie.split('; ').find(c=>c.indexOf(key+'=')===0);
      if(m){
        clearInterval(timer);
        document.cookie=key+'=; Max-Age=0; path=/';
        if(m.slice(key.length+1)==='ok'){
          setTimeout(()=>f.remove(),60000);
          resolve();
        }else{
          setTimeout(()=>{
            let msg='Could not download the video. Please try again.';
            let mt=false;
            try{const j=JSON.parse(f.contentDocument.body.innerText); if(j&&j.error) msg=j.error; mt=!!(j&&j.maint);}catch(_){}
            f.remove(); const e=new Error(msg); e.maint=mt; reject(e);
          },150);
        }
      }else if(Date.now()-t0>45000){
        clearInterval(timer); f.remove();
        reject(new Error('Could not start the download. Please try again.'));
      }
    },20);
  });
}

async function run(raw,paste){
  lastRaw=raw; lastPaste=!!paste;
  app.dataset.mode=lastPaste?'paste':'share';
  clearTimeout(backTimer); stopFake();
  progress(0); pct.textContent=''; note.textContent='';
  setState('loading');
  say('Downloading','');
  startFake();
  try{
    await startDownload(raw);
    stopFake(); progress(100);
    setState('done');
    if(lastPaste){
      say('Download started','Your reel is saving. Paste another link whenever you like.');
    }else{
      say('Download started','Going back to Instagram…');
      goBack();
    }
  }catch(err){
    stopFake(); progress(0); pct.textContent='';
    setState('error');
    say(err.maint?'Under maintenance':'Download failed',err.message||'Something went wrong. Please try again.');
  }
}

function begin(raw,paste){
  raw=(raw||'').trim();
  if(!/instagram\.com|instagr\.am/i.test(raw)){
    setState('error');
    say('This does not look like a reel link','Paste an Instagram reel link.');
    return;
  }
  run(raw,paste);
}

$('#btn-back').addEventListener('click',goBack);
$('#btn-retry').addEventListener('click',()=>begin(lastRaw,lastPaste));
$('#btn-another').addEventListener('click',()=>{resetToIdle(); inp.focus();});
$('#paste-form').addEventListener('submit',e=>{e.preventDefault();begin(inp.value,true);});
$('#paste').addEventListener('click',async()=>{
  try{const t=await navigator.clipboard.readText(); if(t){inp.value=t;}}catch(_){}
  inp.focus();
});

const q=new URLSearchParams(location.search);
const shared=['url','text','title'].map(k=>q.get(k)||'').join(' ').trim();
// Only a link shared from Instagram goes back to Instagram. Links typed or pasted by the user (home page, or this page's own box) stay here.
const sameSite=(()=>{try{return new URL(document.referrer).origin===location.origin;}catch(_){return false;}})();
const fromPaste=q.get('src')==='paste'||sameSite;
if(location.search) history.replaceState(null,'',location.pathname); // forget the shared link so it can never run twice

const IDLE_H='Reel link needed', IDLE_S='Tap Share on a reel in Instagram and choose NirnayDown. Or paste the link here.';
function resetToIdle(){
  clearTimeout(backTimer); stopFake();
  frame.classList.remove('has-thumb'); frame.style.removeProperty('--thumb');
  progress(0); pct.textContent=''; note.textContent=''; inp.value=''; lastRaw=''; lastPaste=false; app.dataset.mode='share';
  say(IDLE_H,IDLE_S);
  setState('idle');
}
// coming back to the app after a finished download: show a fresh, empty page
document.addEventListener('visibilitychange',()=>{
  if(document.visibilityState==='visible' && app.dataset.state==='done') resetToIdle();
});
addEventListener('pageshow',e=>{ if(e.persisted && app.dataset.state==='done') resetToIdle(); });

if(shared){begin(shared,fromPaste);}
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
    return render(HOME_HTML, "NirnayDown")


@app.get("/share")
def share():
    return render(SHARE_HTML, "NirnayDown")


# ==========================================================================
# ADMIN PANEL  (/admin)  -  no setup needed: edit ADMIN_PASSWORD below on GitHub and restart.
# Data lives in a local SQLite file (nirnaydown.db). On hosts with a temporary disk
# (Render free) it resets whenever the server is redeployed or restarted.
# ==========================================================================
import csv
import functools
import hashlib
import hmac
import io
import queue
import sqlite3

from flask import redirect  # noqa: F401
from itsdangerous import URLSafeTimedSerializer

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "ChangeMe@123")  # <- change this line
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "nirnaydown.db"))
_signer = URLSafeTimedSerializer(hashlib.sha256(("nd|" + ADMIN_PASSWORD).encode()).hexdigest())
STARTED = time.time()

DEFAULTS = {
    "maint_on": False, "maint_hard": True, "maint_msg": "We are making things better. Back soon.",
    "maint_from": 0, "maint_until": 0,
    "banner_on": False, "banner_kind": "info", "banner_title": "", "banner_text": "", "banner_link": "",
    "banner_label": "Open", "banner_dismiss": True, "banner_from": 0, "banner_to": 0, "banner_id": 1,
    "rate_limit": 15, "blocked_ips": [], "blocked_codes": [], "use_embed": True, "ig_cookies": "",
}
S = dict(DEFAULTS)


def _q(sql, args=(), write=False):
    con = sqlite3.connect(DB_PATH, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        cur = con.execute(sql, args)
        rows = [] if write else cur.fetchall()
        if write:
            con.commit()
        return rows
    finally:
        con.close()


def _init_db():
    try:
        _q("CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, code TEXT, ok INTEGER,"
           " ms INTEGER, source TEXT, reason TEXT, dev TEXT, ip TEXT)", write=True)
        _q("CREATE INDEX IF NOT EXISTS i_ts ON logs(ts)", write=True)
        _q("CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT)", write=True)
        for row in _q("SELECT k, v FROM settings"):
            if row["k"] in DEFAULTS:
                try:
                    S[row["k"]] = json.loads(row["v"])
                except Exception:
                    pass
        return True
    except Exception:
        log.exception("admin database unavailable")
        return False


def apply_cookies(text):
    global COOKIE_FILE
    text = (text or "").strip()
    if not text:
        COOKIE_FILE = None
        return
    COOKIE_FILE = os.path.join(tempfile.gettempdir(), "ig_cookies.txt")
    with open(COOKIE_FILE, "w") as fh:
        fh.write(text + "\n")


DB_OK = _init_db()
if S.get("ig_cookies"):
    apply_cookies(S["ig_cookies"])

LOGSTAT = {"calls": 0, "saved": 0, "error": ""}


def _iph(ip):
    return hashlib.sha256((ip + "|nd").encode()).hexdigest()[:10]


def track(code, ok, ms, source="", reason=""):
    """Save one download record right away (a tiny local write). Never raises, so downloads are never affected."""
    LOGSTAT["calls"] += 1
    try:
        if not DB_OK:
            LOGSTAT["error"] = "database unavailable"
            return
        ua = request.headers.get("User-Agent", "")
        dev = "Android" if "Android" in ua else "iPhone" if "iPhone" in ua else "Desktop" if ua else "?"
        _q("INSERT INTO logs(ts,code,ok,ms,source,reason,dev,ip) VALUES(?,?,?,?,?,?,?,?)",
           (time.time(), str(code), 1 if ok else 0, int(ms), str(source or ""), str(reason or "")[:200], dev, _iph(client_ip())),
           write=True)
        LOGSTAT["saved"] += 1
        if LOGSTAT["saved"] % 200 == 0:
            _q("DELETE FROM logs WHERE id < (SELECT MAX(id) FROM logs) - 20000", write=True)
    except Exception as exc:
        LOGSTAT["error"] = f"{type(exc).__name__}: {exc}"[:200]
        log.exception("could not save log row")


def save_settings(values):
    for k, v in values.items():
        if k in DEFAULTS:
            S[k] = v
            try:
                _q("INSERT OR REPLACE INTO settings(k,v) VALUES(?,?)", (k, json.dumps(v)), write=True)
            except Exception:
                log.exception("settings write failed")


def maint_active():
    if not S["maint_on"]:
        return False
    now = time.time()
    return (not S["maint_from"] or now >= S["maint_from"]) and (not S["maint_until"] or now <= S["maint_until"])


def is_admin():
    token = request.cookies.get("nd_admin")
    if not token:
        return False
    try:
        return _signer.loads(token, max_age=86400 * 7) == "admin"
    except Exception:
        return False


def admin_only(fn):
    @functools.wraps(fn)
    def wrapper(*a, **k):
        if not is_admin():
            return jsonify(ok=False, error="auth"), 401
        return fn(*a, **k)
    return wrapper


_tries = {}


@app.post("/admin/login")
def admin_login():
    ip, now = client_ip(), time.time()
    tries = [x for x in _tries.get(ip, []) if now - x < 300]
    if len(tries) >= 5:
        return jsonify(ok=False, error="Too many attempts. Wait 5 minutes."), 429
    pw = str((request.get_json(silent=True) or {}).get("password", ""))
    if not hmac.compare_digest(pw.encode(), ADMIN_PASSWORD.encode()):
        tries.append(now)
        _tries[ip] = tries
        return jsonify(ok=False, error="Wrong password"), 401
    resp = jsonify(ok=True)
    resp.set_cookie("nd_admin", _signer.dumps("admin"), max_age=86400 * 7, httponly=True, samesite="Lax", secure=request.is_secure)
    return resp


@app.post("/admin/logout")
def admin_logout():
    resp = jsonify(ok=True)
    resp.delete_cookie("nd_admin")
    return resp


def _pct(values, p):
    if not values:
        return 0
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * p / 100))]


@app.get("/admin/api/stats")
@admin_only
def a_stats():
    now = time.time()
    out = {}
    for name, sec in (("d1", 86400), ("d7", 7 * 86400), ("d30", 30 * 86400)):
        rows = _q("SELECT ok, ms, ip FROM logs WHERE ts>?", (now - sec,))
        oks = [r["ms"] for r in rows if r["ok"]]
        out[name] = {
            "total": len(rows), "ok": len(oks), "fail": len(rows) - len(oks),
            "users": len({r["ip"] for r in rows}),
            "avg": int(sum(oks) / len(oks)) if oks else 0, "p50": _pct(oks, 50), "p95": _pct(oks, 95),
        }
    hourly = [[0, 0] for _ in range(24)]
    for r in _q("SELECT ts, ok FROM logs WHERE ts>?", (now - 86400,)):
        i = 23 - int((now - r["ts"]) // 3600)
        if 0 <= i < 24:
            hourly[i][0 if r["ok"] else 1] += 1
    wk = now - 7 * 86400
    try:
        import yt_dlp.version as yv
        ver = yv.__version__
    except Exception:
        ver = "not installed"
    out.update({
        "hourly": hourly,
        "top_fail": [[r["reason"] or "?", r["n"]] for r in _q("SELECT reason, COUNT(*) n FROM logs WHERE ok=0 AND ts>? GROUP BY reason ORDER BY n DESC LIMIT 8", (wk,))],
        "top_reels": [[r["code"], r["n"]] for r in _q("SELECT code, COUNT(*) n FROM logs WHERE ok=1 AND ts>? GROUP BY code ORDER BY n DESC LIMIT 8", (wk,))],
        "slowest": [[time.strftime("%d %b %H:%M", time.localtime(r["ts"])), r["code"], str(r["ms"]) + " ms", r["source"]] for r in _q("SELECT ts, code, ms, source FROM logs WHERE ok=1 AND ts>? ORDER BY ms DESC LIMIT 8", (wk,))],
        "sources": [[r["source"] or "?", r["n"]] for r in _q("SELECT source, COUNT(*) n FROM logs WHERE ok=1 AND ts>? GROUP BY source", (wk,))],
        "devices": [[r["dev"] or "?", r["n"]] for r in _q("SELECT dev, COUNT(*) n FROM logs WHERE ts>? GROUP BY dev ORDER BY n DESC", (wk,))],
        "health": {
            "version": APP_VERSION, "log_calls": LOGSTAT["calls"], "log_saved": LOGSTAT["saved"], "log_error": LOGSTAT["error"], "uptime_min": int((now - STARTED) / 60), "yt_dlp": ver, "cache_items": len(_cache), "db_ok": DB_OK,
            "rows": (_q("SELECT COUNT(*) n FROM logs")[0]["n"] if DB_OK else 0),
            "default_password": ADMIN_PASSWORD == "ChangeMe@123", "maintenance_now": maint_active(),
            "cookies_set": bool(COOKIE_FILE),
        },
    })
    return jsonify(out)


@app.get("/admin/api/logs")
@admin_only
def a_logs():
    where, args = [], []
    term = request.args.get("q", "").strip()
    if term:
        where.append("(code LIKE ? OR reason LIKE ? OR ip LIKE ?)")
        args += [f"%{term}%"] * 3
    if request.args.get("ok") in ("0", "1"):
        where.append("ok=?")
        args.append(int(request.args["ok"]))
    try:
        limit = max(1, min(500, int(request.args.get("limit", 100))))
    except ValueError:
        limit = 100
    sql = "SELECT * FROM logs" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
    rows = _q(sql, (*args, limit))
    return jsonify(rows=[[r["ts"], r["code"], r["ok"], r["ms"], r["source"], r["reason"], r["dev"], r["ip"]] for r in rows])


@app.get("/admin/export.csv")
def a_csv():
    if not is_admin():
        return "auth", 401
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["time_utc", "code", "ok", "ms", "source", "reason", "device", "user"])
    for r in _q("SELECT ts, code, ok, ms, source, reason, dev, ip FROM logs ORDER BY id DESC LIMIT 20000"):
        writer.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(r["ts"])), r["code"], r["ok"], r["ms"], r["source"], r["reason"], r["dev"], r["ip"]])
    return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=nirnaydown_logs.csv"})


@app.get("/admin/api/settings")
@admin_only
def a_settings():
    data = {k: v for k, v in S.items() if k != "ig_cookies"}
    data["cookies_set"] = bool(COOKIE_FILE)
    return jsonify(data)


@app.post("/admin/api/settings")
@admin_only
def a_settings_save():
    body = request.get_json(silent=True) or {}
    out = {}
    for key, default in DEFAULTS.items():
        if key not in body or key == "banner_id":
            continue
        val = body[key]
        if isinstance(default, bool):
            val = bool(val)
        elif isinstance(default, int):
            try:
                val = int(val)
            except (TypeError, ValueError):
                continue
        elif isinstance(default, list):
            val = [str(x).strip() for x in (val if isinstance(val, list) else []) if str(x).strip()][:500]
        else:
            val = str(val)[:4000]
        out[key] = val
    if "rate_limit" in out:
        out["rate_limit"] = max(1, min(300, out["rate_limit"]))
    if out.get("ig_cookies") == "__clear__":
        out["ig_cookies"] = ""
    if any(k in out and out[k] != S[k] for k in ("banner_title", "banner_text", "banner_link", "banner_label")):
        out["banner_id"] = S["banner_id"] + 1  # a changed banner shows again for people who dismissed the old one
    save_settings(out)
    if "ig_cookies" in out:
        apply_cookies(out["ig_cookies"])
    return jsonify(ok=True)


@app.post("/admin/api/cache/clear")
@admin_only
def a_cache_clear():
    with _lock:
        _cache.clear()
    return jsonify(ok=True)


@app.post("/admin/api/test")
@admin_only
def a_test():
    try:
        code, url = find_post((request.get_json(silent=True) or {}).get("link", ""))
    except ReelError as exc:
        return jsonify(ok=False, error=str(exc))
    results = {}
    for name, fn in (("yt-dlp", lambda: via_ytdlp(url)), ("embed", lambda: via_embed(code))):
        t = time.time()
        try:
            data = fn()
            results[name] = {"ok": bool(data.get("video")), "ms": int((time.time() - t) * 1000), "reason": ""}
        except Exception as exc:
            results[name] = {"ok": False, "ms": int((time.time() - t) * 1000), "reason": _why(exc)}
    return jsonify(ok=True, code=code, results=results)


@app.get("/api/banner")
def api_banner():
    now = time.time()
    if maint_active():
        out = {"on": True, "id": "m", "kind": "warn", "title": "Under maintenance", "text": S["maint_msg"], "link": "", "label": "", "dismiss": False}
    elif S["banner_on"] and (not S["banner_from"] or now >= S["banner_from"]) and (not S["banner_to"] or now <= S["banner_to"]):
        out = {"on": True, "id": S["banner_id"], "kind": S["banner_kind"], "title": S["banner_title"], "text": S["banner_text"],
               "link": S["banner_link"], "label": S["banner_label"], "dismiss": S["banner_dismiss"]}
    else:
        out = {"on": False}
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "no-store"
    return resp


ADMIN_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>Admin</title>
<style>
:root{--bg:#0a0d0b;--card:rgba(255,255,255,.06);--line:rgba(255,255,255,.12);--tx:#eef3ea;--mut:#98a393;--lime:#b6ff3c;--red:#ff5d6c;--amb:#ffd34d}
*{box-sizing:border-box}body{margin:0 auto;background:var(--bg);color:var(--tx);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding:14px;max-width:980px}
h1{font-size:20px;margin:6px 0 12px}h2{font-size:15px;margin:0 0 8px;color:var(--lime)}
.tabs{display:flex;gap:6px;overflow:auto;margin-bottom:12px}
button{background:var(--card);color:var(--tx);border:1px solid var(--line);border-radius:10px;padding:9px 13px;font:inherit;cursor:pointer;white-space:nowrap}
button.on,button.pri{background:var(--lime);color:#0a0d0b;border-color:var(--lime);font-weight:700}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px;margin-bottom:12px;backdrop-filter:blur(8px)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:8px}
.k{font-size:12px;color:var(--mut)}.v{font-size:19px;font-weight:700}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
.sx{overflow:auto}
input,textarea,select{width:100%;background:rgba(0,0,0,.35);color:var(--tx);border:1px solid var(--line);border-radius:10px;padding:9px;font:inherit}
input[type=checkbox]{width:auto}label{display:block;font-size:13px;color:var(--mut);margin:10px 0 4px}
.row{display:flex;gap:8px;flex-wrap:wrap}.row>*{flex:1;min-width:140px}
.ok{color:var(--lime)}.bad{color:var(--red)}.warn{color:var(--amb)}.chk{display:flex;gap:8px;align-items:center;color:var(--tx);font-size:15px}
</style></head><body><div id="app"></div>
<script>
const $=s=>document.querySelector(s);
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(p,o){const r=await fetch(p,Object.assign({headers:{'Content-Type':'application/json'}},o||{}));if(r.status===401){login();throw new Error('auth')}return r.json()}
const post=(p,b)=>api(p,{method:'POST',body:JSON.stringify(b||{})});
function login(msg){
  $('#app').innerHTML='<div class="card" style="max-width:340px;margin:12vh auto"><h1>Admin</h1><label>Password</label><input id="pw" type="password"><p class="bad" id="le">'+esc(msg||'')+'</p><button class="pri" id="lb" style="width:100%">Login</button></div>';
  const go=async()=>{const r=await fetch('/admin/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:$('#pw').value})});const j=await r.json();if(j.ok)boot();else $('#le').textContent=j.error||'Failed'};
  $('#lb').onclick=go;$('#pw').onkeydown=e=>{if(e.key==='Enter')go()};
}
let tab='ov';
function boot(){
  const T=[['ov','Overview'],['lg','Logs'],['st','Settings'],['tl','Test']];
  $('#app').innerHTML='<h1>NirnayDown · Admin</h1><div class="tabs">'+T.map(t=>'<button data-t="'+t[0]+'" class="'+(t[0]===tab?'on':'')+'">'+t[1]+'</button>').join('')+'<button id="out">Logout</button></div><div id="view"></div>';
  document.querySelectorAll('[data-t]').forEach(b=>b.onclick=()=>{tab=b.dataset.t;boot()});
  $('#out').onclick=async()=>{await fetch('/admin/logout',{method:'POST'});login()};
  ({ov:ov,lg:lg,st:st,tl:tl})[tab]().catch(e=>{if(e.message!=='auth')$('#view').innerHTML='<div class="card bad">Error: '+esc(e.message)+'</div>'});
}
const tbl=(h,rows)=>'<div class="sx"><table><tr>'+h.map(x=>'<th>'+x+'</th>').join('')+'</tr>'+(rows.map(r=>'<tr>'+r.map(c=>'<td>'+esc(c)+'</td>').join('')+'</tr>').join('')||'<tr><td>No data yet</td></tr>')+'</table></div>';
async function ov(){
  const d=await api('/admin/api/stats'),h=d.health;
  const pc=(a,b)=>b?Math.round(a*100/b)+'%':'-';
  const card=(t,x)=>'<div class="card"><h2>'+t+'</h2><div class="grid">'+[['Downloads',x.total],['Success',pc(x.ok,x.total)],['Failed',x.fail],['Users',x.users],['Avg',x.avg+' ms'],['Median',x.p50+' ms'],['p95',x.p95+' ms']].map(a=>'<div><div class="k">'+a[0]+'</div><div class="v">'+esc(a[1])+'</div></div>').join('')+'</div></div>';
  const mx=Math.max(1,...d.hourly.map(x=>x[0]+x[1]));
  const bars=d.hourly.map((x,i)=>{const o=x[0]*70/mx,b=x[1]*70/mx;return '<rect x="'+(i*14)+'" y="'+(72-o-b)+'" width="11" height="'+b+'" fill="#ff5d6c"/><rect x="'+(i*14)+'" y="'+(72-o)+'" width="11" height="'+o+'" fill="#b6ff3c"/>'}).join('');
  let warn='';
  if(h.default_password)warn+='<p class="bad">Change ADMIN_PASSWORD in app.py (still the default).</p>';
  if(!h.db_ok)warn+='<p class="bad">Database unavailable, stats are not being saved.</p>';
  $('#view').innerHTML=warn+card('Last 24 hours',d.d1)+card('Last 7 days',d.d7)+card('Last 30 days',d.d30)+
   '<div class="card"><h2>Hourly (last 24h, oldest to newest)</h2><svg viewBox="0 0 336 76" width="100%"><g>'+bars+'</g></svg><div class="k">green = success, red = failed</div></div>'+
   '<div class="card"><h2>Top fail reasons (7d)</h2>'+tbl(['Reason','Count'],d.top_fail)+'</div>'+
   '<div class="card"><h2>Slowest downloads (7d)</h2>'+tbl(['Time','Reel','Took','Source'],d.slowest)+'</div>'+
   '<div class="row"><div class="card"><h2>Top reels (7d)</h2>'+tbl(['Reel','Downloads'],d.top_reels)+'</div><div class="card"><h2>Sources</h2>'+tbl(['Source','Count'],d.sources)+'<h2 style="margin-top:12px">Devices</h2>'+tbl(['Device','Count'],d.devices)+'</div></div>'+
   '<div class="card"><h2>Health</h2>'+tbl(['Item','Value'],[['Version',h.version],['Uptime',h.uptime_min+' min'],['yt-dlp',h.yt_dlp],['Cache',h.cache_items+' items'],['Log rows',h.rows],['Saved / attempted',h.log_saved+' / '+h.log_calls],['Last log error',h.log_error||'none'],['Maintenance now',h.maintenance_now?'ON':'off'],['IG cookies',h.cookies_set?'set':'not set']])+'</div>';
}
async function lg(){
  $('#view').innerHTML='<div class="card"><div class="row"><input id="q" placeholder="Search reel code, reason or user"><select id="f"><option value="">All</option><option value="1">Success</option><option value="0">Failed</option></select><button id="go" class="pri">Search</button><a href="/admin/export.csv"><button>CSV</button></a></div></div><div class="card" id="lt"></div>';
  const load=async()=>{const d=await api('/admin/api/logs?q='+encodeURIComponent($('#q').value)+'&ok='+$('#f').value+'&limit=200');
    $('#lt').innerHTML=tbl(['Time','Reel','Status','ms','Source','Reason','Device','User'],d.rows.map(r=>[new Date(r[0]*1000).toLocaleString(),r[1],r[2]?'OK':'FAIL',r[3],r[4],r[5],r[6],r[7]]))};
  $('#go').onclick=load;load();
}
async function st(){
  const s=await api('/admin/api/settings');
  const dt=e=>e?new Date(e*1000-new Date().getTimezoneOffset()*60000).toISOString().slice(0,16):'';
  const ts=v=>v?Math.floor(new Date(v).getTime()/1000):0;
  const ck=(id,v,t)=>'<label class="chk"><input type="checkbox" id="'+id+'"'+(v?' checked':'')+'> '+t+'</label>';
  $('#view').innerHTML=
  '<div class="card"><h2>Maintenance</h2>'+ck('m_on',s.maint_on,'Maintenance on')+ck('m_hard',s.maint_hard,'Block downloads (off = warning on home only)')+
  '<label>Message</label><textarea id="m_msg" rows="2">'+esc(s.maint_msg)+'</textarea><div class="row"><div><label>Starts (optional)</label><input type="datetime-local" id="m_from" value="'+dt(s.maint_from)+'"></div><div><label>Ends (optional)</label><input type="datetime-local" id="m_to" value="'+dt(s.maint_until)+'"></div></div><p class="k">You (logged in as admin) can still download during maintenance.</p></div>'+
  '<div class="card"><h2>Announcement banner (home screen only, never in share flow)</h2>'+ck('b_on',s.banner_on,'Banner on')+ck('b_dis',s.banner_dismiss,'Users can dismiss it')+
  '<div class="row"><div><label>Style</label><select id="b_kind"><option value="info">Info</option><option value="warn">Warning</option><option value="ok">Good news</option></select></div><div><label>Title</label><input id="b_title" value="'+esc(s.banner_title)+'"></div></div>'+
  '<label>Text</label><textarea id="b_text" rows="2">'+esc(s.banner_text)+'</textarea><div class="row"><div><label>Button link (optional)</label><input id="b_link" value="'+esc(s.banner_link)+'"></div><div><label>Button label</label><input id="b_label" value="'+esc(s.banner_label)+'"></div></div>'+
  '<div class="row"><div><label>Show from (optional)</label><input type="datetime-local" id="b_from" value="'+dt(s.banner_from)+'"></div><div><label>Show until (optional)</label><input type="datetime-local" id="b_to" value="'+dt(s.banner_to)+'"></div></div></div>'+
  '<div class="card"><h2>Limits and fallbacks</h2><label>Lookups per minute per user</label><input type="number" id="rl" value="'+s.rate_limit+'">'+ck('emb',s.use_embed,'Use embed fallback when yt-dlp fails')+
  '<label>Blocked users (one per line, copy the user id from Logs)</label><textarea id="bi" rows="2">'+esc(s.blocked_ips.join('\n'))+'</textarea><label>Blocked reel codes (one per line)</label><textarea id="bc" rows="2">'+esc(s.blocked_codes.join('\n'))+'</textarea></div>'+
  '<div class="card"><h2>Instagram cookies</h2><p class="k">Status: '+(s.cookies_set?'<span class="ok">set</span>':'<span class="warn">not set</span>')+' (never shown again after saving)</p><textarea id="ck" rows="3" placeholder="Paste Netscape cookies text to replace"></textarea><div class="row"><button id="ckc">Remove cookies</button></div></div>'+
  '<div class="row"><button class="pri" id="sv">Save settings</button><button id="cc">Clear cache</button></div><p id="msg" class="ok"></p>';
  $('#b_kind').value=s.banner_kind;
  const lines=v=>v.split('\n').map(x=>x.trim()).filter(Boolean);
  $('#sv').onclick=async()=>{const b={maint_on:$('#m_on').checked,maint_hard:$('#m_hard').checked,maint_msg:$('#m_msg').value,maint_from:ts($('#m_from').value),maint_until:ts($('#m_to').value),
    banner_on:$('#b_on').checked,banner_dismiss:$('#b_dis').checked,banner_kind:$('#b_kind').value,banner_title:$('#b_title').value,banner_text:$('#b_text').value,banner_link:$('#b_link').value,banner_label:$('#b_label').value,banner_from:ts($('#b_from').value),banner_to:ts($('#b_to').value),
    rate_limit:$('#rl').value,use_embed:$('#emb').checked,blocked_ips:lines($('#bi').value),blocked_codes:lines($('#bc').value)};
    if($('#ck').value.trim())b.ig_cookies=$('#ck').value;
    const r=await post('/admin/api/settings',b);$('#msg').textContent=r.ok?'Saved':'Failed';if(r.ok)setTimeout(st,600)};
  $('#ckc').onclick=async()=>{await post('/admin/api/settings',{ig_cookies:'__clear__'});st()};
  $('#cc').onclick=async()=>{await post('/admin/api/cache/clear');$('#msg').textContent='Cache cleared'};
}
async function tl(){
  $('#view').innerHTML='<div class="card"><h2>Test a reel</h2><p class="k">Checks both methods and shows exactly why one fails.</p><input id="tu" placeholder="Paste reel link"><div class="row" style="margin-top:8px"><button class="pri" id="tr">Run test</button></div></div><div class="card" id="to" hidden></div>';
  $('#tr').onclick=async()=>{const o=$('#to');o.hidden=false;o.textContent='Testing...';const r=await post('/admin/api/test',{link:$('#tu').value});
    if(!r.ok){o.innerHTML='<span class="bad">'+esc(r.error)+'</span>';return}
    o.innerHTML='<h2>Reel '+esc(r.code)+'</h2>'+tbl(['Method','Result','Time','Reason'],Object.keys(r.results).map(k=>[k,r.results[k].ok?'OK':'FAIL',r.results[k].ms+' ms',r.results[k].reason]))};
}
fetch('/admin/api/settings').then(r=>r.status===401?login():boot());
</script></body></html>"""


@app.get("/admin")
def admin_page():
    resp = Response(ADMIN_HTML, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
