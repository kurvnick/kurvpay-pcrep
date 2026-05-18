"""
KurvPay PC Rep Daily Report Generator
Pulls data from Zoho CRM, scores reps, generates HTML, uploads to tiiny.host
Run daily via GitHub Actions at 5am PT (12:00 UTC Mon-Fri)
"""

import os
import sys
import json
import requests
from datetime import date, timedelta
from collections import defaultdict

# ── CONFIG (set as GitHub Secrets, never hardcode) ──────────────────────────
ZOHO_CLIENT_ID     = os.environ["ZOHO_CLIENT_ID"]
ZOHO_CLIENT_SECRET = os.environ["ZOHO_CLIENT_SECRET"]
ZOHO_REFRESH_TOKEN = os.environ["ZOHO_REFRESH_TOKEN"]
TIINY_API_KEY      = os.environ["TIINY_API_KEY"]
TIINY_DOMAIN       = "paymentcloudcallstats-inday"  # subdomain only, no .tiiny.site

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

GOALS = {"calls": 125, "duration": 120, "accounts": 3}

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
    """Run a COQL query, paginating automatically."""
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    records = []
    offset = 0
    while True:
        paginated = query.rstrip() + f" LIMIT 200 OFFSET {offset}"
        r = requests.post(
            "https://www.zohoapis.com/crm/v2/coql",
            headers=headers,
            json={"select_query": paginated},
        )
        if r.status_code == 204:
            break
        data = r.json()
        if "data" not in data or not data["data"]:
            break
        records.extend(data["data"])
        if not data.get("info", {}).get("more_records"):
            break
        offset += 200
    return records

# ── DATA PULLS ───────────────────────────────────────────────────────────────
def pull_calls(token, day_str):
    """Returns {last_name: (call_count, duration_minutes)}"""
    next_day = (date.fromisoformat(day_str) + timedelta(days=1)).isoformat()
    records = zoho_coql(token,
        f"SELECT Owner, Call_Duration_in_seconds FROM Calls "
        f"WHERE Call_Start_Time >= '{day_str}T00:00:00-07:00' "
        f"AND Call_Start_Time < '{next_day}T00:00:00-07:00' "
        f"AND Call_Type != 'Missed'"
    )
    counts = defaultdict(int)
    secs   = defaultdict(int)
    for r in records:
        owner = r.get("Owner", {}).get("name", "")
        if owner in ALL_REPS:
            counts[owner] += 1
            secs[owner]   += (r.get("Call_Duration_in_seconds") or 0)
    return {last: (counts[last], round(secs[last] / 60, 1)) for last in ALL_REPS}

def pull_accounts(token, day_str):
    """Returns {last_name: count}"""
    next_day = (date.fromisoformat(day_str) + timedelta(days=1)).isoformat()
    records = zoho_coql(token,
        f"SELECT Owner FROM Accounts "
        f"WHERE Created_Time >= '{day_str}T00:00:00-07:00' "
        f"AND Created_Time < '{next_day}T00:00:00-07:00'"
    )
    counts = defaultdict(int)
    for r in records:
        owner = r.get("Owner", {}).get("name", "")
        if owner in ALL_REPS:
            counts[owner] += 1
    return dict(counts)

def pull_approvals(token, day_str):
    """Returns {last_name: count} for all approval stages."""
    counts = defaultdict(int)
    for stage in APPROVAL_STAGES:
        records = zoho_coql(token,
            f"SELECT Owner, Closing_Date FROM Deals "
            f"WHERE Stage = '{stage}' AND Closing_Date = '{day_str}'"
        )
        for r in records:
            owner = r.get("Owner", {}).get("name", "")
            if owner in ALL_REPS:
                counts[owner] += 1
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

# ── REPORT DATES ─────────────────────────────────────────────────────────────
def today_str():
    """Returns today as ISO string."""
    return str(date.today())

# ── HTML GENERATION ──────────────────────────────────────────────────────────
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

def badge(pts):
    cls = {1:"grn",2:"ylw",3:"red"}[pts]
    label = {1:"1-GRN",2:"2-YLW",3:"3-RED"}[pts]
    return f'<span class="badge badge-{cls}">{label}</span>'

def row_cls(pts):
    return {1:"row-grn",2:"row-ylw",3:"row-red"}[pts]

def fmt_day(d):
    dt = date.fromisoformat(d)
    return dt.strftime("%a %b %-d").replace(" 0"," ") if sys.platform != "win32" \
        else dt.strftime("%a %b %d").lstrip("0")

def sup_card_inday(name, team_size, avg_a, avg_ap, pct_goal, grn, ylw, red, tot_ap, d1):
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


# ── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    print("Getting Zoho access token...")
    token = get_access_token()

    d1 = today_str()
    print(f"Report date: {d1} (today)")

    print("Pulling calls...")
    calls_d1 = pull_calls(token, d1)

    print("Pulling accounts...")
    accts_d1 = pull_accounts(token, d1)

    print("Pulling approvals...")
    apprvs_d1 = pull_approvals(token, d1)

    print("Pulling rolling data...")
    conlan_rolling = compute_rolling(token, CONLAN)
    stokoe_rolling = compute_rolling(token, STOKOE)

    # Build rows — single day, no averaging
    rows = []
    for last, name in sorted(REP_LAST_TO_FULL.items(), key=lambda x: x[1]):
        c1, dur1 = calls_d1[last]
        a1 = accts_d1.get(last, 0)
        ap1 = apprvs_d1.get(last, 0)
        pts, reason = score(c1, dur1, a1)
        rows.append({"name": name, "last": last,
                     "ac": c1, "ad": dur1, "adl": a1, "apd": ap1,
                     "pts": pts, "reason": reason})

    # Team stats
    def team_stats(team_set, rows, d1_apprvs):
        tr = [r for r in rows if r["last"] in team_set]
        n = len(tr)
        return {
            "n": n,
            "grn": sum(r["pts"]==1 for r in tr),
            "ylw": sum(r["pts"]==2 for r in tr),
            "red": sum(r["pts"]==3 for r in tr),
            "avg_a":  sum(r["adl"] for r in tr) / n,
            "avg_ap": sum(r["apd"] for r in tr) / n,
            "pct_goal": sum(r["adl"] >= 3 for r in tr) / n * 100,
            "tot_ap_d1": sum(d1_apprvs.get(l, 0) for l in team_set),
        }

    conlan_stats = team_stats(CONLAN, rows, apprvs_d1)
    stokoe_stats = team_stats(STOKOE, rows, apprvs_d1)

    data = {
        "rows": rows,
        "conlan_stats": conlan_stats,
        "stokoe_stats": stokoe_stats,
        "conlan_rolling": conlan_rolling,
        "stokoe_rolling": stokoe_rolling,
        "org_tot_ap_d1": sum(apprvs_d1.values()),
    }

    print("Generating HTML...")
    html = generate_html(d1, data)

    with open("inday_report.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("Saved inday_report.html")

    print("Uploading to tiiny.host...")
    import io, zipfile
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("index.html", html.encode("utf-8"))
    zip_buffer.seek(0)

    r = requests.put(
        "https://ext.tiiny.host/v1/upload",
        headers={"x-api-key": TIINY_API_KEY},
        files={"files": ("report.zip", zip_buffer, "application/zip")},
        data={"domain": f"{TIINY_DOMAIN}.tiiny.site"},
    )
    if r.status_code == 200:
        print(f"✅ Report live at https://{TIINY_DOMAIN}.tiiny.site/")
    else:
        print(f"❌ tiiny upload failed: {r.status_code} {r.text}")
        sys.exit(1)

if __name__ == "__main__":
    main()
