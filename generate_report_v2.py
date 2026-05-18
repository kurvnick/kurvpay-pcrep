"""
KurvPay PC Activity Report v2
- Accounts: 5-day trailing average
- Calls & Duration: 2-day trailing average
- Scoring: 5 (best) to 1 (worst)
Runs daily via GitHub Actions, uploads to tiiny.host
"""

import os, sys, io, zipfile, requests
from datetime import date, timedelta, datetime
from collections import defaultdict

# ── CONFIG ───────────────────────────────────────────────────────────────────
ZOHO_CLIENT_ID     = os.environ["ZOHO_CLIENT_ID"]
ZOHO_CLIENT_SECRET = os.environ["ZOHO_CLIENT_SECRET"]
ZOHO_REFRESH_TOKEN = os.environ["ZOHO_REFRESH_TOKEN"]
TIINY_API_KEY      = os.environ["TIINY_API_KEY"]
TIINY_DOMAIN       = "paymentcloudcallstats-v2.tiiny.site"

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
    from zoneinfo import ZoneInfo
    _PT = ZoneInfo("America/Los_Angeles")
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "tzdata", "-q"])
    from zoneinfo import ZoneInfo
    _PT = ZoneInfo("America/Los_Angeles")

def today_pt():
    return datetime.now(_PT).strftime("%Y-%m-%d")

def last_n_business_days(n):
    """Return list of last n business days as ISO strings, oldest first."""
    today = date.fromisoformat(today_pt())
    days = []
    d = today - timedelta(days=1)
    while len(days) < n:
        if d.weekday() < 5:
            days.append(str(d))
        d -= timedelta(days=1)
    return list(reversed(days))

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
        paginated = f"{query.strip()} LIMIT 200 OFFSET {offset}"
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
                print(f"  [zoho_coql] unexpected: {str(data)[:200]}")
            break
        records.extend(data["data"])
        if not data.get("info", {}).get("more_records"):
            break
        offset += 200
    return records

# ── DATA PULLS ────────────────────────────────────────────────────────────────
def pull_calls(token, day_str):
    """2-day trailing — returns {last: (count, dur_min)}"""
    day = date.fromisoformat(day_str)
    start_utc = f"{day}T00:00:00+00:00"
    query = (
        f"SELECT Owner, Call_Duration_in_seconds, Call_Start_Time FROM Calls "
        f"WHERE Call_Start_Time >= '{start_utc}' "
        f"AND Call_Type not in ('Missed')"
    )
    records = zoho_coql(token, query)
    counts = defaultdict(int)
    secs   = defaultdict(int)
    for r in records:
        if r.get("Call_Start_Time", "")[:10] != day_str:
            continue
        owner = r.get("Owner", {}).get("name", "")
        if owner in ALL_REPS:
            counts[owner] += 1
            secs[owner]   += (r.get("Call_Duration_in_seconds") or 0)
    return {last: (counts[last], round(secs[last] / 60, 1)) for last in ALL_REPS}

def pull_accounts_day(token, day_str):
    """Returns {last: count} for a single day."""
    day      = date.fromisoformat(day_str)
    next_day = day + timedelta(days=1)
    start_utc = f"{day}T00:00:00+00:00"
    end_utc   = f"{next_day}T00:00:00+00:00"
    records = zoho_coql(token,
        f"SELECT Owner FROM Accounts "
        f"WHERE Created_Time >= '{start_utc}' "
        f"AND Created_Time < '{end_utc}'"
    )
    counts = defaultdict(int)
    for r in records:
        owner = r.get("Owner", {}).get("name", "")
        if owner in ALL_REPS:
            counts[owner] += 1
    return dict(counts)

def pull_approvals_day(token, day_str):
    """Returns {last: count} for a single day."""
    counts = defaultdict(int)
    for stage in APPROVAL_STAGES:
        for r in zoho_coql(token,
            f"SELECT Owner FROM Deals "
            f"WHERE Stage = '{stage}' AND Closing_Date = '{day_str}'"
        ):
            owner = r.get("Owner", {}).get("name", "")
            if owner in ALL_REPS:
                counts[owner] += 1
    return dict(counts)

# ── SCORING ───────────────────────────────────────────────────────────────────
def score_v2(calls, dur, accts):
    """
    5 (best) to 1 (worst)
    5 = accounts >= 3 OR (calls >= 150 AND accounts >= 2)
    4 = calls >= 150 OR accounts >= 2
    3 = at least 2 of: calls >= 100 / dur >= 60 / accounts >= 1
    2 = at least 2 of: calls >= 50 / dur >= 30 / accounts >= 1
    1 = none of the above
    """
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

# ── HELPERS ───────────────────────────────────────────────────────────────────
def fmt_day(d):
    dt = date.fromisoformat(d)
    if sys.platform == "win32":
        return dt.strftime("%a %b %d").replace(" 0", " ")
    return dt.strftime("%a %b %-d")

def badge(pts):
    colors = {5:"grn5", 4:"grn4", 3:"ylw", 2:"org", 1:"red"}
    labels = {5:"5-GRN", 4:"4-GRN", 3:"3-YLW", 2:"2-ORG", 1:"1-RED"}
    return f'<span class="badge badge-{colors[pts]}">{labels[pts]}</span>'

def row_cls(pts):
    return {5:"row-5", 4:"row-4", 3:"row-3", 2:"row-2", 1:"row-1"}[pts]

# ── ROLLING 30-DAY ────────────────────────────────────────────────────────────
def compute_rolling(token, team_set, window_days=30):
    today = date.fromisoformat(today_pt())
    bdays = []
    d = today - timedelta(days=1)
    while len(bdays) < window_days:
        if d.weekday() < 5:
            bdays.append(str(d))
        d -= timedelta(days=1)
    bdays.reverse()

    start_utc = f"{bdays[0]}T00:00:00+00:00"
    end_utc   = f"{str(date.fromisoformat(bdays[-1]) + timedelta(days=1))}T00:00:00+00:00"
    records = zoho_coql(token,
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

# ── CSS ───────────────────────────────────────────────────────────────────────
CSS = """
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --ink:#0f0f0f;--ink2:#444;--ink3:#888;--border:#e0e0e0;--border2:#f0f0f0;
  --bg:#fafaf8;--white:#ffffff;
  --grn5:#05764a;--grn5-bg:#d1fae5;--grn5-border:#6ee7b7;
  --grn4:#0e7490;--grn4-bg:#e0f2fe;--grn4-border:#7dd3fc;
  --ylw:#a86400;--ylw-bg:#fef9e6;--ylw-border:#f5d98c;
  --org:#c2410c;--org-bg:#fff7ed;--org-border:#fdba74;
  --red:#c0111a;--red-bg:#fdf0f0;--red-border:#f0b0b3;
  --pur:#5b21b6;--pur-bg:#ede9fe;--pur-border:#c4b5fd;
}
body{font-family:'IBM Plex Sans',sans-serif;background:var(--bg);color:var(--ink);min-height:100vh;padding:2.5rem 1.5rem 4rem}
.page{max-width:980px;margin:0 auto}
.masthead{display:flex;align-items:flex-end;justify-content:space-between;padding-bottom:1.25rem;margin-bottom:1.75rem;border-bottom:2px solid var(--ink);flex-wrap:wrap;gap:12px}
.kicker{font-family:'IBM Plex Mono',monospace;font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--ink3);margin-bottom:5px}
h1{font-size:22px;font-weight:600;line-height:1.2}
.masthead-right{text-align:right;font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--ink3);line-height:1.7}
.legend{display:flex;gap:16px;margin-bottom:1.5rem;flex-wrap:wrap;font-size:12px;color:var(--ink2);background:var(--white);border:1px solid var(--border);border-radius:10px;padding:10px 14px}
.legend-item{display:flex;align-items:center;gap:6px}
.leg-dot{width:8px;height:8px;border-radius:2px;display:inline-block}
.notice{background:var(--grn5-bg);border:1px solid var(--grn5-border);border-radius:8px;padding:10px 14px;font-size:12px;color:var(--grn5);margin-bottom:1.5rem;font-family:'IBM Plex Mono',monospace}
.summary-row{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-bottom:1.75rem}
.scard{background:var(--white);border:1px solid var(--border);border-radius:10px;padding:12px 14px}
.scard-label{font-family:'IBM Plex Mono',monospace;font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--ink3);margin-bottom:5px}
.scard-val{font-size:26px;font-weight:600;line-height:1}
.scard-val.c5{color:var(--grn5)}.scard-val.c4{color:var(--grn4)}.scard-val.c3{color:var(--ylw)}.scard-val.c2{color:var(--org)}.scard-val.c1{color:var(--red)}
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
td.apprvs{text-align:right;font-family:'IBM Plex Mono',monospace;font-size:12px;color:var(--pur);font-weight:500}
tr.row-5 td:first-child{border-left:3px solid var(--grn5)}
tr.row-4 td:first-child{border-left:3px solid var(--grn4)}
tr.row-3 td:first-child{border-left:3px solid #f5b800}
tr.row-2 td:first-child{border-left:3px solid var(--org)}
tr.row-1 td:first-child{border-left:3px solid var(--red)}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:500;white-space:nowrap}
.badge-grn5{background:var(--grn5-bg);color:var(--grn5);border:1px solid var(--grn5-border)}
.badge-grn4{background:var(--grn4-bg);color:var(--grn4);border:1px solid var(--grn4-border)}
.badge-ylw{background:var(--ylw-bg);color:var(--ylw);border:1px solid var(--ylw-border)}
.badge-org{background:var(--org-bg);color:var(--org);border:1px solid var(--org-border)}
.badge-red{background:var(--red-bg);color:var(--red);border:1px solid var(--red-border)}
.callout-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:1.5rem}
.callout-box{border-radius:10px;overflow:hidden;border:1px solid}
.red-box{border-color:var(--red-border);background:var(--red-bg)}
.grn-box{border-color:var(--grn5-border);background:var(--grn5-bg)}
.callout-head{padding:10px 14px;font-size:12px;font-weight:600;border-bottom:1px solid}
.red-box .callout-head{color:var(--red);border-color:var(--red-border)}
.grn-box .callout-head{color:var(--grn5);border-color:var(--grn5-border)}
.callout-item{padding:8px 14px;border-bottom:.5px solid;font-size:12px;display:flex;justify-content:space-between;align-items:center}
.red-box .callout-item{border-color:var(--red-border)}
.grn-box .callout-item{border-color:var(--grn5-border)}
.callout-item:last-child{border-bottom:none}
.callout-name{font-weight:500}
.callout-stats{font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--ink3);margin-top:1px}
.callout-note{font-size:11px;color:var(--ink3);text-align:right;max-width:150px}
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
.chip-4{background:var(--grn4-bg);color:var(--grn4)}
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
@media(max-width:640px){.callout-grid,.sup-grid,.rolling-grid,.summary-row{grid-template-columns:1fr 1fr}th,td{padding:7px 9px}}
@media print{body{background:#fff;padding:1rem}}
"""

# ── HTML COMPONENTS ───────────────────────────────────────────────────────────
def sup_card(name, team_size, avg_a, avg_ap, pct_goal, c5, c4, c3, c2, c1, tot_ap_d1, tot_ap_d2, d1, d2):
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
            <div class="metric-label">Avg accts / rep / day (5-day)</div>
            <div class="metric-val">{avg_a:.2f}</div>
            <div class="metric-goal">Goal: 3.0 &middot; {bar_w}% to goal</div>
            <div class="progress-bar-wrap"><div class="progress-bar-fill" style="width:{bar_w}%"></div></div>
          </div>
          <div>
            <div class="metric-label">Avg apprvs / rep / day</div>
            <div class="metric-val" style="color:var(--pur)">{avg_ap:.2f}</div>
          </div>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:14px">
          <div>
            <div class="metric-label">% reps at acct goal</div>
            <div class="metric-val" style="font-size:16px">{pct_goal:.1f}%</div>
          </div>
          <div>
            <div class="metric-label">Total apprvs / day</div>
            <div class="metric-val" style="color:var(--pur);font-size:16px">{(tot_ap_d1+tot_ap_d2)/2:.1f}</div>
            <div class="metric-goal">{tot_ap_d1} ({fmt_day(d1)}) &middot; {tot_ap_d2} ({fmt_day(d2)})</div>
          </div>
        </div>
        <div class="pts-dist">
          <div class="pts-chip chip-5"><div class="pts-num">{c5}</div><div class="pts-lbl">5-GRN</div></div>
          <div class="pts-chip chip-4"><div class="pts-num">{c4}</div><div class="pts-lbl">4-GRN</div></div>
          <div class="pts-chip chip-3"><div class="pts-num">{c3}</div><div class="pts-lbl">3-YLW</div></div>
          <div class="pts-chip chip-2"><div class="pts-num">{c2}</div><div class="pts-lbl">2-ORG</div></div>
          <div class="pts-chip chip-1"><div class="pts-num">{c1}</div><div class="pts-lbl">1-RED</div></div>
        </div>
      </div>
    </div>"""

def rolling_card(sup_name, n_days, n_reps, avg_a, pct_goal, top3, bot3):
    bar_w    = min(100, int(avg_a / 3 * 100))
    top_rows = "".join(
        f'<div style="display:flex;justify-content:space-between">'
        f'<span style="font-weight:500">{n}</span>'
        f'<span style="font-family:\'IBM Plex Mono\',monospace;color:var(--grn5)">{v:.2f}/day</span></div>'
        for n, v in top3)
    bot_rows = "".join(
        f'<div style="display:flex;justify-content:space-between">'
        f'<span style="font-weight:500">{n}</span>'
        f'<span style="font-family:\'IBM Plex Mono\',monospace;color:var(--red)">{v:.2f}/day</span></div>'
        for n, v in bot3)
    return f"""
    <div class="rolling-card">
      <div class="rolling-head">
        <div class="rh-name">{sup_name} &mdash; Team Rolling</div>
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

# ── HTML GENERATION ───────────────────────────────────────────────────────────
def generate_html(d1, d2, acct_days, data):
    rows = data["rows"]
    n    = len(rows)
    cnt  = {i: sum(1 for r in rows if r["pts"] == i) for i in range(1, 6)}
    org_avg_c  = sum(r["ac"]  for r in rows) / n
    org_avg_d  = sum(r["ad"]  for r in rows) / n
    org_avg_a  = sum(r["adl"] for r in rows) / n
    org_avg_ap = sum(r["apd"] for r in rows) / n
    org_ap_d1  = data["org_ap_d1"]
    org_ap_d2  = data["org_ap_d2"]
    d1_fmt     = fmt_day(d1)
    d2_fmt     = fmt_day(d2)
    acct_label = " &ndash; ".join(fmt_day(d) for d in [acct_days[0], acct_days[-1]])

    table_rows = ""
    for r in rows:
        table_rows += f"""
      <tr class="{row_cls(r['pts'])}">
        <td class="rep-name">{r['name']}</td>
        <td class="r">{r['ac']:.1f}</td>
        <td class="r">{r['ad']:.1f}m</td>
        <td class="r">{r['adl']:.2f}</td>
        <td class="apprvs">{r['apd']:.1f}</td>
        <td class="r">{badge(r['pts'])}</td>
        <td class="reason">{r['reason']}</td>
      </tr>"""

    flagged = "".join(f"""
      <div class="callout-item">
        <div><div class="callout-name">{r['name']}</div>
        <div class="callout-stats">{r['ac']:.1f}c &middot; {r['ad']:.1f}m &middot; {r['adl']:.2f}a &middot; {r['apd']:.1f}ap</div></div>
        <div class="callout-note">{r['reason']}</div>
      </div>""" for r in rows if r["pts"] <= 2) or '<div class="callout-item"><div>No reps flagged</div></div>'

    greens = "".join(f"""
      <div class="callout-item">
        <div><div class="callout-name">{r['name']}</div>
        <div class="callout-stats">{r['ac']:.1f}c &middot; {r['ad']:.1f}m &middot; {r['adl']:.2f}a &middot; {r['apd']:.1f}ap</div></div>
        <div class="callout-note">{r['reason']}</div>
      </div>""" for r in rows if r["pts"] == 5) or '<div class="callout-item"><div>No 5-GRN performers</div></div>'

    cs = data["conlan_stats"]
    ss = data["stokoe_stats"]
    cr = data["conlan_rolling"]
    sr = data["stokoe_rolling"]

    c_card = sup_card("Brandon Conlan", cs["n"], cs["avg_a"], cs["avg_ap"], cs["pct_goal"],
                      cs["c5"],cs["c4"],cs["c3"],cs["c2"],cs["c1"],
                      cs["ap_d1"], cs["ap_d2"], d1, d2)
    s_card = sup_card("George Stokoe",  ss["n"], ss["avg_a"], ss["avg_ap"], ss["pct_goal"],
                      ss["c5"],ss["c4"],ss["c3"],ss["c2"],ss["c1"],
                      ss["ap_d1"], ss["ap_d2"], d1, d2)
    c_roll = rolling_card("Brandon Conlan", cr["n_days"], cr["n_reps"], cr["avg_a"], cr["pct_goal"], cr["top3"], cr["bot3"])
    s_roll = rolling_card("George Stokoe",  sr["n_days"], sr["n_reps"], sr["avg_a"], sr["pct_goal"], sr["top3"], sr["bot3"])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PC Activity Report v2 &mdash; {d1_fmt} &amp; {d2_fmt}</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head>
<body>
<div class="page">

  <div class="masthead">
    <div>
      <div class="kicker">KurvPay &mdash; PC Activity Report v2</div>
      <h1>Calls/Duration: avg {d1_fmt} &amp; {d2_fmt} &middot; Accounts: 5-day avg</h1>
    </div>
    <div class="masthead-right">
      Accounts avg: {acct_label}<br>
      Calls/dur avg: {d1_fmt} &amp; {d2_fmt}<br>
      Approvals: Approved / Conditionally Approved / Auto Approved
    </div>
  </div>

  <div class="notice">
    &#9432; Accounts use a 5-day trailing average ({acct_label}).
    Calls and duration use the standard 2-day average ({d1_fmt} &amp; {d2_fmt}).
  </div>

  <div class="legend">
    <div class="legend-item"><span class="leg-dot" style="background:var(--grn5)"></span>5-GRN &mdash; accts &ge;3 or (150+ calls &amp; 2+ accts)</div>
    <div class="legend-item"><span class="leg-dot" style="background:var(--grn4)"></span>4-GRN &mdash; calls &ge;150 or accts &ge;2</div>
    <div class="legend-item"><span class="leg-dot" style="background:#f5b800"></span>3-YLW &mdash; 2 of: 100+ calls / 60+ min / 1+ acct</div>
    <div class="legend-item"><span class="leg-dot" style="background:var(--org)"></span>2-ORG &mdash; 2 of: 50+ calls / 30+ min / 1+ acct</div>
    <div class="legend-item"><span class="leg-dot" style="background:var(--red)"></span>1-RED &mdash; below all thresholds</div>
    <div class="legend-item"><span class="leg-dot" style="background:var(--pur)"></span>Approvals &mdash; informational</div>
  </div>

  <div class="summary-row">
    <div class="scard"><div class="scard-label">5-GRN (best)</div><div class="scard-val c5">{cnt[5]}</div></div>
    <div class="scard"><div class="scard-label">4-GRN</div><div class="scard-val c4">{cnt[4]}</div></div>
    <div class="scard"><div class="scard-label">3-YLW</div><div class="scard-val c3">{cnt[3]}</div></div>
    <div class="scard"><div class="scard-label">2-ORG</div><div class="scard-val c2">{cnt[2]}</div></div>
    <div class="scard"><div class="scard-label">1-RED (worst)</div><div class="scard-val c1">{cnt[1]}</div></div>
  </div>

  <div class="section-title">Rep scorecard &mdash; calls/dur avg {d1_fmt} &amp; {d2_fmt} &middot; accts 5-day avg</div>
  <div class="table-wrap"><table>
    <thead><tr>
      <th>Rep</th>
      <th class="r">Avg calls (2d)</th>
      <th class="r">Avg dur (2d)</th>
      <th class="r">Avg accts (5d)</th>
      <th class="r" style="color:var(--pur)">Avg apprvs</th>
      <th class="r">PTS</th>
      <th>Reason</th>
    </tr></thead>
    <tbody>{table_rows}</tbody>
    <tfoot>
      <tr style="background:#f4f3ef;border-top:1.5px solid var(--border)">
        <td style="font-family:'IBM Plex Mono',monospace;font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--ink3);font-weight:500;padding:9px 12px">Org avg &mdash; {n} reps</td>
        <td class="r" style="font-weight:600;padding:9px 12px;font-family:'IBM Plex Mono',monospace;font-size:12px">{org_avg_c:.1f}</td>
        <td class="r" style="font-weight:600;padding:9px 12px;font-family:'IBM Plex Mono',monospace;font-size:12px">{org_avg_d:.1f}m</td>
        <td class="r" style="font-weight:600;padding:9px 12px;font-family:'IBM Plex Mono',monospace;font-size:12px">{org_avg_a:.2f}</td>
        <td style="text-align:right;font-family:'IBM Plex Mono',monospace;font-size:12px;color:var(--pur);font-weight:600;padding:9px 12px">{org_avg_ap:.2f}</td>
        <td colspan="2" style="padding:9px 12px;font-size:11px;color:var(--ink3);font-family:'IBM Plex Mono',monospace">
          org apprvs: {org_ap_d1} ({d1_fmt}) / {org_ap_d2} ({d2_fmt}) &middot; avg {(org_ap_d1+org_ap_d2)/2:.1f}/day
        </td>
      </tr>
    </tfoot>
  </table></div>

  <div class="callout-grid">
    <div class="callout-box red-box">
      <div class="callout-head">&#9888; Flagged (1-RED or 2-ORG) &mdash; {cnt[1]+cnt[2]} reps</div>
      {flagged}
    </div>
    <div class="callout-box grn-box">
      <div class="callout-head">&#10003; 5-GRN performers &mdash; {cnt[5]} reps</div>
      {greens}
    </div>
  </div>

  <div class="section-title">Supervisor team comparison</div>
  <div class="sup-grid">{c_card}{s_card}</div>

  <div class="section-title">Rolling data &mdash; {cr['window_label']}</div>
  <div class="rolling-grid">{c_roll}{s_roll}</div>

  <div class="footer">
    <span>Source: Zoho CRM &middot; Accounts + Submissions modules &middot; PC Activity Report v2</span>
    <span>Calls/dur: {d1_fmt} &amp; {d2_fmt} &middot; Accts: {acct_label} &middot; Rolling: {cr['window_label']}</span>
  </div>

</div>
</body>
</html>"""

# ── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    print("Getting Zoho access token...")
    token = get_access_token()

    # 2 most recent business days for calls/duration
    call_days = last_n_business_days(2)
    d1, d2 = call_days[0], call_days[1]
    print(f"Call/dur period: {d1} & {d2}")

    # 5 most recent business days for accounts
    acct_days = last_n_business_days(5)
    print(f"Account period: {acct_days[0]} to {acct_days[-1]}")

    print("Pulling calls (2 days)...")
    calls_d1 = pull_calls(token, d1)
    calls_d2 = pull_calls(token, d2)

    print("Pulling accounts (5 days)...")
    accts_by_day = {}
    for d in acct_days:
        accts_by_day[d] = pull_accounts_day(token, d)

    print("Pulling approvals (2 days)...")
    apprvs_d1 = pull_approvals_day(token, d1)
    apprvs_d2 = pull_approvals_day(token, d2)

    print("Pulling rolling data...")
    conlan_rolling = compute_rolling(token, CONLAN)
    stokoe_rolling = compute_rolling(token, STOKOE)

    # Build rows
    rows = []
    for last, name in sorted(REP_LAST_TO_FULL.items(), key=lambda x: x[1]):
        c1, dur1 = calls_d1[last]
        c2, dur2 = calls_d2[last]
        ac  = (c1 + c2) / 2
        ad  = (dur1 + dur2) / 2
        # 5-day account average
        adl = sum(accts_by_day[d].get(last, 0) for d in acct_days) / len(acct_days)
        ap1 = apprvs_d1.get(last, 0)
        ap2 = apprvs_d2.get(last, 0)
        apd = (ap1 + ap2) / 2
        pts, reason = score_v2(ac, ad, adl)
        rows.append({"name": name, "last": last,
                     "ac": ac, "ad": ad, "adl": adl, "apd": apd,
                     "pts": pts, "reason": reason})

    def team_stats(team_set):
        tr = [r for r in rows if r["last"] in team_set]
        n  = len(tr)
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
            "ap_d1":    sum(apprvs_d1.get(l, 0) for l in team_set),
            "ap_d2":    sum(apprvs_d2.get(l, 0) for l in team_set),
        }

    data = {
        "rows":           rows,
        "conlan_stats":   team_stats(CONLAN),
        "stokoe_stats":   team_stats(STOKOE),
        "conlan_rolling": conlan_rolling,
        "stokoe_rolling": stokoe_rolling,
        "org_ap_d1":      sum(apprvs_d1.values()),
        "org_ap_d2":      sum(apprvs_d2.values()),
    }

    print("Generating HTML...")
    html = generate_html(d1, d2, acct_days, data)

    with open("reportv2.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("Saved reportv2.html")

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
