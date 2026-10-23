# HS_Dedupe

Daily HubSpot duplicate contact finder. Runs on GitHub Actions and writes proposed merges
to a Google Sheet for review before anything is merged. Read-only against HubSpot.

Owner: Daniel (CRM Manager)

## What it does

1. Pulls every contact from HubSpot (name, phone, mobile, create date, modified date, deal count).
2. Takes contacts modified in the last `LOOKBACK_DAYS` and checks them against every contact.
3. A duplicate is **same first name** (exact, case-insensitive) **plus a shared phone**,
   cross-matched: Phone or Mobile on one record vs Phone or Mobile on the other.
4. The **older record** (by create date) is the primary. Tie goes to the lower Record ID.
5. Writes to the sheet:
   - **Merge Log** tab: one row per duplicate pair found, appended every run, with links to both records.
     Pairs keep showing up until they are merged in HubSpot.
   - **Snapshot** tab: one row per record in each pair with every contact property plus
     associated deal and company IDs. This is the backup before any merge.

## Files

| File | Purpose |
|---|---|
| `dedupe.py` | The script |
| `requirements.txt` | Python packages |
| `.github/workflows/dedupe.yml` | Schedule (6:00 AM Arizona daily) and manual run button |

## Setup

### HubSpot
Private app with scopes `crm.objects.contacts.read` and `crm.schemas.contacts.read`.
Copy the access token.

### Google Cloud
1. Create a project, enable the **Google Sheets API**.
2. Create a **service account**, download a JSON key.
3. Create the Google Sheet. Share it with the service account email (`client_email` in the JSON) as **Editor**.

### GitHub secrets
Settings > Secrets and variables > Actions > New repository secret.

| Secret | Value |
|---|---|
| `HUBSPOT_TOKEN` | Private app token (`pat-na1-...`) |
| `HUBSPOT_PORTAL_ID` | Portal number from any HubSpot URL, used for record links |
| `GOOGLE_CREDENTIALS` | Entire contents of the service account JSON file |
| `SHEET_ID` | The long id in the sheet URL between `/d/` and `/edit` |

## Running it

- **First run:** Actions > HubSpot duplicate check > Run workflow > set `lookback_days` to `180`.
  Expect 10 to 15 minutes.
- **After that:** runs automatically every day with a 3-day lookback.
- To change the schedule, edit the `cron` line in `.github/workflows/dedupe.yml` (times are UTC).

## Phone handling

Phones are normalized before comparing, so `(602) 555-1234`, `16025551234`, `+1 602.555.1234 ext 12`
and `6025551234x8` all match. Extensions are dropped, two numbers in one cell are split, junk like
`tbd` or `1111111111` is ignored. Values Excel has mangled into `9.17E+11` can't be recovered.

## Next phase (not built yet)

Once the sheet has been reviewed, a merge step will call
`POST /crm/v3/objects/contacts/merge` with `{"primaryObjectId": ..., "objectIdToMerge": ...}`
for each approved pair. That needs the `crm.objects.contacts.write` scope added to the private app.
Merges are not reversible, which is what the Snapshot tab is for.
