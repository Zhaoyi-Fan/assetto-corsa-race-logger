"""report_html — pack analysis into the single-file HTML report.

Replay binary layout (little-endian, per car contiguous, uniform time grid):
  stride 40 bytes/sample:
    0  f32 x            4  f32 z
    8  i16 heading      (rad * 1000, map-space heading from position deltas)
    10 u16 speed        (km/h * 10)
    12 u8  gas (0-255)  13 u8 brake (0-255)
    14 i16 steer        (deg * 10)
    16 i8  gear         (0 N, -1 R)
    17 u8  wheels out
    18 u8[4] ndSlip     (clamped 12.75, * 20)
    22 u16 surf         (4 nibbles FL FR RL RR, ac.SurfaceExtendedType)
    24 i16 beta         (deg * 10)
    26 u16 spline       (s * 65535)
    28 i16 acc_x        (lateral G * 100)
    30 i16 acc_z        (longitudinal G * 100)
    32 i16 kW           (MGU-K power * 10, + deploy / - harvest; schema 2 CAN cars)
    34 u16 soc          (battery 0..1 * 10000)
    36 u8  eflags       1 SM wing open, 2 SM armed, 4 OT active, 8 OT pending,
                        16 power limited, 32 boost, 64 kW valid, 128 SoC valid
    37 u8  latch        (raw straight-mode latch 0-4, 255 = n/a)
    38 2 bytes padding
  Energy fields are the change-only E stream forward-filled onto the grid; schema-1 logs
  carry zeros with the valid bits clear.
JS decodes with a DataView; grid dt is exactly duration/(n-1).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import zlib
import numpy as np

import corner_style
import energy
import thresholds as th

STRIDE = 40
REPLAY_HZ = 15.0
REPORT_VERSION = "1.6"
EF_SM_OPEN, EF_SM_ARMED, EF_OT_ACTIVE, EF_OT_PENDING = 1, 2, 4, 8
EF_PLIM, EF_BOOST, EF_KW_VALID, EF_SOC_VALID = 16, 32, 64, 128


def _thresholds_fingerprint():
    """Short hash of thresholds.py so a shared report says which calibration made it."""
    try:
        with open(th.__file__, "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()[:8]
    except Exception:
        return "?"

PALETTE = ["#e8433f", "#3b7ddd", "#f2a93b", "#39c07a", "#b06ef2", "#e86ab0",
           "#4fd1e0", "#c9d13f", "#f07f2d", "#7f8cff", "#5fe0b0", "#e0e0e0",
           "#a05252", "#52a0a0", "#c98fe8", "#8fb562"]


def _pack_replay(rd, an):
    dur = rd.duration
    n = max(2, int(dur * REPLAY_HZ) + 1)
    grid = np.linspace(0.0, dur, n)
    dt = grid[1] - grid[0]
    C = rd.n_cars
    buf = bytearray(n * C * STRIDE)

    for ci in range(C):
        f = rd.F[ci]
        t = f["t"]

        def rs(a):
            return np.interp(grid, t, a)

        x, z = rs(f["x"]), rs(f["z"])
        dx, dz = np.gradient(x), np.gradient(z)
        heading = np.arctan2(dx, dz)
        sp = rs(f["speed"])
        # hold heading when (nearly) stopped: forward-fill from last moving sample,
        # and backfill the pre-race standstill (position-delta heading is noise on
        # the grid, which pointed every car the same way at lights out)
        moving = sp > 4.0
        if moving.any():
            idx = np.where(moving, np.arange(n), 0)
            np.maximum.accumulate(idx, out=idx)
            heading = heading[idx]
            first = int(np.argmax(moving))
            if first > 0:
                heading[:first] = heading[first]
        beta = np.degrees(np.arctan2(rs(f["vlx"]), np.maximum(np.abs(rs(f["vlz"])), 0.1)))
        beta[sp < 15] = 0
        # absent cars carry NaN positions -> NaN heading/beta; zero them for the
        # int16 cast (their NaN x/z already hide them on the map)
        heading = np.nan_to_num(heading, nan=0.0)
        beta = np.nan_to_num(beta, nan=0.0)

        gear = np.round(rs(f["gear"].astype(np.float64))).astype(np.int8)
        out = np.round(rs(f["out"].astype(np.float64))).astype(np.uint8)
        # nearest-sample for discrete surf (interp would corrupt the nibbles)
        near = np.clip(np.searchsorted(t, grid), 0, len(t) - 1)
        surf = f["surf"][near]
        # spline wraps 1->0 at the line: unwrap before interpolating, else grid
        # points inside the wrap get garbage mid-values (0.27 etc.), which breaks
        # lap slicing in the driving-analysis tab
        spl = f["spline"].astype(np.float64)
        dsp = np.diff(spl)
        steps = np.where(dsp < -0.5, 1.0, np.where(dsp > 0.5, -1.0, 0.0))
        unwrapped = spl.copy()
        unwrapped[1:] += np.cumsum(steps)
        spline_g = np.mod(np.interp(grid, t, unwrapped), 1.0)

        nd = [np.clip(rs(f[k]) * 20, 0, 255).astype(np.uint8)
              for k in ("nd0", "nd1", "nd2", "nd3")]
        acc_x = np.clip(np.nan_to_num(rs(f["acc_x"])) * 100, -31000, 31000).astype(np.int16)
        acc_z = np.clip(np.nan_to_num(rs(f["acc_z"])) * 100, -31000, 31000).astype(np.int16)

        # schema-2 energy: change-only E samples forward-filled onto the replay grid
        e = rd.E[ci] if ci < len(getattr(rd, "E", [])) else None
        if e is not None and len(e["t"]):
            kw_g = energy.hold_last(e["t"], e["kw"], grid)
            soc_g = energy.hold_last(e["t"], e["soc"], grid)
            fl_g = energy.hold_last(e["t"], e["flags"].astype(np.float64), grid)
            la_g = energy.hold_last(e["t"], e["latch"].astype(np.float64), grid)
            soc_ok = np.isfinite(soc_g)
            kw_ok = np.isfinite(kw_g)
            fl = np.nan_to_num(fl_g, nan=0.0).astype(np.int64)
            la = np.nan_to_num(la_g, nan=-1.0).astype(np.int64)
            eflags = (np.where(fl & 128, EF_SM_OPEN, 0) | np.where((la >= 1) & (la <= 3), EF_SM_ARMED, 0)
                      | np.where(fl & 4, EF_OT_ACTIVE, 0) | np.where(fl & 8, EF_OT_PENDING, 0)
                      | np.where(fl & 16, EF_PLIM, 0) | np.where(fl & 1, EF_BOOST, 0)
                      | np.where(kw_ok, EF_KW_VALID, 0) | np.where(soc_ok, EF_SOC_VALID, 0))
            kw_i = np.clip(np.nan_to_num(kw_g, nan=0.0) * 10, -32000, 32000).astype(np.int16)
            soc_i = np.clip(np.nan_to_num(soc_g, nan=0.0) * 10000, 0, 65535).astype(np.uint16)
            latch_i = np.where(la < 0, 255, np.clip(la, 0, 254)).astype(np.uint8)
            eflags = eflags.astype(np.uint8)
        else:
            kw_i = np.zeros(n, np.int16); soc_i = np.zeros(n, np.uint16)
            eflags = np.zeros(n, np.uint8); latch_i = np.full(n, 255, np.uint8)

        rows = zip(x, z,
                   np.clip(heading * 1000, -31000, 31000).astype(np.int16),
                   np.clip(sp * 10, 0, 65535).astype(np.uint16),
                   np.clip(rs(f["gas"]) * 255, 0, 255).astype(np.uint8),
                   np.clip(rs(f["brake"]) * 255, 0, 255).astype(np.uint8),
                   np.clip(rs(f["steer"]) * 10, -31000, 31000).astype(np.int16),
                   gear, out, nd[0], nd[1], nd[2], nd[3], surf,
                   np.clip(beta * 10, -31000, 31000).astype(np.int16),
                   np.clip(spline_g * 65535, 0, 65535).astype(np.uint16),
                   acc_x, acc_z, kw_i, soc_i, eflags, latch_i)
        base = ci * n * STRIDE
        pk = struct.Struct("<ffhHBBhbB4BHhHhhhHBBxx").pack_into
        for k, (xx, zz, hh, ss, g8, b8, st, gr, ot, n0, n1, n2, n3, sf, bt, spn, ax, az,
                ek, es, ef, el) in enumerate(rows):
            pk(buf, base + k * STRIDE, float(xx), float(zz), int(hh), int(ss),
               int(g8), int(b8), int(st), int(gr), int(ot),
               int(n0), int(n1), int(n2), int(n3), int(sf), int(bt), int(spn),
               int(ax), int(az), int(ek), int(es), int(ef), int(el))

    return {"n": n, "dt": dt, "stride": STRIDE, "cars": C}, bytes(buf)


def _round2(arr):
    return [[round(float(a), 1), round(float(b), 1)] for a, b in arr]


def build_payload(rd, an, tm):
    C = rd.n_cars
    cars = []
    for ci in range(C):
        c = rd.cars[ci]
        cars.append({"i": ci, "driver": c.get("driver", f"car{ci}"),
                     "car": c.get("car", ""), "skin": c.get("skin", ""),
                     "ai": bool(c.get("ai")), "aiLevel": c.get("aiLevel", -1),
                     "aiAggression": c.get("aiAggression", -1),
                     "color": PALETTE[ci % len(PALETTE)]})

    # standings: last known race position; retired flagged
    retired = set(an.dnfs)
    start = getattr(rd, "start", None)  # V1.3+ logs only
    launch = start["launch"] if start else {}
    last_pos = []
    for ci in range(C):
        s = rd.S[ci]
        pos = int(s["race_pos"][-1]) if len(s["race_pos"]) else ci + 1
        laps_done = len(rd.laps_of(ci))
        best = min([l["lap_ms"] for l in rd.laps_of(ci)], default=0)
        lc = launch.get(ci, {})
        last_pos.append({"car": ci, "pos": pos, "laps": laps_done, "best": best,
                         "dnf": ci in retired,
                         "react": lc.get("react_ms"), "gas": lc.get("gas_ms"),
                         "jump": bool(lc.get("jumped"))})
    if rd.meta.get("sessionName", "race") == "race":
        results = sorted(last_pos, key=lambda r: (r["dnf"], r["pos"]))
    else:  # quali/practice: race position is meaningless — rank by best lap
        results = sorted(last_pos, key=lambda r: (r["best"] == 0, r["best"]))

    laps = [{"car": l["car"], "n": l["lap_n"], "ms": l["lap_ms"], "t": round(l["t"], 2)}
            for l in rd.ev_lap]
    max_lap = max([l["lap_n"] for l in rd.ev_lap], default=0)
    pos_by_lap = []
    for ln in range(1, max_lap + 1):
        crossers = sorted([l for l in rd.ev_lap if l["lap_n"] == ln], key=lambda l: l["t"])
        pos_by_lap.append([l["car"] for l in crossers])

    episodes = []
    for e in an.episodes:
        i = tm.idx_at(e["s"])
        episodes.append({
            "id": e["id"], "t0": round(e["t0"], 2), "t1": round(e["t1"], 2),
            "lap": e["lap"], "sev": round(e["severity"]),
            "corner": e["corner"], "cornerEn": e.get("cornerEn", e["corner"]),
            "title": e["title"], "titleEn": e.get("titleEn", e["title"]),
            "cars": e["cars"],
            "x": round(float(tm.pts[i, 0]), 1), "z": round(float(tm.pts[i, 2]), 1),
            "chain": [{"car": c["car"], "text": c["text"],
                       "textEn": c.get("textEn", c["text"])} for c in e["chain"]],
            "evidence": [{"car": v["car"], "conf": v["conf"], "text": v["text"],
                          "textEn": v.get("textEn", v["text"])}
                         for v in e["evidence"][:6]],
            "isCard": e["severity"] >= th.CARD_MIN_SEVERITY,
        })

    # corner_fail: {corner_key: {label, ids}} — label is language-independent (T5 Eau Rouge)
    hotspots = sorted([{"corner": v["label"], "cornerEn": v["label"], "n": len(v["ids"])}
                       for v in an.corner_fail.values()], key=lambda h: -h["n"])

    center, left, right = tm.ribbon()
    labels = []
    for c in tm.corners:
        mid = (c["s0"] + c["s1"]) / 2
        i = tm.idx_at(mid)
        labels.append({"n": c["n"], "name": c["name"],
                       "s0": round(float(c["s0"]), 5), "s1": round(float(c["s1"]), 5),
                       "x": round(float(tm.pts[i, 0]), 1), "z": round(float(tm.pts[i, 2]), 1)})
    sf = tm.idx_at(0.0)

    rep_meta, rep_bin = _pack_replay(rd, an)

    # pit stops enriched with lap number and tyre change (S tier compound index)
    def lap_at(ci, tt):
        n = 1
        for l in rd.laps_of(ci):
            if l["t"] <= tt:
                n = l["lap_n"] + 1
        return n

    def compound_at(ci, tt):
        s = rd.S[ci]
        if not len(s["t"]):
            return -1
        i = min(max(0, np.searchsorted(s["t"], tt) - 1), len(s["t"]) - 1)
        return int(s["compound"][i])

    pits = [{"car": p["car"], "t0": round(p["t0"], 1), "t1": round(p["t1"], 1),
             "dur": round(p["dur"], 1), "lap": lap_at(p["car"], p["t0"]),
             "tyreFrom": compound_at(p["car"], p["t0"] - 5.0),
             "tyreTo": compound_at(p["car"], p["t1"] + 8.0)} for p in an.pit_stops]

    # whole-race per-corner style stats (v1.4). Optional by design: a failure here
    # (no corners, odd track, future edge case) must never cost the user the report.
    try:
        cs_res = corner_style.analyze_corners(rd, tm)
        cs_block = corner_style.payload_block(
            cs_res, corner_style.hints_for(rd.meta), tm.total)
    except Exception as e:
        print(f"      note: corner style stats skipped ({type(e).__name__}: {e})")
        cs_block = None

    # hybrid energy telemetry (v1.6, logger V1.4 / schema 2). None on older logs -> the
    # template hides the tab; a failure must not cost the user the rest of the report.
    try:
        energy_block = energy.analyze(rd)
    except Exception as e:
        print(f"      note: energy analysis skipped ({type(e).__name__}: {e})")
        energy_block = None

    payload = {
        "meta": {
            "trackName": rd.meta.get("trackName", ""), "trackFull": rd.meta.get("trackFull", ""),
            "session": rd.meta.get("sessionName", ""), "date": rd.meta.get("date", ""),
            "lapsPlanned": rd.meta.get("laps", 0), "cars": C,
            "durationS": round(rd.duration, 1),
            "air": rd.meta.get("airTemp", 0), "road": rd.meta.get("roadTemp", 0),
            "grip": rd.meta.get("grip", 0), "trackLenM": rd.meta.get("trackLengthM", 0),
            "endReason": rd.end.get("reason", "?"),
            "generator": f"logger v{rd.app_version} (schema {rd.schema}) / "
                         f"report v{REPORT_VERSION} / thr {_thresholds_fingerprint()}",
        },
        "cars": cars,
        "results": results,
        # rolling = most of the field already moving at green: standing-start reaction
        # times don't exist, so the template hides the column entirely
        "start": {"green": round(start["green_t"], 2), "moving": start["moving"],
                  "rolling": start["moving"] >= max(2, C // 2)} if start else None,
        "laps": laps,
        "posByLap": pos_by_lap,
        "pits": pits,
        "weather": {"t": [round(float(x), 1) for x in rd.W["t"]],
                    "grip": [round(float(x), 4) for x in rd.W["grip"]],
                    "rain": [round(float(x), 3) for x in rd.W["rain"]]},
        "cautions": [[round(a, 1), round(b, 1)] for a, b in an.cautions],
        "dnfs": sorted(an.dnfs),
        "dnfT": {str(ev["car"]): round(ev["t"], 1)
                 for ev in rd.events if ev["type"] == "RETIRE"},
        "episodes": episodes,
        "hotspots": hotspots,
        "track": {"center": _round2(center), "left": _round2(left), "right": _round2(right),
                  "labels": labels,
                  "sf": [round(float(tm.pts[sf, 0]), 1), round(float(tm.pts[sf, 2]), 1)]},
        "cornerStyle": cs_block,
        "energy": energy_block,
        "replay": rep_meta,
    }
    return payload, rep_bin


def render(payload, rep_bin, template_path, out_path):
    with open(template_path, "r", encoding="utf-8") as f:
        html = f.read()
    # zlib halves the file; the template inflates it with DecompressionStream("deflate")
    payload["replay"]["compressed"] = True
    html = html.replace("/*__REPORT_JSON__*/null",
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    html = html.replace("__REPLAY_B64__",
                        base64.b64encode(zlib.compress(rep_bin, 6)).decode("ascii"))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path
