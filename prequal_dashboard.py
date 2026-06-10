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

ANCHOR_MOVED_TO = "Sent App to Merchant"   # lead status that starts the clock
APP_IN_BANK     = "App in Bank"            # lead status, end of S1
UW_STIPS        = "UW / Stips Needed"      # deal stage, end of S3 / start of S4
DECISION_STAGES = ["Approved", "Declined"] # deal stages, end of S4

THRESHOLDS = {                              # (fast_max, watch_max) in DAYS
    "S1": (1.0, 3.0),
    "S2": (0.5, 2.0),
    "S3": (1.0, 3.0),
    "S4": (2.0, 7.0),
}

LOOKBACK_DAYS = 120          # how far back to FETCH transitions (keep generous so
                             # long-cycle deals decided recently still resolve)
ACTIVE_WINDOW_DAYS = 30      # only DISPLAY rows with funnel activity in this window
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
# UDW SNAPSHOT STATE (forward-only capture of blank -> value)
# ----------------------------------------------------------------------------

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def update_udw_snapshot(state, deals, now_iso):
    for did, d in deals.items():
        if (d.get("UDW_Notes") or "").strip() and did not in state:
            state[did] = now_iso
    return state

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


def build_context_live():
    token = _access_token()
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    now_iso = datetime.now(timezone.utc).isoformat()

    # Lead side — the anchor defines our universe (leads each rep pushed).
    sent = _earliest(fetch_lead_transitions(token, ANCHOR_MOVED_TO, cutoff), "Full_Name")
    bank = _earliest(fetch_lead_transitions(token, APP_IN_BANK, cutoff), "Full_Name")
    leads = fetch_records(token, "Leads",
                          ["id", "Company", "Last_Name", "Owner"], sent.keys())

    # Deal side. Approvals are EXCLUDED entirely. Declines are kept (closed,
    # shown with the time they took). Everything else still open is in-flight.
    uw       = _earliest(fetch_deal_transitions(token, UW_STIPS, cutoff), "Potential_Name")
    approved = _earliest(fetch_deal_transitions(token, "Approved", cutoff), "Potential_Name")
    declined = _earliest(fetch_deal_transitions(token, "Declined", cutoff), "Potential_Name")
    approved_ids = set(approved)

    deals = fetch_records(token, "Deals",
                          ["id", "Deal_Name", "Owner", "Stage", "Amount", "UDW_Notes", "Created_Time"],
                          set(uw) | set(declined))

    state = update_udw_snapshot(load_state(), deals, now_iso)
    save_state(state)

    def deal_seg(did):
        """(S4, decision, open?, last_event_ts) for a deal."""
        uw_t = uw.get(did)
        dec_t = declined.get(did)
        if dec_t:                                    # closed: declined
            return (delta_days(uw_t, dec_t) if uw_t else None, "Declined", False, dec_t)
        if uw_t:                                     # open: still in underwriting
            return (delta_days(uw_t, now_iso), None, True, uw_t)
        return (None, None, False, None)

    # Match leads to deals by normalized merchant name (Converted_Deal is empty
    # in this org). Unmatched deals still render on their own (B).
    deal_by_name = {}
    for did, d in deals.items():
        n = _norm(d.get("Deal_Name"))
        if n:
            deal_by_name.setdefault(n, did)

    rows, used = [], set()

    # (A) Lead-anchored rows, EXCLUDING any whose deal was approved.
    for lid, anchor_t in sent.items():
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
        udw_first = state.get(did) if did else None
        bank_t = bank.get(lid)
        uw_t = uw.get(did) if did else None
        s4, decision, is_open, dlast = deal_seg(did) if did else (None, None, False, None)
        rows.append({
            "rep": ID_TO_REP[owner],
            "merchant": d.get("Deal_Name") or name,
            "amount": d.get("Amount"),
            "stage": "Declined" if decision == "Declined" else (d.get("Stage") or "Lead"),
            "has_note": bool((d.get("UDW_Notes") or "").strip()),
            "submitted": d.get("Created_Time") or anchor_t,
            "_last": _max_ts(anchor_t, bank_t, udw_first, uw_t, dlast),
            "_open": is_open,
            "S1": delta_days(anchor_t, bank_t),
            "S2": delta_days(bank_t, udw_first),
            "S3": delta_days(udw_first, uw_t),
            "S4": s4,
            "decision": decision,
        })

    # (B) Deal-only rows: in-UW or declined deals (never approved) not matched
    # to a tracked lead. Attributed by the deal's own Owner.
    for did in (set(uw) | set(declined)):
        if did in used or did in approved_ids:
            continue
        d = deals.get(did, {})
        owner = (d.get("Owner") or {}).get("id")
        if owner not in REP_IDS:
            continue
        udw_first = state.get(did)
        uw_t = uw.get(did)
        s4, decision, is_open, dlast = deal_seg(did)
        rows.append({
            "rep": ID_TO_REP[owner],
            "merchant": d.get("Deal_Name") or "(deal)",
            "amount": d.get("Amount"),
            "stage": "Declined" if decision == "Declined" else (d.get("Stage") or ""),
            "has_note": bool((d.get("UDW_Notes") or "").strip()),
            "submitted": d.get("Created_Time"),
            "_last": _max_ts(d.get("Created_Time"), udw_first, uw_t, dlast),
            "_open": is_open,
            "S1": None,
            "S2": None,
            "S3": delta_days(udw_first, uw_t),
            "S4": s4,
            "decision": decision,
        })

    # Open deals always show (an old one = a live stall). Declines and stale
    # early-funnel leads are pruned to recent activity by the active window.
    active_cut = datetime.now(timezone.utc) - timedelta(days=ACTIVE_WINDOW_DAYS)
    rows = [r for r in rows
            if r["_open"] or (r["_last"] and r["_last"] >= active_cut)]

    return assemble(rows, now_iso, mode="live")


def assemble(rows, now_iso, mode):
    by_rep = {name: [] for name in REPS}
    for r in rows:
        by_rep.setdefault(r["rep"], []).append(r)
    reps = []
    for name, deals in by_rep.items():
        s4s = [d["S4"] for d in deals if d["S4"] is not None]
        # a "stall" is an OPEN deal stuck in underwriting past the watch line;
        # a declined deal is closed and doesn't count, even if it took a while.
        stalls = sum(1 for d in deals
                     if d.get("_open") and d["S4"] is not None
                     and speed("S4", d["S4"]) == "slow")
        reps.append({
            "name": name,
            "deals": sorted(deals, key=lambda d: (not d.get("_open"),
                                                  d["S4"] is None, -(d["S4"] or 0))),
            "n": len(deals),
            "median_s4": round(statistics.median(s4s), 1) if s4s else None,
            "worst_s4": max(s4s) if s4s else None,
            "stalls": stalls,
        })
    reps.sort(key=lambda r: (-(r["stalls"]), -((r["median_s4"] or 0))))
    return {"reps": reps, "generated": now_iso, "mode": mode}

# ----------------------------------------------------------------------------
# RENDER
# ----------------------------------------------------------------------------
import html as _html

SEG_LABELS = {
    "S1": "Sent App \u2192 App in Bank",
    "S2": "App in Bank \u2192 UDW Note",
    "S3": "UDW Note \u2192 UW / Stips",
    "S4": "Time in UW / Stips",
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

def _seg_cell(seg_key, value):
    cls = speed(seg_key, value)
    val = _fmt_days(value) if value is not None else "pending"
    return (f'<div class="seg seg--{cls}"><span class="seg__k">{seg_key}</span>'
            f'<span class="seg__v">{val}</span></div>')

def _deal_row(d):
    flag = ""
    if d.get("_open") and d["S4"] is not None and speed("S4", d["S4"]) == "slow":
        flag = '<span class="flag">UW stall</span>'
    elif d.get("_open") and d["S4"] is not None and speed("S4", d["S4"]) == "watch":
        flag = '<span class="flag flag--watch">watch</span>'
    track = "".join(_seg_cell(k, d.get(k)) for k in ("S1", "S2", "S3", "S4"))
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
          <div class="metric"><span class="metric__k">In-flight</span><span class="metric__v">{rep['n']}</span></div>
          <div class="metric"><span class="metric__k">Median days in UW</span><span class="metric__v">{_fmt_days(rep['median_s4'])}</span></div>
          <div class="metric"><span class="metric__k">Oldest</span><span class="metric__v">{_fmt_days(rep['worst_s4'])}</span></div>
          <div class="metric"><span class="metric__k">Stalls</span><span class="metric__v {stall_cls}">{rep['stalls']}</span></div>
        </div></header>
      <div class="rep__rows">{rows}</div>
    </section>"""

def render_html(ctx) -> str:
    gen = _parse(ctx["generated"]).astimezone(DISPLAY_TZ).strftime("%b %-d, %Y \u00b7 %-I:%M %p PT")
    banner = "" if ctx["mode"] == "live" else (
        '<div class="banner">Sample view \u2014 in-flight deals with real time-in-underwriting. '
        'S1\u2013S3 populate on the scheduled run (S2/S3 accrue forward from first snapshot).</div>')
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
  .row{{display:grid;grid-template-columns:minmax(150px,1.1fr) minmax(280px,2fr) minmax(120px,.8fr);
    gap:16px;align-items:center;padding:11px 0;border-bottom:1px solid #eef2f4}}
  .row:last-child{{border-bottom:none}}
  .row__id{{display:flex;flex-direction:column;gap:2px;min-width:0}}
  .merchant{{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
  .amt{{font:500 11px/1 'IBM Plex Mono',monospace;color:var(--muted)}}
  .track{{display:grid;grid-template-columns:repeat(4,1fr);gap:5px}}
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
    <p class="sub">Clock starts when a rep moves a lead to <b>Sent App to Merchant</b>.
      <b>In-flight deals plus recent declines</b> (approvals excluded), across <b>{len(REPS)} reps</b>.
      Most stuck in underwriting surface first.</p>
  </div><div>
    <p class="gen">Generated<br>{gen}</p>
    <div class="controls"><button class="ctl" id="flagBtn">Flagged only</button>
      <button class="ctl" id="sortBtn">Sort: stalls</button></div>
  </div></div>
  <div class="legend">{legend}</div>
  {banner}
  <div id="reps">{cards}</div>
  <div class="foot"><b>How to read this.</b> Each row is an in-flight deal or a recent decline;
    approvals are excluded. <b>S1</b>\u2013<b>S3</b> are completed segment times from CRM status history.
    <b>S4</b> is time in UW / Stips \u2014 still counting for open deals (a red S4 is a live stall), or the
    time it took to decline for closed ones. Stall flags and the stall count apply only to open deals;
    declines show a red <i>Declined</i> chip. <b>S2</b>/<b>S3</b> depend on when UDW Notes first gets a
    value, which CRM does not history-track, so they accrue forward from the first scheduled run and read
    <i>pending</i> until then. Edit the roster at the top of the script to change who is tracked.</div>
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
# DEMO  (bundled REAL sample pulled from CRM on build day)
# ----------------------------------------------------------------------------

SAMPLE = [
    ("Rockwell","Nathan Wilkie",30000,"2026-04-22T10:57:19-07:00","2026-06-01T13:30:04-07:00","Approved",True),
    ("TG Deals LLC","Jordan Koestner",15000,"2026-05-15T06:38:18-07:00","2026-05-29T11:49:42-07:00","Approved",True),
    ("Exp Lyft LLC","Richard McCausland",15000,"2026-05-22T13:56:53-07:00","2026-06-01T07:38:00-07:00","Approved",True),
    ("Cars R Us","Richard McCausland",100000,"2026-05-27T09:23:29-07:00","2026-06-02T10:16:32-07:00","Approved",True),
    ("Lynette Escobar Tiling","Nathan Wilkie",15000,"2026-05-27T10:19:24-07:00","2026-05-29T14:54:10-07:00","Approved",True),
    ("Be Good Organix LLC","Richard McCausland",20000,"2026-06-04T07:34:37-07:00","2026-06-05T10:43:38-07:00","Approved",True),
    ("AV Home Innovations LLC","Richard McCausland",None,"2026-06-02T09:18:18-07:00","2026-06-03T10:07:00-07:00","Approved",False),
    ("Discovery Water Management LLC.","Jordan Koestner",25000,"2026-05-28T16:31:14-07:00","2026-05-29T11:05:18-07:00","Approved",True),
]

DECLINED_DEMO = {"TG Deals LLC", "Cars R Us"}    # show these as recent declines

def build_context_demo():
    now_iso = datetime.now(timezone.utc).isoformat()
    rows = []
    for m, rep, amt, uw, dec_t, dec, note in SAMPLE:
        if m in DECLINED_DEMO:
            rows.append({"rep": rep, "merchant": m, "amount": amt, "stage": "Declined",
                         "has_note": note, "decision": "Declined", "submitted": uw, "_open": False,
                         "S1": None, "S2": None, "S3": None, "S4": delta_days(uw, dec_t)})
        else:
            rows.append({"rep": rep, "merchant": m, "amount": amt, "stage": "UW / Stips Needed",
                         "has_note": note, "decision": None, "submitted": uw, "_open": True,
                         "S1": None, "S2": None, "S3": None, "S4": delta_days(uw, now_iso)})
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
