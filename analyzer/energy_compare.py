"""energy_compare — A/B/C comparison of hybrid energy deployment across race logs.

Made for AI-deployment experiments: run the same race under different AI settings, record
each with logger V1.4+ (schema 2), then

  py energy_compare.py <logA> <logB> [<logC> ...] [--labels A,B,C] [--out compare.md]
                       [--html compare.html]

prints a markdown report: pooled AI medians per arm (all laps of all AI cars with CAN data,
median and interquartile range) with deltas against the first log, the player alongside, and a
per-zone breakdown of where along the lap the AI deploys (zones = the first log's embedded
drs_zones.ini: straight-mode, power-reduction, power-reset, alternative-curve, DRS). --html adds
a self-contained page overlaying the arms' pooled AI kW / SoC profiles on the zone bands.
No track model is needed (profiles come from the log's own spline), so it runs anywhere.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np

import vrclog_parser
import energy

ARM_COLORS = ["#e8433f", "#3b7ddd", "#39c07a", "#f2a93b", "#b06ef2", "#4fd1e0"]


# ---- loading ------------------------------------------------------------------------------

def load_arm(path, label):
    rd = vrclog_parser.parse(path)
    blk = energy.analyze(rd)
    if not blk or not blk["hasCan"]:
        raise ValueError(f"{os.path.basename(path)}: no CAN energy data (needs logger V1.4+ "
                         f"and a car with an energy profile)")
    ai_cars = [c for c in blk["cars"] if c["ai"] and c["profile"] == "can"]
    players = [c for c in blk["cars"] if not c["ai"] and c["profile"] == "can"]
    ai_laps = [l for c in ai_cars for l in c["laps"]]
    lap_ms = {}
    for l in rd.ev_lap:
        lap_ms.setdefault(l["car"], []).append((l["lap_n"], l["lap_ms"]))
    def clean_laps(ci):
        laps = sorted(lap_ms.get(ci, []))
        laps = [ms for n, ms in laps if n >= 2] or [ms for n, ms in laps]
        return laps
    ai_lap_times = [ms / 1000.0 for c in ai_cars for ms in clean_laps(c["i"])]
    pl_lap_times = [ms / 1000.0 for c in players for ms in clean_laps(c["i"])]
    pu = np.concatenate([rd.E[c["i"]]["pu_mode"] for c in ai_cars]) if ai_cars else np.zeros(0)
    pu = pu[pu >= 0]
    return {
        "label": label, "path": path, "file": os.path.basename(path),
        "track": rd.meta.get("trackFull", "?"), "date": rd.meta.get("date", ""),
        "session": rd.meta.get("sessionName", ""), "cars": rd.n_cars,
        "ai_cars": ai_cars, "players": players, "ai_laps": ai_laps,
        "ai_lap_times": ai_lap_times, "pl_lap_times": pl_lap_times,
        "ai_pu_mode": int(np.bincount(pu.astype(np.int64)).argmax()) if len(pu) else None,
        "blk": blk,
    }


def load_arms(paths, labels=None):
    labels = labels or [chr(ord("A") + i) for i in range(len(paths))]
    if len(labels) != len(paths):
        raise ValueError("--labels must name every log")
    return [load_arm(p, l) for p, l in zip(paths, labels)]


# ---- metrics ------------------------------------------------------------------------------

def _q(values):
    vals = [v for v in values if v is not None and np.isfinite(v)]
    if not vals:
        return None
    a = np.asarray(vals, dtype=np.float64)
    return (float(np.median(a)), float(np.percentile(a, 25)), float(np.percentile(a, 75)), len(a))


def _mode(values):
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return max(set(vals), key=vals.count)


METRICS = [
    # key, label, unit, how (fn(arm) -> (median, p25, p75, n) or scalar), kind
    ("ai_n", "AI cars with CAN data", "", lambda a: len(a["ai_cars"]), "int"),
    ("ai_laps", "AI laps pooled", "", lambda a: len(a["ai_laps"]), "int"),
    ("dep", "AI deploy MJ/lap", "MJ", lambda a: _q(l["dep"] for l in a["ai_laps"]), "q"),
    ("reg", "AI harvest MJ/lap", "MJ", lambda a: _q(l["reg"] for l in a["ai_laps"]), "q"),
    ("socLine", "AI SoC at the line", "%", lambda a: _q(l["socLine"] for l in a["ai_laps"]), "pct"),
    ("socMin", "AI min SoC in lap", "%", lambda a: _q(l["socMin"] for l in a["ai_laps"]), "pct"),
    ("socDrift", "AI SoC drift (last - first lap)", "pp", lambda a: a["blk"]["field"]["socDrift"], "pctpt"),
    ("kwMax", "AI peak deploy kW", "kW", lambda a: _q(c["summary"]["kwMax"] for c in a["ai_cars"]), "q0"),
    ("reqPct", "AI time requesting deploy", "%", lambda a: _q(c["summary"]["reqPct"] for c in a["ai_cars"]), "pct"),
    ("blockedPct", "AI requests blocked (cap = 0)", "%", lambda a: _q(c["summary"]["blockedPct"] for c in a["ai_cars"]), "pct"),
    ("deliveredPct", "AI requests delivered", "%", lambda a: _q(c["summary"]["deliveredPct"] for c in a["ai_cars"]), "pct"),
    ("deployS", "AI deploy s/lap", "s", lambda a: _q(l["deployS"] for l in a["ai_laps"]), "q"),
    ("harvestS", "AI harvest s/lap", "s", lambda a: _q(l["harvestS"] for l in a["ai_laps"]), "q"),
    ("smS", "AI straight-mode s/lap", "s", lambda a: _q(l["smS"] for l in a["ai_laps"]), "q"),
    ("otN", "AI overtake uses/lap", "", lambda a: _q(l["otN"] for l in a["ai_laps"]), "q"),
    ("plimS", "AI power-limited s/lap", "s", lambda a: _q(l["plimS"] for l in a["ai_laps"]), "q"),
    ("vmax", "AI Vmax km/h", "km/h", lambda a: _q(l["vmax"] for l in a["ai_laps"]), "q0"),
    ("lapT", "AI lap time s (laps ≥ 2)", "s", lambda a: _q(a["ai_lap_times"]), "q2"),
    ("bestT", "AI best lap s", "s", lambda a: min(a["ai_lap_times"]) if a["ai_lap_times"] else None, "f2"),
    ("strat", "AI STRAT (mode)", "", lambda a: _mode(c["summary"]["strat"] for c in a["ai_cars"]), "int"),
    ("puMode", "AI PU mode (mode)", "", lambda a: a["ai_pu_mode"], "int"),
    ("pl_dep", "Player deploy MJ/lap", "MJ", lambda a: _q(l["dep"] for c in a["players"] for l in c["laps"]), "q"),
    ("pl_socLine", "Player SoC at the line", "%", lambda a: _q(l["socLine"] for c in a["players"] for l in c["laps"]), "pct"),
    ("pl_kwMax", "Player peak deploy kW", "kW", lambda a: _q(c["summary"]["kwMax"] for c in a["players"]), "q0"),
    ("pl_blocked", "Player requests blocked", "%", lambda a: _q(c["summary"]["blockedPct"] for c in a["players"]), "pct"),
    ("pl_vmax", "Player Vmax km/h", "km/h", lambda a: _q(l["vmax"] for c in a["players"] for l in c["laps"]), "q0"),
    ("pl_bestT", "Player best lap s", "s", lambda a: min(a["pl_lap_times"]) if a["pl_lap_times"] else None, "f2"),
]


def _scalar(v, kind):
    """Median (or the scalar) as a number for deltas."""
    if v is None:
        return None
    if isinstance(v, tuple):
        v = v[0]
    if kind in ("pct", "pctpt"):
        return v * 100.0
    return float(v)


def _fmt(v, kind):
    if v is None:
        return "—"
    if kind == "int":
        return str(int(v))
    if isinstance(v, tuple):
        m, lo, hi, n = v
        if kind == "pct":
            return f"{m*100:.0f}% [{lo*100:.0f}–{hi*100:.0f}]"
        if kind == "q0":
            return f"{m:.0f} [{lo:.0f}–{hi:.0f}]"
        if kind == "q2":
            return f"{m:.2f} [{lo:.2f}–{hi:.2f}]"
        return f"{m:.2f} [{lo:.2f}–{hi:.2f}]"
    if kind == "pct":
        return f"{v*100:.0f}%"
    if kind == "pctpt":
        return f"{v*100:+.0f} pp"
    if kind == "f2":
        return f"{v:.2f}"
    if kind == "q0":
        return f"{v:.0f}"
    return f"{v:.2f}"


def _fmt_delta(d, kind):
    if d is None:
        return "—"
    if kind in ("pct", "pctpt"):
        return f"{d:+.0f} pp"
    if kind in ("q0",):
        return f"{d:+.0f}"
    if kind == "int":
        return f"{d:+.0f}"
    return f"{d:+.2f}"


def _zone_rows(arms):
    """Time-averaged pooled-AI kW (mean across laps and bins) and deploy share inside each
    zone of the first log, per arm. Means, not medians: the AI's pulsed bursts carry energy
    that a median hides."""
    base = arms[0]["blk"]
    bins = base["bins"]
    zones = [z for z in base["zones"] if z.get("s0") is not None and z.get("s1") is not None
             and z["k"] in ("sm", "prd", "prs", "alt", "spd", "drs")]
    def mask(z):
        s0, s1 = float(z["s0"]), float(z["s1"])
        c = (np.arange(bins) + 0.5) / bins
        return ((c >= s0) & (c < s1)) if s1 >= s0 else ((c >= s0) | (c < s1))
    rows = []
    entries = [("whole lap", np.ones(bins, dtype=bool))] + [(f"{z['name']} ({z['k']})", mask(z)) for z in zones]
    sm_any = np.zeros(bins, dtype=bool)
    for z in zones:
        if z["k"] == "sm":
            sm_any |= mask(z)
    if sm_any.any():
        entries.append(("outside SM zones", ~sm_any))
    for name, m in entries:
        vals = []
        for a in arms:
            prof = a["blk"]["aiProfile"]
            if not prof:
                vals.append((None, None, None)); continue
            kw = np.array([np.nan if v is None else v for v in prof["kwMean"]], dtype=np.float64)
            pl = None
            if a["players"] and a["players"][0].get("kwMean"):
                pk = np.array([np.nan if v is None else v for v in a["players"][0]["kwMean"]], dtype=np.float64)
                pl = float(np.nanmean(pk[m])) if np.isfinite(pk[m]).any() else None
            sel = kw[m]
            ok = np.isfinite(sel)
            vals.append((float(np.nanmean(sel)) if ok.any() else None,
                         float((sel[ok] >= 10).mean()) if ok.any() else None, pl))
        rows.append((name, vals))
    return rows


def compare(arms):
    res = {"arms": [{k: a[k] for k in ("label", "file", "track", "date", "session", "cars")}
                    for a in arms], "metrics": [], "zones": []}
    for key, label, unit, fn, kind in METRICS:
        vals = [fn(a) for a in arms]
        base = _scalar(vals[0], kind)
        deltas = [None] + [(None if base is None or _scalar(v, kind) is None
                            else _scalar(v, kind) - base) for v in vals[1:]]
        res["metrics"].append({"key": key, "label": label, "unit": unit, "kind": kind,
                               "values": [_fmt(v, kind) for v in vals],
                               "raw": [_scalar(v, kind) for v in vals],
                               "deltas": [_fmt_delta(d, kind) for d in deltas]})
    for name, vals in _zone_rows(arms):
        res["zones"].append({"zone": name, "kw": [v[0] for v in vals], "share": [v[1] for v in vals],
                             "playerKw": [v[2] for v in vals]})
    tracks = {a["track"] for a in arms}
    res["warnings"] = [] if len(tracks) == 1 else [f"logs come from different layouts: {sorted(tracks)}"]
    return res


# ---- output -------------------------------------------------------------------------------

def to_markdown(res):
    labels = [a["label"] for a in res["arms"]]
    out = ["# Energy deployment comparison", ""]
    for a in res["arms"]:
        out.append(f"- **{a['label']}** — `{a['file']}` · {a['track']} · {a['session']} · {a['date']} · {a['cars']} cars")
    for w in res["warnings"]:
        out.append(f"- **warning:** {w}")
    out += ["", "AI values pool every lap of every AI car with CAN data: median [p25–p75]. "
            "Δ = arm minus " + labels[0] + " (medians).", ""]
    head = "| metric | " + " | ".join(labels) + " | " + " | ".join(f"Δ {l}" for l in labels[1:]) + " |"
    out += [head, "|" + "---|" * (1 + len(labels) + max(0, len(labels) - 1))]
    for m in res["metrics"]:
        out.append(f"| {m['label']} | " + " | ".join(m["values"]) + " | "
                   + " | ".join(m["deltas"][1:]) + " |")
    out += ["", "## Where the AI deploys (pooled AI time-averaged kW per zone; deploy share = bins with mean ≥ 10 kW)", "",
            "| zone | " + " | ".join(f"{l} kW / share" for l in labels) + " | "
            + " | ".join(f"Δ {l} kW" for l in labels[1:]) + " | player " + labels[0] + " kW |",
            "|" + "---|" * (1 + len(labels) + max(0, len(labels) - 1) + 1)]
    for z in res["zones"]:
        cells = []
        for kw, sh in zip(z["kw"], z["share"]):
            cells.append("—" if kw is None else f"{kw:+.0f} / {sh*100:.0f}%")
        d = ["—" if (z["kw"][0] is None or v is None) else f"{v - z['kw'][0]:+.0f}" for v in z["kw"][1:]]
        pl = "—" if z["playerKw"][0] is None else f"{z['playerKw'][0]:+.0f}"
        out.append(f"| {z['zone']} | " + " | ".join(cells) + " | " + " | ".join(d) + f" | {pl} |")
    return "\n".join(out) + "\n"


HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Energy comparison</title>
<style>:root{--bg:#101318;--bg2:#171b22;--line:#2a3140;--fg:#e8ebf0;--dim:#9aa3b2}
body{background:var(--bg);color:var(--fg);font-family:"Segoe UI",system-ui,sans-serif;margin:0;padding:18px 22px;font-size:15px}
h1{font-size:1.25rem} h2{font-size:1rem;margin:18px 0 8px} table{border-collapse:collapse;font-size:.85rem}
th,td{padding:4px 10px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap} th{color:var(--dim);font-weight:500}
td.num{font-variant-numeric:tabular-nums} .hint{color:var(--dim);font-size:.78rem}
.panel{background:var(--bg2);border:1px solid var(--line);border-radius:8px;padding:12px;margin-top:8px}
.leg span{display:inline-block;margin-right:14px}.leg i{display:inline-block;width:18px;height:3px;vertical-align:middle;margin-right:5px}</style></head>
<body><h1>Energy deployment comparison</h1><div id="arms" class="hint"></div>
<h2>Pooled AI time-averaged kW along the lap (solid) · SoC (thin, right axis) · zones of the first log</h2>
<div class="panel"><canvas id="prof" height="320"></canvas><div class="leg" id="leg"></div></div>
<h2>Metrics</h2><table id="metrics"></table>
<h2>Where the AI deploys</h2><table id="zones"></table>
<script>
const RES = /*__RES__*/null, PROF = /*__PROF__*/null;
const COLORS = %s;
const esc = s => String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
document.getElementById("arms").innerHTML = RES.arms.map((a, i) => `<span style="color:${COLORS[i%%COLORS.length]}">■</span> <b>${esc(a.label)}</b> ${esc(a.file)} · ${esc(a.track)} · ${esc(a.date)} · ${a.cars} cars`).join("<br>")
  + (RES.warnings.length ? "<br><b>warning:</b> " + RES.warnings.map(esc).join("; ") : "");
const labels = RES.arms.map(a => a.label);
document.getElementById("metrics").innerHTML = `<tr><th>metric</th>${labels.map(l => `<th>${esc(l)}</th>`).join("")}${labels.slice(1).map(l => `<th>Δ ${esc(l)}</th>`).join("")}</tr>`
  + RES.metrics.map(m => `<tr><td>${esc(m.label)}</td>${m.values.map(v => `<td class="num">${esc(v)}</td>`).join("")}${m.deltas.slice(1).map(v => `<td class="num">${esc(v)}</td>`).join("")}</tr>`).join("");
const f0 = v => v === null || v === undefined ? "—" : (v >= 0 ? "+" : "") + v.toFixed(0);
document.getElementById("zones").innerHTML = `<tr><th>zone</th>${labels.map(l => `<th>${esc(l)} kW / share</th>`).join("")}${labels.slice(1).map(l => `<th>Δ ${esc(l)} kW</th>`).join("")}<th>player ${esc(labels[0])} kW</th></tr>`
  + RES.zones.map(z => `<tr><td>${esc(z.zone)}</td>${z.kw.map((kw, i) => `<td class="num">${kw === null ? "—" : f0(kw) + " / " + (z.share[i]*100).toFixed(0) + "%%"}</td>`).join("")}${z.kw.slice(1).map(kw => `<td class="num">${(kw === null || z.kw[0] === null) ? "—" : f0(kw - z.kw[0])}</td>`).join("")}<td class="num">${f0(z.playerKw[0])}</td></tr>`).join("");
(function(){
  const cv = document.getElementById("prof"), w = cv.parentElement.clientWidth - 26, h = 320;
  cv.width = w * devicePixelRatio; cv.height = h * devicePixelRatio; cv.style.width = w + "px"; cv.style.height = h + "px";
  const c = cv.getContext("2d"); c.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
  const L = 40, R = 40, top = 12, bot = 22, B = PROF.bins, X = s => L + s * (w - L - R);
  const vals = PROF.arms.flatMap(a => a.kw).filter(v => v !== null).map(Math.abs);
  const KW = Math.max(50, Math.ceil(Math.max(50, ...vals) / 50) * 50);
  const yK = v => top + (1 - (v + KW) / (2 * KW)) * (h - top - bot), yS = v => top + (1 - v) * (h - top - bot);
  const band = (z, col) => { if (z.s0 === null || z.s1 === null) return; c.fillStyle = col;
    if (z.s1 >= z.s0) c.fillRect(X(z.s0), top, Math.max(1, X(z.s1) - X(z.s0)), h - top - bot);
    else { c.fillRect(X(z.s0), top, X(1) - X(z.s0), h - top - bot); c.fillRect(X(0), top, X(z.s1) - X(0), h - top - bot); } };
  const vline = (s, col, dash) => { if (s === null || s === undefined) return; c.strokeStyle = col; c.setLineDash(dash ? [3,3] : []);
    c.beginPath(); c.moveTo(X(s), top); c.lineTo(X(s), h - bot); c.stroke(); c.setLineDash([]); };
  for (const z of PROF.zones){
    if (z.k === "sm") band(z, "rgba(57,192,122,.16)"); else if (z.k === "drs"){ band(z, "rgba(79,209,224,.14)"); vline(z.det, "#4fd1e0", true); }
    else if (z.k === "prd") band(z, "rgba(232,67,63,.14)"); else if (z.k === "prs") band(z, "rgba(59,125,221,.14)");
    else if (z.k === "alt") band(z, "rgba(242,169,59,.10)"); else if (z.k === "spd") band(z, "rgba(154,163,178,.08)");
    else if (z.k === "ot"){ vline(z.det, "#b06ef2", true); vline(z.s0, "#b06ef2", false); }
  }
  c.font = "9px sans-serif";
  for (const v of [-KW, -KW/2, 0, KW/2, KW]){ c.strokeStyle = v === 0 ? "#3a4252" : "#232a38"; c.beginPath(); c.moveTo(L, yK(v)); c.lineTo(w - R, yK(v)); c.stroke(); c.fillStyle = "#6b7482"; c.fillText((v > 0 ? "+" : "") + v, 4, yK(v) + 3); }
  for (const p of [0, .5, 1]){ c.fillStyle = "#6b7482"; c.fillText((p*100).toFixed(0) + "%%", w - R + 5, yS(p) + 3); }
  for (let s = 0; s <= 1.0001; s += 0.1){ c.fillStyle = "#6b7482"; c.fillText(s.toFixed(1), X(s) - 6, h - 6); }
  const plot = (arr, y, col, dash, lw) => { if (!arr) return; c.strokeStyle = col; c.lineWidth = lw; c.setLineDash(dash ? [5,4] : []); c.beginPath(); let pen = false;
    for (let k = 0; k < arr.length; k++){ const v = arr[k]; if (v === null){ pen = false; continue; } const x = X((k + .5) / B), yy = y(v); pen ? c.lineTo(x, yy) : c.moveTo(x, yy); pen = true; }
    c.stroke(); c.setLineDash([]); c.lineWidth = 1; };
  if (PROF.player) plot(PROF.player, yK, "#8b93a2", true, 1);
  PROF.arms.forEach((a, i) => { const col = COLORS[i %% COLORS.length]; plot(a.soc, yS, col + "77", true, 1); plot(a.kw, yK, col, false, 1.8); });
  document.getElementById("leg").innerHTML = PROF.arms.map((a, i) => `<span><i style="background:${COLORS[i %% COLORS.length]}"></i>${esc(a.label)} AI kW (${a.laps} laps)</span>`).join("")
    + (PROF.player ? `<span><i style="background:#8b93a2"></i>player ${esc(labels[0])} (dashed)</span>` : "");
})();
</script></body></html>
"""


def write_html(res, arms, path):
    prof = {"bins": arms[0]["blk"]["bins"], "zones": arms[0]["blk"]["zones"],
            "arms": [{"label": a["label"], "kw": a["blk"]["aiProfile"]["kwMean"] if a["blk"]["aiProfile"] else None,
                      "soc": a["blk"]["aiProfile"]["soc"] if a["blk"]["aiProfile"] else None,
                      "laps": a["blk"]["aiProfile"]["laps"] if a["blk"]["aiProfile"] else 0} for a in arms],
            "player": (arms[0]["players"][0].get("kwMean") if arms[0]["players"] else None)}
    page = (HTML % json.dumps(ARM_COLORS)).replace(
        "/*__RES__*/null", json.dumps(res, ensure_ascii=False)).replace(
        "/*__PROF__*/null", json.dumps(prof, ensure_ascii=False))
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logs", nargs="+")
    ap.add_argument("--labels", help="comma-separated arm names, default A,B,C,...")
    ap.add_argument("--out", help="write the markdown report here (also printed)")
    ap.add_argument("--html", help="write a self-contained HTML page with the profile overlay")
    a = ap.parse_args()
    if len(a.logs) < 2:
        sys.exit("give at least two logs to compare")
    try:  # Windows consoles default to a legacy code page; the table uses Δ / ≥ / –
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    t0 = time.time()
    labels = a.labels.split(",") if a.labels else None
    try:
        arms = load_arms(a.logs, labels)
    except (ValueError, FileNotFoundError) as e:
        sys.exit(str(e))
    res = compare(arms)
    md = to_markdown(res)
    print(md)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"markdown: {a.out}")
    if a.html:
        write_html(res, arms, a.html)
        print(f"html: {a.html}")
    print(f"done in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
