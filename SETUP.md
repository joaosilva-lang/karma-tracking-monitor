# Tracking Monitor — Setup Guide

## Prerequisites
- Python 3.11+
- A GitHub account
- Access to the client's GA4 property and GAds account

---

## Step 1 — Google Cloud Project

Use the existing Karma team GCP project — no need to create a new one.

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and open the existing Karma project
2. Go to **APIs & Services → Library** and confirm these 4 APIs are enabled (enable if not):
   - **Google Analytics Data API**
   - **Google Ads API**
   - **Google Sheets API**
   - **Tag Manager API** (used by the analyze_history GTM-tag mapping step)
3. If the OAuth consent screen is not yet configured: go to **APIs & Services → OAuth consent screen**
   - User type: **Internal**
   - App name: `Karma Tracking Monitor`
   - Add scopes: `analytics.readonly`, `spreadsheets`, `adwords`, `tagmanager.readonly`
4. Go to **APIs & Services → Credentials → Create Credentials → OAuth client ID**
   - Application type: **Desktop app**
   - Name: `karma-tracking-monitor`
5. Download the JSON file and save it as `client_secret.json` in the project root (this file must NEVER be committed)

---

## Step 2 — Get a Google Ads Developer Token

1. Go to your Google Ads account → Tools → API Center
2. Apply for a developer token (Standard Access is needed for production; Basic Access works for testing)
3. Copy the developer token — you'll need it in Step 4

---

## Step 3 — Get OAuth Refresh Token

Run this once locally:

```bash
pip install google-auth-oauthlib
python setup_oauth.py
```

A browser window will open — log in with your Google account and grant access.  
The script prints 3 values: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN`.  
**Copy them immediately — you'll add them as GitHub Secrets in Step 5.**

> **Note:** a refresh token is permanently bound to the scopes consented when it was
> created. Whenever a scope is added to `setup_oauth.py` (e.g. `tagmanager.readonly`,
> added Jul 2026), re-run this script and replace the `GOOGLE_REFRESH_TOKEN` secret.
> Until you do, features needing the new scope are skipped with a warning; everything
> else keeps working.

---

## Step 4 — Create the Google Sheet

1. Create a new Google Sheet and name it `Karma Tracking Monitor`
2. Copy the Sheet ID from the URL: `https://docs.google.com/spreadsheets/d/**SHEET_ID**/edit`
3. Create a tab named **`config`** with these headers in row 1:

   | client_id | platform | event_name | severity |
   |-----------|----------|------------|----------|

4. Populate it with the Westlake events. Example rows:

   | westlake | GA4 | StickyButton_RequestInfo | critical |
   | westlake | GA4 | DownloadBrochure | critical |
   | westlake | GA4 | Footer_OpenForm | critical |
   | westlake | GA4 | Lakehouses_OpenForm | critical |
   | westlake | GA4 | StickyButton_BuyProperty | critical |
   | westlake | GA4 | Footer_ContactUs | critical |
   | westlake | GA4 | TimeOnPage_45s | secondary |
   | westlake | GAds | StickyButton_RequestInfo | critical |
   | westlake | GAds | DownloadBrochure | critical |
   | westlake | GAds | Footer_OpenForm | critical |
   | westlake | GAds | Lakehouses_OpenForm | critical |
   | westlake | GAds | StickyButton_BuyProperty | critical |
   | westlake | GAds | Footer_ContactUs | critical |

   > Events not listed here are automatically treated as `secondary` (logged to Sheet only, no Slack alert).

---

## Step 5 — Create GitHub Repository & Add Secrets

1. Create a new **private** repository on GitHub (e.g. `karma-tracking-monitor`)
2. Push this project to the repo:
   ```bash
   git init
   git add .
   git commit -m "Initial commit"
   git remote add origin https://github.com/YOUR_ORG/karma-tracking-monitor.git
   git push -u origin main
   ```
3. Go to repository **Settings → Secrets and variables → Actions → New repository secret** and add:

   | Secret name | Value |
   |---|---|
   | `GOOGLE_CLIENT_ID` | from Step 3 |
   | `GOOGLE_CLIENT_SECRET` | from Step 3 |
   | `GOOGLE_REFRESH_TOKEN` | from Step 3 |
   | `GOOGLE_ADS_DEVELOPER_TOKEN` | from Step 2 |
   | `GOOGLE_SHEET_ID` | from Step 4 |
   | `SLACK_WEBHOOK_URL` | from your Slack app |
   | `GEMINI_API_KEY` *(optional)* | from [aistudio.google.com](https://aistudio.google.com) — enables the automatic triage agent (a Gemini-written diagnosis posted to Slack after critical alerts) and phrases the weekly config-divergence digest. Without it the triage step is a silent no-op and the digest falls back to a plain deterministic list. |

---

## Step 6 — Create Slack Webhook

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → "Create New App" → "From scratch"
2. Name: `Karma Tracking Monitor`, choose your workspace
3. "Add features" → **Incoming Webhooks** → Activate → "Add New Webhook to Workspace"
4. Select the channel for alerts and copy the webhook URL
5. Add it as `SLACK_WEBHOOK_URL` in GitHub Secrets

---

## Step 7 — Test the Workflow

In GitHub, go to **Actions → Daily Tracking Monitor → Run workflow** to trigger it manually.  
Check the Sheet for a new `results` tab and your Slack channel for any critical alerts.

---

## Adding a New Client

No code changes. Use the onboarding workflow — see "Como adicionar um novo cliente"
in [ARCHITECTURE.md](ARCHITECTURE.md) for the full walkthrough. In short:

1. Ensure your Google account has access to the client's GA4 property, GAds account
   (under the Karma MCC) and GTM container
2. Run **Actions → Onboard New Client** with the client's IDs — it validates each
   access and writes a proposed config (with stats and suggestions per discovered
   event) to the `config_proposta` tab
3. Review the proposal (promote the events that matter to `critical`) and copy the
   rows into the `config` tab
