# JobRadar

A personal job-search dashboard for US remote Site Reliability Engineer, DevOps and Platform Engineer roles posted less than 24 hours ago. Runs in Docker with a browser interface, a background scanner and persistent application history.

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

The bundled directory is an August 2026 snapshot. Once daily, the scanner merges current Greenhouse/Ashby addresses marked live in the public [ATS directory](https://github.com/moonie0201/ats-directory). A validated local cache survives upstream failures and container restarts. This registry is not exhaustive, and its upstream maintenance frequency is outside our control. Workday, Lever and other unsupported platforms are not scanned. Sources may limit or delay their feeds. No promise is made that you will be first to apply.

Missing employer boards (HTTP 404/410) are persisted as **unavailable** and skipped for seven days, separately from actual connection failures. Transient failures back off from 15 minutes to a day; HTTP 429 respects Retry-After. Jobicy is never automatically polled more than hourly, including after failures. Registry-listed alternate Greenhouse/Ashby addresses with the same slug are checked before being recorded and prioritized; at most five recovery attempts run per round. Arbitrary renamed slugs cannot always be linked to the original company automatically. Existing job and application records are retained.

The dashboard reports successful requests, deferred sources, unavailable boards, connection failures and how many saved matches were excluded by the freshness filter or application status. Title matching includes variants such as “Software Engineer, Platform.” Country-specific Jobicy postings outside the supported US/worldwide regions are excluded.

## Freshness and notifications

Discovery includes only a source timestamp with an age from zero to strictly less than 24 hours. Unknown, invalid and future dates are excluded. Greenhouse uses first publication; Ashby uses **last publication**, which may be a repost; feeds use their supplied timestamps. First discovery is not substituted for publication time.

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
