"""corner_style — per-corner driving-style comparison: reference driver vs the field.

Rule-based feature extraction over distance-normalized telemetry (same philosophy as
detectors.py: deterministic thresholds, no ML). For every corner in the track's corners
json and every car, each pass through the corner is isolated, cleaned (pit entry,
car-car contact, traffic), and reduced to style metrics:

  braking   — onset point (m before corner entry), onset speed, peak, release point
              (trail depth into the corner); lift point for no-brake corners
  speeds    — entry / min (+where) / exit
  throttle  — first sustained 50% and 90% points, interruption count (gas-lift-gas)
  traction  — power-on rear-slip time (nd>ND_SPIN, gas>GAS_COMMIT), part-throttle
              rear-slip time, body-slip slide time, max |beta|, kerb / off-track time,
              wheels-out max
  pace      — corner segment time between fixed per-corner anchors

Output: per-driver medians + a pooled median for "the rest" (in champ races = the AI),
plus median speed/gas/brake/rear-slip profiles per group for later report embedding.
Multi-log mode diffs the field's medians corner by corner — the measuring instrument
for ai_hints A/B tuning (change one hint, run a race, read the shift in metres).

Usage:
  py corner_style.py <log.txt> [<log2.txt> ...] [--ref <driver>] [--corner <label>]
                     [--json out.json] [--ai path] [--corners path] [--ac-root path]

Notes: works on any session type, but traffic filtering is calibrated for races.
Detection latency of the 15 Hz F grid quantizes distance metrics to ~6 m at 320 km/h;
medians over several laps recover most of it. Compare relative gaps, not absolutes.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys

import numpy as np

import vrclog_parser
import track_model
from detectors import runs_of, wrap_ds
import thresholds as th

HERE = os.path.dirname(os.path.abspath(__file__))

# window construction (bounded by neighbouring corners, see _corner_windows)
APPROACH_MAX_M = 350.0   # how far before s0 the approach window may reach
EXIT_MAX_M     = 250.0   # how far past s1 the exit window may reach
NEIGHBOR_GAP_M = 10.0    # keep this clear of the neighbouring corner's zone
TIME_ANCHOR_M  = 120.0   # corner-time segment: min(this, window) before s0 / after s1

KERB_SURF = 4            # ac.SurfaceExtendedType: kerb
OFF_SURF = (1, 2, 3, 6)  # extraturf, grass, gravel, sand (ice/snow never in league)


def _beta_deg(vlx, vlz, speed):
    """Body slip angle in degrees, gated like detectors (noise below crawl speed)."""
    beta = np.degrees(np.arctan2(vlx, np.maximum(np.abs(vlz), 0.1) * np.sign(vlz + 1e-9)))
    beta[speed < th.BETA_SPEED_GATE] = 0.0
    return beta


def _corner_windows(corners, total):
    """Per corner: approach/exit lengths in spline units, bounded by neighbours."""
    out = []
    n = len(corners)
    for k, c in enumerate(corners):
        prev_s1 = corners[(k - 1) % n]["s1"]
        next_s0 = corners[(k + 1) % n]["s0"]
        gap_appr = wrap_ds(c["s0"], prev_s1) * total - NEIGHBOR_GAP_M
        gap_exit = wrap_ds(next_s0, c["s1"]) * total - NEIGHBOR_GAP_M
        if n == 1:                       # single-corner track (synthetic): split the lap
            gap_appr = gap_exit = total / 4
        appr = max(20.0, min(APPROACH_MAX_M, gap_appr)) / total
        exit_ = max(20.0, min(EXIT_MAX_M, gap_exit)) / total
        out.append({"appr": appr, "exit": exit_,
                    "start": (c["s0"] - appr) % 1.0,
                    "len": appr + wrap_ds(c["s1"], c["s0"]) + exit_})
    return out


def _first_sustained(vals, idxs, hi, lo):
    """First index in idxs where vals crosses hi and holds >= lo on the next sample."""
    for j, k in enumerate(idxs[:-1]):
        if vals[k] > hi and vals[idxs[j + 1]] > lo:
            return k
    return None


def _label(c):
    return f"T{c['n']} {c['name']}".strip() if c.get("name") else f"T{c['n']}"


def _pit_windows(rd):
    """Per car (t0, t1) pit spans, open-ended if PIT_OUT never came (mirrors detectors)."""
    win = [[] for _ in range(rd.n_cars)]
    opens = {}
    for ev in rd.events:
        if ev["type"] == "PIT_IN":
            opens[ev["car"]] = ev["t"]
        elif ev["type"] == "PIT_OUT":
            t0 = opens.pop(ev["car"], None)
            if t0 is not None:
                win[ev["car"]].append((t0, ev["t"]))
    for ci, t0 in opens.items():
        win[ci].append((t0, rd.duration + 1))
    return win


def analyze_corners(rd, tm, ref=None):
    """-> {"ref": idx, "corners": [{label, s0, s1, appr_m, exit_m, passes:[...],
    drivers:{...}, groups:{...}, profiles:{...}}, ...]}  (see module docstring)."""
    L = tm.total
    corners = tm.corners
    if not corners:
        raise ValueError("track model has no corners loaded")
    wins = _corner_windows(corners, L)
    pit = _pit_windows(rd)
    cc = rd.ev_coll
    coll_t = [cc["t"][(cc["car"] == ci) & (cc["raw"] > 0)] if len(cc.get("t", []))
              else np.zeros(0) for ci in range(rd.n_cars)]
    absent = set(getattr(rd, "absent_cars", []))

    if ref is None:
        humans = [ci for ci in range(rd.n_cars) if not rd.is_ai(ci)]
        ref_i = humans[0] if humans else 0
    elif isinstance(ref, int):
        ref_i = ref
    else:
        match = [ci for ci in range(rd.n_cars) if rd.driver(ci).lower() == str(ref).lower()]
        if not match:
            raise ValueError(f"--ref driver not found: {ref} "
                             f"(have: {', '.join(rd.driver(i) for i in range(rd.n_cars))})")
        ref_i = match[0]

    result = {"ref": ref_i, "ref_name": rd.driver(ref_i), "track": rd.meta.get("trackFull"),
              "session": rd.meta.get("sessionName"), "corners": []}

    for k, c in enumerate(corners):
        w = wins[k]
        appr_m = w["appr"] * L
        exit_m = w["exit"] * L
        d_entry = appr_m                                   # window-local metres of s0
        d_exit = appr_m + wrap_ds(c["s1"], c["s0"]) * L    # ... of s1
        d_total = w["len"] * L
        rec = {"label": _label(c), "s0": c["s0"], "s1": c["s1"], "dir": c.get("dir", ""),
               "appr_m": round(appr_m, 1), "exit_m": round(exit_m, 1), "passes": []}

        for ci in range(rd.n_cars):
            if ci in absent:
                continue
            f = rd.F[ci]
            u = wrap_ds(f["spline"], w["start"])           # window-local spline distance
            mask = u < w["len"]
            i = 0
            n = len(mask)
            while i < n:
                if not mask[i]:
                    i += 1
                    continue
                j = i
                while j + 1 < n and mask[j + 1]:
                    j += 1
                seg = slice(i, j + 1)
                i = j + 1
                d = u[seg] * L
                if len(d) < 15 or d.min() > 0.06 * d_total or d.max() < 0.94 * d_total:
                    continue                               # partial coverage (grid, joins)
                o = np.argsort(d, kind="stable")           # monotonic-ify 15 Hz jitter
                d = d[o]
                ch = {x: f[x][seg][o] for x in ("t", "speed", "gas", "brake",
                                                "nd2", "nd3", "vlx", "vlz", "out")}
                sw = f["surf_w"][seg][o]
                p = _analyze_pass(d, ch, sw, d_entry, d_exit, d_total, exit_m)
                grid = np.arange(0.0, d_total, th.CS_PROFILE_GRID_M)
                p["_grid"] = {
                    "speed": np.interp(grid, d, ch["speed"]),
                    "gas": np.interp(grid, d, ch["gas"]),
                    "brake": np.interp(grid, d, ch["brake"]),
                    "nd_rear": np.interp(grid, d, np.maximum(ch["nd2"], ch["nd3"])),
                }
                p["car"] = ci
                p["driver"] = rd.driver(ci)
                p["ai"] = rd.is_ai(ci)
                p["t0"] = float(ch["t"][0])
                p["contact"] = bool(len(coll_t[ci]) and
                                    np.any((coll_t[ci] >= p["t0"] - 2) &
                                           (coll_t[ci] <= float(ch["t"][-1]) + 2)))
                p["in_pit"] = any(a - 5 <= p["t0"] <= b or a <= float(ch["t"][-1]) <= b + 5
                                  for a, b in pit[ci])
                rec["passes"].append(p)

        # second phase: traffic flags need the field's medians for THIS corner
        va = [p["v_appr"] for p in rec["passes"] if not math.isnan(p["v_appr"])]
        vm = [p["v_min"] for p in rec["passes"] if not math.isnan(p["v_min"])]
        med_va = float(np.median(va)) if va else 0.0
        med_vm = float(np.median(vm)) if vm else 0.0
        for p in rec["passes"]:
            p["traffic"] = (p["v_appr"] < th.CS_TRAFFIC_APPR * med_va or
                            p["v_min"] < max(15.0, th.CS_TRAFFIC_VMIN * med_vm))
            p["clean"] = not (p["contact"] or p["in_pit"] or p["traffic"])

        rec["drivers"] = {}
        for p in rec["passes"]:
            rec["drivers"].setdefault(p["driver"], []).append(p)
        rec["stats"] = {drv: _agg(ps) for drv, ps in rec["drivers"].items()}
        ref_ps = [p for p in rec["passes"] if p["car"] == ref_i]
        oth_ps = [p for p in rec["passes"] if p["car"] != ref_i]
        rec["groups"] = {"ref": _agg(ref_ps), "others": _agg(oth_ps)}
        rec["profiles"] = {"ref": _profiles(ref_ps), "others": _profiles(oth_ps),
                           "grid_m": th.CS_PROFILE_GRID_M, "d_entry": round(d_entry, 1),
                           "d_exit": round(d_exit, 1)}
        for p in rec["passes"]:                 # grids served their purpose
            p.pop("_grid", None)
        result["corners"].append(rec)
    return result


def _analyze_pass(d, ch, surf_w, d_entry, d_exit, d_total, exit_m):
    """All style metrics for one pass; distances window-local metres (see analyze_corners)."""
    p = {}
    v = ch["speed"]
    p["v_appr"] = float(np.interp(min(10.0, d[0] + 1), d, v))

    # braking: first sustained press inside [window start, 60% into the corner zone]
    zone_hi = d_entry + 0.6 * (d_exit - d_entry)
    idxs = np.flatnonzero(d <= zone_hi)
    bi = _first_sustained(ch["brake"], idxs, th.CS_BRAKE_ON, th.CS_BRAKE_HOLD)
    if bi is not None:
        p["brake_m"] = float(d_entry - d[bi])          # + = before entry, - = after
        p["v_brake"] = float(v[bi])
        zi = np.flatnonzero((d >= d[bi]) & (d <= d_exit))
        p["brake_peak"] = float(ch["brake"][zi].max()) if len(zi) else float("nan")
        rel = np.flatnonzero((ch["brake"] > 0.10) & (d <= d_exit))
        p["release_m"] = float(d[rel[-1]] - d_entry) if len(rel) else float("nan")
    else:
        p["brake_m"] = p["v_brake"] = p["brake_peak"] = p["release_m"] = float("nan")
        li = _first_sustained(-ch["gas"], idxs, -0.30, -0.50)   # sustained lift instead
        p["lift_m"] = float(d_entry - d[li]) if li is not None else float("nan")
    p["min_gas"] = float(ch["gas"][(d >= d_entry - 50) & (d <= d_exit)].min()
                         if np.any((d >= d_entry - 50) & (d <= d_exit)) else np.nan)

    # speeds
    p["v_entry"] = float(np.interp(d_entry, d, v))
    zc = (d >= d_entry) & (d <= d_exit + 10)
    if zc.any():
        kmin = np.flatnonzero(zc)[np.argmin(v[zc])]
        p["v_min"] = float(v[kmin])
        p["v_min_m"] = float(d[kmin] - d_entry)        # metres past entry
    else:
        p["v_min"], p["v_min_m"], kmin = float("nan"), float("nan"), None
    p["v_exit"] = float(np.interp(min(d_exit + 80, d[-1]), d, v))

    # throttle after the apex: sustained 50% / 90% (m relative to corner end s1,
    # negative = still inside the corner), interruptions between apex and full throttle
    if kmin is not None:
        after = np.flatnonzero(d >= d[kmin])
        g50 = _first_sustained(ch["gas"], after, 0.50, 0.40)
        g90 = _first_sustained(ch["gas"], after, 0.90, 0.80)
        p["gas50_m"] = float(d[g50] - d_exit) if g50 is not None else float("nan")
        p["full_m"] = float(d[g90] - d_exit) if g90 is not None else float("nan")
        stop = d[g90] if g90 is not None else d[-1]
        seg = (d >= d[kmin]) & (d <= stop)
        gs = ch["gas"][seg]
        drops = int(np.sum((gs[:-1] > 0.50) & (gs[1:] < 0.30)))
        p["gas_cuts"] = drops
    else:
        p["gas50_m"] = p["full_m"] = float("nan")
        p["gas_cuts"] = 0

    # traction from apex-ish to window end (the user-visible spin/slide zone)
    z0 = d_entry if kmin is None else max(d_entry, d[kmin] - 20)
    ze = d >= z0
    nd_rear = np.maximum(ch["nd2"][ze], ch["nd3"][ze])  # max: the unloaded inner wheel
    gas = ch["gas"][ze]
    dt = 1.0 / 15.0                                     # F grid step
    p["spin_s"] = float(np.sum((nd_rear > th.CS_ND_SPIN) & (gas > th.CS_GAS_COMMIT)) * dt)
    p["part_spin_s"] = float(np.sum((nd_rear > th.CS_ND_PART) & (gas > 0.15) &
                                    (gas <= th.CS_GAS_COMMIT)) * dt)
    beta = _beta_deg(ch["vlx"][ze], ch["vlz"][ze], ch["speed"][ze])
    p["slide_s"] = float(np.sum((np.abs(beta) > th.CS_SLIDE_DEG) &
                                (ch["speed"][ze] > th.SLIDE_MIN_SPEED)) * dt)
    p["beta_max"] = float(np.abs(beta).max()) if len(beta) else float("nan")
    sw = surf_w[ze]
    p["kerb_s"] = float(np.sum(np.any(sw == KERB_SURF, axis=1)) * dt)
    p["off_s"] = float(np.sum(np.isin(sw, OFF_SURF).any(axis=1)) * dt)
    p["out_max"] = int(ch["out"][ze].max()) if ze.any() else 0

    # segment time between fixed anchors (same for every car at this corner)
    a0 = d_entry - min(TIME_ANCHOR_M, d_entry)
    a1 = min(d_exit + min(TIME_ANCHOR_M, exit_m), d[-1])
    p["time_s"] = float(np.interp(a1, d, ch["t"]) - np.interp(a0, d, ch["t"]))
    return p


def _agg(passes):
    """Medians (+range) over clean passes; counts always over all passes."""
    cl = [p for p in passes if p["clean"]]
    out = {"n": len(passes), "clean": len(cl)}
    if not cl:
        return out
    for key in ("brake_m", "v_brake", "brake_peak", "release_m", "lift_m", "min_gas",
                "v_appr", "v_entry", "v_min", "v_min_m", "v_exit", "gas50_m", "full_m",
                "spin_s", "part_spin_s", "slide_s", "beta_max", "kerb_s", "off_s",
                "time_s"):
        vals = np.array([p[key] for p in cl if not math.isnan(p.get(key, float("nan")))])
        if len(vals):
            out[key] = {"med": float(np.median(vals)), "min": float(vals.min()),
                        "max": float(vals.max()), "n": len(vals)}
    out["gas_cuts"] = float(np.median([p["gas_cuts"] for p in cl]))
    out["out_max"] = max(p["out_max"] for p in cl)
    return out


def _profiles(passes):
    """Median speed/gas/brake/rear-slip vs distance over clean passes (report-ready).

    Values are plain rounded lists so the result dict is json-dumpable as-is.
    """
    cl = [p for p in passes if p["clean"] and "_grid" in p]
    if not cl:
        return None
    out = {"n": len(cl)}
    for key, dg in (("speed", 1), ("gas", 2), ("brake", 2), ("nd_rear", 2)):
        med = np.median(np.vstack([p["_grid"][key] for p in cl]), axis=0)
        out[key] = [round(float(v), dg) for v in med]
    return out


def hints_for(meta, ac_root=None):
    """Active ai_hints.ini sections for the log's track, [] if unresolvable.

    Generic INI scan: every [SECTION] with START= and END= is reported (HINT_*,
    BRAKEHINT_*, DANGER_* all match), VALUE and margin keys carried verbatim.
    """
    ai = track_model.resolve_ai_path(meta, ac_root or track_model.default_ac_root())
    if not ai:
        return []
    path = os.path.join(os.path.dirname(os.path.dirname(ai)), "data", "ai_hints.ini")
    if not os.path.isfile(path):
        return []
    hints, sec, kv = [], None, {}
    def flush():
        if sec and "START" in kv and "END" in kv:
            hints.append({"section": sec, **{k.lower(): v for k, v in kv.items()}})
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.split(";", 1)[0].strip()
        m = re.match(r"\[([^\]]+)\]", line)
        if m:
            flush()
            sec, kv = m.group(1), {}
        elif "=" in line and sec:
            k, v = line.split("=", 1)
            try:
                kv[k.strip().upper()] = float(v.strip())
            except ValueError:
                kv[k.strip().upper()] = v.strip()
    flush()
    return hints


def _overlapping_hints(hints, s0, s1, appr, total):
    """Hints intersecting [s0 - appr, s1] — braking for a corner happens before s0."""
    lo = (s0 - appr) % 1.0
    span = appr + wrap_ds(s1, s0)
    out = []
    for h in hints:
        h0, h1 = h["start"], h["end"]
        if wrap_ds(h0, lo) < span or wrap_ds(h1, lo) < span:
            out.append(h)
    return out


def payload_block(result, hints, total_m):
    """Compact report-ready dict -> payload["cornerStyle"] (see report_template).

    Scalar stats become [med, min, max] triplets; profiles ride along as the rounded
    lists _profiles() built. Per-driver rows only keep drivers with clean passes.
    Hint spans are precomputed to window-local metres so the chart can shade them.
    """
    keep = ("brake_m", "v_brake", "brake_peak", "release_m", "v_entry", "v_min",
            "v_min_m", "v_exit", "gas50_m", "full_m", "spin_s", "part_spin_s",
            "slide_s", "kerb_s", "off_s", "time_s")

    def slim(st):
        if not st or not st.get("clean"):
            return None
        o = {"n": st["n"], "c": st["clean"]}
        for k in keep:
            s = st.get(k)
            if s:
                o[k] = [round(s["med"], 2), round(s["min"], 2), round(s["max"], 2)]
        if "gas_cuts" in st:
            o["cuts"] = st["gas_cuts"]
        return o

    corners = []
    for rec in result["corners"]:
        appr_u = rec["appr_m"] / total_m
        w0 = (rec["s0"] - appr_u) % 1.0
        d_total = rec["profiles"]["d_exit"] + rec["exit_m"]
        hs = []
        for h in _overlapping_hints(hints, rec["s0"], rec["s1"], appr_u, total_m):
            m0 = wrap_ds(h["start"], w0) * total_m
            m1 = wrap_ds(h["end"], w0) * total_m
            if m1 < m0:                                  # wraps out of the window
                m0 = 0.0
            hs.append({"sec": h["section"],
                       "m0": round(max(0.0, min(m0, d_total)), 1),
                       "m1": round(max(0.0, min(m1, d_total)), 1),
                       "txt": " ".join(f"{k}={v}" for k, v in h.items()
                                       if k not in ("section", "start", "end"))})
        prof = rec["profiles"]
        corners.append({
            "label": rec["label"],
            "ref": slim(rec["groups"]["ref"]),
            "field": slim(rec["groups"]["others"]),
            "drivers": {d: s for d, s in
                        ((d, slim(st)) for d, st in rec["stats"].items()) if s},
            "prof": {"ref": prof["ref"], "field": prof["others"],
                     "gridM": prof["grid_m"], "dEntry": prof["d_entry"],
                     "dExit": prof["d_exit"]},
            "hints": hs,
        })
    return {"refName": result["ref_name"], "corners": corners}


# ---------- CLI ------------------------------------------------------------------------

def _fmt(stat, key, unit="", digits=0, signed=False):
    s = stat.get(key)
    if not s:
        return "-"
    v = round(s["med"], digits)
    v = int(v) if digits == 0 else v
    txt = f"{v:+}" if signed else f"{v}"
    return f"{txt}{unit} ({round(s['min'], digits)}..{round(s['max'], digits)})"


def _delta(a, b, key):
    if a.get(key) and b.get(key):
        return a[key]["med"] - b[key]["med"]
    return float("nan")


def print_corner(rec, ref_name, hints, total, full=False):
    g_ref, g_oth = rec["groups"]["ref"], rec["groups"]["others"]
    print(f"\n{'='*98}\n{rec['label']}  [{rec['s0']:.3f}-{rec['s1']:.3f} {rec['dir']}]  "
          f"approach {rec['appr_m']:.0f} m, exit {rec['exit_m']:.0f} m   "
          f"{ref_name} {g_ref.get('clean', 0)}/{g_ref.get('n', 0)} clean, "
          f"field {g_oth.get('clean', 0)}/{g_oth.get('n', 0)}")
    hs = _overlapping_hints(hints, rec["s0"], rec["s1"], rec["appr_m"] / total, total)
    for h in hs:
        extras = {k: v for k, v in h.items() if k not in ("section", "start", "end")}
        print(f"  ai_hint [{h['section']}] {h['start']}-{h['end']} "
              + " ".join(f"{k}={v}" for k, v in extras.items()))
    if not g_ref.get("clean") and not g_oth.get("clean"):
        print("  no clean passes")
        return
    rows = [
        ("brake pt (m before entry)", "brake_m", "m", 0),
        ("brake onset speed (km/h)", "v_brake", "", 0),
        ("brake peak (0-1)", "brake_peak", "", 2),
        ("brake release (m past entry)", "release_m", "m", 0),
        ("entry speed (km/h)", "v_entry", "", 0),
        ("min speed (km/h)", "v_min", "", 0),
        ("min-speed point (m past entry)", "v_min_m", "m", 0),
        ("exit speed (km/h)", "v_exit", "", 0),
        ("gas 50% (m past corner end)", "gas50_m", "m", 0),
        ("gas 90% (m past corner end)", "full_m", "m", 0),
        ("power-on rear slip (s)", "spin_s", "s", 2),
        ("part-throttle rear slip (s)", "part_spin_s", "s", 2),
        ("slide >8deg (s)", "slide_s", "s", 2),
        ("kerb time (s)", "kerb_s", "s", 2),
        ("off-track time (s)", "off_s", "s", 2),
        ("corner time (s)", "time_s", "s", 2),
    ]
    print(f"  {'metric':<32} {ref_name[:18]:>22} {'field median':>22} {'d(ref-field)':>12}")
    for name, key, unit, dg in rows:
        dv = _delta(g_ref, g_oth, key)
        ds = "-" if math.isnan(dv) else f"{dv:+.{dg}f}{unit}"
        print(f"  {name:<32} {_fmt(g_ref, key, unit, dg):>22} "
              f"{_fmt(g_oth, key, unit, dg):>22} {ds:>12}")
    gc_r = g_ref.get("gas_cuts")
    gc_o = g_oth.get("gas_cuts")
    if gc_r is not None or gc_o is not None:
        print(f"  {'throttle interruptions (med)':<32} {gc_r if gc_r is not None else '-':>22} "
              f"{gc_o if gc_o is not None else '-':>22}")
    if full:
        print(f"  {'-'*96}")
        for drv, st in sorted(rec["stats"].items(),
                              key=lambda kv: -(kv[1].get("brake_m", {}) or {}).get("med", -9e9)
                              if kv[1].get("brake_m") else 9e9):
            if not st.get("clean"):
                continue
            print(f"    {drv:<22} n={st['clean']:<3} brake {_fmt(st, 'brake_m', 'm'):>16} "
                  f"vmin {_fmt(st, 'v_min'):>12} gas50 {_fmt(st, 'gas50_m', 'm'):>14} "
                  f"spin {_fmt(st, 'spin_s', 's', 2):>12} time {_fmt(st, 'time_s', 's', 2)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("logs", nargs="+")
    ap.add_argument("--ref", help="reference driver name (default: the human player)")
    ap.add_argument("--corner", help="only print corners whose label contains this")
    ap.add_argument("--full", action="store_true", help="per-driver breakdown per corner")
    ap.add_argument("--json", help="dump full results (all logs) to this path")
    ap.add_argument("--ai")
    ap.add_argument("--corners")
    ap.add_argument("--ac-root", default=track_model.default_ac_root())
    a = ap.parse_args()

    from vrclog_report import prepare_track   # late import: avoids cycle at module load
    results = []
    for path in a.logs:
        rd = vrclog_parser.parse(path)
        tm, _ = prepare_track(rd, a.ai, a.corners, a.ac_root, verbose=False)
        res = analyze_corners(rd, tm, ref=a.ref)
        res["log"] = os.path.basename(path)
        res["total_m"] = tm.total
        results.append((res, rd, tm))
        hints = hints_for(rd.meta, a.ac_root)
        print(f"\n#### {res['log']} -- {res['track']} {res['session']}, "
              f"{rd.n_cars} cars, ref = {res['ref_name']}")
        for rec in res["corners"]:
            if a.corner and a.corner.lower() not in rec["label"].lower():
                continue
            print_corner(rec, res["ref_name"], hints, tm.total, full=a.full)

    if len(results) > 1:
        print(f"\n{'='*98}\nFIELD MEDIAN SHIFTS (later log minus earlier -- the A/B view)")
        base = results[0][0]
        for other, _, _ in results[1:]:
            print(f"\n  {base['log']}  ->  {other['log']}")
            print(f"  {'corner':<26} {'d_brake':>10} {'d_vmin':>8} {'d_gas50':>8} "
                  f"{'d_spin':>8} {'d_time':>8}")
            by_label = {r["label"]: r for r in other["corners"]}
            for rb in base["corners"]:
                ro = by_label.get(rb["label"])
                if not ro:
                    continue
                gb, go = rb["groups"]["others"], ro["groups"]["others"]
                def dd(key, dg=0):
                    v = _delta(go, gb, key)
                    return "-" if math.isnan(v) else f"{v:+.{dg}f}"
                print(f"  {rb['label']:<26} {dd('brake_m'):>9}m {dd('v_min'):>8} "
                      f"{dd('gas50_m'):>7}m {dd('spin_s', 2):>8} {dd('time_s', 2):>7}s")
        print("\n  (d_brake < 0 = the field brakes LATER; d_time < 0 = faster through)")

    if a.json:
        dump = []
        for res, _, _ in results:
            slim = {k: v for k, v in res.items()}
            dump.append(slim)
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(dump, fh, ensure_ascii=False, indent=1, default=float)
        print(f"\njson written: {a.json}")


if __name__ == "__main__":
    main()
