"""vrclog_parser — schema-1 / schema-2 VRC Race Logger txt -> numpy RaceData.

Parsing contract: docs/log_format_spec.md (keep in lockstep).
Known schema-1 quirks handled here:
  * LAP valid/cuts are a race-session artifact -> parsed but flagged unreliable.
  * V1.0 files: raw=0 COLL flood from floor scrapes -> classified (impact vs scrape)
    downstream in detectors, parser keeps everything.
  * Salvaged files may lack a proper END line; .parts dirs are accepted as input.
Schema 2 (logger V1.4) adds the energy telemetry: per-car E stream (change-only, so
readers hold the last value), DEPLOY/HARVEST/SM/OT events, ELAP per-lap summaries, and
the ZONES / ENERGY header lines. Schema-1 logs simply have none of it (rd.E empty).
Known schema-2 quirk handled here:
  * Logger 1.4 ELAP depMJ/regMJ can repeat the previous lap's total (the CAN counters
    reset a frame or two after lapCount) -> rebuilt from the E stream, see _repair_elap_carry.
"""
from __future__ import annotations

import json
import os
import re
import numpy as np

WHEEL_NAMES = ("FL", "FR", "RL", "RR")
SURFACE_NAMES = {0: "asphalt", 1: "extraturf", 2: "grass", 3: "gravel",
                 4: "kerb", 5: "old", 6: "sand", 7: "ice", 8: "snow"}
# surfaces that mean "off the intended racing surface" for detection purposes
OFF_SURFACES = frozenset((1, 2, 3, 6, 7, 8))

F_FIELDS = ("t", "x", "y", "z", "compass", "speed", "gas", "brake", "steer",
            "gear", "vlx", "vlz", "yaw_rate", "acc_x", "acc_y", "acc_z",
            "nd0", "nd1", "nd2", "nd3", "out", "surf", "spline")
S_FIELDS = ("t", "fuel", "tc0", "tc1", "tc2", "tc3", "pr0", "pr1", "pr2", "pr3",
            "wear0", "wear1", "wear2", "wear3", "dmg0", "dmg1", "dmg2", "dmg3",
            "dmg4", "engine_life", "gearbox_dmg", "race_pos", "lap", "flags",
            "kers", "flat_max", "sd0", "sd1", "sd2", "sd3", "compound")
W_FIELDS = ("t", "air", "road", "grip", "rain", "wet", "wind_kmh", "wind_dir", "flag")
# schema 2 E stream; blank fields (native-only cars) -> NaN for floats, -1 for ints
E_FIELDS = ("t", "kw", "soc", "dep_mj", "reg_mj", "kin", "regen", "max_kw", "max_kw_lim",
            "strat", "split", "latch", "pu_mode", "flags")
E_INT_FIELDS = frozenset(("strat", "split", "latch", "pu_mode", "flags"))

# S.flags bitmask
FLAG_PITLANE, FLAG_PITBOX, FLAG_RETIRED, FLAG_FINISHED = 1, 2, 4, 8
FLAG_AI_PITTING, FLAG_AI_RAIN, FLAG_DRS_AVAIL, FLAG_DRS_ACTIVE = 16, 32, 64, 128
# E.flags bitmask (schema 2)
EFLAG_BOOST, EFLAG_ANTI, EFLAG_OT_ACTIVE, EFLAG_OT_PENDING = 1, 2, 4, 8
EFLAG_PLIM, EFLAG_PLIM_PENDING, EFLAG_CHARGING, EFLAG_SM_OPEN = 16, 32, 64, 128
ENERGY_EVENT_TYPES = frozenset(("DEPLOY", "HARVEST", "SM", "OT"))
# ELAP counter carry-over (logger 1.4, fixed in 1.4.1): see _repair_elap_carry
ELAP_FIXED_IN = (1, 4, 1)
ELAP_CARRY_TOL_MJ = 0.005   # ELAP above the E-stream rebuild by more than this = carried value
ELAP_SETTLE_S = 1.0         # the counters reset within this long after the lap change


class RaceData:
    """Parsed race: meta/cars dicts, per-car F/S numpy channel dicts, W dict, events."""

    def __init__(self):
        self.path = ""
        self.schema = 0
        self.app_version = ""
        self.meta = {}
        self.cars = []              # CAR json dicts, index = car index
        self.n_cars = 0
        self.F = []                 # per car: {field: np.ndarray}; t in SECONDS f64
        self.S = []
        self.W = {}
        self.ev_coll = None         # structured np arrays (see _finish)
        self.ev_lap = []            # list of dicts (splits variable length)
        self.events = []            # every other EV as dicts: {t, type, car, ...}
        self.end = {}               # END json (may be missing on salvaged tails)
        self.duration = 0.0         # seconds, last seen t
        self.start = None           # V1.3+ race start: {green_t, moving, launch:{car:{...}}}
        # schema 2 (logger V1.4) energy telemetry; all empty/None on schema-1 logs
        self.E = []                 # per car: {field: np.ndarray} change-only samples, t in s
        self.energy_cars = []       # per car: True when the E stream carries CAN kW data
        self.ev_elap = []           # per-lap energy summaries (dicts, None for blanks)
        self.elap_repaired = 0      # logger-1.4 ELAP counters rebuilt from the E stream
        self.zones = None           # ZONES header json (layout drs_zones.ini) or None
        self.energy_src = {}        # ENERGY header lines keyed by car ID

    # -- convenience -------------------------------------------------------------
    def driver(self, i):
        return self.cars[i]["driver"] if 0 <= i < self.n_cars else f"car{i}"

    def is_ai(self, i):
        return bool(self.cars[i].get("ai")) if 0 <= i < self.n_cars else True

    def laps_of(self, i):
        return [l for l in self.ev_lap if l["car"] == i]


def _read_text(path):
    """Accept a merged .txt, or a .parts dir (contiguous part_NNNNNN.txt chunks)."""
    if os.path.isdir(path):
        pieces, i = [], 1
        while True:
            p = os.path.join(path, "part_%06d.txt" % i)
            if not os.path.isfile(p):
                break
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                pieces.append(f.read())
            i += 1
        if not pieces:
            raise FileNotFoundError(f"no part_*.txt chunks in {path}")
        return "".join(pieces)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def parse(path):
    text = _read_text(path)
    rd = RaceData()
    rd.path = path

    f_cols = [None]   # per car -> list of row-lists (grown on demand)
    s_cols = [None]
    e_cols = [None]
    w_rows = []
    coll_rows = []
    bad_lines = 0
    nan = float("nan")

    def opt(v):
        return float(v) if v != "" else None

    def car_bucket(store, ci):
        while len(store) <= ci:
            store.append(None)
        if store[ci] is None:
            store[ci] = []
        return store[ci]

    for line in text.split("\n"):
        if not line:
            continue
        kind = line[:line.find(",")] if "," in line else line
        try:
            if kind == "F":
                p = line.split(",")
                ci = int(p[2])
                row = [float(p[1])] + [float(v) for v in p[3:23]] + \
                      [int(p[23], 16), float(p[24])]
                car_bucket(f_cols, ci).append(row)
            elif kind == "S":
                p = line.split(",")
                ci = int(p[2])
                row = [float(p[1])] + [float(v) for v in p[3:33]]
                car_bucket(s_cols, ci).append(row)
            elif kind == "E":
                p = line.split(",")
                ci = int(p[2])
                row = [float(p[1])] + [float(v) if v != "" else nan for v in p[3:16]]
                car_bucket(e_cols, ci).append(row)
            elif kind == "W":
                p = line.split(",")
                w_rows.append([float(v) for v in p[1:10]])
            elif kind == "EV":
                p = line.split(",")
                t, et = float(p[1]) / 1000.0, p[2]
                if et == "COLL":
                    coll_rows.append([t, int(p[3]), int(p[4]), int(p[5]), float(p[6]),
                                      float(p[7]), float(p[8]), float(p[9]), float(p[10]),
                                      float(p[11]), float(p[12]), int(p[13])])
                elif et == "LAP":
                    rd.ev_lap.append({
                        "t": t, "car": int(p[3]), "lap_ms": int(p[4]),
                        "valid_unreliable": int(p[5]), "cuts_unreliable": int(p[6]),
                        "lap_n": int(p[7]), "splits": [int(v) for v in p[8:]],
                    })
                elif et == "FLAG":
                    rd.events.append({"t": t, "type": "FLAG", "flag": int(p[3])})
                elif et in ("PIT_IN", "PIT_OUT", "BOX_IN", "BOX_OUT", "RETIRE"):
                    rd.events.append({"t": t, "type": et, "car": int(p[3])})
                elif et == "FINISH":
                    rd.events.append({"t": t, "type": "FINISH", "car": int(p[3]),
                                      "pos": int(p[4])})
                elif et == "JUMP":
                    rd.events.append({"t": t, "type": "JUMP", "car": int(p[3]),
                                      "reset_n": int(p[4]) if len(p) > 4 else 0})
                elif et == "GREEN":   # V1.3+: race start (green light) moment
                    rd.events.append({"t": t, "type": "GREEN",
                                      "moving": int(p[3]) if len(p) > 3 else 0})
                elif et == "LAUNCH":  # V1.3+: kind 0 = first throttle, 1 = first movement
                    rd.events.append({"t": t, "type": "LAUNCH", "car": int(p[3]),
                                      "kind": int(p[4]), "delta_ms": int(p[5])})
                elif et in ENERGY_EVENT_TYPES:  # schema 2: state samples of the energy FSMs
                    rd.events.append({"t": t, "type": et, "car": int(p[3]), "state": int(p[4]),
                                      "spline": float(p[5]), "speed": float(p[6]),
                                      "soc": float(p[7]), "kw": float(p[8])})
                elif et == "ELAP":    # schema 2: per-lap energy summary (blanks = native car)
                    rd.ev_elap.append({
                        "t": t, "car": int(p[3]), "lap_n": int(p[4]),
                        "dep_mj": opt(p[5]), "reg_mj": opt(p[6]),
                        "soc_line": opt(p[7]), "soc_min": opt(p[8]), "soc_max": opt(p[9]),
                        "deploy_ms": opt(p[10]), "harvest_ms": opt(p[11]), "sm_ms": opt(p[12]),
                        "ot_ms": opt(p[13]), "plim_ms": opt(p[14]),
                    })
                else:
                    rd.events.append({"t": t, "type": et, "raw": p[3:]})
            elif kind == "ZONES":
                rd.zones = json.loads(line[6:])
            elif kind == "ENERGY":
                d = json.loads(line[7:])
                rd.energy_src[d.get("car", "")] = d
            elif kind == "META":
                rd.meta = json.loads(line[5:])
            elif kind == "CAR":
                p = line.split(",", 2)
                ci = int(p[1])
                while len(rd.cars) <= ci:
                    rd.cars.append({})
                rd.cars[ci] = json.loads(p[2])
            elif kind == "VRCLOG":
                p = line.split(",")
                rd.schema, rd.app_version = int(p[1]), p[2]
            elif kind == "END":
                p = line.split(",", 2)
                try:
                    rd.end = json.loads(p[2])
                except Exception:
                    rd.end = {"reason": "unparsed"}
                rd.end["t"] = float(p[1]) / 1000.0
        except Exception:
            bad_lines += 1  # tolerate torn tails (crash chunks)

    if rd.schema not in (1, 2):
        raise ValueError(f"unsupported schema {rd.schema} (expected 1 or 2)")
    rd.n_cars = rd.meta.get("cars", len(rd.cars))
    rd.bad_lines = bad_lines

    # -- to numpy ------------------------------------------------------------------
    def pack(rows, fields, ms_to_s=True):
        if not rows:
            return {k: np.zeros(0, dtype=np.float32) for k in fields}
        a = np.asarray(rows, dtype=np.float64)
        d = {}
        for j, k in enumerate(fields):
            col = a[:, j]
            if k == "t":
                d[k] = (col / 1000.0) if ms_to_s else col
            elif k in ("gear", "out", "race_pos", "lap", "flags", "compound"):
                d[k] = col.astype(np.int32)
            elif k == "surf":
                d[k] = col.astype(np.uint16)
            else:
                d[k] = col.astype(np.float32)
        return d

    def pack_e(rows):
        if not rows:
            return {k: np.zeros(0, dtype=np.float32) for k in E_FIELDS}
        a = np.asarray(rows, dtype=np.float64)
        d = {}
        for j, k in enumerate(E_FIELDS):
            col = a[:, j]
            if k == "t":
                d[k] = col / 1000.0
            elif k in E_INT_FIELDS:
                d[k] = np.where(np.isnan(col), -1, col).astype(np.int32)
            else:
                d[k] = col.astype(np.float32)
        return d

    for ci in range(rd.n_cars):
        rd.F.append(pack(f_cols[ci] if ci < len(f_cols) else None, F_FIELDS))
        rd.S.append(pack(s_cols[ci] if ci < len(s_cols) else None, S_FIELDS))
        rd.E.append(pack_e(e_cols[ci] if ci < len(e_cols) else None))
        rd.energy_cars.append(bool(len(rd.E[ci]["t"]) and np.isfinite(rd.E[ci]["kw"]).any()))
    rd.W = pack(w_rows, W_FIELDS)

    if coll_rows:
        a = np.asarray(coll_rows, dtype=np.float64)
        rd.ev_coll = {
            "t": a[:, 0], "car": a[:, 1].astype(np.int32),
            "raw": a[:, 2].astype(np.int32), "nearest": a[:, 3].astype(np.int32),
            "depth": a[:, 4].astype(np.float32), "speed": a[:, 5].astype(np.float32),
            "near_speed": a[:, 6].astype(np.float32), "rel_speed": a[:, 7].astype(np.float32),
            "x": a[:, 8].astype(np.float32), "z": a[:, 9].astype(np.float32),
            "spline": a[:, 10].astype(np.float32), "lap": a[:, 11].astype(np.int32),
            # verified semantics: raw = other car index + 1, 0 = track
            "other": (a[:, 2].astype(np.int32) - 1),
        }
    else:
        rd.ev_coll = {k: np.zeros(0) for k in
                      ("t", "car", "raw", "nearest", "depth", "speed", "near_speed",
                       "rel_speed", "x", "z", "spline", "lap", "other")}

    ts = [rd.F[i]["t"][-1] for i in range(rd.n_cars) if len(rd.F[i]["t"])]
    rd.duration = float(max(ts)) if ts else 0.0

    # per-wheel surface nibbles, precomputed once (FL,FR,RL,RR)
    for ci in range(rd.n_cars):
        surf = rd.F[ci]["surf"]
        rd.F[ci]["surf_w"] = np.stack(
            [(surf >> 12) & 0xF, (surf >> 8) & 0xF, (surf >> 4) & 0xF, surf & 0xF],
            axis=1).astype(np.uint8) if len(surf) else np.zeros((0, 4), np.uint8)

    _build_start(rd)
    _align_grids(rd)
    _repair_elap_carry(rd)
    return rd


def _build_start(rd):
    """V1.3+ GREEN/LAUNCH events -> rd.start (None on older logs: feature undetectable).

    launch[car] = {react_ms: green->first movement (None = never moved on record),
                   gas_ms: green->first throttle (0 = preloaded at green),
                   jumped: already moving at green (rolling start or jump start)}
    """
    greens = [e for e in rd.events if e["type"] == "GREEN"]
    if not greens:
        return
    g = greens[0]  # one per file by construction; first wins on torn/salvaged concats
    launch = {}
    for e in rd.events:
        if e["type"] != "LAUNCH":
            continue
        d = launch.setdefault(e["car"], {"react_ms": None, "gas_ms": None, "jumped": False})
        if e["kind"] == 0 and d["gas_ms"] is None:
            d["gas_ms"] = e["delta_ms"]
        elif e["kind"] == 1 and d["react_ms"] is None:
            if e["delta_ms"] < 0:
                d["jumped"] = True
            else:
                d["react_ms"] = e["delta_ms"]
    rd.start = {"green_t": g["t"], "moving": g["moving"], "launch": launch}


def _align_grids(rd):
    """Make every car's F channels share one time grid.

    The logger writes all cars in the same tick, so grids are normally identical
    already. Torn tails (crash salvage) or a car inactive for a tick break that,
    and downstream code assumes one shared grid — so resample stragglers to the
    longest car's grid (nearest sample; one tick is ~67 ms) instead of crashing.
    Cars with no F data at all get NaN positions (invisible in the replay) and
    zeroed channels (never trigger detection); both cases are recorded on rd.
    """
    rd.aligned_cars, rd.absent_cars = [], []
    lens = [len(rd.F[ci]["t"]) for ci in range(rd.n_cars)]
    if not lens or max(lens) == 0:
        return
    T = max(lens)
    ref_t = rd.F[int(np.argmax(lens))]["t"]
    for ci in range(rd.n_cars):
        f = rd.F[ci]
        n = len(f["t"])
        if n == T:
            continue
        if n == 0:
            rd.absent_cars.append(ci)
            for k in F_FIELDS:
                if k == "t":
                    f[k] = ref_t.copy()
                elif k in ("x", "y", "z"):
                    f[k] = np.full(T, np.nan, dtype=np.float32)
                else:
                    f[k] = np.zeros(T, dtype=np.float32)
            f["surf_w"] = np.zeros((T, 4), dtype=np.uint8)
        else:
            rd.aligned_cars.append(ci)
            idx = np.clip(np.searchsorted(f["t"], ref_t), 0, n - 1)
            for k in F_FIELDS:
                f[k] = ref_t.copy() if k == "t" else f[k][idx]
            f["surf_w"] = f["surf_w"][idx]


def _version_tuple(v):
    m = re.match(r"(\d+)\.(\d+)(?:\.(\d+))?", v or "")
    return tuple(int(x or 0) for x in m.groups()) if m else (0, 0, 0)


def _counter_at_line(t, v, kw, t0, t1, sign):
    """E-stream value of a per-lap counter at the lap line t1, for the lap that began at t0.

    Rows written in a lap-change frame (t == t0 or t1) may show either lap, so both are left
    out; so are rows before the counter's reset (the first drop within ELAP_SETTLE_S of t0).
    The last remaining row is carried to the line with its kW (sign +1 deploy, -1 harvest):
    the stream writes a row on any 1 kW change, so that kW held until the line.
    """
    idx = np.nonzero((t > t0) & (t < t1) & np.isfinite(v))[0]
    if not len(idx):
        return None
    before = np.nonzero((t <= t0) & np.isfinite(v))[0]
    if len(before):
        prev, k = v[before[-1]], 0
        while k < len(idx) and t[idx[k]] - t0 <= ELAP_SETTLE_S and v[idx[k]] >= prev - 1e-4:
            prev = v[idx[k]]
            k += 1
        # no drop inside the window: nothing reset here (or the lap before ended at 0)
        if k < len(idx) and t[idx[k]] - t0 <= ELAP_SETTLE_S:
            idx = idx[k:]
    last = idx[-1]
    rate = max(sign * float(kw[last]), 0.0) / 1000.0 if np.isfinite(kw[last]) else 0.0
    return max(float(v[idx].max()), float(v[last]) + rate * (t1 - float(t[last])))


def _repair_elap_carry(rd):
    """Logger 1.4 ELAP depMJ/regMJ carry-over repair (bug found 2026-09-23, fixed in 1.4.1).

    The FA26 Pro resets its per-lap counters a frame or two after AC's lapCount changes, and
    logger 1.4 took their maximum over every frame of the new lap, so a lap that ended lower
    than the one before repeated that lap's total (2026-09-22 Silverstone: 29 of 55 laps, the
    AI ones exactly; the player's regen read 8.5 on laps 3 and 5 instead of 8.0). For files
    older than ELAP_FIXED_IN each counter is rebuilt from the E stream (_counter_at_line);
    an ELAP value more than ELAP_CARRY_TOL_MJ above the rebuild is the carried one and is
    replaced (original kept as dep_mj_raw / reg_mj_raw). On that race clean laps matched the
    rebuild within 0.003 MJ and carried values sat 0.013-1.14 MJ above it. A car's first ELAP
    has no lap change before it, and 1.4.1+ files are used as written.
    """
    rd.elap_repaired = 0
    if rd.schema < 2 or _version_tuple(rd.app_version) >= ELAP_FIXED_IN:
        return
    by_car = {}
    for l in rd.ev_elap:
        by_car.setdefault(l["car"], []).append(l)
    for ci, laps in by_car.items():
        if not (0 <= ci < len(rd.E)) or not rd.energy_cars[ci]:
            continue
        e = rd.E[ci]
        laps.sort(key=lambda l: l["t"])
        for prev, cur in zip(laps, laps[1:]):
            for key, sign in (("dep_mj", 1.0), ("reg_mj", -1.0)):
                if cur[key] is None:
                    continue
                est = _counter_at_line(e["t"], e[key], e["kw"], prev["t"], cur["t"], sign)
                if est is not None and cur[key] > est + ELAP_CARRY_TOL_MJ:
                    cur[key + "_raw"] = cur[key]
                    cur[key] = round(est, 3)
                    rd.elap_repaired += 1


def summary(rd):
    lines = [f"file: {os.path.basename(rd.path)}  schema {rd.schema} app {rd.app_version}",
             f"track: {rd.meta.get('trackFull')} ({rd.meta.get('trackLengthM', 0):.0f} m)  "
             f"session: {rd.meta.get('sessionName')}  cars: {rd.n_cars}  "
             f"duration: {rd.duration/60:.1f} min  bad lines: {rd.bad_lines}",
             f"END: {rd.end.get('reason', 'MISSING')}"]
    for ci in range(rd.n_cars):
        f, s = rd.F[ci], rd.S[ci]
        nlaps = len(rd.laps_of(ci))
        dt = np.diff(f["t"]) if len(f["t"]) > 1 else np.zeros(1)
        lines.append(
            f"  car {ci:2d} {rd.driver(ci)[:20]:20s} F={len(f['t']):6d} "
            f"(mono={bool(np.all(dt >= 0))}, medHz={1/np.median(dt) if len(dt) and np.median(dt) > 0 else 0:5.1f}) "
            f"S={len(s['t']):4d} laps={nlaps} "
            f"spline[{f['spline'].min() if len(f['spline']) else 0:.3f},{f['spline'].max() if len(f['spline']) else 0:.3f}]")
    ec = rd.ev_coll
    lines.append(f"  COLL={len(ec['t'])} (car-car={int((ec['raw'] > 0).sum())}) "
                 f"LAP={len(rd.ev_lap)} other EV={len(rd.events)} W={len(rd.W['t'])}")
    if rd.schema >= 2:
        n_e = sum(len(e["t"]) for e in rd.E)
        n_ev = sum(1 for e in rd.events if e["type"] in ENERGY_EVENT_TYPES)
        lines.append(f"  ENERGY: E={n_e} lines, can cars={sum(rd.energy_cars)}/{rd.n_cars}, "
                     f"energy EV={n_ev}, ELAP={len(rd.ev_elap)}, "
                     f"zones={'yes' if rd.zones and rd.zones.get('exists') else 'no'}")
        if rd.elap_repaired:
            lines.append(f"  ELAP: {rd.elap_repaired} carried-over depMJ/regMJ values rebuilt "
                         f"from the E stream (logger {rd.app_version} < 1.4.1)")
    if rd.start:
        reacts = sorted((v["react_ms"], c) for c, v in rd.start["launch"].items()
                        if v["react_ms"] is not None)
        best = f"best {reacts[0][0]} ms ({rd.driver(reacts[0][1])})" if reacts else "no launches"
        lines.append(f"  START green@{rd.start['green_t']:.2f}s "
                     f"moving-at-green={rd.start['moving']} {best}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys, time
    t0 = time.time()
    rd = parse(sys.argv[1])
    print(summary(rd))
    print(f"parsed in {time.time()-t0:.2f}s")
