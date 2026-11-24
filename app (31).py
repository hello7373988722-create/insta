"""
NirnayDown
Share an Instagram reel, post, story or highlight to this installed web app and it downloads automatically.

Run on Render:
    build:  pip install -r requirements.txt
    start:  gunicorn app:app --workers 1 --threads 16 --timeout 120 --keep-alive 5

Optional environment variables:
    USE_SYNAX    set to 1 to try the Synax API after yt-dlp (its video links only work on grabfy.com)
    SYNAX_API    override the Synax API address
    IG_COOKIES   (optional) Netscape cookies text or a bare sessionid; loaded into cookie slot 1 on first start.
                 After that, manage the 5 cookie slots from /admin -> Settings.
"""
import hashlib
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

# --- Instagram cookie slots (managed from /admin) ---------------------------------
# Up to 5 accounts. Each lookup uses the next healthy slot (round robin). A slot that gets
# rate limited or asked for login rests for a while, and the next slot is tried straight away.
SLOTS = 5
COOLDOWN_RATE = 10 * 60   # seconds a rate-limited slot rests
COOLDOWN_LOGIN = 30 * 60  # seconds a slot with a dead/blocked session rests
_slot_state = [{"uses": 0, "fails": 0, "until": 0.0, "err": "", "raw": ""} for _ in range(SLOTS)]
_slot_files = [None] * SLOTS
_slot_lock = threading.Lock()
_rr = [0]


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
STORY_RE = re.compile(
    r"https?://(?:www\.|m\.)?instagram\.com/stories/(highlights/)?([A-Za-z0-9_.]+)/?(\d+)?",
    re.I,
)
SHARE_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:share/(?:(?:reel|p|story)/)?|s/)[A-Za-z0-9_=%-]+(?:\?[^\s]*)?", re.I
)
CODE_RE = re.compile(r"[A-Za-z0-9_-]{5,40}")
IMG_EXTS = {"jpg", "jpeg", "png", "webp"}
MAX_ITEMS = 60  # a highlight can hold ~100 stories; the picker shows the first 60
KIND_LABEL = {"reel": "reel", "post": "post", "story": "story", "highlight": "highlight"}


def _short_key(prefix, canon):
    return prefix + hashlib.sha1(canon.encode()).hexdigest()[:14]


def _match(text):
    """Find a reel / post / story / highlight in a URL or in shared text."""
    st = STORY_RE.search(text)
    if st:
        is_hl, name, sid = bool(st.group(1)), st.group(2), st.group(3)
        if is_hl:
            canon = f"https://www.instagram.com/stories/highlights/{name}/"
            return {"kind": "highlight", "code": _short_key("h", canon), "url": canon, "want": None}
        canon = f"https://www.instagram.com/stories/{name}/{sid}/" if sid else f"https://www.instagram.com/stories/{name}/"
        return {"kind": "story", "code": _short_key("s", canon), "url": canon, "want": sid}
    found = POST_RE.search(text)
    if found:
        kind = "reel" if found.group(1).lower().startswith("reel") else "post"
        code = found.group(2)
        path = "reel" if kind == "reel" else found.group(1).lower()
        return {"kind": kind, "code": code, "url": f"https://www.instagram.com/{path}/{code}/", "want": None}
    return None


def parse_link(raw):
    """Work out what Instagram link this is: {kind, code, url, want}."""
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
            raise ReelError("Could not open the share link. Try the normal link from Instagram.", 502)
        found = _match(final)
        if found:
            return found
    found = _match(raw)
    if found:
        return found
    raise ReelError("This does not look like an Instagram reel, post, story or highlight link.", 400)


def find_post(raw):
    """Pull a (shortcode, clean url) out of whatever text Instagram shared."""
    link = parse_link(raw)
    return link["code"], link["url"]


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


def _pick_slots():
    """Healthy slots in round-robin order (rested ones last), as a list of slot numbers."""
    now = time.time()
    with _slot_lock:
        filled = [i for i in range(SLOTS) if _slot_files[i]]
        if not filled:
            return []
        start = _rr[0] % len(filled)
        _rr[0] += 1
        order = filled[start:] + filled[:start]
        ready = [i for i in order if _slot_state[i]["until"] <= now]
        resting = sorted((i for i in order if i not in ready), key=lambda i: _slot_state[i]["until"])
        return ready + resting


def _clean(exc):
    return re.sub(r"\x1b\[[0-9;]*m", "", str(exc)).strip().replace("\n", " ")[:300]


def _slot_result(i, ok, reason="", raw=""):
    with _slot_lock:
        st = _slot_state[i]
        if ok:
            st["uses"] += 1
            st["err"] = ""
            st["raw"] = ""
            st["until"] = 0.0
            return
        st["fails"] += 1
        st["err"] = reason
        st["raw"] = raw
        if reason == "rate limited by Instagram":
            st["until"] = time.time() + COOLDOWN_RATE
        elif reason == "Instagram asks for login":
            st["until"] = time.time() + COOLDOWN_LOGIN


def _has_slots():
    with _slot_lock:
        return any(_slot_files)


def _run_with_slots(call):
    """Run call(cookie_file) with the next healthy cookie slot. A slot that is rate limited or asked for
    login rests, and the next one is tried (at most 3 per lookup). Returns (result, slot index or None)."""
    slots = _pick_slots()
    if not slots:
        return call(None), None
    last = None
    for i in slots[:3]:
        try:
            res = call(_slot_files[i])
        except Exception as exc:
            reason = _why(exc)
            _slot_result(i, False, reason, _clean(exc))
            last = exc
            if reason in ("rate limited by Instagram", "Instagram asks for login"):
                continue  # this account is the problem, try the next one
            raise
        _slot_result(i, True)
        return res, i
    raise last


def via_ytdlp(url, kind="reel", want=None):
    data, i = _run_with_slots(lambda cf: _ytdlp_once(url, cf, kind, want))
    data["source"] = "ytdlp (no cookie)" if i is None else f"ytdlp (cookie {i + 1})"
    if i is not None:
        data["slot"] = i + 1
    return data


# --- Instagram web API (what the website itself uses). Handles photos, carousels, stories and highlights
# which yt-dlp does not (it only understands videos). Needs a logged-in cookie slot.
WEB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/131.0.0.0 Safari/537.36")
SC_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def _sc_to_id(code):
    n = 0
    for ch in code[:28]:
        n = n * 64 + SC_ALPHABET.index(ch)
    return n


def _cookie_header(path):
    pairs = {}
    with open(path) as fh:
        for line in fh.read().splitlines():
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            elif line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                pairs[parts[5]] = parts[6]
    return "; ".join(f"{k}={v}" for k, v in pairs.items()), pairs.get("csrftoken", "")


def _api_get(path, cookie_file):
    if not cookie_file:
        raise RuntimeError("login required (no cookie slot)")
    cookie, csrf = _cookie_header(cookie_file)
    headers = {
        "User-Agent": WEB_UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9",
        "X-IG-App-ID": "936619743392459", "X-ASBD-ID": "129477", "X-IG-WWW-Claim": "0",
        "X-Requested-With": "XMLHttpRequest", "X-CSRFToken": csrf,
        "Referer": "https://www.instagram.com/", "Origin": "https://www.instagram.com", "Cookie": cookie,
    }
    r = requests.get("https://www.instagram.com/api/v1" + path, headers=headers, timeout=12, allow_redirects=False)
    if 300 <= r.status_code < 400:
        raise RuntimeError(f"HTTP {r.status_code}: redirected to login")
    try:
        j = r.json()
    except ValueError:
        j = None
    if r.status_code >= 400 or (isinstance(j, dict) and j.get("status") == "fail"):
        msg = (j or {}).get("message") if isinstance(j, dict) else ""
        raise RuntimeError(f"HTTP {r.status_code}: {msg or r.reason or 'error'}")
    if not isinstance(j, dict):
        raise RuntimeError(f"HTTP {r.status_code}: not JSON (login page?)")
    return j


def _api_items(m):
    """Flatten an Instagram media object (photo, video or carousel) into our item list."""
    out = []
    if m.get("carousel_media"):
        for child in m["carousel_media"]:
            out.extend(_api_items(child))
        return out
    cands = ((m.get("image_versions2") or {}).get("candidates")) or []
    thumb = None
    if cands:
        thumb = min(cands, key=lambda c: abs((c.get("width") or 0) - 320)).get("url")
    pid = str(m.get("pk") or str(m.get("id", "")).split("_")[0])
    if m.get("video_versions"):
        best = max(m["video_versions"], key=lambda v: (v.get("width") or 0) * (v.get("height") or 0))
        out.append({"id": pid, "type": "video", "url": best["url"], "thumb": thumb, "ext": "mp4", "headers": {"User-Agent": WEB_UA}})
    elif cands:
        best = max(cands, key=lambda c: (c.get("width") or 0) * (c.get("height") or 0))
        out.append({"id": pid, "type": "image", "url": best["url"], "thumb": thumb or best["url"], "ext": "jpg", "headers": {"User-Agent": WEB_UA}})
    return out


def _api_fetch(code, url, kind, want, cookie_file):
    caption = ""
    if kind in ("reel", "post"):
        j = _api_get(f"/media/{_sc_to_id(code)}/info/", cookie_file)
        media = (j.get("items") or [None])[0]
        if not media:
            raise RuntimeError("post not available")
        items = _api_items(media)
        caption = ((media.get("caption") or {}).get("text")) or ""
    else:
        st = STORY_RE.search(url)
        if kind == "highlight":
            reel_id = "highlight:" + st.group(2)
        else:
            who = st.group(2)
            prof = _api_get(f"/users/web_profile_info/?username={who}", cookie_file)
            user = ((prof.get("data") or {}).get("user")) or {}
            if not user.get("id"):
                raise RuntimeError("profile not available")
            reel_id = str(user["id"])
        j = _api_get(f"/feed/reels_media/?reel_ids={reel_id}", cookie_file)
        reel = (j.get("reels") or {}).get(reel_id) or next(iter(j.get("reels_media") or []), None)
        raw = (reel or {}).get("items") or []
        if not raw:
            raise RuntimeError("no stories available (expired or none)")
        if kind == "story" and want:
            only = [m for m in raw if str(m.get("pk") or str(m.get("id", "")).split("_")[0]) == want]
            raw = only or raw
        items = []
        for m in raw:
            items.extend(_api_items(m))
    if kind == "reel":
        items = items[:1]
    items = items[:MAX_ITEMS]
    if not items:
        raise RuntimeError("no media found")
    return {"video": items[0]["url"], "thumb": items[0]["thumb"], "title": caption.strip(), "headers": {"User-Agent": WEB_UA},
            "items": items, "source": "api"}


def via_api(code, url, kind="reel", want=None):
    data, i = _run_with_slots(lambda cf: _api_fetch(code, url, kind, want, cf))
    data["source"] = "api (no cookie)" if i is None else f"api (cookie {i + 1})"
    if i is not None:
        data["slot"] = i + 1
    return data


def _entry_items(info, fallback_headers):
    """Turn a yt-dlp result (single item or playlist) into a flat list of photo/video items."""
    entries = info.get("entries")
    found = []
    for e in ([x for x in entries if x] if entries else [info]):
        if e.get("entries"):
            found.extend(_entry_items(e, fallback_headers))
            continue
        url = e.get("url")
        if not url and e.get("requested_formats"):
            url = e["requested_formats"][0].get("url")
        if not url and e.get("formats"):
            url = e["formats"][-1].get("url")
        if not url:
            continue
        ext = str(e.get("ext") or "").lower()
        is_img = ext in IMG_EXTS or (e.get("vcodec") == "none" and e.get("acodec") in (None, "none"))
        found.append({
            "id": str(e.get("id") or ""),
            "type": "image" if is_img else "video",
            "url": url,
            "thumb": e.get("thumbnail") or (url if is_img else None),
            "ext": (ext if ext in IMG_EXTS else "jpg") if is_img else "mp4",
            "headers": e.get("http_headers") or fallback_headers,
        })
    return found


def _ytdlp_once(url, cookie_file, kind="reel", want=None):
    import yt_dlp  # imported late so the app still starts without it

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": kind not in ("story", "highlight"),
        "format": "best[ext=mp4]/best",
        "check_formats": False,
        "socket_timeout": 10,
        "retries": 1,
        "extractor_retries": 1,
        "cachedir": False,
    }
    if cookie_file:
        opts["cookiefile"] = cookie_file
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    items = _entry_items(info, info.get("http_headers") or {})
    if kind == "story" and want:  # a link to one specific story: keep just that one when we can tell which it is
        only = [x for x in items if x["id"] == want]
        items = only or items
    if kind == "reel":
        items = items[:1]
    items = items[:MAX_ITEMS]
    if not items:
        raise RuntimeError("no direct url")
    return {
        "video": items[0]["url"],
        "thumb": info.get("thumbnail") or items[0]["thumb"],
        "title": (info.get("description") or info.get("title") or "").strip(),
        "headers": items[0]["headers"],
        "items": items,
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
    thumb = re.search(r'"display_url":"([^"]+)"', r.text)
    thumb_url = json.loads('"' + thumb.group(1) + '"') if thumb else None
    if found:
        video = json.loads('"' + found.group(1) + '"')
        item = {"id": code, "type": "video", "url": video, "thumb": thumb_url, "ext": "mp4", "headers": {"User-Agent": UA}}
    elif thumb_url:  # a photo post: the embed page only gives the cover photo
        item = {"id": code, "type": "image", "url": thumb_url, "thumb": thumb_url, "ext": "jpg", "headers": {"User-Agent": UA}}
    else:
        raise RuntimeError("embed page has no video link")
    return {
        "video": item["url"],
        "thumb": thumb_url,
        "title": "",
        "headers": {"User-Agent": UA},
        "items": [item],
        "source": "embed",
    }


def _why(exc):
    """Short, readable reason for a failed lookup (also helps when reporting a bug)."""
    text = re.sub(r"\x1b\[[0-9;]*m", "", str(exc)).strip()
    low = text.lower()
    if "login" in low or "log in" in low or "cookies" in low or "empty media" in low or "checkpoint" in low or "challenge" in low:
        return "Instagram asks for login"
    if "429" in low or "rate" in low or "too many" in low or "wait a few minutes" in low:
        return "rate limited by Instagram"
    if "private" in low:
        return "private reel"
    if "no video" in low or "there is no video" in low:
        return "post has no video"
    if "not available" in low or "unavailable" in low or "not found" in low or "does not exist" in low:
        return "not available"
    if "timed out" in low or "timeout" in low:
        return "timeout"
    if "no module named" in low:
        return "yt-dlp not installed"
    return text.split("\n")[-1][:90] or "unknown"


NOT_FOUND_BY_KIND = {
    "reel": NOT_FOUND,
    "post": "Could not find this post right now. It may be private, or try again in a little while.",
    "story": "Could not get this story. It may have expired, or the account is private.",
    "highlight": "Could not get this highlight. It may have been removed, or the account is private.",
}
NEEDS_LOGIN = "Stories and highlights are not available right now. Please try again later."


def _finish(data, kind):
    """Make sure every result has a clean items list, and remember what kind of link it came from."""
    if not data.get("items"):
        data["items"] = [{"id": "", "type": "video", "url": data.get("video"), "thumb": data.get("thumb"),
                          "ext": "mp4", "headers": data.get("headers") or {}}]
    for item in data["items"]:
        item.setdefault("headers", data.get("headers") or {})
    data["kind"] = kind
    if kind != "reel":
        data["source"] = f"{data.get('source', 'ytdlp')} \u00b7 {kind}"
    return data


def resolve(code, url, kind="reel", want=None):
    data = cache_get(code)
    if data:
        return data
    have_cookie = _has_slots()
    if kind in ("story", "highlight") and not have_cookie:
        raise ReelError(f"{NEEDS_LOGIN} [no cookie slot filled]", 503)
    api = ("api", lambda: via_api(code, url, kind, want))
    ytd = ("yt-dlp", lambda: via_ytdlp(url, kind, want))
    emb = ("embed", lambda: via_embed(code))
    if kind in ("story", "highlight"):
        order = [api, ytd]
    elif kind == "post":  # photos and carousels need the API; yt-dlp only sees videos
        order = ([api] if have_cookie else []) + [ytd, emb]
    else:
        order = [ytd, emb] + ([api] if have_cookie else [])
    errs = []
    for label, fn in order:
        try:
            data = fn()
            break
        except Exception as exc:
            errs.append((label, _why(exc)))
            log.warning("%s failed (%s %s): %s", label, kind, code, exc)
    if not data and USE_SYNAX and kind == "reel":
        try:
            data = via_synax(url)
        except ReelError:
            raise
        except Exception as exc_synax:
            log.warning("synax failed (%s): %s", code, exc_synax)
    if not data:
        seen = []
        for label, why in errs:
            if why not in [w for _, w in seen]:
                seen.append((label, why))
        detail = "; ".join(f"{l}: {w}" for l, w in seen) if len(seen) > 1 else (seen[0][1] if seen else "unknown")
        raise ReelError(f"{NOT_FOUND_BY_KIND.get(kind, NOT_FOUND)} [{detail}]", 502)
    _finish(data, kind)
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


KIND_FLAG = {"post": "allow_post", "story": "allow_story", "highlight": "allow_highlight"}
KIND_OFF = {
    "post": "Post downloads are switched off right now.",
    "story": "Story downloads are switched off right now.",
    "highlight": "Highlight downloads are switched off right now.",
}


def _kind_off(kind):
    key = KIND_FLAG.get(kind)
    return bool(key) and not S.get(key, True)


def _fname(data, code, i):
    kind = data.get("kind", "reel")
    item = data["items"][i]
    base = f"{kind}_{code}" if kind in ("reel", "post") else f"{kind}_{code[1:9]}"
    if len(data["items"]) > 1:
        base += f"_{i + 1}"
    return f"{base}.{item.get('ext') or ('jpg' if item['type'] == 'image' else 'mp4')}"


def _mime(item):
    if item["type"] == "video":
        return "video/mp4"
    return {"png": "image/png", "webp": "image/webp"}.get(item.get("ext"), "image/jpeg")


def _items_view(code, data):
    return [
        {"i": n, "type": it["type"], "thumb": f"/api/thumb/{code}/{n}" if it.get("thumb") else None}
        for n, it in enumerate(data["items"])
    ]


def _send(code, data, i):
    item = data["items"][i]
    return stream(item["url"], item.get("headers") or data.get("headers"), _mime(item), filename=_fname(data, code, i))


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
        link = parse_link(request.args.get("link", ""))
        code = link["code"]
        if _kind_off(link["kind"]):
            return fail(KIND_OFF[link["kind"]], 403)
        data = resolve(code, link["url"], link["kind"], link["want"])
        data["_ms"] = int((time.time() - t0) * 1000)
    except ReelError as exc:
        found = re.search(r"\[(.+)\]$", str(exc))
        track(code, False, (time.time() - t0) * 1000, "", found.group(1) if found else str(exc)[:80])
        return fail(str(exc), exc.status)
    first_line = data["title"].split("\n")[0].strip()[:140]
    return jsonify(
        ok=True,
        code=code,
        kind=data["kind"],
        count=len(data["items"]),
        items=_items_view(code, data),
        title=first_line,
        thumb=f"/api/thumb/{code}/0" if data["items"][0].get("thumb") else None,
        file=f"/api/file/{code}",
        filename=_fname(data, code, 0),
    )


@app.get("/api/file/<code>")
@app.get("/api/file/<code>/<int:i>")
def api_file(code, i=0):
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
        return flag(fail("Link expired. Please send the link again.", 410)[0], "err"), 410
    if not 0 <= i < len(data["items"]):
        return flag(fail("That item does not exist.", 404)[0], "err"), 404
    try:
        resp = _send(code, data, i)
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
    """One hop: find the link and, when it holds a single photo or video, start streaming it in the same request.
    When it holds several (carousel, story set, highlight) answer with the list so the page can show a picker."""
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
        link = parse_link(request.args.get("link", ""))
        code, url, kind, want = link["code"], link["url"], link["kind"], link["want"]
        if code in S["blocked_codes"]:
            return bad("This link is not available.", 403, "blocked reel")
        if _kind_off(kind):
            return bad(KIND_OFF[kind], 403, f"{kind} switched off")
        was_cached = cache_get(code) is not None
        data = resolve(code, url, kind, want)
        if len(data["items"]) > 1:
            return _flag(jsonify(ok=True, multi=True, code=code, kind=kind, count=len(data["items"]),
                                 items=_items_view(code, data)), token, "multi")
        try:
            resp = _send(code, data, 0)
        except ReelError:
            if not was_cached:
                raise
            # a cached link went stale: drop it, resolve fresh and try once more
            with _lock:
                _cache.pop(code, None)
            data = resolve(code, url, kind, want)
            resp = _send(code, data, 0)
        track(code, True, (time.time() - t0) * 1000, data.get("source", "yt-dlp"))
        return _flag(resp, token, "ok")
    except ReelError as exc:
        found = re.search(r"\[(.+)\]$", str(exc))
        return bad(str(exc), exc.status, found.group(1) if found else str(exc)[:80])
    except Exception:
        log.exception("api_dl crashed")
        return bad("Something went wrong. Please try again.", 500, "server error")


@app.get("/api/thumb/<code>")
@app.get("/api/thumb/<code>/<int:i>")
def api_thumb(code, i=0):
    data = cache_get(code) if CODE_RE.fullmatch(code) else None
    if not data or not 0 <= i < len(data["items"]) or not data["items"][i].get("thumb"):
        return Response(status=404)
    try:
        return stream(data["items"][i]["thumb"], {"User-Agent": UA}, "image/jpeg", keep_type=True)
    except ReelError:
        return Response(status=404)


APP_VERSION = "r13-2026-11-22"


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
        "related_applications": [
            {"platform": "webapp", "url": f"{request.headers.get('X-Forwarded-Proto', 'https')}://{request.host}/manifest.webmanifest"}
        ],
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

/* v12: battery, locked share screen, popup, maintenance, faq, installed badge */
.brand{font-size:26px;gap:12px}
.brand svg{width:44px;height:44px}
@keyframes rise{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
@keyframes fadein{from{opacity:0}to{opacity:1}}
@keyframes fadeout{to{opacity:0}}
.batwrap{display:flex;justify-content:center}
.bat{display:block;width:auto;height:var(--bh,164px);overflow:visible;filter:drop-shadow(0 12px 14px rgba(46,62,234,.22))}
.bfill{transform-box:fill-box;transform-origin:50% 100%;transform:scaleY(var(--k,.6));transition:transform .14s linear}
.barrow,.bcheck,.bx{fill:none;stroke-linecap:round;stroke-linejoin:round;stroke-width:9}
.barrow{stroke:#2C3CE2}.bcheck{stroke:#2C3CE2;display:none}.bx{stroke:#E8445F;display:none}
.hero .bfill,[data-state=idle] .bfill{animation:bcharge 3.2s ease-in-out infinite}
@keyframes bcharge{0%,100%{transform:scaleY(.42)}50%{transform:scaleY(.7)}}
[data-state=loading] .barrow{animation:bbob 1s ease-in-out infinite}
@keyframes bbob{50%{transform:translateY(7px)}}
[data-state=done] .barrow,[data-state=error] .barrow{display:none}
[data-state=done] .bcheck,[data-state=error] .bx{display:block;transform-box:fill-box;transform-origin:center;animation:pop .22s cubic-bezier(.2,1.4,.4,1)}
[data-state=error] .bfill{fill:url(#brd);animation:none;transform:scaleY(.24)}
.hero .batwrap{flex:none;margin-top:6px;--bh:130px}
.hero>div:first-child{min-width:0}
.hero h1{overflow-wrap:break-word;font-size:clamp(30px,8.4vw,52px)}
html,body{overflow-x:hidden}
@media (max-width:380px){.hero .batwrap{--bh:112px}.hero{gap:14px}}
.sw{animation:rise .28s ease both}
.actions .btn,#paste-form{animation:rise .28s ease both}

/* share screens: one locked screen, nothing scrolls */
body[data-page=share]{overflow:hidden;height:100vh;height:100dvh;overscroll-behavior:none}
body[data-page=share] .wrap{height:100vh;height:100dvh;display:flex;flex-direction:column;overflow:hidden;padding-top:max(14px,env(safe-area-inset-top));padding-bottom:max(14px,env(safe-area-inset-bottom))}
body[data-page=share] header{flex:none}
body[data-page=share] .stage{flex:1;min-height:0;padding:0;justify-content:center}
body[data-page=share] .batwrap{--bh:clamp(110px,27dvh,230px)}
body[data-page=share] .pct,body[data-page=share] .note{display:none}
body[data-page=share] .stage h1{font-size:clamp(26px,7.4vw,34px);margin-top:18px}
body[data-page=share] .sub{margin-top:8px}
body[data-page=share] .actions{margin-top:16px}
body[data-page=share] #paste-form{margin-top:16px}
body[data-page=share] .go{margin-top:10px}
@media (max-height:740px){
  body[data-page=share] .batwrap{--bh:clamp(96px,23dvh,180px)}
  body[data-page=share] .btn{min-height:46px;font-size:16px}
  body[data-page=share] .field .btn,body[data-page=share] .field input{min-height:40px}
  body[data-page=share] .stage h1{font-size:clamp(24px,6.8vw,30px);margin-top:12px}
  body[data-page=share] .sub{-webkit-line-clamp:2;font-size:15px}
  body[data-page=share] .actions,body[data-page=share] #paste-form{margin-top:10px}
  body[data-page=share] .go{margin-top:8px}
}
@media (max-height:600px){
  body[data-page=share] .batwrap{--bh:19dvh}
  body[data-page=share] .sub{display:none}
}

/* installed badge + faq */
.badge{display:none;align-items:center;gap:12px;margin-top:6px;padding:14px 16px;border:2.5px solid var(--ink);border-radius:20px;background:#8EF0B0;box-shadow:0 4px 0 var(--ink);animation:bpulse 1.6s ease-out 2}
.badge svg{width:30px;height:30px;flex:none}
.badge strong{display:block;font-size:19px;letter-spacing:-.01em}
.badge span{display:block;font-size:14.5px;font-weight:600;color:var(--ink);opacity:.8}
.inst .badge{display:flex}.inst .how{display:none}
@keyframes bpulse{0%{box-shadow:0 4px 0 var(--ink),0 0 0 0 rgba(90,220,140,.85)}100%{box-shadow:0 4px 0 var(--ink),0 0 0 18px rgba(90,220,140,0)}}
.faq{margin-top:34px;padding-top:30px;border-top:2px solid var(--ink)}
.faq h2{font-size:26px;margin-bottom:6px}
.faq details{margin-top:10px;background:#fff;border:2.5px solid var(--ink);border-radius:18px;overflow:hidden}
.faq details[open]{background:#FFF7D6}
.faq summary{list-style:none;cursor:pointer;padding:15px 50px 15px 16px;font-weight:700;position:relative}
.faq summary::-webkit-details-marker{display:none}
.faq summary::after{content:"";position:absolute;right:20px;top:50%;width:9px;height:9px;border-right:3px solid var(--ink);border-bottom:3px solid var(--ink);transform:translateY(-70%) rotate(45deg);transition:transform .2s}
.faq details[open] summary::after{transform:translateY(-30%) rotate(-135deg)}
.faq details p{padding:0 16px 16px;color:var(--muted);animation:rise .25s ease both}

/* announcement popup (home only) */
.pop{position:fixed;inset:0;z-index:900;display:flex;align-items:center;justify-content:center;padding:24px;background:rgba(20,18,59,.38);-webkit-backdrop-filter:blur(4px);backdrop-filter:blur(4px);animation:fadein .22s ease both}
.pop.out,.mt.out{animation:fadeout .2s ease both}
.pop-card{position:relative;width:min(100%,340px);padding:22px 20px 24px;border:3px solid var(--ink);border-radius:26px;box-shadow:0 6px 0 var(--ink);overflow:hidden;animation:popin .32s cubic-bezier(.2,1.3,.4,1) both,floaty 3.6s ease-in-out .4s infinite}
@keyframes popin{from{opacity:0;transform:translateY(12px) scale(.9)}to{opacity:1;transform:none}}
@keyframes floaty{0%,100%{transform:translateY(0)}50%{transform:translateY(-4px)}}
.k-info{background:#D9D6FF}.k-warn{background:#FFD34D}.k-ok{background:#8EF0B0}
.pop-card h3{font-size:22px;font-weight:800;letter-spacing:-.02em;line-height:1.1;padding-right:34px}
.pop-card p{margin-top:8px;font-weight:600;line-height:1.4}
.pop-x{position:absolute;top:10px;right:10px;width:34px;height:34px;border-radius:50%;border:2px solid var(--ink);background:rgba(255,255,255,.65);color:var(--ink);display:grid;place-items:center;cursor:pointer;padding:0}
.pop-btn{position:relative;overflow:hidden;display:inline-flex;align-items:center;justify-content:center;margin-top:16px;min-height:48px;padding:0 24px;border-radius:999px;border:2px solid var(--ink);background:var(--cobalt);color:#fff;font-weight:700;font-size:16px;text-decoration:none;box-shadow:0 4px 0 var(--ink);transition:transform .08s,box-shadow .08s}
.pop-btn:active{transform:translateY(3px);box-shadow:0 1px 0 var(--ink)}
.pop-btn::after{content:"";position:absolute;top:0;bottom:0;left:-60%;width:40%;background:linear-gradient(100deg,transparent,rgba(255,255,255,.45),transparent);animation:shine 4s ease-in-out 1s infinite}
@keyframes shine{0%,55%{left:-60%}100%{left:130%}}
.pop-bar{position:absolute;left:0;bottom:0;height:5px;width:100%;background:var(--ink);transform-origin:left;animation:drain linear forwards}
@keyframes drain{to{transform:scaleX(0)}}

/* maintenance: blur everything, nothing is clickable */
.mt{position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;padding:24px;background:rgba(20,18,59,.62);-webkit-backdrop-filter:blur(18px) saturate(.85);backdrop-filter:blur(18px) saturate(.85);animation:fadein .25s ease both;touch-action:none;overscroll-behavior:contain}
.mt-card{width:min(100%,360px);border:3px solid var(--ink);border-radius:28px;background:#FFD34D;box-shadow:0 8px 0 var(--ink);overflow:hidden;text-align:center;animation:popin .35s cubic-bezier(.2,1.3,.4,1) both}
.mt-stripe{height:16px;background:repeating-linear-gradient(135deg,var(--ink) 0 14px,#FFD34D 14px 28px)}
.mt-body{padding:22px 20px 24px}
.mt-ico{width:66px;height:66px;margin:0 auto 12px;border-radius:50%;background:var(--ink);display:grid;place-items:center;animation:mpulse 2.2s ease-in-out infinite}
@keyframes mpulse{50%{transform:scale(1.07)}}
.mt-card h2{font-size:28px}
.mt-card p{margin-top:10px;font-weight:600;line-height:1.4}
.mt-eta{margin-top:16px;display:inline-block;padding:8px 16px;border:2.5px solid var(--ink);border-radius:999px;background:#fff;font-weight:800;font-variant-numeric:tabular-nums}
.achip{position:fixed;left:50%;bottom:14px;transform:translateX(-50%);z-index:950;padding:8px 14px;border:2px solid var(--ink);border-radius:999px;background:#FFD34D;font-weight:700;font-size:13px;white-space:nowrap}

/* v13: brand lockup, living logo, footer, legal pages */
.brand{align-items:center;gap:12px}
.brand svg{width:44px;height:44px;flex:none;animation:nbping 8s ease-in-out 3s infinite}
.lock{display:flex;flex-direction:column;line-height:1;min-width:0}
.wm{display:block;font-size:26px;font-weight:800;letter-spacing:-.02em;line-height:1;white-space:nowrap}
.tag{margin-top:6px;font-size:10.5px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.wm .ch{display:inline-block}
.wm:not(.still) .ch{animation:chrise .5s cubic-bezier(.2,.9,.3,1) both;animation-delay:calc(var(--i,0)*45ms + 120ms)}
@keyframes chrise{from{opacity:0;transform:translateY(9px)}to{opacity:1;transform:none}}
.ii{position:relative;width:.2em;height:.5em;margin:0 .035em 0 .07em}
.stem{position:absolute;left:calc(50% - .0775em);bottom:0;width:.155em;height:.52em;border-radius:.05em;background:currentColor}
.dot{position:absolute;left:calc(50% - .09em);bottom:.64em;width:.18em;height:.18em;border-radius:50%;background:var(--cobalt)}
.brand .dot{animation:dothop 5.5s ease-in-out 2.2s infinite}
@keyframes dothop{0%,88%,100%{transform:none}91%{transform:translateY(-.34em) scale(.9,1.1)}95%{transform:translateY(0) scale(1.18,.78)}97.5%{transform:scale(.96,1.05)}}
.brand .dot.jump{animation:doottap .5s ease}
@keyframes doottap{0%{transform:none}35%{transform:translateY(-.5em) scale(.9,1.1)}70%{transform:translateY(0) scale(1.2,.75)}100%{transform:none}}
@keyframes nbping{0%,93%,100%{transform:scale(1)}96.5%{transform:scale(1.07)}}
.brand .nbf{animation:nbin 1s cubic-bezier(.2,.8,.2,1) both,nbbreath 6s ease-in-out 1.2s infinite}
@keyframes nbin{from{transform:scaleY(.08)}to{transform:scaleY(1)}}
@keyframes nbbreath{0%,100%{transform:scaleY(1)}50%{transform:scaleY(.86)}}
.nbck{display:none;fill:none;stroke:#2C3CE2;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
[data-state=loading] .brand .nbf{animation:nbload 1.5s ease-in-out infinite}
@keyframes nbload{0%{transform:scaleY(.15)}80%,100%{transform:scaleY(1)}}
[data-state=loading] .brand .nbar{animation:nbbob .9s ease-in-out infinite}
@keyframes nbbob{50%{transform:translateY(1.2px)}}
[data-state=loading] .brand .dot{animation:dotbob .8s ease-in-out infinite}
@keyframes dotbob{50%{transform:translateY(-.14em)}}
[data-state=done] .brand .nbf{animation:none;transform:scaleY(1)}
[data-state=done] .brand .nbar{display:none}
[data-state=done] .brand .nbck{display:block;transform-box:fill-box;transform-origin:center;animation:pop .22s cubic-bezier(.2,1.4,.4,1)}
[data-state=done] .brand .dot{animation:dotpop .5s ease both}
@keyframes dotpop{0%{transform:scale(1)}40%{transform:scale(1.7)}100%{transform:scale(1)}}
[data-state=error] .brand .nbf{animation:none;transform:scaleY(.3);fill:url(#nbr)}
[data-state=error] .brand svg{animation:nbshake .4s ease both}
@keyframes nbshake{20%{transform:translateX(-2px)}40%{transform:translateX(2px)}60%{transform:translateX(-1.5px)}80%{transform:translateX(1px)}}
[data-state=error] .brand .dot{background:var(--coral);animation:none}

.hint{margin-top:12px;text-align:center;font-size:13.5px;font-weight:600;color:var(--muted)}
.foot{margin-top:34px;padding:22px 0 10px;border-top:2px solid var(--ink);text-align:center}
.foot .wm{display:inline-block;font-size:20px}
.foot nav{display:flex;justify-content:center;gap:20px;margin-top:12px;font-weight:700}
.foot nav a{color:var(--cobalt);text-decoration:none;padding:6px 2px;border-bottom:2px solid var(--sun)}
.foot p{margin:12px auto 0;max-width:34ch;font-size:13px;color:var(--muted)}
.faq details p a{color:var(--cobalt);font-weight:700}

.legal{padding:22px 0 6px}
.legal h1{font-size:clamp(32px,9vw,44px)}
.legal .stamp{display:inline-block;margin-top:12px;padding:5px 13px;border:2px solid var(--ink);border-radius:999px;background:var(--sun);font-weight:700;font-size:13px}
.legal .intro{margin-top:16px;font-size:18px;line-height:1.5}
.legal section{margin-top:28px}
.legal h2{font-size:21px;padding-left:12px;border-left:5px solid var(--sun)}
.legal p,.legal li{margin-top:10px;color:var(--muted);line-height:1.55}
.legal ul{padding-left:20px}
.legal strong{color:var(--ink)}
.legal a{color:var(--cobalt);font-weight:700}
.legal-back{margin-top:30px}


/* picker: posts, stories and highlights that hold several items */
.picker{display:none;width:100%;text-align:left;margin-top:14px}
[data-state=pick] .picker{display:block}
body[data-page=share] [data-state=pick] .stage{justify-content:flex-start;padding-top:10px}
[data-state=pick] .batwrap,[data-state=pick] .pct,[data-state=pick] .note:empty{display:none}
.pk-top{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:10px}
.pk-count{font-weight:700;font-size:15px;color:var(--muted)}
.pk-all{appearance:none;border:2px solid var(--ink);background:var(--lilac);color:var(--ink);font:inherit;font-weight:700;font-size:14px;padding:7px 14px;border-radius:999px;cursor:pointer}
.pk-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}
.pk-item{position:relative;aspect-ratio:3/4;padding:0;border:2.5px solid var(--ink);border-radius:14px;overflow:hidden;background:var(--lilac);cursor:pointer;-webkit-tap-highlight-color:transparent;transition:transform .08s}
.pk-item:active{transform:scale(.97)}
.pk-item img{width:100%;height:100%;object-fit:cover;display:block}
.pk-item::after{content:"";position:absolute;inset:0;background:rgba(59,47,224,0);transition:background .12s}
.pk-item[aria-pressed=true]{border-color:var(--cobalt);box-shadow:0 0 0 3px var(--cobalt)}
.pk-item[aria-pressed=true]::after{background:rgba(59,47,224,.22)}
.pk-tick{position:absolute;top:6px;right:6px;z-index:2;width:24px;height:24px;border-radius:50%;border:2.5px solid #fff;background:rgba(20,18,59,.35)}
.pk-item[aria-pressed=true] .pk-tick{background:var(--cobalt)}
.pk-item[aria-pressed=true] .pk-tick::after{content:"";position:absolute;left:6px;top:2px;width:5px;height:10px;border:solid #fff;border-width:0 2.5px 2.5px 0;transform:rotate(45deg)}
.pk-vid{position:absolute;left:6px;bottom:6px;z-index:2;width:24px;height:24px;border-radius:50%;background:rgba(20,18,59,.72)}
.pk-vid::after{content:"";position:absolute;left:9px;top:6px;border-left:8px solid #fff;border-top:6px solid transparent;border-bottom:6px solid transparent}
.pk-bar{position:sticky;bottom:0;z-index:5;margin-top:14px;padding:10px 0 12px;background:linear-gradient(to top,var(--mist) 72%,rgba(236,235,255,0))}
.pk-bar .btn{width:100%}
.pk-bar .btn[disabled]{opacity:.45;pointer-events:none}

/* support button */
.support{display:flex;align-items:center;gap:12px;width:100%;max-width:340px;margin:16px auto 0;padding:12px 16px;border:2.5px solid var(--ink);border-radius:20px;background:#fff;color:var(--ink);text-decoration:none;text-align:left;box-shadow:0 4px 0 var(--ink);transition:transform .08s,box-shadow .08s}
.support:active{transform:translateY(3px);box-shadow:0 1px 0 var(--ink)}
.support .si{flex:none;width:38px;height:38px;border-radius:50%;background:var(--cobalt);display:grid;place-items:center}
.support .si svg{width:20px;height:20px}
.support b{display:block;font-size:16px;letter-spacing:-.01em;line-height:1.15}
.support small{display:block;margin-top:2px;font-size:13.5px;font-weight:600;color:var(--cobalt);line-height:1.2}
.support.mini{flex:none;width:auto;max-width:none;margin:10px auto 0;padding:7px 14px 7px 8px;gap:9px;border-radius:999px;border-width:2px;box-shadow:0 3px 0 var(--ink)}
.support.mini .si{width:28px;height:28px}
.support.mini .si svg{width:15px;height:15px}
.support.mini b{display:inline;font-size:14px}
.support.mini small{display:inline;margin:0 0 0 4px;font-size:14px}
@media (max-height:600px){.support.mini small{display:none}}
body[data-page=share] .wrap{align-items:stretch}
body[data-page=share] .support.mini{align-self:center}

@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{animation:none!important;transition:none!important}
  .pop-bar{display:none}
}
"""

HEAD = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#ECEBFF">
<title>__TITLE__</title>
<meta name="description" content="Share an Instagram reel to NirnayDown and it downloads automatically. Free, no login.">
<meta property="og:type" content="website">
<meta property="og:site_name" content="NirnayDown">
<meta property="og:title" content="__TITLE__">
<meta property="og:description" content="Share an Instagram reel to NirnayDown and it downloads automatically. Free, no login.">
<meta property="og:image" content="__ORIGIN__/icon-512.png?v=3">
<meta name="twitter:card" content="summary">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/icon-192.png?v=3">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wdth,wght@12..96,75..100,400..800&display=swap" rel="stylesheet" media="print" onload="this.media='all'">
<noscript><link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wdth,wght@12..96,75..100,400..800&display=swap" rel="stylesheet"></noscript>
<style>__CSS__</style>
</head>
"""

CONTACT_URL = "https://t.me/NirnayIG"
CONTACT_NAME = "@NirnayIG"
LEGAL_DATE = "22 November 2026"


def _wordmark(animated=True):
    """'NirnayDown' with a hand-built i, so the dot can move and no extra font file is needed."""
    out = []
    for n, ch in enumerate("NirnayDown"):
        style = f' style="--i:{n}"' if animated else ""
        if ch == "i":
            out.append(f'<span class="ch ii"{style}><i class="stem"></i><i class="dot"></i></span>')
        else:
            out.append(f'<span class="ch"{style}>{ch}</span>')
    cls = "wm" if animated else "wm still"
    return f'<span class="{cls}" aria-hidden="true">{"".join(out)}</span><span class="sr">NirnayDown</span>'


BRAND = (
    '<a class="brand" href="/" aria-label="NirnayDown home">'
    '<svg viewBox="0 0 32 32" aria-hidden="true"><defs>'
    '<linearGradient id="nbg" x1="0" y1="0" x2="0.15" y2="1"><stop offset="0" stop-color="#60B8FF"/><stop offset="1" stop-color="#2E3EEA"/></linearGradient>'
    '<linearGradient id="nby" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#FFE87A"/><stop offset="1" stop-color="#FFB800"/></linearGradient>'
    '<linearGradient id="nbr" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#FF8FA0"/><stop offset="1" stop-color="#E8445F"/></linearGradient>'
    '<clipPath id="nbc"><rect x="9.4" y="9.2" width="13.2" height="17" rx="3.4"/></clipPath></defs>'
    '<rect width="32" height="32" rx="7.4" fill="url(#nbg)"/>'
    '<rect x="12.6" y="5.6" width="6.8" height="2.2" rx="1.1" fill="#ECF3FF"/>'
    '<rect x="8.2" y="7.4" width="15.6" height="20.6" rx="4.6" fill="#fff"/>'
    '<g clip-path="url(#nbc)"><rect class="nbf" x="9" y="9" width="14" height="17.4" fill="url(#nby)"/></g>'
    '<path class="nbar" d="M16 13.6v6.6M13.2 17.7L16 20.5l2.8-2.8" fill="none" stroke="#2C3CE2" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/>'
    '<path class="nbck" d="M12.7 17.6l2.5 2.6 4.4-5"/></svg>'
    '<span class="lock">' + _wordmark(True) + '<small class="tag">Instagram reel downloader</small></span></a>'
)


_PLANE = '<svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 3L10.5 13.5M21 3l-6.5 18-4-7.5L3 9.5z"/></svg>'
SUPPORT = (
    f'<a class="support" href="{CONTACT_URL}" target="_blank" rel="noopener">'
    f'<span class="si">{_PLANE}</span><span><b>Need help? Support</b><small>Click here to chat on Telegram</small></span></a>'
)
SUPPORT_MINI = (
    f'<a class="support mini" href="{CONTACT_URL}" target="_blank" rel="noopener">'
    f'<span class="si">{_PLANE}</span><span><b>Support</b><small>Click here</small></span></a>'
)

FOOT = (
    '<footer class="foot">' + _wordmark(False) + SUPPORT +
    '<nav><a href="/privacy">Privacy</a><a href="/terms">Terms</a>'
    f'<a href="{CONTACT_URL}" target="_blank" rel="noopener">Contact</a></nav>'
    '<p>Only download content that is yours or that you have permission to save. NirnayDown is not affiliated with Instagram or Meta.</p></footer>'
)

PLAY = r"""<svg class="play" viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5.5v13l11-6.5z" fill="currentColor"/></svg>"""

BAT = r"""<svg class="bat" viewBox="0 0 120 192" aria-hidden="true"><defs><linearGradient id="bbd" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fff"/><stop offset="1" stop-color="#DCE5FF"/></linearGradient><linearGradient id="bys" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#FFE87A"/><stop offset="1" stop-color="#FFB800"/></linearGradient><linearGradient id="brd" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#FF8FA0"/><stop offset="1" stop-color="#E8445F"/></linearGradient><clipPath id="bcl"><rect x="18" y="26" width="84" height="152" rx="22"/></clipPath></defs><rect x="42" y="3" width="36" height="13" rx="6.5" fill="#E4EBFF"/><rect x="6" y="12" width="108" height="176" rx="32" fill="url(#bbd)"/><g clip-path="url(#bcl)"><rect class="bfill" x="18" y="26" width="84" height="152" fill="url(#bys)"/><rect x="24" y="32" width="9" height="140" rx="4.5" fill="#fff" opacity=".35"/></g><path class="barrow" d="M60 68v44M42 96l18 19 18-19"/><path class="bcheck" d="M41 98l14 15 26-30"/><path class="bx" d="M45 82l30 30M75 82L45 112"/></svg>"""

NDX = r"""<script>
(function(){
var page=document.body.getAttribute('data-page'),mt=null,left=0,tick=null,popped=false,chip=null;
function pad(n){return String(n).padStart(2,'0');}
function fmt(s){var h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;return h?h+':'+pad(m)+':'+pad(x):m+':'+pad(x);}
function lock(on){
  document.documentElement.style.overflow=on?'hidden':'';
  [].forEach.call(document.querySelectorAll('.wrap,.pop'),function(e){e.inert=on;});
}
function showMaint(b){
  left=b.left||0;
  if(!mt){
    mt=document.createElement('div');mt.className='mt';mt.setAttribute('role','alertdialog');mt.setAttribute('aria-modal','true');
    mt.innerHTML='<div class="mt-card"><div class="mt-stripe"></div><div class="mt-body"><div class="mt-ico"><svg viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="#FFD34D" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3.5L2.8 19.5h18.4z"/><path d="M12 10v4.5M12 17.3v.2"/></svg></div><h2>Under maintenance</h2><p class="mt-msg"></p><div class="mt-eta" hidden></div></div><div class="mt-stripe"></div></div>';
    document.body.appendChild(mt);lock(true);
  }
  mt.querySelector('.mt-msg').textContent=b.msg||'';
  var eta=mt.querySelector('.mt-eta');
  clearInterval(tick);
  if(left>0){
    eta.hidden=false;eta.textContent='Back in '+fmt(left);
    tick=setInterval(function(){left--;if(left<=0){clearInterval(tick);eta.hidden=true;check();}else{eta.textContent='Back in '+fmt(left);}},1000);
  }else{eta.hidden=true;}
}
function hideMaint(){
  if(!mt)return;
  clearInterval(tick);lock(false);
  var m=mt;mt=null;m.classList.add('out');setTimeout(function(){m.remove();},220);
}
function popup(b){
  var key='nd_b_'+b.id;
  try{if(b.dismiss&&localStorage.getItem(key))return;}catch(e){}
  var secs=+b.secs||0,closed=false,t=null;
  var ov=document.createElement('div');ov.className='pop';ov.setAttribute('role','dialog');ov.setAttribute('aria-modal','true');
  var c=document.createElement('div');c.className='pop-card k-'+(b.kind==='warn'||b.kind==='ok'?b.kind:'info');
  var x=document.createElement('button');x.type='button';x.className='pop-x';x.setAttribute('aria-label','Close');
  x.innerHTML='<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>';
  c.appendChild(x);
  if(b.title){var h=document.createElement('h3');h.textContent=b.title;c.appendChild(h);}
  if(b.text){var p=document.createElement('p');p.textContent=b.text;c.appendChild(p);}
  if(b.link&&/^https?:\/\//i.test(b.link)){var a=document.createElement('a');a.className='pop-btn';a.href=b.link;a.target='_blank';a.rel='noopener noreferrer';a.textContent=b.label||'Open';c.appendChild(a);}
  if(secs>0){var bar=document.createElement('i');bar.className='pop-bar';bar.style.animationDuration=secs+'s';c.appendChild(bar);}
  ov.appendChild(c);document.body.appendChild(ov);
  function close(){
    if(closed)return;closed=true;clearTimeout(t);
    if(b.dismiss){try{localStorage.setItem(key,'1');}catch(e){}}
    ov.classList.add('out');setTimeout(function(){ov.remove();},220);
  }
  x.addEventListener('click',close);
  ov.addEventListener('click',function(e){if(e.target===ov)close();});
  if(secs>0)t=setTimeout(close,secs*1000);
}
function admChip(on){
  if(on&&!chip){chip=document.createElement('div');chip.className='achip';chip.textContent='Maintenance is ON (admin view)';document.body.appendChild(chip);}
  else if(!on&&chip){chip.remove();chip=null;}
}
function check(){
  fetch('/api/banner',{cache:'no-store'}).then(function(r){return r.json();}).then(function(b){
    admChip(!!b.admin_maint);
    if(b.maint&&b.hard){showMaint(b);}
    else{
      hideMaint();
      if(!popped&&page==='home'&&b.on){popped=true;setTimeout(function(){popup(b);},400);}
    }
  }).catch(function(){});
}
var br=document.querySelector('.brand');
if(br)br.addEventListener('click',function(e){
  if(page!=='home')return;
  e.preventDefault();
  var d=br.querySelector('.dot');if(!d)return;
  d.classList.remove('jump');void d.offsetWidth;d.classList.add('jump');
});
check();
setInterval(function(){if(document.visibilityState==='visible')check();},45000);
document.addEventListener('visibilitychange',function(){if(document.visibilityState==='visible')check();});
})();
</script>"""

HOME_HTML = (
    HEAD
    + r"""<body data-page="home">
<script>try{if(matchMedia('(display-mode: standalone)').matches||localStorage.getItem('nd_inst')==='1')document.documentElement.classList.add('inst')}catch(e){}</script>
<div class="wrap">
  <header>__BRAND__</header>

  <section class="hero">
    <div>
      <h1>Share a reel, it downloads automatically.</h1>
      <p>Tap Share on a reel, post, story or highlight in Instagram and choose NirnayDown. It saves straight to your phone.</p>
    </div>
    <div class="batwrap" aria-hidden="true">__BAT__</div>
  </section>

  <form id="go" autocomplete="off">
    <div class="field">
      <input id="link" type="url" inputmode="url" placeholder="Paste the link" aria-label="Instagram link" required>
      <button type="button" class="btn" id="paste">Paste</button>
    </div>
    <button class="btn primary block go" type="submit">Download</button>
  </form>

  <section class="install">
    <div class="how">
      <h2>Add to the share menu</h2>
      <p class="lead">Install it once, then share straight from any reel.</p>
      <button class="btn primary" id="install" hidden>Install NirnayDown</button>
      <ol class="track">
        <li><b>Install it</b><span>Choose "Install app" from the Chrome menu.</span></li>
        <li><b>Open a reel, post or story in Instagram</b><span>Tap the Share button.</span></li>
        <li><b>Choose NirnayDown</b><span>The download starts, then you go back to Instagram.</span></li>
      </ol>
    </div>
    <div class="badge" id="installed" role="status">
      <svg viewBox="0 0 32 32" aria-hidden="true"><circle cx="16" cy="16" r="14" fill="#14123B"/><path d="M9.5 16.5l4.5 4.5 8.5-9.5" fill="none" stroke="#8EF0B0" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"/></svg>
      <div><strong>Already installed</strong><span>Share any reel from Instagram to NirnayDown.</span></div>
    </div>
  </section>

  <section class="faq">
    <h2>Quick answers</h2>
    <details><summary>Is NirnayDown really free?</summary><p>Yes. Share a reel and it downloads. No sign-up and no payment.</p></details>
    <details><summary>Do I need to log in to Instagram?</summary><p>No. You just share or paste the link. You never enter your Instagram password here.</p></details>
    <details><summary>How good is the video quality?</summary><p>You get the video file exactly as Instagram serves it. We add no extra compression.</p></details>
    <details><summary>How fast is it?</summary><p>Usually a few seconds from tap to download start, depending on your connection.</p></details>
    <details><summary>Is my data safe?</summary><p>Your videos are never stored. They stream straight to your phone. We only keep anonymous usage numbers to keep the app running smoothly. Read the <a href="/privacy">Privacy Policy</a>.</p></details>
  </section>

  __FOOT__
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
const standalone=matchMedia('(display-mode: standalone)').matches;
const mark=on=>{document.documentElement.classList.toggle('inst',on);try{on?localStorage.setItem('nd_inst','1'):localStorage.removeItem('nd_inst');}catch(_){}};
addEventListener('appinstalled',()=>{$('#install').hidden=true;mark(true);});
if(standalone) mark(true);
if(navigator.getInstalledRelatedApps){navigator.getInstalledRelatedApps().then(a=>{if(a&&a.length) mark(true); else if(!standalone) mark(false);}).catch(()=>{});}
if('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js');
</script>
__NDX__
</body>
</html>
"""
)

SHARE_HTML = (
    HEAD
    + r"""<body data-page="share">
<main class="wrap" id="app" data-state="idle" data-mode="share">
  <header>__BRAND__</header>
  <section class="stage">
    <div class="batwrap" id="frame" aria-hidden="true">__BAT__</div>
    <p class="pct" id="pct" aria-hidden="true"></p>
    <h1 id="status" aria-live="polite">Link needed</h1>
    <p class="sub" id="sub">Tap Share on a reel, post, story or highlight in Instagram and choose NirnayDown. Or paste the link here.</p>
    <p class="note" id="note" aria-live="polite"></p>

    <section class="picker" id="picker" aria-label="Choose what to download">
      <div class="pk-top"><span class="pk-count" id="pk-count"></span><button type="button" class="pk-all" id="pk-all">Select all</button></div>
      <div class="pk-grid" id="pk-grid"></div>
      <div class="pk-bar"><button type="button" class="btn primary block" id="pk-go" disabled>Download (0) selected</button></div>
    </section>

    <div class="actions">
      <button class="btn primary" id="btn-back" type="button">Back to Instagram</button>
      <button class="btn primary" id="btn-retry" type="button">Try again</button>
      <button class="btn primary" id="btn-another" type="button">Download another</button>
    </div>

    <form id="paste-form" autocomplete="off">
      <div class="field">
        <input id="link" type="url" inputmode="url" placeholder="Paste the link" aria-label="Instagram link" required>
        <button type="button" class="btn" id="paste">Paste</button>
      </div>
      <button class="btn primary block go" type="submit">Download</button>
    </form>
  </section>
  __SUPPORT_MINI__
</main>
<script>
const $=s=>document.querySelector(s);
const app=$('#app'), frame=$('#frame'), statusEl=$('#status'), sub=$('#sub'), note=$('#note'), pct=$('#pct'), inp=$('#link');
const INTENT='intent://#Intent;package=com.instagram.android;action=android.intent.action.MAIN;category=android.intent.category.LAUNCHER;end';
let lastRaw='', lastPaste=false, backTimer=null, fakeTimer=null, current=0;

const setState=s=>{app.dataset.state=s;};
const progress=p=>{current=p;frame.style.setProperty('--k',(p/100).toFixed(3));};
const _say=(h,s)=>{statusEl.textContent=h;sub.textContent=s||'';};
const say=(h,s)=>{_say(h,s);[statusEl,sub].forEach(e=>{e.classList.remove('sw');void e.offsetWidth;e.classList.add('sw');});};

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

// Loads one of our download URLs in a hidden frame and reports how it went (the server sets a short-lived cookie).
function dlFrame(path){
  return new Promise((resolve,reject)=>{
    const token=Math.random().toString(36).slice(2,12);
    const key='dl_'+token;
    const f=document.createElement('iframe');
    f.style.display='none';
    f.src=path+(path.indexOf('?')<0?'?':'&')+'t='+token;
    document.body.appendChild(f);
    const t0=Date.now();
    const timer=setInterval(()=>{
      const m=document.cookie.split('; ').find(c=>c.indexOf(key+'=')===0);
      if(m){
        clearInterval(timer);
        document.cookie=key+'=; Max-Age=0; path=/';
        const val=m.slice(key.length+1);
        if(val==='ok'){ setTimeout(()=>f.remove(),60000); resolve({val}); return; }
        setTimeout(()=>{
          let j=null;
          try{j=JSON.parse(f.contentDocument.body.innerText);}catch(_){}
          f.remove(); resolve({val,j});
        },150);
      }else if(Date.now()-t0>45000){
        clearInterval(timer); f.remove();
        reject(new Error('Could not start the download. Please try again.'));
      }
    },20);
  });
}

// Hand the link straight to the server. One photo/video starts streaming at once (returns null).
// Several items (carousel, stories, highlight) come back as a list for the picker.
async function startDownload(raw){
  const r=await dlFrame('/api/dl?link='+encodeURIComponent(raw));
  if(r.val==='ok') return null;
  if(r.val==='multi' && r.j && r.j.multi) return r.j;
  const e=new Error((r.j&&r.j.error)||'Could not download. Please try again.');
  e.maint=!!(r.j&&r.j.maint);
  throw e;
}

// ---- picker ----
const picker=$('#picker'), pkGrid=$('#pk-grid'), pkGo=$('#pk-go'), pkAll=$('#pk-all'), pkCount=$('#pk-count');
let pickData=null, picked=new Set(), busy=false;
function pkRefresh(){
  const n=picked.size, total=pickData?pickData.items.length:0;
  pkGo.disabled=busy||n===0;
  if(!busy) pkGo.textContent='Download ('+n+') selected';
  pkAll.textContent=(n===total&&total>0)?'Clear all':'Select all';
  pkAll.disabled=busy;
  pkCount.textContent=n+' of '+total+' selected';
  pkGrid.querySelectorAll('.pk-item').forEach(b=>b.setAttribute('aria-pressed',picked.has(+b.dataset.i)?'true':'false'));
}
function showPicker(info){
  pickData=info; picked=new Set(); busy=false;
  pkGrid.textContent='';
  info.items.forEach(it=>{
    const b=document.createElement('button');
    b.type='button'; b.className='pk-item'; b.dataset.i=it.i; b.setAttribute('aria-pressed','false');
    b.setAttribute('aria-label',(it.type==='video'?'Video ':'Photo ')+(it.i+1));
    if(it.thumb){const im=new Image(); im.alt=''; im.loading='lazy'; im.decoding='async'; im.src=it.thumb; b.appendChild(im);}
    const t=document.createElement('span'); t.className='pk-tick'; b.appendChild(t);
    if(it.type==='video'){const v=document.createElement('span'); v.className='pk-vid'; b.appendChild(v);}
    pkGrid.appendChild(b);
  });
  setState('pick');
  say('Choose what to save',info.count+' items here. Tap the ones you want.');
  pkRefresh();
  window.scrollTo(0,0);
}
pkGrid.addEventListener('click',e=>{
  const b=e.target.closest('.pk-item'); if(!b||busy) return;
  const i=+b.dataset.i;
  if(picked.has(i)) picked.delete(i); else picked.add(i);
  pkRefresh();
});
pkAll.addEventListener('click',()=>{
  if(!pickData||busy) return;
  if(picked.size===pickData.items.length) picked.clear(); else pickData.items.forEach(it=>picked.add(it.i));
  pkRefresh();
});
pkGo.addEventListener('click',async()=>{
  if(busy||!picked.size||!pickData) return;
  busy=true; note.textContent=''; pkRefresh();
  const sel=[...picked].sort((a,b)=>a-b);
  let done=0;
  try{
    for(const i of sel){
      pkGo.textContent='Downloading '+(done+1)+' of '+sel.length+'…';
      const r=await dlFrame('/api/file/'+pickData.code+'/'+i);
      if(r.val!=='ok') throw new Error((r.j&&r.j.error)||'Could not download item '+(i+1)+'.');
      done++; picked.delete(i);
      if(done<sel.length) await new Promise(res=>setTimeout(res,450)); // let the browser start each file
    }
  }catch(err){
    busy=false; pkRefresh();
    note.textContent=(done?done+' saved. ':'')+(err.message||'Something went wrong.')+' Tap Download to try the rest again.';
    return;
  }
  busy=false;
  progress(100);
  setState('done');
  if(lastPaste){
    say('Download started',done+(done>1?' files are':' file is')+' saving. Paste another link whenever you like.');
  }else{
    say('Download started','Going back to Instagram…');
    goBack();
  }
});

async function run(raw,paste){
  lastRaw=raw; lastPaste=!!paste;
  app.dataset.mode=lastPaste?'paste':'share';
  clearTimeout(backTimer); stopFake();
  progress(0); pct.textContent=''; note.textContent='';
  setState('loading');
  say('Downloading','');
  startFake();
  try{
    const list=await startDownload(raw);
    stopFake();
    if(list){ progress(0); showPicker(list); return; }
    progress(100);
    setState('done');
    if(lastPaste){
      say('Download started','Your file is saving. Paste another link whenever you like.');
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
    say('This does not look like an Instagram link','Paste a reel, post, story or highlight link.');
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

const IDLE_H='Link needed', IDLE_S='Tap Share on a reel, post, story or highlight in Instagram and choose NirnayDown. Or paste the link here.';
function resetToIdle(){
  clearTimeout(backTimer); stopFake();
  frame.classList.remove('has-thumb'); frame.style.removeProperty('--thumb');
  progress(0); pct.textContent=''; note.textContent=''; inp.value=''; lastRaw=''; lastPaste=false; app.dataset.mode='share';
  pkGrid.textContent=''; pickData=null; picked=new Set(); busy=false;
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
__NDX__
</body>
</html>
"""
)


def render(page, title):
    html = page.replace("__CSS__", CSS).replace("__TITLE__", title)
    origin = f"{request.headers.get('X-Forwarded-Proto', 'https')}://{request.host}"
    html = html.replace("__BRAND__", BRAND).replace("__FOOT__", FOOT).replace("__PLAY__", PLAY).replace("__BAT__", BAT).replace("__NDX__", NDX).replace("__SUPPORT_MINI__", SUPPORT_MINI)
    html = html.replace("__ORIGIN__", origin)
    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/")
def home():
    return render(HOME_HTML, "NirnayDown \u00b7 Instagram Reel Downloader")


@app.get("/share")
def share():
    return render(SHARE_HTML, "NirnayDown")


def _legal(title, intro, sections):
    body = f'<h1>{title}</h1><p class="stamp">Last updated {LEGAL_DATE}</p><p class="intro">{intro}</p>'
    for heading, html in sections:
        body += f"<section><h2>{heading}</h2>{html}</section>"
    return body


LEGAL_HTML = (
    HEAD
    + """<body data-page="legal">
<div class="wrap">
  <header>__BRAND__</header>
  <article class="legal">__LEGAL__</article>
  <a class="btn primary block legal-back" href="/">Back to NirnayDown</a>
  __FOOT__
</div>
</body>
</html>"""
)

_CONTACT = f'<a href="{CONTACT_URL}" target="_blank" rel="noopener">{CONTACT_NAME}</a> on Telegram'

PRIVACY = _legal(
    "Privacy Policy",
    "NirnayDown fetches a reel, post, story or highlight when you share or paste its link. There is no sign-up or login, and we do not keep your videos.",
    [
        ("What we handle", "<ul>"
            "<li><strong>The Instagram link</strong> (reel, post, story or highlight) you share or paste. We use it only to fetch that content from Instagram for you.</li>"
            "<li><strong>The video</strong> passes through our server on its way to your phone. We do not keep a copy. We may remember the video&rsquo;s web address in temporary memory for about 10 minutes so a retry is quick.</li>"
            "<li><strong>Anonymous usage records:</strong> the reel&rsquo;s short code, the time, whether it worked, how long it took, which method was used, your device type (Android, iPhone or desktop) and a shortened, scrambled (hashed) version of your network address. We do not collect your name, phone number, email or Instagram account.</li>"
            "</ul>"),
        ("Why we keep usage records", "<p>To fix problems, see how fast the app is and keep it reliable. Only the most recent records are kept, and they may be cleared whenever the server restarts.</p>"),
        ("Cookies and storage", "<ul>"
            "<li>A short-lived cookie (about one minute) tells the page that your download has started.</li>"
            "<li>Your browser&rsquo;s local storage remembers whether the app is installed and whether you have already seen an announcement.</li>"
            "<li>We do not use advertising or tracking cookies today.</li>"
            "</ul>"),
        ("Other services involved", "<ul>"
            "<li><strong>Instagram / Meta</strong> servers, to fetch the video you asked for.</li>"
            "<li><strong>Google Fonts</strong>, which delivers our text font. Google can see your network address when it does.</li>"
            "<li><strong>Our hosting provider</strong>, which processes requests for us.</li>"
            "<li>If we add ads in future, the ad partner may use cookies. We will update this page when that happens.</li>"
            "</ul>"),
        ("Sharing your information", "<p>We do not sell personal information. We may share information if the law requires it.</p>"),
        ("Children", "<p>NirnayDown is not meant for children under 13.</p>"),
        ("Your choices", "<p>You can clear this site&rsquo;s data in your browser at any time. Our records are anonymous, so we may not be able to find the ones that belong to you. If you have a question, contact us.</p>"),
        ("Changes", "<p>If we change this policy we will update the date at the top of this page.</p>"),
        ("Contact", f"<p>{_CONTACT}.</p>"),
    ],
)

TERMS = _legal(
    "Terms of Use",
    "By using NirnayDown you agree to these terms. If you do not agree, please do not use the app.",
    [
        ("What NirnayDown does", "<p>It helps you save Instagram reels, posts, stories and highlights to your own phone when you share or paste a link.</p>"),
        ("Your responsibility", "<ul>"
            "<li>Only download reels, posts, stories and highlights that are yours or that the creator has given you permission to save.</li>"
            "<li>Respect creators&rsquo; rights, copyright and Instagram&rsquo;s own terms.</li>"
            "<li>Do not re-upload, sell or claim other people&rsquo;s content as your own.</li>"
            "</ul>"),
        ("Not allowed", "<ul>"
            "<li>Sending bulk or automated requests, or trying to overload, break or bypass the app.</li>"
            "<li>Trying to reach private content or anything you are not allowed to see.</li>"
            "<li>Using the app for anything illegal, abusive or harmful.</li>"
            "</ul>"),
        ("No connection to Instagram", "<p>NirnayDown is an independent app. It is not affiliated with, endorsed by or sponsored by Instagram or Meta. Names and logos belong to their owners.</p>"),
        ("Copyright and removal requests", f"<p>If you own a reel and want it blocked from NirnayDown, message {_CONTACT} with the reel link and a short note that you are the owner. We will review it and block the reel where appropriate.</p>"),
        ("Availability", "<p>The app may change, pause or go offline for maintenance. Instagram can change how reels work, so we cannot promise that every reel will always download.</p>"),
        ("No warranty", "<p>NirnayDown is provided &ldquo;as is&rdquo; without promises of any kind. To the extent the law allows, we are not responsible for any loss or damage that comes from using it.</p>"),
        ("Governing law", "<p>These terms are governed by the laws of India.</p>"),
        ("Changes", "<p>We may update these terms. The date at the top shows when they last changed. Using the app after a change means you accept it.</p>"),
        ("Contact", f"<p>{_CONTACT}.</p>"),
    ],
)


@app.get("/privacy")
def privacy():
    return render(LEGAL_HTML.replace("__LEGAL__", PRIVACY), "Privacy Policy \u00b7 NirnayDown")


@app.get("/terms")
def terms():
    return render(LEGAL_HTML.replace("__LEGAL__", TERMS), "Terms of Use \u00b7 NirnayDown")



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
    "banner_label": "Open", "banner_dismiss": True, "banner_secs": 10, "banner_from": 0, "banner_to": 0, "banner_id": 1,
    "rate_limit": 15, "blocked_ips": [], "blocked_codes": [], "use_embed": True, "ig_cookies": "", "ig_slots": ["", "", "", "", ""],
    "allow_post": True, "allow_story": True, "allow_highlight": True,
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


def _netscape(text):
    """Accept Netscape cookies text, or just the sessionid value, and return Netscape text ('' if unusable)."""
    text = (text or "").strip()
    if not text:
        return ""
    if text[0] in "[{":  # Cookie-Editor / browser JSON export
        try:
            items = json.loads(text)
        except ValueError:
            return ""
        items = [items] if isinstance(items, dict) else items
        lines = ["# Netscape HTTP Cookie File"]
        for c in items:
            if not isinstance(c, dict) or not c.get("name") or c.get("value") is None:
                continue
            dom = str(c.get("domain") or ".instagram.com")
            if "instagram.com" not in dom:
                continue
            exp = int(c.get("expirationDate") or 2000000000)
            sub = "FALSE" if c.get("hostOnly") else "TRUE"
            sec = "TRUE" if c.get("secure", True) else "FALSE"
            lines.append("\t".join([dom, sub, str(c.get("path") or "/"), sec, str(exp), str(c["name"]), str(c["value"])]))
        text = "\n".join(lines)
        return text + "\n" if "\tsessionid\t" in text else ""
    if "\t" not in text and "\n" not in text:
        sid = text.split("sessionid=")[-1].split(";")[0].strip() if "sessionid=" in text else text
        if not re.fullmatch(r"[A-Za-z0-9%._-]{20,}", sid):
            return ""
        return ("# Netscape HTTP Cookie File\n"
                f".instagram.com\tTRUE\t/\tTRUE\t2000000000\tsessionid\t{sid}\n")
    if "sessionid" not in text:
        return ""
    if not text.startswith("# Netscape"):
        text = "# Netscape HTTP Cookie File\n" + text
    return text + "\n"


def _slots_list():
    raw = S.get("ig_slots")
    raw = list(raw) if isinstance(raw, list) else []
    return (raw + [""] * SLOTS)[:SLOTS]


def apply_slots():
    """Write each filled slot to its own temp file; empty slots get no file."""
    slots = _slots_list()
    with _slot_lock:
        for i, text in enumerate(slots):
            path = os.path.join(tempfile.gettempdir(), f"ig_cookies_{i + 1}.txt")
            if text.strip():
                with open(path, "w") as fh:
                    fh.write(text)
                _slot_files[i] = path
            else:
                _slot_files[i] = None
                try:
                    os.remove(path)
                except OSError:
                    pass
                _slot_state[i].update({"until": 0.0, "err": "", "raw": ""})


DB_OK = _init_db()
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


# one-time migration: old single cookie setting / IG_COOKIES env var -> slot 1 (only while every slot is empty)
if not any(x.strip() for x in _slots_list()):
    _old = _netscape(S.get("ig_cookies") or os.environ.get("IG_COOKIES", ""))
    if _old:
        _sl = _slots_list()
        _sl[0] = _old
        save_settings({"ig_slots": _sl, "ig_cookies": ""})
apply_slots()


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
            "cookies_set": sum(1 for f in _slot_files if f),
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
    data = {k: v for k, v in S.items() if k not in ("ig_cookies", "ig_slots")}
    data["cookies_set"] = sum(1 for f in _slot_files if f)
    return jsonify(data)


def _slot_report():
    now = time.time()
    out = []
    with _slot_lock:
        for i in range(SLOTS):
            st = _slot_state[i]
            filled = bool(_slot_files[i])
            if not filled:
                status = "empty"
            elif st["until"] > now:
                status = "resting"
            else:
                status = "active"
            out.append({"slot": i + 1, "filled": filled, "status": status, "uses": st["uses"], "fails": st["fails"],
                        "rest_min": max(0, int((st["until"] - now) / 60) + 1) if st["until"] > now else 0, "err": st["err"], "raw": st["raw"]})
    return out


@app.get("/admin/api/cookies")
@admin_only
def a_cookies():
    return jsonify(slots=_slot_report(), filled=sum(1 for f in _slot_files if f), total=SLOTS)


@app.post("/admin/api/cookies")
@admin_only
def a_cookies_save():
    body = request.get_json(silent=True) or {}
    try:
        n = int(body.get("slot"))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="Bad slot"), 400
    if not 1 <= n <= SLOTS:
        return jsonify(ok=False, error="Bad slot"), 400
    action = body.get("action", "save")
    slots = _slots_list()
    if action == "clear":
        if slots[n - 1].strip() and sum(1 for x in slots if x.strip()) <= 1:
            return jsonify(ok=False, error="At least 1 slot must stay filled. Replace this cookie instead of removing it."), 400
        slots[n - 1] = ""
    else:
        text = _netscape(str(body.get("value", ""))[:20000])
        if not text:
            return jsonify(ok=False, error="Not a valid cookie. Paste the cookie export (JSON or Netscape) that includes sessionid, or just the sessionid value."), 400
        slots[n - 1] = text
    save_settings({"ig_slots": slots})
    apply_slots()
    return jsonify(ok=True)


@app.post("/admin/api/cookies/reset")
@admin_only
def a_cookies_reset():
    with _slot_lock:
        for st in _slot_state:
            st.update({"until": 0.0, "err": "", "raw": ""})
    return jsonify(ok=True)


@app.post("/admin/api/settings")
@admin_only
def a_settings_save():
    body = request.get_json(silent=True) or {}
    out = {}
    for key, default in DEFAULTS.items():
        if key not in body or key in ("banner_id", "ig_cookies", "ig_slots"):
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
    if any(k in out and out[k] != S[k] for k in ("banner_title", "banner_text", "banner_link", "banner_label")):
        out["banner_id"] = S["banner_id"] + 1  # a changed banner shows again for people who dismissed the old one
    save_settings(out)
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
        link = parse_link((request.get_json(silent=True) or {}).get("link", ""))
    except ReelError as exc:
        return jsonify(ok=False, error=str(exc))
    code, url, kind, want = link["code"], link["url"], link["kind"], link["want"]
    tests = [("api", lambda: via_api(code, url, kind, want)), ("yt-dlp", lambda: via_ytdlp(url, kind, want))]
    if kind in ("reel", "post"):
        tests.append(("embed", lambda: via_embed(code)))
    results = {}
    for name, fn in tests:
        t = time.time()
        try:
            data = fn()
            n = len(data.get("items") or [1])
            results[name] = {"ok": bool(data.get("video")), "ms": int((time.time() - t) * 1000), "reason": "", "via": data.get("source", name), "items": n}
        except Exception as exc:
            results[name] = {"ok": False, "ms": int((time.time() - t) * 1000), "reason": _why(exc), "raw": _clean(exc)}
    return jsonify(ok=True, code=code, kind=kind, results=results)


@app.get("/api/banner")
def api_banner():
    now = time.time()
    admin = is_admin()
    out = {"on": False, "maint": False}
    if maint_active() and not admin:
        left = max(0, int(S["maint_until"] - now)) if S["maint_until"] else 0
        if S["maint_hard"]:
            out = {"on": False, "maint": True, "hard": True, "msg": S["maint_msg"], "left": left}
        else:
            out = {"on": True, "maint": True, "hard": False, "id": "m", "kind": "warn", "title": "Under maintenance",
                   "text": S["maint_msg"], "link": "", "label": "", "secs": 0, "dismiss": False}
    elif S["banner_on"] and (not S["banner_from"] or now >= S["banner_from"]) and (not S["banner_to"] or now <= S["banner_to"]):
        out = {"on": True, "maint": False, "id": S["banner_id"], "kind": S["banner_kind"], "title": S["banner_title"],
               "text": S["banner_text"], "link": S["banner_link"], "label": S["banner_label"],
               "secs": max(0, min(60, int(S["banner_secs"]))), "dismiss": S["banner_dismiss"]}
    if admin and maint_active():
        out["admin_maint"] = True
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
  '<div class="card"><h2>Maintenance</h2>'+ck('m_on',s.maint_on,'Maintenance on')+ck('m_hard',s.maint_hard,'Hard mode: blur the whole app and block downloads (off = warning popup on home only)')+
  '<label>Message</label><textarea id="m_msg" rows="2">'+esc(s.maint_msg)+'</textarea><div class="row"><div><label>Starts (optional)</label><input type="datetime-local" id="m_from" value="'+dt(s.maint_from)+'"></div><div><label>Ends (optional)</label><input type="datetime-local" id="m_to" value="'+dt(s.maint_until)+'"></div></div><p class="k">You (logged in as admin) can still download during maintenance.</p></div>'+
  '<div class="card"><h2>Announcement popup (home screen only, never in share flow)</h2>'+ck('b_on',s.banner_on,'Banner on')+ck('b_dis',s.banner_dismiss,'Show only once per user')+
  '<div class="row"><div><label>Style</label><select id="b_kind"><option value="info">Info</option><option value="warn">Warning</option><option value="ok">Good news</option></select></div><div><label>Auto-hide</label><select id="b_secs"><option value="5">5 sec</option><option value="10">10 sec</option><option value="15">15 sec</option><option value="0">Manual close</option></select></div><div><label>Title</label><input id="b_title" value="'+esc(s.banner_title)+'"></div></div>'+
  '<label>Text</label><textarea id="b_text" rows="2">'+esc(s.banner_text)+'</textarea><div class="row"><div><label>Button link (optional)</label><input id="b_link" value="'+esc(s.banner_link)+'"></div><div><label>Button label</label><input id="b_label" value="'+esc(s.banner_label)+'"></div></div>'+
  '<div class="row"><div><label>Show from (optional)</label><input type="datetime-local" id="b_from" value="'+dt(s.banner_from)+'"></div><div><label>Show until (optional)</label><input type="datetime-local" id="b_to" value="'+dt(s.banner_to)+'"></div></div></div>'+
  '<div class="card"><h2>Limits and fallbacks</h2><label>Lookups per minute per user</label><input type="number" id="rl" value="'+s.rate_limit+'">'+ck('emb',s.use_embed,'Use embed fallback when yt-dlp fails')+ck('apost',s.allow_post,'Allow posts (photos, carousels)')+ck('astory',s.allow_story,'Allow stories (needs a cookie slot)')+ck('ahl',s.allow_highlight,'Allow highlights (needs a cookie slot)')+
  '<label>Blocked users (one per line, copy the user id from Logs)</label><textarea id="bi" rows="2">'+esc(s.blocked_ips.join('\n'))+'</textarea><label>Blocked reel codes (one per line)</label><textarea id="bc" rows="2">'+esc(s.blocked_codes.join('\n'))+'</textarea></div>'+
  '<div class="card" id="ckcard"><h2>Instagram cookies</h2><p class="k">Loading...</p></div>'+
  '<div class="row"><button class="pri" id="sv">Save settings</button><button id="cc">Clear cache</button></div><p id="msg" class="ok"></p>';
  $('#b_kind').value=s.banner_kind;$('#b_secs').value=String(s.banner_secs);
  const lines=v=>v.split('\n').map(x=>x.trim()).filter(Boolean);
  $('#sv').onclick=async()=>{const b={maint_on:$('#m_on').checked,maint_hard:$('#m_hard').checked,maint_msg:$('#m_msg').value,maint_from:ts($('#m_from').value),maint_until:ts($('#m_to').value),
    banner_on:$('#b_on').checked,banner_dismiss:$('#b_dis').checked,banner_kind:$('#b_kind').value,banner_secs:$('#b_secs').value,banner_title:$('#b_title').value,banner_text:$('#b_text').value,banner_link:$('#b_link').value,banner_label:$('#b_label').value,banner_from:ts($('#b_from').value),banner_to:ts($('#b_to').value),
    rate_limit:$('#rl').value,use_embed:$('#emb').checked,allow_post:$('#apost').checked,allow_story:$('#astory').checked,allow_highlight:$('#ahl').checked,blocked_ips:lines($('#bi').value),blocked_codes:lines($('#bc').value)};
    const r=await post('/admin/api/settings',b);$('#msg').textContent=r.ok?'Saved':'Failed';if(r.ok)setTimeout(st,600)};
  ckLoad();
  $('#cc').onclick=async()=>{await post('/admin/api/cache/clear');$('#msg').textContent='Cache cleared'};
}
async function ckLoad(){
  const c=await api('/admin/api/cookies');const el=$('#ckcard');if(!el)return;
  const col={active:'ok',resting:'warn',empty:'k'};
  let h='<h2>Instagram cookies</h2><p class="k">'+c.filled+' of '+c.total+' slots filled (keep at least 1). Each lookup uses the next healthy account; one that is rate limited rests and the next is tried. Saved cookies are never shown again.</p>';
  c.slots.forEach(x=>{
    const info=x.filled?(x.status==='resting'?'resting ~'+x.rest_min+' min':'active')+' | ok '+x.uses+' | failed '+x.fails+(x.err?' | '+esc(x.err):'')+(x.raw?'<br><span class="k">last error: '+esc(x.raw)+'</span>':''):'empty';
    h+='<div style="border-top:1px solid var(--bd,#2a2a35);padding:10px 0"><b>Slot '+x.slot+'</b> <span class="'+col[x.status]+'">'+info+'</span>'+
      '<textarea id="ck'+x.slot+'" rows="2" placeholder="'+(x.filled?'Paste new cookies to replace':'Paste cookies (JSON or Netscape) or just the sessionid')+'"></textarea>'+
      '<div class="row"><button class="pri" data-s="'+x.slot+'" data-a="save">'+(x.filled?'Replace':'Save')+'</button>'+
      (x.filled?'<button data-s="'+x.slot+'" data-a="clear"'+(c.filled<=1?' disabled title="At least 1 slot must stay filled"':'')+'>Remove</button>':'')+'</div></div>';
  });
  h+='<div class="row" style="margin-top:8px"><button id="ckr">Wake all resting slots</button></div><p id="ckm" class="warn"></p>';
  el.innerHTML=h;
  el.querySelectorAll('button[data-s]').forEach(b=>b.onclick=async()=>{
    const n=b.dataset.s,a=b.dataset.a;
    const r=await post('/admin/api/cookies',{slot:n,action:a,value:a==='save'?$('#ck'+n).value:''});
    if(r.ok)ckLoad();else $('#ckm').textContent=r.error||'Failed';});
  $('#ckr').onclick=async()=>{await post('/admin/api/cookies/reset');ckLoad()};
}
async function tl(){
  $('#view').innerHTML='<div class="card"><h2>Test a reel</h2><p class="k">Checks both methods and shows exactly why one fails.</p><input id="tu" placeholder="Paste reel link"><div class="row" style="margin-top:8px"><button class="pri" id="tr">Run test</button></div></div><div class="card" id="to" hidden></div>';
  $('#tr').onclick=async()=>{const o=$('#to');o.hidden=false;o.textContent='Testing...';const r=await post('/admin/api/test',{link:$('#tu').value});
    if(!r.ok){o.innerHTML='<span class="bad">'+esc(r.error)+'</span>';return}
    o.innerHTML='<h2>'+esc(r.kind)+' '+esc(r.code)+'</h2>'+tbl(['Method','Result','Time','Reason'],Object.keys(r.results).map(k=>[k,r.results[k].ok?'OK':'FAIL',r.results[k].ms+' ms',r.results[k].ok?('used: '+(r.results[k].via||k)+' | '+r.results[k].items+' item(s)'):(r.results[k].reason+(r.results[k].raw?' | '+r.results[k].raw:''))]))};
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
