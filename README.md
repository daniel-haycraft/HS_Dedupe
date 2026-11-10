# HS_Dedupe

Daily HubSpot duplicate contact finder. Runs on GitHub Actions and writes proposed merges
to a Google Sheet for review before anything is merged. Read-only against HubSpot.

Owner: Daniel (CRM Manager)

## What it does

1. Pulls every contact from HubSpot (name, phones, email, owner, dates, deal count, lead status, type, source).
2. Takes contacts modified in the last `LOOKBACK_DAYS` and checks them against every contact.
3. Finds duplicates in two stages. Both need a **shared phone**, cross-matched (Phone or Mobile on one
   record vs Phone or Mobile on the other):
   - **Stage 1 (code):** same first name, exact after lowercasing. Decided in code, no AI cost.
   - **Stage 2 (Claude):** first names differ but could be the same person (Ben/Benjamin, Mike/Michael,
     Rober/Robert) and last names agree. A prefilter in code picks these, then Claude reads both full
     records and answers same person yes/no with a confidence and a one-line reason.
     Only a yes at or above `CONFIDENCE_MIN` (default 0.85) can merge. Lower = `review`. No = `not duplicate`.
   - **Shared lines:** a number on more than `SHARED_LINE_CAP` (default 5) contacts is an office
     switchboard. Skipped entirely and listed on the **Shared Lines** tab.
4. The **older record** (by create date) is the primary. Tie goes to the lower Record ID.
5. Writes to the sheet:
   - **Merge Log** tab: one row per pair, appended every run, with who decided it (code or Claude),
     confidence, reason, and links to both records. Pairs keep showing up until they are merged.
   - **Snapshot** tab: one row per record in each merge-eligible pair with every contact property plus
     associated deal and company IDs. This is the backup before any merge.
   - **Shared Lines** tab: the switchboard numbers that were skipped, with the contacts on them.

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
| `ANTHROPIC_API_KEY` | Claude API key from platform.claude.com > Settings > API keys (service account key recommended). Without it, Stage 2 pairs are logged as `review` and never merged. |

Optional repo **variables** (Variables tab, not Secrets): `MERGE_ENABLED`, `SHARED_LINE_CAP`, `CONFIDENCE_MIN` (0 to 1, e.g. `0.85`; the run stops if it's outside that range).

## Contact cache (speed)

The first run pulls every contact from HubSpot (about 15 minutes) and saves a compressed copy as
`contacts_cache.json.gz`. GitHub Actions keeps that file between runs. Every run after that only
asks HubSpot for contacts modified since the last run, lays them over the saved copy, and saves again.
After a merge, the retired IDs are removed from the copy and the surviving record is pulled fresh.

- The copy is rebuilt from scratch automatically every `FULL_REFRESH_DAYS` (default 7), so contacts
  deleted in HubSpot eventually fall out of it.
- To force a rebuild now: Actions > Run workflow > set `force_full_pull` to `true`.
- GitHub drops caches not used in 7 days. If the job hasn't run in a week it just does a full pull again.

## Testing on a few contacts

1. Create 2 or 3 contacts in HubSpot with an obvious fake first name (e.g. `Zztest`) and the same phone.
   Give one of them a lead status of DO NOT CONTACT so you can watch the rule fire.
2. Actions > Run workflow:
   - `only_ids`: the Record IDs of your test contacts, comma-separated. Only pairs involving them are processed.
   - `max_merges`: a cap on merges for the run (e.g. `2`). Anything past it is logged as `proposed`.
3. Check the Merge Log and Snapshot tabs, then the surviving record in HubSpot.

Both inputs are blank for normal runs.

## Running it

- **First run:** Actions > HubSpot duplicate check > Run workflow > set `lookback_days` to `180`.
  Expect 10 to 15 minutes.
- **After that:** runs automatically every day with a 3-day lookback.
- To change the schedule, edit the `cron` line in `.github/workflows/dedupe.yml` (times are UTC).

## Phone handling

Phones are normalized before comparing, so `(602) 555-1234`, `16025551234`, `+1 602.555.1234 ext 12`
and `6025551234x8` all match. Extensions are dropped, two numbers in one cell are split, junk like
`tbd` or `1111111111` is ignored. Values Excel has mangled into `9.17E+11` can't be recovered.

## Merging

Merging is controlled by a repository **variable** (not a secret): Settings > Secrets and variables >
Actions > Variables tab > `MERGE_ENABLED`.

- Missing or anything other than `true`: the run only writes to the sheet (status `proposed`).
- `true`: the run snapshots both records, patches the primary per the rules below, merges, and writes
  the result (`merged`, `failed`, or `skipped`) to the Merge Log.

Before setting it to `true`, add the `crm.objects.contacts.write` scope to the private app.

### What survives a merge
HubSpot default: the primary (older record) keeps its own value wherever both records have one, and
fills in from the secondary wherever it is blank. All activities, deals, companies, and list memberships
from both records land on the primary. Merges are not reversible, which is what the Snapshot tab is for.

### CF1 contact guideline exceptions
Applied by updating the primary before the merge, so the value survives:

| Rule | Behaviour |
|---|---|
| Lead status | If either record is **DO NOT CONTACT** or **On A Deal / NA**, the primary gets it. DO NOT CONTACT wins if both appear. |
| Contact type | If either record has a protected type (Internal, Attorney, Insurance, Title / Escrow, Investor (capital side), Broker's Client, Vendor / Partner), the primary gets it. If both are protected, the primary keeps its own. |
| Broker's Client | If it appears in Contact Type or Sales Lead Source 2.0 on either record, the primary gets Broker's Client in **both** fields. |

The Merge Log's `Primary Changes` column shows exactly what was (or would be) changed on the primary.
