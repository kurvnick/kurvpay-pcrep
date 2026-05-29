"""
KurvPay PC In-Day Activity Report
Pulls today's data from Zoho CRM, scores reps, generates HTML, uploads to tiiny.host
Run every 30 min during business hours via GitHub Actions
"""

import os, sys, io, zipfile, requests
from report_analysis import generate_analysis
from datetime import date, timedelta, datetime, timezone
from collections import defaultdict

# ── CONFIG ───────────────────────────────────────────────────────────────────
ZOHO_CLIENT_ID     = os.environ["ZOHO_CLIENT_ID"]
ZOHO_CLIENT_SECRET = os.environ["ZOHO_CLIENT_SECRET"]
ZOHO_REFRESH_TOKEN = os.environ["ZOHO_REFRESH_TOKEN"]
TIINY_API_KEY      = os.environ["TIINY_API_KEY"]
TIINY_DOMAIN       = "paymentcloudcallstats-inday.tiiny.site"

# ── CONSTANTS ────────────────────────────────────────────────────────────────
REP_LAST_TO_FULL = {
    "Gilden":"Brandon Gilden","Green":"Brisa Green","Heflin":"Bryan Heflin",
    "Kopstein":"Cheryl Kopstein","Jackson":"Daniel Jackson","Weiss":"Eric Weiss",
    "Travis":"Faith Travis","Riffe":"Frank Riffe","Vartevanian":"Hovak Vartevanian",
    "Todd":"Jessie Todd","Colovas":"John Colovas","Lotut":"Jonathan Lotut",
    "Koestner":"Jordan Koestner","Landeros":"Julian Landeros","Margolis":"Julie Margolis",
    "Villasenor":"Kirstan Villasenor","Friedhoff":"Matthew Friedhoff","Crozier":"Max Crozier",
    "Daleske":"Melissa Daleske","Wilkie":"Nathan Wilkie","McCausland":"Richard McCausland",
    "Carranza":"Romario Carranza","Silva":"Russell Silva","Riley":"Samantha Riley",
    "Bender":"Samuel Bender",
}
CONLAN = {"Kopstein","Silva","Friedhoff","Riley","Weiss","Vartevanian",
          "Colovas","Green","Crozier","Landeros","Riffe","Todd"}
STOKOE = {"Daleske","Heflin","Travis","Villasenor","Margolis","Jackson","Lotut",
          "Koestner","Wilkie","Gilden","Bender","Carranza","McCausland"}
ALL_REPS = set(REP_LAST_TO_FULL.keys())
APPROVAL_STAGES = ["Approved", "Conditionally Approved", "Auto Approved", "Auto Approved New"]

# ── TIMEZONE ─────────────────────────────────────────────────────────────────
try:
    from zoneinfo import ZoneInfo          # Python 3.9+
    _PT = ZoneInfo("America/Los_Angeles")
except ImportError:
    import subprocess, sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "tzdata", "-q"])
    from zoneinfo import ZoneInfo
    _PT = ZoneInfo("America/Los_Angeles")

def today_pt():
    """Return today's date in Pacific time as an ISO string (DST-aware)."""
    return datetime.now(_PT).strftime("%Y-%m-%d")

def pt_offset_str():
    """Return the current UTC offset string for Pacific time, e.g. '-07:00' or '-08:00'."""
    offset = datetime.now(_PT).utcoffset()
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    h, m = divmod(abs(total_minutes), 60)
    return f"{sign}{h:02d}:{m:02d}"

# ── ZOHO AUTH ────────────────────────────────────────────────────────────────
def get_access_token():
    r = requests.post("https://accounts.zoho.com/oauth/v2/token", data={
        "refresh_token": ZOHO_REFRESH_TOKEN,
        "client_id":     ZOHO_CLIENT_ID,
        "client_secret": ZOHO_CLIENT_SECRET,
        "grant_type":    "refresh_token",
    })
    r.raise_for_status()
    return r.json()["access_token"]

def zoho_coql(token, query):
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    records = []
    offset = 0
    while True:
        paginated = query.rstrip() + f" LIMIT 200 OFFSET {offset}"
        r = requests.post("https://www.zohoapis.com/crm/v7/coql",
                          headers=headers, json={"select_query": paginated})
        if r.status_code == 204:
            break
        if r.status_code != 200:
            print(f"  [zoho_coql] ERROR {r.status_code}: {r.text[:300]}")
            break
        data = r.json()
        if "data" not in data or not data["data"]:
            if "info" not in data:
                print(f"  [zoho_coql] unexpected response: {str(data)[:300]}")
            break
        records.extend(data["data"])
        if not data.get("info", {}).get("more_records"):
            break
        offset += 200
    return records

# ── DATA PULLS ───────────────────────────────────────────────────────────────
def pull_calls(token, day_str):
    """Returns ({last: (count, dur_min)}, {hour: count}) for a single day."""
    day       = date.fromisoformat(day_str)
    start_utc = f"{day}T00:00:00+00:00"
    query = (
        f"SELECT Owner, Call_Duration_in_seconds, Call_Start_Time FROM Calls "
        f"WHERE Call_Start_Time >= '{start_utc}' "
        f"AND Call_Type not in ('Missed')"
    )
    print(f"  [calls query] {query[:140]}")
    records = zoho_coql(token, query)
    print(f"  [calls] {len(records)} raw records, filtering to {day_str}")
    counts = defaultdict(int)
    secs   = defaultdict(int)
    hourly = defaultdict(int)
    for r in records:
        cst = r.get("Call_Start_Time", "")
        if not cst or cst[:10] != day_str:
            continue
        owner = r.get("Owner", {}).get("name", "")
        if owner in ALL_REPS:
            counts[owner] += 1
            secs[owner]   += (r.get("Call_Duration_in_seconds") or 0)
            try:
                hour = int(cst[11:13])
                hourly[hour] += 1
            except (ValueError, IndexError):
                pass
    print(f"  [calls] {sum(counts.values())} records matched {day_str}")
    return {last: (counts[last], round(secs[last] / 60, 1)) for last in ALL_REPS}, dict(hourly)

def pull_accounts(token, day_str):
    day = date.fromisoformat(day_str)
    next_day = day + timedelta(days=1)
    start_utc = f"{day}T00:00:00+00:00"
    end_utc   = f"{next_day}T00:00:00+00:00"
    query = (
        f"SELECT Owner FROM Accounts "
        f"WHERE Created_Time >= '{start_utc}' "
        f"AND Created_Time < '{end_utc}'"
    )
    print(f"  [accounts query] {query[:120]}")
    records = zoho_coql(token, query)
    print(f"  [accounts] {len(records)} records returned")
    counts = defaultdict(int)
    for r in records:
        owner = r.get("Owner", {}).get("name", "")
        if owner in ALL_REPS:
            counts[owner] += 1
    return dict(counts)

def pull_approvals(token, day_str):
    counts = defaultdict(int)
    for stage in APPROVAL_STAGES:
        query = (
            f"SELECT Owner, Closing_Date FROM Deals "
            f"WHERE Stage = '{stage}' AND Closing_Date = '{day_str}'"
        )
        records = zoho_coql(token, query)
        if records:
            print(f"  [approvals] stage='{stage}' -> {len(records)} records")
        for r in records:
            owner = r.get("Owner", {}).get("name", "")
            if owner in ALL_REPS:
                counts[owner] += 1
    print(f"  [approvals] total: {sum(counts.values())}")
    return dict(counts)

# ── SCORING ──────────────────────────────────────────────────────────────────
def score(calls, dur, accts):
    if accts >= 3 or (calls >= 150 and accts >= 2):
        return 5, "accts &ge;3, or 150+ calls &amp; 2+ accts"
    if calls >= 150 or accts >= 2:
        return 4, "calls &ge;150 or accts &ge;2"
    tier3 = sum([calls >= 100, dur >= 60, accts >= 1])
    if tier3 >= 2:
        return 3, "2 of: 100+ calls / 60+ min / 1+ acct"
    tier2 = sum([calls >= 50, dur >= 30, accts >= 1])
    if tier2 >= 2:
        return 2, "2 of: 50+ calls / 30+ min / 1+ acct"
    return 1, "below all thresholds"

# ── HELPERS ──────────────────────────────────────────────────────────────────
def fmt_day(d):
    dt = date.fromisoformat(d)
    if sys.platform == "win32":
        return dt.strftime("%a %b %d").replace(" 0", " ")
    return dt.strftime("%a %b %-d")

def now_pt_str():
    """Return current Pacific time as a readable string."""
    return datetime.now(_PT).strftime("%b %-d, %Y %-I:%M %p PT") if sys.platform != "win32" \
        else datetime.now(_PT).strftime("%b %d, %Y %I:%M %p PT").replace(" 0", " ")

def badge(pts):
    cls   = {5:"grn5", 4:"blu4", 3:"ylw", 2:"org", 1:"red"}[pts]
    label = {5:"5-GRN", 4:"4-BLU", 3:"3-YLW", 2:"2-ORG", 1:"1-RED"}[pts]
    return f'<span class="badge badge-{cls}">{label}</span>'

def row_cls(pts):
    return {5:"row-5", 4:"row-4", 3:"row-3", 2:"row-2", 1:"row-1"}[pts]

# ── ROLLING DATA ─────────────────────────────────────────────────────────────
def compute_rolling(token, team_set, window_days=30):
    today = date.fromisoformat(today_pt())
    bdays = []
    d = today - timedelta(days=1)
    while len(bdays) < window_days:
        if d.weekday() < 5:
            bdays.append(str(d))
        d -= timedelta(days=1)
    bdays.reverse()

    start    = bdays[0]
    next_end = str(date.fromisoformat(bdays[-1]) + timedelta(days=1))
    start_utc = f"{bdays[0]}T00:00:00+00:00"
    end_utc   = f"{str(date.fromisoformat(bdays[-1]) + timedelta(days=1))}T00:00:00+00:00"
    records  = zoho_coql(token,
        f"SELECT Owner, Created_Time FROM Accounts "
        f"WHERE Created_Time >= '{start_utc}' "
        f"AND Created_Time < '{end_utc}'"
    )
    day_rep = defaultdict(lambda: defaultdict(int))
    for r in records:
        owner = r.get("Owner", {}).get("name", "")
        ct    = r.get("Created_Time", "")[:10]
        if owner in team_set and ct in bdays:
            day_rep[ct][owner] += 1

    rep_avgs = {last: sum(day_rep[d].get(last, 0) for d in bdays) / len(bdays)
                for last in team_set}
    all_vals = [day_rep[d].get(last, 0) for d in bdays for last in team_set]
    team_avg = sum(all_vals) / len(all_vals)
    pct_goal = sum(1 for v in all_vals if v >= 3) / len(all_vals) * 100

    srt  = sorted(rep_avgs.items(), key=lambda x: -x[1])
    top3 = [(REP_LAST_TO_FULL[l], v) for l, v in srt[:3]]
    bot3 = [(REP_LAST_TO_FULL[l], v) for l, v in srt[-3:]]

    return {
        "n_days": len(bdays), "n_reps": len(team_set),
        "avg_a": team_avg, "pct_goal": pct_goal,
        "top3": top3, "bot3": bot3,
        "window_label": f"{fmt_day(bdays[0])} &ndash; {fmt_day(bdays[-1])} ({len(bdays)} bdays)",
    }

# ── HTML HELPERS ─────────────────────────────────────────────────────────────
def sup_card(name, team_size, avg_a, avg_ap, pct_goal, c5, c4, c3, c2, c1, tot_ap, d1):
    bar_w = min(100, int(avg_a / 3 * 100))
    return f"""
    <div class="sup-card">
      <div class="sup-head">
        <div class="sup-name">{name}</div>
        <div class="sup-team-size">{team_size} reps</div>
      </div>
      <div class="sup-body">
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:14px">
          <div>
            <div class="metric-label">Avg accts / rep today</div>
            <div class="metric-val">{avg_a:.2f}</div>
            <div class="metric-goal">Goal: 3.0 &middot; {bar_w}% to goal</div>
            <div class="progress-bar-wrap"><div class="progress-bar-fill" style="width:{bar_w}%"></div></div>
          </div>
          <div>
            <div class="metric-label">Avg apprvs / rep today</div>
            <div class="metric-val" style="color:var(--pur)">{avg_ap:.2f}</div>
          </div>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:14px">
          <div>
            <div class="metric-label">% reps at acct goal</div>
            <div class="metric-val" style="font-size:16px">{pct_goal:.1f}%</div>
          </div>
          <div>
            <div class="metric-label">Total apprvs today</div>
            <div class="metric-val" style="color:var(--pur);font-size:16px">{tot_ap}</div>
            <div class="metric-goal">{fmt_day(d1)}</div>
          </div>
        </div>
        <div class="pts-dist">
          <div class="pts-chip chip-5"><div class="pts-num">{c5}</div><div class="pts-lbl">5-GRN</div></div>
          <div class="pts-chip chip-4"><div class="pts-num">{c4}</div><div class="pts-lbl">4-BLU</div></div>
          <div class="pts-chip chip-3"><div class="pts-num">{c3}</div><div class="pts-lbl">3-YLW</div></div>
          <div class="pts-chip chip-2"><div class="pts-num">{c2}</div><div class="pts-lbl">2-ORG</div></div>
          <div class="pts-chip chip-1"><div class="pts-num">{c1}</div><div class="pts-lbl">1-RED</div></div>
        </div>
      </div>
    </div>"""

def rolling_card(sup_name, team_label, n_days, n_reps, avg_a, pct_goal, top3, bot3):
    bar_w    = min(100, int(avg_a / 3 * 100))
    top_rows = "".join(
        f'<div style="display:flex;justify-content:space-between">'
        f'<span style="font-weight:500">{n}</span>'
        f'<span style="font-family:\'IBM Plex Mono\',monospace;color:var(--grn)">{v:.2f}/day</span></div>'
        for n, v in top3)
    bot_rows = "".join(
        f'<div style="display:flex;justify-content:space-between">'
        f'<span style="font-weight:500">{n}</span>'
        f'<span style="font-family:\'IBM Plex Mono\',monospace;color:var(--red)">{v:.2f}/day</span></div>'
        for n, v in bot3)
    return f"""
    <div class="rolling-card">
      <div class="rolling-head">
        <div class="rh-name">{sup_name} &mdash; {team_label}</div>
        <div class="rh-days">{n_days} days &middot; {n_reps} reps</div>
      </div>
      <div class="rolling-body">
        <div style="display:flex;justify-content:space-between;margin-bottom:10px">
          <div>
            <div class="rs-label">Avg accts / rep / day</div>
            <div class="rs-val">{avg_a:.2f}</div>
            <div style="font-size:10px;color:var(--ink3);font-family:'IBM Plex Mono',monospace">Goal: 3.0 &middot; {bar_w}% to goal</div>
            <div class="rs-bar-wrap"><div class="rs-bar-fill" style="width:{bar_w}%"></div></div>
          </div>
          <div style="text-align:right">
            <div class="rs-label">% rep-days at goal</div>
            <div class="rs-val" style="font-size:14px">{pct_goal:.1f}%</div>
          </div>
        </div>
        <div style="margin-top:12px;padding-top:12px;border-top:1px solid var(--border2)">
          <div class="rs-label" style="margin-bottom:8px">Top performers</div>
          <div style="display:flex;flex-direction:column;gap:5px;font-size:12px">{top_rows}</div>
        </div>
        <div style="margin-top:12px;padding-top:12px;border-top:1px solid var(--border2)">
          <div class="rs-label" style="margin-bottom:8px">Needs attention</div>
          <div style="display:flex;flex-direction:column;gap:5px;font-size:12px">{bot_rows}</div>
        </div>
      </div>
    </div>"""

# ── CSS ──────────────────────────────────────────────────────────────────────
CSS = """
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --ink:#0f0f0f;--ink2:#444;--ink3:#888;--border:#e0e0e0;--border2:#f0f0f0;
  --bg:#fafaf8;--white:#ffffff;
  --grn5:#05764a;--grn5-bg:#d1fae5;--grn5-border:#6ee7b7;
  --blu4:#0e7490;--blu4-bg:#e0f2fe;--blu4-border:#7dd3fc;
  --ylw:#a86400;--ylw-bg:#fef9e6;--ylw-border:#f5d98c;
  --org:#c2410c;--org-bg:#fff7ed;--org-border:#fdba74;
  --red:#c0111a;--red-bg:#fdf0f0;--red-border:#f0b0b3;
  --pur:#5b21b6;--pur-bg:#ede9fe;--pur-border:#c4b5fd;
  --grn:#05764a;--grn-bg:#e6f7ef;--grn-border:#b3e6ce;
}
body{font-family:'IBM Plex Sans',sans-serif;background:var(--bg);color:var(--ink);min-height:100vh;padding:2.5rem 1.5rem 4rem}
.page{max-width:980px;margin:0 auto}
.masthead{display:flex;align-items:flex-end;justify-content:space-between;padding-bottom:1.25rem;margin-bottom:1.75rem;border-bottom:2px solid var(--ink);flex-wrap:wrap;gap:12px}
.kicker{font-family:'IBM Plex Mono',monospace;font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--ink3);margin-bottom:5px}
h1{font-size:22px;font-weight:600;line-height:1.2}
.masthead-right{text-align:right;font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--ink3);line-height:1.7}
.goals-bar{display:flex;gap:10px;margin-bottom:1.5rem;flex-wrap:wrap}
.goal-chip{display:flex;align-items:center;gap:7px;background:var(--white);border:1px solid var(--border);border-radius:6px;padding:7px 12px;font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--ink2)}
.goal-chip .label{color:var(--ink3);margin-right:2px}
.legend{display:flex;gap:20px;margin-bottom:1.5rem;flex-wrap:wrap;font-size:12px;color:var(--ink2)}
.legend-item{display:flex;align-items:center;gap:6px}
.leg-dot{width:8px;height:8px;border-radius:2px;display:inline-block}
.summary-row{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-bottom:1.75rem}
.scard{background:var(--white);border:1px solid var(--border);border-radius:10px;padding:14px 16px}
.scard-label{font-family:'IBM Plex Mono',monospace;font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--ink3);margin-bottom:5px}
.scard-val{font-size:30px;font-weight:600;line-height:1}
.scard-val.c5{color:var(--grn5)}.scard-val.c4{color:var(--blu4)}.scard-val.c3{color:var(--ylw)}.scard-val.c2{color:var(--org)}.scard-val.c1{color:var(--red)}
.section-title{font-family:'IBM Plex Mono',monospace;font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--ink3);margin-bottom:.75rem;margin-top:2rem}
.table-wrap{background:var(--white);border:1px solid var(--border);border-radius:12px;overflow:hidden;margin-bottom:1.5rem}
table{width:100%;border-collapse:collapse;font-size:13px}
thead tr{background:#f4f3ef;border-bottom:1.5px solid var(--border)}
th{padding:9px 12px;font-family:'IBM Plex Mono',monospace;font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--ink3);font-weight:500;text-align:left;white-space:nowrap}
th.r{text-align:right}
td{padding:8px 12px;border-bottom:.5px solid var(--border2);vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:#f9f8f5}
td.r{text-align:right;font-family:'IBM Plex Mono',monospace;font-size:12px}
td.rep-name{font-weight:500}
td.reason{font-size:11px;color:var(--ink3)}
td.approvals{text-align:right;font-family:'IBM Plex Mono',monospace;font-size:12px;color:var(--pur);font-weight:500}
tr.row-5 td:first-child{border-left:3px solid var(--grn5)}
tr.row-4 td:first-child{border-left:3px solid var(--blu4)}
tr.row-3 td:first-child{border-left:3px solid #f5b800}
tr.row-2 td:first-child{border-left:3px solid var(--org)}
tr.row-1 td:first-child{border-left:3px solid var(--red)}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:500;white-space:nowrap}
.badge-grn5{background:var(--grn5-bg);color:var(--grn5);border:1px solid var(--grn5-border)}
.badge-blu4{background:var(--blu4-bg);color:var(--blu4);border:1px solid var(--blu4-border)}
.badge-ylw{background:var(--ylw-bg);color:var(--ylw);border:1px solid var(--ylw-border)}
.badge-org{background:var(--org-bg);color:var(--org);border:1px solid var(--org-border)}
.badge-red{background:var(--red-bg);color:var(--red);border:1px solid var(--red-border)}
.callout-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:1.5rem}
.callout-box{border-radius:10px;overflow:hidden;border:1px solid}
.red-box{border-color:var(--red-border);background:var(--red-bg)}
.grn-box{border-color:var(--grn-border);background:var(--grn-bg)}
.callout-head{padding:10px 14px;font-size:12px;font-weight:600;border-bottom:1px solid}
.red-box .callout-head{color:var(--red);border-color:var(--red-border)}
.grn-box .callout-head{color:var(--grn);border-color:var(--grn-border)}
.callout-item{padding:8px 14px;border-bottom:.5px solid;font-size:12px;display:flex;justify-content:space-between;align-items:center}
.red-box .callout-item{border-color:var(--red-border)}
.grn-box .callout-item{border-color:var(--grn-border)}
.callout-item:last-child{border-bottom:none}
.callout-name{font-weight:500}
.callout-stats{font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--ink3);margin-top:1px}
.callout-note{font-size:11px;color:var(--ink3);text-align:right;max-width:140px}
.sup-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:1.5rem}
.sup-card{background:var(--white);border:1px solid var(--border);border-radius:12px;overflow:hidden}
.sup-head{background:#f4f3ef;padding:12px 16px;border-bottom:1px solid var(--border)}
.sup-name{font-size:14px;font-weight:600;margin-bottom:2px}
.sup-team-size{font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--ink3)}
.sup-body{padding:14px 16px}
.metric-label{font-size:11px;color:var(--ink3);font-family:'IBM Plex Mono',monospace;text-transform:uppercase;letter-spacing:.06em}
.metric-val{font-size:18px;font-weight:600}
.metric-goal{font-size:10px;color:var(--ink3);font-family:'IBM Plex Mono',monospace}
.progress-bar-wrap{margin-top:4px;height:5px;background:var(--border2);border-radius:3px;width:120px}
.progress-bar-fill{height:100%;border-radius:3px;background:#f5b800}
.pts-dist{display:flex;gap:4px;margin-top:12px;padding-top:12px;border-top:1px solid var(--border2)}
.pts-chip{flex:1;text-align:center;border-radius:6px;padding:6px 2px}
.pts-chip .pts-num{font-size:16px;font-weight:600}
.pts-chip .pts-lbl{font-size:9px;font-family:'IBM Plex Mono',monospace;text-transform:uppercase;letter-spacing:.06em;margin-top:2px}
.chip-5{background:var(--grn5-bg);color:var(--grn5)}
.chip-4{background:var(--blu4-bg);color:var(--blu4)}
.chip-3{background:var(--ylw-bg);color:var(--ylw)}
.chip-2{background:var(--org-bg);color:var(--org)}
.chip-1{background:var(--red-bg);color:var(--red)}
.rolling-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:1.5rem}
.rolling-card{background:var(--white);border:1px solid var(--border);border-radius:12px;overflow:hidden}
.rolling-head{background:#f4f3ef;padding:10px 14px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center}
.rh-name{font-size:13px;font-weight:600}
.rh-days{font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--ink3)}
.rolling-body{padding:14px 16px}
.rs-label{font-size:11px;color:var(--ink3);font-family:'IBM Plex Mono',monospace;text-transform:uppercase;letter-spacing:.06em}
.rs-val{font-size:16px;font-weight:600}
.rs-bar-wrap{width:100px;height:4px;background:var(--border2);border-radius:2px;margin-top:3px}
.rs-bar-fill{height:100%;border-radius:2px;background:#f5b800}
.footer{font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--ink3);padding-top:1.5rem;border-top:1px solid var(--border);display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px;letter-spacing:.04em}
@media(max-width:640px){.callout-grid,.sup-grid,.rolling-grid{grid-template-columns:1fr}th,td{padding:7px 9px}}
@media print{body{background:#fff;padding:1rem}}
"""

# ── HTML GENERATION ───────────────────────────────────────────────────────────
def generate_inday_analysis(rows, fmt_day, d1):
    """Generate action-oriented in-day analysis focused on path to green."""

    d1_fmt = fmt_day(d1)
    n = len(rows)

    def pill(val, color="default"):
        STYLES = {
            "green":  "background:#e6f7ef;color:#05764a",
            "red":    "background:#fdf0f0;color:#c0111a",
            "yellow": "background:#fef9e6;color:#a86400",
            "purple": "background:#ede9fe;color:#5b21b6",
            "blue":   "background:#e0f2fe;color:#0e7490",
            "default":"background:#f0f0f0;color:#444",
        }
        st = STYLES.get(color, STYLES["default"])
        return (f'<span style="display:inline-block;font-family:\'IBM Plex Mono\',monospace;'
                f'font-size:11px;border-radius:4px;padding:1px 7px;margin:0 2px;{st}">{val}</span>')

    def tp(label, headline, body, lc="trend"):
        LABEL_COLORS = {
            "trend":    "color:#888",
            "concern":  "color:#c0111a",
            "positive": "color:#05764a",
            "action":   "color:#0e7490",
        }
        lcolor = LABEL_COLORS.get(lc, "#888")
        return (
            f'<div style="margin-bottom:20px;padding-bottom:20px;border-bottom:.5px solid #f0f0f0">'
            f'<div style="font-family:\'IBM Plex Mono\',monospace;font-size:10px;text-transform:uppercase;'
            f'letter-spacing:.1em;{lcolor};margin-bottom:5px">{label}</div>'
            f'<div style="font-size:14px;font-weight:600;margin-bottom:6px;line-height:1.3;color:#0f0f0f">{headline}</div>'
            f'<div style="font-size:13px;color:#444;line-height:1.7">{body}</div>'
            f'</div>'
        )

    points = []

    grn_rows = [r for r in rows if r["pts"] >= 4]
    ylw_rows = [r for r in rows if r["pts"] == 3]
    red_rows = [r for r in rows if r["pts"] == 1]

    # ── 1. WHAT'S GETTING REPS TO GREEN ─────────────────────────────────────
    if grn_rows:
        reason_counts = {}
        for r in grn_rows:
            reason_counts[r["reason"]] = reason_counts.get(r["reason"], 0) + 1
        top_reason = max(reason_counts, key=reason_counts.get)
        # Parse reason into plain english
        reason_map = {
            "accounts &ge; 3": "hitting 3+ accounts",
            "calls &gt; 124":  "hitting 125+ calls",
            "duration &gt; 119 min": "hitting 120+ min of talk time",
        }
        top_reason_plain = reason_map.get(top_reason, top_reason)

        grn_body = f"<strong>{len(grn_rows)} of {n} reps ({len(grn_rows)*100//n}%) are green today.</strong> "
        grn_body += f"The most common path: {pill(top_reason_plain, 'green')} ({reason_counts[top_reason]} reps).<br><br>"
        grn_body += "<strong>Green reps today:</strong><br>"
        for r in sorted(grn_rows, key=lambda x: -x["adl"]):
            p_a = pill(f"{r['adl']:.0f} accts", "green")
            p_c = pill(f"{r['ac']:.0f} calls")
            p_d = pill(f"{r['ad']:.0f} min")
            p_r = pill(r['reason'], "blue")
            grn_body += f"&bull; <strong>{r['name']}</strong>: {p_a} {p_c} {p_d} {p_r}<br>"
        points.append(tp("What's working", f"{top_reason_plain.capitalize()} is the top path to green today", grn_body, "positive"))
    else:
        points.append(tp("What's working", "No reps at 1-GRN yet today", "The day is still early. First rep to 3 accounts, 125 calls, or 120 min talk time hits green.", "trend"))

    # ── 2. CLOSEST TO GREEN (yellow reps) ────────────────────────────────────
    if ylw_rows:
        # Score each yellow rep on how close they are to each green threshold
        closest = []
        for r in ylw_rows:
            gaps = []
            if r["adl"] < 3:
                gaps.append(("accounts", 3 - r["adl"], f"{3 - r['adl']:.0f} more account{'s' if 3-r['adl'] != 1 else ''}"))
            if r["ac"] < 125:
                gaps.append(("calls", 125 - r["ac"], f"{125 - r['ac']:.0f} more calls"))
            if r["ad"] < 120:
                gaps.append(("duration", 120 - r["ad"], f"{120 - r['ad']:.0f} more min"))
            if gaps:
                best_gap = min(gaps, key=lambda x: x[1] / {"accounts": 3, "calls": 125, "duration": 120}[x[0]])
                closest.append((r, best_gap))
        closest.sort(key=lambda x: x[1][1] / {"accounts": 3, "calls": 125, "duration": 120}[x[1][0]])

        close_body = f"<strong>{len(ylw_rows)} reps at 2-YLW</strong> — closest to flipping green:<br><br>"
        for r, gap in closest[:6]:
            metric, delta, gap_str = gap
            color = "yellow" if delta > 1 else "green"
            p_gap = pill(gap_str, color)
            p_a = pill(f"{r['adl']:.0f}a")
            p_c = pill(f"{r['ac']:.0f}c")
            p_d = pill(f"{r['ad']:.0f}m")
            close_body += f"&bull; <strong>{r['name']}</strong>: needs {p_gap} to go green &middot; currently {p_a} {p_c} {p_d}<br>"
        points.append(tp("Closest to green", f"{len(ylw_rows)} reps one push away from 1-GRN", close_body, "action"))

    # ── 3. FLAGGED REPS — WHAT THEY NEED ─────────────────────────────────────
    if red_rows:
        red_body = f"<strong>{len(red_rows)} reps at 3-RED.</strong> What each needs to escape:<br><br>"
        for r in sorted(red_rows, key=lambda x: -(x["ac"] + x["adl"] * 10)):
            activity_flag = ""
            if r["ac"] == 0:
                activity_flag = pill("no calls yet", "red")
            elif r["ac"] < 20:
                activity_flag = pill("very low activity", "red")
            elif r["ac"] > 80 and r["adl"] == 0:
                activity_flag = pill("high calls, no accounts", "yellow")
            if r["adl"] >= 2:
                best = "1 more account gets to 2-YLW"
            elif r["ac"] >= 100:
                best = f"{125 - r['ac']:.0f} more calls to green"
            elif r["ad"] >= 90:
                best = f"{120 - r['ad']:.0f} more min to green"
            else:
                best = "needs effort across all metrics"
            p_a = pill(f"{r['adl']:.0f}a", "red")
            p_c = pill(f"{r['ac']:.0f}c")
            p_d = pill(f"{r['ad']:.0f}m")
            p_b = pill(best, "blue")
            sep = " " + activity_flag + " " if activity_flag else " "
            red_body += f"&bull; <strong>{r['name']}</strong>: {p_a} {p_c} {p_d}{sep}&rarr; {p_b}<br>"
        points.append(tp("Path out of red", f"{len(red_rows)} reps need a push — here's what it takes", red_body, "concern"))

    # ── 4. EFFICIENCY CALLOUT ─────────────────────────────────────────────────
    high_calls_no_accts = [r for r in rows if r["ac"] > 60 and r["adl"] == 0]
    if high_calls_no_accts:
        eff_body = "These reps have strong call volume today but haven't opened any accounts:<br><br>"
        for r in sorted(high_calls_no_accts, key=lambda x: -x["ac"]):
            p_c = pill(f"{r['ac']:.0f} calls", "yellow")
            p_d = pill(f"{r['ad']:.0f} min")
            p_z = pill("0 accounts", "red")
            eff_body += f"&bull; <strong>{r['name']}</strong>: {p_c} {p_d} {p_z} &mdash; 1 account puts them at 2-YLW<br>"
        points.append(tp("Efficiency watch", "High call volume, no accounts yet today", eff_body, "concern"))

    # ── 5. APPROVALS TODAY ────────────────────────────────────────────────────
    reps_with_ap = [r for r in rows if r["apd"] > 0]
    total_ap = sum(r["apd"] for r in rows)
    if reps_with_ap:
        ap_body = f"<strong>{total_ap:.0f} total approvals today</strong> across {len(reps_with_ap)} reps.<br><br>"
        for r in sorted(reps_with_ap, key=lambda x: -x["apd"]):
            p_ap = pill(f"{r['apd']:.0f} apprvs", "purple")
            p_a  = pill(f"{r['adl']:.0f} accts")
            ap_body += f"&bull; <strong>{r['name']}</strong>: {p_ap} {p_a}<br>"
        points.append(tp("Approvals today", f"{total_ap:.0f} approvals logged — {len(reps_with_ap)} reps contributing", ap_body, "positive"))

    # Remove last divider
    if points:
        points[-1] = points[-1].replace("border-bottom:.5px solid #f0f0f0", "border-bottom:none")

    return (
        f'<div style="background:var(--white,#fff);border:1px solid var(--border,#e0e0e0);'
        f'border-radius:12px;overflow:hidden;margin-bottom:1.5rem">'
        f'<div style="background:#f4f3ef;padding:12px 16px;font-size:13px;font-weight:600;'
        f'border-bottom:1px solid var(--border,#e0e0e0)">&#128200; In-day analysis &mdash; {d1_fmt}</div>'
        f'<div style="padding:16px">{"".join(points)}</div>'
        f'</div>'
    )



def generate_hourly_chart(hourly_counts, d1_fmt):
    hours  = list(range(6, 19))
    counts = [hourly_counts.get(h, 0) for h in hours]
    max_c  = max(counts) if max(counts) > 0 else 1
    W, H   = 900, 170
    pl, pr, pt, pb = 36, 16, 18, 36
    cw = W - pl - pr
    ch = H - pt - pb
    bw = (cw - 4 * (len(hours) - 1)) // len(hours)
    lbl_map = {6:"6am",7:"7am",8:"8am",9:"9am",10:"10am",11:"11am",
               12:"12pm",13:"1pm",14:"2pm",15:"3pm",16:"4pm",17:"5pm",18:"6pm"}
    parts = []
    for pct in [0.25, 0.5, 0.75, 1.0]:
        gy = pt + ch - int(pct * ch)
        gv = int(pct * max_c)
        parts.append("<line x1=\"" + str(pl) + "\" y1=\"" + str(gy) + "\" x2=\"" + str(W-pr) + "\" y2=\"" + str(gy) + "\" stroke=\"#f3f4f6\" stroke-width=\"1\"/>")
        parts.append("<text x=\"" + str(pl-4) + "\" y=\"" + str(gy+3) + "\" text-anchor=\"end\" font-size=\"8\" fill=\"#9ca3af\">" + str(gv) + "</text>")
    for i, (h, c) in enumerate(zip(hours, counts)):
        x  = pl + i * (bw + 4)
        bh = max(2, int((c / max_c) * ch))
        y  = pt + ch - bh
        fill = "#e5e7eb"
        if c > 0:
            if c >= max_c * 0.8:   fill = "#05764a"
            elif c >= max_c * 0.5: fill = "#0e7490"
            elif c >= max_c * 0.25:fill = "#f59e0b"
            else:                   fill = "#d1d5db"
        parts.append("<rect x=\"" + str(x) + "\" y=\"" + str(y) + "\" width=\"" + str(bw) + "\" height=\"" + str(bh) + "\" fill=\"" + fill + "\" rx=\"2\"/>")
        if c > 0:
            parts.append("<text x=\"" + str(x+bw//2) + "\" y=\"" + str(y-3) + "\" text-anchor=\"middle\" font-size=\"9\" fill=\"#6b7280\">" + str(c) + "</text>")
        lb = lbl_map.get(h, str(h))
        parts.append("<text x=\"" + str(x+bw//2) + "\" y=\"" + str(pt+ch+16) + "\" text-anchor=\"middle\" font-size=\"9\" fill=\"#9ca3af\">" + lb + "</text>")
    total = sum(counts)
    ph    = hours[counts.index(max(counts))] if max(counts) > 0 else None
    plbl  = lbl_map.get(ph, "N/A") if ph else "N/A"
    inner = " ".join(parts)
    mono  = "IBM Plex Mono,monospace"
    html  = (
        "<div style=\"background:var(--white);border:1px solid var(--border);border-radius:12px;padding:14px 16px;margin-bottom:1.5rem\">"
        "<div style=\"display:flex;justify-content:space-between;align-items:center;margin-bottom:10px\">"
        "<div style=\"font-family:" + mono + ";font-size:10px;text-transform:uppercase;letter-spacing:.12em;color:var(--ink3)\">Call volume by hour &mdash; " + d1_fmt + "</div>"
        "<div style=\"display:flex;gap:16px;font-family:" + mono + ";font-size:11px;color:var(--ink3)\">"
        "<span>Total: <strong style=\"color:var(--ink)\">" + str(total) + "</strong></span> "
        "<span>Peak: <strong style=\"color:var(--ink)\">" + plbl + " (" + str(max(counts)) + ")</strong></span>"
        "</div></div>"
        "<svg viewBox=\"0 0 " + str(W) + " " + str(H) + "\" xmlns=\"http://www.w3.org/2000/svg\" style=\"width:100%;height:auto\">"
        + inner +
        "</svg></div>"
    )
    return html


def generate_html(d1, data, analysis_html="", inday_analysis_html=""):
    rows      = data["rows"]
    n         = len(rows)
    cnt5 = sum(1 for r in rows if r["pts"] == 5)
    cnt4 = sum(1 for r in rows if r["pts"] == 4)
    cnt3 = sum(1 for r in rows if r["pts"] == 3)
    cnt2 = sum(1 for r in rows if r["pts"] == 2)
    cnt1 = sum(1 for r in rows if r["pts"] == 1)
    cnt4plus = cnt4 + cnt5
    org_avg_c  = sum(r["ac"]  for r in rows) / n
    org_avg_d  = sum(r["ad"]  for r in rows) / n
    org_avg_a  = sum(r["adl"] for r in rows) / n
    org_tot_ap = data["org_tot_ap"]
    d1_fmt = fmt_day(d1)
    now_pt = now_pt_str()
    chart_html = generate_hourly_chart(data.get("hourly_counts", {}), d1_fmt)
    now_str    = date.today().strftime("%B %d, %Y").replace(" 0", " ")

    table_rows = ""
    for r in rows:
        table_rows += f"""
      <tr class="{row_cls(r['pts'])}">
        <td class="rep-name">{r['name']}</td>
        <td class="r">{r['ac']:.0f}</td>
        <td class="r">{r['ad']:.1f}m</td>
        <td class="r">{r['adl']:.0f}</td>
        <td class="approvals">{r['apd']:.0f}</td>
        <td class="r">{badge(r['pts'])}</td>
        <td class="reason">{r['reason']}</td>
      </tr>"""

    flagged = "".join(f"""
      <div class="callout-item">
        <div><div class="callout-name">{r['name']}</div>
        <div class="callout-stats">{r['ac']:.0f}c &middot; {r['ad']:.1f}m &middot; {r['adl']:.0f}a &middot; {r['apd']:.0f}ap</div></div>
        <div class="callout-note">{r['reason']}</div>
      </div>""" for r in rows if r["pts"] == 1) or '<div class="callout-item"><div>No reps flagged</div></div>'

    greens = "".join(f"""
      <div class="callout-item">
        <div><div class="callout-name">{r['name']}</div>
        <div class="callout-stats">{r['ac']:.0f}c &middot; {r['ad']:.1f}m &middot; {r['adl']:.0f}a &middot; {r['apd']:.0f}ap</div></div>
        <div class="callout-note">{badge(r['pts'])}</div>
      </div>""" for r in rows if r["pts"] >= 4) or '<div class="callout-item"><div>No top performers yet today</div></div>'

    cs = data["conlan_stats"]
    ss = data["stokoe_stats"]
    cr = data["conlan_rolling"]
    sr = data["stokoe_rolling"]

    conlan_card = sup_card("Brandon Conlan", cs["n"], cs["avg_a"], cs["avg_ap"],
                           cs["pct_goal"], cs["c5"], cs["c4"], cs["c3"], cs["c2"], cs["c1"], cs["tot_ap"], d1)
    stokoe_card = sup_card("George Stokoe",  ss["n"], ss["avg_a"], ss["avg_ap"],
                           ss["pct_goal"], ss["c5"], ss["c4"], ss["c3"], ss["c2"], ss["c1"], ss["tot_ap"], d1)
    conlan_roll = rolling_card("Brandon Conlan", "Team Rolling",
                               cr["n_days"], cr["n_reps"], cr["avg_a"], cr["pct_goal"], cr["top3"], cr["bot3"])
    stokoe_roll = rolling_card("George Stokoe",  "Team Rolling",
                               sr["n_days"], sr["n_reps"], sr["avg_a"], sr["pct_goal"], sr["top3"], sr["bot3"])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PC In-Day Activity Report &mdash; {d1_fmt}</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head>
<body>
<div class="page">
  <div class="masthead">
    <div>
      <div class="kicker">KurvPay &mdash; PC In-Day Activity Report</div>
      <h1>Today &mdash; {d1_fmt}</h1>
    </div>
    <div class="masthead-right">
      Metric: new accounts created today<br>
      Scoring: accounts &ge;3 OR calls &gt;124 OR dur &gt;119 min &rarr; 1-GRN<br>
      Approvals: Approved / Conditionally Approved / Auto Approved
    </div>
  </div>
  <div style="background:var(--white);border:1px solid var(--border);border-radius:8px;padding:10px 16px;margin-bottom:1.5rem;display:flex;align-items:center;justify-content:space-between;font-family:'IBM Plex Mono',monospace;font-size:12px">
    <span style="color:var(--ink3);text-transform:uppercase;letter-spacing:.08em;font-size:10px">Last updated</span>
    <span style="font-weight:600;color:var(--ink)">{now_pt}</span>
  </div>
  {chart_html}
  <div class="goals-bar">
    <div class="goal-chip"><span class="label">Daily goal:</span> 3 new accounts</div>
    <div class="goal-chip"><span class="label">Daily goal:</span> 125 calls</div>
    <div class="goal-chip"><span class="label">Daily goal:</span> 120 min talk time</div>
  </div>
  <div class="legend">
    <div class="legend-item"><span class="leg-dot" style="background:var(--grn5)"></span>5-GRN &mdash; accts &ge;3 or (150+ calls &amp; 2+ accts)</div>
    <div class="legend-item"><span class="leg-dot" style="background:var(--blu4)"></span>4-BLU &mdash; calls &ge;150 or accts &ge;2</div>
    <div class="legend-item"><span class="leg-dot" style="background:#f5b800"></span>3-YLW &mdash; 2 of: 100+ calls / 60+ min / 1+ acct</div>
    <div class="legend-item"><span class="leg-dot" style="background:var(--org)"></span>2-ORG &mdash; 2 of: 50+ calls / 30+ min / 1+ acct</div>
    <div class="legend-item"><span class="leg-dot" style="background:var(--red)"></span>1-RED &mdash; below all thresholds</div>
    <div class="legend-item"><span class="leg-dot" style="background:var(--pur)"></span>Approvals &mdash; informational</div>
  </div>
  <div class="summary-row">
    <div class="scard"><div class="scard-label">5-GRN (best)</div><div class="scard-val c5">{cnt5}</div></div>
    <div class="scard"><div class="scard-label">4-BLU</div><div class="scard-val c4">{cnt4}</div></div>
    <div class="scard"><div class="scard-label">3-YLW</div><div class="scard-val c3">{cnt3}</div></div>
    <div class="scard"><div class="scard-label">2-ORG</div><div class="scard-val c2">{cnt2}</div></div>
    <div class="scard"><div class="scard-label">1-RED (worst)</div><div class="scard-val c1">{cnt1}</div></div>
  </div>
  <div class="section-title">Rep scorecard &mdash; today, {d1_fmt}</div>
  <div class="table-wrap"><table>
    <thead><tr>
      <th>Rep</th>
      <th class="r">Calls</th>
      <th class="r">Duration</th>
      <th class="r">Accounts</th>
      <th class="r" style="color:var(--pur)">Approvals</th>
      <th class="r">PTS</th>
      <th>Reason</th>
    </tr></thead>
    <tbody>{table_rows}</tbody>
    <tfoot>
      <tr style="background:#f4f3ef;border-top:1.5px solid var(--border)">
        <td style="font-family:'IBM Plex Mono',monospace;font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--ink3);font-weight:500;padding:9px 12px">Org avg &mdash; {n} reps</td>
        <td class="r" style="font-weight:600;padding:9px 12px;font-family:'IBM Plex Mono',monospace;font-size:12px">{org_avg_c:.1f} avg</td>
        <td class="r" style="font-weight:600;padding:9px 12px;font-family:'IBM Plex Mono',monospace;font-size:12px">{org_avg_d:.1f}m avg</td>
        <td class="r" style="font-weight:600;padding:9px 12px;font-family:'IBM Plex Mono',monospace;font-size:12px">{org_avg_a:.2f} avg</td>
        <td style="text-align:right;font-family:'IBM Plex Mono',monospace;font-size:12px;color:var(--pur);font-weight:600;padding:9px 12px">{org_tot_ap} total</td>
        <td colspan="2" style="padding:9px 12px;font-size:11px;color:var(--ink3);font-family:'IBM Plex Mono',monospace">today&apos;s activity only &middot; as of {now_str}</td>
      </tr>
    </tfoot>
  </table></div>
  <div class="callout-grid">
    <div class="callout-box red-box">
      <div class="callout-head">&#9888; Flagged (1-RED only) &mdash; {cnt1} reps</div>
      {flagged}
    </div>
    <div class="callout-box grn-box">
      <div class="callout-head">&#10003; Top performers (4-BLU &amp; 5-GRN) &mdash; {cnt4plus} reps</div>
      {greens}
    </div>
  </div>
  <div class="section-title">Supervisor team comparison &mdash; today, {d1_fmt}</div>
  <div class="sup-grid">{conlan_card}{stokoe_card}</div>
  <div class="section-title">Analysis &amp; talking points</div>
  {inday_analysis_html}

  <div class="section-title">Rolling data &mdash; {cr['window_label']}</div>
  <div class="rolling-grid">{conlan_roll}{stokoe_roll}</div>
  <div class="section-title">Analysis &amp; talking points</div>
  {analysis_html}

  <div class="footer">
    <span>Source: Zoho CRM &middot; Accounts + Submissions modules &middot; auto-generated &middot; <strong>Updated: {now_pt}</strong></span>
    <span>Today: {d1_fmt} &middot; Rolling: {cr['window_label']}</span>
  </div>
</div>
</body>
</html>"""

# ── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    print("Getting Zoho access token...")
    token = get_access_token()

    d1 = today_pt()
    print(f"Report date: {d1} (today, Pacific time)")

    print("Pulling calls...")
    calls, hourly_counts = pull_calls(token, d1)

    print("Pulling accounts...")
    accts = pull_accounts(token, d1)

    print("Pulling approvals...")
    apprvs = pull_approvals(token, d1)

    print("Pulling rolling data...")
    conlan_rolling = compute_rolling(token, CONLAN)
    stokoe_rolling = compute_rolling(token, STOKOE)

    # Build rows — single day, no averaging
    rows = []
    for last, name in sorted(REP_LAST_TO_FULL.items(), key=lambda x: x[1]):
        c, dur = calls[last]
        a      = accts.get(last, 0)
        ap     = apprvs.get(last, 0)
        pts, reason = score(c, dur, a)
        rows.append({"name": name, "last": last,
                     "ac": c, "ad": dur, "adl": a, "apd": ap,
                     "pts": pts, "reason": reason})

    def team_stats(team_set):
        tr  = [r for r in rows if r["last"] in team_set]
        n   = len(tr)
        return {
            "n":        n,
            "c5":       sum(r["pts"] == 5 for r in tr),
            "c4":       sum(r["pts"] == 4 for r in tr),
            "c3":       sum(r["pts"] == 3 for r in tr),
            "c2":       sum(r["pts"] == 2 for r in tr),
            "c1":       sum(r["pts"] == 1 for r in tr),
            "avg_a":    sum(r["adl"] for r in tr) / n,
            "avg_ap":   sum(r["apd"] for r in tr) / n,
            "pct_goal": sum(r["adl"] >= 3 for r in tr) / n * 100,
            "tot_ap":   sum(apprvs.get(l, 0) for l in team_set),
        }

    data = {
        "rows":           rows,
        "conlan_stats":   team_stats(CONLAN),
        "stokoe_stats":   team_stats(STOKOE),
        "conlan_rolling": conlan_rolling,
        "stokoe_rolling": stokoe_rolling,
        "org_tot_ap":     sum(apprvs.values()),
        "hourly_counts":  hourly_counts,
    }

    print("Generating HTML...")
    def _tstats_inday(team_set):
        tr = [r for r in rows if r["last"] in team_set]
        n  = len(tr)
        return {
            "avg_a":    sum(r["adl"] for r in tr)/n if n else 0,
            "avg_ap":   sum(r["apd"] for r in tr)/n if n else 0,
            "pct_goal": sum(r["adl"]>=3 for r in tr)/n*100 if n else 0,
            "c5": sum(r["pts"]==5 for r in tr),
            "c4": sum(r["pts"]==4 for r in tr),
            "c3": sum(r["pts"]==3 for r in tr),
            "c2": sum(r["pts"]==2 for r in tr),
            "c1": sum(r["pts"]==1 for r in tr),
        }
    team_stats_cur = {"conlan": _tstats_inday(CONLAN), "stokoe": _tstats_inday(STOKOE)}
    analysis_html = generate_analysis(
        rows=rows, prev_rows=None,
        team_stats_cur=team_stats_cur, team_stats_prev=None,
        d1=d1, d2=d1,
        rolling_c=data["conlan_rolling"], rolling_s=data["stokoe_rolling"],
        fmt_day=fmt_day, REP_LAST_TO_FULL=REP_LAST_TO_FULL,
        CONLAN=CONLAN, STOKOE=STOKOE, scoring_system="1-5"
    )
    inday_analysis_html = generate_inday_analysis(rows, fmt_day, d1)
    html = generate_html(d1, data, analysis_html=analysis_html, inday_analysis_html=inday_analysis_html)

    with open("inday_report.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("Saved inday_report.html")

    print("Uploading to tiiny.host...")
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("index.html", html.encode("utf-8"))
    zip_buf.seek(0)

    r = requests.put(
        "https://ext.tiiny.host/v1/upload",
        headers={"x-api-key": TIINY_API_KEY},
        files={"files": ("report.zip", zip_buf, "application/zip")},
        data={"domain": TIINY_DOMAIN},
    )
    if r.status_code == 200:
        print(f"✅ Report live at https://{TIINY_DOMAIN}/")
    else:
        print(f"❌ tiiny upload failed: {r.status_code} {r.text}")
        sys.exit(1)

if __name__ == "__main__":
    main()
