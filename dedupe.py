"""
HubSpot duplicate finder -> Google Sheet log.  Runs daily on GitHub Actions.

Match rule:  same First Name (exact, case-insensitive) AND a shared phone,
             cross-matched (Phone or Mobile on one record vs Phone or Mobile on the other).
Primary:     the older record (by Create Date) wins. Tie -> lower Record ID.

Writes two tabs to the sheet (creates them if missing):
  Merge Log  - one row per duplicate pair found, appended every run. Pairs stay listed
               until they are merged in HubSpot (merged records can't be found again).
  Snapshot   - one row per record in each pair, with EVERY contact property plus
               associated deal / company IDs. This is the undo button for later merges.

Env vars (GitHub Secrets):
  HUBSPOT_TOKEN       private app token, needs crm.objects.contacts.read + crm.schemas.contacts.read
  GOOGLE_CREDENTIALS  full JSON of the Google service account key
  SHEET_ID            the long id in the Google Sheet URL
  LOOKBACK_DAYS       optional, default 3. Contacts modified in this window are checked
                      against every contact. Use 180 for the first run.

This script is READ ONLY against HubSpot. The merge step comes later.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import gspread
import requests

# =====================================================================
# SETTINGS
# =====================================================================

HUBSPOT_TOKEN = os.environ['HUBSPOT_TOKEN']
SHEET_ID = os.environ['SHEET_ID']
GOOGLE_CREDENTIALS = json.loads(os.environ['GOOGLE_CREDENTIALS'])
LOOKBACK_DAYS = int(os.environ.get('LOOKBACK_DAYS', '3'))

LOG_TAB = 'Merge Log'
SNAPSHOT_TAB = 'Snapshot'

HS = 'https://api.hubapi.com'
HEADERS = {'Authorization': f'Bearer {HUBSPOT_TOKEN}', 'Content-Type': 'application/json'}

# Only these are pulled for the full scan. The snapshot pulls everything.
MATCH_PROPS = ['firstname', 'lastname', 'phone', 'mobilephone', 'createdate',
               'hs_lastmodifieddate', 'num_associated_deals']

RUN_ID = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


# =====================================================================
# PHONE NORMALIZATION (same engine as the CSV matchers)
# =====================================================================

DIGITS = re.compile(r'\d+')
EXT_SPLIT = re.compile(r'(?i)\s*(?:ext\.?|extension|x)\s*\d')
JUNK_10 = {'0000000000', '1111111111', '2222222222', '3333333333', '4444444444',
           '5555555555', '6666666666', '7777777777', '8888888888', '9999999999',
           '1234567890', '0123456789', '9876543210'}


def _valid_us(d):
    return len(d) == 10 and d[0] not in '01' and d[3] not in '01' and d not in JUNK_10


def _split_us(d):
    if not d:
        return []
    for take in (11, 10):
        if len(d) >= take:
            head, rest = d[:take], d[take:]
            if take == 11:
                if not head.startswith('1'):
                    continue
                head = head[1:]
            if _valid_us(head):
                tail = _split_us(rest) if rest else []
                if rest and tail == []:
                    continue
                return [head] + tail
    return []


def phone_keys(raw):
    if raw is None:
        return []
    v = str(raw).strip()
    if not v:
        return []
    low = v.lower()
    if '@' in v or re.search(r'^(na|n/a|tbd|none|unknown|null)\b', low) or not re.search(r'\d', v):
        return []
    if re.search(r'(?i)\d\.\d+e\+\d+', v):
        return []
    body = v
    m = EXT_SPLIT.search(v)
    if m:
        body = v[:m.start()]
    body = re.sub(r'(?i)[a-z]+$', '', body).strip()
    d = ''.join(DIGITS.findall(body))
    intl = v.startswith('+') or d.startswith('00')
    if d.startswith('00'):
        d = d[2:]
    if not d or len(set(d)) <= 2:
        return []
    us = _split_us(d)
    if us:
        return us
    if intl and 7 <= len(d) <= 15 and d[0] != '1':
        return [d]
    return []


def format_phone(key):
    if len(key) == 10 and key.isdigit():
        return f'({key[:3]}) {key[3:6]}-{key[6:]}'
    return key


def clean_first(s):
    """Exact match after lowercasing and trimming. 'Mike' != 'Michael'."""
    return ' '.join((s or '').split()).casefold()


# =====================================================================
# HUBSPOT API HELPERS
# =====================================================================

def hs_request(method, path, **kwargs):
    """Request with retry on 429 / 5xx."""
    for attempt in range(6):
        resp = requests.request(method, HS + path, headers=HEADERS, timeout=60, **kwargs)
        if resp.status_code == 429 or resp.status_code >= 500:
            wait = 2 ** attempt
            print(f'  HubSpot {resp.status_code}, retrying in {wait}s')
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    sys.exit(f'HubSpot kept failing on {path}')


def fetch_all_contacts():
    """Every contact with just the matching properties. ~100 per call."""
    contacts = []
    after = None
    while True:
        params = {'limit': 100, 'properties': ','.join(MATCH_PROPS)}
        if after:
            params['after'] = after
        data = hs_request('GET', '/crm/v3/objects/contacts', params=params)
        for r in data.get('results', []):
            p = r.get('properties', {})
            contacts.append({
                'id': str(r['id']),
                'first': clean_first(p.get('firstname')),
                'first_raw': p.get('firstname') or '',
                'last_raw': p.get('lastname') or '',
                'phone_raw': p.get('phone') or '',
                'mobile_raw': p.get('mobilephone') or '',
                'phone_keys': phone_keys(p.get('phone')),
                'mobile_keys': phone_keys(p.get('mobilephone')),
                'created': p.get('createdate') or '',
                'modified': p.get('hs_lastmodifieddate') or '',
                'deals': p.get('num_associated_deals') or '0',
            })
        after = data.get('paging', {}).get('next', {}).get('after')
        if len(contacts) % 5000 < 100:
            print(f'  pulled {len(contacts)} contacts')
        if not after:
            break
        time.sleep(0.11)   # stay under 10 req/sec
    return contacts


def fetch_all_property_names():
    data = hs_request('GET', '/crm/v3/properties/contacts')
    return sorted(p['name'] for p in data.get('results', []))


def fetch_full_records(ids, prop_names):
    """Every property for the given ids, 100 at a time."""
    out = {}
    ids = list(ids)
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        data = hs_request('POST', '/crm/v3/objects/contacts/batch/read',
                          json={'properties': prop_names, 'inputs': [{'id': cid} for cid in chunk]})
        for r in data.get('results', []):
            out[str(r['id'])] = r.get('properties', {})
        time.sleep(0.11)
    return out


def fetch_associations(ids, to_object):
    """{contact_id: 'id1, id2, ...'} for deals or companies."""
    out = {}
    ids = list(ids)
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        data = hs_request('POST', f'/crm/v4/associations/contacts/{to_object}/batch/read',
                          json={'inputs': [{'id': cid} for cid in chunk]})
        for r in data.get('results', []):
            out[str(r['from']['id'])] = ', '.join(str(t['toObjectId']) for t in r.get('to', []))
        time.sleep(0.11)
    return out


# =====================================================================
# MATCHING
# =====================================================================

def parse_hs_date(s):
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00'))
    except Exception:
        return datetime.max.replace(tzinfo=timezone.utc)


def find_pairs(contacts):
    """Returns list of (primary, secondary, matched_phone, how)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)

    # index: (first name, phone key) -> list of (contact, which column)
    index = {}
    for c in contacts:
        if not c['first']:
            continue
        for k in c['phone_keys']:
            index.setdefault((c['first'], k), []).append((c, 'Phone'))
        for k in c['mobile_keys']:
            index.setdefault((c['first'], k), []).append((c, 'Mobile'))

    candidates = [c for c in contacts if c['first'] and parse_hs_date(c['modified']) >= cutoff]
    print(f'{len(contacts)} contacts total, {len(candidates)} modified in last {LOOKBACK_DAYS} days')

    pairs = {}
    for c in candidates:
        mine = [(k, 'Phone') for k in c['phone_keys']] + [(k, 'Mobile') for k in c['mobile_keys']]
        for k, my_col in mine:
            for other, their_col in index.get((c['first'], k), []):
                if other['id'] == c['id']:
                    continue
                key = tuple(sorted((c['id'], other['id'])))
                if key in pairs:
                    continue
                a, b = c, other
                # older record wins, tie -> lower id
                if (parse_hs_date(b['created']), int(b['id'])) < (parse_hs_date(a['created']), int(a['id'])):
                    a, b = b, a
                    my_col, their_col = their_col, my_col
                pairs[key] = (a, b, format_phone(k), f'{my_col} vs {their_col}')
    return list(pairs.values())


# =====================================================================
# GOOGLE SHEETS
# =====================================================================

LOG_HEADER = ['Run', 'Status', 'Primary ID', 'Secondary ID', 'First Name', 'Matched Phone', 'Matched How',
              'Primary Last Name', 'Primary Created', 'Primary Deals', 'Primary Phone', 'Primary Mobile',
              'Secondary Last Name', 'Secondary Created', 'Secondary Deals', 'Secondary Phone', 'Secondary Mobile',
              'Primary Link', 'Secondary Link']


def get_or_create_tab(sh, title, header):
    try:
        ws = sh.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=1000, cols=max(26, len(header)))
        ws.append_row(header, value_input_option='RAW')
        ws.freeze(rows=1)
    return ws


def write_snapshot(ws, records, prop_names, deals, companies, roles):
    """Appends one row per record. Header = fixed cols + every property. Extends header if new props appear."""
    fixed = ['Run', 'Record ID', 'Role', 'Pair', 'Deal IDs', 'Company IDs']
    existing = ws.row_values(1)
    header = existing if existing else fixed + prop_names
    missing = [p for p in prop_names if p not in header]
    if not existing or missing:
        header = header + missing
        ws.update('A1', [header], value_input_option='RAW')
        ws.freeze(rows=1)
    col = {h: i for i, h in enumerate(header)}

    rows = []
    for rid, props in records.items():
        row = [''] * len(header)
        row[col['Run']] = RUN_ID
        row[col['Record ID']] = rid
        row[col['Role']] = roles[rid][0]
        row[col['Pair']] = roles[rid][1]
        row[col['Deal IDs']] = deals.get(rid, '')
        row[col['Company IDs']] = companies.get(rid, '')
        for k, v in props.items():
            if k in col and v not in (None, ''):
                row[col[k]] = str(v)[:49000]
        rows.append(row)
    if rows:
        ws.append_rows(rows, value_input_option='RAW')


# =====================================================================
# MAIN
# =====================================================================

def main():
    print(f'Run {RUN_ID}')
    gc = gspread.service_account_from_dict(GOOGLE_CREDENTIALS)
    sh = gc.open_by_key(SHEET_ID)
    log_ws = get_or_create_tab(sh, LOG_TAB, LOG_HEADER)
    snap_ws = get_or_create_tab(sh, SNAPSHOT_TAB, ['Run', 'Record ID', 'Role', 'Pair', 'Deal IDs', 'Company IDs'])

    print('Pulling contacts from HubSpot...')
    contacts = fetch_all_contacts()
    pairs = find_pairs(contacts)
    print(f'{len(pairs)} duplicate pairs found')

    if not pairs:
        print('No duplicates. Done.')
        return

    # ---- Merge Log ----
    log_rows = []
    for a, b, phone, how in pairs:
        log_rows.append([
            RUN_ID, 'proposed', a['id'], b['id'], a['first_raw'], phone, how,
            a['last_raw'], a['created'], a['deals'], a['phone_raw'], a['mobile_raw'],
            b['last_raw'], b['created'], b['deals'], b['phone_raw'], b['mobile_raw'],
            f"https://app.hubspot.com/contacts/{os.environ.get('HUBSPOT_PORTAL_ID', '')}/record/0-1/{a['id']}",
            f"https://app.hubspot.com/contacts/{os.environ.get('HUBSPOT_PORTAL_ID', '')}/record/0-1/{b['id']}",
        ])
    log_ws.append_rows(log_rows, value_input_option='RAW')
    print(f'Appended {len(log_rows)} rows to {LOG_TAB}')

    # ---- Snapshot (every property + associations) ----
    roles = {}
    for a, b, _, _ in pairs:
        pair = f"{a['id']} <- {b['id']}"
        roles[a['id']] = ('primary', pair)
        roles[b['id']] = ('secondary', pair)
    ids = list(roles.keys())
    print(f'Snapshotting {len(ids)} records with all properties...')
    prop_names = fetch_all_property_names()
    records = fetch_full_records(ids, prop_names)
    deals = fetch_associations(ids, 'deals')
    companies = fetch_associations(ids, 'companies')
    write_snapshot(snap_ws, records, prop_names, deals, companies, roles)
    print(f'Appended {len(records)} rows to {SNAPSHOT_TAB}. Done.')


if __name__ == '__main__':
    main()
