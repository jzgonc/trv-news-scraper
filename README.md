# News Scraper

Scrapes all news articles from the AppPack news widget embedded on trv-fidelia.de and saves each one into its own directory (rich-text HTML body, metadata, original-quality images and a print PDF). On a server it runs every 30 minutes. For every **new** article it:

1. uploads it to a Nextcloud public share (`App-News/<date>_<slug>/`),
2. emails the Redaktion with the Nextcloud link,
3. emails the office with the print PDF attached.

**Edited** articles (same id, changed title/text/images) are re-generated and overwritten in Nextcloud. No emails are sent for edits.

```text
<DATA_DIR>/articles/<date>_<title-slug>/
  article.html    # title, date, author + rich-text body
  article.pdf     # A4 print version (1 page, at most 2)
  metadata.json   # id, title, author, dates, category, content_hash, delivery status
  images/         # attached images at original quality
```

Code: `scraper.py` (scraping and run logic), `pdf.py` + `templates/` + `fonts/` (print PDF, Raleway font, SIL OFL), `nextcloud.py` (WebDAV upload), `mailer.py` (SMTP), `config.py` (`.env`).

## Behaviour

- **New article** = unknown id. It is processed once `publishedDate` is at least `MIN_AGE_MINUTES` (default 60) old.
- **Delivery status** is stored per article in `metadata.json` under `"delivery"`. Steps run in this order: Nextcloud → Redaktion mail → office mail. If a step fails, the remaining steps wait and are retried on the next run. Steps that already succeeded are never repeated.
- **Failures** never stop the other articles. The exit code is non-zero, and `MAIL_TO_ADMIN` gets one summary mail per new set of failures, not one every 30 minutes.
- **PDF layout**: the text is never cut. Grid images are dropped first to fit 1 page. The lead image is shrunk, and removed only if that is the only way to stay within 2 pages.
- A normal run refuses to start until `--baseline` has been run once, so a fresh install never mails every article that is already online.

| Command | Purpose |
| --- | --- |
| `scraper.py` | normal run (what the timer does) |
| `scraper.py --dry-run` | everything except upload and mails; prints what would be sent |
| `scraper.py --test-to you@example.org` | uploads the newest saved article and sends both mails to you; delivery status is not changed |
| `scraper.py --baseline` | saves everything online and marks it delivered (first server run only) |

Local run (Mac): `conda run -n scraper python scraper.py --dry-run` (needs `pip install -r requirements.txt` or conda-forge `weasyprint pillow`).

## Server setup (Ubuntu, Docker)

### 1. Prerequisites

Docker Engine with the compose plugin (already present for Collabora):

```bash
docker compose version
```

### 2. Get the project

Create a read-only **deploy key** for this repo (as your normal user, not root):

```bash
ssh-keygen -t ed25519 -f ~/.ssh/trv_deploy -N "" -C "trv-news-scraper deploy key"
cat >> ~/.ssh/config <<'EOF'
Host github-trv
  HostName github.com
  User git
  IdentityFile ~/.ssh/trv_deploy
  IdentitiesOnly yes
EOF
cat ~/.ssh/trv_deploy.pub
```

On GitHub: repo → **Settings** → **Deploy keys** → **Add deploy key** → paste the output. Leave "Allow write access" **unticked**.

Clone into a folder owned by you, so `git` runs without sudo and uses your key:

```bash
sudo mkdir -p /opt/trv-news-scraper && sudo chown $USER: /opt/trv-news-scraper
git clone git@github-trv:<owner>/<repo>.git /opt/trv-news-scraper
cd /opt/trv-news-scraper
mkdir -p data && sudo chown 1000:1000 data            # container runs as uid 1000
```

Use the `github-trv` host alias exactly as shown, not `github.com`, so this key is used.

### 3. Create the Nextcloud share link

In the external Nextcloud: create a folder (e.g. `TRV`) → **Share** → **Share link** → open the link's settings:

- Permission **"Allow upload and editing"** (not "Read only", not "File drop")
- optionally **Set password**
- Save, then copy the link (`https://cloud.example.org/s/<token>`)

The `App-News/` subfolder is created automatically (name set by `NEXTCLOUD_ARTICLES_FOLDER`).

### 4. Fill in `.env`

```bash
cp .env.example .env && chmod 600 .env && nano .env
```

Set the SMTP values, the three recipients, and `NEXTCLOUD_SHARE_URL` / `NEXTCLOUD_SHARE_PASSWORD`. Put passwords in `'single quotes'`.

### 5. Build the image

```bash
sudo docker compose build
```

### 6. Test run with `--dry-run`

```bash
sudo docker compose run --rm scraper --dry-run
```

This downloads all articles into `data/articles/` and creates the PDFs, then prints the upload targets and both mails for each one. Nothing is sent. Check a few PDFs, for example `data/articles/*/article.pdf`.

### 7. Single test upload + mail

```bash
sudo docker compose run --rm scraper --test-to you@example.org
```

This uploads the newest article to Nextcloud (`App-News/...`) and sends **both** mails to you only, marked `[TEST]`. Check the link in the first mail and the PDF attachment in the second. You can delete the test folder in Nextcloud afterwards.

### 8. Baseline (once)

```bash
sudo docker compose run --rm scraper --baseline
```

This marks everything currently online as delivered. No uploads, no mails.

### 9. Enable the timer

```bash
sudo cp deploy/trv-news-scraper.service deploy/trv-news-scraper.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now trv-news-scraper.timer
systemctl list-timers trv-news-scraper.timer
```

If the project is not in `/opt/trv-news-scraper`, change `WorkingDirectory=` in the `.service` file.

### 10. Check logs

```bash
journalctl -u trv-news-scraper -n 100 --no-pager      # last runs
journalctl -u trv-news-scraper -f                     # follow
sudo systemctl start trv-news-scraper                 # run now
```

### 11. Update

```bash
cd /opt/trv-news-scraper
git pull                    # no sudo: uses your deploy key
sudo docker compose build
```

The timer picks up the new image on its next run. `data/` and `.env` are kept.

### 12. Change recipients

Edit `MAIL_TO_REDAKTION`, `MAIL_TO_OFFICE` or `MAIL_TO_ADMIN` in `.env` (several addresses: comma-separated). No rebuild is needed; the next run uses the new values.

## Troubleshooting

- **Re-send an article**: in its `metadata.json`, set the step under `"delivery"` to `false`. The next run repeats only that step.
- **Nextcloud HTTP 401/404**: check the password and that the link allows upload and editing. Set `NEXTCLOUD_DAV=legacy` or `new` to skip auto-detection.
- **Mail errors**: port 587 needs `SMTP_SECURITY=starttls`, port 465 needs `ssl`. Some providers require `SMTP_FROM` to match `SMTP_USER`.
