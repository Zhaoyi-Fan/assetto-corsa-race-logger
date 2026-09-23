"""End-to-end pipeline test on a fully synthetic race.

Builds a stadium track (two 400 m straights + two 180-degree r=100 corners) as a
v7 fast_lane.ai and a scripted schema-1 log, then asserts parser, track model,
detectors, attribution and report rendering behavior:

  car 0 (player) clean laps + a reverse-gear glitch (|beta|~177 at 35 km/h)
        -> must NOT become a spin episode (gear gate)
  car 1 (AI)     rear-ended by car 2 at t=38 in the first corner, spins, goes
        off onto grass, hits the wall, gets stuck, JUMP+PIT_IN+RETIRE at t=60
        (stuck-AI recovery signature); F lines stop at 60 -> grid alignment
  car 2 (AI)     causes the contact; missing F ticks around t=20 -> alignment
  car 3 (AI)     declared in META but has zero F/S lines -> absent handling

Schema 2 adds an energy scenario on the same race, plus a separate frame-level fixture for
the logger-1.4 ELAP carry-over bug (build_carry_log).

Run:  py tests/test_pipeline.py
"""
import json
import math
import os
import shutil
import struct
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import vrclog_parser
import track_model
import detectors
import attribution
import corner_style
import energy
import energy_compare
import report_html
import thresholds as th

R_ARC, STRAIGHT = 100.0, 400.0
L = 2 * STRAIGHT + 2 * math.pi * R_ARC          # ~1428.3 m
HZ, DT_MS = 15, 67
T_END = 90.0
T_CONTACT, T_OFF, T_STUCK, T_DNF = 38.0, 39.0, 41.0, 60.0
T_REV0, T_REV1 = 15.0, 20.0                      # car 0 reverse-gear window


def pos_at(d):
    s = d % L
    if s < STRAIGHT:
        return -200.0 + s, -100.0
    s -= STRAIGHT
    if s < math.pi * R_ARC:
        a = s / R_ARC
        return 200.0 + R_ARC * math.sin(a), -R_ARC * math.cos(a)
    s -= math.pi * R_ARC
    if s < STRAIGHT:
        return 200.0 - s, 100.0
    s -= STRAIGHT
    a = s / R_ARC
    return -200.0 - R_ARC * math.sin(a), R_ARC * math.cos(a)


def write_ai(path):
    n = 1000
    out = [struct.pack("<iiii", 7, n, 0, 0)]   # 16-byte header: version, count, 2 reserved
    for i in range(n):
        d = L * i / n
        x, z = pos_at(d)
        out.append(struct.pack("<ffffi", x, 0.0, z, d, i))
    out.append(struct.pack("<i", n))
    payload = np.zeros((n, 18), dtype="<f4")
    payload[:, 0] = 50.0            # speed; sides (f5/f6) left at 0 -> fallback path
    out.append(payload.tobytes())
    with open(path, "wb") as f:
        f.write(b"".join(out))


# schema-2 energy scenario (logger V1.4): car 0 (player) and car 2 (AI) publish CAN data,
# car 1 is a native-only car (SoC wobble, no kW), car 3 has no data at all.
# kW along the lap: deploy on both straights from 2 % in (player 350 kW until 20 %,
# AI 200 kW until 12 %), harvest -300 kW through the first 11 % of each corner.
# One straight-mode zone 5–25 % (latch 2 at 3 %, 4 inside), overtake detection 48 % / start
# 50 % used by car 2 on its second lap only, one power-reduction zone 55–65 %.
EN_PEAK = {0: 350.0, 2: 200.0}
EN_END = {0: 0.20, 2: 0.12}
ENERGY_EXPECTED = {}   # car -> [(lap_n, dep_mj, reg_mj)] as written into the ELAP lines


def kw_at(ci, s):
    if ci not in EN_PEAK:
        return None
    for base in (0.0, 0.5):
        if base + 0.02 <= s < base + EN_END[ci]:
            return EN_PEAK[ci]
    for base in (0.28, 0.78):
        if base <= s < base + 0.11:
            return -300.0
    return 0.0


def blocked_at(ci, s):
    """AI car 2 keeps asking for energy after its burst (12-20 % of each straight) but the
    strategy caps the power at 0 -> a 'request blocked' segment for the request statistics."""
    return ci == 2 and any(base + 0.12 <= s < base + 0.20 for base in (0.0, 0.5))


def latch_at(s):
    return 2 if 0.03 <= s < 0.05 else (4 if 0.05 <= s < 0.25 else 0)


def ot_at(ci, s, lap):
    if ci != 2 or lap != 1:
        return 0
    return 1 if 0.48 <= s < 0.5 else (2 if 0.5 <= s < 0.6 else 0)


def build_log(schema=1):
    energy_on = schema >= 2
    lines = [
        "VRCLOG,%d,%s" % (schema, "1.3-test" if schema == 1 else "1.4-test"),
        'META,{"schema":%d,"date":"2026-07-10 12:00:00","track":"stadium",'
        '"trackFull":"stadium/gp","trackName":"Stadium GP","trackLengthM":%.1f,'
        '"sessionIndex":0,"sessionType":3,"sessionName":"race","laps":3,'
        '"durationMin":0,"timedRace":false,"cars":4,"fastHz":15,"slowHz":1,'
        '"weatherEvery":5,"simTime0":0,"systemTime":0,"restart":0,'
        '"airTemp":22.0,"roadTemp":30.0,"grip":0.980,"rain":0.000%s}'
        % (schema, L, ',"energy":true,"energyHz":10' if energy_on else ""),
        'CAR,0,{"driver":"TestPlayer","car":"vrc_fa","skin":"red","ai":false,'
        '"aiLevel":1.0,"aiAggression":0.0,"ballast":0.0,"restrictor":0.0,'
        '"maxFuel":100.0,"compound":2%s}' % (',"energy":"can"' if energy_on else ""),
        'CAR,1,{"driver":"AI Alpha","car":"%s","skin":"blu","ai":true,'
        '"aiLevel":0.97,"aiAggression":0.6,"ballast":0.0,"restrictor":0.0,'
        '"maxFuel":100.0,"compound":2%s}'
        % ("vrc_fa_native" if energy_on else "vrc_fa", ',"energy":"native"' if energy_on else ""),
        'CAR,2,{"driver":"AI Bravo","car":"vrc_fa","skin":"grn","ai":true,'
        '"aiLevel":0.96,"aiAggression":0.7,"ballast":0.0,"restrictor":0.0,'
        '"maxFuel":100.0,"compound":2%s}' % (',"energy":"can"' if energy_on else ""),
        'CAR,3,{"driver":"AI Ghost","car":"vrc_fa","skin":"wht","ai":true,'
        '"aiLevel":0.95,"aiAggression":0.5,"ballast":0.0,"restrictor":0.0,'
        '"maxFuel":100.0,"compound":2%s}' % (',"energy":"can"' if energy_on else ""),
    ]
    if energy_on:
        lines.append(
            'ZONES,{"file":"stadium/gp/data/drs_zones.ini","exists":true,"sections":{'
            '"ZONE_0":{"START":0.05,"END":0.25,"SESSION_TYPE":"ALL"},'
            '"ZONE_OVERTAKE":{"DETECTION":0.48,"START":0.5,"SESSION_TYPE":"ALL","DETECTION_GAP_S":1},'
            '"ZONE_POWER_REDUCTION_0":{"START":0.55,"END":0.65,"POWER_REDUCTION_KW":350,'
            '"SESSION_TYPE":"ALL"}}}')
        lines.append('ENERGY,{"car":"vrc_fa","profile":"can","key":"vrc_fa_CAN","inputs":122,'
                     '"idx":{"kW":162,"kIn":5,"depMJ":139},"error":""}')
        lines.append('ENERGY,{"car":"vrc_fa_native","profile":"native"}')
        ENERGY_EXPECTED.clear()
    dist = [0.0, -12.0, -24.0]           # start offsets (m along the line)
    laps = [0, 0, 0]
    en = {ci: {"dep": 0.0, "reg": 0.0, "soc": 1.0, "socmin": 1.0, "socmax": 1.0,
               "latch": 0, "ot": 0, "deploying": False, "harvesting": False,
               "ms": [0.0] * 5, "last_e": None} for ci in range(3)}

    def emit_energy(t_ms, ci):
        st = en[ci]
        s = (dist[ci] % L) / L
        kw = kw_at(ci, s)
        if ci == 0 and kw is not None and T_REV0 <= t_ms / 1000.0 < T_REV1:
            kw = 0.0   # parked in reverse on the straight: no deployment
        dt_s = 1.0 / HZ
        if kw is None:                      # native car: only the SoC is observable
            st["soc"] = 0.9 + 0.05 * math.sin(t_ms / 1000.0)
        else:
            if kw > 0:
                st["dep"] += kw * dt_s / 1000
            elif kw < 0:
                st["reg"] += -kw * dt_s / 1000
            st["soc"] = min(1.0, max(0.0, st["soc"] - kw * dt_s / 1000 / 4))
        st["socmin"] = min(st["socmin"], st["soc"])
        st["socmax"] = max(st["socmax"], st["soc"])
        if kw is not None:
            ev = lambda kind, state: lines.append(
                "EV,%d,%s,%d,%d,%.5f,%.1f,%.4f,%.1f" % (t_ms, kind, ci, state, s, 180.0, st["soc"], kw))
            dep_on, har_on = kw >= 10, kw <= -10
            if dep_on != st["deploying"]:
                st["deploying"] = dep_on
                ev("DEPLOY", 1 if dep_on else 0)
            if har_on != st["harvesting"]:
                st["harvesting"] = har_on
                ev("HARVEST", 1 if har_on else 0)
            latch = latch_at(s)
            if latch != st["latch"]:
                st["latch"] = latch
                ev("SM", latch)
            ot = ot_at(ci, s, laps[ci])
            if ot != st["ot"]:
                st["ot"] = ot
                ev("OT", ot)
            tick_ms = 1000.0 / HZ
            for j, on in enumerate((st["deploying"], st["harvesting"], latch == 4, ot == 2, False)):
                if on:
                    st["ms"][j] += tick_ms
            flags = (4 if ot == 2 else 0) + (8 if ot == 1 else 0) + (128 if latch == 4 else 0) \
                + (64 if kw < 0 else 0)
            blocked = blocked_at(ci, s)
            key = (round(kw), round(st["soc"] * 100), round(st["dep"] * 10), latch, ot, blocked)
            if key != st["last_e"]:
                st["last_e"] = key
                lines.append("E,%d,%d,%.1f,%.4f,%.3f,%.3f,%.2f,%.2f,%.0f,350,1,%d,%d,1,%d"
                             % (t_ms, ci, kw, st["soc"], st["dep"], st["reg"],
                                1.0 if (kw > 0 or blocked) else 0.0, 0.5 if kw < 0 else 0.0,
                                EN_PEAK[ci] if kw > 0 else 0.0, int(s * 15), latch, flags))
        else:
            key = (round(st["soc"] * 100),)
            if key != st["last_e"]:
                st["last_e"] = key
                lines.append("E,%d,%d,,%.4f,,,0.00,,,,1,,,,64" % (t_ms, ci, st["soc"]))

    def emit_elap(t_ms, ci):
        st = en[ci]
        if ci in EN_PEAK:
            lines.append("EV,%d,ELAP,%d,%d,%.3f,%.3f,%.4f,%.4f,%.4f,%d,%d,%d,%d,%d"
                         % (t_ms, ci, laps[ci], st["dep"], st["reg"], st["soc"],
                            st["socmin"], st["socmax"], *[round(m) for m in st["ms"]]))
        else:
            lines.append("EV,%d,ELAP,%d,%d,,,%.4f,%.4f,%.4f,,,,,"
                         % (t_ms, ci, laps[ci], st["soc"], st["socmin"], st["socmax"]))
        ENERGY_EXPECTED.setdefault(ci, []).append((laps[ci], round(st["dep"], 3), round(st["reg"], 3)))
        st["dep"] = st["reg"] = 0.0
        st["socmin"] = st["socmax"] = st["soc"]
        st["ms"] = [0.0] * 5

    def emit_f(t_ms, ci, d, spd_kmh, gear, beta_deg, out, surf):
        x, z = pos_at(d)
        v = spd_kmh / 3.6
        b = math.radians(beta_deg)
        vlz, vlx = v * math.cos(b), v * math.sin(b)
        if gear < 0:
            vlz = -abs(vlz)
        lines.append(
            "F,%d,%d,%.2f,%.2f,%.2f,%.1f,%.1f,%.3f,%.3f,%.1f,%d,%.2f,%.2f,%.3f,"
            "%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%d,%X,%.5f"
            % (t_ms, ci, x, 0.0, z, 0.0, spd_kmh, 0.8, 0.0, 0.0, gear,
               vlx, vlz, 0.0, 0.0, 0.0, 0.0, 0.5, 0.5, 0.5, 0.5,
               out, surf, (d % L) / L))

    def emit_s(t_ms, ci, pos, lap):
        lines.append(
            "S,%d,%d,%.1f," % (t_ms, ci, 80.0)
            + ",".join(["85.0"] * 4) + "," + ",".join(["27.0"] * 4) + ","
            + ",".join(["0.1000"] * 4) + "," + ",".join(["0.0"] * 5)
            + ",1000,0.000,%d,%d,0,0.500,0.000," % (pos, lap)
            + ",".join(["0.000"] * 4) + ",2")

    n_ticks = int(T_END * HZ)
    for k in range(n_ticks):
        t = k / HZ
        t_ms = int(t * 1000)
        # --- car 0: cruise + reverse-gear glitch (must not read as a spin) ----
        if T_REV0 <= t < T_REV1:
            emit_f(t_ms, 0, dist[0], 35.0, -1, 177.0, 0, 0x0000)
        else:
            dist[0] += 50.0 / HZ
            emit_f(t_ms, 0, dist[0], 180.0, 6, 0.0, 0, 0x0000)
        # --- car 1: contact -> spin -> off -> stuck -> recovered DNF ----------
        if t < T_CONTACT:
            dist[1] += 50.0 / HZ
            emit_f(t_ms, 1, dist[1], 180.0, 6, 0.0, 0, 0x0000)
        elif t < T_OFF:
            v = 180.0 - (t - T_CONTACT) * 120.0      # 180 -> 60 km/h
            dist[1] += v / 3.6 / HZ
            emit_f(t_ms, 1, dist[1], v, 3, 70.0, 0, 0x0000)
        elif t < T_STUCK:
            v = 60.0 - (t - T_OFF) * 26.0            # 60 -> 8 km/h, on grass
            dist[1] += v / 3.6 / HZ
            emit_f(t_ms, 1, dist[1], v, 2, 10.0, 4, 0x2222)
        elif t < T_DNF:
            emit_f(t_ms, 1, dist[1], 2.0, 1, 0.0, 4, 0x2222)
        # after T_DNF: teleported away, no more F lines (tests grid alignment)
        # --- car 2: follows; drops ticks around t=20 (tests alignment) --------
        dist[2] += 50.0 / HZ
        if not (20.0 <= t <= 20.4):
            emit_f(t_ms, 2, dist[2], 180.0, 6, 0.0, 0, 0x0000)
        # --- energy (schema 2 only) ---------------------------------------------
        if energy_on:
            for ci in range(3):
                if ci == 1 and t >= T_DNF:
                    continue
                emit_energy(t_ms, ci)
        # --- laps / S / W ------------------------------------------------------
        for ci in range(3):
            if ci == 1 and t >= T_DNF:
                continue
            if dist[ci] >= (laps[ci] + 1) * L:
                laps[ci] += 1
                lines.append("EV,%d,LAP,%d,%d,1,0,%d,%d,%d"
                             % (t_ms, ci, int(L / 50 * 1000), laps[ci],
                                14000, 14000))
                if energy_on:
                    emit_elap(t_ms, ci)
        if k % HZ == 0:
            for ci in range(3):
                if ci == 1 and t >= T_DNF:
                    continue
                emit_s(t_ms, ci, ci + 1, laps[ci])
        if k % (5 * HZ) == 0:
            lines.append("W,%d,22.0,30.0,0.980,0.000,0.000,5.0,180,%d"
                         % (t_ms, 2 if T_OFF + 1 <= t <= 55.0 else 0))

    # race start (V1.3): green at t=2 s; car 0 preloaded throttle, car 1 jump-started,
    # car 2 best reaction 198 ms, car 3 (absent) no launch data at all
    lines.append("EV,2000,GREEN,1")
    lines.append("EV,2000,LAUNCH,0,0,0")
    lines.append("EV,2000,LAUNCH,1,1,-1")
    lines.append("EV,2150,LAUNCH,2,0,150")
    lines.append("EV,2180,LAUNCH,1,0,180")
    lines.append("EV,2198,LAUNCH,2,1,198")
    lines.append("EV,2260,LAUNCH,0,1,260")

    # events: contact pair, wall hit, caution, stuck-AI recovery DNF
    x1, z1 = pos_at(50.0 * T_CONTACT - 12.0)
    s1 = ((50.0 * T_CONTACT - 12.0) % L) / L
    lines.append("EV,%d,COLL,1,3,2,0.020,178.0,182.0,15.0,%.2f,%.2f,%.5f,1"
                 % (int(T_CONTACT * 1000), x1, z1, s1))
    lines.append("EV,%d,COLL,2,2,1,0.020,182.0,178.0,15.0,%.2f,%.2f,%.5f,1"
                 % (int(T_CONTACT * 1000), x1, z1, s1))
    lines.append("EV,%d,COLL,1,0,2,0.050,100.0,180.0,20.0,%.2f,%.2f,%.5f,1"
                 % (int((T_CONTACT + 0.5) * 1000), x1, z1, s1))
    lines.append("EV,%d,FLAG,2" % int((T_OFF + 1.0) * 1000))
    lines.append("EV,%d,FLAG,0" % 55000)
    t_dnf_ms = int(T_DNF * 1000)
    lines.append("EV,%d,JUMP,1,1" % t_dnf_ms)
    lines.append("EV,%d,PIT_IN,1" % t_dnf_ms)
    lines.append("EV,%d,BOX_IN,1" % t_dnf_ms)
    lines.append("EV,%d,RETIRE,1" % t_dnf_ms)
    lines.append('END,%d,{"reason":"test","lines":%d,"chunks":1}'
                 % (int(T_END * 1000), len(lines)))
    return "\n".join(lines) + "\n"


# ELAP carry-over regression (logger 1.4 bug, found 2026-09-23): the FA26 Pro resets its per-lap
# counters a frame or two after lapCount changes and logger 1.4 took their maximum over every
# frame of the new lap, so a lap that ended lower than the one before repeated that lap's total.
# This fixture replays it frame by frame (60 fps, 12 s laps, an E tick every 6th frame):
#   car 0  counters reset 2 frames late; deploys across every line, so the carried value still
#          climbs in the lag frames; its lap line falls on an E tick (a row in the change frame)
#   car 1  AI-like: idle at the line, so the carried value is exact; its first lag frame falls on
#          an E tick and a -2 kW trickle writes a row there that still shows the old lap
#   car 2  counters reset in the change frame (logger 1.4 was right) + a row in that frame
# ELAP is written the 1.4 way; truth = the counters in the last frame before the change, which
# is what logger 1.4.1 writes.
CARRY_LAP, CARRY_TICK, CARRY_FPS = 720, 6, 60
CARRY_CARS = {0: dict(offset=0, lag=2, dep=[300, 200, 300, 200, 300], reg=[300, 350, 300, 350, 300]),
              1: dict(offset=5, lag=2, dep=[200, 170, 160, 190, 175], reg=[250, 240, 245, 255, 235]),
              2: dict(offset=0, lag=0, dep=[300, 200, 300, 200, 300], reg=[300, 350, 300, 350, 300])}


def carry_kw(ci, lap, k):
    """kW of fixture car ci in frame k of its lap `lap` (AC's lapCount)."""
    c = CARRY_CARS[ci]
    if ci == 1:
        if 150 <= k < 270:
            return float(c["dep"][lap])
        if 400 <= k < 520:
            return -float(c["reg"][lap])
        return -2.0 if lap > 0 and k <= c["lag"] else 0.0
    if k >= CARRY_LAP - 30 or (lap > 0 and k < 30):
        return 300.0
    if 120 <= k < 300:
        return float(c["dep"][lap])
    if 420 <= k < 540:
        return -float(c["reg"][lap])
    return 0.0


def build_carry_log(app_version, laps=5):
    """-> (log text, truth {(car, lapCount): (depMJ, regMJ)}) for the carry-over fixture."""
    q = lambda x: math.floor(x + 0.5)   # the logger's change-key rounding
    lines = [
        "VRCLOG,2,%s" % app_version,
        'META,{"schema":2,"date":"2026-09-23 12:00:00","track":"stadium","trackFull":"stadium/gp",'
        '"trackName":"Stadium GP","trackLengthM":%.1f,"sessionIndex":0,"sessionType":3,'
        '"sessionName":"race","laps":%d,"durationMin":0,"timedRace":false,"cars":%d,"fastHz":15,'
        '"slowHz":1,"weatherEvery":5,"simTime0":0,"systemTime":0,"restart":0,"airTemp":22.0,'
        '"roadTemp":30.0,"grip":1.000,"rain":0.000,"energy":true,"energyHz":10}'
        % (L, laps, len(CARRY_CARS))]
    for ci in CARRY_CARS:
        lines.append('CAR,%d,{"driver":"Carry %d","car":"vrc_fa","skin":"s","ai":%s,"aiLevel":1.0,'
                     '"aiAggression":0.2,"ballast":0.0,"restrictor":0.0,"maxFuel":100.0,'
                     '"compound":1,"energy":"can"}' % (ci, ci, "false" if ci == 0 else "true"))
    st = {ci: {"dep": 0.0, "reg": 0.0, "soc": 1.0, "clap": 0, "lap": 0, "mx": [0.0, 0.0],
               "prev": (0.0, 0.0), "key": None, "wt": -1e9} for ci in CARRY_CARS}
    truth = {}
    for fr in range(laps * CARRY_LAP + 10):
        t = round(fr * 1000 / CARRY_FPS)
        for ci, c in CARRY_CARS.items():
            s = st[ci]
            g = fr - c["offset"]
            lap = max(0, g // CARRY_LAP)
            clap = max(0, (g - c["lag"]) // CARRY_LAP)
            if clap != s["clap"]:                     # the car's own counter reset
                s["dep"], s["reg"], s["clap"] = 0.0, 0.0, clap
            kw = carry_kw(ci, lap, max(0, g) - lap * CARRY_LAP)
            if kw > 0:
                s["dep"] += kw / CARRY_FPS / 1000
            else:
                s["reg"] += -kw / CARRY_FPS / 1000
            s["soc"] = min(1.0, max(0.0, s["soc"] - kw / CARRY_FPS / 1000 / 4))
            if lap != s["lap"]:                       # AC lap line: ELAP from the maxima so far
                truth[(ci, lap)] = (round(s["prev"][0], 3), round(s["prev"][1], 3))
                lines.append("EV,%d,ELAP,%d,%d,%.3f,%.3f,%.4f,%.4f,%.4f,0,0,0,0,0"
                             % (t, ci, lap, s["mx"][0], s["mx"][1], s["soc"], s["soc"], s["soc"]))
                s["lap"], s["mx"] = lap, [0.0, 0.0]
            s["mx"] = [max(s["mx"][0], s["dep"]), max(s["mx"][1], s["reg"])]  # 1.4: no guard
            s["prev"] = (s["dep"], s["reg"])
            if fr % CARRY_TICK == 0:
                key = (q(kw), q(s["soc"] * 100), q(s["dep"] * 10), q(s["reg"] * 10))
                if key != s["key"] or t - s["wt"] >= 2000:
                    s["key"], s["wt"] = key, t
                    lines.append("E,%d,%d,%.1f,%.4f,%.3f,%.3f,1.00,0.00,350,350,1,0,0,1,0"
                                 % (t, ci, kw, s["soc"], s["dep"], s["reg"]))
    lines.append('END,%d,{"reason":"test","lines":%d,"chunks":1}'
                 % (round((laps * CARRY_LAP + 10) * 1000 / CARRY_FPS), len(lines)))
    return "\n".join(lines) + "\n", truth


FAILS = []


def check(name, cond, detail=""):
    status = "ok  " if cond else "FAIL"
    print(f"  [{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def main():
    tmp = tempfile.mkdtemp(prefix="vrctest_")
    ai_path = os.path.join(tmp, "fast_lane.ai")
    log_path = os.path.join(tmp, "vrclog_test_race.txt")
    write_ai(ai_path)
    text = build_log()
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(text)

    print("parser")
    rd = vrclog_parser.parse(log_path)
    check("schema/cars", rd.schema == 1 and rd.n_cars == 4)
    check("no bad lines", rd.bad_lines == 0, str(rd.bad_lines))
    check("laps parsed", len(rd.ev_lap) >= 6, str(len(rd.ev_lap)))
    check("aligned cars = {1,2}", set(rd.aligned_cars) == {1, 2}, str(rd.aligned_cars))
    check("absent car = [3]", rd.absent_cars == [3], str(rd.absent_cars))
    check("grids equal", len({len(rd.F[ci]['t']) for ci in range(4)}) == 1)

    # .parts directory input parses to the same data
    parts = os.path.join(tmp, "vrclog_test_race.parts")
    os.makedirs(parts)
    cut = text.find("\n", len(text) // 2) + 1
    with open(os.path.join(parts, "part_000001.txt"), "w", encoding="utf-8") as f:
        f.write(text[:cut])
    with open(os.path.join(parts, "part_000002.txt"), "w", encoding="utf-8") as f:
        f.write(text[cut:])
    rd2 = vrclog_parser.parse(parts)
    check(".parts input", len(rd2.F[0]["t"]) == len(rd.F[0]["t"]))

    print("race start (V1.3)")
    st = rd.start
    check("start parsed", st is not None and abs(st["green_t"] - 2.0) < 1e-6
          and st["moving"] == 1, str(st and (st["green_t"], st["moving"])))
    if st:
        check("car0 preloaded gas + react 260",
              st["launch"][0] == {"react_ms": 260, "gas_ms": 0, "jumped": False},
              str(st["launch"].get(0)))
        check("car1 jump-started",
              st["launch"][1]["jumped"] and st["launch"][1]["react_ms"] is None,
              str(st["launch"].get(1)))
        check("car2 best react 198",
              st["launch"][2] == {"react_ms": 198, "gas_ms": 150, "jumped": False},
              str(st["launch"].get(2)))
        check("car3 no launch data", 3 not in st["launch"], str(list(st["launch"])))
    # logs from pre-1.3 loggers (no GREEN/LAUNCH lines) must yield start = None
    old_text = "\n".join(l for l in text.split("\n")
                         if ",GREEN," not in l and ",LAUNCH," not in l)
    old_path = os.path.join(tmp, "vrclog_old_version.txt")
    with open(old_path, "w", encoding="utf-8") as f:
        f.write(old_text)
    check("pre-1.3 log -> start None", vrclog_parser.parse(old_path).start is None)

    print("track model")
    tm = track_model.load_fast_lane(ai_path)
    tm, med = track_model.pick_z_sign(tm, rd)
    check("z-sign check finite", med < 3.0, f"med={med:.2f}")
    check("sides fallback", not tm.sides_ok and float(tm.side_l[0]) == 5.0)
    segs = tm.detect_segments()
    check("2 corners on stadium", len(segs) == 2, str(len(segs)))
    tm.corners = [{"n": str(i + 1), "name": "", "s0": round(c["s0"], 4),
                   "s1": round(c["s1"], 4), "dir": c["dir"]} for i, c in enumerate(segs)]
    tm.straights = []

    print("detectors")
    an = detectors.analyze(rd, tm)
    car0_loss = [e for e in an.episodes
                 if any(p["car"] == 0 for p in e["phases"])]
    check("reverse gear is not a spin", not car0_loss,
          str([[p["kind"] for p in e["phases"]] for e in car0_loss]))
    inc = [e for e in an.episodes if 1 in e["cars"]]
    check("car1 episode exists", len(inc) == 1, str(len(inc)))
    e = inc[0] if inc else None
    if e:
        kinds = {p["kind"] for p in e["phases"]}
        check("spin+off+stuck+dnf merged",
              {"spin", "stuck", "dnf"} <= kinds and any(k.startswith("off") for k in kinds),
              str(kinds))
        check("contact car joined", 2 in e["cars"], str(e["cars"]))
        check("wall attached", len(e.get("walls", [])) >= 1)
        check("severity >= 42", e["severity"] >= 42, str(e["severity"]))
        check("located in a corner", e["corner"].startswith("T"), e["corner"])
        check("corner key stable", e["corner_key"].startswith("T"), e["corner_key"])
    check("one grouped rear contact",
          len(an.contacts) == 1 and an.contacts[0]["mode"] == "rear"
          and an.contacts[0]["behind"] == 2,
          str(an.contacts))
    check("caution window", len(an.cautions) == 1
          and abs(an.cautions[0][0] - (T_OFF + 1)) < 0.5, str(an.cautions))
    check("dnf recorded", an.dnfs == [1], str(an.dnfs))
    check("AI hotspot keyed by corner",
          any(k.startswith("T") for k in an.corner_fail), str(list(an.corner_fail)))

    print("attribution")
    attribution.attribute(an)
    if e:
        tags = {v["tag"] for v in e["evidence"]}
        check("contact evidence", "contact" in tags, str(tags))
        check("wall evidence", "wall" in tags, str(tags))
        texts = " | ".join(c["textEn"] for c in e["chain"])
        check("stuck-recovery DNF narrative", "auto-recovered" in texts, texts)
        check("bilingual title", bool(e["title"]) and bool(e["titleEn"]))

    print("corner_style")
    cs = corner_style.analyze_corners(rd, tm)
    check("two corner records", len(cs["corners"]) == 2, str(len(cs["corners"])))
    check("ref = the human player", cs["ref"] == 0 and cs["ref_name"] == "TestPlayer")
    all_passes = [p for r in cs["corners"] for p in r["passes"]]
    check("corner passes found", len(all_passes) >= 8, str(len(all_passes)))
    check("absent car -> no passes", all(p["car"] != 3 for p in all_passes))
    check("no-brake log -> brake stats absent, no crash",
          all("brake_m" not in r["groups"]["others"] for r in cs["corners"]))
    oth = cs["corners"][0]["groups"]["others"]
    check("cruise v_min ~ 180", bool(oth.get("v_min"))
          and abs(oth["v_min"]["med"] - 180.0) < 3.0,
          str(oth.get("v_min")))
    check("glitch/crash passes flagged dirty",
          any(not p["clean"] for p in all_passes),
          "all passes clean?!")
    n1 = sum(1 for p in all_passes if p["car"] == 1)
    n2 = sum(1 for p in all_passes if p["car"] == 2)
    check("DNF car has fewer passes than survivor", n1 < n2, f"car1={n1} car2={n2}")
    prof = cs["corners"][0]["profiles"]["others"]
    check("profiles: 4 channels, one length, >30 pts",
          prof is not None
          and len({len(prof[k]) for k in ("speed", "gas", "brake", "nd_rear")}) == 1
          and len(prof["speed"]) > 30,
          str(prof and {k: len(v) for k, v in prof.items() if isinstance(v, list)}))

    print("report")
    payload, rep_bin = report_html.build_payload(rd, an, tm)
    check("weather in payload", len(payload["weather"]["t"]) >= 18)
    check("results has 4 cars", len(payload["results"]) == 4)
    check("corner style block in payload",
          payload["cornerStyle"] is not None
          and len(payload["cornerStyle"]["corners"]) == 2
          and payload["cornerStyle"]["refName"] == "TestPlayer"
          and payload["cornerStyle"]["corners"][0]["prof"]["field"] is not None
          and payload["cornerStyle"]["corners"][0]["field"] is not None,
          str(payload["cornerStyle"] and list(payload["cornerStyle"].keys())))
    check("start in payload", payload["start"] is not None
          and payload["start"]["rolling"] is False
          and payload["start"]["green"] == 2.0, str(payload["start"]))
    by_car = {r["car"]: r for r in payload["results"]}
    check("react in results rows",
          by_car[2]["react"] == 198 and by_car[2]["gas"] == 150
          and by_car[1]["jump"] is True and by_car[3]["react"] is None,
          str({c: (r["react"], r["gas"], r["jump"]) for c, r in by_car.items()}))
    out_html = os.path.join(tmp, "out.report.html")
    template = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "report_template.html")
    report_html.render(payload, rep_bin, template, out_html)
    html_text = open(out_html, "r", encoding="utf-8").read()
    check("placeholders replaced",
          "__REPLAY_B64__" not in html_text and "/*__REPORT_JSON__*/null" not in html_text)
    check("compressed flag", '"compressed":true' in html_text)
    check("report size sane", os.path.getsize(out_html) > 50_000,
          str(os.path.getsize(out_html)))

    print("energy (schema 2, logger V1.4)")
    check("schema-1 log -> energy None", payload["energy"] is None and energy.analyze(rd) is None)
    text2 = build_log(schema=2)
    log2 = os.path.join(tmp, "vrclog_test_race_s2.txt")
    with open(log2, "w", encoding="utf-8") as f:
        f.write(text2)
    rd2 = vrclog_parser.parse(log2)
    check("schema 2 parsed, no bad lines", rd2.schema == 2 and rd2.bad_lines == 0,
          f"schema={rd2.schema} bad={rd2.bad_lines}")
    check("can cars = [0,2]", rd2.energy_cars == [True, False, True, False], str(rd2.energy_cars))
    check("native car: E lines without kW",
          len(rd2.E[1]["t"]) > 0 and not np.isfinite(rd2.E[1]["kw"]).any()
          and int(rd2.E[1]["latch"][0]) == -1)
    check("zones + sources parsed", bool(rd2.zones) and len(rd2.zones["sections"]) == 3
          and set(rd2.energy_src) == {"vrc_fa", "vrc_fa_native"})
    lap_n = sum(1 for l in rd2.ev_lap if l["car"] in (0, 1, 2))
    check("ELAP per LAP", len(rd2.ev_elap) == lap_n and lap_n >= 6,
          f"elap={len(rd2.ev_elap)} lap={lap_n}")
    check("clean 1.4 log: no ELAP counter rebuilt", rd2.elap_repaired == 0, str(rd2.elap_repaired))
    n_ev = sum(1 for e in rd2.events if e["type"] in ("DEPLOY", "HARVEST", "SM", "OT"))
    check("energy events parsed", n_ev >= 30
          and all("state" in e for e in rd2.events if e["type"] == "SM"), str(n_ev))
    an2 = detectors.analyze(rd2, tm)
    attribution.attribute(an2)
    check("schema 2: same incidents as schema 1",
          len(an2.episodes) == len(an.episodes) and an2.dnfs == an.dnfs)
    blk = energy.analyze(rd2)
    check("energy block", blk is not None and blk["hasCan"] and blk["bins"] == energy.BINS)
    c0, c2 = blk["cars"][0], blk["cars"][2]
    exp0 = [d for _, d, _ in ENERGY_EXPECTED[0]]
    check("player deploy/lap = ELAP median",
          c0["summary"]["depMed"] is not None
          and abs(c0["summary"]["depMed"] - float(np.median(exp0))) < 1e-3,
          f"{c0['summary']['depMed']} vs {exp0}")
    check("player deploy/lap ~ 3.6 MJ (analytic)", abs(c0["summary"]["depMed"] - 3.6) < 0.25,
          str(c0["summary"]["depMed"]))
    check("AI deploy/lap ~ 1.14 MJ", c2["summary"]["depMed"] is not None
          and abs(c2["summary"]["depMed"] - 1.14) < 0.15, str(c2["summary"]["depMed"]))
    check("field = the one AI can car", blk["field"]["cars"] == 1
          and blk["field"]["depMed"] == c2["summary"]["depMed"], str(blk["field"]))
    check("native car: soc only", blk["cars"][1]["profile"] == "native"
          and blk["cars"][1]["kw"] is None and blk["cars"][1]["summary"]["depMed"] is None
          and blk["cars"][1]["summary"]["socLineMed"] is not None)
    check("absent car: profile none", blk["cars"][3]["profile"] == "none")
    kw0 = c0["kw"]
    straight = [v for v in kw0[12:36] if v is not None]   # spline 6-18 %: 350 kW deploy
    corner = [v for v in kw0[58:76] if v is not None]     # spline 29-38 %: -300 kW harvest
    coast = [v for v in kw0[80:96] if v is not None]      # spline 40-48 %: nothing
    check("profile: deploy on the straight", len(straight) >= 20 and min(straight) >= 340,
          str(straight[:5]))
    check("profile: harvest in the corner", len(corner) >= 14 and max(corner) <= -290,
          str(corner[:5]))
    check("profile: coasting = 0", len(coast) >= 12 and max(abs(v) for v in coast) < 1,
          str(coast[:5]))
    ai = blk["aiProfile"]
    check("AI pooled profile = 200 kW deploy", ai is not None and ai["cars"] == 1
          and all(v is not None and abs(v - 200) < 1 for v in ai["kw"][8:22]),
          str(ai and ai["kw"][8:22]))
    zk = sorted(z["k"] for z in blk["zones"])
    check("zones: sm + ot + prd", zk == ["ot", "prd", "sm"], str(zk))
    check("SM opens once per lap", all(l["smN"] == 1 for l in c0["laps"]),
          str([l["smN"] for l in c0["laps"]]))
    check("OT used on lap 2 by car 2 only",
          [l["otN"] for l in c2["laps"]][:3] == [0, 1, 0] and all(l["otN"] == 0 for l in c0["laps"]),
          str([l["otN"] for l in c2["laps"]]))
    check("lap budgets: deploy s, SM s = zone length",
          c0["laps"][0]["deployS"] > 5 and abs(c0["laps"][0]["smS"] - 0.2 * L / 50.0) < 0.3,
          str(c0["laps"][0]))
    digest = energy.season_summary(blk)
    check("season digest", digest["aiDep"] == c2["summary"]["depMed"]
          and digest["plDep"] == c0["summary"]["depMed"], str(digest))
    payload2, rep_bin2 = report_html.build_payload(rd2, an2, tm)
    check("energy block in payload", payload2["energy"] is not None
          and len(payload2["energy"]["events"]) == n_ev)
    out2 = os.path.join(tmp, "out_s2.report.html")
    report_html.render(payload2, rep_bin2, template, out2)
    html2 = open(out2, "r", encoding="utf-8").read()
    check("energy tab + data rendered", 'id="tab-energy"' in html2
          and '"aiProfile":{' in html2 and '"hasCan":true' in html2)
    json_ok = True
    try:
        json.loads(html2.split("const DATA = ", 1)[1].split(";\nconst REPLAY_B64", 1)[0])
    except Exception:
        json_ok = False
    check("payload json round-trips", json_ok)
    check("blocked deploy requests measured (AI ~44 %, player 0 %)",
          c2["summary"]["blockedPct"] is not None and 0.3 < c2["summary"]["blockedPct"] < 0.6
          and c0["summary"]["blockedPct"] == 0.0
          and abs(blk["field"]["blockedPct"] - c2["summary"]["blockedPct"]) < 1e-3,
          f"ai={c2['summary']['blockedPct']} player={c0['summary']['blockedPct']}")

    print("ELAP carry-over repair (logger 1.4, fixed in 1.4.1)")
    text_c, truth = build_carry_log("1.4")
    log_c = os.path.join(tmp, "vrclog_carry_14.txt")
    with open(log_c, "w", encoding="utf-8") as f:
        f.write(text_c)
    raw = {}
    for ln in text_c.split("\n"):
        p = ln.split(",")
        if len(p) > 6 and p[0] == "EV" and p[2] == "ELAP":
            raw[(int(p[3]), int(p[4]))] = (float(p[5]), float(p[6]))
    carried = {(k, j) for k in truth for j in (0, 1) if raw[k][j] > truth[k][j] + 0.005}
    check("fixture reproduces the 1.4 carry-over (cars 0+1, deploy + regen, not car 2)",
          len(raw) == len(truth) == 15 and {k[0] for k, _ in carried} == {0, 1}
          and {j for _, j in carried} == {0, 1} and raw[(0, 2)][0] > 1.0 > truth[(0, 2)][0],
          str(sorted(carried)))
    rdc = vrclog_parser.parse(log_c)
    got = {(l["car"], l["lap_n"]): l for l in rdc.ev_elap}
    keys = ("dep_mj", "reg_mj")
    worst = max(abs(got[k][keys[j]] - truth[k][j]) for k in truth for j in (0, 1))
    check("every lap = the true counters (carried ones rebuilt, within one frame)",
          worst <= 0.006, f"worst {worst:.4f}")
    check("clean values used as written",
          all(got[k][keys[j]] == raw[k][j] for k in truth for j in (0, 1) if (k, j) not in carried))
    check("rebuilt count + raw values kept",
          rdc.elap_repaired == len(carried)
          and all((keys[j] + "_raw" in got[k]) == ((k, j) in carried) for k in truth for j in (0, 1))
          and all(got[k][keys[j] + "_raw"] == raw[k][j] for k, j in carried),
          f"repaired={rdc.elap_repaired} carried={len(carried)}")
    blkc = energy.analyze(rdc)
    check("energy block: player regen per lap alternates like the real cap (0.6 / 0.7)",
          [l["reg"] for l in blkc["cars"][0]["laps"]] == [0.6, 0.7, 0.6, 0.7, 0.6]
          and blkc["elapRepaired"] == len(carried),
          str([l["reg"] for l in blkc["cars"][0]["laps"]]))
    check("energy block: AI deploy median from the true laps (0.35, carried 0.38)",
          blkc["cars"][1]["summary"]["depMed"] == 0.35, str(blkc["cars"][1]["summary"]["depMed"]))
    log_f = os.path.join(tmp, "vrclog_carry_141.txt")
    with open(log_f, "w", encoding="utf-8") as f:
        f.write(build_carry_log("1.4.1")[0])
    rdf = vrclog_parser.parse(log_f)
    check("logger 1.4.1 file used as written",
          rdf.elap_repaired == 0
          and all((l["dep_mj"], l["reg_mj"]) == raw[(l["car"], l["lap_n"])] for l in rdf.ev_elap))

    print("replay binary: energy fields")
    check("replay stride 40 (both schemas)",
          payload2["replay"]["stride"] == 40 and payload["replay"]["stride"] == 40)
    n2, dt2 = payload2["replay"]["n"], payload2["replay"]["dt"]
    k5 = int(round(5.0 / dt2))   # t = 5 s: car 0 at 17 % of lap 1 -> 350 kW, SM wing open
    ek, es, ef, el = struct.unpack_from("<hHBB", rep_bin2, (0 * n2 + k5) * 40 + 32)
    check("replay sample car 0: +350 kW, soc, SM open, valid bits",
          ek == 3500 and 0 < es <= 10000 and (ef & 64) and (ef & 128) and (ef & 1) and el == 4,
          f"kw={ek} soc={es} ef={ef} latch={el}")
    ek1, es1, ef1, el1 = struct.unpack_from("<hHBB", rep_bin2, (1 * n2 + k5) * 40 + 32)
    check("replay sample native car: soc only",
          ek1 == 0 and (ef1 & 128) and not (ef1 & 64) and el1 == 255,
          f"kw={ek1} soc={es1} ef={ef1} latch={el1}")
    k12 = int(round(9.5 / dt2))   # t = 9.5 s: car 0 at 33 % (first corner) -> harvesting -300 kW
    ekh = struct.unpack_from("<h", rep_bin2, (0 * n2 + k12) * 40 + 32)[0]
    check("replay sample car 0: -300 kW while harvesting", ekh == -3000, str(ekh))
    ef_s1 = struct.unpack_from("<B", rep_bin, (0 * payload["replay"]["n"] + k5) * 40 + 36)[0]
    check("schema-1 replay: energy flags clear", ef_s1 == 0, str(ef_s1))

    print("energy_compare (A/B arms)")
    EN_PEAK[2] = 300.0            # arm B: the AI deploys at 300 kW instead of 200
    text_b = build_log(schema=2)
    EN_PEAK[2] = 200.0
    log_b = os.path.join(tmp, "vrclog_test_race_s2_armB.txt")
    with open(log_b, "w", encoding="utf-8") as f:
        f.write(text_b)
    arms = energy_compare.load_arms([log2, log_b], ["A", "B"])
    res = energy_compare.compare(arms)
    dep = next(m for m in res["metrics"] if m["key"] == "dep")
    check("arm B deploys ~1.5x more", dep["raw"][0] is not None and dep["raw"][1] is not None
          and 1.3 < dep["raw"][1] / dep["raw"][0] < 1.7, str(dep["raw"]))
    check("delta column signed", dep["deltas"][1].startswith("+"), str(dep["deltas"]))
    kwm = next(m for m in res["metrics"] if m["key"] == "kwMax")
    check("peak kW 200 -> 300", kwm["raw"] == [200.0, 300.0], str(kwm["raw"]))
    zrow = next(z for z in res["zones"] if z["zone"].startswith("ZONE_0"))
    check("zone breakdown: AI deploys inside the SM zone",
          zrow["kw"][0] is not None and 50 < zrow["kw"][0] < 90 and 0.3 <= zrow["share"][0] <= 0.4,
          str(zrow))
    md = energy_compare.to_markdown(res)
    html_c = os.path.join(tmp, "compare.html")
    energy_compare.write_html(res, arms, html_c)
    check("markdown + html written", "AI deploy MJ/lap" in md and os.path.getsize(html_c) > 10_000
          and "<canvas" in open(html_c, encoding="utf-8").read())

    print("corner reuse across layouts")
    fake_root = os.path.join(tmp, "acroot")
    for lay, data in (("gp", None), ("f12026", None), ("other", b"different line")):
        d = os.path.join(fake_root, "content", "tracks", "stadium", lay, "ai")
        os.makedirs(d)
        if data is None:
            shutil.copy(ai_path, os.path.join(d, "fast_lane.ai"))
        else:
            with open(os.path.join(d, "fast_lane.ai"), "wb") as f:
                f.write(data)
    cdir = os.path.join(tmp, "corners")
    os.makedirs(cdir)
    for lay in ("gp", "other"):
        with open(os.path.join(cdir, f"stadium-{lay}.json"), "w", encoding="utf-8") as f:
            json.dump({"track": f"stadium/{lay}", "corners": []}, f)
    ai26 = os.path.join(fake_root, "content", "tracks", "stadium", "f12026", "ai", "fast_lane.ai")
    check("identical AI line -> curated corners reused",
          track_model.sibling_corners({"trackFull": "stadium/f12026"}, ai26, cdir, fake_root)
          == os.path.join(cdir, "stadium-gp.json"))
    ai_o = os.path.join(fake_root, "content", "tracks", "stadium", "other", "ai", "fast_lane.ai")
    check("different AI line -> no reuse",
          track_model.sibling_corners({"trackFull": "stadium/other"}, ai_o, cdir, fake_root) is None)

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s): {FAILS}")
        sys.exit(1)
    print(f"all checks passed  (fixture in {tmp})")


if __name__ == "__main__":
    main()
