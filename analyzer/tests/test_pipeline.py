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

Run:  py tests/test_pipeline.py
"""
import math
import os
import struct
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import vrclog_parser
import track_model
import detectors
import attribution
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


def build_log():
    lines = [
        "VRCLOG,1,1.3-test",
        'META,{"schema":1,"date":"2026-07-10 12:00:00","track":"stadium",'
        '"trackFull":"stadium/gp","trackName":"Stadium GP","trackLengthM":%.1f,'
        '"sessionIndex":0,"sessionType":3,"sessionName":"race","laps":3,'
        '"durationMin":0,"timedRace":false,"cars":4,"fastHz":15,"slowHz":1,'
        '"weatherEvery":5,"simTime0":0,"systemTime":0,"restart":0,'
        '"airTemp":22.0,"roadTemp":30.0,"grip":0.980,"rain":0.000}' % L,
        'CAR,0,{"driver":"TestPlayer","car":"vrc_fa","skin":"red","ai":false,'
        '"aiLevel":1.0,"aiAggression":0.0,"ballast":0.0,"restrictor":0.0,'
        '"maxFuel":100.0,"compound":2}',
        'CAR,1,{"driver":"AI Alpha","car":"vrc_fa","skin":"blu","ai":true,'
        '"aiLevel":0.97,"aiAggression":0.6,"ballast":0.0,"restrictor":0.0,'
        '"maxFuel":100.0,"compound":2}',
        'CAR,2,{"driver":"AI Bravo","car":"vrc_fa","skin":"grn","ai":true,'
        '"aiLevel":0.96,"aiAggression":0.7,"ballast":0.0,"restrictor":0.0,'
        '"maxFuel":100.0,"compound":2}',
        'CAR,3,{"driver":"AI Ghost","car":"vrc_fa","skin":"wht","ai":true,'
        '"aiLevel":0.95,"aiAggression":0.5,"ballast":0.0,"restrictor":0.0,'
        '"maxFuel":100.0,"compound":2}',
    ]
    dist = [0.0, -12.0, -24.0]           # start offsets (m along the line)
    laps = [0, 0, 0]

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
        # --- laps / S / W ------------------------------------------------------
        for ci in range(3):
            if ci == 1 and t >= T_DNF:
                continue
            if dist[ci] >= (laps[ci] + 1) * L:
                laps[ci] += 1
                lines.append("EV,%d,LAP,%d,%d,1,0,%d,%d,%d"
                             % (t_ms, ci, int(L / 50 * 1000), laps[ci],
                                14000, 14000))
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

    print("report")
    payload, rep_bin = report_html.build_payload(rd, an, tm)
    check("weather in payload", len(payload["weather"]["t"]) >= 18)
    check("results has 4 cars", len(payload["results"]) == 4)
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

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s): {FAILS}")
        sys.exit(1)
    print(f"all checks passed  (fixture in {tmp})")


if __name__ == "__main__":
    main()
