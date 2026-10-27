"""
HubSpot duplicate finder + merger.  Runs daily on GitHub Actions.

HOW TO READ THIS FILE
  The script is split into sections, top to bottom:
    1. SETTINGS           - knobs you might change, pulled from env vars / GitHub secrets
    2. PHONE NORMALIZATION - turns messy phone strings into a clean comparable key
    3. HUBSPOT API         - thin wrappers around the handful of HubSpot endpoints we use
    4. GUIDELINE LOOKUP    - turns dropdown labels into HubSpot's internal values
    5. MATCHING            - finds duplicate pairs and decides which record is primary
    6. GUIDELINE RULES     - the CF1 contact rules that decide what gets written to the primary
    7. GOOGLE SHEETS       - writing the Merge Log and Snapshot tabs
    8. MAIN                - the actual run order: load, match, snapshot, merge, log

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

import json      # parsing the Google credentials JSON string
import os        # reading env vars (the GitHub secrets land here)
import re        # regex, used heavily in phone cleaning
import sys       # sys.exit() to stop the run hard if HubSpot keeps failing
import time      # time.sleep() to stay under HubSpot's rate limit
from datetime import datetime, timedelta, timezone

import gspread   # Google Sheets client
import requests  # HTTP calls to HubSpot


# =====================================================================
# 1. SETTINGS
# Everything here comes from GitHub secrets/variables so nothing sensitive lives in the code.
# os.environ['X'] crashes if X is missing (on purpose: the run should die loudly without a token).
# os.environ.get('X', default) is for optional ones.
# =====================================================================

HUBSPOT_TOKEN = os.environ['HUBSPOT_TOKEN']
SHEET_ID = os.environ['SHEET_ID']
GOOGLE_CREDENTIALS = json.loads(os.environ['GOOGLE_CREDENTIALS'])   # the JSON file contents -> dict
PORTAL_ID = os.environ.get('HUBSPOT_PORTAL_ID', '')                 # only used to build record links
LOOKBACK_DAYS = int(os.environ.get('LOOKBACK_DAYS', '3'))
MERGE_ENABLED = os.environ.get('MERGE_ENABLED', 'false').strip().lower() == 'true'   # anything but 'true' = off

LOG_TAB = 'Merge Log'
SNAPSHOT_TAB = 'Snapshot'

HS = 'https://api.hubapi.com'
HEADERS = {'Authorization': f'Bearer {HUBSPOT_TOKEN}', 'Content-Type': 'application/json'}

# Only these properties are pulled during the full 185k-contact scan. Keeping this list short is
# what keeps the pull fast. The Snapshot step pulls EVERY property, but only for the pairs found.
MATCH_PROPS = ['firstname', 'lastname', 'phone', 'mobilephone', 'createdate', 'hs_lastmodifieddate',
               'num_associated_deals', 'hs_lead_status', 'contact_type_2_0', 'sales_lead_source_2_0']

# The three properties the CF1 guideline rules care about (HubSpot internal names).
LEAD_STATUS_PROP = 'hs_lead_status'
CONTACT_TYPE_PROP = 'contact_type_2_0'
LEAD_SOURCE_PROP = 'sales_lead_source_2_0'

# Written here as the LABELS you see in HubSpot. HubSpot actually stores an internal value behind
# each label (e.g. label "Broker's Client" might be stored as "brokers_client"). Section 4 looks up
# the real internal values at runtime so you never have to hunt for them.
LEAD_STATUS_PRIORITY = ['DO NOT CONTACT', 'On A Deal / NA']          # order matters: first one wins if both appear
PROTECTED_CONTACT_TYPES = ['Internal', 'Attorney', 'Insurance', 'Title / Escrow',
                           'Investor (capital side)', "Broker's Client", 'Vendor / Partner']
BROKERS_CLIENT = "Broker's Client"

# Stamped on every row this run writes, so you can tell runs apart in the sheet.
RUN_ID = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


# =====================================================================
# 2. PHONE NORMALIZATION
# Goal: turn whatever a human typed into a bare 10-digit string ("6025551234") so two records
# with the same number in different formats compare as equal. Real data in the portal includes
# "(602) 555-1234", "16025551234", "+1 602.555.1234 ext 12", "6025551234x8", two numbers jammed in
# one cell, "tbd", "1", emails, and Excel-mangled "9.17E+11".
# =====================================================================

DIGITS = re.compile(r'\d+')                                    # grabs runs of digits
EXT_SPLIT = re.compile(r'(?i)\s*(?:ext\.?|extension|x)\s*\d')   # finds "ext 12", "ext.709", "x8" so we can cut it off

# Placeholder numbers people type when they don't have a real one. Two strangers who both have
# "5555555555" are NOT a match, so these are treated as "no phone".
JUNK_10 = {'0000000000', '1111111111', '2222222222', '3333333333', '4444444444',
           '5555555555', '6666666666', '7777777777', '8888888888', '9999999999',
           '1234567890', '0123456789', '9876543210'}


def _valid_us(d):
    """Is this 10-digit string a plausible US number?
    North American rules: area code (d[0]) and exchange (d[3]) can't start with 0 or 1."""
    return len(d) == 10 and d[0] not in '01' and d[3] not in '01' and d not in JUNK_10


def _split_us(d):
    """Pull valid US numbers out of a run of digits. Usually returns one number.
    Handles '1' + 10 digits, and two numbers stuck together like '60255512346025559876'.
    Returns [] if the digits don't form a valid number."""
    if not d:
        return []
    for take in (11, 10):                     # try "1 + 10 digits" first, then plain 10
        if len(d) >= take:
            head, rest = d[:take], d[take:]
            if take == 11:
                if not head.startswith('1'):
                    continue
                head = head[1:]               # drop the leading 1
            if _valid_us(head):
                tail = _split_us(rest) if rest else []   # recurse on whatever is left over
                if rest and tail == []:
                    continue                  # leftover digits that aren't a number = this split is wrong
                return [head] + tail
    return []


def phone_keys(raw):
    """The main entry point. Raw phone string in, list of clean comparable keys out.
    Usually one key. Sometimes two (two numbers in one cell). Empty list = unusable."""
    if raw is None:
        return []
    v = str(raw).strip()
    if not v:
        return []
    low = v.lower()
    # Obvious junk: emails, placeholder words, nothing numeric at all
    if '@' in v or re.search(r'^(na|n/a|tbd|none|unknown|null)\b', low) or not re.search(r'\d', v):
        return []
    # Excel turned it into scientific notation; the real digits are gone, nothing to recover
    if re.search(r'(?i)\d\.\d+e\+\d+', v):
        return []
    body = v
    m = EXT_SPLIT.search(v)
    if m:
        body = v[:m.start()]                                # chop the extension off
    body = re.sub(r'(?i)[a-z]+$', '', body).strip()         # chop trailing words like "Work"
    d = ''.join(DIGITS.findall(body))                       # keep only the digits
    intl = v.startswith('+') or d.startswith('00')          # looks international?
    if d.startswith('00'):
        d = d[2:]                                           # "00" is the old-school international prefix
    if not d or len(set(d)) <= 2:                           # empty, or junk like "1212121212"
        return []
    us = _split_us(d)
    if us:
        return us
    # Not a US number but clearly international (+44, +234...). Keep the digits as the key so two
    # records with the same foreign number still match each other.
    if intl and 7 <= len(d) <= 15 and d[0] != '1':
        return [d]
    return []


def format_phone(key):
    """For display in the sheet only: '6025551234' -> '(602) 555-1234'."""
    if len(key) == 10 and key.isdigit():
        return f'({key[:3]}) {key[3:6]}-{key[6:]}'
    return key


def clean_first(s):
    """First name key: trim, collapse spaces, lowercase. 'Mike' and 'mike' match. 'Mike' and 'Michael' don't."""
    return ' '.join((s or '').split()).casefold()


def norm_label(s):
    """Loose compare for dropdown labels: lowercase, curly apostrophe -> straight, collapsed spaces.
    So "Broker's Client" typed here matches "Broker’s Client" in HubSpot."""
    return ' '.join((s or '').replace('\u2019', "'").split()).casefold()


# =====================================================================
# 3. HUBSPOT API
# Every call goes through hs_request() so retry and error handling live in one place.
# The time.sleep(0.11) sprinkled around keeps us under HubSpot's 10 requests/second limit.
# =====================================================================

def hs_request(method, path, **kwargs):
    """One HubSpot call with retries. 429 = rate limited, 5xx = HubSpot hiccup; both get retried
    with a growing wait (1s, 2s, 4s...). Any other error is raised so the caller can log it."""
    for attempt in range(6):
        resp = requests.request(method, HS + path, headers=HEADERS, timeout=60, **kwargs)
        if resp.status_code == 429 or resp.status_code >= 500:
            wait = 2 ** attempt
            print(f'  HubSpot {resp.status_code}, retrying in {wait}s')
            time.sleep(wait)
            continue
        if not resp.ok:
            raise requests.HTTPError(f'{resp.status_code} {resp.text[:300]}', response=resp)
        return resp.json() if resp.text else {}       # merge endpoint returns an empty body on success
    sys.exit(f'HubSpot kept failing on {path}')


def fetch_all_contacts():
    """Walk every contact in the portal, 100 per page, with only MATCH_PROPS.
    HubSpot gives you a cursor ('after') for the next page; when it's missing you're at the end.
    Each contact becomes a small dict with both raw values (for the sheet) and cleaned keys (for matching)."""
    contacts = []
    after = None
    while True:
        params = {'limit': 100, 'properties': ','.join(MATCH_PROPS)}
        if after:
            params['after'] = after
        data = hs_request('GET', '/crm/objects/2026-03/contacts', params=params)
        for r in data.get('results', []):
            p = r.get('properties', {})
            contacts.append({
                'id': str(r['id']),
                'first': clean_first(p.get('firstname')),       # matching key
                'first_raw': p.get('firstname') or '',          # for the sheet
                'last_raw': p.get('lastname') or '',
                'phone_raw': p.get('phone') or '',
                'mobile_raw': p.get('mobilephone') or '',
                'phone_keys': phone_keys(p.get('phone')),       # matching keys
                'mobile_keys': phone_keys(p.get('mobilephone')),
                'created': p.get('createdate') or '',           # decides who is primary
                'modified': p.get('hs_lastmodifieddate') or '', # decides who gets checked this run
                'deals': p.get('num_associated_deals') or '0',
                'lead_status': p.get(LEAD_STATUS_PROP) or '',   # the three guideline properties
                'contact_type': p.get(CONTACT_TYPE_PROP) or '',
                'lead_source': p.get(LEAD_SOURCE_PROP) or '',
            })
        after = data.get('paging', {}).get('next', {}).get('after')
        if len(contacts) % 10000 < 100:
            print(f'  pulled {len(contacts)} contacts')          # progress in the Actions log
        if not after:
            break
        time.sleep(0.11)
    return contacts


def fetch_property_defs():
    """Every contact property definition in the portal: name, label, and for dropdowns, the options.
    Used for two things: the full property list for the Snapshot, and label -> value lookup in section 4."""
    data = hs_request('GET', '/crm/properties/2026-03/0-1')
    return {p['name']: p for p in data.get('results', [])}


def fetch_full_records(ids, prop_names):
    """Every property for a specific list of contact IDs, 100 at a time. Only used for the Snapshot."""
    out = {}
    ids = list(ids)
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        data = hs_request('POST', '/crm/objects/2026-03/contacts/batch/read',
                          json={'properties': prop_names, 'inputs': [{'id': cid} for cid in chunk]})
        for r in data.get('results', []):
            out[str(r['id'])] = r.get('properties', {})
        time.sleep(0.11)
    return out


def fetch_associations(ids, to_object):
    """Deal IDs (or company IDs) attached to each contact, as a comma-separated string.
    to_object is 'deals' or 'companies'. Goes in the Snapshot so you know what was linked pre-merge."""
    out = {}
    ids = list(ids)
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        data = hs_request('POST', f'/crm/associations/2026-03/contacts/{to_object}/batch/read{to_object}',
                          json={'inputs': [{'id': cid} for cid in chunk]})
        for r in data.get('results', []):
            out[str(r['from']['id'])] = ', '.join(str(t['toObjectId']) for t in r.get('to', []))
        time.sleep(0.11)
    return out


def patch_contact(cid, props):
    """Update properties on one contact. This is how the guideline values get onto the primary
    BEFORE the merge, so HubSpot's 'primary wins' behaviour keeps them."""
    hs_request('PATCH', f'/crm/objects/2026-03/contacts/{cid}', json={'properties': props})


def merge_contacts(primary_id, secondary_id):
    """The actual merge. Not reversible.
    IMPORTANT: by default HubSpot gives the merged record a NEW Record ID. Both old IDs stop existing
    and are stored in the 'Merged Contact IDs' property. (Unless the portal is in the 'Primary ID
    Preservation' beta, in which case the primary keeps its ID.) Either way, the API returns the
    resulting record, so we return its ID and never assume the primary's ID survived."""
    data = hs_request('POST', '/crm/objects/2026-03/contacts/merge',
                      json={'primaryObjectId': str(primary_id), 'objectIdToMerge': str(secondary_id)})
    return str(data.get('id') or primary_id)


# =====================================================================
# 4. GUIDELINE VALUE LOOKUP (label -> internal value)
# HubSpot stores dropdown picks as an internal value, not the label you see. The API returns and
# expects the internal value. This turns the labels written in SETTINGS into whatever the portal
# actually uses, so the rules in section 6 compare apples to apples.
# =====================================================================

def build_value_map(prop_defs, prop_name, labels):
    """Returns {label: internal value} for the labels we care about.
    Tries matching the label first, then the value itself (in case the label IS the value).
    If a label isn't found, warns in the log and uses the label as-is so the run doesn't die."""
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
# 5. MATCHING
# Build an index of (first name, phone key) -> contacts that have it, across ALL contacts.
# Then only the contacts modified recently (LOOKBACK_DAYS) are looked up in that index.
# That's what makes a daily run cheap while still catching a new contact that matches a 5-year-old one.
# =====================================================================

def parse_hs_date(s):
    """HubSpot dates look like '2024-11-25T10:14:03.123Z'. Unparseable -> far future, so a record
    with a missing date never wins 'oldest'."""
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00'))
    except Exception:
        return datetime.max.replace(tzinfo=timezone.utc)


def find_pairs(contacts):
    """Returns a list of pair dicts: {primary, secondary, phone, how}.
    Each unordered pair of IDs appears once, no matter how many phones they share."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)

    # The index. Key is (first name, phone key). Value is every (contact, which column) that has it.
    # A contact with a phone and a mobile goes in twice. Contacts with no first name are skipped
    # entirely, because first name is half of the match rule.
    index = {}
    for c in contacts:
        if not c['first']:
            continue
        for k in c['phone_keys']:
            index.setdefault((c['first'], k), []).append((c, 'Phone'))
        for k in c['mobile_keys']:
            index.setdefault((c['first'], k), []).append((c, 'Mobile'))

    # Only recently modified contacts get checked. They're checked against the FULL index though.
    candidates = [c for c in contacts if c['first'] and parse_hs_date(c['modified']) >= cutoff]
    print(f'{len(contacts)} contacts total, {len(candidates)} modified in last {LOOKBACK_DAYS} days')

    pairs = {}
    for c in candidates:
        # Every phone key this contact has, tagged with which column it came from
        mine = [(k, 'Phone') for k in c['phone_keys']] + [(k, 'Mobile') for k in c['mobile_keys']]
        for k, my_col in mine:
            for other, their_col in index.get((c['first'], k), []):
                if other['id'] == c['id']:
                    continue                                # a contact always matches itself; ignore
                key = tuple(sorted((c['id'], other['id'])))  # (A,B) and (B,A) are the same pair
                if key in pairs:
                    continue
                a, b = c, other
                # Older record becomes primary. Compare (create date, id) so ties are deterministic.
                if (parse_hs_date(b['created']), int(b['id'])) < (parse_hs_date(a['created']), int(a['id'])):
                    a, b = b, a
                    my_col, their_col = their_col, my_col
                pairs[key] = {'primary': a, 'secondary': b, 'phone': format_phone(k), 'how': f'{my_col} vs {their_col}'}

    # Oldest primaries first. If A<-B and A<-C both exist, A gets both merges in order. If B<-C also
    # exists, by the time we reach it B is already gone and main() redirects C into A.
    return sorted(pairs.values(), key=lambda p: (parse_hs_date(p['primary']['created']), int(p['primary']['id'])))


# =====================================================================
# 6. CF1 GUIDELINE RULES -> what to PATCH onto the primary before merging
# HubSpot's merge keeps the primary's value on any conflict. These rules say "unless the secondary
# has one of THESE values, in which case the primary must end up with it." We make that happen by
# writing the value onto the primary first, then merging.
# =====================================================================

def guideline_changes(primary, secondary, lead_status_vals, contact_type_vals, lead_source_vals):
    """Returns {property: new_value} to write onto the primary. Empty dict = nothing to change.
    All comparisons use HubSpot internal values (from section 4), not labels."""
    changes = {}

    # Lead status. Walk the priority list top to bottom.
    #   primary already has this one -> done, nothing beats it
    #   secondary has this one       -> primary gets it, done
    #   neither                      -> check the next one down
    for label in LEAD_STATUS_PRIORITY:
        v = lead_status_vals[label]
        if primary['lead_status'] == v:
            break
        if secondary['lead_status'] == v:
            changes[LEAD_STATUS_PROP] = v
            break

    # Contact type. A protected type on the secondary overrides a non-protected one on the primary.
    # If the primary is already protected, it keeps its own (older wins).
    protected = set(contact_type_vals.values())
    if primary['contact_type'] not in protected and secondary['contact_type'] in protected:
        changes[CONTACT_TYPE_PROP] = secondary['contact_type']

    # Broker's Client. If it shows up in Contact Type OR Lead Source on EITHER record,
    # the primary ends up with Broker's Client in BOTH fields. This runs after the contact type
    # rule so it can override whatever that picked.
    bc_type = contact_type_vals[BROKERS_CLIENT]
    bc_source = lead_source_vals[BROKERS_CLIENT]
    final_type = changes.get(CONTACT_TYPE_PROP, primary['contact_type'])   # what contact type is about to be
    is_bc = (primary['contact_type'] == bc_type or secondary['contact_type'] == bc_type or
             primary['lead_source'] == bc_source or secondary['lead_source'] == bc_source)
    if is_bc:
        if final_type != bc_type:
            changes[CONTACT_TYPE_PROP] = bc_type
        if primary['lead_source'] != bc_source:
            changes[LEAD_SOURCE_PROP] = bc_source

    return changes


def describe_changes(changes, prop_defs):
    """Human-readable version for the sheet: 'hs_lead_status -> DO NOT CONTACT; contact_type_2_0 -> Attorney'.
    Looks the label back up so the sheet shows what you'd see in HubSpot, not the internal value."""
    parts = []
    for prop, val in changes.items():
        label = next((o['label'] for o in prop_defs.get(prop, {}).get('options', []) if o['value'] == val), val)
        parts.append(f'{prop} -> {label}')
    return '; '.join(parts)


# =====================================================================
# 7. GOOGLE SHEETS
# Two tabs. Both are append-only logs; nothing is ever deleted or rewritten except the Snapshot
# header row, which grows if new properties get added to the portal.
# value_input_option='RAW' tells Sheets to store exactly what we send, so IDs stay text and
# don't get turned into 2.38E+11.
# =====================================================================

LOG_HEADER = ['Run', 'Status', 'Primary ID', 'Secondary ID', 'First Name', 'Matched Phone', 'Matched How',
              'Primary Changes', 'Merge Result', 'Resulting ID', 'Resulting Link',
              'Primary Last Name', 'Primary Created', 'Primary Deals', 'Primary Lead Status', 'Primary Contact Type',
              'Primary Lead Source', 'Primary Phone', 'Primary Mobile',
              'Secondary Last Name', 'Secondary Created', 'Secondary Deals', 'Secondary Lead Status',
              'Secondary Contact Type', 'Secondary Lead Source', 'Secondary Phone', 'Secondary Mobile',
              'Primary Link', 'Secondary Link']
SNAP_FIXED = ['Run', 'Record ID', 'Role', 'Pair', 'Deal IDs', 'Company IDs']   # Snapshot's first 6 columns; every property follows


def get_or_create_tab(sh, title, header):
    """Open the tab, or create it with a bold-free header row and frozen top row if it's the first run."""
    try:
        ws = sh.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=1000, cols=max(26, len(header)))
        ws.append_row(header, value_input_option='RAW')
        ws.freeze(rows=1)
    return ws


def record_link(cid):
    """Clickable HubSpot URL for the sheet."""
    return f'https://app.hubspot.com/contacts/{PORTAL_ID}/record/0-1/{cid}'


def write_snapshot(ws, records, prop_names, deals, companies, roles):
    """One row per record. Columns = SNAP_FIXED + every property name in the portal.
    If the portal gained a property since the tab was created, it's added to the end of the header
    so old rows stay lined up."""
    existing = ws.row_values(1)
    header = existing if existing else SNAP_FIXED + prop_names
    missing = [p for p in prop_names if p not in header]
    if not existing or missing:
        header = header + missing
        ws.update('A1', [header], value_input_option='RAW')
        ws.freeze(rows=1)
    col = {h: i for i, h in enumerate(header)}      # column name -> position

    rows = []
    for rid, props in records.items():
        row = [''] * len(header)
        row[col['Run']] = RUN_ID
        row[col['Record ID']] = rid
        row[col['Role']] = roles[rid][0]            # 'primary' or 'secondary'
        row[col['Pair']] = roles[rid][1]            # '123 <- 456' so you can find its partner
        row[col['Deal IDs']] = deals.get(rid, '')
        row[col['Company IDs']] = companies.get(rid, '')
        for k, v in props.items():
            if k in col and v not in (None, ''):
                row[col[k]] = str(v)[:49000]        # Sheets caps a cell at 50k characters
        rows.append(row)
    if rows:
        ws.append_rows(rows, value_input_option='RAW')


# =====================================================================
# 8. MAIN
# Run order matters:
#   load property defs -> pull contacts -> find pairs -> work out guideline changes
#   -> SNAPSHOT (always, before anything is touched) -> patch + merge (only if MERGE_ENABLED)
#   -> write Merge Log
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

    # Work out what each primary needs changed. Done for every pair even when merging is off,
    # so the sheet shows you the rule hits before you ever flip the switch.
    for p in pairs:
        p['changes'] = guideline_changes(p['primary'], p['secondary'],
                                         lead_status_vals, contact_type_vals, lead_source_vals)
        p['changes_text'] = describe_changes(p['changes'], prop_defs)

    # ---- Snapshot FIRST (before anything is touched) ----
    # roles maps each record id -> ('primary' or 'secondary', 'A <- B') for the Snapshot columns.
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
    # merged_into maps any ID that no longer exists -> the ID that replaced it THIS run.
    # A merge retires BOTH old IDs and produces a new one (HubSpot default), so after A<-B becomes X:
    #   merged_into = {A: X, B: X}
    # A later pair A<-C follows the chain and merges C into X instead of the dead A.
    # A later pair B<-C sees B is gone and C is still alive, so C merges into X as well.
    merged_into = {}
    for p in pairs:
        prim, sec = p['primary']['id'], p['secondary']['id']
        p['new_id'] = ''
        if not MERGE_ENABLED:
            p['status'], p['result'] = 'proposed', 'MERGE_ENABLED is off'
            continue
        while prim in merged_into:                  # follow the chain to the current survivor
            prim = merged_into[prim]
        while sec in merged_into:
            sec = merged_into[sec]
        if prim == sec:
            p['status'], p['result'] = 'skipped', f'both already merged into {prim}'
            continue
        try:
            if p['changes']:
                patch_contact(prim, p['changes'])   # guideline values onto the primary first
            new_id = merge_contacts(prim, sec)      # then the merge; returns the surviving record's ID
            merged_into[sec] = new_id
            if new_id != prim:
                merged_into[prim] = new_id          # primary's old ID is retired too
            p['new_id'] = new_id
            p['status'], p['result'] = 'merged', f'{sec} merged into {prim}, result is {new_id}'
            print(f'  merged {sec} + {prim} -> {new_id}')
        except Exception as e:
            # One bad pair shouldn't kill the run. Log it and keep going.
            p['status'], p['result'] = 'failed', str(e)[:300]
            print(f'  FAILED {sec} -> {prim}: {e}')
        time.sleep(0.25)

    # ---- Merge Log ----
    # One row per pair, every run, with the status this run gave it.
    log_rows = []
    for p in pairs:
        a, b = p['primary'], p['secondary']
        log_rows.append([
            RUN_ID, p['status'], a['id'], b['id'], a['first_raw'], p['phone'], p['how'],
            p['changes_text'], p['result'], p['new_id'], record_link(p['new_id']) if p['new_id'] else '',
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


# Standard Python: only run main() when this file is executed directly, not when imported.
if __name__ == '__main__':
    main()
