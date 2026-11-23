#!/usr/bin/env python3
"""Daily data refresh for the Pitchside app.

Pulls news (with photos), fixtures, tables and team badges from ESPN's free
public football API and writes them next to index.html:
  data.json         everything the app shows
  photos/<id>.jpg   news photos, resized
  logos/<id>.png    team badges cache (inlined into data.json)
Then republish index.html with data.json and photos/ as files.
"""
import json, os, io, re, sys, base64, datetime, urllib.request, concurrent.futures as cf
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = sys.argv[1] if len(sys.argv) > 1 else HERE
API = "https://site.api.espn.com/apis/site/v2/sports/soccer/"
STAND = "https://site.api.espn.com/apis/v2/sports/soccer/"
LEAGUES = ["eng.1", "eng.2", "eng.fa", "esp.1", "ita.1", "ger.1", "fra.1", "sco.1",
           "ned.1", "por.1", "usa.1", "uefa.champions", "uefa.europa"]
NEWS_PER_LEAGUE = 14
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
            img = (a.get("images") or [{}])[0]
            aid = str(a.get("id"))
            if img.get("url"): photos[aid] = img["url"]
            arts.append({
                "id": aid, "title": a.get("headline"), "desc": a.get("description"),
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
