"""detectors — derived channels, loss-of-control phases, contacts, episode building.

Input: RaceData (vrclog_parser) + TrackModel (track_model) + thresholds.
Output: Analysis with aligned (T, C) matrices and a list of Episode dicts —
the unit that becomes an incident card in the report.
"""
from __future__ import annotations

import numpy as np
import thresholds as th
from vrclog_parser import OFF_SURFACES


# ---------- small helpers -----------------------------------------------------------------

def runs_of(mask, t, min_dur):
    """Index runs [i0, i1] (inclusive) where mask holds for >= min_dur seconds."""
    out = []
    n = len(mask)
    i = 0
    while i < n:
        if mask[i]:
            j = i
            while j + 1 < n and mask[j + 1]:
                j += 1
            if t[j] - t[i] >= min_dur:
                out.append((i, j))
            i = j + 1
        else:
            i += 1
    return out


def wrap_ds(a, b):
    """Spline distance from b to a going forward (0..1)."""
    return (a - b) % 1.0


class Analysis:
    pass


# ---------- main ---------------------------------------------------------------------------

def analyze(rd, tm):
    an = Analysis()
    an.rd, an.tm = rd, tm
    C = rd.n_cars
    t = rd.F[0]["t"]
    T = len(t)
    if T == 0:
        raise ValueError("no F samples in log — nothing to analyze")
    for ci in range(C):  # parser resamples every car onto one grid (_align_grids)
        if len(rd.F[ci]["t"]) != T:
            raise ValueError(f"car {ci} F grid {len(rd.F[ci]['t'])} != {T} "
                             f"— parser alignment failed")
    an.t, an.T, an.C = t, T, C
    an.absent = list(getattr(rd, "absent_cars", []))
    an.session = rd.meta.get("sessionName", "race")

    def mat(field):
        return np.stack([rd.F[ci][field] for ci in range(C)], axis=1)

    an.spline = mat("spline")
    an.speed = mat("speed")
    an.x, an.z = mat("x"), mat("z")
    an.out = mat("out")
    an.gas, an.brake, an.steer = mat("gas"), mat("brake"), mat("steer")
    an.acc_y = mat("acc_y")
    an.gear = mat("gear")

    vlx, vlz = mat("vlx"), mat("vlz")
    beta = np.degrees(np.arctan2(vlx, np.maximum(np.abs(vlz), 0.1) * np.sign(vlz + 1e-9)))
    beta[an.speed < th.BETA_SPEED_GATE] = 0.0
    an.beta = beta

    nd_f = (mat("nd0") + mat("nd1")) / 2.0
    nd_r = (mat("nd2") + mat("nd3")) / 2.0
    an.nd_f, an.nd_r = nd_f, nd_r

    surf_off = np.stack(
        [np.isin(rd.F[ci]["surf_w"], tuple(OFF_SURFACES)).mean(axis=1) for ci in range(C)],
        axis=1)
    an.off_frac = surf_off

    an.offset = np.stack(
        [tm.lateral_offset(rd.F[ci]["x"], rd.F[ci]["z"], rd.F[ci]["spline"])
         for ci in range(C)], axis=1).astype(np.float32)

    # gap (seconds) to nearest car ahead on track
    ds = wrap_ds(an.spline[:, None, :], an.spline[:, :, None])  # [t, me, other]
    for i in range(C):
        ds[:, i, i] = 1.0
    for j in an.absent:
        ds[:, :, j] = 1.0  # data-less cars sit at spline 0 — never "the car ahead"
    dist_ahead = ds.min(axis=2) * tm.total
    v_ms = np.maximum(an.speed / 3.6, 3.0)
    an.gap_ahead_s = (dist_ahead / v_ms).astype(np.float32)
    an.ahead_car = ds.argmin(axis=2).astype(np.int8)

    # -- pit & caution windows ---------------------------------------------------------------
    an.pit_windows = [[] for _ in range(C)]   # (t0, t1)
    an.pit_stops = []                          # {car, t0, t1, dur}
    opens = {}
    box_open = {}
    for ev in rd.events:
        if ev["type"] == "PIT_IN":
            opens[ev["car"]] = ev["t"]
        elif ev["type"] == "PIT_OUT":
            t0 = opens.pop(ev["car"], None)
            if t0 is not None:
                an.pit_windows[ev["car"]].append((t0, ev["t"]))
        elif ev["type"] == "BOX_IN":
            box_open[ev["car"]] = ev["t"]
        elif ev["type"] == "BOX_OUT":
            t0 = box_open.pop(ev["car"], None)
            if t0 is not None:
                an.pit_stops.append({"car": ev["car"], "t0": t0, "t1": ev["t"],
                                     "dur": ev["t"] - t0})
    for ci, t0 in opens.items():
        an.pit_windows[ci].append((t0, rd.duration + 1))

    an.cautions = []
    c_open = None
    for ev in rd.events:
        if ev["type"] == "FLAG":
            if ev["flag"] == 2 and c_open is None:
                c_open = ev["t"]
            elif ev["flag"] != 2 and c_open is not None:
                an.cautions.append((c_open, ev["t"]))
                c_open = None
    if c_open is not None:
        an.cautions.append((c_open, rd.duration))

    def in_pit_mask(ci):
        m = np.zeros(T, dtype=bool)
        for a, b in an.pit_windows[ci]:
            m |= (t >= a) & (t <= b)
        return m

    # -- per-car loss phases -------------------------------------------------------------------
    phases = []  # {car, kind, t0, t1, i0, i1, s, speed_in, beta_max}
    for ci in range(C):
        pit = in_pit_mask(ci)
        sp = an.speed[:, ci]
        b = np.abs(an.beta[:, ci])
        off_cond = ((an.out[:, ci] >= th.OFF_WHEELS) |
                    (an.off_frac[:, ci] >= th.OFF_SURF_FRAC)) & ~pit
        # gear >= 0 gate: an AI reversing back onto track reads |beta|~180 — not a spin
        spin_cond = (b >= th.SPIN_BETA_DEG) & ~pit & (an.gear[:, ci] >= 0)
        slide_cond = (b >= th.SLIDE_BETA_DEG) & (b < th.SPIN_BETA_DEG) & \
                     (sp >= th.SLIDE_MIN_SPEED) & ~pit

        for i0, i1 in runs_of(off_cond, t, th.OFF_MIN_DUR_S):
            speed_in = sp[i0]
            stat = runs_of(sp[i0:i1 + 1] < th.STUCK_SPEED, t[i0:i1 + 1], th.STUCK_MIN_DUR_S)
            if speed_in < th.OFF_MIN_SPEED and not stat:
                continue
            pre = slice(max(0, i0 - 15), max(1, i0))
            onset_beta = b[max(0, i0 - 3):i0 + 4].max() if i0 else b[i0]
            if onset_beta >= th.OVERSTEER_BETA:
                kind = "off_oversteer"
            elif (onset_beta <= th.UNDERSTEER_BETA
                  and an.nd_f[pre, ci].mean() > th.UNDERSTEER_ND
                  and an.nd_f[pre, ci].mean() > an.nd_r[pre, ci].mean() * th.UNDERSTEER_RATIO):
                kind = "off_understeer"
            else:
                kind = "off"
            phases.append({"car": ci, "kind": kind, "i0": i0, "i1": i1,
                           "t0": t[i0], "t1": t[i1], "s": float(an.spline[i0, ci]),
                           "speed_in": float(speed_in), "beta_max": float(b[i0:i1 + 1].max())})
            for a0, a1 in stat:
                phases.append({"car": ci, "kind": "stuck", "i0": i0 + a0, "i1": i0 + a1,
                               "t0": t[i0 + a0], "t1": t[i0 + a1],
                               "s": float(an.spline[i0 + a0, ci]),
                               "speed_in": 0.0, "beta_max": 0.0})

        for i0, i1 in runs_of(spin_cond, t, th.SPIN_MIN_DUR_S):
            if sp[max(0, i0 - 3)] < th.SPIN_MIN_SPEED:
                continue
            phases.append({"car": ci, "kind": "spin", "i0": i0, "i1": i1,
                           "t0": t[i0], "t1": t[i1], "s": float(an.spline[i0, ci]),
                           "speed_in": float(sp[max(0, i0 - 3)]),
                           "beta_max": float(b[i0:i1 + 1].max())})

        for i0, i1 in runs_of(slide_cond, t, th.SLIDE_MIN_DUR_S):
            near_big = any(p["car"] == ci and p["kind"] != "slide"
                           and p["t0"] - 2.5 <= t[i1] and t[i0] <= p["t1"] + 2.5
                           for p in phases)
            if not near_big:
                phases.append({"car": ci, "kind": "slide", "i0": i0, "i1": i1,
                               "t0": t[i0], "t1": t[i1], "s": float(an.spline[i0, ci]),
                               "speed_in": float(sp[i0]), "beta_max": float(b[i0:i1 + 1].max())})

    # -- contacts (car-car) & wall hits ----------------------------------------------------------
    ec = rd.ev_coll
    car_car = np.where(ec["raw"] > 0)[0]
    grouped = {}
    for k in car_car:
        a, bcar = int(ec["car"][k]), int(ec["other"][k])
        key = (min(a, bcar), max(a, bcar))
        lst = grouped.setdefault(key, [])
        if lst and ec["t"][k] - lst[-1]["t_last"] <= th.CONTACT_GROUP_S:
            g = lst[-1]
            g["t_last"] = float(ec["t"][k])
            g["n"] += 1
            g["rel"] = max(g["rel"], float(ec["rel_speed"][k]))
        else:
            lst.append({"t": float(ec["t"][k]), "t_last": float(ec["t"][k]),
                        "a": a, "b": bcar, "rel": float(ec["rel_speed"][k]),
                        "s": float(ec["spline"][k]), "n": 1})
    an.contacts = []
    for lst in grouped.values():
        for g in lst:
            gi = np.searchsorted(t, g["t"])
            gi = min(gi, T - 1)
            d_ab = wrap_ds(an.spline[gi, g["a"]], an.spline[gi, g["b"]])
            behind, front = (g["b"], g["a"]) if d_ab < 0.5 else (g["a"], g["b"])
            lat = abs(float(an.offset[gi, g["a"]] - an.offset[gi, g["b"]]))
            g["behind"], g["front"] = behind, front
            # same line (small lateral difference) = nose-to-tail hit; a lateral
            # difference around a car's width = side-by-side contact
            g["mode"] = "rear" if lat < th.SIDE_BY_SIDE_M else "side"
            an.contacts.append(g)
    an.contacts.sort(key=lambda g: g["t"])

    wall = np.where((ec["raw"] == 0) & (ec["speed"] < th.SCRAPE_SPEED) &
                    (ec["depth"] >= th.WALL_MIN_DEPTH))[0]
    an.wall_hits = [{"t": float(ec["t"][k]), "car": int(ec["car"][k]),
                     "speed": float(ec["speed"][k]), "s": float(ec["spline"][k])}
                    for k in wall]

    # -- episodes: merge same-car phases, then link across cars ------------------------------------
    phases.sort(key=lambda p: (p["car"], p["t0"]))
    episodes = []
    for p in phases:
        e = episodes[-1] if episodes else None
        if (e and p["car"] in e["cars"] and len(e["cars"]) == 1
                and p["t0"] - e["t1"] <= th.MERGE_GAP_S):
            e["t1"] = max(e["t1"], p["t1"])
            e["phases"].append(p)
        else:
            episodes.append({"cars": {p["car"]}, "t0": p["t0"], "t1": p["t1"],
                             "phases": [p], "contacts": []})

    # attach contacts to episodes (and create contact-only episodes for real hits)
    for g in an.contacts:
        hit = []
        for e in episodes:
            if (g["a"] in e["cars"] or g["b"] in e["cars"]) and \
                    e["t0"] - th.CONTACT_LINK_S <= g["t"] <= e["t1"] + th.CONTACT_LINK_S:
                e["contacts"].append(g)
                hit.append(e)
        if not hit and g["rel"] >= 15.0:
            episodes.append({"cars": {g["a"], g["b"]}, "t0": g["t"], "t1": g["t_last"],
                             "phases": [], "contacts": [g]})

    # union: merge episodes sharing a contact / pileup proximity
    def overlaps(e1, e2):
        if e1 is e2:
            return False
        shared = any(g in e2["contacts"] for g in e1["contacts"])
        if shared:
            return True
        near_t = e1["t0"] - th.PILEUP_WINDOW_S <= e2["t1"] and \
                 e2["t0"] - th.PILEUP_WINDOW_S <= e1["t1"]
        if not near_t:
            return False
        s1 = [p["s"] for p in e1["phases"]] or [g["s"] for g in e1["contacts"]]
        s2 = [p["s"] for p in e2["phases"]] or [g["s"] for g in e2["contacts"]]
        if not s1 or not s2:
            return False
        d = wrap_ds(np.median(s1), np.median(s2))
        return min(d, 1 - d) <= th.PILEUP_SPLINE

    merged = True
    while merged:
        merged = False
        for i in range(len(episodes)):
            for j in range(i + 1, len(episodes)):
                e1, e2 = episodes[i], episodes[j]
                if overlaps(e1, e2) and (e1["cars"] & e2["cars"] or
                                         any(g["a"] in e1["cars"] or g["b"] in e1["cars"]
                                             for g in e2["contacts"]) or
                                         any(g["a"] in e2["cars"] or g["b"] in e2["cars"]
                                             for g in e1["contacts"]) or
                                         len(e1["cars"] | e2["cars"]) >= 3):
                    e1["cars"] |= e2["cars"]
                    e1["t0"] = min(e1["t0"], e2["t0"])
                    e1["t1"] = max(e1["t1"], e2["t1"])
                    e1["phases"] += e2["phases"]
                    e1["contacts"] = list({id(g): g for g in e1["contacts"] + e2["contacts"]}.values())
                    episodes.pop(j)
                    merged = True
                    break
            if merged:
                break

    # attach wall hits (street barriers etc.) to their episodes: consumed by
    # attribution as evidence and by the severity model below
    for e in episodes:
        e["walls"] = [w for w in an.wall_hits
                      if w["car"] in e["cars"]
                      and e["t0"] - 2.0 <= w["t"] <= e["t1"] + th.CONTACT_LINK_S]

    # attach DNFs
    an.dnfs = []
    for ev in rd.events:
        if ev["type"] != "RETIRE":
            continue
        ci = ev["car"]
        an.dnfs.append(ci)
        # attach to this car's NEAREST episode ending before (or spanning) the retire moment
        home, best_gap = None, th.DNF_LOOKBACK_S + 1
        for e in episodes:
            if ci not in e["cars"]:
                continue
            if e["t0"] - 5 <= ev["t"] <= e["t1"] + th.DNF_LOOKBACK_S:
                gap = max(0.0, ev["t"] - e["t1"])
                if gap < best_gap:
                    best_gap, home = gap, e
        if home is None:
            home = {"cars": {ci}, "t0": ev["t"] - 1, "t1": ev["t"], "phases": [],
                    "contacts": [], "walls": []}
            episodes.append(home)
        ti = min(np.searchsorted(t, ev["t"]), T - 1)
        # location from ~2 s BEFORE the retire tick: stuck-AI DNFs get teleported
        # to the pit box on the same tick, so the spline AT retire is the pits
        ti_pre = min(np.searchsorted(t, ev["t"] - 2.0), T - 1)
        home["phases"].append({"car": ci, "kind": "dnf", "t0": ev["t"], "t1": ev["t"],
                               "i0": ti, "i1": ti,
                               "s": float(an.spline[ti_pre, ci]),
                               "speed_in": 0.0, "beta_max": 0.0})
        home["t1"] = max(home["t1"], ev["t"])

    # finalize: severity, location, title
    KIND_SEV = {"slide": 15, "off": 40, "off_understeer": 42, "off_oversteer": 45,
                "spin": 55, "stuck": 70, "dnf": 85}
    for e in episodes:
        for g in e["contacts"]:  # contact partners are involved even without a loss phase
            e["cars"] |= {g["a"], g["b"]}
        e["cars"] = sorted(e["cars"])
        kinds = [p["kind"] for p in e["phases"]]
        sev = max([KIND_SEV[k] for k in kinds], default=0)
        if e["contacts"]:
            sev = max(sev, 35) + min(30, max(g["rel"] for g in e["contacts"]) / 2)
        if e.get("walls"):
            sev = max(sev, 42)
        if len(e["cars"]) >= 3:
            sev += 10
        fast = [p["speed_in"] for p in e["phases"] if p["speed_in"] > 150]
        if fast:
            sev += 8
        e["severity"] = min(100.0, sev)
        locs = [p["s"] for p in e["phases"] if p["kind"] not in ("dnf",)] or \
               [g["s"] for g in e["contacts"]] or \
               [p["s"] for p in e["phases"]] or [0.0]
        e["s"] = float(np.median(locs))
        e["corner"] = tm.corner_at(e["s"])
        e["cornerEn"] = tm.corner_at(e["s"], "en")
        e["corner_key"] = tm.corner_key(e["s"])
        e["lap"] = _lap_at(rd, e["cars"][0], e["t0"])
    episodes = [e for e in episodes if e["phases"] or e["contacts"]]
    episodes.sort(key=lambda e: e["t0"])
    for i, e in enumerate(episodes):
        e["id"] = i
    an.episodes = episodes

    # corner hotspots: DISTINCT episodes with an AI loss of control, grouped by
    # corner via a language-independent key (approach/apex/exit count together)
    an.corner_fail = {}
    for e in episodes:
        if any((p["kind"].startswith("off") or p["kind"] in ("spin", "stuck"))
               and rd.is_ai(p["car"]) for p in e["phases"]):
            c, _ = tm.nearest_corner(e["s"])
            label = tm._corner_label(c) if c is not None else e["corner"]
            slot = an.corner_fail.setdefault(e["corner_key"], {"label": label, "ids": set()})
            slot["ids"].add(e["id"])
    return an


def _lap_at(rd, ci, tt):
    n = 1
    for l in rd.laps_of(ci):
        if l["t"] <= tt:
            n = l["lap_n"] + 1
    return n


if __name__ == "__main__":
    import sys
    import vrclog_parser, track_model
    rd = vrclog_parser.parse(sys.argv[1])
    tm = track_model.load_fast_lane(sys.argv[2])
    tm, med = track_model.pick_z_sign(tm, rd)
    tm.load_corners(sys.argv[3])
    an = analyze(rd, tm)
    print(f"episodes={len(an.episodes)} contacts={len(an.contacts)} "
          f"wall_hits={len(an.wall_hits)} cautions={len(an.cautions)} dnfs={an.dnfs}")
    for e in an.episodes:
        cars = ",".join(f"{c}:{rd.driver(c)[:12]}" for c in e["cars"])
        kinds = "+".join(sorted({p['kind'] for p in e["phases"]})) or "contact"
        print(f"  #{e['id']:2d} t={e['t0']:6.1f}-{e['t1']:6.1f}s lap{e['lap']} "
              f"sev={e['severity']:3.0f} {e['corner']:28s} [{kinds}] "
              f"contacts={len(e['contacts'])} cars({len(e['cars'])})={cars}")
