#!/usr/bin/env python3
"""Colony feed collector engine — self-contained, stdlib-only.

This file is BOTH a package module and a deployable script: the feeds manager
copies it verbatim into the harness scripts dir (as ``colony-feed-engine.py``)
where per-instance shims execute it under any python3.  Keep it stdlib-only.

Stages (selected via $COLONY_FEED_STAGE, default ``collect``):
  collect -> run every configured source adapter, score + dedup new items,
             overwrite the instance queue with never-seen items, write the
             alert file when top-priority items appear
  alert   -> print pending alert text (empty stdout = silent), optionally
             piping it through the instance send_command

Config file (path in $COLONY_FEED_CONFIG) is either the spec-rendered shape
produced by ``FeedSpec.engine_config()`` or a legacy hand-written shape
(detected by the absence of ``feed_name``) kept for pre-framework installs.
"""

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

UA = "Colony-Feed-Collector/1.0"


# --------------------------------------------------------------------- config
def load_config():
    path = os.environ.get("COLONY_FEED_CONFIG")
    if not path:
        raise SystemExit("COLONY_FEED_CONFIG not set")
    with open(os.path.expanduser(path), encoding="utf-8") as f:
        cfg = json.load(f)
    if "feed_name" not in cfg:
        cfg = _normalize_legacy(cfg)
    cfg.setdefault("benchmark_regex", r"\d+\s*(tok/s|tokens?/?s|t/s|tflops?|ms|latency|throughput)")
    return cfg


def _normalize_legacy(cfg):
    """Map the pre-framework single-feed config shape onto the engine shape."""
    home = os.path.expanduser("~")
    sc = cfg.get("scoring", {})
    cats = []
    for key, points_key in (("hardware_keywords", "hardware_match"),
                            ("model_keywords", "model_match"),
                            ("inference_keywords", "inference_technique")):
        kws = cfg.get(key) or []
        if kws:
            cats.append({"keywords": kws, "points": sc.get(points_key, 10)})
    # boosts that were hardcoded in the original collector
    if sc.get("agent_infra"):
        cats.append({"keywords": ["agent", "harness", "router", "orchestrat", "mcp",
                                  "tool use", "sub-agent"],
                     "points": sc["agent_infra"]})
    if sc.get("training_relevant"):
        cats.append({"keywords": ["training", "fine-tune", "fine tune", "lora", "qlora",
                                  "distributed", "fsdp", "deepspeed"],
                     "points": sc["training_relevant"]})
    return {
        "feed_name": cfg.get("name", "legacy"),
        "queue_path": cfg.get("queue_path", f"{home}/.hermes/data/feed_queue.json"),
        "seen_db": cfg.get("seen_db", f"{home}/.hermes/data/feed_seen.db"),
        "alert_path": cfg.get("alert_path", f"{home}/.hermes/data/p0_alert.json"),
        "x_searches": cfg.get("x_searches", []),
        "x_results_per_query": 15,
        "key_accounts": cfg.get("key_accounts", []),
        "hf_keywords": cfg.get("hf_keywords", []),
        "github_search": cfg.get("github_search",
                                 "inference OR vllm OR sglang OR speculative OR llm serving"),
        "github_keywords": cfg.get("github_keywords", []),
        "forum_urls": cfg.get("forum_urls", []),
        "forum_title_keywords": cfg.get("forum_title_keywords", []),
        "arxiv_categories": cfg.get("arxiv_categories", []),
        "arxiv_query_terms": cfg.get("arxiv_query_terms",
                                     ["speculative", "inference", "quantization", "serving",
                                      "efficient", "throughput", "KV cache", "mixture of experts"]),
        "arxiv_keywords": cfg.get("arxiv_keywords", []),
        "rss": cfg.get("rss", []),
        "keyword_categories": cats,
        "scoring": {
            "account_whitelist": sc.get("account_whitelist", 10),
            "engagement_per_100_likes": sc.get("engagement_per_100_likes", 1),
            "engagement_per_1000_likes": sc.get("engagement_per_1000_likes", 2),
            "engagement_max": sc.get("engagement_max", 10),
            "human_curated": sc.get("human_curated", 20),
            "benchmark_numbers": sc.get("benchmark_numbers", 15),
            "code_release": sc.get("code_release", 10),
            "open_weights": sc.get("open_weights", 10),
        },
        "priority_thresholds": cfg.get("priority_thresholds", {"p0": 50, "p1": 35, "p2": 20}),
        "send_command": cfg.get("send_command", ""),
        "alert_min_priority": cfg.get("alert_min_priority", "P0"),
    }


# ----------------------------------------------------------------- seen store
def get_db(cfg):
    path = os.path.expanduser(cfg["seen_db"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS seen_items (
        id TEXT PRIMARY KEY, source TEXT, url TEXT, score INTEGER,
        priority TEXT, raw_json TEXT, processed_at TEXT)""")
    return conn


def is_seen(conn, item_id):
    return conn.execute("SELECT 1 FROM seen_items WHERE id=?", (item_id,)).fetchone() is not None


def mark_seen(conn, item_id, source, url, score, priority, raw=""):
    conn.execute(
        "INSERT OR REPLACE INTO seen_items (id, source, url, score, priority, raw_json, processed_at)"
        " VALUES (?,?,?,?,?,?,datetime('now'))",
        (item_id, source, url, score, priority, json.dumps(raw)[:5000]))
    conn.commit()


# -------------------------------------------------------------------- scoring
def score_item(text, cfg, account=None, likes=0, has_code=False,
               has_weights=False, human_curated=False, has_benchmarks=False):
    sc = cfg["scoring"]
    tl = text.lower()
    score = 0
    for cat in cfg.get("keyword_categories", []):
        if any(kw.lower() in tl for kw in cat.get("keywords", [])):
            score += cat.get("points", 10)
    if account and account.lower() in [a.lower() for a in cfg.get("key_accounts", [])]:
        score += sc["account_whitelist"]
    if likes > 0:
        score += min(int(likes / 100) * sc["engagement_per_100_likes"]
                     + int(likes / 1000) * sc["engagement_per_1000_likes"],
                     sc["engagement_max"])
    if human_curated:
        score += sc["human_curated"]
    if has_benchmarks or re.search(cfg["benchmark_regex"], tl):
        score += sc["benchmark_numbers"]
    if has_code:
        score += sc["code_release"]
    if has_weights or "open weights" in tl or ("weights" in tl and "release" in tl):
        score += sc["open_weights"]
    th = cfg["priority_thresholds"]
    for pri in ("p0", "p1", "p2"):
        if score >= th.get(pri, 10**9):
            return score, pri.upper()
    return score, "P3"


def _fetch_json(url, timeout=15, headers=None):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _fetch_text(url, timeout=20):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode(errors="replace")


# ------------------------------------------------------------------- adapters
def collect_x(cfg, conn):
    items = []
    for query in cfg.get("x_searches", []):
        try:
            result = subprocess.run(["xurl", "search", query, "-n",
                                     str(cfg.get("x_results_per_query", 15))],
                                    capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                continue
            data = json.loads(result.stdout)
            users = {u.get("id"): u.get("username", "")
                     for u in data.get("includes", {}).get("users", [])}
            for tweet in data.get("data", []):
                tid = tweet.get("id", "")
                if not tid or is_seen(conn, f"x:{tid}"):
                    continue
                text = tweet.get("text", "")
                author = users.get(tweet.get("author_id"), "")
                likes = tweet.get("public_metrics", {}).get("like_count", 0)
                has_code = "github.com" in text or "huggingface.co" in text
                sc, pri = score_item(text, cfg, account=author, likes=likes, has_code=has_code)
                url = (f"https://x.com/{author}/status/{tid}" if author
                       else f"https://x.com/i/web/status/{tid}")
                mark_seen(conn, f"x:{tid}", "x", url, sc, pri,
                          {"text": text[:500], "author": author})
                items.append({"source": "x", "id": tid, "author": author,
                              "text": text[:500], "url": url, "score": sc,
                              "priority": pri, "likes": likes,
                              "created_at": tweet.get("created_at", "")})
        except Exception as e:
            print(f"X error for '{query[:40]}': {e}", file=sys.stderr)
        time.sleep(1)
    return items


def collect_hf(cfg, conn):
    kws = cfg.get("hf_keywords", [])
    if not kws:
        return []
    items = []
    try:
        models = _fetch_json("https://huggingface.co/api/models?sort=trending&limit=30")
        for model in models[:30]:
            mid = model.get("id", "")
            if not mid or is_seen(conn, f"hf:{mid}"):
                continue
            text = (mid + " " + " ".join(model.get("tags", []))).lower()
            if not any(kw.lower() in text for kw in kws):
                continue
            sc, pri = score_item(text, cfg, has_weights=True, has_code=True)
            url = f"https://huggingface.co/{mid}"
            mark_seen(conn, f"hf:{mid}", "huggingface", url, sc, pri, {"name": mid})
            items.append({"source": "huggingface", "id": mid, "url": url,
                          "text": f"Model: {mid} | Tags: {', '.join(model.get('tags', [])[:5])}"
                                  f" | Downloads: {model.get('downloads', 0)}",
                          "score": sc, "priority": pri})
    except Exception as e:
        print(f"HF error: {e}", file=sys.stderr)
    return items


def collect_github(cfg, conn):
    query = cfg.get("github_search", "")
    if not query:
        return []
    items = []
    try:
        q = urllib.parse.quote(query)
        data = _fetch_json("https://api.github.com/search/repositories"
                           f"?q={q}&sort=updated&order=desc&per_page=15",
                           headers={"Accept": "application/vnd.github.v3+json"})
        kws = cfg.get("github_keywords", [])
        for repo in data.get("items", []):
            full = repo.get("full_name", "")
            key = f"gh:{full}:{repo.get('updated_at', '')}"
            if not full or is_seen(conn, key):
                continue
            text = (full + " " + (repo.get("description") or "")).lower()
            if kws and not any(kw.lower() in text for kw in kws):
                continue
            stars = repo.get("stargazers_count", 0)
            sc, pri = score_item(text, cfg, has_code=True, likes=min(stars, 10000))
            url = repo.get("html_url", "")
            mark_seen(conn, key, "github", url, sc, pri, {"name": full})
            items.append({"source": "github", "id": full, "url": url,
                          "text": f"Repo: {full} | {repo.get('description', '')} | Stars: {stars}",
                          "score": sc, "priority": pri, "stars": stars})
    except Exception as e:
        print(f"GitHub error: {e}", file=sys.stderr)
    return items


def collect_forums(cfg, conn):
    items = []
    title_kws = [k.lower() for k in cfg.get("forum_title_keywords", [])]
    for forum_url in cfg.get("forum_urls", []):
        try:
            data = _fetch_json(forum_url.rstrip("/") + ".json")
            base = "/".join(forum_url.split("/")[:3])
            for topic in data.get("topic_list", {}).get("topics", [])[:20]:
                tid = str(topic.get("id", ""))
                title = topic.get("title", "")
                if not tid or is_seen(conn, f"forum:{tid}"):
                    continue
                if title_kws and not any(kw in title.lower() for kw in title_kws):
                    continue
                views = topic.get("views", 0)
                sc, pri = score_item(title, cfg, has_code=True, likes=views // 100)
                url = f"{base}/t/{topic.get('slug', '')}/{tid}"
                mark_seen(conn, f"forum:{tid}", "forum", url, sc, pri, {"title": title})
                items.append({"source": "forum", "id": tid, "text": title, "url": url,
                              "score": sc, "priority": pri, "views": views,
                              "replies": topic.get("posts_count", 1) - 1})
        except Exception as e:
            print(f"Forum error for {forum_url}: {e}", file=sys.stderr)
    return items


def collect_arxiv(cfg, conn):
    kws = [k.lower() for k in cfg.get("arxiv_keywords", [])]
    terms = cfg.get("arxiv_query_terms", [])
    items = []
    for cat in cfg.get("arxiv_categories", []):
        try:
            abs_q = "+OR+".join(f"abs:{urllib.parse.quote(t)}" for t in terms) or "abs:survey"
            xml = _fetch_text("http://export.arxiv.org/api/query?search_query="
                              f"cat:{cat}+AND+({abs_q})"
                              "&start=0&max_results=15&sortBy=submittedDate&sortOrder=descending")
            for entry in re.findall(r"<entry>(.*?)</entry>", xml, re.DOTALL):
                m_id = re.search(r"<id>(.*?)</id>", entry)
                m_title = re.search(r"<title>(.*?)</title>", entry, re.DOTALL)
                if not m_id or not m_title:
                    continue
                arxiv_id = m_id.group(1).split("/")[-1]
                if is_seen(conn, f"arxiv:{arxiv_id}"):
                    continue
                title = m_title.group(1).strip().replace("\n", " ")
                m_sum = re.search(r"<summary>(.*?)</summary>", entry, re.DOTALL)
                summary = m_sum.group(1).strip()[:500] if m_sum else ""
                text = (title + " " + summary).lower()
                if kws and not any(kw in text for kw in kws):
                    continue
                sc, pri = score_item(title + " " + summary, cfg, has_code=True)
                url = f"https://arxiv.org/abs/{arxiv_id}"
                mark_seen(conn, f"arxiv:{arxiv_id}", "arxiv", url, sc, pri, {"title": title})
                items.append({"source": "arxiv", "id": arxiv_id, "text": title,
                              "summary": summary[:300], "url": url,
                              "score": sc, "priority": pri})
        except Exception as e:
            print(f"arXiv error for {cat}: {e}", file=sys.stderr)
        time.sleep(3)  # arXiv rate limit: 1 req / 3s
    return items


def collect_rss(cfg, conn):
    items = []
    for feed in cfg.get("rss", []):
        name, url = feed.get("name", "rss"), feed.get("url", "")
        if not url:
            continue
        try:
            xml = _fetch_text(url)
            entries = (re.findall(r"<item>(.*?)</item>", xml, re.DOTALL)
                       or re.findall(r"<entry>(.*?)</entry>", xml, re.DOTALL))
            for entry in entries[:20]:
                m_title = re.search(r"<title[^>]*>(.*?)</title>", entry, re.DOTALL)
                m_link = (re.search(r"<link[^>]*href=\"(.*?)\"", entry)
                          or re.search(r"<link[^>]*>(.*?)</link>", entry, re.DOTALL))
                if not m_title:
                    continue
                title = re.sub(r"<!\[CDATA\[|\]\]>", "", m_title.group(1)).strip()
                link = (m_link.group(1).strip() if m_link else url)
                key = f"rss:{hashlib.md5((name + title).encode()).hexdigest()[:16]}"
                if is_seen(conn, key):
                    continue
                sc, pri = score_item(title, cfg)
                mark_seen(conn, key, f"rss:{name}", link, sc, pri, {"title": title})
                items.append({"source": f"rss:{name}", "id": key, "text": title,
                              "url": link, "score": sc, "priority": pri})
        except Exception as e:
            print(f"RSS error for {name}: {e}", file=sys.stderr)
    return items


# --------------------------------------------------------------------- stages
ADAPTERS = [("x", collect_x), ("huggingface", collect_hf), ("github", collect_github),
            ("forum", collect_forums), ("arxiv", collect_arxiv), ("rss", collect_rss)]


def stage_collect(cfg):
    conn = get_db(cfg)
    all_items = []
    for label, fn in ADAPTERS:
        got = fn(cfg, conn)
        all_items.extend(got)
        print(f"  {label}: {len(got)} new items", file=sys.stderr)
    all_items.sort(key=lambda x: x["score"], reverse=True)

    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %I:%M %p %Z")
    queue_path = os.path.expanduser(cfg["queue_path"])
    os.makedirs(os.path.dirname(queue_path), exist_ok=True)
    with open(queue_path, "w", encoding="utf-8") as f:
        json.dump({"feed": cfg["feed_name"], "collected_at": now,
                   "total_items": len(all_items), "items": all_items}, f, indent=2)

    min_pri = cfg.get("alert_min_priority", "P0")
    alerts = [i for i in all_items if i["priority"] <= min_pri]  # 'P0' < 'P1'
    if alerts:
        with open(os.path.expanduser(cfg["alert_path"]), "w", encoding="utf-8") as f:
            json.dump({"alerts": alerts, "generated_at": now, "delivered": False}, f, indent=2)

    conn.close()
    by_pri = {}
    for i in all_items:
        by_pri[i["priority"]] = by_pri.get(i["priority"], 0) + 1
    print(json.dumps({"feed": cfg["feed_name"], "collected_at": now,
                      "total": len(all_items), "by_priority": by_pri,
                      "top_5": [{"text": i["text"][:100], "priority": i["priority"],
                                 "url": i["url"]} for i in all_items[:5]]}, indent=2))


def stage_alert(cfg):
    """Emit pending alerts once. Empty stdout = silent (no-agent cron pattern)."""
    path = os.path.expanduser(cfg["alert_path"])
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if data.get("delivered") or not data.get("alerts"):
        return
    lines = [f"[{cfg['feed_name']}] {len(data['alerts'])} top-priority item(s):"]
    for a in data["alerts"][:10]:
        lines.append(f"- {a['text'][:160]}\n  {a['url']}")
    text = "\n".join(lines)
    send_cmd = cfg.get("send_command", "")
    if send_cmd:
        try:
            subprocess.run(send_cmd, shell=True, input=text.encode(), timeout=60, check=True)
            print(f"[{cfg['feed_name']}] alert posted ({len(data['alerts'])} items)")
        except Exception as e:
            print(f"[{cfg['feed_name']}] alert post FAILED: {e}")
            return  # keep undelivered so the next tick retries
    else:
        print(text)
    data["delivered"] = True
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def main():
    cfg = load_config()
    stage = os.environ.get("COLONY_FEED_STAGE", "collect")
    if stage == "collect":
        stage_collect(cfg)
    elif stage == "alert":
        stage_alert(cfg)
    else:
        raise SystemExit(f"unknown stage: {stage}")


if __name__ == "__main__":
    main()
