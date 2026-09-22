"""energy — schema-2 hybrid energy telemetry analysis (report v1.6 "Energy" tab).

Input: a parsed RaceData with the logger V1.4 data (rd.E change-only samples, rd.ev_elap
per-lap summaries, DEPLOY/HARVEST/SM/OT state events in rd.events, rd.zones header).
Output of analyze(): a JSON-ready block, or None when the log carries no energy data
(schema-1 logs) — the report then hides the tab.

Block layout:
  cars[i]      per-car summary + per-lap rows (from ELAP) + kW/SoC-vs-spline profiles
  field        medians over the AI cars' per-car medians (the "what the AI does" baseline)
  aiProfile    pooled AI kW / SoC per spline bin
  zones        the layout's zone list (SM / overtake / power reduction / ...) from ZONES
  events       compact [t, car, kind, state, spline] rows for the timeline panel
Profiles: the change-only E stream is forward-filled onto a 10 Hz grid, joined with the F
stream's spline, cut into laps and interpolated onto BINS bin centres per lap; the reported
line is the median across laps (pooled across cars for the AI baseline). Samples below
30 km/h are dropped, so a car sitting on the grid never floods the start/finish bins.
"""
from __future__ import annotations

import numpy as np

BINS = 200            # spline bins for the profiles (0.5 % of a lap)
RESAMPLE_HZ = 10.0
MOVING_KMH = 30.0     # samples below this speed are left out of the profiles
EVENT_KINDS = {"DEPLOY": 0, "HARVEST": 1, "SM": 2, "OT": 3}


def _unwrapped_spline(f):
    spl = f["spline"].astype(np.float64)
    if len(spl) < 2:
        return spl
    dsp = np.diff(spl)
    steps = np.where(dsp < -0.5, 1.0, np.where(dsp > 0.5, -1.0, 0.0))
    out = spl.copy()
    out[1:] += np.cumsum(steps)
    return out


def hold_last(t_src, v_src, t_grid):
    """Forward-fill change-only samples onto a grid; NaN before the first sample."""
    if len(t_src) == 0:
        return np.full(len(t_grid), np.nan)
    idx = np.searchsorted(t_src, t_grid, side="right") - 1
    vals = v_src[np.clip(idx, 0, len(v_src) - 1)].astype(np.float64)
    vals[idx < 0] = np.nan
    return vals


_hold_last = hold_last


def _request_stats(rd, ci):
    """Deploy REQUEST vs DELIVERY on a 10 Hz hold-last grid (moving samples only).

    Returns (request share of moving time, share of requesting samples whose power cap was
    0 = the strategy withheld the deployment, share of requesting samples that deployed) or
    None. A request is kersInput >= 0.5; deploying is kW >= 10. This is the number that
    separates "the AI does not ask for energy" from "the AI asks and the car says no".
    """
    e, f = rd.E[ci], rd.F[ci]
    if len(e["t"]) < 2 or len(f["t"]) < 2 or not np.isfinite(e["kw"]).any():
        return None
    t0, t1 = float(e["t"][0]), min(float(e["t"][-1]), float(f["t"][-1]))
    if t1 - t0 < 5.0:
        return None
    grid = np.arange(t0, t1, 1.0 / RESAMPLE_HZ)
    kw = hold_last(e["t"], e["kw"], grid)
    kin = hold_last(e["t"], e["kin"], grid)
    cap = hold_last(e["t"], e["max_kw"], grid)
    speed = np.interp(grid, f["t"], f["speed"].astype(np.float64))
    m = (speed >= MOVING_KMH) & np.isfinite(kw) & np.isfinite(kin)
    if m.sum() < 20:
        return None
    req = m & (kin >= 0.5)
    n_req = int(req.sum())
    if n_req == 0:
        return (0.0, None, None)
    deployed = req & (kw >= 10)
    blocked = req & (kw < 10) & (np.nan_to_num(cap, nan=1.0) <= 0)
    return (round(float(n_req / m.sum()), 4),
            round(float(blocked.sum() / n_req), 4),
            round(float(deployed.sum() / n_req), 4))


def _round_list(arr, nd):
    return [None if not np.isfinite(x) else round(float(x), nd) for x in arr]


def _med(values):
    vals = [v for v in values if v is not None and np.isfinite(v)]
    return round(float(np.median(vals)), 3) if vals else None


def _lap_profiles(rd, ci):
    """Per-lap kW / SoC profiles sampled at the bin centres -> (laps x BINS) arrays, or None.

    The change-only E stream is forward-filled onto a 10 Hz grid and joined with the F
    stream's (unwrapped) spline; each lap is then interpolated onto the bin centres, and bins
    farther than two bin widths from any moving sample stay NaN (partial first/last laps, pit
    visits, standing on the grid). Medians are taken across laps afterwards, so a bin never
    mixes one lap's sample with two of another's.
    """
    e, f = rd.E[ci], rd.F[ci]
    if len(e["t"]) < 2 or len(f["t"]) < 2:
        return None
    t0, t1 = float(e["t"][0]), min(float(e["t"][-1]), float(f["t"][-1]))
    if t1 - t0 < 5.0:
        return None
    grid = np.arange(t0, t1, 1.0 / RESAMPLE_HZ)
    kw = _hold_last(e["t"], e["kw"], grid)
    soc = _hold_last(e["t"], e["soc"], grid)
    su = np.interp(grid, f["t"], _unwrapped_spline(f))
    speed = np.interp(grid, f["t"], f["speed"].astype(np.float64))
    moving = speed >= MOVING_KMH
    lapno = np.floor(su).astype(np.int64)
    centers = (np.arange(BINS) + 0.5) / BINS
    kws, socs = [], []
    for ln in np.unique(lapno):
        m = (lapno == ln) & moving
        if m.sum() < 20:
            continue
        s = su[m] - ln
        order = np.argsort(s, kind="stable")
        s = s[order]
        kw_l = np.interp(centers, s, kw[m][order])
        soc_l = np.interp(centers, s, soc[m][order])
        idx = np.searchsorted(s, centers)
        lo = np.clip(idx - 1, 0, len(s) - 1)
        hi = np.clip(idx, 0, len(s) - 1)
        covered = np.minimum(np.abs(centers - s[lo]), np.abs(centers - s[hi])) <= 2.0 / BINS
        kw_l[~covered] = np.nan
        soc_l[~covered] = np.nan
        kws.append(kw_l)
        socs.append(soc_l)
    if not kws:
        return None
    return np.vstack(kws), np.vstack(socs)


def _lap_reduce(mat, fn):
    """nan-reduction over the lap axis without the all-NaN warning."""
    out = np.full(mat.shape[1], np.nan)
    ok = np.isfinite(mat).any(axis=0)
    if ok.any():
        with np.errstate(all="ignore"):
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                out[ok] = fn(mat[:, ok], axis=0)
    return out


def _lap_median(mat):
    return _lap_reduce(mat, np.nanmedian)


def _lap_mean(mat):
    """Time-averaged kW per bin across laps: the right statistic for pulsed deployment
    (the AI's 100-300 ms bursts vanish in a median but carry energy in a mean)."""
    return _lap_reduce(mat, np.nanmean)


def _zones(rd):
    z = rd.zones
    if not z or not z.get("exists") or not isinstance(z.get("sections"), dict):
        return []
    out = []
    for name, sec in z["sections"].items():
        if not isinstance(sec, dict):
            continue
        g = lambda k: sec.get(k)
        sess = str(sec.get("SESSION_TYPE", "ALL"))
        up = name.upper()
        if up == "ZONE_OVERTAKE":
            out.append({"k": "ot", "name": name, "det": g("DETECTION"), "s0": g("START"),
                        "s1": g("END"), "sess": sess, "gap": g("DETECTION_GAP_S")})
        elif up.startswith("ZONE_ALT_POWER_CURVE"):
            out.append({"k": "alt", "name": name, "s0": g("START"), "s1": g("END"), "sess": sess})
        elif up.startswith("ZONE_POWER_REDUCTION"):
            out.append({"k": "prd", "name": name, "s0": g("START"), "s1": g("END"),
                        "sess": sess, "kw": g("POWER_REDUCTION_KW")})
        elif up.startswith("ZONE_POWER_RESET"):
            out.append({"k": "prs", "name": name, "s0": g("START"), "s1": g("END"), "sess": sess})
        elif up.startswith("ZONE_SPEED_THRESHOLD"):
            out.append({"k": "spd", "name": name, "s0": g("START"), "s1": g("END"),
                        "sess": sess, "kmh": g("SPEED_THRESHOLD_KMH")})
        elif up.startswith("ZONE"):
            # plain ZONE_n: 2026 straight-mode zone (START/END[/START_LOW_GRIP]) or, on
            # older layouts, a DRS zone (DETECTION/START/END)
            kind = "drs" if g("DETECTION") is not None else "sm"
            out.append({"k": kind, "name": name, "det": g("DETECTION"), "s0": g("START"),
                        "s1": g("END"), "sess": sess, "lowGrip": g("START_LOW_GRIP")})
    return [zz for zz in out if zz.get("s0") is not None or zz.get("det") is not None]


def analyze(rd):
    if not getattr(rd, "E", None) or not any(len(e["t"]) for e in rd.E):
        return None
    C = rd.n_cars
    ev_by_car = {}
    for e in rd.events:
        if e["type"] in EVENT_KINDS:
            ev_by_car.setdefault(e["car"], []).append(e)
    elap_by_car = {}
    for l in sorted(rd.ev_elap, key=lambda l: l["t"]):
        elap_by_car.setdefault(l["car"], []).append(l)

    cars, ai_kw, ai_soc = [], [], []
    for ci in range(C):
        can = bool(rd.energy_cars[ci]) if ci < len(rd.energy_cars) else False
        f = rd.F[ci]
        laps, prev_t = [], 0.0
        evs = ev_by_car.get(ci, [])
        for l in elap_by_car.get(ci, []):
            t0, t1 = prev_t, l["t"]
            prev_t = t1
            sm_n = sum(1 for e in evs if e["type"] == "SM" and e["state"] == 4 and t0 < e["t"] <= t1)
            ot_n = sum(1 for e in evs if e["type"] == "OT" and e["state"] == 2 and t0 < e["t"] <= t1)
            vmax = None
            if len(f["t"]):
                m = (f["t"] > t0) & (f["t"] <= t1)
                if m.any():
                    vmax = round(float(f["speed"][m].max()), 1)
            ms = lambda k: None if l.get(k) is None else round(l[k] / 1000.0, 1)
            laps.append({"n": l["lap_n"], "t": round(t1, 1),
                         "dep": None if l["dep_mj"] is None else round(l["dep_mj"], 3),
                         "reg": None if l["reg_mj"] is None else round(l["reg_mj"], 3),
                         "socLine": None if l["soc_line"] is None else round(l["soc_line"], 3),
                         "socMin": None if l["soc_min"] is None else round(l["soc_min"], 3),
                         "socMax": None if l["soc_max"] is None else round(l["soc_max"], 3),
                         "deployS": ms("deploy_ms"), "harvestS": ms("harvest_ms"),
                         "smS": ms("sm_ms"), "otS": ms("ot_ms"), "plimS": ms("plim_ms"),
                         "smN": sm_n, "otN": ot_n, "vmax": vmax})
        summary = {
            "laps": len(laps),
            "depMed": _med(l["dep"] for l in laps), "regMed": _med(l["reg"] for l in laps),
            "depTotal": round(sum(l["dep"] for l in laps if l["dep"] is not None), 3) if laps else None,
            "socLineMed": _med(l["socLine"] for l in laps),
            "socMin": min((l["socMin"] for l in laps if l["socMin"] is not None), default=None),
            "socDrift": (round(laps[-1]["socLine"] - laps[0]["socLine"], 3)
                         if len(laps) >= 2 and laps[0]["socLine"] is not None
                         and laps[-1]["socLine"] is not None else None),
            "deploySMed": _med(l["deployS"] for l in laps), "harvestSMed": _med(l["harvestS"] for l in laps),
            "smSMed": _med(l["smS"] for l in laps), "smNMed": _med(l["smN"] for l in laps) if can else None,
            "otN": sum(l["otN"] for l in laps) if can else None,
            "plimSMed": _med(l["plimS"] for l in laps), "vmaxMed": _med(l["vmax"] for l in laps),
        }
        e = rd.E[ci]
        kw_max = float(np.nanmax(e["kw"])) if can and np.isfinite(e["kw"]).any() else None
        kw_min = float(np.nanmin(e["kw"])) if can and np.isfinite(e["kw"]).any() else None
        summary["kwMax"] = None if kw_max is None else round(kw_max, 1)
        summary["kwMin"] = None if kw_min is None else round(kw_min, 1)
        strat = e["strat"][e["strat"] >= 0]
        summary["strat"] = int(np.bincount(strat).argmax()) if len(strat) else None
        rq = _request_stats(rd, ci) if can else None
        summary["reqPct"] = rq[0] if rq else None
        summary["blockedPct"] = rq[1] if rq else None
        summary["deliveredPct"] = rq[2] if rq else None
        prof_kw = prof_kw_mean = prof_soc = None
        lp = _lap_profiles(rd, ci)
        if lp is not None:
            kw_m, soc_m = lp
            if can:
                prof_kw = _round_list(_lap_median(kw_m), 1)
                prof_kw_mean = _round_list(_lap_mean(kw_m), 1)
            prof_soc = _round_list(_lap_median(soc_m), 3)
            if rd.is_ai(ci) and can:
                ai_kw.append(kw_m); ai_soc.append(soc_m)
        cars.append({"i": ci, "profile": "can" if can else ("native" if len(e["t"]) else "none"),
                     "ai": rd.is_ai(ci), "summary": summary, "laps": laps,
                     "kw": prof_kw, "kwMean": prof_kw_mean, "soc": prof_soc,
                     "profLaps": 0 if lp is None else int(lp[0].shape[0])})

    ai_profile = None
    if ai_kw:
        ai_profile = {"kw": _round_list(_lap_median(np.vstack(ai_kw)), 1),
                      "kwMean": _round_list(_lap_mean(np.vstack(ai_kw)), 1),
                      "soc": _round_list(_lap_median(np.vstack(ai_soc)), 3),
                      "cars": len(ai_kw), "laps": int(sum(m.shape[0] for m in ai_kw))}

    def field_med(key):
        return _med(c["summary"].get(key) for c in cars if c["ai"] and c["profile"] == "can")
    field = {k: field_med(k) for k in ("depMed", "regMed", "socLineMed", "socMin", "socDrift",
                                       "deploySMed", "harvestSMed", "smSMed", "smNMed",
                                       "plimSMed", "vmaxMed", "kwMax", "reqPct", "blockedPct",
                                       "deliveredPct")}
    field["cars"] = sum(1 for c in cars if c["ai"] and c["profile"] == "can")

    events = [[round(e["t"], 2), e["car"], EVENT_KINDS[e["type"]], e["state"], round(e["spline"], 4)]
              for e in rd.events if e["type"] in EVENT_KINDS]
    return {
        "bins": BINS,
        "hasCan": any(c["profile"] == "can" for c in cars),
        "energyHz": rd.meta.get("energyHz"),
        "cars": cars,
        "field": field,
        "aiProfile": ai_profile,
        "zones": _zones(rd),
        "events": events,
        "sources": rd.energy_src,
    }


def season_summary(block):
    """Per-race digest for season_report: AI vs player deployment and SoC drift."""
    if not block:
        return None
    players = [c for c in block["cars"] if not c["ai"] and c["profile"] == "can"]
    p = players[0]["summary"] if players else {}
    f = block["field"]
    return {"aiCars": f.get("cars", 0), "aiDep": f.get("depMed"), "aiReg": f.get("regMed"),
            "aiSocDrift": f.get("socDrift"), "aiKwMax": f.get("kwMax"),
            "aiSm": f.get("smSMed"), "aiBlocked": f.get("blockedPct"),
            "plDep": p.get("depMed"), "plReg": p.get("regMed"), "plSocDrift": p.get("socDrift"),
            "plKwMax": p.get("kwMax"), "plSm": p.get("smSMed"), "plBlocked": p.get("blockedPct")}
