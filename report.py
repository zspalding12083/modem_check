"""HTML report for modem_check.py data. Factual wording only: what was logged and measured."""
import base64
import datetime as dt
import html
import io
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.patches  # noqa: E402
import matplotlib.ticker  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

from modem_check import HERE, open_db, short_event_text, now_local  # noqa: E402

# Reference palette (dataviz skill, light mode) - categorical slots in fixed order.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
INK, INK2, MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#898781", "#e4e3df", "#fcfcfb"
SHADE = "#d03b3b"  # status "critical", used only for the below-threshold upload periods

KEY_EVENTS = [
    ("T3 time-out (No Ranging Response)", r"T3 time-out"),
    ("T4 time-out", r"T4 time-out"),
    ("Config file rejected (TLV 11)", r"TLV 11|TLV-11"),
    ("RNG-RSP commanded power (DRW)", r"RNG-RSP CCAP Commanded Power"),
    ("CM-STATUS message sent", r"CM-STATUS message sent"),
    ("DS profile assignment change", r"DS profile assignment change"),
    ("Cable Modem Reboot", r"Cable Modem Reboot|Reboot"),
    ("Registration complete", r"REGISTRATION COMPLETE"),
    ("DHCP failures", r"DHCP"),
    ("Partial service", r"Partial Service"),
]


def _parse_ts(s):
    return dt.datetime.fromisoformat(s) if s else None


def _local_naive(s):
    """DB timestamps carry the local offset; charts use naive local time."""
    t = _parse_ts(s)
    return t.astimezone().replace(tzinfo=None) if t and t.tzinfo else t


def _parse_bound(s, default):
    if not s:
        return default
    t = dt.datetime.fromisoformat(s)
    return t.replace(tzinfo=None) if t.tzinfo else t


def esc(x):
    return html.escape("" if x is None else str(x))


def _f(x, nd=2):
    return "" if x is None else f"{x:.{nd}f}"


def fmt_t(t):
    return t.strftime("%a %b %d %H:%M") if t else ""


def _xnum(x):
    return mdates.date2num(x) if isinstance(x, dt.datetime) else x


def fig_to_img(fig, alt, hits=()):
    """PNG of the figure, plus invisible hover/tap targets over the marks.

    hits: ("pt", ax, x, y, text) for a dot, ("rect", ax, x0, y0, x1, y1, text) for a bar/cell (data coords).
    Positions are % of the saved image, computed from the same tight bbox savefig uses (pad 0.1 in)."""
    fig.canvas.draw()
    box = fig.get_tightbbox(fig.canvas.get_renderer()).padded(0.1)

    def frac(ax, x, y):
        dx, dy = ax.transData.transform((_xnum(x), y)) / fig.dpi
        return (dx - box.x0) / box.width * 100, (box.y1 - dy) / box.height * 100

    spans = []
    for h in hits:
        if h[0] == "pt":
            _, ax, x, y, text = h
            left, top = frac(ax, x, y)
            style = f"left:{left:.2f}%;top:{top:.2f}%"
            cls = "hit pt"
        else:
            _, ax, x0, y0, x1, y1, text = h
            l0, t0 = frac(ax, x0, y1)
            l1, t1 = frac(ax, x1, y0)
            style = f"left:{l0:.2f}%;top:{t0:.2f}%;width:{l1 - l0:.2f}%;height:{t1 - t0:.2f}%"
            left, cls = l0, "hit rect"
        if left > 65:
            cls += " r"  # open the tooltip leftwards near the right edge
        spans.append(f'<span class="{cls}" style="{style}" tabindex="0" data-tip="{esc(text)}"></span>')

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=SURFACE)
    plt.close(fig)
    img = f'<img alt="{esc(alt)}" src="data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}">'
    return f'<figure class="chart">{img}{"".join(spans)}</figure>' if spans else img


def tip_time(t):
    return t.strftime("%a %b %d %H:%M")


def new_ax(title, ylabel, height=2.8):
    fig, ax = plt.subplots(figsize=(10, height))
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    ax.set_title(title, loc="left", fontsize=11, color=INK, fontweight="bold", pad=8)
    ax.set_ylabel(ylabel, color=INK2, fontsize=9)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=8)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    return fig, ax


def time_axis(ax, start, end):
    ax.set_xlim(start, end)
    loc = mdates.AutoDateLocator(minticks=4, maxticks=10)
    ax.xaxis.set_major_locator(loc)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))


def shade_periods(ax, periods, end):
    for p in periods:
        ax.axvspan(p["start"], p["end"] or end, color=SHADE, alpha=0.12, linewidth=0)


# ---------------------------------------------------------------------------

def load(db, since, until):
    s, u = since.isoformat(sep=" "), until.isoformat(sep=" ")

    def rows(sql, *a):
        return [dict(r) for r in db.execute(sql, a)]

    data = {}
    polls = rows("SELECT * FROM polls ORDER BY ts")
    for p in polls:
        p["t"] = _local_naive(p["ts"])
    data["all_polls"] = polls
    data["polls"] = [p for p in polls if since <= p["t"] <= until]
    ids = [p["id"] for p in data["polls"] if p["ok"]]
    q = ",".join("?" * len(ids)) or "NULL"
    data["downstream"] = rows(f"SELECT * FROM downstream WHERE poll_id IN ({q})", *ids)
    data["upstream"] = rows(f"SELECT * FROM upstream WHERE poll_id IN ({q})", *ids)
    for name in ("speedtests", "pings", "notes"):
        data[name] = [r for r in rows(f"SELECT * FROM {name} ORDER BY ts")
                      if since <= _local_naive(r["ts"]) <= until]
        for r in data[name]:
            r["t"] = _local_naive(r["ts"])
    data["events"] = rows("SELECT * FROM events ORDER BY id")
    return data


def clock_offset_for(polls, when=None):
    """Seconds to add to modem clock to get this computer's local time (from the nearest poll)."""
    ok = [p for p in polls if p["clock_offset_s"] is not None]
    if not ok:
        return None
    if when is None:
        return ok[-1]["clock_offset_s"]
    return min(ok, key=lambda p: abs((p["t"] - when).total_seconds()))["clock_offset_s"]


def attach_event_local_times(events, polls):
    for e in events:
        mt = _parse_ts(e["time"])
        e["modem_t"] = mt
        off = clock_offset_for(polls, mt) if mt else None
        e["t"] = mt + dt.timedelta(seconds=off) if (mt and off is not None) else None
        e["short"] = short_event_text(e["text"])


def upload_periods(speedtests, threshold):
    """Consecutive speed tests with upload below threshold -> periods."""
    periods, cur = [], None
    for s in speedtests:
        bad = s["up_mbps"] is None or s["up_mbps"] < threshold
        if bad and cur is None:
            cur = {"start": s["t"], "end": None, "tests": 0, "min_up": s["up_mbps"]}
        if bad:
            cur["tests"] += 1
            cur["min_up"] = min(x for x in (cur["min_up"], s["up_mbps"]) if x is not None) \
                if s["up_mbps"] is not None else cur["min_up"]
        elif cur is not None:
            cur["end"] = s["t"]
            periods.append(cur)
            cur = None
    if cur:
        periods.append(cur)
    return periods


def run_reason(trigger):
    if not trigger or trigger == "timer":
        return "scheduled"
    if trigger.startswith("ping"):
        return f"triggered by {trigger}"
    return trigger


def ping_problem(p, cfg):
    return (p["received"] < p["sent"] or bool(p["error"])
            or (p["rtt_avg"] or 0) > cfg["tests"].getfloat("ping_trigger_ms"))


def ping_loss_periods(pings):
    """Consecutive ping tests (any target) that lost packets -> periods."""
    periods, cur = [], None
    for p in sorted(pings, key=lambda x: x["t"]):
        bad = p["received"] < p["sent"]
        if bad:
            if cur is None:
                cur = {"start": p["t"], "end": None, "tests": 0, "lost": 0, "sent": 0}
            cur["tests"] += 1
            cur["lost"] += p["sent"] - p["received"]
            cur["sent"] += p["sent"]
        elif cur is not None:
            cur["end"] = p["t"]
            periods.append(cur)
            cur = None
    if cur:
        periods.append(cur)
    return periods


def find_outages(speedtests, pings, upload_mbps, loss_pct):
    """Outage = upload test below the threshold, or a ping test losing >= loss_pct of packets.
    Tests are discrete, so each outage carries bounds: it began after the last passing test of that kind
    and before the first failing one, and ended after the last failing test and before the next passing one."""
    def spans(tests, is_bad, kind):
        out, cur, last_good = [], None, None
        for x in sorted(tests, key=lambda x: x["t"]):
            if is_bad(x):
                if cur is None:
                    cur = {"last_good": last_good, "first_bad": x["t"], "last_bad": x["t"], "next_good": None,
                           "bad": []}
                cur["last_bad"] = x["t"]
                cur["bad"].append((kind, x))
            else:
                if cur is not None:
                    cur["next_good"] = x["t"]
                    out.append(cur)
                    cur = None
                last_good = x["t"]
        if cur:
            out.append(cur)
        return out

    found = spans(speedtests, lambda s: s["up_mbps"] is None or s["up_mbps"] < upload_mbps, "upload")
    found += spans(pings, lambda p: p["sent"] and (p["sent"] - p["received"]) * 100 / p["sent"] >= loss_pct, "ping")
    found.sort(key=lambda o: o["first_bad"])
    merged = []
    for o in found:
        prev = merged[-1] if merged else None
        prev_end = prev and (prev["next_good"] or dt.datetime.max)
        if prev and o["first_bad"] <= prev_end:
            if o["last_bad"] > prev["last_bad"]:
                prev["last_bad"], prev["next_good"] = o["last_bad"], o["next_good"]
            elif o["last_bad"] == prev["last_bad"] and o["next_good"] is None:
                prev["next_good"] = None
            prev["bad"] += o["bad"]
        else:
            merged.append(dict(o))
    return merged


def fmt_pair(a, b):
    """'Sat Oct 10 13:00 and 13:31', repeating the date only when it differs."""
    return f"{fmt_t(a)} and {b:%H:%M}" if a.date() == b.date() else f"{fmt_t(a)} and {fmt_t(b)}"


def fmt_dur(td):
    m = int(round(td.total_seconds() / 60))
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"


def outage_snapshot(o, data, events, deltas, cfg):
    """Modem readings taken during the outage compared with the last reading before it. Returns kv rows."""
    rg = cfg["ranges"]
    p_step, snr_drop = rg.getfloat("power_change_db"), rg.getfloat("snr_drop_db")
    lo = o["first_bad"] - dt.timedelta(minutes=3)
    hi = o["last_bad"] + dt.timedelta(minutes=3)
    if o["next_good"]:
        hi = min(hi, o["next_good"])
    ok = [p for p in data["all_polls"] if p["ok"]]
    during = [p for p in ok if lo <= p["t"] <= hi]
    before = next((p for p in reversed(ok) if p["t"] < lo), None)
    rows = []
    if not during:
        rows.append(["Modem readings", "None taken during this outage (it ended before the next full run)."])
    else:
        rows.append(["Modem readings", f"{len(during)} taken during the outage ("
                     + ", ".join(p["t"].strftime("%H:%M") for p in during[:12]) + ("…" if len(during) > 12 else "")
                     + ")" + (f", compared with the last reading before it at {fmt_t(before['t'])}" if before else "")
                     + "."])
    ids = {p["id"] for p in during} | ({before["id"]} if before else set())
    ds, us = defaultdict(dict), defaultdict(dict)
    for r in data["downstream"]:
        if r["poll_id"] in ids:
            ds[r["poll_id"]][(r["kind"], r["chid"])] = r
    for r in data["upstream"]:
        if r["poll_id"] in ids:
            us[r["poll_id"]][(r["kind"], r["chid"])] = r

    if during:
        # status
        statuses = sorted({p["cm_status"] for p in during if p["cm_status"]})
        st = f"Modem status: {', '.join(statuses)}"
        ups = [p["uptime_s"] for p in ([before] if before else []) + during if p["uptime_s"] is not None]
        st += "; modem restarted during the outage" if any(b < a for a, b in zip(ups, ups[1:])) else "; no modem restart"
        rows.append(["Status", esc(st) + "."])

        # downstream
        base = ds.get(before["id"], {}) if before else {}
        keys = sorted({k for p in during for k in ds.get(p["id"], {})} | set(base), key=lambda k: (k[0] != "SC-QAM", k[1]))
        name = lambda k: f"{k[0]} {k[1]}"
        notes = []
        unlocked = sorted({name(k) for p in during for k, r in ds.get(p["id"], {}).items() if not r["locked"]})
        missing = sorted({name(k) for k in base for p in during if k not in ds.get(p["id"], {})})
        if unlocked:
            notes.append("Not locked: " + ", ".join(unlocked))
        if missing:
            notes.append("Not listed by the modem (were before): " + ", ".join(missing))
        times = {p["t"] for p in during}
        added = {}
        dropped = set()
        for k, v in deltas.items():
            for x in v:
                if x["t"] in times:
                    if x["uncorrectable"] == COUNTER_DROP:
                        dropped.add(k)
                    elif x["uncorrectable"]:
                        added[k] = added.get(k, 0) + x["uncorrectable"]
        if added:
            notes.append("Added uncorrectable codewords: " + ", ".join(
                f"{name(k)} +{n:,}" for k, n in sorted(added.items(), key=lambda kv: (kv[0][0] != "SC-QAM", kv[0][1]))))
        if dropped:
            notes.append("Uncorrectable counter reset (no modem restart): " + ", ".join(name(k) for k in sorted(dropped)))
        changes, outside = [], []
        for k in keys:
            vals = [ds[p["id"]][k] for p in during if k in ds.get(p["id"], {})]
            if not vals:
                continue
            for f, unit, label in (("snr_db", "dB", "SNR/MER"), ("power_dbmv", "dBmV", "power")):
                cur = [r[f] for r in vals if r[f] is not None]
                if not cur:
                    continue
                if k in base and base[k][f] is not None:
                    b0 = base[k][f]
                    if f == "snr_db":
                        worst = min(cur)
                        if b0 - worst >= snr_drop:
                            changes.append(f"{name(k)} {label} {b0:g} → {worst:g} {unit}")
                    else:
                        worst = max(cur, key=lambda v: abs(v - b0))
                        if abs(worst - b0) >= p_step:
                            changes.append(f"{name(k)} {label} {b0:g} → {worst:g} {unit}")
                if f == "power_dbmv" and (min(cur) < rg.getfloat("ds_power_min") or max(cur) > rg.getfloat("ds_power_max")):
                    outside.append(f"{name(k)} power {min(cur):g}..{max(cur):g} dBmV")
                if f == "snr_db" and k[0] == "SC-QAM" and min(cur) < rg.getfloat("ds_snr_min"):
                    outside.append(f"{name(k)} SNR {min(cur):g} dB")
        if changes:
            notes.append(f"Changed from before (SNR/MER down {snr_drop:g} dB or more, power {p_step:g} dB or more): "
                         + ", ".join(changes))
        if outside:
            notes.append(f"Outside reference range (power {rg['ds_power_min']} to {rg['ds_power_max']} dBmV, "
                         f"SC-QAM SNR ≥ {rg['ds_snr_min']} dB): " + ", ".join(outside))
        n_ch = len({k for p in during for k in ds.get(p["id"], {})})
        rows.append(["Downstream", esc("; ".join(notes) + ".") if notes else esc(
            f"No differences found: all {n_ch} channels locked, no uncorrectable codewords added, "
            f"no SNR/MER drop of {snr_drop:g} dB or more, power within {p_step:g} dB of the earlier reading, "
            "and all inside the reference range.")])

        # upstream: always list values, they are few
        ubase = us.get(before["id"], {}) if before else {}
        ukeys = sorted({k for p in during for k in us.get(p["id"], {})} | set(ubase), key=lambda k: k[1])
        parts, flags = [], []
        for k in ukeys:
            cur = [us[p["id"]][k]["power_dbmv"] for p in during if k in us.get(p["id"], {})
                   and us[p["id"]][k]["power_dbmv"] is not None]
            unl = [p["t"] for p in during if k in us.get(p["id"], {}) and not us[p["id"]][k]["locked"]]
            rng = (f"{min(cur):g}" if min(cur) == max(cur) else f"{min(cur):g}–{max(cur):g}") if cur else "not listed"
            b = f" (before {ubase[k]['power_dbmv']:g})" if k in ubase and ubase[k]["power_dbmv"] is not None else ""
            unl_txt = ""
            if unl:
                n_with = sum(1 for p in during if k in us.get(p["id"], {}))
                unl_txt = (f", NOT LOCKED in {len(unl)} of {n_with} readings "
                           f"({unl[0]:%H:%M}" + (f" to {unl[-1]:%H:%M}" if len(unl) > 1 else "") + ")")
            parts.append(f"ch {k[1]} ({(us[during[0]['id']].get(k) or ubase.get(k) or {}).get('freq_hz', 0) / 1e6:.1f} MHz): "
                         f"{rng} dBmV{b}{unl_txt}")
            if cur and (min(cur) < rg.getfloat("us_power_min") or max(cur) > rg.getfloat("us_power_max")):
                flags.append(f"ch {k[1]}")
            if k in ubase and not cur:
                flags.append(f"ch {k[1]} missing")
        counts = sorted({sum(1 for r in us.get(p["id"], {}).values() if r["locked"]) for p in during})
        txt = (f"{' or '.join(map(str, counts))} channel(s) locked per reading"
               + (f" (before: {sum(1 for r in ubase.values() if r['locked'])})" if ubase else "")
               + ". Transmit power: " + "; ".join(parts) + ".")
        if flags:
            txt += (f" Outside reference range {rg['us_power_min']}–{rg['us_power_max']} dBmV or missing: "
                    + ", ".join(flags) + ".")
        rows.append(["Upstream", esc(txt)])

    # modem log entries in the outage window
    wlo = o["last_good"] or lo
    whi = o["next_good"] or hi
    win = [e for e in events if e["t"] and wlo <= e["t"] <= whi]
    if win:
        c = Counter()
        for e in win:
            m = re.search(r"Event Type Code: (\d+); Chan ID: ([\w/]+)", e["text"])
            if "T3 time-out" in e["text"]:
                c["T3 time-out"] += 1
            elif m:
                c[f"CM-STATUS code {m[1]} ch {m[2]}"] += 1
            elif "profile assignment change" in e["text"]:
                c["DS profile change " + (re.search(r"DS Chan ID: (\d+)", e["text"]) or [None, "?"])[1]] += 1
            else:
                c[e["short"][:80]] += 1
        rows.append(["Modem log", esc("; ".join(f"{k} ×{n}" if n > 1 else k for k, n in c.most_common()))])
    else:
        rows.append(["Modem log", "No entries in this window."])
    return rows


def outages_html(outages, events, cfg, data=None, deltas=None):
    t = cfg["tests"]
    up, loss = t.getfloat("upload_outage_mbps"), t.getfloat("ping_outage_loss_pct")
    head = (f"<h2>Outages</h2><p class='muted'>Counted as an outage: an upload test below {up:g} Mbps, or a ping test "
            f"that lost {loss:g}% or more of its packets. Tests run at intervals, so each start and end is a "
            "window between the last passing and the first failing test. Outages written down by hand are in the "
            "Timeline.</p>")
    if not outages:
        return head + "<p>None measured in this range.</p>"
    rows = []
    total_min, total_max, open_ended = dt.timedelta(), dt.timedelta(), False
    for o in outages:
        total_min += o["last_bad"] - o["first_bad"]
        if o["last_good"] and o["next_good"]:
            total_max += o["next_good"] - o["last_good"]
        else:
            total_max += o["last_bad"] - o["first_bad"]
            open_ended = True
        start = (f"between {fmt_pair(o['last_good'], o['first_bad'])}" if o["last_good"]
                 else f"by {fmt_t(o['first_bad'])}")
        if o["next_good"]:
            end = f"between {fmt_pair(o['last_bad'], o['next_good'])}"
            shortest = o["last_bad"] - o["first_bad"]
            if o["last_good"]:
                longest = o["next_good"] - o["last_good"]
                length = (f"{fmt_dur(shortest)} to {fmt_dur(longest)}" if shortest.total_seconds() >= 60
                          else f"up to {fmt_dur(longest)}")
            else:
                length = f"at least {fmt_dur(shortest)}"
        else:
            end = "not yet over at the last test"
            length = f"at least {fmt_dur(o['last_bad'] - o['first_bad'])} so far"
        ups = [x for k, x in o["bad"] if k == "upload"]
        pings = [x for k, x in o["bad"] if k == "ping"]
        lo = o["last_good"] or o["first_bad"]
        hi = o["next_good"] or dt.datetime.max
        t3 = sum(1 for e in events if e["t"] and "T3 time-out" in e["text"] and lo <= e["t"] <= hi)
        ev = []
        if ups:
            ev.append(f"{len(ups)} upload test(s) below {up:g} Mbps (lowest "
                      f"{min(x['up_mbps'] or 0 for x in ups):.2f} Mbps)")
        if pings:
            ev.append(f"{len(pings)} ping test(s) losing {loss:g}%+ of packets "
                      f"({sum(x['sent'] - x['received'] for x in pings)} of {sum(x['sent'] for x in pings)} lost)")
        ev.append(f"{t3} T3 time-out(s) in the modem log in this window")
        rows.append([str(len(rows) + 1), esc(start), esc(end), esc(length), esc("; ".join(ev))])
    total = (f"at least {fmt_dur(total_min)}" if open_ended and total_max == total_min
             else f"{fmt_dur(total_min)} to {fmt_dur(total_max)}" + (" or more" if open_ended else ""))
    summary = (f"<p><strong>{len(outages)} outage(s) measured in this range, {total} in total.</strong></p>"
               "<p class='muted'>Total = the sum of the Length column: shortest possible (first to last failing "
               "test) to longest possible (last passing test before to first passing test after).</p>")
    html_ = head + summary + table(["#", "Started", "Ended", "Length", "Measurements"], rows)
    if data is not None:
        html_ += ("<p class='muted'>Conditions during each outage: modem readings taken during the outage compared "
                  "with the last reading before it. Reference ranges are commonly published guidance for DOCSIS "
                  "cable modems.</p>")
        for i, o in enumerate(outages, 1):
            html_ += f"<h3>Outage {i}: conditions during the outage</h3>" + table(
                ["", ""], outage_snapshot(o, data, events, deltas, cfg), "kv")
    return html_


def detect_reboots(polls):
    out, prev = [], None
    for p in polls:
        if not p["ok"] or p["uptime_s"] is None:
            continue
        if prev is not None and p["uptime_s"] < prev["uptime_s"]:
            out.append({"t": p["t"] - dt.timedelta(seconds=p["uptime_s"]), "seen": p["t"]})
        prev = p
    return out


COUNTER_DROP = -1  # counter went down between polls without a modem restart: increase can't be computed


def codeword_deltas(polls, downstream):
    """{(kind, chid): [{"t", "prev_t", "corrected", "uncorrectable"}]} - increase since the previous poll.
    After a modem restart the counters start from zero, so the increase is the current value.
    Intervals where the channel was not locked or had no SNR (at either end) are left out (None): the modem
    keeps counting failed decodes on a channel it cannot receive, e.g. +11.8M on OFDM 194 during the
    2026-10-13 outage, which is not a meaningful error count."""
    def no_signal(r):
        return r is not None and (not r["locked"] or not r["snr_db"])

    by_poll = defaultdict(dict)
    for r in downstream:
        by_poll[r["poll_id"]][(r["kind"], r["chid"])] = r
    out = defaultdict(list)
    prev_poll = None
    for p in polls:
        if not p["ok"] or p["id"] not in by_poll:
            continue
        if prev_poll is not None:
            reset = (p["uptime_s"] or 0) < (prev_poll["uptime_s"] or 0)
            for key, r in by_poll[p["id"]].items():
                old = by_poll[prev_poll["id"]].get(key)
                entry = {"t": p["t"], "prev_t": prev_poll["t"]}
                unusable = no_signal(r) or (not reset and no_signal(old))
                for f in ("corrected", "uncorrectable"):
                    cur = r[f]
                    if unusable or cur is None or (not reset and (old is None or old[f] is None)):
                        entry[f] = None
                    elif reset:
                        entry[f] = cur
                    elif cur < old[f]:
                        entry[f] = COUNTER_DROP
                    else:
                        entry[f] = cur - old[f]
                out[key].append(entry)
        prev_poll = p
    return out


# ---------------------------------------------------------------------------
# Charts

def chart_speed(speedtests, periods, start, end, field, title, threshold=None):
    fig, ax = new_ax(title, "Mbps")
    pts = [(s["t"], s[field]) for s in speedtests if s[field] is not None]
    label = "Upload" if field == "up_mbps" else "Download"
    hits = [("pt", ax, s["t"], s[field], f"{tip_time(s['t'])}\n{label}: {s[field]:.2f} Mbps"
             + (f"\n{s['error']}" if s["error"] else "")) for s in speedtests if s[field] is not None]
    shade_periods(ax, periods, end)
    if pts:
        ax.plot([p[0] for p in pts], [p[1] for p in pts], color=SERIES[0], linewidth=2,
                marker="o", markersize=4, markeredgecolor=SURFACE, markeredgewidth=1)
    if threshold is not None:
        ax.axhline(threshold, color=MUTED, linewidth=1, linestyle="--")
        ax.annotate(f"{threshold:g} Mbps threshold", (end, threshold), xytext=(-4, 3),
                    textcoords="offset points", ha="right", fontsize=8, color=INK2)
    ax.set_ylim(bottom=0)
    time_axis(ax, start, end)
    return fig_to_img(fig, title, hits)


def chart_ping(pings, periods, start, end):
    fig, ax = new_ax("Ping round-trip time (average per test)", "ms")
    shade_periods(ax, periods, end)
    targets = sorted({p["target"] for p in pings})
    hits = [("pt", ax, p["t"], p["rtt_avg"] if p["rtt_avg"] is not None else 0,
             f"{tip_time(p['t'])}\n{p['target']}: {p['received']}/{p['sent']} received ({p['loss_pct']:g}% loss)"
             + (f"\nRTT min/avg/max {p['rtt_min']:.1f} / {p['rtt_avg']:.1f} / {p['rtt_max']:.1f} ms"
                if p["rtt_avg"] is not None else "")
             + (f"\n{p['error']}" if p["error"] else "")) for p in pings]
    for i, tgt in enumerate(targets):
        pts = [(p["t"], p["rtt_avg"]) for p in pings if p["target"] == tgt and p["rtt_avg"] is not None]
        if pts:
            ax.plot([x[0] for x in pts], [x[1] for x in pts], color=SERIES[i % 8], linewidth=2,
                    marker="o", markersize=4, markeredgecolor=SURFACE, label=tgt)
        lost = [p["t"] for p in pings if p["target"] == tgt and p["loss_pct"] and p["loss_pct"] > 0]
        if lost:
            ax.plot(lost, [0] * len(lost), linestyle="none", marker="v", markersize=8,
                    color=SERIES[(i + 1) % 8], label=f"{tgt}: packet loss > 0%")
    if len(targets) > 1 or any(p["loss_pct"] for p in pings):
        ax.legend(frameon=False, fontsize=8, loc="upper left")
    ax.set_ylim(bottom=0)
    time_axis(ax, start, end)
    return fig_to_img(fig, "Ping round-trip time", hits)


def chart_us_power(polls, upstream, start, end):
    """One strip per channel on a shared scale (small multiples), so channels at the same level don't hide each other."""
    t_of = {p["id"]: p["t"] for p in polls}
    by_ch = defaultdict(list)
    for r in upstream:
        if r["power_dbmv"] is not None and r["poll_id"] in t_of:
            by_ch[(r["kind"], r["chid"])].append((t_of[r["poll_id"]], r["power_dbmv"]))
    title = "Upstream transmit power by channel (dBmV)"
    if not by_ch:
        return f"<p class='muted'>{esc(title)}: no data in this range.</p>"
    keys = sorted(by_ch, key=lambda k: k[1])
    vals = [v for k in keys for _, v in by_ch[k]]
    lo, hi = min(vals), max(vals)
    pad = max(0.5, (hi - lo) * 0.2)
    fig, axes = plt.subplots(len(keys), 1, figsize=(10, 0.75 * len(keys) + 0.9), sharex=True, sharey=True,
                             squeeze=False)
    fig.patch.set_facecolor(SURFACE)
    axes[0][0].set_title(title, loc="left", fontsize=11, color=INK, fontweight="bold", pad=8)
    hits = []
    for i, key in enumerate(keys):
        ax = axes[i][0]
        ax.set_facecolor(SURFACE)
        pts = sorted(by_ch[key])
        hits += [("pt", ax, t, v, f"{tip_time(t)}\nUpstream ch {key[1]} ({key[0]}): {v:g} dBmV") for t, v in pts]
        ax.plot([p[0] for p in pts], [p[1] for p in pts], color=SERIES[0], linewidth=2,
                marker="o", markersize=3, markeredgecolor=SURFACE, markeredgewidth=0.8)
        ax.set_ylim(lo - pad, hi + pad)
        ax.set_yticks(sorted({lo, hi}))
        ax.set_ylabel(f"ch {key[1]}", rotation=0, ha="right", va="center", fontsize=9, color=INK2)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(GRID)
        ax.tick_params(colors=MUTED, labelsize=8)
        ax.grid(axis="y", color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)
    time_axis(axes[-1][0], start, end)
    fig.tight_layout(h_pad=0.4)
    return fig_to_img(fig, "Upstream power", hits)


def chart_t3(events, start, end):
    t3 = [e["t"] for e in events if e["t"] and "T3 time-out" in e["text"] and start <= e["t"] <= end]
    fig, ax = new_ax("T3 time-outs logged by the modem, per hour (local time)", "count", height=2.3)
    if t3:
        hours = Counter(t.replace(minute=0, second=0, microsecond=0) for t in t3)
        xs = sorted(hours)
        ax.bar(xs, [hours[x] for x in xs], width=1 / 24 * 0.85, align="edge", color=SERIES[1],
               edgecolor=SURFACE, linewidth=1)
        hits = [("rect", ax, x, 0, x + dt.timedelta(hours=1), hours[x],
                 f"{x:%a %b %d} {x:%H:00}-{x + dt.timedelta(hours=1):%H:00} (local)\n{hours[x]} T3 time-out(s)")
                for x in xs]
    else:
        hits = []
    ax.yaxis.get_major_locator().set_params(integer=True)
    time_axis(ax, start, end)
    return fig_to_img(fig, "T3 time-outs per hour", hits)


def chart_uncorrectables(deltas, downstream, start, end):
    """Uncorrectable codewords added in each polling interval, one strip per channel that had any (shared scale)."""
    title = "Downstream uncorrectable codewords added between polls"
    keys = sorted((k for k, v in deltas.items()
                   if any((x["uncorrectable"] or 0) > 0 or x["uncorrectable"] == COUNTER_DROP for x in v)),
                  key=lambda k: (k[0] != "SC-QAM", k[1]))
    if not keys:
        return f"<p class='muted'>{esc(title)}: no channel added uncorrectable codewords in this range.</p>"
    freq = {(r["kind"], r["chid"]): r["freq_hz"] for r in downstream if r["freq_hz"]}
    top = max([x["uncorrectable"] or 0 for k in keys for x in deltas[k]] + [1])
    fig, axes = plt.subplots(len(keys), 1, figsize=(10, 0.85 * len(keys) + 1.0), sharex=True, sharey=True,
                             squeeze=False)
    fig.patch.set_facecolor(SURFACE)
    axes[0][0].set_title(title, loc="left", fontsize=11, color=INK, fontweight="bold", pad=8)
    hits, any_drop = [], False
    for i, key in enumerate(keys):
        ax = axes[i][0]
        ax.set_facecolor(SURFACE)
        name = f"{key[0]} {key[1]}" + (f" · {freq[key] / 1e6:.0f} MHz" if key in freq else "")
        for x in deltas[key]:
            v, t0, t1 = x["uncorrectable"], x["prev_t"], x["t"]
            if v == COUNTER_DROP:
                any_drop = True
                ax.axvspan(t0, t1, facecolor=SURFACE, edgecolor=MUTED, hatch="////", linewidth=0)
                hits.append(("rect", ax, t0, 0, t1, top, f"{name}\n{tip_time(t0)} to {t1:%H:%M}\n"
                             "counter reset: went down with no modem restart, so the number added is unknown"))
            elif v:
                ax.bar(t0, v, width=(t1 - t0) * 0.9, align="edge", color=SERIES[0], linewidth=0)
                hits.append(("rect", ax, t0, 0, t1, max(v, top * 0.15),
                             f"{name}\n{tip_time(t0)} to {t1:%H:%M}\n+{v:,} uncorrectable"))
        ax.set_ylim(0, top * 1.1)
        ax.set_ylabel(name, rotation=0, ha="right", va="center", fontsize=8, color=INK2)
        ax.yaxis.set_major_locator(matplotlib.ticker.MaxNLocator(2, integer=True))
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(GRID)
        ax.tick_params(colors=MUTED, labelsize=8)
        ax.grid(axis="y", color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)
    time_axis(axes[-1][0], start, end)
    if any_drop:
        axes[-1][0].legend(handles=[matplotlib.patches.Patch(facecolor=SURFACE, edgecolor=MUTED, hatch="////",
                                                             label="counter reset (went down with no modem restart)")],
                           frameon=False, fontsize=8, loc="upper left", bbox_to_anchor=(0, -0.45))
    fig.tight_layout(h_pad=0.4)
    return fig_to_img(fig, title, hits)


# ---------------------------------------------------------------------------

def table(headers, rows, cls=""):
    h = "".join(f"<th>{esc(x)}</th>" for x in headers)
    body = []
    for r in rows:
        attrs = ""
        if isinstance(r, tuple):  # (cells, row_class)
            r, cls_ = r
            attrs = f' class="{cls_}"' if cls_ else ""
        body.append(f"<tr{attrs}>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>")
    return f'<div class="tw"><table class="{cls}"><thead><tr>{h}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


def build_html(cfg, data, since, until):
    threshold = cfg["tests"].getfloat("upload_outage_mbps")
    polls, ok_polls = data["polls"], [p for p in data["polls"] if p["ok"]]
    attach_event_local_times(data["events"], data["all_polls"])
    events = [e for e in data["events"]
              if (e["t"] and since <= e["t"] <= until)
              or (not e["t"] and e["first_seen_ts"] and since <= _local_naive(e["first_seen_ts"]) <= until)]
    events = [e for e in events if "GUI Login Status" not in e["text"]]
    periods = upload_periods(data["speedtests"], threshold)
    reboots = [r for r in detect_reboots(data["all_polls"]) if since <= r["seen"] <= until]
    deltas = codeword_deltas(polls, data["downstream"])
    # Charts span the measurements plus any modem log entries in range (the log reaches back before the first poll).
    start = min([p["t"] for p in polls] + [s["t"] for s in data["speedtests"]] + [e["t"] for e in events if e["t"]]
                or [since]).replace(minute=0, second=0, microsecond=0)  # whole hour so hourly bars aren't clipped
    end = max([p["t"] for p in polls] + [s["t"] for s in data["speedtests"]] or [until])
    if end - start < dt.timedelta(hours=1):
        end = start + dt.timedelta(hours=1)

    latest = ok_polls[-1] if ok_polls else None
    out = []
    out.append("<h1>Cable modem monitoring report</h1>")
    out.append(f"<p class='muted'>Range: {esc(fmt_t(since))} to {esc(fmt_t(until))} (local time, {now_local():%Z}). "
               f"Generated {esc(now_local().strftime('%Y-%m-%d %H:%M %Z'))}.</p>")

    out.append(outages_html(find_outages(data["speedtests"], data["pings"], threshold,
                                         cfg["tests"].getfloat("ping_outage_loss_pct")), events, cfg, data, deltas))

    # --- Modem details
    out.append("<h2>Modem</h2>")
    if latest:
        off = latest["clock_offset_s"]
        rows = [
            ["Model / hardware", f"{esc(latest['hw_model'])} / {esc(latest['hw_version'])}"],
            ["Firmware", esc(latest["sw_version"])],
            ["Cable modem MAC", esc(latest["mac"])],
            ["Serial", esc(latest["serial"])],
            ["DOCSIS mode", esc(latest["docsis_mode"])],
            ["Status at last poll", f"{esc(latest['cm_status'])}, {esc(latest['cm_connected'])}, "
                                    f"network access {esc(latest['network_access'])}, config file {esc(latest['config_file'])}"],
            ["Uptime at last poll", f"{esc(latest['uptime_raw'])} ({esc(fmt_t(latest['t']))})"],
            ["Modem clock vs. this computer",
             f"modem clock read {esc(latest['modem_time_raw'])} at local {esc(latest['t'].strftime('%H:%M:%S'))}; "
             f"difference {off / 3600:+.2f} h" if off is not None else "n/a"],
        ]
        out.append(table(["", ""], rows, "kv"))
        if off is not None and abs(off) > 120:
            out.append(f"<p class='note'>Event-log times are shown as the modem logged them, and also converted to this "
                       f"computer's local time using the measured clock difference ({off / 3600:+.2f} h).</p>")

    # --- Polling summary
    n_fail = sum(1 for p in polls if not p["ok"])
    sts = data["speedtests"]
    ups = [s["up_mbps"] for s in sts if s["up_mbps"] is not None]
    downs = [s["down_mbps"] for s in sts if s["down_mbps"] is not None]
    out.append("<h2>Summary of measurements</h2>")
    srows = [
        ["Modem polls", f"{len(polls)} ({n_fail} where the modem web page could not be read)"],
        ["Speed tests", f"{len(sts)}; {sum(1 for u in ups if u < threshold)} with upload below {threshold:g} Mbps"],
    ]
    if ups:
        srows.append(["Upload Mbps (min / median / max)", f"{min(ups):.2f} / {statistics.median(ups):.2f} / {max(ups):.2f}"])
    if downs:
        srows.append(["Download Mbps (min / median / max)", f"{min(downs):.1f} / {statistics.median(downs):.1f} / {max(downs):.1f}"])
    pings = data["pings"]
    if pings:
        lost = sum(p["sent"] - p["received"] for p in pings)
        sent = sum(p["sent"] for p in pings)
        avgs = [p["rtt_avg"] for p in pings if p["rtt_avg"] is not None]
        srows.append(["Ping", f"{len(pings)} tests, {lost} of {sent} packets lost"
                      + (f"; average RTT min / median / max {min(avgs):.1f} / {statistics.median(avgs):.1f} / {max(avgs):.1f} ms" if avgs else "")])
    out.append(table(["", ""], srows, "kv"))

    # --- Timeline
    out.append("<h2>Timeline</h2>")
    tl = []
    for n in data["notes"]:
        tl.append((n["t"], "Note", esc(n["text"])))
    for p in periods:
        end_s = fmt_t(p["end"]) if p["end"] else "still below threshold at last test"
        t3n = sum(1 for e in events if e["t"] and "T3 time-out" in e["text"]
                  and p["start"] - dt.timedelta(minutes=30) <= e["t"] <= (p["end"] or until))
        tl.append((p["start"], "Upload below threshold",
                   f"Upload measured below {threshold:g} Mbps from {esc(fmt_t(p['start']))} until {esc(end_s)} "
                   f"({p['tests']} consecutive test(s), lowest {p['min_up'] if p['min_up'] is not None else 'n/a'} Mbps); "
                   f"{t3n} T3 time-outs logged from 30 min before through the end of this period"))
    for p in ping_loss_periods(data["pings"]):
        end_s = f"until {fmt_t(p['end'])}" if p["end"] else "still losing packets at last test"
        tl.append((p["start"], "Ping packet loss",
                   f"Ping tests lost packets from {esc(fmt_t(p['start']))} {esc(end_s)} "
                   f"({p['tests']} consecutive test(s), {p['lost']} of {p['sent']} packets lost)"))
    for r in reboots:
        tl.append((r["t"], "Modem restart", f"Uptime counter reset; restart at about {esc(fmt_t(r['t']))} (seen at poll {esc(fmt_t(r['seen']))})"))
    drop_by_interval = defaultdict(list)
    for k, v in deltas.items():
        for x in v:
            fields = [f for f in ("corrected", "uncorrectable") if x[f] == COUNTER_DROP]
            if fields:
                drop_by_interval[(x["prev_t"], x["t"])].append(f"{k[0]} {k[1]} ({', '.join(fields)})")
    for (t0, t1), chans in sorted(drop_by_interval.items()):
        tl.append((t1, "Counter decrease",
                   f"Downstream codeword counters went down between polls at {esc(fmt_t(t0))} and "
                   f"{esc(fmt_t(t1))} with no modem restart (uptime kept increasing): {esc('; '.join(chans))}"))
    for p in polls:
        if not p["ok"]:
            tl.append((p["t"], "Poll failed", esc(p["error"])))
    tl.sort(key=lambda x: x[0])
    out.append(table(["Local time", "Type", "Detail"], [[esc(fmt_t(t)), esc(k), d] for t, k, d in tl])
               if tl else "<p class='muted'>No notes, below-threshold upload periods, or restarts in this range.</p>")

    # --- Key event counts
    out.append("<h2>Modem event log: counts of selected entries</h2>")
    crow = []
    for label, pat in KEY_EVENTS:
        hits = [e for e in events if re.search(pat, e["text"], re.I)]
        if hits:
            ts = [e["t"] for e in hits if e["t"]]
            crow.append([esc(label), str(len(hits)), esc(fmt_t(min(ts))) if ts else "pre clock sync",
                         esc(fmt_t(max(ts))) if ts else "pre clock sync"])
    cm = Counter()
    for e in events:
        m = re.search(r"Event Type Code: (\d+); Chan ID: ([\w/]+)", e["text"])
        if m:
            cm[(m[1], m[2])] += 1
    out.append(table(["Entry", "Count", "First (local)", "Last (local)"], crow) if crow
               else "<p class='muted'>None.</p>")
    if cm:
        out.append("<p>CM-STATUS by event type code and channel: "
                   + ", ".join(f"code {c} ch {ch}: {n}" for (c, ch), n in sorted(cm.items(), key=lambda x: (int(x[0][0]), x[0][1])))
                   + "</p>")

    # --- Latest signal table
    if latest:
        out.append(f"<h2>Signal at last poll ({esc(fmt_t(latest['t']))})</h2>")
        range_unc = {k: sum(max(x["uncorrectable"] or 0, 0) for x in v) for k, v in deltas.items()}
        range_cor = {k: sum(max(x["corrected"] or 0, 0) for x in v) for k, v in deltas.items()}
        dropped = {k for k, v in deltas.items() if any(COUNTER_DROP in (x["corrected"], x["uncorrectable"]) for x in v)}
        ds = sorted([r for r in data["downstream"] if r["poll_id"] == latest["id"]],
                    key=lambda r: (r["kind"] != "SC-QAM", r["chid"]))
        drows = []
        for r in ds:
            k = (r["kind"], r["chid"])
            flagged = (r["uncorrectable"] or 0) > 0 or range_unc.get(k, 0) > 0
            drows.append(([esc(r["kind"]), str(r["chid"]), "Locked" if r["locked"] else "Not locked", esc(r["modulation"]),
                           f"{(r['freq_hz'] or 0) / 1e6:.1f}", esc(r["power_dbmv"]), esc(r["snr_db"]),
                           f"{r['corrected']:,}" if r["corrected"] is not None else "",
                           f"{r['uncorrectable']:,}" if r["uncorrectable"] is not None else "",
                           f"{range_cor.get(k, 0):,}" + ("*" if k in dropped else ""),
                           f"{range_unc.get(k, 0):,}" + ("*" if k in dropped else "")], "flag" if flagged else ""))
        out.append("<p class='muted'>Codeword counters are cumulative since the modem last started. "
                   "Highlighted rows have uncorrectable codewords greater than zero. "
                   "\"In range\" columns are the sum of increases between polls in this report's range; "
                   "* = the channel's counters went down at least once without a modem restart, so the sum leaves out "
                   "those intervals (see Timeline).</p>")
        out.append(table(["Type", "Ch ID", "Lock", "Modulation", "Freq MHz", "Power dBmV", "SNR/MER dB",
                          "Corrected", "Uncorrectable", "Corrected in range", "Uncorrectable in range"], drows, "num"))
        us = sorted([r for r in data["upstream"] if r["poll_id"] == latest["id"]], key=lambda r: r["chid"])
        out.append("<h3>Upstream bonded channels</h3>")
        out.append(table(["Type", "Ch ID", "Lock", "Modulation", "Freq MHz", "Width MHz", "Power dBmV"],
                         [[esc(r["kind"]), str(r["chid"]), "Locked" if r["locked"] else "Not locked", esc(r["modulation"]),
                           f"{(r['freq_hz'] or 0) / 1e6:.1f}", f"{(r['width_hz'] or 0) / 1e6:.1f}", esc(r["power_dbmv"])]
                          for r in us], "num"))

    # --- Charts
    out.append("<h2>Charts</h2>")
    if periods:
        out.append(f"<p class='muted'>Shaded spans: speed tests measured upload below {threshold:g} Mbps.</p>")
    out.append(chart_speed(sts, periods, start, end, "up_mbps", "Upload speed (Mbps)", threshold))
    out.append(chart_speed(sts, periods, start, end, "down_mbps", "Download speed (Mbps)"))
    out.append(chart_ping(pings, periods, start, end))
    out.append(chart_t3(events, start, end))
    out.append(chart_us_power(polls, data["upstream"], start, end))
    out.append(chart_uncorrectables(deltas, data["downstream"], start, end))

    # --- Speed and ping: one row per day; every test only in a collapsed list (dashboard, not printed)
    out.append("<h2>Speed test results</h2>")
    out.append("<p class='muted'>One row per day. Server: speed.cloudflare.com. See \"How the measurements were taken\" "
               "at the end. Every individual test is listed on the dashboard under \"Show every test\".</p>")
    out.append(speed_daily_table(sts, threshold))
    out.append(full_list("speed test", len(sts), table(
        ["Local time", "Down Mbps", "Up Mbps", "HTTP latency ms", "Run", "Notes"],
        [([esc(s["t"].strftime("%Y-%m-%d %H:%M")), _f(s["down_mbps"]), _f(s["up_mbps"]), esc(s["latency_ms"]),
           esc(run_reason(s.get("trigger"))), esc(s["error"] or "")],
          "flag" if s["up_mbps"] is None or s["up_mbps"] < threshold else "") for s in sts], "num")))
    out.append("<h2>Ping results</h2>")
    out.append(f"<p class='muted'>One row per day; {esc(cfg['tests']['ping_targets'])}, "
               f"{cfg['tests'].getint('ping_count')} packets per test. All tests are in the ping chart.</p>")
    out.append(ping_daily_table(pings))
    problems = [p for p in pings if ping_problem(p, cfg)]
    out.append(full_list(f"ping test with loss, errors or average above {cfg['tests'].getfloat('ping_trigger_ms'):g} ms",
                         len(problems), table(
        ["Local time", "Target", "Received", "Loss %", "Min ms", "Avg ms", "Max ms", "Notes"],
        [([esc(p["t"].strftime("%Y-%m-%d %H:%M")), esc(p["target"]), f"{p['received']}/{p['sent']}",
           esc(p["loss_pct"]), _f(p["rtt_min"], 1), _f(p["rtt_avg"], 1), _f(p["rtt_max"], 1), esc(p["error"] or "")],
          "flag" if p["loss_pct"] else "") for p in problems], "num")))

    # --- Event log
    out.append("<h2>Modem event log (critical, error, warning, CM-STATUS and profile-change entries)</h2>")
    sel = [e for e in events if e["priority"].lower() in ("critical", "error", "warning", "alert", "emergency")
           or re.search(r"CM-STATUS|profile assignment change", e["text"], re.I)]
    sel.sort(key=lambda e: (e["t"] or dt.datetime.min, e["id"]))
    out.append(table(["Modem time (as logged, DD/MM/YYYY)", "Local time", "Priority", "Text"],
                     [([esc(e["time_raw"]), esc(e["t"].strftime("%Y-%m-%d %H:%M:%S")) if e["t"] else "pre clock sync",
                        esc(e["priority"]), esc(e["short"])],
                       "flag" if e["priority"].lower() == "critical" else "") for e in sel]))
    out.append("<p class='muted'>Entries are de-duplicated across polls by modem time and text. "
               "GUI login entries created by this monitoring tool are omitted.</p>")
    out.append(methods_html(cfg))
    return PAGE.format(body="\n".join(out))


def failure_kind(err):
    e = (err or "").lower()
    if "resolve host" in e or "exit 6 " in e:
        return "no connection (name lookup failed)"
    if "timed out" in e or "exit 28" in e:
        return "timed out"
    if "reset" in e or "exit 56" in e or "ssl" in e:
        return "connection reset"
    if "exit 7 " in e or "connect" in e:
        return "could not connect"
    return "other error"


def _mmm(vals, nd):
    vals = [v for v in vals if v is not None]
    if not vals:
        return ""
    return f"{min(vals):.{nd}f} / {statistics.median(vals):.{nd}f} / {max(vals):.{nd}f}"


def by_day(rows):
    days = defaultdict(list)
    for r in rows:
        days[r["t"].date()].append(r)
    return sorted(days.items())


def speed_daily_table(sts, threshold):
    rows = []
    for day, ss in by_day(sts):
        low = [s for s in ss if s["up_mbps"] is None or s["up_mbps"] < threshold]
        trig = sum(1 for s in ss if run_reason(s.get("trigger")).startswith("triggered"))
        kinds = Counter(failure_kind(s["error"]) for s in ss if s["error"])
        rows.append(([esc(day.strftime("%a %b %d")), f"{len(ss)}" + (f" ({trig} triggered)" if trig else ""),
                      f"{len(low)}", _mmm([s["up_mbps"] for s in ss], 2), _mmm([s["down_mbps"] for s in ss], 0),
                      esc(", ".join(f"{k} ×{n}" for k, n in kinds.most_common()))], "flag" if low else ""))
    return table(["Day", "Tests", f"Upload below {threshold:g} Mbps", "Upload Mbps min / median / max",
                  "Download Mbps min / median / max", "Failed tests"], rows, "num")


def ping_daily_table(pings):
    rows = []
    for day, ps in by_day(pings):
        sent = sum(p["sent"] for p in ps)
        lost = sum(p["sent"] - p["received"] for p in ps)
        lossy = [p for p in ps if p["received"] < p["sent"]]
        worst = max(ps, key=lambda p: ((p["sent"] - p["received"]) / p["sent"] if p["sent"] else 0, p["rtt_avg"] or 0))
        w = (f"{worst['t']:%H:%M}: {worst['received']}/{worst['sent']} received"
             + (f", avg {worst['rtt_avg']:.0f} ms" if worst["rtt_avg"] is not None else "")) if (lossy or worst["rtt_avg"]) else ""
        rows.append(([esc(day.strftime("%a %b %d")), str(len(ps)), f"{lost} of {sent} ({lost * 100 / sent:.1f}%)" if sent else "",
                      str(len(lossy)), _mmm([p["rtt_avg"] for p in ps], 1), esc(w)], "flag" if lost else ""))
    return table(["Day", "Tests", "Packets lost", "Tests with loss", "Average RTT ms min / median / max",
                  "Worst test"], rows, "num")


def full_list(what, n, table_html):
    if not n:
        return ""
    return (f"<details class='full'><summary>Show every {esc(what)} ({n})</summary>{table_html}</details>")


def methods_html(cfg):
    t, m = cfg["tests"], cfg["modem"]
    iface = m.get("interface", "")
    via = f" All tests are bound to network interface {esc(iface)} (wired, not over any VPN)." if iface else ""
    cap = t.getint("speedtest_timeout")
    down_mb, up_mb = t.getint("download_bytes") / 1e6, t.getint("upload_bytes") / 1e6
    rows = [
        ["Schedule", "Full run every 30 minutes (on the hour and half hour, up to 60 s later): reads the modem, "
                     "then runs the ping test, then the download and upload tests. "
                     "Ping test alone every 5 minutes (2, 7, 12 ... minutes past the hour). If a ping test loses "
                     f"packets or averages above {t.getfloat('ping_trigger_ms'):g} ms, a full run starts "
                     f"immediately, unless one already ran in the previous {t.getint('trigger_cooldown_min')} minutes. "
                     "The Run column of the speed test table shows why each run happened. "
                     "Tests never overlap: a run waits for any run in progress to finish." + via],
        ["Modem readings", f"Read from the modem's own web interface at {esc(m['host'])} (the same figures as its "
                           "status and event-log pages). Codeword counters are the modem's cumulative counts; "
                           "the report computes the change between readings."],
        ["Ping", f"{t.getint('ping_count')} ICMP echo requests to {esc(t['ping_targets'])}, 0.5 s apart, "
                 "each waiting up to 2 s for a reply. Records packets sent/received and min/avg/max round-trip time."],
        ["Download", f"HTTPS GET requests for {min(down_mb, 25):g} MB blocks from "
                     "https://speed.cloudflare.com/__down, one after another over a single connection at a time, "
                     f"until {down_mb:g} MB have arrived or {cap} s have passed. "
                     "Speed = total bytes received × 8 ÷ total transfer time (includes connection setup for each block)."],
        ["Upload", f"One HTTPS POST of {up_mb:g} MB of random (incompressible) data to "
                   f"https://speed.cloudflare.com/__up, stopped after {cap} s if not finished. "
                   "Speed = bytes sent × 8 ÷ transfer time, as measured by curl. A connection reset, a timeout, or "
                   "a cap reached before all data was sent is written in the Notes column with curl's error."],
        ["HTTP latency", "Time from the end of the TLS handshake to the first byte of a zero-byte response from "
                         "speed.cloudflare.com. This is not the same as ping time: it includes server response time."],
        ["Upload threshold", f"A test with upload below {t.getfloat('upload_outage_mbps'):g} Mbps is listed as "
                             "\"upload below threshold\"; consecutive such tests form one period in the timeline."],
        ["Notes", "Single-connection tests can read lower than multi-connection tests (such as Ookla Speedtest) "
                  "on the same line. Cloudflare's server is reached via its nearest site, so the results cover the "
                  "whole path to that server, not only the cable provider's network."],
    ]
    return "<h2>How the measurements were taken</h2>" + table(["", ""], rows, "kv")


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Modem Monitoring Report</title>
<style>
:root {{ --ink:#0b0b0b; --ink2:#52514e; --muted:#898781; --grid:#e4e3df; --surface:#fcfcfb; --flag:#fdecec; }}
body {{ font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; color: var(--ink); background: var(--surface);
       max-width: 1100px; margin: 24px auto; padding: 0 16px; }}
h1 {{ font-size: 22px; margin-bottom: 4px; }}
h2 {{ font-size: 17px; margin-top: 32px; border-bottom: 1px solid var(--grid); padding-bottom: 4px; }}
h3 {{ font-size: 15px; margin-top: 20px; }}
.muted {{ color: var(--muted); font-size: 12px; }}
.note {{ color: var(--ink2); }}
.tw {{ overflow-x: auto; }}
table {{ border-collapse: collapse; width: 100%; font-size: 12px; margin: 8px 0; }}
th, td {{ text-align: left; padding: 3px 8px; border-bottom: 1px solid var(--grid); vertical-align: top; }}
th {{ color: var(--ink2); font-weight: 600; }}
table.num td {{ font-variant-numeric: tabular-nums; }}
table.kv td:first-child {{ color: var(--ink2); width: 260px; }}
table.kv thead {{ display: none; }}
@media (max-width: 600px) {{ table.kv td:first-child {{ width: 40%; }} body {{ margin-top: 8px; }} }}
tr.flag td {{ background: var(--flag); }}
img {{ max-width: 100%; display: block; margin: 12px 0; }}
figure.chart {{ position: relative; margin: 12px 0; }}
figure.chart img {{ width: 100%; margin: 0; }}
.hit {{ position: absolute; cursor: pointer; outline: none; }}
.hit.pt {{ width: 22px; height: 22px; margin: -11px 0 0 -11px; border-radius: 50%; }}
.hit.pt:hover, .hit.pt.on {{ box-shadow: 0 0 0 2px var(--surface), 0 0 0 4px var(--ink); width: 10px; height: 10px; margin: -5px 0 0 -5px; }}
.hit.rect:hover, .hit.rect.on {{ box-shadow: inset 0 0 0 2px var(--ink); }}
.hit:hover::after, .hit.on::after {{ content: attr(data-tip); position: absolute; z-index: 5; left: 50%; bottom: calc(100% + 8px);
  white-space: pre; background: var(--ink); color: #fff; font-size: 12px; line-height: 1.4; padding: 6px 8px;
  border-radius: 6px; pointer-events: none; }}
.hit.r:hover::after, .hit.r.on::after {{ left: auto; right: 50%; }}
details.full {{ margin: 6px 0 12px; }}
details.full summary {{ cursor: pointer; color: var(--ink2); font-size: 13px; }}
@media print {{ details.full {{ display: none; }} .hit {{ display: none; }} .tw {{ overflow: visible; }} table {{ font-size: 9px; }} th, td {{ padding: 2px 4px; }}
               body {{ margin: 0; max-width: none; }} h2 {{ break-after: avoid; }} img, tr {{ break-inside: avoid; }} }}
</style></head><body>
{body}
<script>
// Tap support for phones: tap a mark to show its value, tap elsewhere to hide.
document.addEventListener("click", function (e) {{
  var hit = e.target.closest(".hit");
  document.querySelectorAll(".hit.on").forEach(function (h) {{ if (h !== hit) h.classList.remove("on"); }});
  if (hit) hit.classList.toggle("on");
}});
</script>
</body></html>
"""


def resolve_range(db, since_s=None, until_s=None):
    """Defaults: from the first poll or note until now. A bare date for `until` means the end of that day."""
    firsts = [_local_naive(r[0]) for r in db.execute("SELECT MIN(ts) FROM polls UNION ALL SELECT MIN(ts) FROM notes") if r[0]]
    now = now_local().replace(tzinfo=None)
    since = _parse_bound(since_s, min(firsts) if firsts else now - dt.timedelta(days=1))
    until = _parse_bound(until_s, now)
    if until_s and len(until_s) <= 10:
        until = until + dt.timedelta(days=1) - dt.timedelta(seconds=1)
    return since, until


def cmd_report(cfg, args):
    db = open_db(cfg)
    since, until = resolve_range(db, args.since, args.until)
    data = load(db, since, until)
    out = Path(args.output) if args.output else (
        HERE / cfg["storage"]["reports"] / f"modem_report_{since:%Y%m%d_%H%M}_{until:%Y%m%d_%H%M}.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_html(cfg, data, since, until))
    print(f"wrote {out}")
    if args.pdf:
        write_pdf(out, out.with_suffix(".pdf"))


def write_pdf(html_path, pdf_path):
    import shutil
    import subprocess
    # Playwright's per-user headless Chromium first (used on the Orange Pi, where snap Chromium won't start).
    candidates = sorted(Path.home().glob(".cache/ms-playwright/chromium_headless_shell-*/chrome-*/chrome-headless-shell"))
    candidates += [shutil.which(b) for b in ("google-chrome", "chromium", "chromium-browser", "/snap/bin/chromium")]
    browser = next((str(b) for b in candidates if b), None)
    if not browser:
        raise RuntimeError("no Chrome/Chromium found; open the report and use Print > Save as PDF instead")
    subprocess.run([browser, "--headless", "--disable-gpu", "--no-pdf-header-footer",
                    f"--print-to-pdf={pdf_path}", html_path.resolve().as_uri()],
                   check=True, capture_output=True, timeout=120)
    print(f"wrote {pdf_path}")
