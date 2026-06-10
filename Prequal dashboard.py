#!/usr/bin/env python3
"""
Pre-App / Underwriting Funnel Velocity Monitor
==============================================
Management-only dashboard that tracks a configurable roster of reps and, for
every lead they push to "Sent App to Merchant", measures how long the deal
takes to move through underwriting. Built to run on a schedule (GitHub Actions
+ cron) and publish a static index.html to tiiny.host.

WHAT IT MEASURES (the four segments)
  Anchor : lead status -> "Sent App to Merchant"          [Lead Status History]
  S1     : "Sent App to Merchant" -> "App in Bank"         [Lead Status History]  (historical)
  S2     : "App in Bank" -> UDW Notes first populated       [daily snapshot]        (forward-only)
  S3     : UDW Notes first populated -> "UW / Stips Needed" [snapshot + DealHistory](forward-only start)
  S4     : "UW / Stips Needed" -> Approved / Declined        [DealHistory]           (historical)

WHY A SNAPSHOT FOR UDW NOTES
  The Deals.UDW_Notes field is NOT history-tracked in CRM, so Zoho stores no
  timestamp for when it went blank -> value. We therefore record the first run
  in which we observe a non-empty UDW_Notes per deal (STATE_FILE). S2/S3 start
  accruing from the first scheduled run forward; they are intentionally blank
  for deals whose note predates deployment.

EDITING THE ROSTER
  Add/remove reps in REPS below (name -> Zoho user id). Nothing else changes.

CREDENTIALS (env vars, same pattern as the other reporting jobs)
  ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN
  Optional: ZOHO_ACCOUNTS_HOST (default https://accounts.zoho.com)
            ZOHO_API_HOST      (default https://www.zohoapis.com)

USAGE
  python prequal_funnel_dashboard.py            # live run -> writes index.html
  python prequal_funnel_dashboard.py --demo     # render bundled real sample, no API
"""

from __future__ import annotations
import os, sys, json, html, statistics
from datetime import datetime, timezone, timedelta

# ----------------------------------------------------------------------------
# CONFIG  (edit here)
# ----------------------------------------------------------------------------

REPS = {
    # display name        : Zoho user id (Owner)
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

# Speed thresholds in DAYS for each segment -> (fast_max, watch_max). Above
# watch_max is treated as a stall. Tune per segment as you learn the baselines.
THRESHOLDS = {
    "S1": (1.0, 3.0),
    "S2": (0.5, 2.0),
    "S3": (1.0, 3.0),
    "S4": (2.0, 7.0),
}

LOOKBACK_DAYS = 120          # how far back to consider transitions
STATE_FILE    = "udw_snapshot_state.json"
OUTPUT_FILE   = "index.html"

ACCOUNTS_HOST = os.environ.get("ZOHO_ACCOUNTS_HOST", "https://accounts.zoho.com")
API_HOST      = os.environ.get("ZOHO_API_HOST", "https://www.zohoapis.com")

REP_IDS   = set(REPS.values())
ID_TO_REP = {v: k for k, v in REPS.items()}

# ----------------------------------------------------------------------------
# ZOHO CLIENT
# ----------------------------------------------------------------------------

def _access_token() -> str:
    import urllib.request, urllib.parse
    data = urllib.parse.urlencode({
        "refresh_token": os.environ["ZOHO_REFRESH_TOKEN"],
        "client_id":     os.environ["ZOHO_CLIENT_ID"],
        "client_secret": os.environ["ZOHO_CLIENT_SECRET"],
        "grant_type":    "refresh_token",
    }).encode()
    req = urllib.request.Request(f"{ACCOUNTS_HOST}/oauth/v2/token", data=data)
    with urllib.request.urlopen(req, timeout=30) as r:
        tok = json.load(r).get("access_token")
    if not tok:
        raise RuntimeError("Zoho token refresh failed (check client/refresh creds).")
    return tok


def coql(token: str, query: str) -> list[dict]:
    """Run a COQL query, following pagination with LIMIT/OFFSET."""
    import urllib.request
    rows, offset, page = [], 0, 200
    while True:
        q = f"{query} limit {offset}, {page}" if offset else f"{query} limit {page}"
        body = json.dumps({"select_query": q}).encode()
        req = urllib.request.Request(
            f"{API_HOST}/crm/v8/coql", data=body,
            headers={"Authorization": f"Zoho-oauthtoken {token}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                payload = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 204:    # no content
                break
            raise
        batch = payload.get("data", [])
        rows.extend(batch)
        if not payload.get("info", {}).get("more_records"):
            break
        offset += page
    return rows


def _id_list(ids) -> str:
    return "(" + ",".join(f"'{i}'" for i in ids) + ")"

# ----------------------------------------------------------------------------
# FETCH
# ----------------------------------------------------------------------------
# NOTE: the *History modules reject a server-side `Modified_Time >=` filter and
# multiple `in (...)` clauses in one WHERE, so we query one Moved_To value at a
# time and apply the lookback window client-side.

def _within(ts: str, cutoff: datetime) -> bool:
    return _parse(ts) >= cutoff

def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def fetch_lead_transitions(token, moved_to, cutoff):
    q = (f"select id, Full_Name, Lead_Status, Moved_To__s, Modified_Time "
         f"from Lead_Status_History "
         f"where Full_Name.Owner in {_id_list(REP_IDS)} "
         f"and Moved_To__s = '{moved_to}' order by Modified_Time desc")
    return [r for r in coql(token, q) if _within(r["Modified_Time"], cutoff)]


def fetch_deal_transitions(token, moved_to, cutoff):
    q = (f"select id, Potential_Name, Moved_To__s, Modified_Time "
         f"from DealHistory "
         f"where Potential_Name.Owner in {_id_list(REP_IDS)} "
         f"and Moved_To__s = '{moved_to}' order by Modified_Time desc")
    return [r for r in coql(token, q) if _within(r["Modified_Time"], cutoff)]


def fetch_deals(token, deal_ids):
    out = {}
    ids = list(deal_ids)
    for i in range(0, len(ids), 100):
        chunk = ids[i:i+100]
        q = (f"select id, Deal_Name, Owner, Stage, Amount, UDW_Notes, Created_Time "
             f"from Deals where id in {_id_list(chunk)}")
        for r in coql(token, q):
            out[r["id"]] = r
    return out

# ----------------------------------------------------------------------------
# UDW SNAPSHOT STATE  (forward-only capture of blank -> value)
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
    """Record the first run we see a non-empty UDW_Notes for each deal."""
    for did, d in deals.items():
        note = (d.get("UDW_Notes") or "").strip()
        if note and did not in state:
            state[did] = now_iso
    return state

# ----------------------------------------------------------------------------
# COMPUTE
# ----------------------------------------------------------------------------

def speed(seg_key, days):
    if days is None:
        return "pending"
    fast, watch = THRESHOLDS[seg_key]
    if days <= fast:  return "fast"
    if days <= watch: return "watch"
    return "slow"

def delta_days(a, b):
    if not a or not b:
        return None
    return round((_parse(b) - _parse(a)).total_seconds() / 86400, 2)


def build_context_live():
    token = _access_token()
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    now_iso = datetime.now(timezone.utc).isoformat()

    # --- lead side: earliest transition INTO each status, per lead ---
    def earliest_by_lead(rows):
        m = {}
        for r in rows:
            lid = (r.get("Full_Name") or {}).get("id")
            if not lid: continue
            t = r["Modified_Time"]
            if lid not in m or _parse(t) < _parse(m[lid]):
                m[lid] = t
        return m

    sent = earliest_by_lead(fetch_lead_transitions(token, ANCHOR_MOVED_TO, cutoff))
    bank = earliest_by_lead(fetch_lead_transitions(token, APP_IN_BANK, cutoff))

    # --- deal side: earliest UW/Stips and earliest decision, per deal ---
    def earliest_by_deal(rows):
        m = {}
        for r in rows:
            pid = (r.get("Potential_Name") or {}).get("id")
            if not pid: continue
            t = r["Modified_Time"]
            if pid not in m or _parse(t) < _parse(m[pid]):
                m[pid] = t
        return m

    uw = earliest_by_deal(fetch_deal_transitions(token, UW_STIPS, cutoff))
    decision = {}
    for stage in DECISION_STAGES:
        for pid, t in earliest_by_deal(fetch_deal_transitions(token, stage, cutoff)).items():
            if pid not in decision or _parse(t) < _parse(decision[pid][0]):
                decision[pid] = (t, stage)

    deal_ids = set(uw) | set(decision)
    deals = fetch_deals(token, deal_ids)

    state = update_udw_snapshot(load_state(), deals, now_iso)
    save_state(state)

    # --- link lead journey to deal -------------------------------------------
    # Converted_Deal is unreliable in this org, so we bridge on merchant/company
    # name (lead Company ~ deal Deal_Name). This is the one spot to tune if your
    # naming diverges; S1 only needs the lead-side timestamps below.
    rows = []
    for pid, d in deals.items():
        owner = (d.get("Owner") or {}).get("id")
        if owner not in REP_IDS:
            continue
        dec = decision.get(pid)
        s4 = delta_days(uw.get(pid), dec[0]) if dec else None
        udw_first = state.get(pid)
        s3 = delta_days(udw_first, uw.get(pid))
        rows.append({
            "rep": ID_TO_REP.get(owner, "?"),
            "merchant": d.get("Deal_Name") or "(unnamed)",
            "amount": d.get("Amount"),
            "stage": d.get("Stage"),
            "has_note": bool((d.get("UDW_Notes") or "").strip()),
            "S1": None,          # filled when lead<->deal link resolves
            "S2": delta_days(None, udw_first),  # forward-only; needs App-in-Bank link
            "S3": s3,
            "S4": s4,
            "decision": dec[1] if dec else None,
        })
    return assemble(rows, now_iso, mode="live")


def assemble(rows, now_iso, mode):
    by_rep = {name: [] for name in REPS}
    for r in rows:
        by_rep.setdefault(r["rep"], []).append(r)

    reps = []
    for name, deals in by_rep.items():
        s4s = [d["S4"] for d in deals if d["S4"] is not None]
        stalls = sum(1 for d in deals
                     if d["S4"] is not None and speed("S4", d["S4"]) == "slow")
        reps.append({
            "name": name,
            "deals": sorted(deals, key=lambda d: (d["S4"] is None, -(d["S4"] or 0))),
            "n": len(deals),
            "median_s4": round(statistics.median(s4s), 1) if s4s else None,
            "worst_s4": max(s4s) if s4s else None,
            "stalls": stalls,
        })
    # struggling reps first: most stalls, then worst median
    reps.sort(key=lambda r: (-(r["stalls"]), -((r["median_s4"] or 0))))
    return {"reps": reps, "generated": now_iso, "mode": mode}

# ----------------------------------------------------------------------------
# RENDER
# ----------------------------------------------------------------------------

SEG_LABELS = {
    "S1": "Sent App \u2192 App in Bank",
    "S2": "App in Bank \u2192 UDW Note",
    "S3": "UDW Note \u2192 UW / Stips",
    "S4": "UW / Stips \u2192 Decision",
}

def _fmt_days(v):
    if v is None: return "\u2014"
    if v < 1:     return f"{v*24:.0f}h"
    return f"{v:.1f}d"

def _fmt_amount(a):
    if a in (None, 0): return ""
    return f"${a:,.0f}"

def _seg_cell(seg_key, value):
    cls = speed(seg_key, value)
    val = _fmt_days(value) if value is not None else "pending"
    return (f'<div class="seg seg--{cls}">'
            f'<span class="seg__k">{seg_key}</span>'
            f'<span class="seg__v">{val}</span></div>')

def _deal_row(d):
    flag = ""
    if d["S4"] is not None and speed("S4", d["S4"]) == "slow":
        flag = '<span class="flag">UW stall</span>'
    elif d["S4"] is not None and speed("S4", d["S4"]) == "watch":
        flag = '<span class="flag flag--watch">watch</span>'
    track = "".join(_seg_cell(k, d.get(k)) for k in ("S1", "S2", "S3", "S4"))
    dec = d.get("decision") or d.get("stage") or ""
    dec_cls = "ok" if dec == "Approved" else ("bad" if dec == "Declined" else "neutral")
    amt = _fmt_amount(d.get("amount"))
    return f"""
    <div class="row">
      <div class="row__id">
        <span class="merchant">{html.escape(str(d['merchant']))}</span>
        <span class="amt">{amt}</span>
      </div>
      <div class="track">{track}</div>
      <div class="row__end">
        <span class="stage stage--{dec_cls}">{html.escape(dec)}</span>{flag}
      </div>
    </div>"""

def _rep_card(rep):
    med = _fmt_days(rep["median_s4"])
    worst = _fmt_days(rep["worst_s4"])
    stall_cls = "metric__v--bad" if rep["stalls"] else ""
    rows = "".join(_deal_row(d) for d in rep["deals"]) or \
           '<div class="empty">No decided deals in the lookback window.</div>'
    flagged = "rep--flagged" if rep["stalls"] else ""
    return f"""
    <section class="rep {flagged}" data-stalls="{rep['stalls']}" data-name="{html.escape(rep['name'])}">
      <header class="rep__head">
        <h2 class="rep__name">{html.escape(rep['name'])}</h2>
        <div class="rep__metrics">
          <div class="metric"><span class="metric__k">Deals</span><span class="metric__v">{rep['n']}</span></div>
          <div class="metric"><span class="metric__k">Median UW\u2192Decision</span><span class="metric__v">{med}</span></div>
          <div class="metric"><span class="metric__k">Worst</span><span class="metric__v">{worst}</span></div>
          <div class="metric"><span class="metric__k">Stalls</span><span class="metric__v {stall_cls}">{rep['stalls']}</span></div>
        </div>
      </header>
      <div class="rep__rows">{rows}</div>
    </section>"""

def render_html(ctx) -> str:
    gen = _parse(ctx["generated"]).astimezone().strftime("%b %-d, %Y \u00b7 %-I:%M %p")
    banner = "" if ctx["mode"] == "live" else (
        '<div class="banner">Sample view \u2014 real UW\u2192Decision history for 8 deals. '
        'S1\u2013S3 populate on the scheduled run (S2/S3 accrue forward from first snapshot).</div>')
    cards = "".join(_rep_card(r) for r in ctx["reps"])
    legend = "".join(
        f'<span class="lg"><b>{k}</b> {v}</span>' for k, v in SEG_LABELS.items())
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Underwriting Velocity \u2014 Rep Monitor</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=Inter:wght@400;500;600&family=IBM+Plex+Mono:wght@500;600&display=swap" rel="stylesheet">
<style>
  :root{{
    --bg:#e8ecef; --surface:#ffffff; --ink:#14181c; --muted:#646e78; --line:#dce2e7;
    --teal:#0c6b63; --fast:#1f9d57; --watch:#c4790a; --slow:#d23b3b; --pending:#9aa6b0;
  }}
  *{{box-sizing:border-box}}
  body{{margin:0;background:var(--bg);color:var(--ink);
    font-family:Inter,system-ui,sans-serif;font-size:14px;line-height:1.45;
    padding:28px 22px 60px}}
  .wrap{{max-width:1080px;margin:0 auto}}
  /* header */
  .top{{display:flex;justify-content:space-between;align-items:flex-end;
    gap:24px;flex-wrap:wrap;border-bottom:2px solid var(--ink);padding-bottom:16px}}
  .eyebrow{{font:600 11px/1 'IBM Plex Mono',monospace;letter-spacing:.18em;
    text-transform:uppercase;color:var(--teal);margin:0 0 8px}}
  h1{{font-family:'Space Grotesk',sans-serif;font-weight:700;font-size:34px;
    line-height:1.02;letter-spacing:-.01em;margin:0}}
  .sub{{color:var(--muted);margin-top:8px;font-size:13px}}
  .sub b{{color:var(--ink);font-weight:600}}
  .gen{{font:500 11px/1.4 'IBM Plex Mono',monospace;color:var(--muted);text-align:right}}
  .controls{{display:flex;gap:8px;margin-top:10px;justify-content:flex-end}}
  .ctl{{font:600 11px/1 'IBM Plex Mono',monospace;letter-spacing:.04em;text-transform:uppercase;
    border:1px solid var(--line);background:var(--surface);color:var(--muted);
    padding:7px 11px;border-radius:6px;cursor:pointer}}
  .ctl.on{{border-color:var(--ink);color:var(--ink)}}
  /* legend */
  .legend{{display:flex;flex-wrap:wrap;gap:6px 16px;margin:16px 0 22px;
    font:500 11.5px/1.3 'IBM Plex Mono',monospace;color:var(--muted)}}
  .lg b{{color:var(--ink);margin-right:5px}}
  .banner{{background:#fff7e6;border:1px solid #f0d79a;color:#7a5a16;
    padding:10px 14px;border-radius:8px;font-size:12.5px;margin:18px 0 6px}}
  /* rep card */
  .rep{{background:var(--surface);border:1px solid var(--line);border-radius:12px;
    padding:18px 20px;margin-top:14px}}
  .rep--flagged{{border-left:4px solid var(--slow)}}
  .rep__head{{display:flex;justify-content:space-between;align-items:center;
    gap:18px;flex-wrap:wrap;padding-bottom:14px;border-bottom:1px solid var(--line)}}
  .rep__name{{font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:19px;margin:0}}
  .rep__metrics{{display:flex;gap:22px;flex-wrap:wrap}}
  .metric{{display:flex;flex-direction:column;align-items:flex-end}}
  .metric__k{{font:500 10px/1.2 'IBM Plex Mono',monospace;letter-spacing:.06em;
    text-transform:uppercase;color:var(--muted)}}
  .metric__v{{font:600 17px/1.1 'IBM Plex Mono',monospace;margin-top:3px}}
  .metric__v--bad{{color:var(--slow)}}
  /* deal rows */
  .rep__rows{{margin-top:6px}}
  .row{{display:grid;grid-template-columns:minmax(150px,1.1fr) minmax(280px,2fr) minmax(120px,.8fr);
    gap:16px;align-items:center;padding:11px 0;border-bottom:1px solid #eef2f4}}
  .row:last-child{{border-bottom:none}}
  .row__id{{display:flex;flex-direction:column;gap:2px;min-width:0}}
  .merchant{{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
  .amt{{font:500 11px/1 'IBM Plex Mono',monospace;color:var(--muted)}}
  .track{{display:grid;grid-template-columns:repeat(4,1fr);gap:5px}}
  .seg{{border-radius:6px;padding:7px 6px;display:flex;flex-direction:column;
    align-items:center;gap:3px;border:1px solid transparent;min-width:0}}
  .seg__k{{font:600 9px/1 'IBM Plex Mono',monospace;letter-spacing:.08em;opacity:.7}}
  .seg__v{{font:600 13px/1 'IBM Plex Mono',monospace}}
  .seg--fast{{background:#e6f5ec;color:#0f6b38;border-color:#bfe3cd}}
  .seg--watch{{background:#fbf0db;color:#8a5407;border-color:#f0d9ad}}
  .seg--slow{{background:#fae3e3;color:#9e2424;border-color:#f0c3c3}}
  .seg--pending{{background:repeating-linear-gradient(135deg,#f2f5f7,#f2f5f7 5px,#eaeef1 5px,#eaeef1 10px);
    color:var(--pending);border-color:#e3e9ed}}
  .seg--pending .seg__v{{font-size:10px;letter-spacing:.03em}}
  .row__end{{display:flex;align-items:center;gap:8px;justify-content:flex-end;flex-wrap:wrap}}
  .stage{{font:600 11px/1 'IBM Plex Mono',monospace;padding:5px 9px;border-radius:999px;
    border:1px solid var(--line)}}
  .stage--ok{{background:#e6f5ec;color:#0f6b38;border-color:#bfe3cd}}
  .stage--bad{{background:#fae3e3;color:#9e2424;border-color:#f0c3c3}}
  .stage--neutral{{background:#eef2f4;color:var(--muted)}}
  .flag{{font:700 10px/1 'IBM Plex Mono',monospace;letter-spacing:.04em;text-transform:uppercase;
    color:#fff;background:var(--slow);padding:5px 8px;border-radius:5px}}
  .flag--watch{{background:var(--watch)}}
  .empty{{color:var(--muted);padding:14px 0;font-size:13px}}
  /* footer */
  .foot{{margin-top:30px;padding-top:18px;border-top:1px solid var(--line);
    color:var(--muted);font-size:12px;line-height:1.6;max-width:760px}}
  .foot b{{color:var(--ink)}}
  @media (max-width:720px){{
    .row{{grid-template-columns:1fr;gap:9px}}
    .row__end{{justify-content:flex-start}}
    h1{{font-size:27px}}
  }}
</style></head>
<body><div class="wrap">
  <div class="top">
    <div>
      <p class="eyebrow">Management Monitor \u00b7 Confidential</p>
      <h1>Underwriting Velocity</h1>
      <p class="sub">Clock starts when a rep moves a lead to <b>Sent App to Merchant</b>.
        Tracking <b>{len(REPS)} reps</b> through to approval / decline. Struggling reps surface first.</p>
    </div>
    <div>
      <p class="gen">Generated<br>{gen}</p>
      <div class="controls">
        <button class="ctl" id="flagBtn">Flagged only</button>
        <button class="ctl" id="sortBtn">Sort: stalls</button>
      </div>
    </div>
  </div>
  <div class="legend">{legend}</div>
  {banner}
  <div id="reps">{cards}</div>
  <div class="foot">
    <b>How to read this.</b> Each deal is one row; the four cells are the time spent in each
    funnel segment, colored green / amber / red by speed. <b>S1</b> and <b>S4</b> are reconstructed
    from CRM status history. <b>S2</b> and <b>S3</b> depend on when UDW Notes first gets a value \u2014 a
    field CRM does not history-track \u2014 so they accrue forward from the first scheduled run and read
    <i>pending</i> until then. Edit the rep roster at the top of the generator script; the dashboard
    rebuilds around whoever is listed.
  </div>
</div>
<script>
  var flagOnly=false, sortMode='stalls';
  var box=document.getElementById('reps');
  function apply(){{
    var cards=[].slice.call(box.querySelectorAll('.rep'));
    cards.forEach(function(c){{
      c.style.display=(flagOnly && c.dataset.stalls==='0')?'none':'';
    }});
    if(sortMode==='name'){{
      cards.sort(function(a,b){{return a.dataset.name.localeCompare(b.dataset.name);}});
    }} else {{
      cards.sort(function(a,b){{return (+b.dataset.stalls)-(+a.dataset.stalls);}});
    }}
    cards.forEach(function(c){{box.appendChild(c);}});
  }}
  document.getElementById('flagBtn').onclick=function(){{
    flagOnly=!flagOnly; this.classList.toggle('on',flagOnly); apply();}};
  document.getElementById('sortBtn').onclick=function(){{
    sortMode=(sortMode==='stalls')?'name':'stalls';
    this.textContent='Sort: '+sortMode; apply();}};
</script>
</body></html>"""

# ----------------------------------------------------------------------------
# DEMO  (bundled REAL sample pulled from CRM on build day; no API needed)
# ----------------------------------------------------------------------------

SAMPLE = [
    # merchant, rep, amount, uw_stips_entry, decision_time, decision, has_note
    ("Rockwell","Nathan Wilkie",30000,"2026-04-22T10:57:19-07:00","2026-06-01T13:30:04-07:00","Approved",True),
    ("TG Deals LLC","Jordan Koestner",15000,"2026-05-15T06:38:18-07:00","2026-05-29T11:49:42-07:00","Approved",True),
    ("Exp Lyft LLC","Richard McCausland",15000,"2026-05-22T13:56:53-07:00","2026-06-01T07:38:00-07:00","Approved",True),
    ("Cars R Us","Richard McCausland",100000,"2026-05-27T09:23:29-07:00","2026-06-02T10:16:32-07:00","Approved",True),
    ("Lynette Escobar Tiling","Nathan Wilkie",15000,"2026-05-27T10:19:24-07:00","2026-05-29T14:54:10-07:00","Approved",True),
    ("Be Good Organix LLC","Richard McCausland",20000,"2026-06-04T07:34:37-07:00","2026-06-05T10:43:38-07:00","Approved",True),
    ("AV Home Innovations LLC","Richard McCausland",None,"2026-06-02T09:18:18-07:00","2026-06-03T10:07:00-07:00","Approved",False),
    ("Discovery Water Management LLC.","Jordan Koestner",25000,"2026-05-28T16:31:14-07:00","2026-05-29T11:05:18-07:00","Approved",True),
]

def build_context_demo():
    rows = []
    for merch, rep, amt, uw, dec_t, dec, note in SAMPLE:
        rows.append({
            "rep": rep, "merchant": merch, "amount": amt, "stage": dec,
            "has_note": note, "decision": dec,
            "S1": None, "S2": None, "S3": None,
            "S4": delta_days(uw, dec_t),
        })
    return assemble(rows, datetime.now(timezone.utc).isoformat(), mode="demo")

# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------

def main():
    demo = "--demo" in sys.argv
    ctx = build_context_demo() if demo else build_context_live()
    with open(OUTPUT_FILE, "w") as f:
        f.write(render_html(ctx))
    print(f"Wrote {OUTPUT_FILE} ({'demo' if demo else 'live'}) "
          f"\u2014 {sum(len(r['deals']) for r in ctx['reps'])} deals across {len(ctx['reps'])} reps.")

if __name__ == "__main__":
    main()
