# Job Scanner

Monitors company career boards (Greenhouse, Lever, Ashby) for new ML/AI roles and
emails you **one email per company** whenever that company posts new matching
roles. Runs automatically on GitHub Actions, roughly every 5-15 minutes.

## How it meets the spec

- **One email per company**: if 3 companies post new roles in a cycle, you get 3
  emails, each listing only that company's new roles. Subject: `[Company] N new ML/AI role(s)`.
- **New-only**: diffs against prior runs (`seen_jobs.json`), so you never re-see a role.
- **High precision + recall**: two-tier title filter (see below). Scored P=1.0,
  R=1.0, F1=1.0 on a 44-title labeled test set including hard false-positive cases.
- **Runs ~24x7**: every 5 minutes via GitHub Actions cron.

## Latency reality (read this)

GitHub Actions' minimum cron is 5 minutes, but scheduled jobs on free shared
runners get queued under platform load, so actual cadence is ~5-15 min, sometimes
longer. You'll typically see a new role within 15 minutes of posting, far ahead of
job-board crowds. If you need guaranteed sub-10-min latency, run `scanner.py` on a
cheap always-on VM (DigitalOcean/EC2) with a real `*/5` cron instead. The code is
identical; only the host changes.

## Filter logic (drives the metrics)

A title is KEPT if EITHER:
- it contains a `strong_include` term (e.g. "machine learning", "applied scientist", "llm"), OR
- it contains a `context_term` ("model", "inference", "agent") AND a `role_word` ("engineer", "scientist")

...UNLESS it contains a `hard_exclude` term ("sales", "financial model", "intern", "director", etc.).

This catches odd-titled real roles (Member of Technical Staff, MLE, ML-suffixed
SWE roles) while rejecting cross-domain noise (financial modeling, sales AI, model
risk analyst). All terms live in `config.yml`; edit there, no code changes.

## One-time setup (~10 min)

### 1. Create the repo
New GitHub repo (public = free Actions). Push these files.

### 2. Gmail App Password
- Enable 2-Step Verification on your Google account
- Google Account -> Security -> App passwords -> generate one for "Mail"
- Copy the 16-character password

### 3. Repo secrets
Settings -> Secrets and variables -> Actions -> New repository secret:

| Secret    | Value                                |
|-----------|--------------------------------------|
| SMTP_HOST | smtp.gmail.com                       |
| SMTP_PORT | 587                                  |
| SMTP_USER | your.email@gmail.com                 |
| SMTP_PASS | 16-char app password from step 2     |
| ALERT_TO  | where alerts go (your email)         |

### 4. First run
Actions tab -> job-scanner -> Run workflow. **First run sends no email** (it seeds
the seen-list so you aren't blasted with existing jobs). Every run after emails only
new roles, per company. Then it runs every 5 min on its own.

## Testing it yourself

- `python test_metrics.py` -> prints precision/recall/F1 on the labeled title set.
  Add your own (title, label) rows to `LABELED` to test edge cases you care about.
- `python test_email_grouping.py` -> verifies per-company grouping + diff.

To dry-run the live fetch without email, run `python scanner.py` locally on your
laptop (open network). First run seeds state silently; touch a company's board or
wait for a real new posting to see an email fire.

## Tuning (all in config.yml)

- Add/remove companies under `greenhouse:` / `lever:` / `ashby:`
- Adjust roles via the keyword lists
- Restrict locations via `location_keywords` (empty = all; start empty)
- Want management roles? Remove `"manager, "` and `"director"` from `hard_exclude`
- Want internships? Remove `"intern"`

## Finding Workday tenant info (for the other ~130 companies)

Workday has no single global API like Greenhouse/Lever/Ashby. Each company runs
its own Workday "tenant," and there's no way to guess the three values needed.
You have to find them once per company, but it takes about 15 seconds each:

1. Go to the company's actual careers page and open any job listing.
2. Open browser dev tools (F12 or Cmd+Option+I), go to the **Network** tab.
3. Reload the page, filter requests by typing `jobs` in the filter box.
4. Find a request to a URL shaped like:
   `https://{tenant}.{wdN}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs`
5. Paste that exact URL to me, or fill in `config.yml` yourself:
   ```yaml
   - {tenant: nvidia, wd_host: wd5, site: NVIDIAExternalCareerSite, name: Nvidia}
   ```
   - `tenant` = the part right after `https://`
   - `wd_host` = the `wdN` part (wd1, wd3, wd5, etc.)
   - `site` = the part between the second `{tenant}` and `/jobs`

**Important: many large companies are NOT on Workday at all.** Google, Meta,
Apple, and Microsoft run fully custom in-house systems with no public API of
any kind. No amount of searching or scraping effort changes this; there is
nothing to find. Don't spend time looking for these.

If a Workday entry in the email's warning section says "0 jobs returned" or
"HTTP 4xx," the tenant/host/site combo is wrong, redo the dev-tools steps above.


Any board with a wrong slug or moved ATS appears in the run log as a WARN. Find the
real slug on the company careers page URL (`boards.greenhouse.io/<slug>`,
`jobs.lever.co/<slug>`, `jobs.ashbyhq.com/<slug>`) and fix `config.yml`. The
eval-space companies (Arize, Patronus, Galileo, Langfuse, Fiddler) are the most
likely to need this.
