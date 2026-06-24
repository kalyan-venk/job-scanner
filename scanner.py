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
import re
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

    Listings endpoint (POST) gives title/location/path only, no JD text.
    Full JD text requires a SEPARATE per-job GET call - see fetch_workday_jd().
    We only call that for genuinely NEW jobs (in main()), not on every listing
    fetch, to keep request volume sane on a 5-minute schedule.
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
            "_wd_tenant": tenant,
            "_wd_host": wd_host,
            "_wd_site": site,
            "_wd_path": path,
        })
    return out


def fetch_workday_jd(job):
    """
    Fetch full JD text for a single Workday job. Only called for NEW jobs,
    one GET request per job, to avoid hammering Workday's API (which sits
    behind Akamai bot protection) on every 5-minute scan.
    """
    tenant = job.get("_wd_tenant")
    wd_host = job.get("_wd_host")
    site = job.get("_wd_site")
    path = job.get("_wd_path")
    if not all([tenant, wd_host, site, path]):
        return ""
    url = f"https://{tenant}.{wd_host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/job{path}"
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        posting = data.get("jobPostingInfo", {})
        return strip_html(posting.get("jobDescription", ""))
    except Exception:
        return ""


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

def extract_min_years_required(text):
    """
    Finds patterns like '6+ years', '6-8 years', 'minimum of 7 years',
    '7 yrs experience' and returns the highest MINIMUM years figure found,
    or None if no such pattern exists. Used to penalize JDs asking for
    meaningfully more experience than Kalyan has (~4 yrs; 5 is fine).
    """
    patterns = [
        r"(\d{1,2})\s*\+\s*years?",                          # "6+ years"
        r"(\d{1,2})\s*-\s*\d{1,2}\s*years?",                 # "6-8 years" (take lower bound)
        r"minimum\s+of\s+(\d{1,2})\s*years?",                # "minimum of 7 years"
        r"at\s+least\s+(\d{1,2})\s*years?",                  # "at least 7 years"
        r"(\d{1,2})\s*\+?\s*yrs?\b",                         # "7 yrs" / "7+ yrs"
        r"(\d{1,2})\s*years?\s+of\s+experience",             # "5 years of experience" (bare)
        r"(\d{1,2})\s*years?\s+(?:relevant\s+)?experience",  # "5 years experience" / "5 years relevant experience"
    ]
    found = []
    for pat in patterns:
        for m in re.finditer(pat, text):
            try:
                found.append(int(m.group(1)))
            except (ValueError, IndexError):
                continue
    found = [n for n in found if 1 <= n <= 25]
    return max(found) if found else None


def has_cpp_or_js_requirement(text):
    """
    Detects explicit C++ or JavaScript requirement language. Requires the
    language name to appear near requirement-flavored words, to avoid
    false positives on JDs that merely list it among many nice-to-haves.
    """
    lang_pattern = r"(c\+\+|javascript|typescript|node\.?js|react\.?js)"
    require_pattern = r"(required|requirement|must have|proficien|expert|strong experience|years? of experience)"
    for lang_match in re.finditer(lang_pattern, text):
        window = text[max(0, lang_match.start() - 80): lang_match.end() + 80]
        if re.search(require_pattern, window):
            return True
    return False


US_STATE_ABBR = {
    "al","ak","az","ar","ca","co","ct","de","fl","ga","hi","id","il","in","ia",
    "ks","ky","la","me","md","ma","mi","mn","ms","mo","mt","ne","nv","nh","nj",
    "nm","ny","nc","nd","oh","ok","or","pa","ri","sc","sd","tn","tx","ut","vt",
    "va","wa","wv","wi","wy","dc",
}

NON_US_SIGNALS = [
    "india", "bangalore", "bengaluru", "hyderabad", "pune", "mumbai", "delhi",
    "london", "uk", "united kingdom", "england", "germany", "berlin", "munich",
    "france", "paris", "canada", "toronto", "vancouver", "ontario",
    "singapore", "japan", "tokyo", "china", "shanghai", "beijing",
    "australia", "sydney", "melbourne", "netherlands", "amsterdam",
    "ireland", "dublin", "spain", "madrid", "italy", "milan", "poland",
    "brazil", "mexico", "emea", "apac", "israel", "tel aviv",
]


def is_likely_non_us(location):
    """
    Conservative non-US detector: only flags when a recognizable non-US
    signal is present. Defaults to NOT flagging (assume US) when ambiguous,
    since a false 'likely non-US' tag on a real US role is worse than
    occasionally missing the tag.
    """
    loc = (location or "").lower()
    if not loc:
        return False
    return any(sig in loc for sig in NON_US_SIGNALS)


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

    # Years-of-experience penalty: only penalize when the JD asks for MORE
    # than what's acceptable. 5 years is fine (1 year gap doesn't matter);
    # 6+ is the real problem. Penalty scales slightly with how far over.
    yrs_cfg = scoring.get("experience_penalty", {})
    max_ok_years = yrs_cfg.get("max_acceptable_years", 5)
    yrs_weight = yrs_cfg.get("weight_per_year_over", -4)
    min_years = extract_min_years_required(text)
    if min_years is not None and min_years > max_ok_years:
        over_by = min_years - max_ok_years
        score += yrs_weight * over_by
        matched.append(f"[penalty: asks {min_years}+ yrs experience]")

    # C++/JS requirement penalty: explicit requirement language near the term.
    lang_cfg = scoring.get("language_requirement_penalty", {})
    lang_weight = lang_cfg.get("weight", -4)
    if lang_cfg.get("enabled", True) and has_cpp_or_js_requirement(text):
        score += lang_weight
        matched.append("[penalty: explicit C++/JS requirement]")

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

    # Workday jobs need a SEPARATE per-job fetch for JD text (the listing
    # endpoint doesn't include it). Only do this for genuinely new jobs.
    for j in new_jobs:
        if j["id"].startswith("wd:") and "jd_text" not in j:
            j["jd_text"] = fetch_workday_jd(j)
            time.sleep(0.3)

    # Score each new job against your resume signals (free, keyword-based).
    for j in new_jobs:
        label, score, hits = score_jd(j, cfg)
        j["match_label"] = label
        j["match_score"] = score
        j["match_hits"] = hits
        j["likely_non_us"] = is_likely_non_us(j.get("location", ""))

    # Drop internal Workday helper fields before persisting/emailing.
    for j in matched:
        for k in ["_wd_tenant", "_wd_host", "_wd_site", "_wd_path"]:
            j.pop(k, None)

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


def build_open_all_link(jobs_in_category):
    """
    Gmail strips <script> tags and blocks javascript: links in email bodies,
    so a true one-click 'open all tabs from inside the email' isn't possible.
    Workaround: a data:text/html URL containing a tiny self-contained page
    with one button that opens every job URL in a new tab via window.open.
    Clicking the email link opens this page (one extra click), then the
    button on that page opens everything else.
    """
    import urllib.parse
    urls = [j["url"] for j in jobs_in_category if j.get("url")]
    if not urls:
        return None
    buttons_js = ",".join(f'"{u}"' for u in urls)
    page = f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="font-family:sans-serif;padding:40px;text-align:center;">
<h2>{len(urls)} job(s) ready to open</h2>
<button onclick='var u=[{buttons_js}];u.forEach(function(x){{window.open(x,"_blank");}});'
style="font-size:18px;padding:14px 28px;cursor:pointer;background:#16a34a;color:white;border:none;border-radius:8px;">
Open all {len(urls)} jobs in new tabs
</button>
<p style="color:#888;margin-top:20px;font-size:13px;">Your browser may ask to allow pop-ups for this page the first time.</p>
</body></html>"""
    encoded = urllib.parse.quote(page)
    return f"data:text/html,{encoded}"


def send_company_email(company, jobs):
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]
    to_addr = os.environ.get("ALERT_TO", user)

    n = len(jobs)
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
    label_order = ["Made for you", "Good match", "Average match", "Somewhat stretch", "Time waste"]

    by_label = {}
    for j in jobs_sorted:
        by_label.setdefault(j.get("match_label") or "Unscored", []).append(j)

    lines = [f"<h2>{n} new role(s) at {company}</h2>"]

    # Per-category "open all" link, only shown when a category has 2+ jobs
    # (single-job categories don't need it) and only when there's more than
    # one category total (single-category emails don't need the button either).
    if len(by_label) > 1:
        lines.append('<div style="margin-bottom:16px;">')
        for label in label_order:
            group = by_label.get(label)
            if group and len(group) > 1:
                link = build_open_all_link(group)
                color = label_colors.get(label, "#6b7280")
                if link:
                    lines.append(
                        f'<a href="{link}" style="display:inline-block;margin:4px 8px 4px 0;'
                        f'padding:6px 12px;background:{color};color:white;border-radius:6px;'
                        f'text-decoration:none;font-size:13px;">Open all {len(group)} [{label}]</a>'
                    )
        lines.append("</div>")

    for label in label_order:
        group = by_label.get(label)
        if not group:
            continue
        color = label_colors.get(label, "#6b7280")
        lines.append(f'<p style="color:{color};font-weight:bold;margin:14px 0 4px;">[{label}]</p><ul>')
        for j in group:
            loc = f" &mdash; {j['location']}" if j["location"] else ""
            when = format_posted(j.get("posted_at", ""))
            non_us = ' <span style="color:#9ca3af;">[likely non-US]</span>' if j.get("likely_non_us") else ""
            lines.append(f'<li><a href="{j["url"]}">{j["title"]}</a>{loc}{non_us} &mdash; <i>{when}</i></li>')
        lines.append("</ul>")

    # Any jobs with no label (scoring disabled/unavailable) at the end
    if "Unscored" in by_label:
        lines.append('<p style="color:#6b7280;font-weight:bold;margin:14px 0 4px;">[Unscored]</p><ul>')
        for j in by_label["Unscored"]:
            loc = f" &mdash; {j['location']}" if j["location"] else ""
            when = format_posted(j.get("posted_at", ""))
            lines.append(f'<li><a href="{j["url"]}">{j["title"]}</a>{loc} &mdash; <i>{when}</i></li>')
        lines.append("</ul>")

    html = "\n".join(lines)

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