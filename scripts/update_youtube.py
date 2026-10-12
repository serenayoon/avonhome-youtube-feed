"""AVON HOME — YouTube feed for the homepage CONTENTS section.

Runs hourly on GitHub Actions (free for public repos). Reads the public YouTube RSS feed,
classifies long-form vs Shorts, keeps the latest 3 + 3, and writes data/videos.json.

Safety rules
- RSS error / bad XML / wrong channel / empty feed  -> videos.json is NOT touched.
- A video is classified only when the RSS link and the /shorts/<id> HTTP check agree.
  Unclear videos are skipped (never guessed).
- Upcoming premieres / lives (0 views) are skipped until they have views.
- Kept videos that are no longer in the RSS are re-checked with oEmbed; 404/401/403 -> removed.
- Duplicate ids are merged; an empty result never overwrites existing data.
"""
import json, os, re, sys, time, urllib.error, urllib.request, xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from io import BytesIO

CHANNEL_ID = "UCfeNssjQJWU15LUk74vMkrA"
RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"
PER_TYPE = 3
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
THUMBS = os.path.join(DATA, "thumbs")
VIDEOS = os.path.join(DATA, "videos.json")
STATUS = os.path.join(DATA, "status.json")
SEED = os.path.join(DATA, "seed.json")
EXCLUDE = os.path.join(DATA, "exclude.txt")
KST = timezone(timedelta(hours=9))
UA = "Mozilla/5.0 (compatible; avonhome-youtube-feed/1.0; +https://github.com/serenayoon/avonhome-youtube-feed)"
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
      "media": "http://search.yahoo.com/mrss/"}

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

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None

def http(url, follow=True, timeout=20):
    """Return (status, body_bytes, location). Never raises for HTTP errors."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9",
                                               "Cookie": "CONSENT=YES+1; SOCS=CAI"})
    opener = urllib.request.build_opener() if follow else urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.read(), r.headers.get("Location")
    except urllib.error.HTTPError as e:
        return e.code, b"", e.headers.get("Location")
    except Exception as e:  # network error / timeout
        return 0, str(e).encode(), None

def clean_title(title):
    t = re.sub(r"(?<!\S)#[^\s#]+", "", title)
    t = re.sub(r"\s{2,}", " ", t).strip()
    t = re.sub(r"[\s|｜·,\-]+$", "", t).strip()
    return t or title.replace("#", "").strip()

def fetch_rss():
    if os.environ.get("SIMULATE_RSS_FAILURE", "").lower() == "true":
        return None, "simulated RSS failure (test)"
    last = ""
    for attempt in range(3):
        status, body, _ = http(RSS_URL)
        if status == 200:
            try:
                root = ET.fromstring(body)
            except ET.ParseError as e:
                last = f"RSS XML parse error: {e}"
            else:
                ch = root.findtext("yt:channelId", namespaces=NS)
                entries = root.findall("a:entry", NS)
                # the feed writes the id without the "UC" prefix (e.g. feNss...)
                if (ch or "").removeprefix("UC") != CHANNEL_ID.removeprefix("UC"):
                    last = f"RSS channel mismatch: {ch}"
                elif not entries:
                    last = "RSS returned 0 entries"
                else:
                    return entries, None
        else:
            last = f"RSS HTTP {status}"
        log(f"  attempt {attempt + 1}/3 failed: {last}")
        time.sleep(15 * (attempt + 1))
    return None, last

def shorts_check(vid):
    """'short' if /shorts/<id> serves the Shorts page (200), 'long' if it redirects to /watch."""
    status, _, loc = http(f"https://www.youtube.com/shorts/{vid}", follow=False)
    if status == 200:
        return "short"
    if status in (301, 302, 303, 307, 308) and loc and "/watch" in loc:
        return "long"
    return None

def oembed_public(vid):
    """True = public, False = deleted/private, None = unknown (network)."""
    status, _, _ = http(f"https://www.youtube.com/oembed?format=json&url=https://www.youtube.com/watch?v={vid}")
    if status == 200:
        return True
    if status in (401, 403, 404):
        return False
    return None

def parse_entry(e):
    vid = e.findtext("yt:videoId", namespaces=NS)
    link = e.find("a:link", NS).get("href", "")
    title = e.findtext("a:title", namespaces=NS) or ""
    published = e.findtext("a:published", namespaces=NS) or ""
    stats = e.find("media:group/media:community/media:statistics", NS)
    views = int(stats.get("views", "0")) if stats is not None else None
    return {"id": vid, "link": link, "titleRaw": title, "published": published, "views": views}

def make_thumb(item):
    """Download and shrink the cover to webp. Returns (relative path or None, remote url)."""
    from PIL import Image
    vid, kind = item["id"], item["type"]
    # Shorts: the custom cover is the vertical image centred inside the 16:9 thumbnails
    # (oar2.jpg is an auto frame, not the custom cover -> not used).
    names = ["maxresdefault", "hq720", "sddefault", "hqdefault"] if kind == "short" else ["hq720", "maxresdefault", "hqdefault"]
    for n in names:
        url = f"https://i.ytimg.com/vi/{vid}/{n}.jpg"
        status, body, _ = http(url)
        if status != 200 or len(body) < 2000:
            continue
        try:
            im = Image.open(BytesIO(body)).convert("RGB")
        except Exception:
            continue
        w, h = im.size
        if w / h < 1.5:  # 4:3 thumbnails (sd/hqdefault) carry a 16:9 picture with bars -> take that band
            bh = int(w * 9 / 16); im = im.crop((0, (h - bh) // 2, w, (h - bh) // 2 + bh)); w, h = im.size
        if kind == "short":
            target, size = 9 / 16, (360, 640)
        else:
            target, size = 16 / 9, (640, 360)
        if w / h > target:  # too wide -> crop sides
            nw = int(h * target); im = im.crop(((w - nw) // 2, 0, (w - nw) // 2 + nw, h))
        else:               # too tall -> crop top/bottom
            nh = int(w / target); im = im.crop((0, (h - nh) // 2, w, (h - nh) // 2 + nh))
        im = im.resize(size, Image.LANCZOS)
        os.makedirs(THUMBS, exist_ok=True)
        im.save(os.path.join(THUMBS, f"{vid}.webp"), "WEBP", quality=80, method=6)
        return f"data/thumbs/{vid}.webp", url
    return None, f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"

def build_item(base, kind, origin, stamp):
    vid = base["id"]
    url = f"https://www.youtube.com/shorts/{vid}" if kind == "short" else f"https://www.youtube.com/watch?v={vid}"
    return {"id": vid, "type": kind, "title": clean_title(base["titleRaw"]), "titleRaw": base["titleRaw"],
            "url": url, "published": base["published"], "origin": origin, "verifiedAt": stamp}

def write_output(run, out_lines):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("".join(f"{k}={v}\n" for k, v in out_lines.items()))

def main():
    stamp = now().isoformat()
    prev = load(VIDEOS)
    status = load(STATUS, {})
    exclude = set()
    if os.path.exists(EXCLUDE):
        exclude = {l.strip() for l in open(EXCLUDE, encoding="utf-8") if l.strip() and not l.startswith("#")}

    log(f"[{stamp}] fetching RSS")
    entries, err = fetch_rss()
    pool, rss_ids, skipped = {}, set(), []

    if entries is not None:
        for e in entries:
            it = parse_entry(e)
            vid = it["id"]
            if not vid or vid in exclude:
                continue
            rss_ids.add(vid)
            if it["views"] == 0:
                skipped.append(f"{vid} pending (0 views: premiere/live?)"); continue
            rss_kind = "short" if "/shorts/" in it["link"] else "long"
            http_kind = shorts_check(vid)
            if http_kind != rss_kind:
                skipped.append(f"{vid} unclear (rss={rss_kind}, http={http_kind})"); continue
            pool[vid] = build_item(it, rss_kind, "rss", stamp)
        log(f"  RSS ok: {len(entries)} entries, classified {len(pool)}, skipped {len(skipped)}")
        for s in skipped:
            log("   - skip " + s)

    # Seed only when there is no previous data at all (first run).
    base_items = (prev["long"] + prev["shorts"]) if prev else [
        dict(s, origin="seed") for s in load(SEED, {"items": []})["items"]]
    if not prev:
        log(f"  no previous videos.json -> using {len(base_items)} seed items")

    if entries is None and prev:
        log(f"  RSS FAILED ({err}) -> keeping existing videos.json untouched")
    else:
        # carry over previous/seed items that are not in the current RSS window (re-verify each)
        for old in base_items:
            vid = old["id"]
            if vid in pool or vid in exclude:
                continue
            if vid in rss_ids:      # in RSS but skipped this time -> keep old copy
                pool[vid] = old; continue
            ok = oembed_public(vid)
            if ok is False:
                log(f"   - drop {vid} (oEmbed: deleted/private)"); continue
            kind = shorts_check(vid) or old["type"]
            item = dict(old, type=kind, title=clean_title(old["titleRaw"]),
                        url=(f"https://www.youtube.com/shorts/{vid}" if kind == "short" else f"https://www.youtube.com/watch?v={vid}"))
            if ok:
                item["verifiedAt"] = stamp
            pool[vid] = item

        ordered = sorted(pool.values(), key=lambda x: x["published"], reverse=True)
        long_ = [x for x in ordered if x["type"] == "long"][:PER_TYPE]
        shorts = [x for x in ordered if x["type"] == "short"][:PER_TYPE]

        if not long_ and not shorts:
            log("  result empty -> NOT overwriting existing data")
        else:
            keep = set()
            for it in long_ + shorts:
                rel, remote = make_thumb(it)
                it["thumb"], it["thumbRemote"] = rel or remote, remote
                if rel:
                    keep.add(os.path.basename(rel))
            new = {"schema": 1, "channelId": CHANNEL_ID, "channelUrl": "https://www.youtube.com/@avon_home",
                   "source": "rss" if entries is not None else "seed",
                   "long": long_, "shorts": shorts}
            def strip(d):
                return [{k: v for k, v in x.items() if k != "verifiedAt"} for x in d["long"] + d["shorts"]]
            changed = (prev is None) or strip(prev) != strip(new) or prev.get("source") != new["source"]
            if changed:
                new["generatedAt"] = stamp
                save(VIDEOS, new)
                log(f"  videos.json updated: long {len(long_)}, shorts {len(shorts)}")
                for f in os.listdir(THUMBS) if os.path.isdir(THUMBS) else []:
                    if f.endswith(".webp") and f not in keep:
                        os.remove(os.path.join(THUMBS, f))
                status["lastChangeAt"] = stamp
            else:
                log("  no change in the 3+3 selection")

    # status.json: written on data change, on error-state change, or once per KST day (heartbeat)
    ok = entries is not None
    old_state = (status.get("rssOk"), status.get("lastError"))
    status.update({"rssOk": ok, "lastError": None if ok else err, "lastCheckedAt": stamp})
    if ok:
        status["lastSuccessAt"] = stamp; status["consecutiveFailures"] = 0; status["rssEntries"] = len(entries)
    else:
        status["lastErrorAt"] = stamp; status["consecutiveFailures"] = status.get("consecutiveFailures", 0) + 1
    today = now().date().isoformat()
    if (not ok or old_state != (status["rssOk"], status["lastError"]) or status.get("heartbeat") != today
            or status.get("lastChangeAt") == stamp):
        status["heartbeat"] = today
        save(STATUS, status)
        log("  status.json written")
    write_output(None, {"rss_ok": "true" if ok else "false"})
    if not ok:
        log(f"::warning::RSS unavailable: {err} — "
            + ("existing data kept" if prev else "first run built from seed data"))
    return 0

if __name__ == "__main__":
    sys.exit(main())
