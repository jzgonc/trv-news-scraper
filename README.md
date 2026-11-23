# News Scraper

Scrapes all news articles from the AppPack news widget embedded on trv-fidelia.de and saves each one into its own directory (rich-text HTML body, metadata, original-quality images and a print PDF). On a server it runs every 30 minutes. For every **new** article it:

1. uploads it to a Nextcloud public share (`App-News/<date>_<slug>/`),
2. emails the Redaktion with the Nextcloud link,
3. emails the office with the print PDF attached.

**Edited** articles (same id, changed title/text/images) are re-generated and overwritten in Nextcloud. No emails are sent for edits.

**Newsletters**: the sending mailbox is subscribed to the club newsletter (CleverReach). Each run reads that mailbox via IMAP (read-only; nothing is marked, moved or deleted). For every new mail from `NEWSLETTER_FROM` it:

1. saves the full newsletter as HTML and a print PDF with only the content (no header, no footer, no "Anstehende Termine", images inline),
2. uploads both to Nextcloud (`Newsletter/<date>_<slug>/`),
3. emails the office with the print PDF attached (board section "Aktuelles"). No Redaktion mail.

```text
<DATA_DIR>/articles/<date>_<title-slug>/
  article.html    # title, date, author + rich-text body
  article.pdf     # A4 print version (1 page, at most 2)
  metadata.json   # id, title, author, dates, category, content_hash, delivery status
  images/         # attached images at original quality

<DATA_DIR>/newsletters/<date>_<title-slug>/
  newsletter.html # full newsletter, local images, without tracking pixels and personal unsubscribe link
  newsletter.pdf  # A4 print version (content only)
  newsletter.eml  # original mail (not uploaded)
  metadata.json   # id (= Message-ID), title, date, delivery status
  images/
```

Code: `scraper.py` (scraping and run logic), `newsletter.py` (IMAP, newsletter extraction), `pdf.py` + `templates/` + `fonts/` (print PDF, Raleway font, SIL OFL), `nextcloud.py` (WebDAV upload), `mailer.py` (SMTP), `config.py` (`.env`), `common.py` (shared helpers).

## Behaviour

- **New article** = unknown id. It is processed once `publishedDate` is at least `MIN_AGE_MINUTES` (default 60) old.
- **Delivery status** is stored per article in `metadata.json` under `"delivery"`. Steps run in this order: Nextcloud → Redaktion mail → office mail. If a step fails, the remaining steps wait and are retried on the next run. Steps that already succeeded are never repeated.
- **Failures** never stop the other articles. The exit code is non-zero, and `MAIL_TO_ADMIN` gets one summary mail per new set of failures, not one every 30 minutes.
- **PDF layout**: the text is never cut. Grid images are dropped first to fit 1 page. The lead image is shrunk, and removed only if that is the only way to stay within 2 pages.
- **PDF colors**: the category label and the line under the header use the category's color from `templates/categories.json` (matched by name). Categories not listed there get the `Allgemein` colors and a warning in the log.
- A normal run refuses to start until `--baseline` has been run once, so a fresh install never mails every article that is already online.
- **Newsletters** are recognized by their Message-ID and processed right away (no waiting time). The content is what lies between the header block ("Newsletter im ...") and the "Anstehende Termine" block (or the footer, if there are no dates). Dividers between several content blocks are printed as a line. The title is the mail subject without "TRV News:". Images are shrunk (down to 70 mm height) only if that saves a page; otherwise they move to page 2. Label and header line are TRV red.
- Newsletters have their own baseline (`newsletters/.baseline_done`, also written by `--baseline`). Without it, a normal run skips newsletters with a warning. `NEWSLETTER_FROM=` (empty) turns newsletters off.

| Command | Purpose |
| --- | --- |
| `scraper.py` | normal run (what the timer does) |
| `scraper.py --dry-run` | everything except upload and mails; prints what would be sent |
| `scraper.py --test-to you@example.org` | uploads the newest saved article and newsletter and sends their mails to you; delivery status is not changed |
| `scraper.py --baseline` | saves everything online and every newsletter in the mailbox, uploads it to Nextcloud (skipping folders already up to date there) and marks the mails as sent (first server run only) |
| `scraper.py --dry-run --newsletter-eml mail.eml` | processes a saved newsletter mail instead of reading the mailbox (for testing) |

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
mkdir -p data
```

Use the `github-trv` host alias exactly as shown, not `github.com`, so this key is used.

The container runs as uid 1000 and must be able to write to `data/`. Which host uid that is depends on whether Docker uses user-namespace remapping:

```bash
sudo docker info 2>/dev/null | grep -iE 'userns|root dir'
```

- **No `userns` in the output**: container uid 1000 is host uid 1000.

  ```bash
  sudo chown 1000:1000 data
  ```

- **`userns` is listed** and the root dir looks like `/var/lib/docker/100000.100000`: container uids are shifted by that first number, so container uid 1000 is host uid **101000**.

  ```bash
  sudo chown 101000:101000 data
  ```

  Don't turn remapping off; other containers on the server may depend on it. Files in `data/` then belong to uid 101000 on the host, so you need `sudo` to edit them.

Check that the container can write:

```bash
sudo docker compose run --rm --entrypoint sh scraper -c 'ls -lnd /data; touch /data/test && echo OK && rm /data/test'
```

Inside the container, `/data` must belong to `1000 1000`. If it shows `65534` (nobody), the host owner is wrong. This check works only after the image is built (step 5).

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

For newsletters, the IMAP login defaults to the SMTP account. Set `IMAP_HOST` if your provider uses a different host for IMAP (e.g. `imap.example.org`).

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

This saves everything currently online and uploads it to Nextcloud. Folders that are already there and up to date (e.g. the one from the test in step 7) are skipped. No mails are sent. Failed uploads are retried by the next normal run.

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

**Update that adds newsletters**: add the new settings from `.env.example` (`NEXTCLOUD_NEWSLETTER_FOLDER`, `NEWSLETTER_FROM`, `IMAP_*`) to `.env`, then run the baseline once more:

```bash
sudo docker compose run --rm scraper --baseline
```

This uploads the newsletters already in the mailbox without mailing them. Articles already up to date on Nextcloud are skipped. Until then, normal runs skip newsletters with a warning in the log.

### 12. Change recipients

Edit `MAIL_TO_REDAKTION`, `MAIL_TO_OFFICE` or `MAIL_TO_ADMIN` in `.env` (several addresses: comma-separated). No rebuild is needed; the next run uses the new values.

## Troubleshooting

- **`PermissionError: [Errno 13] Permission denied: '/data/articles'`**: `data/` has the wrong host owner. With user-namespace remapping, `chown 1000:1000` is not enough; see the uid note in step 2.
- **Re-send an article**: in its `metadata.json`, set the step under `"delivery"` to `false`. The next run repeats only that step (with remapping: edit with `sudo`).
- **Nextcloud HTTP 401/404**: check the password and that the link allows upload and editing. Set `NEXTCLOUD_DAV=legacy` or `new` to skip auto-detection.
- **Mail errors**: port 587 needs `SMTP_SECURITY=starttls`, port 465 needs `ssl`. Some providers require `SMTP_FROM` to match `SMTP_USER`.
- **Newsletter not picked up**: check that the mail's sender is exactly `NEWSLETTER_FROM` and that it is in `IMAP_FOLDER` (not moved to spam). To re-process one, delete its folder in `data/newsletters/`.
- **Newsletter PDF contains header/footer or misses content**: the CleverReach template changed. Adjust `HEADER_TEXT` / `STOP_TEXT` in `newsletter.py`; test with `--dry-run --newsletter-eml mail.eml`.
