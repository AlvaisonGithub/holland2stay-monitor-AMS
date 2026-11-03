# FlatRadar: Amsterdam fork

A self-hosted monitor for Dutch student and starter rental platforms. It watches **Holland2Stay**, **OurDomain** and **Xior**, and sends you a Telegram message (or email, web or push notification) as soon as a home becomes available to book or enters a lottery.

This repository is a fork of [**FlatRadar** by 751K](https://github.com/751K/holland2stay-monitor). It keeps the original app and adds what was needed to make monitoring work again from a cloud server in late 2026, after Holland2Stay rebuilt its website and the platforms started blocking cloud-server traffic.

> **Unofficial tool.** Not affiliated with, endorsed by or operated by Holland2Stay, OurDomain, Xior or any other platform it monitors. For personal, non-commercial use only. Respect each platform's terms and keep polling intervals reasonable.

---

## Credits

**FlatRadar was created by [751K](https://github.com/751K)** ([original repository](https://github.com/751K/holland2stay-monitor), [flatradar.app](https://flatradar.app)). Almost everything in this repository is their work: the multi-platform monitor, web dashboard, user and notification system, Telegram/email/push channels, map and calendar views, auto-booking, and the iOS and Android apps.

If this project is useful to you, please star and support the original: [GitHub Sponsors](https://github.com/sponsors/751K) · [flatradar.app](https://flatradar.app).

The full original documentation is kept in [`docs/README.md`](docs/README.md) ([中文](docs/README_cn.md)).

---

## What's new in this fork

### Holland2Stay monitoring restored
Holland2Stay rebuilt its site (and announced a rename to **Codomo**). The public GraphQL API the original scraper relied on is gone, and the site sits behind a Cloudflare check that the built-in browser (CloakBrowser) no longer passes from cloud servers.

- **Reads the residences page directly** through [ZenRows](https://www.zenrows.com/)' rendering browser, using the new city filter (`?city[filter]=Amsterdam,24`) and following pagination.
- **Understands the Dutch and English status labels** ("Direct boeken" → *Available to book*, "Loting" → *Available in lottery*, and so on).
- **Rent and "available from" date** are read from each listing's own page when the overview card doesn't show them. They are cached for 72 hours, so each listing page is only opened once.
- **Card details** filled in from the listing card: type, area, floor, energy label, occupancy.
- **Safety checks so it never pretends.** If the page loads but listings can't be read (wrong city, unknown labels, empty page), the round is reported as failed instead of "0 homes", so existing listings are never wrongly marked as gone.
- **Slow page loads are retried** in the same round. If the retry also fails, only that round is skipped: no 30-minute pause, and other platforms' results are kept.
- The old GraphQL path is still in the code (`H2S_TRY_GRAPHQL=true`), with [Bright Data](https://brightdata.com/) as an alternative provider.

### OurDomain through ZenRows / Bright Data
OurDomain's booking site (RENTCafe / SecureRC) blocks cloud-server IPs at the Cloudflare firewall. This fork fetches it through ZenRows, with Bright Data supported too. Floor-plan IDs are cached for 12 hours, and accounts without Bright Data business verification fall back automatically.

### Lower ZenRows credit usage
- **Cheap first:** each Holland2Stay page is first loaded without the premium proxy. The premium proxy is used only if Cloudflare blocks the cheap request, and that choice is remembered for 6 hours.
- **Night mode:** scans every 2 hours between 00:00 and 07:00 (Amsterdam time) instead of every 5 minutes. The last night wait ends at the window's end, so morning scans start on time.
- **No wasted calls** to the API addresses that no longer exist.

### Easier cloud deployment (Railway)
- Logs go to standard output, so they show up in Railway's log viewer.
- Fixes file permissions on Railway volumes at startup.
- Amsterdam defaults: Holland2Stay Amsterdam, OurDomain Diemen and South-East, Xior Karspeldreef and Naritaweg.

### Smaller fixes
- The dashboard no longer crashes on a user form error; it shows the error message instead.
- A stored Telegram token that can't be decrypted (after the encryption key changed) is treated as empty instead of breaking startup.
- Legal pages are read as UTF-8, which fixes Windows.
- Settings page: "select all" buttons are translated.
- Xior scraper adjustments.

### Not updated in this fork
- **Holland2Stay auto-booking** still uses the old site flow and is **untested** against the rebuilt website. Treat it as not working; notifications are unaffected.
- The **iOS / Android apps and desktop builds** are the original author's and have not been changed or tested here.

---

## How it works

```
Every few minutes (every 15–60 s during peak hours, every 2 h at night):

  Holland2Stay ─┐                        ┌─► Telegram / Email / Web / Push
  OurDomain   ──┼─► ZenRows (bypasses  ──┤
  Xior        ──┘   cloud-IP blocking)   └─► Web dashboard (list, map, calendar)
                         │
                         ▼
                  SQLite database: detects new listings and status changes
```

Each user in the dashboard has their own filters (city, max rent, area, type, energy label and more) and notification channels.

---

## What you need

| | Notes |
|---|---|
| **A server that runs 24/7** | [Railway](https://railway.com) (easiest, used for this fork), any VPS with Docker (≥ 1 GB RAM), or your own computer |
| **A ZenRows account** | Required for Holland2Stay and OurDomain on a cloud server. Get an API key at [app.zenrows.com](https://app.zenrows.com). See [Credit usage](#credit-usage) for what it costs. |
| **A Telegram bot** (recommended) | Create one with [@BotFather](https://t.me/BotFather) to get alerts on your phone |
| Bright Data account | Optional alternative or backup provider |

---

## Setup on Railway (recommended)

**1. Get the code.** Fork this repository to your own GitHub account.

**2. Create the service.** In Railway, choose **New Project → Deploy from GitHub repo** and pick your fork. Railway detects the `Dockerfile` and builds it.

**3. Add a volume** so your database survives redeploys: service → **Settings → Volumes** → mount path **`/app/data`**.

**4. Generate two secret keys** on your own computer (Python needed):

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"   # → DATA_ENCRYPTION_KEY
python -c "import secrets; print(secrets.token_hex(32))"                                       # → FLASK_SECRET
```

> These must be set as Railway variables. Otherwise a new key is generated on every deploy: saved Telegram tokens become unreadable and everyone gets logged out.

**5. Add variables** under service → **Variables**:

| Variable | Example | Purpose |
|---|---|---|
| `WEB_USERNAME` | `admin` | Dashboard login |
| `WEB_PASSWORD` | a strong password | Dashboard login. **Never leave empty on a public server.** |
| `H2S_SKIP_PREFLIGHT` | `1` | The startup check looks for the password in the `.env` file, not in Railway variables. Set this once `WEB_PASSWORD` is set above. |
| `DATA_ENCRYPTION_KEY` | from step 4 | Encrypts stored tokens and passwords |
| `FLASK_SECRET` | from step 4 | Keeps logins valid across restarts |
| `SESSION_COOKIE_SECURE` | `true` | Railway serves over HTTPS |
| `ZENROWS_API_KEY` | your key | Holland2Stay and OurDomain |
| `SOURCES` | `holland2stay,ourdomain,xior` | Which platforms to monitor |
| `CITIES` | `Amsterdam,24` | Holland2Stay cities as `Name,ID`, separated by `\|`. IDs are in `KNOWN_CITIES` in [`config.py`](config.py). |
| `OURDOMAIN_CITIES` | `Amsterdam Diemen,diemen\|Amsterdam South-East,south-east` | OurDomain locations |
| `XIOR_CITIES` | `Amsterdam Karspeldreef,p0196062\|Amsterdam Naritaweg,p0196102` | Xior buildings as `Name,key`. Keys are in `KNOWN_XIOR_CITIES` in [`config.py`](config.py). |

All other settings have sensible defaults; see [Configuration](#configuration).

**6. Make it reachable.** Service → **Settings → Networking → Generate Domain**, target port **`8088`**. Optionally set `PUBLIC_BASE_URL` to that address.

**7. Set up alerts.** Open your domain and log in. Go to **Users** → add yourself → choose **Telegram** and paste your bot token and chat ID. To find your chat ID, send your bot a message, then open `https://api.telegram.org/bot<TOKEN>/getUpdates`. Then set your filters (max rent, minimum area, cities…).

**8. Check the logs.** Monitoring starts automatically. In Railway's logs, a healthy Holland2Stay round looks like this:

```
H2S residences page 1/2 read via ZenRows (24.1s): 10 cards, 3 match city=['24'] status=[179, 336]
[Amsterdam] Holland2Stay 共抓取 3 条房源
```

> **Tip:** on Railway, the dashboard's **Settings** page writes to a `.env` file inside the container, which is reset on every redeploy. Put settings you want to keep in Railway **Variables**; they always take priority.

---

## Other ways to run it

**Docker on a VPS:** follow the original guide in [`docs/README.md`](docs/README.md#docker), using the `docker-compose.yml` and `Caddyfile` in this repo for HTTPS. Add `ZENROWS_API_KEY` and the variables above to `.env`.

**On your own computer** (Python 3.11+):

```bash
pip install -r requirements.txt
python -m cloakbrowser install   # only needed without ZenRows
cp .env.example .env             # then edit .env
python web.py                    # dashboard at http://127.0.0.1:8088
```

On a home internet connection, Cloudflare is less strict, but the rebuilt Holland2Stay site still needs ZenRows to be read with this fork's scraper.

---

## Configuration

Global settings live in environment variables (Railway Variables or `.env`); everything per user lives in the dashboard. Start from [`.env.example`](.env.example). Settings added by this fork:

| Variable | Default | What it does |
|---|---|---|
| `ZENROWS_API_KEY` | – | ZenRows key; enables the cloud scraping path |
| `ZENROWS_COUNTRY` | `nl` | Country for the premium proxy |
| `BRIGHTDATA_API_KEY` / `BRIGHTDATA_ZONE` | – / `web_unlocker1` | Optional Bright Data provider |
| `H2S_FETCH_MODE` | `auto` | `auto` (API if a key is set, else browser), `zenrows`, `brightdata` or `browser` |
| `H2S_DETAIL_MAX_PER_ROUND` | `5` | Maximum listing pages opened per round to fetch rent and dates |
| `H2S_TRY_GRAPHQL` | `false` | Re-enable the old GraphQL attempts |
| `NIGHT_INTERVAL` | `7200` | Seconds between scans at night. `0` = no night mode (normal interval all night) |
| `NIGHT_START` / `NIGHT_END` | `00:00` / `07:00` | Night window in Amsterdam time; may cross midnight |
| `CHECK_INTERVAL` | `300` | Normal interval in seconds |
| `PEAK_INTERVAL` / `MIN_INTERVAL` | `60` / `15` | Faster, adaptive polling during peak windows |
| `PEAK_START` / `PEAK_END` (`_2`) | `08:30`–`10:00`, `13:30`–`15:00` | Peak windows (weekdays) |

---

## Credit usage

Most ZenRows cost comes from Holland2Stay, because each page is rendered in a real browser. Roughly:

- **Per round:** one rendered page per Holland2Stay city page (about 10 listings per page), plus small requests for OurDomain.
- **Per new listing:** one extra rendered page to read its rent and date, cached for 72 hours.
- **Ways to save:** fewer cities, a longer `CHECK_INTERVAL` or `PEAK_INTERVAL`, a longer `NIGHT_INTERVAL`, or a lower `H2S_DETAIL_MAX_PER_ROUND`.

Check your real usage in the ZenRows dashboard after a day and adjust.

---

## Troubleshooting

| Log message | Meaning |
|---|---|
| `H2S page 1/N read … M match` | Working. `M` = listings that are available to book or in lottery. |
| `listings not rendered yet … retrying` | Slow page load, retried automatically. Fine if occasional. |
| `H2S 本轮跳过（页面未渲染完成），下轮重试` | Both tries were too slow; skipped this round only. If it happens most rounds, open an issue. |
| `H2S: premium proxy needed — using it for the next 6h` | Cloudflare blocked the cheap mode; the premium proxy is used for now. |
| `city filter not applied` / `no status label recognised` | Holland2Stay changed its page again; the scraper needs an update. |
| `ZenRows 402` | Out of ZenRows credits. |
| `Telegram token 无法解密` | `DATA_ENCRYPTION_KEY` changed. Set it as a fixed variable (setup step 4) and re-enter the token. |

---

## License

Distributed under the original project's **[PolyForm Noncommercial License 1.0.0](LICENSE)**: free for personal and other non-commercial use; **no commercial use**. Changes in this fork are released under the same license.

Original work © [751K](https://github.com/751K) ([holland2stay-monitor](https://github.com/751K/holland2stay-monitor)). Fork changes by [AlvaisonGithub](https://github.com/AlvaisonGithub).
