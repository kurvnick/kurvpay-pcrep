"""
KurvPay PC In-Day Activity Report
Pulls today's data from Zoho CRM, scores reps, generates HTML, uploads to tiiny.host
Run every 30 min during business hours via GitHub Actions
"""

import os, sys, io, zipfile, requests
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
CONLAN = {"Kopstein","Silva","Friedhoff","McCausland","Riley","Weiss","Vartevanian",
          "Colovas","Green","Crozier","Landeros","Riffe","Todd"}
STOKOE = {"Daleske","Heflin","Travis","Villasenor","Margolis","Jackson","Lotut",
          "Koestner","Wilkie","Gilden","Bender","Carranza"}
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
    """Returns {last_name: (call_count, duration_minutes)}.
    Zoho COQL does not support two range conditions on the same datetime field,
    so we query from start of day forward and filter the upper bound in Python."""
    day       = date.fromisoformat(day_str)
    next_day  = day + timedelta(days=1)
    start_utc = f"{day}T00:00:00+00:00"
    # Store next_day string for Python-side filtering
    next_day_str = str(next_day)
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
    for r in records:
        # Filter upper bound in Python — keep only records from day_str
        cst = r.get("Call_Start_Time", "")
        if not cst or cst[:10] != day_str:
            continue
        owner = r.get("Owner", {}).get("name", "")
        if owner in ALL_REPS:
            counts[owner] += 1
            secs[owner]   += (r.get("Call_Duration_in_seconds") or 0)
    print(f"  [calls] {sum(counts.values())} records matched {day_str}")
    return {last: (counts[last], round(secs[last] / 60, 1)) for last in ALL_REPS}
    counts = defaultdict(int)
    secs   = defaultdict(int)
    for r in records:
        owner = r.get("Owner", {}).get("name", "")
        if owner in ALL_REPS:
            counts[owner] += 1
            secs[owner]   += (r.get("Call_Duration_in_seconds") or 0)
    return {last: (counts[last], round(secs[last] / 60, 1)) for last in ALL_REPS}

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
    if accts >= 3:                      return 1, "accounts &ge; 3"
    if calls > 124:                     return 1, "calls &gt; 124"
    if dur > 119:                       return 1, "duration &gt; 119 min"
    if accts >= 2:                      return 2, "accounts &ge; 2"
    if 99 < calls <= 149 and dur < 60:  return 2, "100&ndash;149 calls, &lt;60 min"
    if 49 < calls <= 99 and dur > 59:   return 2, "50&ndash;99 calls, &gt;59 min"
    if dur > 89 and calls < 50:         return 2, "duration &gt; 89 min"
    return 3, "otherwise"

# ── HELPERS ──────────────────────────────────────────────────────────────────
def fmt_day(d):
    dt = date.fromisoformat(d)
    if sys.platform == "win32":
        return dt.strftime("%a %b %d").replace(" 0", " ")
    return dt.strftime("%a %b %-d")

def badge(pts):
    cls   = {1:"grn", 2:"ylw", 3:"red"}[pts]
    label = {1:"1-GRN", 2:"2-YLW", 3:"3-RED"}[pts]
    return f'<span class="badge badge-{cls}">{label}</span>'

def row_cls(pts):
    return {1:"row-grn", 2:"row-ylw", 3:"row-red"}[pts]

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
def sup_card(name, team_size, avg_a, avg_ap, pct_goal, grn, ylw, red, tot_ap, d1):
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
          <div class="pts-chip grn-chip"><div class="pts-num">{grn}</div><div class="pts-lbl">1-GRN</div></div>
          <div class="pts-chip ylw-chip"><div class="pts-num">{ylw}</div><div class="pts-lbl">2-YLW</div></div>
          <div class="pts-chip red-chip"><div class="pts-num">{red}</div><div class="pts-lbl">3-RED</div></div>
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
  --grn:#05764a;--grn-bg:#e6f7ef;--grn-border:#b3e6ce;
  --ylw:#a86400;--ylw-bg:#fef9e6;--ylw-border:#f5d98c;
  --red:#c0111a;--red-bg:#fdf0f0;--red-border:#f0b0b3;
  --pur:#5b21b6;--pur-bg:#ede9fe;--pur-border:#c4b5fd;
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
.summary-row{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:1.75rem}
.scard{background:var(--white);border:1px solid var(--border);border-radius:10px;padding:14px 16px}
.scard-label{font-family:'IBM Plex Mono',monospace;font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--ink3);margin-bottom:5px}
.scard-val{font-size:30px;font-weight:600;line-height:1}
.scard-val.red{color:var(--red)}.scard-val.grn{color:var(--grn)}.scard-val.ylw{color:var(--ylw)}
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
tr.row-grn td:first-child{border-left:3px solid var(--grn)}
tr.row-ylw td:first-child{border-left:3px solid #f5b800}
tr.row-red td:first-child{border-left:3px solid var(--red)}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:500;white-space:nowrap}
.badge-grn{background:var(--grn-bg);color:var(--grn);border:1px solid var(--grn-border)}
.badge-ylw{background:var(--ylw-bg);color:var(--ylw);border:1px solid var(--ylw-border)}
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
.pts-dist{display:flex;gap:6px;margin-top:12px;padding-top:12px;border-top:1px solid var(--border2)}
.pts-chip{flex:1;text-align:center;border-radius:6px;padding:8px 4px}
.pts-chip .pts-num{font-size:18px;font-weight:600}
.pts-chip .pts-lbl{font-size:10px;font-family:'IBM Plex Mono',monospace;text-transform:uppercase;letter-spacing:.06em;margin-top:2px}
.grn-chip{background:var(--grn-bg);color:var(--grn)}
.ylw-chip{background:var(--ylw-bg);color:var(--ylw)}
.red-chip{background:var(--red-bg);color:var(--red)}
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
def generate_html(d1, data):
    rows      = data["rows"]
    grn_count = sum(1 for r in rows if r["pts"] == 1)
    ylw_count = sum(1 for r in rows if r["pts"] == 2)
    red_count = sum(1 for r in rows if r["pts"] == 3)
    n         = len(rows)
    org_avg_c  = sum(r["ac"]  for r in rows) / n
    org_avg_d  = sum(r["ad"]  for r in rows) / n
    org_avg_a  = sum(r["adl"] for r in rows) / n
    org_tot_ap = data["org_tot_ap"]
    d1_fmt     = fmt_day(d1)
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
      </div>""" for r in rows if r["pts"] == 3) or '<div class="callout-item"><div>No reps flagged</div></div>'

    greens = "".join(f"""
      <div class="callout-item">
        <div><div class="callout-name">{r['name']}</div>
        <div class="callout-stats">{r['ac']:.0f}c &middot; {r['ad']:.1f}m &middot; {r['adl']:.0f}a &middot; {r['apd']:.0f}ap</div></div>
        <div class="callout-note">{r['reason']}</div>
      </div>""" for r in rows if r["pts"] == 1) or '<div class="callout-item"><div>No green performers yet</div></div>'

    cs = data["conlan_stats"]
    ss = data["stokoe_stats"]
    cr = data["conlan_rolling"]
    sr = data["stokoe_rolling"]

    conlan_card = sup_card("Brandon Conlan", cs["n"], cs["avg_a"], cs["avg_ap"],
                           cs["pct_goal"], cs["grn"], cs["ylw"], cs["red"], cs["tot_ap"], d1)
    stokoe_card = sup_card("George Stokoe",  ss["n"], ss["avg_a"], ss["avg_ap"],
                           ss["pct_goal"], ss["grn"], ss["ylw"], ss["red"], ss["tot_ap"], d1)
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
  <div class="goals-bar">
    <div class="goal-chip"><span class="label">Daily goal:</span> 3 new accounts</div>
    <div class="goal-chip"><span class="label">Daily goal:</span> 125 calls</div>
    <div class="goal-chip"><span class="label">Daily goal:</span> 120 min talk time</div>
  </div>
  <div class="legend">
    <div class="legend-item"><span class="leg-dot" style="background:var(--grn)"></span>1-GRN &mdash; accounts &ge;3 OR calls &gt;124 OR dur &gt;119 min</div>
    <div class="legend-item"><span class="leg-dot" style="background:#f5b800"></span>2-YLW &mdash; accounts &ge;2 or activity threshold met</div>
    <div class="legend-item"><span class="leg-dot" style="background:var(--red)"></span>3-RED &mdash; below all thresholds</div>
    <div class="legend-item"><span class="leg-dot" style="background:var(--pur)"></span>Approvals &mdash; informational, does not affect PTS</div>
  </div>
  <div class="summary-row">
    <div class="scard"><div class="scard-label">1-GRN performers</div><div class="scard-val grn">{grn_count}</div></div>
    <div class="scard"><div class="scard-label">2-YLW middle tier</div><div class="scard-val ylw">{ylw_count}</div></div>
    <div class="scard"><div class="scard-label">3-RED flagged</div><div class="scard-val red">{red_count}</div></div>
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
      <div class="callout-head">&#9888; Flagged for leadflow review &mdash; {red_count} reps</div>
      {flagged}
    </div>
    <div class="callout-box grn-box">
      <div class="callout-head">&#10003; 1-GRN performers &mdash; {grn_count} reps</div>
      {greens}
    </div>
  </div>
  <div class="section-title">Supervisor team comparison &mdash; today, {d1_fmt}</div>
  <div class="sup-grid">{conlan_card}{stokoe_card}</div>
  <div class="section-title">Rolling data &mdash; {cr['window_label']}</div>
  <div class="rolling-grid">{conlan_roll}{stokoe_roll}</div>
  <div class="footer">
    <span>Source: Zoho CRM &middot; Accounts + Submissions modules &middot; auto-generated</span>
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
    calls = pull_calls(token, d1)

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
            "grn":      sum(r["pts"] == 1 for r in tr),
            "ylw":      sum(r["pts"] == 2 for r in tr),
            "red":      sum(r["pts"] == 3 for r in tr),
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
    }

    print("Generating HTML...")
    html = generate_html(d1, data)

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
