#!/usr/bin/env python3
"""
PreQual Funnel Dashboard
========================
Reuses the existing repo secrets (ZOHO_CLIENT_ID / ZOHO_CLIENT_SECRET /
ZOHO_REFRESH_TOKEN / TIINY_API_KEY) and the same tiiny.host upload path, so no
new refresh token is needed. Publishes to paymentcloud-uw.tiiny.site.

It tracks a configurable roster of reps and, for every lead they push to
"Sent App to Merchant", measures how long the deal takes to move through
underwriting:

  Anchor : lead -> "Sent App to Merchant"                  [Lead Status History]
  S1     : "Sent App to Merchant" -> "App in Bank"          [Lead Status History]  historical
  S2     : "App in Bank" -> UDW Notes first populated        [daily snapshot]        forward-only
  S3     : UDW Notes first populated -> "UW / Stips Needed"  [snapshot + DealHistory]forward-only start
  S4     : "UW / Stips Needed" -> Approved / Declined         [DealHistory]           historical

UDW Notes is NOT history-tracked in CRM, so S2/S3 accrue forward from the first
run: we persist the first run in which each deal shows a non-empty UDW_Notes in
STATE_FILE. For that to survive between runs, the workflow must commit STATE_FILE
back to the repo (see the updated inday_report.yml).

  python generate_inday_report.py          # live run: build + upload
  python generate_inday_report.py --demo    # render bundled real sample, no API
"""

from __future__ import annotations
import os, sys, re, io, json, zipfile, statistics
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import requests

# ----------------------------------------------------------------------------
# CONFIG  (edit here)
# ----------------------------------------------------------------------------

REPS = {
    "Hovak Vartevanian":  "1429371000290569001",
    "Richard McCausland": "1429371000061247891",
    "Jordan Koestner":    "1429371000364991270",
    "Nathan Wilkie":      "1429371000381063121",
    "Samuel Bender":      "1429371000646655001",
}

PREQUAL_STATUS = "Pre-Qualification Received"  # step 1 anchor (clock starts here)
SENT_STATUS    = "Sent App to Merchant"        # lead status
BANK_STATUS    = "App in Bank"                 # lead status
UW_STIPS       = "UW / Stips Needed"           # deal stage
APPROVED       = "Approved"                    # deal stage  (EXCLUDED from view)
DECLINED       = "Declined"                    # deal stage  (kept, closed)

# Funnel segments and their (fast_max, watch_max) thresholds in DAYS:
#   S1 Pre-Qual Received -> Sent App      S2 Sent App -> App in Bank
#   S3 App in Bank -> UDW Note            S4 UDW Note -> UW / Stips
#   S5 UW / Stips -> Decision (open while in-flight)
THRESHOLDS = {
    "S1": (2.0, 5.0),
    "S2": (1.0, 3.0),
    "S3": (1.0, 3.0),
    "S4": (1.0, 3.0),
    "S5": (2.0, 7.0),
}
SEG_KEYS = ["S1", "S2", "S3", "S4", "S5"]

LOOKBACK_DAYS = 120          # how far back to FETCH transitions
BASELINE_CLEAN_DAYS = 45     # FIRST RUN ONLY: drop items with no activity in the
                             # last 45 days. Persisted, so later runs never re-trim.
DISPLAY_TZ    = ZoneInfo("America/Los_Angeles")   # all displayed times in PT
STATE_FILE    = "udw_snapshot_state.json"
OUTPUT_FILE   = "index.html"

# Publishes to the same site the In-Day report used -> this OVERWRITES it.
# Change this (and create the subdomain on tiiny.host) to publish elsewhere.
TIINY_DOMAIN  = "paymentcloud-uw"

# Zoho hosts (US DC, matching the existing setup).
ACCOUNTS_HOST = os.environ.get("ZOHO_ACCOUNTS_HOST", "https://accounts.zoho.com")
API_HOST      = os.environ.get("ZOHO_API_HOST", "https://www.zohoapis.com")
COQL_URL      = f"{API_HOST}/crm/v7/coql"   # v7 — v2/v3 reject these queries

REP_IDS   = set(REPS.values())
ID_TO_REP = {v: k for k, v in REPS.items()}

# ----------------------------------------------------------------------------
# ZOHO  (same auth + COQL pattern as the other reports)
# ----------------------------------------------------------------------------

def _access_token() -> str:
    r = requests.post(f"{ACCOUNTS_HOST}/oauth/v2/token", data={
        "refresh_token": os.environ["ZOHO_REFRESH_TOKEN"],
        "client_id":     os.environ["ZOHO_CLIENT_ID"],
        "client_secret": os.environ["ZOHO_CLIENT_SECRET"],
        "grant_type":    "refresh_token",
    }, timeout=30)
    r.raise_for_status()
    tok = r.json().get("access_token")
    if not tok:
        raise RuntimeError(f"Token refresh failed: {r.text}")
    return tok


def coql(token: str, query: str) -> list[dict]:
    """COQL with LIMIT offset,count pagination (pages of 200)."""
    headers = {"Authorization": f"Zoho-oauthtoken {token}",
               "Content-Type": "application/json"}
    rows, offset, page = [], 0, 200
    while True:
        q = f"{query} limit {offset}, {page}" if offset else f"{query} limit {page}"
        r = requests.post(COQL_URL, headers=headers, json={"select_query": q}, timeout=60)
        if r.status_code == 204:           # no records
            break
        r.raise_for_status()
        payload = r.json()
        rows.extend(payload.get("data", []))
        if not payload.get("info", {}).get("more_records"):
            break
        offset += page
    return rows


def _id_list(ids) -> str:
    return "(" + ",".join(f"'{i}'" for i in ids) + ")"

# COQL gotchas baked in: no `!=` (use `not in`), no two range conditions on one
# datetime field, no negative tz offsets in literals. We avoid all three by not
# putting date literals in the WHERE at all and windowing client-side instead.

def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)

def _within(ts: str, cutoff: datetime) -> bool:
    return _parse(ts) >= cutoff

def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())

# ----------------------------------------------------------------------------
# FETCH
# ----------------------------------------------------------------------------

def _earliest(rows, key_path):
    """Earliest Modified_Time per parent record id along key_path (lookup)."""
    out = {}
    for r in rows:
        pid = (r.get(key_path) or {}).get("id")
        if not pid:
            continue
        t = r["Modified_Time"]
        if pid not in out or _parse(t) < _parse(out[pid]):
            out[pid] = t
    return out


def fetch_lead_transitions(token, moved_to, cutoff):
    q = (f"select id, Full_Name, Moved_To__s, Modified_Time from Lead_Status_History "
         f"where Full_Name.Owner in {_id_list(REP_IDS)} and Moved_To__s = '{moved_to}' "
         f"order by Modified_Time desc")
    return [r for r in coql(token, q) if _within(r["Modified_Time"], cutoff)]


def fetch_deal_transitions(token, moved_to, cutoff):
    q = (f"select id, Potential_Name, Moved_To__s, Modified_Time from DealHistory "
         f"where Potential_Name.Owner in {_id_list(REP_IDS)} and Moved_To__s = '{moved_to}' "
         f"order by Modified_Time desc")
    return [r for r in coql(token, q) if _within(r["Modified_Time"], cutoff)]


def fetch_records(token, module, fields, ids):
    out, ids = {}, list(ids)
    for i in range(0, len(ids), 100):
        q = (f"select {', '.join(fields)} from {module} "
             f"where id in {_id_list(ids[i:i+100])}")
        for r in coql(token, q):
            out[r["id"]] = r
    return out

# ----------------------------------------------------------------------------
# STATE  (persisted across runs: one-time baseline + UDW blank->value snapshot)
# ----------------------------------------------------------------------------

def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
    except FileNotFoundError:
        s = {}
    s.setdefault("baseline_cutoff", None)   # set on first run, never changed
    s.setdefault("udw", {})                 # deal_id -> first-seen-populated iso
    return s

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def update_udw_snapshot(udw_state, deals, now_iso):
    for did, d in deals.items():
        if (d.get("UDW_Notes") or "").strip() and did not in udw_state:
            udw_state[did] = now_iso
    return udw_state

# ----------------------------------------------------------------------------
# COMPUTE
# ----------------------------------------------------------------------------

def speed(seg_key, days):
    if days is None:
        return "pending"
    fast, watch = THRESHOLDS[seg_key]
    return "fast" if days <= fast else ("watch" if days <= watch else "slow")

def delta_days(a, b):
    if not a or not b:
        return None
    return round((_parse(b) - _parse(a)).total_seconds() / 86400, 2)


def _max_ts(*ts):
    vals = [_parse(t) for t in ts if t]
    return max(vals) if vals else None


def compute_segments(prequal_t, sent_t, bank_t, udw_t, uw_t, declined_t, now_iso):
    """Return (segs, open_key, open_age, is_open, last_event_ts).
    Completed segments show their duration; the segment the deal is currently
    sitting in shows its running age (still counting)."""
    starts = {"S1": prequal_t, "S2": sent_t, "S3": bank_t, "S4": udw_t, "S5": uw_t}
    segs = {
        "S1": delta_days(prequal_t, sent_t),
        "S2": delta_days(sent_t, bank_t),
        "S3": delta_days(bank_t, udw_t),
        "S4": delta_days(udw_t, uw_t),
        "S5": delta_days(uw_t, declined_t) if declined_t else None,
    }
    if declined_t:                                  # closed
        return segs, None, None, False, declined_t
    last_key = last_t = None
    for k in SEG_KEYS:                              # furthest milestone reached
        if starts[k]:
            last_key, last_t = k, starts[k]
    if last_key:                                    # open: age of current segment
        open_age = delta_days(last_t, now_iso)
        segs[last_key] = open_age
        return segs, last_key, open_age, True, last_t
    return segs, None, None, True, None


def build_context_live():
    token = _access_token()
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    now_iso = datetime.now(timezone.utc).isoformat()

    # Lead-side milestones. Universe is anchored on Pre-Qualification Received.
    prequal = _earliest(fetch_lead_transitions(token, PREQUAL_STATUS, cutoff), "Full_Name")
    sent    = _earliest(fetch_lead_transitions(token, SENT_STATUS,    cutoff), "Full_Name")
    bank    = _earliest(fetch_lead_transitions(token, BANK_STATUS,    cutoff), "Full_Name")
    universe = set(prequal) | set(sent) | set(bank)
    leads = fetch_records(token, "Leads",
                          ["id", "Company", "Last_Name", "Owner", "Lead_Status"], universe)

    # Deal side. Approvals excluded; declines kept (closed).
    uw       = _earliest(fetch_deal_transitions(token, UW_STIPS, cutoff), "Potential_Name")
    approved = _earliest(fetch_deal_transitions(token, APPROVED, cutoff), "Potential_Name")
    declined = _earliest(fetch_deal_transitions(token, DECLINED, cutoff), "Potential_Name")
    approved_ids = set(approved)
    deals = fetch_records(token, "Deals",
                          ["id", "Deal_Name", "Owner", "Stage", "Amount", "UDW_Notes", "Created_Time"],
                          set(uw) | set(declined))

    # State: set the one-time baseline on first run; capture UDW blank->value.
    state = load_state()
    if not state["baseline_cutoff"]:
        state["baseline_cutoff"] = (datetime.now(timezone.utc)
                                    - timedelta(days=BASELINE_CLEAN_DAYS)).isoformat()
    baseline_cut = _parse(state["baseline_cutoff"])
    update_udw_snapshot(state["udw"], deals, now_iso)
    save_state(state)
    udw_state = state["udw"]

    deal_by_name = {}
    for did, d in deals.items():
        n = _norm(d.get("Deal_Name"))
        if n:
            deal_by_name.setdefault(n, did)

    rows, used = [], set()

    # (A) Lead-anchored rows (from Pre-Qual onward), EXCLUDING approved deals.
    for lid in universe:
        lead = leads.get(lid, {})
        owner = (lead.get("Owner") or {}).get("id")
        if owner not in REP_IDS:
            continue
        name = lead.get("Company") or lead.get("Last_Name") or "(unnamed lead)"
        did = deal_by_name.get(_norm(lead.get("Company") or lead.get("Last_Name")))
        if did and did in approved_ids:
            used.add(did)
            continue
        if did:
            used.add(did)
        d = deals.get(did, {}) if did else {}
        prequal_t, sent_t, bank_t = prequal.get(lid), sent.get(lid), bank.get(lid)
        udw_t = udw_state.get(did) if did else None
        uw_t = uw.get(did) if did else None
        declined_t = declined.get(did) if did else None
        segs, open_key, open_age, is_open, _ = compute_segments(
            prequal_t, sent_t, bank_t, udw_t, uw_t, declined_t, now_iso)
        status = ("Declined" if declined_t else
                  (d.get("Stage") or lead.get("Lead_Status") or "Lead"))
        rows.append({
            "rep": ID_TO_REP[owner],
            "merchant": d.get("Deal_Name") or name,
            "amount": d.get("Amount"),
            "stage": status,
            "submitted": d.get("Created_Time") or sent_t or prequal_t,
            "decision": "Declined" if declined_t else None,
            "_open": is_open, "_open_key": open_key, "_open_age": open_age,
            "_last": _max_ts(prequal_t, sent_t, bank_t, udw_t, uw_t, declined_t,
                             d.get("Created_Time")),
            **segs,
        })

    # (B) Deal-only rows: in-UW or declined deals (never approved) not matched
    # to a tracked lead. S1/S2 unknown (no lead link); attributed by deal Owner.
    for did in (set(uw) | set(declined)):
        if did in used or did in approved_ids:
            continue
        d = deals.get(did, {})
        owner = (d.get("Owner") or {}).get("id")
        if owner not in REP_IDS:
            continue
        udw_t = udw_state.get(did)
        uw_t = uw.get(did)
        declined_t = declined.get(did)
        segs, open_key, open_age, is_open, _ = compute_segments(
            None, None, None, udw_t, uw_t, declined_t, now_iso)
        status = "Declined" if declined_t else (d.get("Stage") or "")
        rows.append({
            "rep": ID_TO_REP[owner],
            "merchant": d.get("Deal_Name") or "(deal)",
            "amount": d.get("Amount"),
            "stage": status,
            "submitted": d.get("Created_Time"),
            "decision": "Declined" if declined_t else None,
            "_open": is_open, "_open_key": open_key, "_open_age": open_age,
            "_last": _max_ts(d.get("Created_Time"), udw_t, uw_t, declined_t),
            **segs,
        })

    # One-time clean: drop anything with no activity since the persisted baseline
    # (= 45 days before the first run). The cutoff is fixed, so later runs never
    # re-trim and new/aging deals are always kept.
    rows = [r for r in rows if r["_last"] and r["_last"] >= baseline_cut]

    return assemble(rows, now_iso, mode="live")


def assemble(rows, now_iso, mode):
    by_rep = {name: [] for name in REPS}
    for r in rows:
        by_rep.setdefault(r["rep"], []).append(r)
    reps = []
    for name, deals in by_rep.items():
        ages = [d["_open_age"] for d in deals
                if d.get("_open") and d.get("_open_age") is not None]
        # a stall = an OPEN deal whose CURRENT segment has run past its watch line.
        stalls = sum(1 for d in deals
                     if d.get("_open") and d.get("_open_age") is not None
                     and speed(d["_open_key"], d["_open_age"]) == "slow")
        reps.append({
            "name": name,
            "deals": sorted(deals, key=lambda d: (not d.get("_open"),
                                                  -(d.get("_open_age") or 0))),
            "n": len(deals),
            "open_n": sum(1 for d in deals if d.get("_open")),
            "median_age": round(statistics.median(ages), 1) if ages else None,
            "oldest": max(ages) if ages else None,
            "stalls": stalls,
        })
    reps.sort(key=lambda r: (-(r["stalls"]), -((r["oldest"] or 0))))
    return {"reps": reps, "generated": now_iso, "mode": mode}

# ----------------------------------------------------------------------------
# RENDER
# ----------------------------------------------------------------------------
import html as _html

SEG_LABELS = {
    "S1": "Pre-Qual \u2192 Sent App",
    "S2": "Sent App \u2192 App in Bank",
    "S3": "App in Bank \u2192 UDW Note",
    "S4": "UDW Note \u2192 UW / Stips",
    "S5": "UW / Stips \u2192 Decision",
}

def _fmt_days(v):
    if v is None: return "\u2014"
    if v < 1:     return f"{v*24:.0f}h"
    return f"{v:.1f}d"

def _fmt_amount(a):
    return "" if a in (None, 0) else f"${a:,.0f}"

def _fmt_date(iso):
    if not iso:
        return ""
    try:
        return _parse(iso).astimezone(DISPLAY_TZ).strftime("%b %-d")
    except Exception:
        return ""

def _seg_cell(seg_key, value, is_open=False):
    cls = speed(seg_key, value)
    val = _fmt_days(value) if value is not None else "pending"
    open_cls = " seg--open" if is_open else ""
    return (f'<div class="seg seg--{cls}{open_cls}"><span class="seg__k">{seg_key}</span>'
            f'<span class="seg__v">{val}</span></div>')

def _deal_row(d):
    flag = ""
    if d.get("_open") and d.get("_open_age") is not None:
        sp = speed(d["_open_key"], d["_open_age"])
        if sp == "slow":
            flag = '<span class="flag">STALL</span>'
        elif sp == "watch":
            flag = '<span class="flag flag--watch">watch</span>'
    track = "".join(_seg_cell(k, d.get(k), is_open=(d.get("_open_key") == k))
                    for k in SEG_KEYS)
    dec = d.get("decision") or d.get("stage") or ""
    dec_cls = "ok" if dec == "Approved" else ("bad" if dec == "Declined" else "neutral")
    sub = _fmt_date(d.get("submitted"))
    meta = " \u00b7 ".join(x for x in (_fmt_amount(d.get("amount")),
                                       (f"Sub {sub}" if sub else "")) if x)
    return f"""
    <div class="row">
      <div class="row__id"><span class="merchant">{_html.escape(str(d['merchant']))}</span>
        <span class="amt">{meta}</span></div>
      <div class="track">{track}</div>
      <div class="row__end"><span class="stage stage--{dec_cls}">{_html.escape(dec)}</span>{flag}</div>
    </div>"""

def _rep_card(rep):
    stall_cls = "metric__v--bad" if rep["stalls"] else ""
    rows = "".join(_deal_row(d) for d in rep["deals"]) or \
           '<div class="empty">No in-flight deals in the window.</div>'
    flagged = "rep--flagged" if rep["stalls"] else ""
    return f"""
    <section class="rep {flagged}" data-stalls="{rep['stalls']}" data-name="{_html.escape(rep['name'])}">
      <header class="rep__head"><h2 class="rep__name">{_html.escape(rep['name'])}</h2>
        <div class="rep__metrics">
          <div class="metric"><span class="metric__k">In-flight</span><span class="metric__v">{rep['open_n']}</span></div>
          <div class="metric"><span class="metric__k">Median wait</span><span class="metric__v">{_fmt_days(rep['median_age'])}</span></div>
          <div class="metric"><span class="metric__k">Oldest</span><span class="metric__v">{_fmt_days(rep['oldest'])}</span></div>
          <div class="metric"><span class="metric__k">Stalls</span><span class="metric__v {stall_cls}">{rep['stalls']}</span></div>
        </div></header>
      <div class="rep__rows">{rows}</div>
    </section>"""

def render_html(ctx) -> str:
    gen = _parse(ctx["generated"]).astimezone(DISPLAY_TZ).strftime("%b %-d, %Y \u00b7 %-I:%M %p PT")
    banner = "" if ctx["mode"] == "live" else (
        '<div class="banner">Sample view \u2014 illustrative deals across the funnel. The dashed cell '
        '(\u25cf) is where each deal currently sits, still counting; solid cells are completed segments.</div>')
    cards = "".join(_rep_card(r) for r in ctx["reps"])
    legend = "".join(f'<span class="lg"><b>{k}</b> {v}</span>' for k, v in SEG_LABELS.items())
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Underwriting Velocity \u2014 Rep Monitor</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=Inter:wght@400;500;600&family=IBM+Plex+Mono:wght@500;600&display=swap" rel="stylesheet">
<style>
  :root{{--bg:#e8ecef;--surface:#fff;--ink:#14181c;--muted:#646e78;--line:#dce2e7;
    --teal:#0c6b63;--fast:#1f9d57;--watch:#c4790a;--slow:#d23b3b;--pending:#9aa6b0;}}
  *{{box-sizing:border-box}}
  body{{margin:0;background:var(--bg);color:var(--ink);font-family:Inter,system-ui,sans-serif;
    font-size:14px;line-height:1.45;padding:28px 22px 60px}}
  .wrap{{max-width:1080px;margin:0 auto}}
  .top{{display:flex;justify-content:space-between;align-items:flex-end;gap:24px;flex-wrap:wrap;
    border-bottom:2px solid var(--ink);padding-bottom:16px}}
  .eyebrow{{font:600 11px/1 'IBM Plex Mono',monospace;letter-spacing:.18em;text-transform:uppercase;
    color:var(--teal);margin:0 0 8px}}
  h1{{font-family:'Space Grotesk',sans-serif;font-weight:700;font-size:34px;line-height:1.02;
    letter-spacing:-.01em;margin:0}}
  .sub{{color:var(--muted);margin-top:8px;font-size:13px}} .sub b{{color:var(--ink);font-weight:600}}
  .gen{{font:500 11px/1.4 'IBM Plex Mono',monospace;color:var(--muted);text-align:right}}
  .controls{{display:flex;gap:8px;margin-top:10px;justify-content:flex-end}}
  .ctl{{font:600 11px/1 'IBM Plex Mono',monospace;letter-spacing:.04em;text-transform:uppercase;
    border:1px solid var(--line);background:var(--surface);color:var(--muted);padding:7px 11px;
    border-radius:6px;cursor:pointer}}
  .ctl.on{{border-color:var(--ink);color:var(--ink)}}
  .legend{{display:flex;flex-wrap:wrap;gap:6px 16px;margin:16px 0 22px;
    font:500 11.5px/1.3 'IBM Plex Mono',monospace;color:var(--muted)}}
  .lg b{{color:var(--ink);margin-right:5px}}
  .banner{{background:#fff7e6;border:1px solid #f0d79a;color:#7a5a16;padding:10px 14px;
    border-radius:8px;font-size:12.5px;margin:18px 0 6px}}
  .rep{{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin-top:14px}}
  .rep--flagged{{border-left:4px solid var(--slow)}}
  .rep__head{{display:flex;justify-content:space-between;align-items:center;gap:18px;flex-wrap:wrap;
    padding-bottom:14px;border-bottom:1px solid var(--line)}}
  .rep__name{{font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:19px;margin:0}}
  .rep__metrics{{display:flex;gap:22px;flex-wrap:wrap}}
  .metric{{display:flex;flex-direction:column;align-items:flex-end}}
  .metric__k{{font:500 10px/1.2 'IBM Plex Mono',monospace;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}}
  .metric__v{{font:600 17px/1.1 'IBM Plex Mono',monospace;margin-top:3px}}
  .metric__v--bad{{color:var(--slow)}}
  .rep__rows{{margin-top:6px}}
  .row{{display:grid;grid-template-columns:minmax(140px,1fr) minmax(330px,2.4fr) minmax(110px,.7fr);
    gap:14px;align-items:center;padding:11px 0;border-bottom:1px solid #eef2f4}}
  .row:last-child{{border-bottom:none}}
  .row__id{{display:flex;flex-direction:column;gap:2px;min-width:0}}
  .merchant{{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
  .amt{{font:500 11px/1 'IBM Plex Mono',monospace;color:var(--muted)}}
  .track{{display:grid;grid-template-columns:repeat(5,1fr);gap:4px}}
  .seg{{border-radius:6px;padding:7px 6px;display:flex;flex-direction:column;align-items:center;gap:3px;
    border:1px solid transparent;min-width:0}}
  .seg__k{{font:600 9px/1 'IBM Plex Mono',monospace;letter-spacing:.08em;opacity:.7}}
  .seg__v{{font:600 13px/1 'IBM Plex Mono',monospace}}
  .seg--fast{{background:#e6f5ec;color:#0f6b38;border-color:#bfe3cd}}
  .seg--watch{{background:#fbf0db;color:#8a5407;border-color:#f0d9ad}}
  .seg--slow{{background:#fae3e3;color:#9e2424;border-color:#f0c3c3}}
  .seg--pending{{background:repeating-linear-gradient(135deg,#f2f5f7,#f2f5f7 5px,#eaeef1 5px,#eaeef1 10px);
    color:var(--pending);border-color:#e3e9ed}}
  .seg--pending .seg__v{{font-size:10px;letter-spacing:.03em}}
  .seg--open{{border-style:dashed;border-width:1.5px;box-shadow:inset 0 0 0 1px rgba(0,0,0,.02)}}
  .seg--open .seg__k::after{{content:" \\25CF";font-size:7px;vertical-align:middle}}
  .row__end{{display:flex;align-items:center;gap:8px;justify-content:flex-end;flex-wrap:wrap}}
  .stage{{font:600 11px/1 'IBM Plex Mono',monospace;padding:5px 9px;border-radius:999px;border:1px solid var(--line)}}
  .stage--ok{{background:#e6f5ec;color:#0f6b38;border-color:#bfe3cd}}
  .stage--bad{{background:#fae3e3;color:#9e2424;border-color:#f0c3c3}}
  .stage--neutral{{background:#eef2f4;color:var(--muted)}}
  .flag{{font:700 10px/1 'IBM Plex Mono',monospace;letter-spacing:.04em;text-transform:uppercase;color:#fff;
    background:var(--slow);padding:5px 8px;border-radius:5px}}
  .flag--watch{{background:var(--watch)}}
  .empty{{color:var(--muted);padding:14px 0;font-size:13px}}
  .foot{{margin-top:30px;padding-top:18px;border-top:1px solid var(--line);color:var(--muted);
    font-size:12px;line-height:1.6;max-width:760px}} .foot b{{color:var(--ink)}}
  @media (max-width:720px){{.row{{grid-template-columns:1fr;gap:9px}}.row__end{{justify-content:flex-start}}h1{{font-size:27px}}}}
</style></head>
<body><div class="wrap">
  <div class="top"><div>
    <p class="eyebrow">Management Monitor \u00b7 Confidential</p>
    <h1>Underwriting Velocity</h1>
    <p class="sub">Clock starts when a lead hits <b>Pre-Qualification Received</b>, through five steps to
      a decision. <b>In-flight deals plus recent declines</b> (approvals excluded), across <b>{len(REPS)} reps</b>.
      Whatever's stuck longest in its current step surfaces first.</p>
  </div><div>
    <p class="gen">Generated<br>{gen}</p>
    <div class="controls"><button class="ctl" id="flagBtn">Flagged only</button>
      <button class="ctl" id="sortBtn">Sort: stalls</button></div>
  </div></div>
  <div class="legend">{legend}</div>
  {banner}
  <div id="reps">{cards}</div>
  <div class="foot"><b>How to read this.</b> Each row is an in-flight deal or a recent decline; approvals
    are excluded. The five cells are the funnel steps from Pre-Qualification Received to a decision. Solid
    cells are completed segments (green/amber/red by speed); the dashed cell (\u25cf) is the step the deal is
    sitting in right now, still counting \u2014 that running time is what triggers a <b>STALL</b> flag and the
    stall count. <b>S3</b>/<b>S4</b> depend on when UDW Notes first gets a value, which CRM does not
    history-track, so they read <i>pending</i> until the snapshot captures it. The first run trims anything
    with no activity in the prior 45 days; that baseline is then fixed, so later runs never re-trim. Edit
    the roster or the 45-day baseline at the top of the script.</div>
</div>
<script>
  var flagOnly=false, sortMode='stalls', box=document.getElementById('reps');
  function apply(){{
    var c=[].slice.call(box.querySelectorAll('.rep'));
    c.forEach(function(x){{x.style.display=(flagOnly&&x.dataset.stalls==='0')?'none':'';}});
    c.sort(function(a,b){{return sortMode==='name'?a.dataset.name.localeCompare(b.dataset.name):(+b.dataset.stalls)-(+a.dataset.stalls);}});
    c.forEach(function(x){{box.appendChild(x);}});
  }}
  document.getElementById('flagBtn').onclick=function(){{flagOnly=!flagOnly;this.classList.toggle('on',flagOnly);apply();}};
  document.getElementById('sortBtn').onclick=function(){{sortMode=(sortMode==='stalls')?'name':'stalls';this.textContent='Sort: '+sortMode;apply();}};
</script>
</body></html>"""

# ----------------------------------------------------------------------------
# UPLOAD  (same proven tiiny.host path as the other reports)
# ----------------------------------------------------------------------------

def upload(html_str):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("index.html", html_str.encode("utf-8"))
    buf.seek(0)
    r = requests.put("https://ext.tiiny.host/v1/upload",
                     headers={"x-api-key": os.environ["TIINY_API_KEY"]},
                     files={"files": ("report.zip", buf, "application/zip")},
                     data={"domain": f"{TIINY_DOMAIN}.tiiny.site"}, timeout=60)
    if r.status_code == 200:
        print(f"Live at https://{TIINY_DOMAIN}.tiiny.site/")
    else:
        print(f"tiiny upload failed: {r.status_code} {r.text}")
        sys.exit(1)

# ----------------------------------------------------------------------------
# DEMO  (illustrative deals exercising every stage of the funnel)
# ----------------------------------------------------------------------------

# (rep, merchant, amount, prequal, sent, bank, udw, uw, declined) as DAYS AGO
DEMO = [
    ("Nathan Wilkie",     "Rockwell",                30000, 60, 52, 50, 49, 47, None),  # stuck in UW (S5)
    ("Nathan Wilkie",     "Lynette Escobar Tiling",  15000,  9,  7, None, None, None, None),  # stuck S2
    ("Richard McCausland","Exp Lyft LLC",            15000, 20, 18, 16, 15, 13, None),  # stuck in UW (S5)
    ("Richard McCausland","Cars R Us",              100000, 14, 12, 10,  9,  8,    6),  # declined
    ("Richard McCausland","Be Good Organix LLC",     20000,  5,  3,  2,  1, None, None),# in S4
    ("Jordan Koestner",   "TG Deals LLC",            15000, 30, None, None, None, None, None),  # pre-app stall (S1)
    ("Jordan Koestner",   "Discovery Water Mgmt",    25000,  4,  3,  2, None, None, None),  # in S3
]

def build_context_demo():
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    ago = lambda d: (now - timedelta(days=d)).isoformat() if d is not None else None
    rows = []
    for rep, m, amt, pq, sn, bk, ud, uwd, dec in DEMO:
        pq_t, sn_t, bk_t, ud_t, uw_t, dec_t = (ago(pq), ago(sn), ago(bk),
                                               ago(ud), ago(uwd), ago(dec))
        segs, open_key, open_age, is_open, _ = compute_segments(
            pq_t, sn_t, bk_t, ud_t, uw_t, dec_t, now_iso)
        status = ("Declined" if dec_t else
                  "UW / Stips Needed" if uw_t else
                  "App in Bank" if bk_t else
                  "App Sent" if sn_t else "Pre-Qual Received")
        rows.append({"rep": rep, "merchant": m, "amount": amt, "stage": status,
                     "submitted": sn_t or pq_t, "decision": "Declined" if dec_t else None,
                     "_open": is_open, "_open_key": open_key, "_open_age": open_age,
                     "_last": now_iso, **segs})
    return assemble(rows, now_iso, mode="demo")

# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------

def main():
    demo = "--demo" in sys.argv
    ctx = build_context_demo() if demo else build_context_live()
    html_str = render_html(ctx)
    with open(OUTPUT_FILE, "w") as f:
        f.write(html_str)
    print(f"Wrote {OUTPUT_FILE} "
          f"({sum(len(r['deals']) for r in ctx['reps'])} deals / {len(ctx['reps'])} reps).")
    if not demo:
        upload(html_str)

if __name__ == "__main__":
    main()
