#!/usr/bin/env python3
"""Daily data refresh for the Pitchside app.

Pulls news (with photos), fixtures, tables and team badges from ESPN's free
public football API and writes them next to index.html:
  data.json         everything the app shows
  photos/<id>.jpg   news photos, resized
  logos/<id>.png    team badges cache (inlined into data.json)
Then republish index.html with data.json and photos/ as files.
"""
import json, os, io, re, sys, html, base64, hashlib, datetime, email.utils, xml.etree.ElementTree as ET, urllib.request, concurrent.futures as cf
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = sys.argv[1] if len(sys.argv) > 1 else HERE
API = "https://site.api.espn.com/apis/site/v2/sports/soccer/"
STAND = "https://site.api.espn.com/apis/v2/sports/soccer/"
LEAGUES = ["eng.1", "eng.2", "eng.fa", "esp.1", "ita.1", "ger.1", "fra.1", "sco.1",
           "ned.1", "por.1", "usa.1", "uefa.champions", "uefa.europa"]
NEWS_PER_LEAGUE = 14
# Extra news sources (public RSS feeds): headline, summary, photo and a link to the full story.
FEEDS = [
    ("BBC Sport", "https://feeds.bbci.co.uk/sport/football/rss.xml"),
    ("The Guardian", "https://www.theguardian.com/football/rss"),
    ("Sky Sports", "https://www.skysports.com/rss/12040"),
    ("talkSPORT", "https://talksport.com/football/feed/"),
]
FEED_ITEMS = 15
MEDIA = "{http://search.yahoo.com/mrss/}"
DAYS_BACK, DAYS_AHEAD = 7, 7

def get(url, raw=False):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; app-data-refresh)"})
    with urllib.request.urlopen(req, timeout=25) as r:
        b = r.read()
    return b if raw else json.loads(b)

def safe(f, *a):
    try: return f(*a)
    except Exception as e:
        print("skip", a[0] if a else "", e, file=sys.stderr); return None

def topic_of(t):
    t = t.lower()
    if re.search(r"\bfans?\b|supporters|crowd|ultras|tifo|chant|booed|banner", t): return "fans"
    if re.search(r"transfer|signs|signing|\bbid\b|loan|contract|deal", t): return "transfers"
    if re.search(r"viral|incredible|bizarre|wonder ?goal|stunning|record|video", t): return "viral"
    return "matches"

def clean(t):
    t = re.sub(r"<[^>]+>", " ", html.unescape(t or ""))
    return re.sub(r"\s+", " ", t).strip()

def summary(raw, limit=700):
    """Feed summary as whole sentences: list items and paragraphs become sentences,
    'Continue reading' links are dropped, and a cut-off last sentence is removed."""
    t = html.unescape(raw or "")
    cut = "continue reading" in t.lower()   # the feed stopped mid-paragraph
    t = re.sub(r"(?i)<a[^>]*>\s*continue reading.*?</a>", "", t)
    t = re.sub(r"(?i)</(li|p|h\d)>|<br\s*/?>", ". ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"(?i)\s*continue reading\.*\s*$", "", t)
    if re.search(r"(…|\.\.\.)\s*$", t): cut = True; t = re.sub(r"(…|\.\.\.)\s*$", ".", t)
    t = re.sub(r"\s*\.\s*\.(\s*\.)*", ".", t)          # tidy doubled full stops
    t = re.sub(r"([!?])\.", r"\1", t)
    t = re.sub(r"\s+([.!?])", r"\1", t)
    sents = re.findall(r"[^.!?]+[.!?]+[\"'’”]?", t)
    if cut and len(sents) > 1: sents = sents[:-1]
    out = ""
    for snt in sents:
        if len(out) + len(snt) > limit and out: break
        out += snt
    out = out.strip()
    if not out:
        out = t if len(t) <= limit else ""
    return out

JUNK = r"(?i)cookie|subscribe|sign up|newsletter|copyright|all rights reserved|javascript|advert|try again later|unavailable|sky bet|download the|app store|follow us|click here|terms and conditions|gamble"

def espn_paragraph(api, have=""):
    """ESPN stories: take the opening paragraphs from ESPN's story feed."""
    j = get(api)
    story = ((j.get("headlines") or [{}])[0]).get("story") or ""
    paras = [clean(p) for p in re.findall(r"(?is)<p[^>]*>(.*?)</p>", story)]
    paras = [p for p in paras if len(p.split()) >= 8 and not re.search(JUNK, p)]
    out = [have.strip()] if have.strip() else []
    for p in paras:
        if not any(p.lower()[:60] in o.lower() for o in out): out.append(p)
    return summary(" ".join(f"<p>{html.escape(x)}</p>" for x in out), 650)

def page_paragraph(url, have=""):
    """For stories whose feed gives little or no summary: read the story page and take
    its description plus the opening paragraphs, as whole sentences."""
    page = get(url, raw=True).decode("utf-8", "replace")
    parts = []
    m = re.search(r'<meta[^>]+(?:property|name)="(?:og:description|description)"[^>]+content="([^"]+)"', page) or \
        re.search(r'<meta[^>]+content="([^"]+)"[^>]+(?:property|name)="(?:og:description|description)"', page)
    if m: parts.append(html.unescape(m.group(1)))
    body = re.sub(r"(?is)<(script|style|nav|header|footer|figure|aside)[^>]*>.*?</\1>", " ", page)
    for para in re.findall(r"(?is)<p[^>]*>(.*?)</p>", body):
        t = clean(para)
        if len(t.split()) >= 10 and re.search(r"[.!?\"'’”]$", t) and not re.search(JUNK, t):
            parts.append(t)
        if sum(len(x) for x in parts) > 900: break
    seen, out = set(), []
    for x in [have] + parts:
        x = x.strip()
        if x and x.lower() not in seen and not any(x.lower() in y.lower() for y in out):
            seen.add(x.lower()); out.append(x if re.search(r"[.!?\"'’”]$", x) else x + ".")
    return summary(" ".join(f"<p>{html.escape(x)}</p>" for x in out), 650)

def feed_image(it):
    cands = []
    for tag in (MEDIA + "content", MEDIA + "thumbnail"):
        for m in it.iter(tag):
            if m.get("url") and m.get("medium") in (None, "image"):
                cands.append((int(m.get("width") or 0), m.get("url")))
    for e in it.findall("enclosure"):
        if (e.get("type") or "").startswith("image") and e.get("url"): cands.append((0, e.get("url")))
    best = None
    if cands:
        ok = [c for c in cands if c[0] <= 1200]
        best = max(ok)[1] if ok else min(cands)[1]
    else:
        m = re.search(r'<img[^>]+src="([^"]+)"', (it.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or "") + (it.findtext("description") or ""))
        if m: best = html.unescape(m.group(1))
    if best and "ichef.bbci.co.uk" in best: best = re.sub(r"/(\d{2,3})/", "/976/", best, count=1)
    return best

def read_feed(source, url):
    root = ET.fromstring(get(url, raw=True))
    out = []
    for it in root.iter("item"):
        link = (it.findtext("link") or "").strip()
        title = clean(it.findtext("title"))
        if not link.startswith("https://") or not title: continue
        # football only: some feeds mix in NFL, tennis, boxing, etc. Skip betting tips too.
        if "/football/" not in link or re.search(r"\b(odds|tips|bets?|betting)\b", title, re.I): continue
        try: when = email.utils.parsedate_to_datetime(it.findtext("pubDate")).astimezone(datetime.timezone.utc)
        except Exception: continue
        aid = "r" + hashlib.sha1(link.encode()).hexdigest()[:12]
        desc = summary(it.findtext("description"))
        out.append({"id": aid, "title": title, "desc": desc, "source": source,
                    "time": when.isoformat().replace("+00:00", "Z"), "by": clean(it.findtext("{http://purl.org/dc/elements/1.1/}creator")) or None,
                    "imgsrc": feed_image(it), "link": link, "topic": topic_of(title + " " + desc)})
        if len(out) >= FEED_ITEMS: break
    return out

def save_img(url, path, size, fmt):
    if os.path.exists(path): return True
    b = get(url, raw=True)
    im = Image.open(io.BytesIO(b))
    im = im.convert("RGB" if fmt == "JPEG" else "RGBA")
    im.thumbnail(size, Image.LANCZOS)
    if fmt == "JPEG": im.save(path, "JPEG", quality=72, optimize=True, progressive=True)
    else: im.quantize(colors=96, method=Image.FASTOCTREE).save(path, "PNG", optimize=True)
    return True

def main():
    os.makedirs(f"{OUT}/photos", exist_ok=True); os.makedirs(f"{OUT}/logos", exist_ok=True)
    today = datetime.date.today()
    days = [(today + datetime.timedelta(d)).isoformat() for d in range(-DAYS_BACK, DAYS_AHEAD + 1)]
    data = {"updated": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "news": {}, "fixtures": {}, "tables": {}, "teams": {}}
    logos = {}   # team id -> source url
    photos = {}  # article id -> source url

    def league_job(lg):
        news = safe(get, f"{API}{lg}/news?limit={NEWS_PER_LEAGUE}") or {}
        teams = safe(get, f"{API}{lg}/teams") or {}
        table = safe(get, f"{STAND}{lg}/standings") or {}
        fx = {d: safe(get, f"{API}{lg}/scoreboard?dates={d.replace('-', '')}") or {} for d in days}
        return lg, news, teams, table, fx

    with cf.ThreadPoolExecutor(8) as ex:
        results = list(ex.map(league_job, LEAGUES))

    for lg, news, teams, table, fx in results:
        arts = []
        for a in news.get("articles", []):
            if a.get("type") == "Media": continue
            if re.search(r"what to watch|how to watch|where to watch", a.get("headline") or "", re.I): continue
            img = (a.get("images") or [{}])[0]
            aid = str(a.get("id"))
            if img.get("url"): photos[aid] = img["url"]
            arts.append({
                "id": aid, "source": "ESPN", "title": a.get("headline"), "desc": a.get("description"),
                "api": (((a.get("links") or {}).get("api") or {}).get("self") or {}).get("href"),
                "time": a.get("published"), "by": a.get("byline"),
                "img": f"photos/{aid}.jpg" if img.get("url") else None,
                "credit": re.sub(r"^\((.*)\)$", r"\1", (img.get("credit") or "").strip()) or None, "caption": img.get("caption") or img.get("alt"),
                "link": ((a.get("links") or {}).get("web") or {}).get("href"),
                "teams": [{"id": str(c.get("teamId")), "name": c.get("description")}
                          for c in a.get("categories", []) if c.get("type") == "team"],
                "topic": topic_of((a.get("headline") or "") + " " + (a.get("description") or "")),
            })
        data["news"][lg] = arts
        tl = []
        try:
            for x in teams["sports"][0]["leagues"][0]["teams"]:
                t = x["team"]; tid = str(t["id"])
                if t.get("logos"): logos[tid] = t["logos"][0]["href"]
                tl.append({"id": tid, "name": t.get("displayName"), "abbr": t.get("abbreviation")})
        except Exception: pass
        data["teams"][lg] = sorted(tl, key=lambda t: t["name"] or "")
        try:
            ents = table["children"][0]["standings"]["entries"]
            rank = lambda e: next((s["value"] for s in e.get("stats", []) if s.get("name") == "rank"), 99)
            data["tables"][lg] = [str(e["team"]["id"]) for e in sorted(ents, key=rank)]
        except Exception: data["tables"][lg] = []
        data["fixtures"][lg] = {}
        for d, j in fx.items():
            ms = []
            for ev in j.get("events", []):
                c = ev["competitions"][0]; st = (c.get("status") or ev.get("status"))["type"]
                def side(h):
                    x = next((t for t in c["competitors"] if t.get("homeAway") == h), {}); t = x.get("team", {})
                    tid = str(t.get("id"))
                    if t.get("logo"): logos.setdefault(tid, t["logo"])
                    return {"id": tid, "name": t.get("displayName") or t.get("shortDisplayName"),
                            "abbr": t.get("abbreviation"), "score": x.get("score")}
                ms.append({"time": ev.get("date"), "home": side("home"), "away": side("away"),
                           "state": st.get("state"), "detail": st.get("shortDetail"),
                           "venue": (c.get("venue") or {}).get("fullName")})
            if ms: data["fixtures"][lg][d] = ms

    # extra sources
    all_teams = {t["name"]: t["id"] for tl in data["teams"].values() for t in tl if t.get("name")}
    def teams_in(text):
        return [{"id": i, "name": n} for n, i in all_teams.items() if re.search(r"\b" + re.escape(n) + r"\b", text)][:3]
    with cf.ThreadPoolExecutor(4) as ex:
        feeds = list(ex.map(lambda f: (f[0], safe(read_feed, f[0], f[1]) or []), FEEDS))
    extra = []
    for source, items in feeds:
        for a in items:
            src = a.pop("imgsrc")
            if src: photos[a["id"]] = src
            a["img"] = f"photos/{a['id']}.jpg" if src else None
            a["teams"] = teams_in(a["title"] + " " + a["desc"])
            extra.append(a)
        print(f"{source}: {len(items)} stories")
    data["news"]["more"] = extra

    # give every story a proper paragraph
    short = [a for arts in data["news"].values() for a in arts if len(a.get("desc") or "") < 200 and (a.get("api") or (a.get("link") or "").startswith("https://"))]
    def fill(a):
        api = a.pop("api", None)
        more = safe(espn_paragraph, api, a.get("desc") or "") if api else safe(page_paragraph, a["link"], a.get("desc") or "")
        if more and len(more) > len(a.get("desc") or ""): a["desc"] = more
    with cf.ThreadPoolExecutor(8) as ex:
        list(ex.map(fill, short))
    for arts in data["news"].values():
        for a in arts: a.pop("api", None)
    print(f"filled {sum(1 for a in short if len(a.get('desc') or '') >= 200)} of {len(short)} short summaries")

    with cf.ThreadPoolExecutor(12) as ex:
        list(ex.map(lambda kv: safe(save_img, kv[1], f"{OUT}/photos/{kv[0]}.jpg", (720, 420), "JPEG"), photos.items()))
        list(ex.map(lambda kv: safe(save_img, kv[1], f"{OUT}/logos/{kv[0]}.png", (64, 64), "PNG"), logos.items()))
    # only point at images that downloaded
    for arts in data["news"].values():
        for a in arts:
            if a["img"] and not os.path.exists(f"{OUT}/{a['img']}"): a["img"] = None
    # badges travel inside data.json (as small inline images) so a publish stays under the file limit
    data["logos"] = {t: "data:image/png;base64," + base64.b64encode(open(f"{OUT}/logos/{t}.png", "rb").read()).decode()
                     for t in sorted(logos) if os.path.exists(f"{OUT}/logos/{t}.png")}
    # drop old photos no longer used
    used = {os.path.basename(a["img"]) for arts in data["news"].values() for a in arts if a["img"]}
    for f in os.listdir(f"{OUT}/photos"):
        if f not in used: os.remove(f"{OUT}/photos/{f}")
    json.dump(data, open(f"{OUT}/data.json", "w"), separators=(",", ":"))
    n = sum(len(v) for v in data["news"].values())
    g = sum(len(m) for v in data["fixtures"].values() for m in v.values())
    print(f"news {n}, photos {len(used)}, games {g}, logos {len(data['logos'])}")

if __name__ == "__main__":
    main()
