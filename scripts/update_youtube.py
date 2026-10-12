"""AVON HOME — YouTube feed for the homepage CONTENTS section.

Runs on GitHub Actions (public repo, free). Writes data/videos.json = latest 3 long-form + 3 Shorts.

Sources (in order)
1. YouTube Data API v3 (official). Key from the YT_API_KEY GitHub Secret only (env var),
   sent in the X-goog-api-key header; never printed, logged or written.
   - long-form list  = playlist UULF<channel>, Shorts list = playlist UUSH<channel>
     (YouTube's per-type upload playlists; not formally documented → guarded below)
   - videos.list checks: privacyStatus=public, uploadStatus=processed, liveBroadcastContent=none
2. YouTube RSS (fallback, only if the API step fails). Type taken from the RSS link (/shorts/ vs /watch).
No YouTube web pages are scraped.

Safety rules — the existing videos.json is kept unless a source returns a complete, valid result
- any API call error, an id found in BOTH lists, or fewer than 3 valid videos of a type -> API step fails
- RSS error / bad XML / wrong channel / empty feed -> RSS step fails
- both fail -> videos.json untouched (status.json records why)
- duplicates removed; excluded ids (data/exclude.txt) skipped; empty result never written
"""
import json, os, re, sys, time, urllib.error, urllib.parse, urllib.request, xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from io import BytesIO

CHANNEL_ID = "UCfeNssjQJWU15LUk74vMkrA"
TAIL = CHANNEL_ID[2:]
PLAYLIST_LONG, PLAYLIST_SHORT = "UULF" + TAIL, "UUSH" + TAIL
RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"
API = "https://www.googleapis.com/youtube/v3/"
PER_TYPE = 3
SCAN = 10  # newest items read per list (1 API unit per list regardless)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
THUMBS = os.path.join(DATA, "thumbs")
VIDEOS = os.path.join(DATA, "videos.json")
STATUS = os.path.join(DATA, "status.json")
SEED = os.path.join(DATA, "seed.json")
EXCLUDE = os.path.join(DATA, "exclude.txt")
KST = timezone(timedelta(hours=9))
UA = "Mozilla/5.0 (compatible; avonhome-youtube-feed/2.0; +https://github.com/serenayoon/avonhome-youtube-feed)"
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
      "media": "http://search.yahoo.com/mrss/"}
ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

def now():
    return datetime.now(KST).replace(microsecond=0)

def log(msg):
    print(msg, flush=True)

def load(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default

def save(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)

def http(url, headers=None, timeout=20):
    """(status, body). Never raises; never logs the request."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read()
        except Exception:
            return e.code, b""
    except Exception as e:
        return 0, type(e).__name__.encode()

def clean_title(title):
    t = re.sub(r"(?<!\S)#[^\s#]+", "", title)
    t = re.sub(r"\s{2,}", " ", t).strip()
    t = re.sub(r"[\s|｜·,\-]+$", "", t).strip()
    return t or title.replace("#", "").strip()

def video_url(vid, kind):
    return f"https://www.youtube.com/shorts/{vid}" if kind == "short" else f"https://www.youtube.com/watch?v={vid}"

# ---------------------------------------------------------------- 1. YouTube Data API
class ApiError(Exception):
    pass

API_CALLS = 0

def api(key, endpoint, **params):
    global API_CALLS
    API_CALLS += 1
    status, body = http(API + endpoint + "?" + urllib.parse.urlencode(params),
                        headers={"X-goog-api-key": key, "Accept": "application/json"})
    if status != 200:
        reason = ""
        try:
            err = json.loads(body).get("error", {})
            reason = ",".join(x.get("reason", "") for x in err.get("errors", [])) or err.get("status", "")
        except Exception:
            pass
        raise ApiError(f"{endpoint} HTTP {status} {reason}".strip())
    try:
        return json.loads(body)
    except Exception:
        raise ApiError(f"{endpoint} bad JSON")

def collect_api(exclude):
    key = os.environ.get("YT_API_KEY", "").strip()
    if not key:
        raise ApiError("YT_API_KEY not set")
    lists = {}
    for kind, pid in (("long", PLAYLIST_LONG), ("short", PLAYLIST_SHORT)):
        data = api(key, "playlistItems", part="contentDetails", playlistId=pid, maxResults=SCAN)
        ids = [it.get("contentDetails", {}).get("videoId", "") for it in data.get("items", [])]
        ids = [v for v in dict.fromkeys(ids) if ID_RE.match(v)]
        if not ids:
            raise ApiError(f"{kind} list empty")
        lists[kind] = ids
    both = set(lists["long"]) & set(lists["short"])
    if both:
        raise ApiError(f"classification conflict: {len(both)} id(s) in both lists")
    cand = [v for v in lists["long"] + lists["short"] if v not in exclude]
    data = api(key, "videos", part="snippet,status", id=",".join(cand), maxResults=50)
    info = {v["id"]: v for v in data.get("items", [])}
    out = {}
    for kind in ("long", "short"):
        picked = []
        for vid in lists[kind]:
            v = info.get(vid)
            if vid in exclude or not v:
                continue
            sn, st = v.get("snippet", {}), v.get("status", {})
            if st.get("privacyStatus") != "public" or st.get("uploadStatus") != "processed" \
                    or sn.get("liveBroadcastContent", "none") != "none" or not sn.get("title"):
                log(f"   - skip {vid} (not public/processed or live/upcoming)")
                continue
            picked.append({"id": vid, "type": kind, "title": clean_title(sn["title"]), "titleRaw": sn["title"],
                           "url": video_url(vid, kind), "published": sn.get("publishedAt", ""), "origin": "api"})
        picked.sort(key=lambda x: x["published"], reverse=True)
        if len(picked) < PER_TYPE:
            raise ApiError(f"only {len(picked)} valid {kind} video(s)")
        out[kind] = picked[:PER_TYPE]
    key = None
    return out["long"], out["short"]

# ---------------------------------------------------------------- 2. RSS (fallback)
def collect_rss(exclude):
    if os.environ.get("SIMULATE_RSS_FAILURE", "").lower() == "true":
        raise ApiError("simulated RSS failure (test)")
    status, body = http(RSS_URL)
    if status != 200:
        raise ApiError(f"RSS HTTP {status}")
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise ApiError("RSS XML parse error")
    if (root.findtext("yt:channelId", namespaces=NS) or "").removeprefix("UC") != TAIL:
        raise ApiError("RSS channel mismatch")
    items = {"long": [], "short": []}
    for e in root.findall("a:entry", NS):
        vid = e.findtext("yt:videoId", namespaces=NS) or ""
        if not ID_RE.match(vid) or vid in exclude:
            continue
        stats = e.find("media:group/media:community/media:statistics", NS)
        if stats is not None and stats.get("views") == "0":
            continue  # premiere / live not started
        link = e.find("a:link", NS).get("href", "")
        kind = "short" if "/shorts/" in link else "long"
        title = e.findtext("a:title", namespaces=NS) or ""
        if vid not in {x["id"] for x in items[kind]}:
            items[kind].append({"id": vid, "type": kind, "title": clean_title(title), "titleRaw": title,
                                "url": video_url(vid, kind), "published": e.findtext("a:published", namespaces=NS) or "",
                                "origin": "rss"})
    for kind in items:
        items[kind].sort(key=lambda x: x["published"], reverse=True)
    if len(items["long"]) < PER_TYPE or len(items["short"]) < PER_TYPE:
        raise ApiError(f"RSS incomplete (long {len(items['long'])}, shorts {len(items['short'])})")
    return items["long"][:PER_TYPE], items["short"][:PER_TYPE]

# ---------------------------------------------------------------- thumbnails (unchanged method)
def make_thumb(item):
    from PIL import Image
    vid, kind = item["id"], item["type"]
    names = ["maxresdefault", "hq720", "sddefault", "hqdefault"] if kind == "short" else ["hq720", "maxresdefault", "hqdefault"]
    for n in names:
        url = f"https://i.ytimg.com/vi/{vid}/{n}.jpg"
        status, body = http(url)
        if status != 200 or len(body) < 2000:
            continue
        try:
            im = Image.open(BytesIO(body)).convert("RGB")
        except Exception:
            continue
        w, h = im.size
        if w / h < 1.5:
            bh = int(w * 9 / 16); im = im.crop((0, (h - bh) // 2, w, (h - bh) // 2 + bh)); w, h = im.size
        target, size = (9 / 16, (360, 640)) if kind == "short" else (16 / 9, (640, 360))
        if w / h > target:
            nw = int(h * target); im = im.crop(((w - nw) // 2, 0, (w - nw) // 2 + nw, h))
        else:
            nh = int(w / target); im = im.crop((0, (h - nh) // 2, w, (h - nh) // 2 + nh))
        im = im.resize(size, Image.LANCZOS)
        os.makedirs(THUMBS, exist_ok=True)
        im.save(os.path.join(THUMBS, f"{vid}.webp"), "WEBP", quality=80, method=6)
        return f"data/thumbs/{vid}.webp", url
    return None, f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"

# ---------------------------------------------------------------- main
def write_output(values):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("".join(f"{k}={v}\n" for k, v in values.items()))

def main():
    stamp = now().isoformat()
    prev = load(VIDEOS)
    status = load(STATUS, {})
    exclude = set()
    if os.path.exists(EXCLUDE):
        exclude = {l.strip() for l in open(EXCLUDE, encoding="utf-8") if l.strip() and not l.startswith("#")}

    result, source = None, None
    api_state = {"at": stamp}
    log(f"[{stamp}] 1) YouTube Data API")
    try:
        result = collect_api(exclude); source = "api"
        api_state.update(ok=True, error=None, calls=API_CALLS)
        log(f"  API ok ({API_CALLS} calls ≈ {API_CALLS} quota units)")
    except ApiError as e:
        api_state.update(ok=False, error=str(e), calls=API_CALLS)
        log(f"  API failed: {e} ({API_CALLS} calls)")

    rss_state = {"at": stamp, "attempted": result is None}
    if result is None:
        log("  2) RSS fallback")
        try:
            result = collect_rss(exclude); source = "rss"
            rss_state.update(ok=True, error=None)
            log("  RSS ok")
        except ApiError as e:
            rss_state.update(ok=False, error=str(e))
            log(f"  RSS failed: {e}")
    else:
        rss_state.update(ok=None, error=None)

    changed = False
    if result is None:
        log("  no valid source -> existing videos.json kept untouched" if prev else "  no valid source and no data yet")
        if not prev and os.path.exists(SEED):  # first run only: verified seed data
            seed = load(SEED, {"items": []})["items"]
            lg = [dict(s, title=clean_title(s["titleRaw"]), url=video_url(s["id"], s["type"]), origin="seed")
                  for s in seed if s["type"] == "long"][:PER_TYPE]
            sh = [dict(s, title=clean_title(s["titleRaw"]), url=video_url(s["id"], s["type"]), origin="seed")
                  for s in seed if s["type"] == "short"][:PER_TYPE]
            if lg and sh:
                result, source = (lg, sh), "seed"
    if result is not None:
        long_, shorts = result
        keep = set()
        for it in long_ + shorts:
            rel, remote = make_thumb(it)
            it["thumb"], it["thumbRemote"] = rel or remote, remote
            if rel:
                keep.add(os.path.basename(rel))
        new = {"schema": 1, "channelId": CHANNEL_ID, "channelUrl": "https://www.youtube.com/@avon_home",
               "source": source, "long": long_, "shorts": shorts}
        sig = lambda d: [(x["id"], x["title"], x["url"], x.get("origin")) for x in d["long"] + d["shorts"]]
        if prev is None or sig(prev) != sig(new) or prev.get("source") != source:
            new["generatedAt"] = stamp
            save(VIDEOS, new); changed = True
            log(f"  videos.json updated from {source}: " + ", ".join(x["id"] for x in long_ + shorts))
            for f in os.listdir(THUMBS) if os.path.isdir(THUMBS) else []:
                if f.endswith(".webp") and f not in keep:
                    os.remove(os.path.join(THUMBS, f))
        else:
            log(f"  no change ({source})")

    fresh = source in ("api", "rss")
    old_key = (status.get("dataOk"), (status.get("api") or {}).get("ok"), (status.get("rss") or {}).get("ok"))
    status.update({"dataOk": fresh, "lastSource": source if fresh else "kept", "lastCheckedAt": stamp,
                   "api": api_state, "rss": rss_state})
    if fresh:
        status["lastSuccessAt"] = stamp; status["consecutiveFailures"] = 0
    else:
        status["lastErrorAt"] = stamp; status["consecutiveFailures"] = status.get("consecutiveFailures", 0) + 1
    if changed:
        status["lastChangeAt"] = stamp
    for legacy in ("rssOk", "lastError", "rssEntries"):
        status.pop(legacy, None)
    today = now().date().isoformat()
    new_key = (status["dataOk"], api_state.get("ok"), rss_state.get("ok"))
    if changed or not fresh or old_key != new_key or status.get("heartbeat") != today:
        status["heartbeat"] = today
        save(STATUS, status)
        log("  status.json written")
    # 'rss_ok' kept as the output name used by the workflow: true = fresh data from API or RSS
    write_output({"rss_ok": "true" if fresh else "false", "source": source or "none"})
    if not fresh:
        log("::warning::No fresh data (API and RSS failed) — existing videos.json kept")
    return 0

if __name__ == "__main__":
    sys.exit(main())
