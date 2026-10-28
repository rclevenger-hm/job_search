# JobRadar

A personal job-search dashboard for US remote Site Reliability Engineer, DevOps, Platform Engineer and Forward Deployed Engineer roles posted less than 24 hours ago. Runs in Docker with a browser interface, a background scanner and persistent application history.

## Run

Install/start Docker with Linux containers, then:

```sh
git clone https://github.com/rclevenger-hm/job_search.git
cd job_search
docker compose up -d --build
```

Open **http://localhost:8080**. The dashboard is bound to localhost by default; it has no login and is intended for one person on their own machine. Do not publish it to the internet without adding authentication and HTTPS.

```sh
docker compose logs -f
docker compose ps
docker compose down
```

The named `jobradar-data` volume retains jobs, settings, scan progress and applications after `down` and rebuilds. **`docker compose down -v` deletes that volume.** The container restarts with Docker unless explicitly stopped. Scanning continues with the browser closed, but stops when the computer sleeps or Docker stops.

Set `JOBRADAR_PORT` in a local `.env` file to change the host port. Set `JOBRADAR_SCAN_ENABLED=false` to run the dashboard without live polling. Do not run multiple replicas against the same volume.

## What it searches

- A bundled directory of **7,821** Greenhouse and Ashby employer boards, in rotating batches of 150 by default.
- Remote OK, the full We Work Remotely RSS feed, Arbeitnow's German and UK feeds, and Jobicy (hourly, with cursor pagination through the fresh results, capped at 20 pages).
- Optional priority Greenhouse/Ashby employers checked each round. These lists do **not** restrict broad search.
- Optional read-only LinkedIn alert email via IMAP, or manual `.eml` import.

Settings control the interval (minimum five minutes), directory batch size and priority employers. A complete directory sweep takes 53 rounds at the default batch size: roughly 4.5 hours plus request time. Five worker threads keep requests bounded. The progress display and error list show exactly what has been checked. Unavailable boards and failed requests follow the persistent retry schedule below.

The bundled directory is an August 2026 snapshot. Once daily, the scanner merges current Greenhouse/Ashby addresses marked live in the public [ATS directory](https://github.com/moonie0201/ats-directory). A validated local cache survives upstream failures and container restarts. This registry is not exhaustive, and its upstream maintenance frequency is outside our control. Workday and other unsupported platforms are not scanned. Sources may limit or delay their feeds. No promise is made that you will be first to apply.

Missing employer boards (HTTP 404/410) are persisted as **unavailable** and skipped for 30 days, separately from actual connection failures. Transient failures back off from 15 minutes to a day; HTTP 429 respects Retry-After. Jobicy is never automatically polled more than hourly, including after failures. Reviewed endpoint migrations are loaded from `board-audit.json` and replace old addresses in the directory and priority scans. The app no longer guesses migrations from matching slugs, which can belong to unrelated employers. Unresolved addresses remain visible in a separate skipped list; they are not described as repaired. Existing job and application records are retained.

The dashboard reports successful requests, deferred sources, unavailable boards, connection failures and how many saved matches were excluded by the freshness filter or application status. Title matching includes variants such as “Software Engineer, Platform.” Country-specific Jobicy postings outside the supported US/worldwide regions are excluded.

## Freshness and notifications

The default fresh view includes only a source timestamp with an age from zero to strictly less than 24 hours. Unknown, invalid and future dates are excluded. Greenhouse uses first publication; Ashby uses **last publication**, which may be a repost; feeds use their supplied timestamps. First discovery is not substituted for publication time.

The UI separates available matches from new alerts. First successful source scans establish a baseline without generating a flood of alerts. Known jobs are deduplicated within a source, not across different sources. Keyword location rules are approximate; review state restrictions, work authorization and timezone requirements on the employer's page.

Click **Enable desktop alerts** and grant browser permission. Keep the dashboard tab open for notifications. The browser polls every ten seconds and may throttle background tabs. This is not a Windows service sending native notifications while your browser is closed. The scanner continues independently in the container.

## Application history

Use **Record application** on a job or **Add application** under Applications. Save the date, notes and stage (Applied, Interviewing, Offer, Rejected, Withdrawn). Dates may be left blank when unknown. Applying is manual: opening a link does not record an application automatically. History remains available after jobs age out of discovery. Application data is not uploaded to GitHub.

## LinkedIn email

Create LinkedIn email job alerts for your titles and US remote preferences, then route them to a dedicated mail folder. In Sources & settings, enter your IMAP host, username, folder and provider app password. The connector uses TLS on port 993, a read-only mailbox and `BODY.PEEK`. It checks at most 50 messages from the last seven days. Passwords remain in process memory and must be reentered after restart. Full email bodies are not saved. OAuth-only mailboxes are not supported.

LinkedIn email templates do not supply verified posting timestamps to this parser. **Their links are saved but excluded by the strict 24-hour discovery filter.** This is not a LinkedIn website scraper, and email delivery is not an instant job feed.

## Import existing desktop data

The old desktop app and this container share the SQLite schema. Close the desktop app before making a database copy, and stop the container before replacing its database. Keep a backup of both databases. Copy `jobs.sqlite3` into `/data/jobs.sqlite3` and ensure UID/GID `10001:10001` owns it. Import into a fresh volume to avoid stale SQLite WAL files. Never commit a personal database, email settings or password to this repository.

## Development and verification

```sh
python -m venv .venv
# Activate the virtual environment for your shell.
pip install -r requirements.txt
python -m unittest discover -s tests -v
python web.py
```

The Flask application is served by Waitress, not Flask's development server. A single process owns one scanner thread; HTTP requests use separate SQLite connections. Docker runs as non-root with a health check. Browser forms use CSRF tokens; templates escape job and application text. The database lives outside the image in `/data`.

## Attribution

The employer directory is adapted from [LastRound AI's ATS Company Directory](https://github.com/fyrosofttech/lastroundai-hiring-data/tree/main/ats-directory), companion to the [Job Posting Pay Transparency Study](https://lastroundai.com/blog/salary-transparency-study-2026), licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). This version retains Greenhouse/Ashby rows, validates tokens, removes duplicates and reorders records.

Job listings link back to their original sources: [Remote OK](https://remoteok.com), [We Work Remotely](https://weworkremotely.com/remote-job-rss-feed), [Arbeitnow](https://www.arbeitnow.com/blog/job-board-api), [Greenhouse](https://docs.greenhouse.io/job-board.html) and [Ashby](https://developers.ashbyhq.com/docs/public-job-posting-api).

Additional listings: [Jobicy](https://jobicy.com), following its [public API guidelines](https://github.com/Jobicy/remote-jobs-api). Additional employer addresses: [moonie0201/ats-directory](https://github.com/moonie0201/ats-directory), CC0-1.0; only valid supported-provider tokens marked live are retained. Directory contents are data, never executable instructions or arbitrary request URLs.

## Additional sources and browsing

Browse all jobs removes the 24-hour cutoff while preserving title, eligibility and application-status filters. Older and undated saved matches appear in pages of 50. Alerts remain restricted to verified timestamps under 24 hours.

- **Lever:** public employer boards are merged from the daily directory and scanned in the rotating batch. Optional Lever priority tokens are available in settings. Only explicit remote roles with US or unspecified country are considered. Creation dates are displayed as creation dates, not publication times; these records are browse-only. Dead boards use the same persistent quarantine.
- **Himalayas:** searches the three target role phrases with the US country filter, up to five pages per phrase, once daily. Last-updated timestamps are displayed separately and never trigger fresh alerts. Expired listings and records not refreshed within a day are hidden. This is bounded coverage, not a full mirror. Applications retain the supplied Himalayas links. [API documentation](https://himalayas.app/docs/remote-jobs-api).
- **Remotive:** fetches its public active-jobs feed every six hours and filters titles locally. Its 24-hour-delayed records are browse-only; supplied publication dates are displayed without inventing a timezone. Records are hidden if not refreshed within six hours. [API documentation and attribution](https://github.com/remotive-com/remote-jobs-api).
- **USAJOBS:** optional hourly searches with `RemoteIndicator=True`, up to five pages per role phrase. Obtain a key from the [developer portal](https://developer.usajobs.gov/apirequest/index), then enter the registered email and key in Sources & settings. UI-entered keys persist in `/data/usajobs-credential.json`, a mode-0600 file inside the Docker volume (not encrypted). Blank key fields preserve the key; Disconnect saves an empty override, including for environment credentials. Settings shows last attempt/success, result and exclusion counts, and a connection-check button. Known timezone-qualified application deadlines expire listings; a missed refresh does not. Unknown deadlines require manual verification. Alternatively, set `USAJOBS_EMAIL` and `USAJOBS_API_KEY` in the ignored local `.env` file, then recreate the container. Never commit that file. Publication dates without timezone information are browse-only. Citizenship and hiring-path eligibility must still be checked on the announcement.

Feed polling intervals also apply to failure backoff. Provider timestamps are never replaced by discovery time. Source-specific expiry hides listings without deleting application history. Jobs are still deduplicated only within each source.

## Endpoint audit — October 25, 2026

All 724 supplied unavailable endpoints returned HTTP 404 during verification. Cross-platform endpoint checks and employer metadata/career-page review produced 57 replacement mappings, including renamed boards Hightouch → `hightouch-inc`, Atomic Semi → `fab2`, and Interface.ai → `interface-ai`. The remaining 667 have no verified replacement and receive a 30-day cooldown. This is not evidence that those employers have closed or stopped hiring.

`board-audit.json` records each original endpoint, its resolution, and verification source. `board_repairs.py` validates the mappings and applies each audit version once; restarting the container does not extend the retry dates. Verified replacement endpoints use normal request health/backoff. Existing job and application records are preserved. The dashboard distinguishes verified endpoint updates from unresolved skipped endpoints. Previously inferred same-slug aliases are cleared, including mismatches such as Serif.ai versus Serif Biomedicines.

## Curated company monitor

Open **Company watchlist** at `/monitor`. Canonical, Automattic, GitLab, Grafana Labs and Cloudflare are enabled by default; Verrus is an optional example and its office-based SRE posting is excluded. Selection persists across restarts. Disabling watchlist priority does not remove the company from broad directory coverage.

Enabled companies are checked hourly. Canonical and Automattic are read through their official careers-page data; job detail pages supply JobPosting publication dates and eligibility. Verrus uses matching careers-page links and explicit role-level remote statements. GitLab, Grafana Labs and Cloudflare use their public employer APIs. These share existing job IDs and source schedules to avoid duplicate records or duplicate polling in a scan round.

Direct page monitoring follows at most 20 matching job links per company and restricts requests and redirects to the reviewed company's HTTPS host. Embedded JSON is parsed without executing scripts. Removed detail pages (404/410) are skipped; other errors are displayed in the watchlist. Direct-page records expire from display unless refreshed within 24 hours, without deleting application history. Matching publication timestamps are preserved; missing or timezone-ambiguous dates stay browse-only. Application instructions distinguish email, online forms, or directions on the employer's page. No applications or email are sent automatically.

The watchlist shows the last check, matching-role count and errors. Zero matches means no roles met the current title and US-remote filters, not that the employer has no openings. The curated catalog and source-specific extraction live in `curated.py`; additional employers require a reviewed connector entry rather than arbitrary URL crawling.

## Resume workspace and AI handoff

Open `/resumes` to create named resume drafts with contact details, summary, skills, experience, education, and projects. Drafts persist in the SQLite database on the Docker volume. Save before downloading DOCX/PDF or exporting for refinement. Saved drafts can be edited and deleted; deletion requires confirmation in the browser.

Resume refinement and each job listing's Review in ChatGPT button open an editable prompt. Copy it into Codex, or use Copy & open ChatGPT and paste there. No account API integration or automatic submission is involved; personal content is never placed in a ChatGPT URL. A text download is also available. Job reviews include saved listing metadata and the original link, not a scraped full description.
