"""vrclog_parser — schema-1 VRC Race Logger txt -> numpy RaceData.

Parsing contract: knowledge_base\\vrc_race_logger_format_spec.md (keep in lockstep).
Known schema-1 quirks handled here:
  * LAP valid/cuts are a race-session artifact -> parsed but flagged unreliable.
  * V1.0 files: raw=0 COLL flood from floor scrapes -> classified (impact vs scrape)
    downstream in detectors, parser keeps everything.
  * Salvaged files may lack a proper END line; .parts dirs are accepted as input.
"""
from __future__ import annotations

import json
import os
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

# S.flags bitmask
FLAG_PITLANE, FLAG_PITBOX, FLAG_RETIRED, FLAG_FINISHED = 1, 2, 4, 8
FLAG_AI_PITTING, FLAG_AI_RAIN, FLAG_DRS_AVAIL, FLAG_DRS_ACTIVE = 16, 32, 64, 128


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
    w_rows = []
    coll_rows = []
    bad_lines = 0

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
                else:
                    rd.events.append({"t": t, "type": et, "raw": p[3:]})
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

    if rd.schema != 1:
        raise ValueError(f"unsupported schema {rd.schema} (expected 1)")
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

    for ci in range(rd.n_cars):
        rd.F.append(pack(f_cols[ci] if ci < len(f_cols) else None, F_FIELDS))
        rd.S.append(pack(s_cols[ci] if ci < len(s_cols) else None, S_FIELDS))
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

    _align_grids(rd)
    return rd


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
    return "\n".join(lines)


if __name__ == "__main__":
    import sys, time
    t0 = time.time()
    rd = parse(sys.argv[1])
    print(summary(rd))
    print(f"parsed in {time.time()-t0:.2f}s")
