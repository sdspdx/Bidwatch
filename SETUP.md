# BidWatch in the cloud — setup

BidWatch runs every day on GitHub's computers and emails the results to your iPhone. Nothing of yours has to be switched on. It's free on a GitHub account (a private repository gets 2,000 free run-minutes a month; BidWatch typically uses 400–800).

**What runs, and when**
- **Daily bid check:** 5:17 AM Pacific (6:17 AM in summer). Checks your watched pages and SAM.gov, then emails changes and keyword hits.
- **Discovery agent:** every night. Finds new bids/procurement pages and emails them to you as a CSV.

GitHub sometimes starts scheduled runs a few minutes to an hour late.

Setup takes about 20 minutes. Steps 1–3 are easiest on a computer; after that, everything can be done from Safari on your iPhone.

---

## 1. Create the repository

1. Sign up at <https://github.com/signup> (free), or sign in.
2. Go to <https://github.com/new>.
   - Repository name: `bidwatch`
   - Choose **Private**. This matters: the repository will hold your watch list and results.
   - Click **Create repository**.

## 2. Upload the files

1. On the new repository's page, click **uploading an existing file**.
2. Unzip BidWatch-Cloud.zip. Open the folder, select **all the files inside it**, and drag them onto the page.
3. Click **Commit changes**.

Then add the workflow (the file that tells GitHub when to run BidWatch):

1. Click **Add file → Create new file**.
2. In the name box, type exactly: `.github/workflows/bidwatch.yml`
   The slashes create the folders as you type.
3. Open `bidwatch-workflow.yml` from the unzipped folder in a text editor (TextEdit or Notepad). Copy everything and paste it into the big box.
4. Click **Commit changes…**, then **Commit changes** again.

## 3. Add your passwords and keys (GitHub "secrets")

Secrets are stored encrypted. Nobody can read them back, including you, but BidWatch can use them.

First, get a Gmail **App Password** so BidWatch can send email from your Gmail:
- Go to <https://myaccount.google.com/apppasswords>. You'll need 2-Step Verification turned on.
- Create one named "BidWatch" and copy the 16-letter password.

Then, in your repository, go to **Settings → Secrets and variables → Actions → New repository secret** and add each of these:

| Name | Value |
|---|---|
| `SMTP_USERNAME` | your Gmail address |
| `SMTP_PASSWORD` | the 16-letter App Password |
| `EMAIL_TO` | where to send the reports (more than one address: separate with commas) |
| `SAM_API_KEY` | *optional:* your SAM.gov API key, for federal solicitations |
| `BRAVE_API_KEY` | *optional:* Brave Search API key, so the discovery agent finds more engineering firms |
| `SITE_LOGINS` | *optional:* usernames and passwords for bid sites that need a sign-in (see below) |

If you use another email provider, also add `SMTP_SERVER` and `SMTP_PORT`. For example, iCloud is `smtp.mail.me.com` and `587`.

## 4. Set your keywords and states

Open **config.json** in the repository, click the **pencil** (edit), and change:
- `"keywords"`: your words and phrases, each in quotes, separated by commas.
- `"states"`: two-letter codes, e.g. `["WA", "OR"]`. Use `[]` for nationwide.
- `"discovery" → "types"`: what the agent looks for: `city`, `county`, `port`, `utility`, `transit`, `special_district`, `state`, `tribal`, `school_district`, `engineering_firm`.

Click **Commit changes**. Keep the quotes, commas and brackets exactly as they are.

## 5. Add the pages to watch

Open **urls.csv** and click the pencil. Add one page per line:

```
https://www.cityoftacoma.org/bids,City of Tacoma - Bids,WA
```

- **To stop watching a page:** delete its line, or put `#` in front of it.
- **When the discovery agent emails you a CSV:** copy the rows you want and paste them in. The extra columns are ignored.
- **Changes BidWatch makes for you:** when a page moves to a new address, BidWatch updates its line. New links that contain your keywords are added at the bottom under a dated note. Delete any you don't want.

For engineering firms and others that aren't on public lists, add them to **my_list.csv** (`name,website,state,type`) and the discovery agent will look for their bid pages.

## 6. Test it

1. Go to the **Actions** tab. If asked, click **I understand my workflows, go ahead and enable them**.
2. Click **BidWatch → Run workflow**, choose **daily check**, and click the green **Run workflow** button.
3. After a minute or two the run turns green and the email arrives.
   If it turns red, click into it to see which step failed. The usual cause is a mistyped secret.

The first daily run records every page as its starting point. Changes are reported from the second run on.

---

## Websites that need a sign-in

In the cloud there's no pop-up window. Put the sign-ins in the `SITE_LOGINS` secret instead, written like this (the website name or the exact page address, then the details):

```
{
  "vendors.example.gov": {"username": "me@mycompany.com", "password": "MyPassword"},
  "https://bids.anothercity.org/portal": {"username": "mike", "password": "pw2",
                                          "login_url": "https://bids.anothercity.org/login"}
}
```

To change it later, open the secret and paste the whole thing again with your changes.

When a watched page asks for a sign-in that isn't in the secret, the daily email lists it under **Sign-in needed**.

Sites with a CAPTCHA or a texted/app code (two-factor) can't be signed into automatically.

## Day to day, from your iPhone

- **Reports** arrive in Mail. Keyword hits come as High Importance with ★ KEYWORD MATCH in the subject.
- **To change keywords, states or watched pages:** open github.com in Safari, go to your `bidwatch` repository and edit `config.json` or `urls.csv` (pencil icon). Changes apply from the next run.
- **To run it right now:** Actions → BidWatch → Run workflow.
- **Copies of each email** are kept for 14 days on each run's page under **Artifacts**.
- **To change the run times:** edit `.github/workflows/bidwatch.yml`. The `cron` lines are in UTC; Pacific time is UTC minus 8 hours in winter and minus 7 in summer. If you change the discovery time, change it in both places it appears in the file.
- **To pause BidWatch:** Actions → BidWatch → **⋯** → **Disable workflow**.

## Good to know

- Each run saves its database (`bidwatch.db`, `discovery.db`) back into the repository. That's how it remembers yesterday's pages.
- **Usage:** check how many free minutes you've used under your GitHub picture → **Settings → Billing and plans**. If you get close to the limit, lower `"max_sites_per_run"` in config.json.
- Some agency sites block requests from cloud servers. If a page that works in your browser keeps showing as broken here, watch it from the desktop version instead.
- Bid portals that build their pages with JavaScript may show little or nothing to BidWatch.
