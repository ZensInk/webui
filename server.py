#!/usr/bin/env python3
"""ZensInk WebUI adapter · serves the frontend and a JSON API over the
real zens_ink package. Pure stdlib, matching the package philosophy.

Run:    python3 server.py          → http://127.0.0.1:8390
Needs:  the zens_ink package anywhere discoverable — a sibling zens-ink* clone
        (auto-found), a pip install, or ZENSINK_REPO=/path override. Optional
        pro package: sibling zens-ink-pro* (auto-found) or ZENSINK_PRO=/path.

Endpoints (POST JSON unless noted):
  GET  /api/status   engine + key availability
  POST /api/research {seed, zh, expand}            → autocomplete + intent
  POST /api/kd       {keyword, zh}                 → SERP-based difficulty
  POST /api/volume   {keyword, country, zh}        → Bing volume
  POST /api/cluster  {keywords[]}                  → semantic clusters
  POST /api/gap      {urls[]}                      → sitemap topic diff
  POST /api/audit    {dist, sitemap}               → site_audit JSON
  POST /api/geo      {url}                         → AI crawler audit
  POST /api/rank     {keywords[], domain, zh}      → positions + history
"""
import json
import os
import sys
import time
import datetime
import tempfile
import urllib.request
import urllib.error
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = Path(__file__).resolve().parent


def _find_repo():
    """Locate the free zens_ink package: env override → sibling discovery → None (pip)."""
    env = os.environ.get("ZENSINK_REPO")
    if env:
        p = Path(env).resolve()
        return p if (p / "zens_ink" / "__init__.py").exists() else None
    for c in sorted(HERE.parent.glob("zens-ink*")):
        if "pro" in c.name.lower() or c.name == HERE.name:
            continue
        if (c / "zens_ink" / "__init__.py").exists():
            return c.resolve()
    return None


def _find_pro():
    """Locate zens_ink_pro (any version): env override → sibling discovery."""
    env = os.environ.get("ZENSINK_PRO")
    if env:
        p = Path(env).resolve()
        return p if (p / "zens_ink_pro" / "__init__.py").exists() else None
    for c in sorted(HERE.parent.glob("zens-ink-pro*")):  # version-agnostic
        for sub in (c / "scripts", c):
            if (sub / "zens_ink_pro" / "__init__.py").exists():
                return sub.resolve()
    return None


REPO = _find_repo()
PRO_DIR = _find_pro()
# order matters: pro bundles a vendored zens_ink copy — the standalone repo
# clone (canonical, user-maintained) must win. Insert pro first, repo last.
if PRO_DIR:
    sys.path.insert(0, str(PRO_DIR))
if REPO:
    sys.path.insert(0, str(REPO))

try:
    import zens_ink  # noqa: E402
    from zens_ink import config, rank_tracker as rt  # noqa: E402
    from zens_ink.keyword_research import research as kr_research  # noqa: E402
    from zens_ink.search_intent import classify_batch, intent_summary  # noqa: E402
    from zens_ink.keyword_volume import get_stats as vol_stats  # noqa: E402
    from zens_ink.kd import fetch_serp, fetch_search_volume, calculate_kd  # noqa: E402
    from zens_ink.keyword_cluster import cluster as kw_cluster  # noqa: E402
    from zens_ink.competitor_gap import fetch_sitemap, extract_topics  # noqa: E402
    from zens_ink.site_audit import run as site_audit_run  # noqa: E402
    from zens_ink.ai_crawler_audit import audit as ai_audit  # noqa: E402
    from zens_ink.kgr_auto import score_keyword as kgr_score  # noqa: E402
    from zens_ink.domain_rating import get_domain_rating  # noqa: E402
    from zens_ink.serp_intent import analyze as serp_intent_analyze  # noqa: E402
    from zens_ink.onpage_audit import run as onpage_run  # noqa: E402
    from zens_ink.geo_fanout import generate_fanout  # noqa: E402
    from zens_ink.reddit_blueocean import discover as reddit_discover  # noqa: E402
    from zens_ink.content_qc import check_draft as qc_check  # noqa: E402
    from zens_ink.content_matrix import generate_matrix  # noqa: E402
    from zens_ink.llms_gen import (MetaParser, SKIP_DIRS,  # noqa: E402
                                    build_llms_txt)  # noqa: E402
    from zens_ink import search_performance as gsc  # noqa: E402
    from zens_ink.brave_volume import search_brave, estimate_demand as brave_demand  # noqa: E402
    ENGINE = True
except Exception as e:  # engine missing → API 503, static demo still works
    ENGINE = False
    ENGINE_ERR = str(e)

# ── Pro package (optional) ───────────────────────────────────────────────────
try:
    import zens_ink_pro as pro_pkg  # noqa: E402
    from zens_ink_pro import content_briefs as pro_briefs  # noqa: E402
    PRO = True
    PRO_VERSION = getattr(pro_pkg, "__version__", "?")
except Exception as e:
    PRO = False
    PRO_VERSION = None
    PRO_ERR = str(e)


VERSION = getattr(zens_ink, "__version__", "?") if ENGINE else "unavailable"
WEBUI_VERSION = "0.1.0"
MAX_TABLE = 60          # keywords returned to the table
MAX_HARVEST = 200       # keywords kept for clustering


# ── helpers ──────────────────────────────────────────────────────────────────

def _norm_sitemap(u: str) -> str:
    u = u.strip()
    if not u:
        return u
    if not u.startswith("http"):
        u = "https://" + u.lstrip("/")
    if "sitemap" not in u.split("?")[0].rsplit(".", 1)[-1]:
        pass  # user may pass a bare domain; competitor_gap handles sitemap.xml discovery
    return u


def api_research(body):
    seed = (body.get("seed") or "tarot").strip()
    zh = bool(body.get("zh"))
    expand = bool(body.get("expand"))
    lang = "zh" if zh else "en"
    t0 = time.time()

    # reddit blue-ocean source: keywords people search WITH reddit intent
    if body.get("source") == "reddit":
        log = [f'$ zens-ink reddit_blueocean "{seed}"']
        r = reddit_discover(seed, lang=lang)
        gd = r.get("google_demand", {})
        rows = [{"kw": q["query"], "intent": "commercial" if q.get("has_commercial_intent") else "informational",
                 "confidence": min(.9, .5 + q.get("blue_ocean_score", 0) * .15),
                 "content_type": "Reddit-targeted guide"}
                for q in gd.get("reddit_demand_queries", [])[:MAX_TABLE]]
        harvest = [q["query"] for q in gd.get("reddit_demand_queries", [])][:MAX_HARVEST]
        log.append(f"→ google autocomplete (reddit-intent): {gd.get('total_queries_found', 0)} queries")
        log.append(f"→ blue ocean: {r.get('blue_ocean_count', 0)} low-competition candidates")
        if r.get("total_posts"):
            log.append(f"→ reddit api: {r['total_posts']} posts scored")
        dt = time.time() - t0
        log.append(f"✓ done in {dt:.1f}s · showing {len(rows)} of {gd.get('total_queries_found', 0)}")
        return {"ok": True, "log": log, "keywords": rows, "harvest": harvest,
                "total": gd.get("total_queries_found", len(rows))}

    log = [f"$ zens-ink keyword_research \"{seed}\"{' --expand' if expand else ''}"
           + (" --zh" if zh else "")]

    r = kr_research(seed, lang=lang, expand=expand)
    kws = list(r["suggestions"])
    log.append(f"→ google autocomplete: \"{seed}\" … {r['suggestion_count']} suggestions"
               f" ({r['activity_level']})")
    if expand and r.get("long_tail"):
        kws += [k for k in r["long_tail"] if k not in set(kws)]
        log.append(f"→ a–z expansion: 26 prefixes … {len(r['long_tail'])} long-tail kept")
    seen, uniq = set(), []
    for k in kws:
        if k.lower() not in seen:
            seen.add(k.lower())
            uniq.append(k)
    log.append(f"→ dedupe … {len(uniq)} keywords")

    intents = classify_batch(uniq)
    summ = intent_summary(intents)
    log.append("$ zens-ink search_intent")
    log.append("→ " + " / ".join(f"{v} {k}" for k, v in summ.items()))

    rows = [{"kw": i["keyword"], "intent": i["intent"],
             "confidence": i["confidence"], "content_type": i["content_type"]}
            for i in intents]
    dt = time.time() - t0
    log.append(f"✓ done in {dt:.1f}s · showing {min(len(rows), MAX_TABLE)} of {len(rows)}")
    return {"ok": True, "log": log, "keywords": rows[:MAX_TABLE],
            "harvest": uniq[:MAX_HARVEST], "total": len(rows)}


def api_kd(body):
    kw = (body.get("keyword") or "").strip()
    if not kw:
        return {"ok": False, "error": "keyword required"}
    if not config.SERPER_API_KEY:
        return {"ok": False, "error":
                "SERPER_API_KEY not set · free key at https://serper.dev (2500 searches)"}
    zh = bool(body.get("zh"))
    gl = "cn" if zh else body.get("gl", "us")
    hl = "zh-CN" if zh else "en"
    market = "zh-CN" if zh else "en-US"
    t0 = time.time()
    serp = fetch_serp(kw, gl=gl, hl=hl)
    vol = fetch_search_volume(kw, market=market)
    result = calculate_kd(serp, kw, vol)
    result["_elapsed"] = round(time.time() - t0, 1)
    return {"ok": not result.get("error"), "data": result,
            "error": result.get("error")}


def api_volume(body):
    kw = (body.get("keyword") or "").strip()
    if not kw:
        return {"ok": False, "error": "keyword required"}
    zh = bool(body.get("zh"))
    country = body.get("country", "us")
    if config.BING_API_KEY:
        language = "zh-CN" if zh else "en-US"
        stats = vol_stats(kw, country=country, language=language)
        if stats.get("error"):
            return {"ok": False, "error": stats["error"]}
        stats["monthly_estimate"] = int(stats.get("avg_weekly", 0) * 4.33)
        stats["source"] = "bing"
        return {"ok": True, "data": stats}
    if config.BRAVE_API_KEY:
        data = search_brave(kw, count=10)
        est = brave_demand(kw, data)
        return {"ok": True, "data": {
            "keyword": kw, "source": "brave",
            "monthly_estimate": None, "demand_score": est.get("demand_score", 0),
            "note": "relative demand 0-100 (brave) · set BING_API_KEY for real volume"}}
    return {"ok": False, "error":
            "BING_API_KEY or BRAVE_API_KEY not set · free keys: bing webmaster / api.search.brave.com"}


def api_cluster(body):
    kws = body.get("keywords") or []
    if not kws:
        return {"ok": False, "error": "no keywords · run Keyword Lab first"}
    t0 = time.time()
    clusters = kw_cluster([k for k in kws if k.strip()])
    log = [f"$ zens-ink keyword_cluster ({len(kws)} keywords)",
           f"→ {len(clusters)} clusters in {time.time()-t0:.1f}s"]
    return {"ok": True, "log": log, "clusters": clusters}


def api_gap(body):
    urls = body.get("urls") or []
    urls = [_norm_sitemap(u) for u in urls if u.strip()]
    if len(urls) < 2:
        return {"ok": False, "error": "need at least 2 sitemap URLs (yours + one competitor)"}
    log = ["$ zens-ink competitor_gap --url {} --compare {}".format(
        urls[0], " ".join(urls[1:]))]
    sites = []
    for u in urls:
        t0 = time.time()
        pages = fetch_sitemap(u)
        topics = extract_topics(pages)
        host = u.split("//", 1)[-1].split("/")[0]
        log.append(f"→ {host}: {len(pages)} urls, {len(topics)} topics ({time.time()-t0:.1f}s)")
        if not pages:
            log.append(f"  ! no urls found for {host} · sitemap may be blocked")
        sites.append({"url": u, "host": host, "pages": len(pages),
                      "topics": dict(topics)})

    union = set().union(*(set(s["topics"]) for s in sites))
    you, comps = sites[0], sites[1:]
    gaps = []
    for t in union:
        if t not in you["topics"] and all(t in c["topics"] for c in comps):
            strength = min(c["topics"].get(t, 0) for c in comps)
            gaps.append({"topic": t, "strength": strength})
    gaps.sort(key=lambda g: -g["strength"])
    for s in sites:
        s["coverage"] = round(100 * len(set(s["topics"]) & union) / max(1, len(union)))
        del s["topics"]  # payload slim
    log.append(f"✓ union {len(union)} topics · {len(gaps)} gap topics (in all competitors, missing on you)")
    return {"ok": True, "log": log, "sites": sites, "gaps": gaps[:40],
            "union": len(union)}


def api_audit(body):
    dist = body.get("dist") or "sample-dist"
    sitemap = body.get("sitemap") or (str(Path(dist) / "sitemap.xml"))
    d = Path(dist)
    if not d.exists():
        return {"ok": False, "error": f"dist directory not found: {dist}"}
    t0 = time.time()
    out = site_audit_run(d, Path(sitemap), output_format="json")
    data = json.loads(out)
    data["elapsed"] = round(time.time() - t0, 1)
    data["dist"] = dist
    log = [f"$ zens-ink site_audit --dist {dist} --sitemap {sitemap}",
           f"→ {data['summary']['html_files']} html files · {data['summary']['sitemap_urls']} sitemap urls",
           f"→ {data['summary']['errors']} errors · {data['summary']['warnings']} warnings ({data['elapsed']}s)"]
    # companion: onpage_audit per-page scores (same dist)
    try:
        op = onpage_run(dist, sitemap)
        if isinstance(op, dict) and op.get("pages") is not None:
            data["onpage"] = {"pages": op.get("pages", []),
                              "summary": op.get("summary", {})}
            log.append(f"→ onpage_audit: {len(op.get('pages', []))} pages scored")
    except Exception as e:
        log.append(f"  ! onpage_audit skipped: {e}")
    return {"ok": True, "log": log, "data": data}


def api_geo(body):
    url = (body.get("url") or "").strip()
    if not url:
        return {"ok": False, "error": "url required"}
    t0 = time.time()
    rep = ai_audit(url)
    rep["elapsed"] = round(time.time() - t0, 1)
    log = [f"$ zens-ink ai_crawler_audit --url {url}",
           f"→ robots.txt {'ok' if rep['robots_status']==200 else 'unreachable ('+str(rep['robots_status'])+')'}"
           f" · {len(rep['crawlers'])} AI crawlers checked",
           f"→ llms.txt: HTTP {rep['llms']['llms.txt']['status']} · llms-full.txt: HTTP {rep['llms']['llms-full.txt']['status']}",
           f"✓ AI-readiness {rep['score']}/100 ({rep['elapsed']}s)"]
    return {"ok": True, "log": log, "data": rep}


def api_rank(body):
    kws = [k.strip() for k in (body.get("keywords") or []) if k.strip()]
    domain = (body.get("domain") or "").strip()
    if not kws:
        return {"ok": False, "error": "no keywords given"}
    if not config.SERPER_API_KEY:
        return {"ok": False, "error":
                "SERPER_API_KEY not set · free key at https://serper.dev (2500 searches)"}
    zh = bool(body.get("zh"))
    gl = "cn" if zh else "us"
    hl = "zh-CN" if zh else "en"
    log = [f"$ zens-ink rank_tracker check --domain {domain} ({len(kws)} keywords)"]
    rows = []
    with rt._conn() as c:
        now = rt._now()
        for kw in kws[:15]:
            try:
                pos, url = rt.fetch_positions(kw, domain, gl=gl, hl=hl)
            except Exception as e:
                log.append(f"  ! {kw}: {e}")
                continue
            found = bool(pos) and 0 < pos < 100
            c.execute("INSERT OR REPLACE INTO keywords (keyword, domain, tag, created_at) "
                      "VALUES (?,?,?,?)", (kw, domain, "webui", now))
            c.execute("INSERT INTO snapshots (keyword, position, url, checked_at) "
                      "VALUES (?,?,?,?)", (kw, pos or 999, url, now))
            hist = [r["position"] for r in c.execute(
                "SELECT position FROM snapshots WHERE keyword=? ORDER BY checked_at DESC, id DESC LIMIT 12",
                (kw,))][::-1]
            rows.append({"kw": kw, "pos": pos if found else None, "url": url,
                         "hist": hist})
            log.append(f"→ {kw}: {'#'+str(pos) if found else 'not in top 20'}")
    c and c.commit()
    log.append(f"✓ {len(rows)} keywords checked · history saved to ~/.zens_ink/ranks.db")
    return {"ok": True, "log": log, "rows": rows}


# ── remaining tool endpoints ─────────────────────────────────────────────────

def api_kgr(body):
    kw = (body.get("keyword") or "").strip()
    if not kw:
        return {"ok": False, "error": "keyword required"}
    if not config.BING_API_KEY:
        return {"ok": False, "error":
                "kgr_auto needs BING_API_KEY for the volume signal · add it in Settings"}
    zh = bool(body.get("zh"))
    lang = "zh" if zh else "en"
    country = body.get("country", "us")
    t0 = time.time()
    d = kgr_score(kw, lang=lang, country=country)
    return {"ok": True, "data": d,
            "log": [f'$ zens-ink kgr_auto "{kw}"',
                    f"→ opportunity {d['opportunity_score']} ({d['priority']}) · {time.time()-t0:.1f}s"]}


def api_dr(body):
    target = (body.get("target") or "").strip()
    if not target:
        return {"ok": False, "error": "target required"}
    d = get_domain_rating(target)
    if d.get("error"):
        return {"ok": False, "error": d["error"]}
    return {"ok": True, "data": d}


def api_serp_intent(body):
    kw = (body.get("keyword") or "").strip()
    if not kw:
        return {"ok": False, "error": "keyword required"}
    if not config.SERPER_API_KEY:
        return {"ok": False, "error": "SERPER_API_KEY not set · free key at https://serper.dev"}
    zh = bool(body.get("zh"))
    gl = "cn" if zh else "us"
    hl = "zh-CN" if zh else "en"
    d = serp_intent_analyze(kw, gl=gl, hl=hl)
    if d.get("error"):
        return {"ok": False, "error": d["error"]}
    return {"ok": True, "data": d}


def api_fanout(body):
    kw = (body.get("keyword") or "").strip()
    if not kw:
        return {"ok": False, "error": "keyword required"}
    zh = bool(body.get("zh"))
    t0 = time.time()
    d = generate_fanout(kw, icp=body.get("icp", ""), lang="zh" if zh else "en")
    return {"ok": True, "data": d,
            "log": [f'$ zens-ink geo_fanout "{kw}"',
                    f"→ {d['total_subqueries']} sub-queries · {d['gold_mine_count']} gold mines ({time.time()-t0:.1f}s)"]}


def api_qc(body):
    text = body.get("text") or ""
    fmt = "html" if body.get("format") == "html" else "md"
    if len(text.strip()) < 40:
        return {"ok": False, "error": "paste a real draft first (min ~40 chars)"}
    suffix = ".html" if fmt == "html" else ".md"
    tf = Path(tempfile.gettempdir()) / ("zensink-qc" + suffix)
    tf.write_text(text, encoding="utf-8")
    try:
        d = qc_check(tf)
    finally:
        try:
            tf.unlink()
        except OSError:
            pass
    return {"ok": True, "data": d,
            "log": [f"$ zens-ink content_qc ({fmt})",
                    f"→ score {d['score']}/100 · {d['word_count']} words · fact-density {d['fact_density']}"]}


LLMS_PROBE_LIMIT = 60  # cap pages probed per request (sitemap can be huge)


def _probe_page(url):
    """Fetch one URL's meta (title/description/date) — llms_gen MetaParser."""
    import urllib.parse as _up
    path = _up.urlparse(url).path or "/"
    if path.strip("/").split("/")[0] in SKIP_DIRS:
        return None
    p = MetaParser()
    try:
        import urllib.request as _ur
        req = _ur.Request(url, headers={"User-Agent": "zens-ink webui"})
        with _ur.urlopen(req, timeout=15) as r:
            p.feed(r.read().decode("utf-8", errors="replace"))
    except Exception:
        p.title = path
    return {"url": url,
            "path": path if path.endswith("/") else path + "/",
            "title": p.title or path,
            "description": p.description,
            "date": p.date,
            "depth": 0 if path == "/" else path.strip("/").count("/")}


def _web_fetch(url, timeout=20):
    import urllib.request as _ur
    req = _ur.Request(url, headers={"User-Agent": "zens-ink webui"})
    with _ur.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def api_llmgen(body):
    raw = (body.get("url") or "").strip()
    if not raw:
        return {"ok": False, "error": "url required"}
    if "://" not in raw:
        raw = "https://" + raw
    base = raw.split("?")[0].rstrip("/")
    t0 = time.time()
    import re as _re

    # sitemap discovery: explicit → robots.txt Sitemap: → common paths
    tried = []
    if "sitemap" in base.rsplit("/", 1)[-1]:
        candidates = [base]
    else:
        candidates = []
        try:
            robots = _web_fetch(base + "/robots.txt", timeout=10)
            candidates += _re.findall(r"(?im)^sitemap:\s*(\S+)", robots)
        except Exception:
            pass
        candidates += [base + "/sitemap.xml", base + "/sitemap-index.xml",
                       base + "/sitemap_index.xml"]

    xml, used = None, None
    for c in candidates:
        tried.append(c)
        try:
            x = _web_fetch(c)
            if "<loc" in x:
                xml, used = x, c
                break
        except Exception:
            continue
    if xml is None:
        return {"ok": False,
                "error": "no sitemap found · tried: " + ", ".join(tried[:4])}

    locs = []
    if "<sitemapindex" in xml:
        for m in _re.finditer(r"<loc>([^<]+)</loc>", xml):
            try:
                sub = _web_fetch(m.group(1))
                locs += _re.findall(r"<loc>([^<]+)</loc>", sub)
            except Exception:
                pass
    else:
        locs = _re.findall(r"<loc>([^<]+)</loc>", xml)
    if not locs:
        return {"ok": False, "error": f"sitemap has no urls ({used})"}

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(12) as ex:
        probed = [p for p in ex.map(_probe_page, locs[:LLMS_PROBE_LIMIT]) if p]
    if not probed:
        return {"ok": False, "error": "no pages could be probed"}
    site = used.split("//", 1)[-1].split("/")[0]
    txt = build_llms_txt(site, probed, body.get("name") or site, body.get("tagline") or "")
    note = "" if len(locs) <= LLMS_PROBE_LIMIT else \
        f" (probed first {LLMS_PROBE_LIMIT} of {len(locs)})"
    return {"ok": True,
            "data": {"text": txt, "pages": len(probed), "site": site,
                     "total": len(locs), "sitemap": used},
            "log": [f"$ zens-ink llms_gen --sitemap {used}",
                    f"→ {len(probed)} pages probed{note} ({time.time()-t0:.1f}s) · llms.txt built"]}


def api_gsc(body):
    if not getattr(config, "GSC_SITE_URL", None):
        return {"ok": False, "error":
                "GSC_SITE_URL not set · run python3 -m zens_ink.setup_gsc once on this machine"}
    try:
        token = gsc.get_access_token()
    except Exception as e:
        return {"ok": False, "error": f"GSC OAuth failed: {e}"}
    end = datetime.date.today()
    start = end - datetime.timedelta(days=90)
    try:
        res = gsc.query(token, ["query"], start.isoformat(), end.isoformat(), None, 50)
    except Exception as e:
        return {"ok": False, "error": f"GSC query failed: {e}"}
    rows = res.get("rows", []) or []
    return {"ok": True, "data": {"rows": rows, "start": start.isoformat(), "end": end.isoformat()},
            "log": [f"$ zens-ink search_performance --start {start} --end {end}",
                    f"→ {len(rows)} queries returned by Google Search Console"]}


def api_matrix(body):
    scores = body.get("scores") or []
    if not scores:
        return {"ok": False, "error":
                "no scored keywords · run Keyword Lab with a BING key (kgr scores), then cluster"}
    clusters = body.get("clusters") or None
    m = generate_matrix(scores, clusters)
    m.pop("ranked", None)  # payload slim — frontend already has rows
    return {"ok": True, "data": m,
            "log": [f"$ zens-ink content_matrix ({len(scores)} scored keywords)",
                    f"→ P0 {m['summary']['p0_count']} · P1 {m['summary']['p1_count']} · "
                    f"P2 {m['summary']['p2_count']} · P3 {m['summary']['p3_count']}"]}


def api_pro_brief(body):
    if not PRO:
        return {"ok": False, "error":
                f"zens_ink_pro not importable: {globals().get('PRO_ERR','package missing')} · place the pro package next to the repo"}
    kw = (body.get("keyword") or "").strip()
    if not kw:
        return {"ok": False, "error": "keyword required"}
    zh = bool(body.get("zh"))
    t0 = time.time()
    b = pro_briefs.build_brief(kw, zh=zh)
    b["_elapsed"] = round(time.time() - t0, 1)
    return {"ok": True, "data": b,
            "log": [f"$ zens_ink_pro.content_briefs \"{kw}\"",
                    f"→ brief built · intent {b.get('intent','?')} · {b['_elapsed']}s"]}


# ── Pro license — real verify against zens.ink ───────────────────────────────
LICENSE_URL = "https://zens.ink/api/verify-key/"   # trailing slash: POST must not 301
LICENSE_FILE = Path.home() / ".zensink-webui" / "license.json"
LICENSE = {"unlocked": False, "tier": None, "mask": None}


def _mask_key(k: str):
    return (k[:6] + "…" + k[-4:]) if len(k) > 12 else "set"


def _verify_key_remote(key: str):
    """POST {key} → zens.ink verify-key. Returns {valid, type?, error?}.
    Pure stdlib; network failures come back as {'valid': False, 'offline': True}."""
    try:
        req = urllib.request.Request(
            LICENSE_URL,
            data=json.dumps({"key": key}).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "User-Agent": "zensink-webui/" + WEBUI_VERSION},
            method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            return {"valid": False, "error": f"zens.ink returned HTTP {e.code}"}
    except Exception as e:
        return {"valid": False, "offline": True, "error": f"cannot reach zens.ink: {e}"}


def _load_license():
    """Boot: re-verify a persisted key. Offline failure keeps the cached
    unlock (never lock a paying customer out because their wifi is down)."""
    try:
        rec = json.loads(LICENSE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    key = rec.get("key") or ""
    if len(key) < 8:
        return
    res = _verify_key_remote(key)
    if res.get("valid"):
        tier = res.get("type") or rec.get("tier") or "annual"
    elif res.get("offline"):
        tier = rec.get("tier") or "annual"      # trust the cache offline
    else:                                        # expired / invalid → drop
        LICENSE_FILE.unlink(missing_ok=True)
        return
    LICENSE.update(unlocked=True, tier=tier, mask=_mask_key(key))


_load_license()


def api_pro_unlock(body):
    key = (body.get("key") or "").strip()
    if len(key) < 8:
        return {"ok": False, "error": "license key required (check your zens.ink purchase email)"}
    if not PRO:
        return {"ok": False, "error": f"zens_ink_pro not importable: {globals().get('PRO_ERR', 'package missing')}"}
    res = _verify_key_remote(key)
    if not res.get("valid"):
        return {"ok": False, "error": res.get("error") or "invalid license key"}
    tier = res.get("type") or "annual"
    if tier == "supporter":
        return {"ok": False, "error": "Supporter subscription doesn't include Pro tools — needs Pro 1-Year or Lifetime (zens.ink/pricing)"}
    LICENSE_FILE.parent.mkdir(parents=True, exist_ok=True)
    LICENSE_FILE.write_text(json.dumps({"key": key, "tier": tier,
                                        "saved": datetime.date.today().isoformat()},
                                       indent=2), encoding="utf-8")
    LICENSE.update(unlocked=True, tier=tier, mask=_mask_key(key))
    return {"ok": True, "data": {"tier": tier, "mask": LICENSE["mask"]},
            "log": ["$ POST zens.ink/api/verify-key", f"✓ valid · {tier} license · saved to {LICENSE_FILE}"]}


def api_pro_lock(body):
    LICENSE_FILE.unlink(missing_ok=True)
    LICENSE.update(unlocked=False, tier=None, mask=None)
    return {"ok": True, "log": ["license removed · Pro tools locked"]}


def _pro_guard():
    if not PRO:
        return {"ok": False, "error":
                f"zens_ink_pro not importable: {globals().get('PRO_ERR','package missing')}"}
    if not LICENSE["unlocked"]:
        return {"ok": False, "locked": True,
                "error": "Pro locked — verify your zens.ink license key (Pro 1-Year or Lifetime) in Settings"}
    return None


def api_pro_gapdeep(body):
    g = _pro_guard()
    if g:
        return g
    url = (body.get("url") or "").strip()
    if not url:
        return {"ok": False, "error": "sitemap url required"}
    from zens_ink_pro.gap_deep import deep_analyze
    t0 = time.time()
    d = deep_analyze(url)
    if d.get("error"):
        return {"ok": False, "error": d["error"]}
    d["_elapsed"] = round(time.time() - t0, 1)
    return {"ok": True, "data": d,
            "log": [f"$ zens_ink_pro.gap_deep --url {url}",
                    f"→ {d['total_pages']} pages · {len(d.get('categories', {}))} categories · {d['_elapsed']}s"]}


def api_pro_win(body):
    g = _pro_guard()
    if g:
        return g
    if not config.SERPER_API_KEY:
        return {"ok": False, "error": "winability needs SERPER_API_KEY (KD signal) · add it in Settings"}
    kw = (body.get("keyword") or "").strip()
    if not kw:
        return {"ok": False, "error": "keyword required"}
    zh = bool(body.get("zh"))
    gl, hl = ("cn", "zh-CN") if zh else ("us", "en")
    from zens_ink_pro import winability as W
    t0 = time.time()
    serp = fetch_serp(kw, gl=gl, hl=hl)
    kd = calculate_kd(serp, kw, fetch_search_volume(kw, market="zh-CN" if zh else "en-US"))
    gsc_rows, momentum = [], None
    try:
        gsc_rows = W.fetch_gsc_queries(days=90, limit=300) or []
    except Exception:
        pass
    try:
        momentum = W.compute_momentum()
    except Exception:
        pass
    profile = W.compute_domain_profile(gsc_rows, momentum)
    win = W.compute_winability(kd, profile, keyword=kw)
    if win.get("error"):
        return {"ok": False, "error": win["error"]}
    return {"ok": True,
            "data": {"win": win, "profile": profile,
                     "kd": {"kd": kd.get("kd"), "label_en": kd.get("label_en")}},
            "log": [f'$ zens_ink_pro.winability "{kw}"',
                    f"→ generic kd {kd.get('kd')} · your winability {win.get('score')}/100 ({win.get('verdict')})",
                    f"→ domain profile: tier {profile.get('tier')} · authority {profile.get('authority')} · {time.time()-t0:.1f}s"]}


def api_pro_radar(body):
    g = _pro_guard()
    if g:
        return g
    if not config.SERPER_API_KEY:
        return {"ok": False, "error": "content_radar needs SERPER_API_KEY (KD per keyword) · add it in Settings"}
    kws = [k.strip() for k in (body.get("keywords") or []) if k.strip()][:8]
    if not kws:
        return {"ok": False, "error": "keywords required (max 8 per run)"}
    from zens_ink_pro import content_radar as R
    t0 = time.time()
    kd_cache = {}
    for kw in kws:
        serp = fetch_serp(kw)
        kd = calculate_kd(serp, kw, fetch_search_volume(kw))
        kd_cache[kw] = {"kd": kd.get("kd"), "label_en": kd.get("label_en", ""),
                        "serp_analysis": kd.get("serp_analysis", {})}
    queue = R.build_content_queue(kd_cache)
    calendar = R.assign_to_calendar(queue, weeks=4, per_week=2)
    return {"ok": True,
            "data": {"queue": queue, "calendar": calendar},
            "log": [f"$ zens_ink_pro.content_radar ({len(kws)} keywords)",
                    f"→ {len(queue)} briefs · GO {sum(1 for b in queue if b['verdict']=='GO')} · "
                    f"CAUTION {sum(1 for b in queue if b['verdict']=='CAUTION')} · "
                    f"WAIT {sum(1 for b in queue if b['verdict']=='WAIT')} ({time.time()-t0:.1f}s)"]}


def api_pro_sov(body):
    g = _pro_guard()
    if g:
        return g
    if not config.SERPER_API_KEY:
        return {"ok": False, "error": "ai_sov needs SERPER_API_KEY · add it in Settings"}
    brand = (body.get("brand") or "").strip()
    if not brand:
        return {"ok": False, "error": "brand required"}
    kws = [k.strip() for k in (body.get("keywords") or []) if k.strip()][:6]
    if not kws:
        return {"ok": False, "error": "keyword(s) required"}
    comps = [c.strip() for c in (body.get("competitors") or []) if c.strip()]
    brands = [brand] + comps
    from zens_ink_pro import ai_sov as S
    t0 = time.time()
    results = []
    for kw in kws:
        try:
            results.append(S.analyze_keyword(kw, brands, "us", "en"))
        except Exception as e:
            results.append({"keyword": kw, "modules": {}, "hits": {}, "error": str(e)})
    agg = S.aggregate(results, [brand], comps)
    return {"ok": True, "data": {"agg": agg, "results": results},
            "log": [f"$ zens_ink_pro.ai_sov --brand {brand} ({len(kws)} keywords)",
                    f"→ your SoV {agg['mine_vs_competitors']['mine_sov_pct']}% · "
                    f"{agg['mine_vs_competitors']['mine_mentions']} mentions ({time.time()-t0:.1f}s)"]}


def api_pro_cradar(body):
    g = _pro_guard()
    if g:
        return g
    if not (os.environ.get("ANYSEARCH_API_KEY") or getattr(config, "ANYSEARCH_API_KEY", "")):
        return {"ok": False, "error": "competitor_radar needs ANYSEARCH_API_KEY · add it in Settings"}
    from zens_ink_pro import competitor_radar as C
    domain = (body.get("domain") or "").strip()
    if not domain:
        return {"ok": False, "error": "domain required"}
    api_key = os.environ.get("ANYSEARCH_API_KEY") or getattr(config, "ANYSEARCH_API_KEY", "")
    queries = body.get("queries") or [f"best tool like {domain}", f"{domain} alternatives",
                                      f"{domain} vs competitors"]
    t0 = time.time()
    competitors = C.discover_competitors(queries, api_key)
    return {"ok": True, "data": {"competitors": competitors[:20], "queries": queries},
            "log": [f"$ zens_ink_pro.competitor_radar --queries ×{len(queries)}",
                    f"→ {len(competitors)} competitors discovered ({time.time()-t0:.1f}s)"]}


def api_pro_geo(body):
    g = _pro_guard()
    if g:
        return g
    from zens_ink_pro import geo_visibility as G
    cfg = G.get_config()
    if not cfg.get("api_key"):
        return {"ok": False, "error": "geo_visibility needs LLM_API_KEY (default: deepseek) · add it in Settings"}
    brand = (body.get("brand") or "").strip()
    category = (body.get("category") or "").strip()
    comps = [c.strip() for c in (body.get("competitors") or []) if c.strip()]
    if not brand or not category:
        return {"ok": False, "error": "brand and category required"}
    # faithful to the CLI: run the module as a subprocess, capture JSON
    import subprocess
    out_json = Path(tempfile.gettempdir()) / "zensink-geo-report.json"
    cmd = [sys.executable, "-m", "zens_ink_pro.geo_visibility",
           "--brand", brand, "--category", category,
           "--competitors", ",".join(comps) or "none",
           "--json", str(out_json)]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, env=env, cwd=str(HERE), timeout=280,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "LLM queries timed out (280s)"}
    tail = [l for l in (proc.stdout or "").splitlines() if l.strip()][-12:]
    if not out_json.exists():
        return {"ok": False, "error": "geo_visibility produced no report",
                "log": tail}
    report = json.loads(out_json.read_text(encoding="utf-8"))
    out_json.unlink(missing_ok=True)
    return {"ok": True, "data": report,
            "log": [f"$ zens_ink_pro.geo_visibility --brand {brand}",
                    f"→ {report.get('total_questions','?')} questions · mention rate "
                    f"{report.get('mention_rate', 0) if isinstance(report.get('mention_rate'), str) else round(100*report.get('mention_rate',0))}% ({time.time()-t0:.0f}s)"] + tail[-4:]}


# ── full_audit: background job (subprocess, faithful to the 12-step CLI) ────
import threading  # noqa: E402

PRO_JOBS = {}
_job_seq = [0]


def _run_full_audit(job_id, cfg_path, out_dir):
    import subprocess
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    job = PRO_JOBS[job_id]
    cmd = [sys.executable, str(HERE / "_pro_runner.py"), "--config", str(cfg_path),
           "--output-dir", str(out_dir)]
    proc = subprocess.Popen(cmd, env=env, cwd=str(HERE),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    job["pid"] = proc.pid
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            job["log"].append(line)
            job["log"] = job["log"][-200:]
    proc.wait()
    job["done"] = True
    job["exit"] = proc.returncode
    artifacts = sorted(p.name for p in out_dir.glob("*") if p.is_file()) if out_dir.exists() else []
    job["artifacts"] = artifacts


def api_pro_fullaudit(body):
    g = _pro_guard()
    if g:
        return g
    raw = body.get("config") or ""
    try:
        cfg = json.loads(raw) if isinstance(raw, str) else raw
    except Exception as e:
        return {"ok": False, "error": f"invalid config JSON: {e}"}
    if not cfg.get("site_url") or not cfg.get("seed_keywords"):
        return {"ok": False, "error": "config needs at least site_url + seed_keywords"}
    out_dir = HERE / "audit-output"
    out_dir.mkdir(exist_ok=True)
    cfg.setdefault("output_dir", str(out_dir))
    cfg.setdefault("dist_dir", str(HERE / "sample-dist"))
    cfg_path = out_dir / "webui-config.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    _job_seq[0] += 1
    jid = f"fa{_job_seq[0]}"
    PRO_JOBS[jid] = {"log": ["$ zens_ink_pro.full_audit --config webui-config.json"],
                     "done": False, "artifacts": []}
    threading.Thread(target=_run_full_audit, args=(jid, cfg_path, out_dir), daemon=True).start()
    return {"ok": True, "data": {"job": jid},
            "log": ["full audit started · 12 steps · polling…"]}


def api_pro_job(body):
    jid = (body.get("id") or "").strip()
    job = PRO_JOBS.get(jid)
    if not job:
        return {"ok": False, "error": f"unknown job {jid}"}
    return {"ok": True, "data": {"done": job["done"], "exit": job.get("exit"),
                                 "log": job["log"][-40:], "artifacts": job["artifacts"]}}


def api_status():
    return {"ok": True, "engine": ENGINE, "version": VERSION, "webui": WEBUI_VERSION,
            "error": None if ENGINE else ENGINE_ERR,
            "pro": {"available": PRO,
                    "version": PRO_VERSION if PRO else None,
                    "error": None if PRO else globals().get("PRO_ERR", "")},
            "license": {"unlocked": LICENSE["unlocked"], "tier": LICENSE["tier"],
                        "mask": LICENSE["mask"]},
            "paths": {"repo": str(REPO) if REPO else "(pip/site-packages)",
                      "pro": str(PRO_DIR) if PRO_DIR else "(not found)",
                      "env": str(ENV_PATH),
                      "engine_src": (str(Path(zens_ink.__file__).resolve().parent.parent)
                                     if ENGINE else "")},
            "keys": {**{ui: bool(getattr(config, var, "") or os.environ.get(var))
                        for ui, var in KEY_MAP.items()},
                     "gsc": bool(getattr(config, "GSC_CREDENTIALS_FILE", None))}}


# ── settings: manage .env API keys ───────────────────────────────────────────

KEY_MAP = {  # ui name → .env var (and config attribute)
    "serper": "SERPER_API_KEY",
    "bing": "BING_API_KEY",
    "brave": "BRAVE_API_KEY",
    "ahrefs": "AHREFS_API_KEY",
    "anysearch": "ANYSEARCH_API_KEY",
    "llm": "LLM_API_KEY",
}
ENV_PATH = ((getattr(config, "_ROOT", None) or Path(config.__file__).resolve().parent.parent) / ".env") \
    if ENGINE else (REPO / ".env" if REPO else HERE / ".env")
KEY_DOCS = {
    "serper": ("kd · rank_tracker · serp_intent · kgr_auto · winability · ai_sov · content_radar",
               "https://serper.dev", "2,500 free searches"),
    "bing": ("keyword_volume · kgr_auto",
             "https://www.bing.com/webmasters", "free with webmaster account"),
    "brave": ("brave_volume",
              "https://api.search.brave.com", "2,000 queries/mo free"),
    "ahrefs": ("domain_rating",
               "https://ahrefs.com/api", "free public DR endpoint"),
    "anysearch": ("competitor_radar (pro)",
                  "https://anysearch.global", "paid API"),
    "llm": ("geo_visibility (pro) · defaults to deepseek-chat",
            "https://platform.deepseek.com", "pay per token"),
}


def _mask(v: str):
    if not v:
        return None
    return v[:4] + "…" + v[-4:] if len(v) > 12 else "set"


def _env_disp():
    """Human-friendly .env path: relative to the workspace root, never a
    machine-absolute path in UI-facing payloads."""
    try:
        return str(ENV_PATH.resolve().relative_to(HERE.parent.resolve()))
    except ValueError:
        return ENV_PATH.name


def api_keys_get():
    keys = {}
    for ui, var in KEY_MAP.items():
        v = getattr(config, var, "") or ""
        keys[ui] = {"set": bool(v), "masked": _mask(v)}
    return {"ok": True, "keys": keys, "env_path": _env_disp(),
            "docs": {k: {"tools": t, "url": u, "tier": f} for k, (t, u, f) in KEY_DOCS.items()}}


def api_keys_set(body):
    updates = {}
    for ui, val in (body or {}).items():
        if ui in KEY_MAP and isinstance(val, str):
            updates[KEY_MAP[ui]] = val.strip()  # "" → clear
    if not updates:
        return {"ok": False, "error": 'no keys provided (send e.g. {"serper": "…"})'}

    # 1. rewrite .env (create if absent)
    lines = []
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    for var, val in updates.items():
        new_ln = f"{var}={val}" if val else f"# {var}="
        hits = [i for i, ln in enumerate(lines)
                if ln.strip().startswith(var + "=") or ln.strip().startswith("# " + var + "=")]
        if hits:
            lines[hits[0]] = new_ln
            for i in hits[1:]:
                lines[i] = None  # dedupe older duplicates
            lines = [ln for ln in lines if ln is not None]
        elif val:
            lines.append(new_ln)
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # 2. hot-apply: force values into os.environ + config + every loaded module
    #    (config.load_env uses setdefault, so we set directly with known values)
    import os as _os
    import sys as _s
    applied = []
    for var, val in updates.items():
        _os.environ[var] = val                      # "" clears
        setattr(config, var, val)
        for name, mod in list(_s.modules.items()):
            if name == "zens_ink" or name.startswith("zens_ink."):
                if hasattr(mod, var):
                    setattr(mod, var, val)
        applied.append(f"{var}={'set' if val else 'cleared'}")

    log = ["$ zens-ink config · .env",
           f"→ {_env_disp()}"] + [f"→ {a}" for a in applied] + \
          ["✓ hot-applied to all loaded tools · no restart needed"]
    return {"ok": True, "log": log, "keys": api_keys_get()["keys"]}


ROUTES = {
    "research": api_research, "kd": api_kd, "volume": api_volume,
    "cluster": api_cluster, "gap": api_gap, "audit": api_audit,
    "geo": api_geo, "rank": api_rank, "keys": api_keys_set,
    "kgr": api_kgr, "dr": api_dr, "serp_intent": api_serp_intent,
    "fanout": api_fanout, "qc": api_qc, "llmgen": api_llmgen, "gsc": api_gsc,
    "matrix": api_matrix, "pro_brief": api_pro_brief,
    "pro_gapdeep": api_pro_gapdeep, "pro_win": api_pro_win,
    "pro_radar": api_pro_radar, "pro_sov": api_pro_sov,
    "pro_cradar": api_pro_cradar, "pro_geo": api_pro_geo,
    "pro_fullaudit": api_pro_fullaudit, "pro_job": api_pro_job,
    "pro_unlock": api_pro_unlock, "pro_lock": api_pro_lock,
}


class Handler(BaseHTTPRequestHandler):
    server_version = "ZensInkWebUI/1.0"

    def log_message(self, fmt, *args):  # quieter logs
        sys.stderr.write("· %s %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, payload, ctype="application/json; charset=utf-8"):
        body = (json.dumps(payload) if isinstance(payload, (dict, list))
                else payload).encode() if "json" in ctype else payload
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/status":
            return self._send(200, api_status())
        if path == "/api/keys":
            if not ENGINE:
                return self._send(503, {"ok": False, "error": "engine missing"})
            return self._send(200, api_keys_get())
        if path == "/":
            f = HERE / "index.html"
            if f.exists():
                return self._send(200, f.read_bytes(), "text/html; charset=utf-8")
            return self._send(404, {"ok": False, "error": "index.html missing"})
        # static: files in webui root + read-only audit-output/ artifacts
        # (strict prefix whitelist — no ".." traversal, no arbitrary subdirs)
        rel = path.lstrip("/")
        f = HERE / rel
        allowed = f.is_file() and (
            (f.parent == HERE and rel == "index.html")
            or rel.startswith("audit-output/") and f.parent == (HERE / "audit-output"))
        if allowed:
            ctype = ("image/svg+xml" if f.suffix == ".svg" else
                     "text/html; charset=utf-8" if f.suffix == ".html" else
                     "text/plain; charset=utf-8" if f.suffix in (".md", ".txt", ".csv", ".json")
                     else "application/octet-stream")
            return self._send(200, f.read_bytes(), ctype)
        return self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0].removeprefix("/api/")
        fn = ROUTES.get(path)
        if not fn:
            return self._send(404, {"ok": False, "error": f"unknown endpoint {path}"})
        if not ENGINE:
            return self._send(503, {"ok": False,
                                    "error": "zens_ink package not importable: " + ENGINE_ERR})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            return self._send(400, {"ok": False, "error": f"bad json: {e}"})
        try:
            return self._send(200, fn(body))
        except SystemExit:
            # some tools sys.exit(1) on missing keys — surface as JSON, keep thread alive
            return self._send(422, {"ok": False,
                                    "error": "tool aborted (missing API key) · check Settings"})
        except Exception as e:
            return self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    port = int(os.environ.get("ZENSINK_PORT")
               or (sys.argv[1] if len(sys.argv) > 1 else 8390))
    print(f"ZensInk WebUI adapter · engine {'OK v' + VERSION if ENGINE else 'MISSING'}")
    print(f"  repo : {REPO or '(pip/site-packages)'}")
    print(f"  pro  : {PRO_DIR or '(not found)'}" + (f" · v{PRO_VERSION}" if PRO else ""))
    print(f"  .env : {ENV_PATH}")
    print(f"→ http://127.0.0.1:{port}   (ZENSINK_REPO / ZENSINK_PRO / ZENSINK_PORT to override)")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
