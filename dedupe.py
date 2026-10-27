"""
HubSpot duplicate finder + merger.  Runs daily on GitHub Actions.

Match rule:  same First Name (exact, case-insensitive) AND a shared phone,
             cross-matched (Phone or Mobile on one record vs Phone or Mobile on the other).
Primary:     the older record (by Create Date) wins. Tie -> lower Record ID.

Merge behaviour (HubSpot default): the primary keeps its own value wherever both records have one,
and fills in from the secondary wherever the primary is blank. Activities, deals, companies, list
memberships from both records all land on the primary.

CF1 contact guideline exceptions (applied by PATCHing the primary BEFORE the merge):
  - Lead status: if either record is DO NOT CONTACT or On A Deal / NA, the primary gets it.
    DO NOT CONTACT beats On A Deal / NA.
  - Contact type: if either record has a protected type (Internal, Attorney, Insurance,
    Title / Escrow, Investor (capital side), Broker's Client, Vendor / Partner), the primary gets it.
    If both have a protected type, the primary keeps its own.
  - Broker's Client: if either record has it in Contact Type OR Sales Lead Source 2.0,
    the primary gets Broker's Client in BOTH.

Sheet tabs:
  Merge Log  - one row per pair, appended every run, with what was changed on the primary
               and the merge result.
  Snapshot   - one row per record in each pair with EVERY property plus deal/company IDs.
               Written BEFORE merging. This is the undo button.

Env vars (GitHub Secrets / Variables):
  HUBSPOT_TOKEN       private app token. Read: crm.objects.contacts.read, crm.schemas.contacts.read
                      Merge: also needs crm.objects.contacts.write
  HUBSPOT_PORTAL_ID   portal number, used for record links
  GOOGLE_CREDENTIALS  full JSON of the Google service account key
  SHEET_ID            the long id in the Google Sheet URL
  LOOKBACK_DAYS       default 3. Contacts modified in this window are checked against everyone.
  MERGE_ENABLED       'true' to actually patch + merge. Anything else = sheet only.
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
PORTAL_ID = os.environ.get('HUBSPOT_PORTAL_ID', '')
LOOKBACK_DAYS = int(os.environ.get('LOOKBACK_DAYS', '3'))
MERGE_ENABLED = os.environ.get('MERGE_ENABLED', 'false').strip().lower() == 'true'

LOG_TAB = 'Merge Log'
SNAPSHOT_TAB = 'Snapshot'

HS = 'https://api.hubapi.com'
HEADERS = {'Authorization': f'Bearer {HUBSPOT_TOKEN}', 'Content-Type': 'application/json'}

# Properties pulled for the full scan. The snapshot pulls everything.
MATCH_PROPS = ['firstname', 'lastname', 'phone', 'mobilephone', 'createdate', 'hs_lastmodifieddate',
               'num_associated_deals', 'hs_lead_status', 'contact_type_2_0', 'sales_lead_source_2_0']

# CF1 guideline values, by LABEL. Internal values are looked up from the property definitions at runtime.
LEAD_STATUS_PROP = 'hs_lead_status'
CONTACT_TYPE_PROP = 'contact_type_2_0'
LEAD_SOURCE_PROP = 'sales_lead_source_2_0'

LEAD_STATUS_PRIORITY = ['DO NOT CONTACT', 'On A Deal / NA']          # first wins if both present
PROTECTED_CONTACT_TYPES = ['Internal', 'Attorney', 'Insurance', 'Title / Escrow',
                           'Investor (capital side)', "Broker's Client", 'Vendor / Partner']
BROKERS_CLIENT = "Broker's Client"

RUN_ID = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


# =====================================================================
# PHONE NORMALIZATION
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
    return ' '.join((s or '').split()).casefold()


def norm_label(s):
    """Loose compare for labels: lowercase, straight apostrophes, collapsed spaces."""
    return ' '.join((s or '').replace('\u2019', "'").split()).casefold()


# =====================================================================
# HUBSPOT API
# =====================================================================

def hs_request(method, path, **kwargs):
    for attempt in range(6):
        resp = requests.request(method, HS + path, headers=HEADERS, timeout=60, **kwargs)
        if resp.status_code == 429 or resp.status_code >= 500:
            wait = 2 ** attempt
            print(f'  HubSpot {resp.status_code}, retrying in {wait}s')
            time.sleep(wait)
            continue
        if not resp.ok:
            raise requests.HTTPError(f'{resp.status_code} {resp.text[:300]}', response=resp)
        return resp.json() if resp.text else {}
    sys.exit(f'HubSpot kept failing on {path}')


def fetch_all_contacts():
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
                'lead_status': p.get(LEAD_STATUS_PROP) or '',
                'contact_type': p.get(CONTACT_TYPE_PROP) or '',
                'lead_source': p.get(LEAD_SOURCE_PROP) or '',
            })
        after = data.get('paging', {}).get('next', {}).get('after')
        if len(contacts) % 10000 < 100:
            print(f'  pulled {len(contacts)} contacts')
        if not after:
            break
        time.sleep(0.11)
    return contacts


def fetch_property_defs():
    data = hs_request('GET', '/crm/v3/properties/contacts')
    return {p['name']: p for p in data.get('results', [])}


def fetch_full_records(ids, prop_names):
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


def patch_contact(cid, props):
    hs_request('PATCH', f'/crm/v3/objects/contacts/{cid}', json={'properties': props})


def merge_contacts(primary_id, secondary_id):
    hs_request('POST', '/crm/v3/objects/contacts/merge',
               json={'primaryObjectId': str(primary_id), 'objectIdToMerge': str(secondary_id)})


# =====================================================================
# GUIDELINE VALUE LOOKUP (label -> internal value)
# =====================================================================

def build_value_map(prop_defs, prop_name, labels):
    """{label: internal value} for the labels we care about. Also accepts the label as the value itself."""
    options = prop_defs.get(prop_name, {}).get('options', [])
    by_label = {norm_label(o.get('label')): o.get('value') for o in options}
    by_value = {norm_label(o.get('value')): o.get('value') for o in options}
    out = {}
    for lab in labels:
        v = by_label.get(norm_label(lab)) or by_value.get(norm_label(lab))
        if v is None:
            print(f'  WARNING: "{lab}" not found in options for {prop_name}, using the label as-is')
            v = lab
        out[lab] = v
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
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)

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
                if (parse_hs_date(b['created']), int(b['id'])) < (parse_hs_date(a['created']), int(a['id'])):
                    a, b = b, a
                    my_col, their_col = their_col, my_col
                pairs[key] = {'primary': a, 'secondary': b, 'phone': format_phone(k), 'how': f'{my_col} vs {their_col}'}
    # oldest primaries first so chains (A<-B, A<-C) resolve cleanly
    return sorted(pairs.values(), key=lambda p: (parse_hs_date(p['primary']['created']), int(p['primary']['id'])))


# =====================================================================
# CF1 GUIDELINE RULES -> what to PATCH onto the primary before merging
# =====================================================================

def guideline_changes(primary, secondary, lead_status_vals, contact_type_vals, lead_source_vals):
    """Returns {property: new_value} for the primary. Empty dict = nothing to change."""
    changes = {}

    # Lead status: DO NOT CONTACT > On A Deal / NA. Applies if either record has one.
    for label in LEAD_STATUS_PRIORITY:
        v = lead_status_vals[label]
        if primary['lead_status'] == v:
            break                                  # primary already has the top-priority one present
        if secondary['lead_status'] == v:
            changes[LEAD_STATUS_PROP] = v
            break

    # Contact type: protected on secondary overrides non-protected on primary. Primary keeps its own if protected.
    protected = set(contact_type_vals.values())
    if primary['contact_type'] not in protected and secondary['contact_type'] in protected:
        changes[CONTACT_TYPE_PROP] = secondary['contact_type']

    # Broker's Client: if it shows up anywhere on either record, both fields on the primary get it.
    bc_type = contact_type_vals[BROKERS_CLIENT]
    bc_source = lead_source_vals[BROKERS_CLIENT]
    final_type = changes.get(CONTACT_TYPE_PROP, primary['contact_type'])
    is_bc = (primary['contact_type'] == bc_type or secondary['contact_type'] == bc_type or
             primary['lead_source'] == bc_source or secondary['lead_source'] == bc_source)
    if is_bc:
        if final_type != bc_type:
            changes[CONTACT_TYPE_PROP] = bc_type
        if primary['lead_source'] != bc_source:
            changes[LEAD_SOURCE_PROP] = bc_source

    return changes


def describe_changes(changes, prop_defs):
    """'hs_lead_status: Open -> DO NOT CONTACT' style text for the sheet."""
    parts = []
    for prop, val in changes.items():
        label = next((o['label'] for o in prop_defs.get(prop, {}).get('options', []) if o['value'] == val), val)
        parts.append(f'{prop} -> {label}')
    return '; '.join(parts)


# =====================================================================
# GOOGLE SHEETS
# =====================================================================

LOG_HEADER = ['Run', 'Status', 'Primary ID', 'Secondary ID', 'First Name', 'Matched Phone', 'Matched How',
              'Primary Changes', 'Merge Result',
              'Primary Last Name', 'Primary Created', 'Primary Deals', 'Primary Lead Status', 'Primary Contact Type',
              'Primary Lead Source', 'Primary Phone', 'Primary Mobile',
              'Secondary Last Name', 'Secondary Created', 'Secondary Deals', 'Secondary Lead Status',
              'Secondary Contact Type', 'Secondary Lead Source', 'Secondary Phone', 'Secondary Mobile',
              'Primary Link', 'Secondary Link']
SNAP_FIXED = ['Run', 'Record ID', 'Role', 'Pair', 'Deal IDs', 'Company IDs']


def get_or_create_tab(sh, title, header):
    try:
        ws = sh.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=1000, cols=max(26, len(header)))
        ws.append_row(header, value_input_option='RAW')
        ws.freeze(rows=1)
    return ws


def record_link(cid):
    return f'https://app.hubspot.com/contacts/{PORTAL_ID}/record/0-1/{cid}'


def write_snapshot(ws, records, prop_names, deals, companies, roles):
    existing = ws.row_values(1)
    header = existing if existing else SNAP_FIXED + prop_names
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
    print(f'Run {RUN_ID} | MERGE_ENABLED={MERGE_ENABLED} | LOOKBACK_DAYS={LOOKBACK_DAYS}')
    gc = gspread.service_account_from_dict(GOOGLE_CREDENTIALS)
    sh = gc.open_by_key(SHEET_ID)
    log_ws = get_or_create_tab(sh, LOG_TAB, LOG_HEADER)
    snap_ws = get_or_create_tab(sh, SNAPSHOT_TAB, SNAP_FIXED)

    print('Loading property definitions...')
    prop_defs = fetch_property_defs()
    prop_names = sorted(prop_defs.keys())
    lead_status_vals = build_value_map(prop_defs, LEAD_STATUS_PROP, LEAD_STATUS_PRIORITY)
    contact_type_vals = build_value_map(prop_defs, CONTACT_TYPE_PROP, PROTECTED_CONTACT_TYPES)
    lead_source_vals = build_value_map(prop_defs, LEAD_SOURCE_PROP, [BROKERS_CLIENT])

    print('Pulling contacts from HubSpot...')
    contacts = fetch_all_contacts()
    pairs = find_pairs(contacts)
    print(f'{len(pairs)} duplicate pairs found')
    if not pairs:
        print('No duplicates. Done.')
        return

    for p in pairs:
        p['changes'] = guideline_changes(p['primary'], p['secondary'],
                                         lead_status_vals, contact_type_vals, lead_source_vals)
        p['changes_text'] = describe_changes(p['changes'], prop_defs)

    # ---- Snapshot FIRST (before anything is touched) ----
    roles = {}
    for p in pairs:
        pair = f"{p['primary']['id']} <- {p['secondary']['id']}"
        roles[p['primary']['id']] = ('primary', pair)
        roles[p['secondary']['id']] = ('secondary', pair)
    ids = list(roles.keys())
    print(f'Snapshotting {len(ids)} records with all properties...')
    records = fetch_full_records(ids, prop_names)
    deals = fetch_associations(ids, 'deals')
    companies = fetch_associations(ids, 'companies')
    write_snapshot(snap_ws, records, prop_names, deals, companies, roles)
    print(f'Appended {len(records)} rows to {SNAPSHOT_TAB}')

    # ---- Merge (or not) ----
    merged_into = {}            # secondary id -> primary id it was folded into this run
    for p in pairs:
        prim, sec = p['primary']['id'], p['secondary']['id']
        if not MERGE_ENABLED:
            p['status'], p['result'] = 'proposed', 'MERGE_ENABLED is off'
            continue
        # follow chains: if this primary was already merged away, use its survivor
        while prim in merged_into:
            prim = merged_into[prim]
        if sec in merged_into:
            p['status'], p['result'] = 'skipped', f'secondary already merged into {merged_into[sec]}'
            continue
        if prim == sec:
            p['status'], p['result'] = 'skipped', 'already the same record'
            continue
        try:
            if p['changes']:
                patch_contact(prim, p['changes'])
            merge_contacts(prim, sec)
            merged_into[sec] = prim
            p['status'], p['result'] = 'merged', f'{sec} merged into {prim}'
            print(f'  merged {sec} -> {prim}')
        except Exception as e:
            p['status'], p['result'] = 'failed', str(e)[:300]
            print(f'  FAILED {sec} -> {prim}: {e}')
        time.sleep(0.25)

    # ---- Merge Log ----
    log_rows = []
    for p in pairs:
        a, b = p['primary'], p['secondary']
        log_rows.append([
            RUN_ID, p['status'], a['id'], b['id'], a['first_raw'], p['phone'], p['how'],
            p['changes_text'], p['result'],
            a['last_raw'], a['created'], a['deals'], a['lead_status'], a['contact_type'], a['lead_source'],
            a['phone_raw'], a['mobile_raw'],
            b['last_raw'], b['created'], b['deals'], b['lead_status'], b['contact_type'], b['lead_source'],
            b['phone_raw'], b['mobile_raw'],
            record_link(a['id']), record_link(b['id']),
        ])
    log_ws.append_rows(log_rows, value_input_option='RAW')
    counts = {}
    for p in pairs:
        counts[p['status']] = counts.get(p['status'], 0) + 1
    print(f'Appended {len(log_rows)} rows to {LOG_TAB}: {counts}. Done.')


if __name__ == '__main__':
    main()
