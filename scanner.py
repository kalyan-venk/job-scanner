#!/usr/bin/env python3
"""
Job scanner: hits Greenhouse / Lever / Ashby public JSON APIs, filters to ML/AI
roles with a two-tier keyword filter, diffs against the previous run so only NEW
postings surface, and sends ONE EMAIL PER COMPANY that has new roles.

State lives in seen_jobs.json, committed back by the GitHub Action so the diff
persists across runs.
"""

import json
import os
import smtplib
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import yaml

SEEN_FILE = "seen_jobs.json"
CONFIG_FILE = "config.yml"
UA = {"User-Agent": "Mozilla/5.0 (job-scanner)"}
TIMEOUT = 15


def fetch_json(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------- ATS adapters: each returns normalized job dicts ----------

def strip_html(html):
    """Crude HTML-to-text: drop tags, collapse whitespace. Good enough for keyword scoring."""
    import re
    text = re.sub(r"<[^>]+>", " ", html or "")
    text = text.replace("&amp;", "&").replace("&nbsp;", " ").replace("&lt;", "<").replace("&gt;", ">")
    return re.sub(r"\s+", " ", text).strip()


def from_greenhouse(slug, name):
    data = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    return [{
        "id": f"gh:{slug}:{j['id']}",
        "title": j.get("title", ""),
        "location": (j.get("location") or {}).get("name", ""),
        "url": j.get("absolute_url", ""),
        "company": name,
        "posted_at": j.get("first_published") or j.get("updated_at", ""),
        "jd_text": strip_html(j.get("content", "")),
    } for j in data.get("jobs", [])]


def from_lever(slug, name):
    data = fetch_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    out = []
    for j in data:
        cats = j.get("categories", {}) or {}
        created = j.get("createdAt")  # epoch ms
        posted = ""
        if created:
            try:
                posted = datetime.fromtimestamp(int(created) / 1000, tz=timezone.utc).isoformat()
            except (ValueError, TypeError):
                posted = ""
        jd_parts = [
            j.get("descriptionPlain", "") or strip_html(j.get("description", "")),
        ]
        for section in j.get("lists", []) or []:
            jd_parts.append(section.get("text", ""))
            jd_parts.append(strip_html(section.get("content", "")))
        out.append({
            "id": f"lv:{slug}:{j.get('id','')}",
            "title": j.get("text", ""),
            "location": cats.get("location", ""),
            "url": j.get("hostedUrl", ""),
            "company": name,
            "posted_at": posted,
            "jd_text": " ".join(p for p in jd_parts if p),
        })
    return out


def from_ashby(slug, name):
    data = fetch_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=false")
    return [{
        "id": f"as:{slug}:{j.get('id','')}",
        "title": j.get("title", ""),
        "location": j.get("location", ""),
        "url": j.get("jobUrl", ""),
        "company": name,
        "posted_at": j.get("publishedAt", ""),
        "jd_text": j.get("descriptionPlain", "") or strip_html(j.get("descriptionHtml", "")),
    } for j in data.get("jobs", [])]


def from_workday(entry, name):
    """
    Workday has no single global API; each tenant needs three things found by
    hand from the company's real career page:
      tenant  - the Workday account slug (often, but not always, the company name)
      wd_host - which numbered Workday pod they're on, e.g. 'wd1', 'wd3', 'wd5'
      site    - the career site name within that tenant, often 'External' but varies

    Config shape:
      {tenant: nvidia, wd_host: wd5, site: NVIDIAExternalCareerSite, name: Nvidia}

    Endpoint pattern (POST, not GET - Workday's CXS API requires POST with a body):
      https://{tenant}.{wd_host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
    """
    tenant = entry["tenant"]
    wd_host = entry["wd_host"]
    site = entry["site"]
    url = f"https://{tenant}.{wd_host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    body = json.dumps({"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}).encode()
    req = urllib.request.Request(url, data=body, headers={**UA, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    out = []
    for j in data.get("jobPostings", []):
        path = j.get("externalPath", "")
        out.append({
            "id": f"wd:{tenant}:{path}",
            "title": j.get("title", ""),
            "location": j.get("locationsText", ""),
            "url": f"https://{tenant}.{wd_host}.myworkdayjobs.com/{site}{path}" if path else "",
            "company": name,
            "posted_at": "",  # Workday gives relative text ("Posted 3 Days Ago"), not a parseable date
        })
    return out


ADAPTERS = {"greenhouse": from_greenhouse, "lever": from_lever, "ashby": from_ashby}


# ---------- Two-tier filter (drives precision/recall) ----------

def title_matches(title, cfg):
    t = title.lower()

    for x in cfg["hard_exclude"]:
        if x in t:
            return False

    for s in cfg["strong_include"]:
        if s in t:
            return True

    has_context = any(c in t for c in cfg["context_terms"])
    if has_context:
        has_role = any(r in t for r in cfg["role_words"])
        if has_role:
            return True

    return False


def location_matches(location, loc_kw):
    if not loc_kw:
        return True
    loc = location.lower()
    return any(k in loc for k in loc_kw)


def keep(job, cfg):
    return title_matches(job["title"], cfg) and location_matches(job["location"], cfg["location_keywords"])


# ---------- Main ----------

def score_jd(job, cfg):
    """
    Free keyword-based JD scoring. Returns (label, score, matched_terms).
    Reads jd_text (falls back to title if JD text unavailable, e.g. Workday).
    """
    scoring = cfg.get("match_scoring")
    if not scoring:
        return (None, 0, [])

    text = (job.get("jd_text") or "") + " " + job.get("title", "")
    text = text.lower()
    if not text.strip():
        return (None, 0, [])

    score = 0
    matched = []
    for tier_key in ["tier_s_terms", "tier_a_terms", "tier_b_terms", "penalty_terms"]:
        tier = scoring.get(tier_key, {})
        weight = tier.get("weight", 0)
        for term in tier.get("terms", []):
            if term.lower() in text:
                score += weight
                if weight > 0:
                    matched.append(term)

    th = scoring.get("thresholds", {})
    if score >= th.get("made_for_you", 15):
        label = "Made for you"
    elif score >= th.get("good_match", 8):
        label = "Good match"
    elif score >= th.get("average_match", 3):
        label = "Average match"
    elif score >= th.get("somewhat_stretch", 0):
        label = "Somewhat stretch"
    else:
        label = "Time waste"

    return (label, score, matched)


def load_cfg():
    with open(CONFIG_FILE) as f:
        cfg = yaml.safe_load(f)
    for key in ["strong_include", "context_terms", "role_words", "hard_exclude"]:
        cfg[key] = [x.lower() for x in cfg.get(key, [])]
    cfg["location_keywords"] = [x.lower() for x in cfg.get("location_keywords", [])]
    return cfg


def main():
    cfg = load_cfg()

    try:
        with open(SEEN_FILE) as f:
            seen = set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        seen = set()
    first_run = len(seen) == 0

    matched = []
    warnings = []

    for ats, adapter in ADAPTERS.items():
        for entry in cfg.get(ats, []):
            slug, name = entry["slug"], entry.get("name", entry["slug"])
            try:
                jobs = adapter(slug, name)
                if not jobs:
                    warnings.append(f"{name} ({ats}:{slug}): 0 jobs returned - check slug")
                matched.extend(j for j in jobs if keep(j, cfg))
            except urllib.error.HTTPError as e:
                warnings.append(f"{name} ({ats}:{slug}): HTTP {e.code} - slug wrong or moved ATS")
            except Exception as e:
                warnings.append(f"{name} ({ats}:{slug}): {str(e)[:60]}")
            time.sleep(0.3)

    # Workday: different config shape (tenant/wd_host/site, no single 'slug'),
    # different HTTP method (POST), kept as its own loop rather than forcing
    # it into the GET-based ADAPTERS dict.
    for entry in cfg.get("workday", []):
        name = entry.get("name", entry.get("tenant", "?"))
        tag = f"{entry.get('tenant')}.{entry.get('wd_host')}/{entry.get('site')}"
        try:
            jobs = from_workday(entry, name)
            if not jobs:
                warnings.append(f"{name} (workday:{tag}): 0 jobs returned - check tenant/wd_host/site")
            matched.extend(j for j in jobs if keep(j, cfg))
        except urllib.error.HTTPError as e:
            warnings.append(f"{name} (workday:{tag}): HTTP {e.code} - tenant/host/site wrong")
        except Exception as e:
            warnings.append(f"{name} (workday:{tag}): {str(e)[:60]}")
        time.sleep(0.3)

    new_jobs = [j for j in matched if j["id"] not in seen]

    # Score each new job against your resume signals (free, keyword-based).
    for j in new_jobs:
        label, score, hits = score_jd(j, cfg)
        j["match_label"] = label
        j["match_score"] = score
        j["match_hits"] = hits

    # Persist everything currently matched (closed roles age out, no re-alerts).
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted({j["id"] for j in matched}), f, indent=0)

    print(f"Matched {len(matched)} | new {len(new_jobs)} | warnings {len(warnings)}")
    for w in warnings:
        print("  WARN:", w)

    if first_run:
        print("First run: state seeded, no emails sent.")
        return
    if not new_jobs:
        print("No new jobs.")
        return

    # ONE EMAIL PER COMPANY
    by_company = {}
    for j in new_jobs:
        by_company.setdefault(j["company"], []).append(j)

    sent = 0
    for company in sorted(by_company):
        send_company_email(company, by_company[company])
        sent += 1
        time.sleep(1)  # gentle on SMTP
    print(f"Sent {sent} email(s), one per company.")


def format_posted(posted_at):
    """Human-readable relative time, e.g. 'posted 3h ago' or 'posted Jun 22'."""
    if not posted_at:
        return "post time unknown"
    try:
        dt = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = now - dt
        hours = delta.total_seconds() / 3600
        if hours < 1:
            return f"posted {int(delta.total_seconds() / 60)}m ago"
        if hours < 24:
            return f"posted {int(hours)}h ago"
        return f"posted {dt.strftime('%b %d')}"
    except (ValueError, TypeError):
        return "post time unknown"


def send_company_email(company, jobs):
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]
    to_addr = os.environ.get("ALERT_TO", user)

    n = len(jobs)
    # Sort best-match-first, posted-time as tiebreak
    jobs_sorted = sorted(
        jobs,
        key=lambda x: (x.get("match_score", 0), x.get("posted_at") or "", x["title"]),
        reverse=True,
    )

    label_colors = {
        "Made for you": "#15803d",
        "Good match": "#16a34a",
        "Average match": "#ca8a04",
        "Somewhat stretch": "#ea580c",
        "Time waste": "#9ca3af",
    }

    lines = [f"<h2>{n} new role(s) at {company}</h2><ul>"]
    for j in jobs_sorted:
        loc = f" &mdash; {j['location']}" if j["location"] else ""
        when = format_posted(j.get("posted_at", ""))
        label = j.get("match_label")
        tag = ""
        if label:
            color = label_colors.get(label, "#6b7280")
            tag = f' <b style="color:{color}">[{label}]</b>'
        lines.append(f'<li>{tag} <a href="{j["url"]}">{j["title"]}</a>{loc} &mdash; <i>{when}</i></li>')
    lines.append("</ul>")
    html = "\n".join(lines)

    # Subject leads with the best match label found in this batch, if any.
    newest_when = format_posted(jobs_sorted[0].get("posted_at", "")) if jobs_sorted else ""
    best_label = jobs_sorted[0].get("match_label") if jobs_sorted else None
    plural = "s" if n != 1 else ""
    prefix = f"[{best_label}] " if best_label else ""
    subject = f"{prefix}{n} new role{plural} at {company}"
    if newest_when:
        subject += f" ({newest_when})"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Job Scanner <{user}>"
    msg["To"] = to_addr
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(host, port) as server:
        server.starttls()
        server.login(user, password)
        server.sendmail(user, [to_addr], msg.as_string())
    print(f"  emailed {company}: {n} role(s)")


if __name__ == "__main__":
    main()