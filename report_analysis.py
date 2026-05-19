"""
report_analysis.py
Shared analysis engine for all KurvPay PC rep reports.
Generates rich HTML talking points from live data.
"""

def generate_analysis(rows, prev_rows, team_stats_cur, team_stats_prev,
                      d1, d2, rolling_c, rolling_s, fmt_day,
                      REP_LAST_TO_FULL, CONLAN, STOKOE, scoring_system="1-3"):
    """
    Generate rich analysis HTML.

    rows          : current period [{name, last, ac, ad, adl, apd, pts, reason}]
    prev_rows     : previous period rows or None
    team_stats_cur: {"conlan": {...}, "stokoe": {...}}
    team_stats_prev: same for previous period or None
    d1, d2        : current period date strings
    rolling_c/s   : rolling dicts for conlan/stokoe
    scoring_system: "1-3" or "1-5"
    """

    n      = len(rows)
    d1_fmt = fmt_day(d1)
    d2_fmt = fmt_day(d2)

    # ── HELPERS ──────────────────────────────────────────────────────────────
    def pill(val, color="default"):
        STYLES = {
            "green":  "background:var(--grn-bg,#e6f7ef);color:var(--grn,#05764a)",
            "red":    "background:var(--red-bg,#fdf0f0);color:var(--red,#c0111a)",
            "purple": "background:var(--pur-bg,#ede9fe);color:var(--pur,#5b21b6)",
            "yellow": "background:var(--ylw-bg,#fef9e6);color:var(--ylw,#a86400)",
            "blue":   "background:#e0f2fe;color:#0e7490",
            "default":"background:var(--border2,#f0f0f0);color:var(--ink2,#444)",
        }
        st = STYLES.get(color, STYLES["default"])
        return (f'<span style="display:inline-block;font-family:\'IBM Plex Mono\',monospace;'
                f'font-size:11px;border-radius:4px;padding:1px 7px;margin:0 2px;{st}">{val}</span>')

    def tp(label, headline, body, lc="trend"):
        LABEL_COLORS = {
            "trend":    "color:var(--ink3,#888)",
            "concern":  "color:var(--red,#c0111a)",
            "positive": "color:var(--grn,#05764a)",
            "approval": "color:var(--pur,#5b21b6)",
            "team":     "color:#0e7490",
        }
        lcolor = LABEL_COLORS.get(lc, LABEL_COLORS["trend"])
        return (
            f'<div style="margin-bottom:20px;padding-bottom:20px;border-bottom:.5px solid var(--border2,#f0f0f0)">'
            f'<div style="font-family:\'IBM Plex Mono\',monospace;font-size:10px;text-transform:uppercase;'
            f'letter-spacing:.1em;{lcolor};margin-bottom:5px">{label}</div>'
            f'<div style="font-size:14px;font-weight:600;margin-bottom:6px;line-height:1.3;color:var(--ink,#0f0f0f)">{headline}</div>'
            f'<div style="font-size:13px;color:var(--ink2,#444);line-height:1.7">{body}</div>'
            f'</div>'
        )

    points = []

    # ── 1. SCORE DISTRIBUTION ────────────────────────────────────────────────
    if scoring_system == "1-3":
        grn = sum(1 for r in rows if r["pts"] == 1)
        ylw = sum(1 for r in rows if r["pts"] == 2)
        red = sum(1 for r in rows if r["pts"] == 3)
        body = (f"{pill(f'{grn} reps &mdash; 1-GRN', 'green')} "
                f"{pill(f'{ylw} reps &mdash; 2-YLW')} "
                f"{pill(f'{red} reps &mdash; 3-RED', 'red')} &mdash; "
                f"<strong>{grn*100//n}% of the org hit green</strong> this period.")
        if prev_rows:
            pg = sum(1 for r in prev_rows if r["pts"] == 1)
            pr = sum(1 for r in prev_rows if r["pts"] == 3)
            dg = grn - pg; dr = red - pr
            if dg != 0:
                body += f" 1-GRN {pill(f'▲{dg:+d} vs last period', 'green') if dg > 0 else pill(f'▼{dg:+d} vs last period', 'red')}."
            if dr != 0:
                body += f" 3-RED {pill(f'▲{dr:+d}', 'red') if dr > 0 else pill(f'▼{abs(dr)}', 'green')}."
    else:
        c5 = sum(1 for r in rows if r["pts"] == 5)
        c4 = sum(1 for r in rows if r["pts"] == 4)
        c3 = sum(1 for r in rows if r["pts"] == 3)
        c2 = sum(1 for r in rows if r["pts"] == 2)
        c1 = sum(1 for r in rows if r["pts"] == 1)
        top_pct = (c5 + c4) * 100 // n
        body = (f"{pill(f'{c5} at 5-GRN', 'green')} {pill(f'{c4} at 4-BLU', 'blue')} "
                f"{pill(f'{c3} at 3-YLW', 'yellow')} {pill(f'{c2} at 2-ORG')} "
                f"{pill(f'{c1} at 1-RED', 'red')} &mdash; "
                f"<strong>{top_pct}% in the top two tiers</strong> (4-BLU or 5-GRN).")
        if prev_rows:
            pc5 = sum(1 for r in prev_rows if r["pts"] == 5)
            pc4 = sum(1 for r in prev_rows if r["pts"] == 4)
            pc1 = sum(1 for r in prev_rows if r["pts"] == 1)
            dt = (c5+c4) - (pc5+pc4); dl = c1 - pc1
            if dt != 0:
                body += f" Top tier {pill(f'{dt:+d} vs last period', 'green' if dt > 0 else 'red')}."
            if dl != 0:
                body += f" 1-RED {pill(f'{dl:+d}', 'red' if dl > 0 else 'green')}."
    points.append(tp("Score distribution", "Score breakdown this period", body, "trend"))

    # ── 2. STAR PERFORMERS ────────────────────────────────────────────────────
    top5 = sorted(rows, key=lambda x: (-x["adl"], -x["ac"]))[:5]
    # Build rolling lookup from top3/bot3 lists
    roll_lookup = {}
    for name, val in rolling_c.get("top3",[]) + rolling_c.get("bot3",[]) + rolling_s.get("top3",[]) + rolling_s.get("bot3",[]):
        roll_lookup[name] = val

    star_body = ""
    for r in top5:
        roll = roll_lookup.get(r["name"])
        roll_str = (" &middot; rolling " + pill(f"{roll:.2f}/day")) if roll else ""
        vs_roll = ""
        if roll:
            delta = r["adl"] - roll
            if abs(delta) >= 0.3:
                vs_roll = " " + pill(f"{delta:+.2f} vs rolling", "green" if delta > 0 else "red")
        p_accts = pill(f"{r['adl']:.2f} accts", "green")
        p_calls = pill(f"{r['ac']:.0f} calls")
        p_dur   = pill(f"{r['ad']:.0f} min")
        star_body += f"&bull; <strong>{r['name']}</strong>: {p_accts} {p_calls} {p_dur}{roll_str}{vs_roll}<br>"
    headline = f"{top5[0]['name']} leads the org &mdash; {top5[0]['adl']:.2f} avg accts/day"
    points.append(tp("Star performers", headline, star_body, "positive"))

    # ── 3. LOW PERFORMERS ─────────────────────────────────────────────────────
    bot5 = sorted(rows, key=lambda x: (x["adl"], x["ac"]))[:5]
    low_body = ""
    for r in bot5:
        flags = []
        if r["ac"] > 70 and r["adl"] < 1.5:
            flags.append(pill("high calls, low conversion", "yellow"))
        if r["ac"] < 20:
            flags.append(pill("very low activity", "red"))
        if r["adl"] == 0 and r["ac"] > 0:
            flags.append(pill("0 accounts despite calls", "red"))
        flag_str = " ".join(flags)
        roll = roll_lookup.get(r["name"])
        roll_str = (" &middot; rolling " + pill(f"{roll:.2f}/day")) if roll else ""
        p_accts = pill(f"{r['adl']:.2f} accts", "red")
        p_calls = pill(f"{r['ac']:.0f} calls")
        p_dur   = pill(f"{r['ad']:.0f} min")
        low_body += f"&bull; <strong>{r['name']}</strong>: {p_accts} {p_calls} {p_dur}{roll_str} {flag_str}<br>"
    headline = f"{bot5[0]['name']} is lowest on accounts &mdash; {bot5[0]['adl']:.2f}/day avg"
    points.append(tp("Low performers", headline, low_body, "concern"))

    # ── 4. MOVERS VS PREVIOUS PERIOD ─────────────────────────────────────────
    if prev_rows:
        prev_map = {r["last"]: r for r in prev_rows}
        movers_up, movers_dn = [], []
        for r in rows:
            if r["last"] in prev_map:
                p = prev_map[r["last"]]
                da = r["adl"] - p["adl"]
                dp = r["pts"] - p["pts"]
                if da >= 0.5 or dp >= 1:
                    movers_up.append((r, da, dp, p))
                elif da <= -0.5 or dp <= -1:
                    movers_dn.append((r, da, dp, p))
        movers_up.sort(key=lambda x: -x[1])
        movers_dn.sort(key=lambda x: x[1])

        if movers_up or movers_dn:
            m_body = ""
            if movers_up:
                m_body += "<strong>Moving up vs last period:</strong><br>"
                for r, da, dp, prev_r in movers_up[:5]:
                    p_prev  = pill(f"{prev_r['adl']:.2f}")
                    p_cur   = pill(f"{r['adl']:.2f} accts", "green")
                    p_delta = pill(f"+{da:.2f}", "green")
                    pts_str = (" " + pill(f"▲{dp} pts", "green")) if dp > 0 else ""
                    m_body += f"&bull; <strong>{r['name']}</strong>: {p_prev} &rarr; {p_cur} {p_delta}{pts_str}<br>"
            if movers_dn:
                m_body += "<strong>Moving down vs last period:</strong><br>"
                for r, da, dp, prev_r in movers_dn[:5]:
                    p_prev  = pill(f"{prev_r['adl']:.2f}")
                    p_cur   = pill(f"{r['adl']:.2f} accts", "red")
                    p_delta = pill(f"{da:.2f}", "red")
                    pts_str = (" " + pill(f"▼{abs(dp)} pts", "red")) if dp < 0 else ""
                    m_body += f"&bull; <strong>{r['name']}</strong>: {p_prev} &rarr; {p_cur} {p_delta}{pts_str}<br>"
            headline = f"{movers_up[0][0]['name']} shows biggest improvement" if movers_up else f"{movers_dn[0][0]['name']} shows biggest drop"
            points.append(tp("Movers", headline, m_body, "trend"))

    # ── 5. VS 30-DAY ROLLING AVERAGE ─────────────────────────────────────────
    beating, lagging = [], []
    for r in rows:
        if r["name"] in roll_lookup:
            roll = roll_lookup[r["name"]]
            delta = r["adl"] - roll
            if delta >= 0.4:
                beating.append((r, delta, roll))
            elif delta <= -0.4:
                lagging.append((r, delta, roll))
    beating.sort(key=lambda x: -x[1])
    lagging.sort(key=lambda x: x[1])

    if beating or lagging:
        roll_body = ""
        if beating:
            roll_body += "<strong>Running above their 30-day rolling average:</strong><br>"
            for r, delta, roll in beating[:5]:
                p_cur   = pill(f"{r['adl']:.2f} this period", "green")
                p_roll  = pill(f"{roll:.2f} rolling")
                p_delta = pill(f"+{delta:.2f}", "green")
                roll_body += f"&bull; <strong>{r['name']}</strong>: {p_cur} vs {p_roll} {p_delta}<br>"
        if lagging:
            roll_body += "<strong>Running below their 30-day rolling average:</strong><br>"
            for r, delta, roll in lagging[:5]:
                p_cur   = pill(f"{r['adl']:.2f} this period", "red")
                p_roll  = pill(f"{roll:.2f} rolling")
                p_delta = pill(f"{delta:.2f}", "red")
                roll_body += f"&bull; <strong>{r['name']}</strong>: {p_cur} vs {p_roll} {p_delta}<br>"
        headline = f"{len(beating)} reps above rolling avg &middot; {len(lagging)} below"
        points.append(tp("Vs 30-day rolling", headline, roll_body, "trend"))

    # ── 6. EFFICIENCY GAP ────────────────────────────────────────────────────
    high_low = sorted([r for r in rows if r["ac"] > 65 and r["adl"] < 1.5], key=lambda x: -x["ac"])
    low_high = sorted([r for r in rows if r["ac"] < 45 and r["adl"] >= 2.0], key=lambda x: -x["adl"])

    if high_low or low_high:
        eff_body = ""
        if high_low:
            eff_body += "<strong>High calls, low accounts &mdash; pitch/qualifying may need review:</strong><br>"
            for r in high_low[:5]:
                rate = r["adl"] / r["ac"] * 100 if r["ac"] > 0 else 0
                p_calls = pill(f"{r['ac']:.0f} calls", "yellow")
                p_accts = pill(f"{r['adl']:.2f} accts", "red")
                p_rate  = pill(f"{rate:.1f}% conversion rate")
                eff_body += f"&bull; <strong>{r['name']}</strong>: {p_calls} &rarr; {p_accts} {p_rate}<br>"
        if low_high:
            eff_body += "<strong>High efficiency &mdash; fewer calls but strong output:</strong><br>"
            for r in low_high[:5]:
                rate = r["adl"] / r["ac"] * 100 if r["ac"] > 0 else 0
                p_calls = pill(f"{r['ac']:.0f} calls")
                p_accts = pill(f"{r['adl']:.2f} accts", "green")
                p_rate  = pill(f"{rate:.1f}% conversion", "green")
                eff_body += f"&bull; <strong>{r['name']}</strong>: {p_calls} &rarr; {p_accts} {p_rate}<br>"
        points.append(tp("Efficiency gap", "Activity vs output mismatches", eff_body, "concern"))

    # ── 7. APPROVALS ─────────────────────────────────────────────────────────
    reps_with_ap = [r for r in rows if r["apd"] > 0 and r["adl"] > 0]
    if reps_with_ap:
        rates = sorted([(r, r["apd"] / r["adl"]) for r in reps_with_ap], key=lambda x: -x[1])
        ap_body = "<strong>Top approval rates (approvals &divide; accounts):</strong><br>"
        for r, rate in rates[:6]:
            color = "green" if rate >= 0.6 else "yellow" if rate >= 0.3 else "default"
            p_ap    = pill(f"{r['apd']:.1f} apprvs", "purple")
            p_accts = pill(f"{r['adl']:.2f} accts")
            p_rate  = pill(f"{rate*100:.0f}%", color)
            ap_body += f"&bull; <strong>{r['name']}</strong>: {p_ap} / {p_accts} = {p_rate}<br>"
        zero_ap = [r for r in rows if r["apd"] == 0 and r["adl"] >= 1.5]
        if zero_ap:
            ap_body += "<strong>No approvals despite accounts (pipeline timing?):</strong><br>"
            for r in sorted(zero_ap, key=lambda x: -x["adl"])[:4]:
                p_accts = pill(f"{r['adl']:.2f} accts", "yellow")
                p_zero  = pill("0 apprvs", "red")
                ap_body += f"&bull; <strong>{r['name']}</strong>: {p_accts} / {p_zero}<br>"
        headline = f"{rates[0][0]['name']} leads on approval rate &mdash; {rates[0][1]*100:.0f}%"
        points.append(tp("Approvals", headline, ap_body, "approval"))

    # ── 8. TEAM HEAD-TO-HEAD ─────────────────────────────────────────────────
    cs = team_stats_cur["conlan"]
    ss = team_stats_cur["stokoe"]
    winner = "Brandon Conlan" if cs["avg_a"] >= ss["avg_a"] else "George Stokoe"
    loser  = "George Stokoe" if winner == "Brandon Conlan" else "Brandon Conlan"
    ws = cs if winner == "Brandon Conlan" else ss
    ls = ss if winner == "Brandon Conlan" else cs
    diff = abs(cs["avg_a"] - ss["avg_a"])

    def team_row(label, stats, scoring_system):
        if scoring_system == "1-3":
            pts_str = (pill(f"{stats['grn']} GRN", "green") + " " +
                       pill(f"{stats['ylw']} YLW", "yellow") + " " +
                       pill(f"{stats['red']} RED", "red"))
        else:
            top = stats.get("c5", 0) + stats.get("c4", 0)
            pts_str = (pill(f"{top} top tier", "green") + " " +
                       pill(f"{stats.get('c1',0)} RED", "red"))
        pct_color = "green" if stats["pct_goal"] >= 25 else "default"
        p_avg  = pill(f"{stats['avg_a']:.2f} accts/rep/day")
        p_goal = pill(f"{stats['pct_goal']:.1f}% at goal", pct_color)
        p_ap   = pill(f"{stats['avg_ap']:.2f} apprvs/rep/day", "purple")
        return f"<strong>{label}</strong>: {p_avg} &middot; {p_goal} &middot; {p_ap} &middot; {pts_str}<br>"

    team_body = team_row("Brandon Conlan", cs, scoring_system)
    team_body += team_row("George Stokoe", ss, scoring_system)
    team_body += f"<strong>{winner}'s team leads on accounts</strong> by {pill(f'+{diff:.2f}/rep/day', 'green')}."

    if team_stats_prev:
        pc = team_stats_prev["conlan"]
        ps = team_stats_prev["stokoe"]
        dc = cs["avg_a"] - pc["avg_a"]
        ds = ss["avg_a"] - ps["avg_a"]
        team_body += (f"<br>Trend vs last period: "
                      f"Conlan {pill(f'{dc:+.2f}', 'green' if dc >= 0 else 'red')} &middot; "
                      f"Stokoe {pill(f'{ds:+.2f}', 'green' if ds >= 0 else 'red')}.")

    headline = f"{winner}'s team leads on accounts ({ws['avg_a']:.2f} vs {ls['avg_a']:.2f} accts/rep/day)"
    points.append(tp("Team comparison", headline, team_body, "team"))

    # ── 9. ZERO / ABSENT ACTIVITY ────────────────────────────────────────────
    zero_calls = [r for r in rows if r["ac"] == 0]
    zero_accts = [r for r in rows if r["adl"] == 0 and r["ac"] > 20]

    if zero_calls or zero_accts:
        z_body = ""
        if zero_calls:
            z_body += "<strong>Zero calls logged this period:</strong> "
            z_body += ", ".join(f"<strong>{r['name']}</strong>" for r in zero_calls) + "<br>"
        if zero_accts:
            z_body += "<strong>Calls but zero accounts (conversion issue):</strong><br>"
            for r in sorted(zero_accts, key=lambda x: -x["ac"])[:5]:
                p_calls = pill(f"{r['ac']:.0f} calls", "yellow")
                p_zero  = pill("0 accounts", "red")
                z_body += f"&bull; <strong>{r['name']}</strong>: {p_calls} but {p_zero}<br>"
        headline = f"{len(zero_calls)} reps with zero calls &middot; {len(zero_accts)} with calls but no accounts"
        points.append(tp("Attention required", headline, z_body, "concern"))

    # ── ASSEMBLE ──────────────────────────────────────────────────────────────
    # Remove last divider
    if points:
        last = points[-1]
        points[-1] = last.replace(
            "border-bottom:.5px solid var(--border2,#f0f0f0)",
            "border-bottom:none"
        )

    return (
        f'<div style="background:var(--white);border:1px solid var(--border);'
        f'border-radius:12px;overflow:hidden;margin-bottom:1.5rem">'
        f'<div style="background:#f4f3ef;padding:12px 16px;font-size:13px;font-weight:600;'
        f'border-bottom:1px solid var(--border)">&#128200; Performance analysis &mdash; {d1_fmt} &amp; {d2_fmt}</div>'
        f'<div style="padding:16px">{"".join(points)}</div>'
        f'</div>'
    )
