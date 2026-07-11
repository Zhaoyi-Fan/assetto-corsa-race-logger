"""attribution — cause chains + evidence for detected episodes (bilingual zh/en).

Annotates each episode with: title/titleEn, chain (per-car phase narrative, text/textEn),
evidence (ordered, confidence-scored, text/textEn) — the report's language toggle just
picks the field.
"""
from __future__ import annotations

import numpy as np
import thresholds as th

KIND_ZH = {"slide": "大滑移", "spin": "打转", "off": "冲出赛道",
           "off_understeer": "推头冲出", "off_oversteer": "甩尾冲出",
           "stuck": "困住", "dnf": "退赛 (DNF)"}
KIND_EN = {"slide": "big slide", "spin": "spun", "off": "off track",
           "off_understeer": "understeer off", "off_oversteer": "oversteer off",
           "stuck": "stuck", "dnf": "retired (DNF)"}


def _s_row(rd, ci, tt):
    s = rd.S[ci]
    if not len(s["t"]):
        return None
    i = min(np.searchsorted(s["t"], tt), len(s["t"]) - 1)
    return {k: v[i] for k, v in s.items()}


def _in_corner(tm, s):
    s = s % 1.0
    return any(c["s0"] <= s <= c["s1"] for c in tm.corners)


def _stuck_recovery(rd, ci, t_ret):
    """Stuck-AI DNF signature: the sim teleports the car (JUMP) on the same tick it
    retires — distinguishes 'recovered by the game' from a natural retirement."""
    return any(ev["type"] == "JUMP" and ev["car"] == ci and abs(ev["t"] - t_ret) <= 1.5
               for ev in rd.events)


def attribute(an):
    rd, tm = an.rd, an.tm
    t = an.t
    is_race = getattr(an, "session", "race") == "race"
    absent = set(getattr(an, "absent", []))
    for e in an.episodes:
        ev_list = []
        loss = [p for p in e["phases"] if p["kind"] not in ("dnf",)]
        loss_cars = sorted({p["car"] for p in loss})
        multi = len(loss_cars) > 1

        def add(car, tag, conf, zh, en):
            name = rd.driver(car)
            ev_list.append({"car": car, "tag": tag, "conf": conf,
                            "text": (f"{name}: {zh}" if multi else zh),
                            "textEn": (f"{name}: {en}" if multi else en)})

        for ci in loss_cars:
            ph = min((p for p in loss if p["car"] == ci), key=lambda p: p["t0"])
            t_loss, i_loss = ph["t0"], ph["i0"]

            # 1) contact immediately before the loss
            got_contact = False
            for g in sorted(e["contacts"] + an.contacts, key=lambda g: g["t"]):
                if ci not in (g["a"], g["b"]):
                    continue
                if not (t_loss - th.ATTR_CONTACT_S <= g["t"] <= t_loss + 1.0):
                    continue
                other = g["b"] if g["a"] == ci else g["a"]
                oname = rd.driver(other)
                if g["mode"] == "rear":
                    if g["behind"] == ci:
                        add(ci, "contact", 88,
                            f"追尾了 {oname}（相对速度 {g['rel']:.0f} km/h）",
                            f"rear-ended {oname} (closing {g['rel']:.0f} km/h)")
                    else:
                        add(ci, "contact", 88,
                            f"被 {oname} 追尾（相对速度 {g['rel']:.0f} km/h）",
                            f"rear-ended by {oname} (closing {g['rel']:.0f} km/h)")
                else:
                    add(ci, "contact", 78,
                        f"与 {oname} 并排接触（相对速度 {g['rel']:.0f} km/h）",
                        f"side contact with {oname} (closing {g['rel']:.0f} km/h)")
                got_contact = True
                break

            # 1b) wall impact: before the loss = likely cause (high conf); during or
            # after = consequence, kept below 50 so "no external cause" still shows
            walls = [w for w in e.get("walls", []) if w["car"] == ci]
            if walls:
                w_pre = [w for w in walls if w["t"] <= t_loss + 0.5]
                wmax = max(w_pre or walls, key=lambda w: w["speed"])
                add(ci, "wall", 75 if w_pre else 48,
                    f"撞墙（{wmax['speed']:.0f} km/h）",
                    f"hit the wall ({wmax['speed']:.0f} km/h)")

            # 2) kerb strike right before losing it
            j0 = max(0, i_loss - int(th.ATTR_KERB_S * 15))
            sw = rd.F[ci]["surf_w"][j0:i_loss + 1]
            ay = np.abs(an.acc_y[j0:i_loss + 1, ci])
            if len(sw) and (sw == 4).any() and len(ay) and ay.max() >= th.KERB_ACC_Y_G:
                add(ci, "kerb", 66, f"路肩弹跳（垂直 {ay.max():.1f} G）",
                    f"kerb strike ({ay.max():.1f} G vertical)")

            # 3) badly off the racing line before the loss
            j1 = max(0, i_loss - 15)
            offm = float(np.median(an.offset[j1:i_loss + 1, ci])) if i_loss > j1 else 0.0
            k = tm.idx_at(ph["s"])
            edge = tm.side_l[k] if offm > 0 else tm.side_r[k]
            if abs(offm) > edge + 1.0:
                add(ci, "offline", 56, f"严重偏离走线（{offm:+.1f} m）",
                    f"far off the racing line ({offm:+.1f} m)")

            # 4) dirty air: glued to the car ahead through a corner
            j2 = max(0, i_loss - int(th.DIRTY_AIR_DUR_S * 15))
            gap = an.gap_ahead_s[j2:i_loss + 1, ci]
            if (len(gap) and (gap < th.DIRTY_AIR_GAP_S).mean() > 0.85
                    and _in_corner(tm, ph["s"]) and not got_contact):
                ahead = int(an.ahead_car[i_loss, ci])
                gmed = float(np.median(gap))
                add(ci, "dirty_air", 52,
                    f"长时间贴住 {rd.driver(ahead)}（gap {gmed:.2f}s，脏气流）",
                    f"running in {rd.driver(ahead)}'s dirty air (gap {gmed:.2f}s)")

            # 5) cold tyres / worn tyres / flat spot (S tier)
            srow = _s_row(rd, ci, t_loss)
            if srow is not None:
                lap = int(srow["lap"])
                tf = (srow["tc0"] + srow["tc1"]) / 2
                if lap + 1 <= th.COLD_TYRE_LAP and tf < th.COLD_TYRE_TEMP:
                    add(ci, "cold_tyres", 46, f"冷胎阶段（前胎芯 {tf:.0f}°C，第 {lap + 1} 圈）",
                        f"cold tyres (front core {tf:.0f}°C, lap {lap + 1})")
                wmax = max(srow["wear0"], srow["wear1"], srow["wear2"], srow["wear3"])
                if wmax > th.WORN_TYRE:
                    add(ci, "worn_tyres", 42, f"轮胎磨损偏高（{wmax*100:.0f}%）",
                        f"high tyre wear ({wmax*100:.0f}%)")
                if srow["flat_max"] > th.FLAT_SPOT:
                    add(ci, "flat_spot", 42, f"轮胎平斑（{srow['flat_max']:.2f}）",
                        f"flat-spotted tyre ({srow['flat_max']:.2f})")

            # 5b) carried trouble from a recent earlier episode (damage is off in this
            # league, so "recently crashed" is the proxy for a compromised car);
            # pick the NEAREST prior episode, not the first in list order
            best_prev, best_dtp = None, 1e9
            for prev in an.episodes:
                if prev is e or ci not in prev["cars"]:
                    continue
                dtp = t_loss - prev["t1"]
                if prev["t0"] < e["t0"] and 0 < dtp <= 60.0 and dtp < best_dtp:
                    best_prev, best_dtp = prev, dtp
            if best_prev is not None:
                add(ci, "prior_incident", 55,
                    f"{best_dtp:.0f} 秒前刚在 {best_prev['corner']} 卷入事故（车辆状态/位置受影响）",
                    f"involved in an incident at {best_prev.get('cornerEn', best_prev['corner'])} "
                    f"{best_dtp:.0f}s earlier (compromised pace/position)")

            # 6) avoiding a wreck under caution (race-only semantics)
            if is_race and any(a <= t_loss <= b for a, b in an.cautions):
                ds = (an.spline[i_loss, :] - an.spline[i_loss, ci]) % 1.0
                near_stopped = [j for j in range(an.C)
                                if j != ci and j not in absent
                                and an.speed[i_loss, j] < 10
                                and ds[j] * tm.total < th.AVOIDANCE_DIST_M]
                if near_stopped:
                    who = ", ".join(rd.driver(j) for j in near_stopped[:2])
                    add(ci, "avoidance", 62, f"黄旗下避让前方事故现场（{who}）",
                        f"avoiding the wreck ahead under yellow ({who})")

            # 7) wet / low grip context (last sample at/before the loss — searchsorted
            # alone would return the one AFTER)
            if len(rd.W["t"]):
                wi = min(max(0, np.searchsorted(rd.W["t"], t_loss) - 1), len(rd.W["t"]) - 1)
                if rd.W["rain"][wi] > 0.05:
                    add(ci, "rain", 34, f"雨况（rain {rd.W['rain'][wi]:.2f}）",
                        f"wet conditions (rain {rd.W['rain'][wi]:.2f})")
                elif rd.W["grip"][wi] < 0.97:
                    add(ci, "grip", 30, f"低抓地（grip {rd.W['grip'][wi]:.2f}）",
                        f"low grip ({rd.W['grip'][wi]:.2f})")

            # 8) AI-line suspicion: repeated separate AI incidents at this corner
            if rd.is_ai(ci):
                fails = an.corner_fail.get(
                    e.get("corner_key", e["corner"]), {"ids": set()})["ids"]
                if len(fails) >= th.AI_LINE_MIN_EPISODES:
                    add(ci, "ai_line", 44,
                        f"本场 {e['corner']} 已有 {len(fails)} 起独立 AI 失控事故 — AI 走线/hint 嫌疑",
                        f"{len(fails)} separate AI incidents at {e.get('cornerEn', e['corner'])} "
                        f"this race — AI line/hint suspect")

            if not any(v["car"] == ci and v["conf"] >= 50 for v in ev_list):
                add(ci, "no_external", 32, "无明确外因 — 驾驶/AI 极限失误",
                    "no clear external cause — driver/AI error at the limit")

        # contact-only episodes still get a line of evidence
        if not loss_cars and e["contacts"]:
            g = max(e["contacts"], key=lambda g: g["rel"])
            a, b = rd.driver(g["behind"]), rd.driver(g["front"])
            if g["mode"] == "rear":
                zh = f"{a} 追尾 {b}（相对速度 {g['rel']:.0f} km/h，无失控后果）"
                en = f"{a} rear-ended {b} (closing {g['rel']:.0f} km/h, no loss of control)"
            else:
                zh = f"{rd.driver(g['a'])} 与 {rd.driver(g['b'])} 并排接触（相对速度 {g['rel']:.0f} km/h，无失控后果）"
                en = f"side contact {rd.driver(g['a'])} / {rd.driver(g['b'])} (closing {g['rel']:.0f} km/h, no loss of control)"
            ev_list.append({"car": g["a"], "tag": "contact", "conf": 80,
                            "text": zh, "textEn": en})

        seen, dedup = set(), []
        for v in sorted(ev_list, key=lambda v: -v["conf"]):
            key = (v["car"], v["tag"])
            if key not in seen:
                seen.add(key)
                dedup.append(v)
        e["evidence"] = dedup

        # per-car chains (both languages, same structure)
        chains = []
        for ci in e["cars"]:
            items = []
            for g in sorted(e["contacts"], key=lambda g: g["t"]):
                if ci in (g["a"], g["b"]):
                    other = g["b"] if g["a"] == ci else g["a"]
                    items.append((g["t"], f"与 {rd.driver(other)} 接触",
                                  f"contact with {rd.driver(other)}"))
            for w in sorted(e.get("walls", []), key=lambda w: w["t"]):
                if w["car"] == ci:
                    items.append((w["t"], "撞墙", "hit wall"))
            for p in sorted([p for p in e["phases"] if p["car"] == ci], key=lambda p: p["t0"]):
                zh, en = KIND_ZH.get(p["kind"], p["kind"]), KIND_EN.get(p["kind"], p["kind"])
                if p["kind"] == "stuck":
                    zh += f" {p['t1'] - p['t0']:.0f}s"
                    en += f" {p['t1'] - p['t0']:.0f}s"
                elif p["kind"] == "dnf" and _stuck_recovery(rd, ci, p["t0"]):
                    zh, en = "卡死回收退赛 (DNF)", "stuck — auto-recovered (DNF)"
                items.append((p["t0"], zh, en))
            if items:
                items.sort(key=lambda x: x[0])
                seq_zh, seq_en = [], []
                for _, zh, en in items:
                    if not seq_zh or seq_zh[-1] != zh:
                        seq_zh.append(zh)
                        seq_en.append(en)
                chains.append({"car": ci, "text": " → ".join(seq_zh),
                               "textEn": " → ".join(seq_en)})
        e["chain"] = chains

        # title (both languages)
        n = len(e["cars"])
        dnf_cars = [p["car"] for p in e["phases"] if p["kind"] == "dnf"]
        cz, cen = e["corner"], e.get("cornerEn", e["corner"])
        if n >= 3:
            e["title"] = f"{cz} 多车事故（{n} 车" + (f"，{len(dnf_cars)} 退赛）" if dnf_cars else "）")
            e["titleEn"] = f"{cen} multi-car incident ({n} cars" + \
                           (f", {len(dnf_cars)} DNF)" if dnf_cars else ")")
        elif dnf_cars:
            e["title"] = f"{rd.driver(dnf_cars[0])} 事故退赛 — {cz}"
            e["titleEn"] = f"{rd.driver(dnf_cars[0])} crashed out — {cen}"
        elif loss_cars:
            main = max(loss, key=lambda p: p["t1"] - p["t0"])
            e["title"] = f"{rd.driver(main['car'])} {KIND_ZH.get(main['kind'], '事故')} — {cz}"
            e["titleEn"] = f"{rd.driver(main['car'])} {KIND_EN.get(main['kind'], 'incident')} — {cen}"
        elif e["contacts"]:
            g = e["contacts"][0]
            e["title"] = f"{rd.driver(g['a'])} / {rd.driver(g['b'])} 接触 — {cz}"
            e["titleEn"] = f"{rd.driver(g['a'])} / {rd.driver(g['b'])} contact — {cen}"
        else:
            e["title"] = f"事件 — {cz}"
            e["titleEn"] = f"incident — {cen}"
    return an


if __name__ == "__main__":
    import sys
    import vrclog_parser, track_model, detectors
    rd = vrclog_parser.parse(sys.argv[1])
    tm = track_model.load_fast_lane(sys.argv[2])
    tm, _ = track_model.pick_z_sign(tm, rd)
    tm.load_corners(sys.argv[3])
    an = detectors.analyze(rd, tm)
    attribute(an)
    for e in an.episodes:
        if e["severity"] < th.CARD_MIN_SEVERITY:
            continue
        print(f"\n#{e['id']} [{e['severity']:.0f}] {e['title']}  |  {e['titleEn']}")
        for c in e["chain"]:
            print(f"   {rd.driver(c['car'])[:16]:16s} {c['textEn']}")
        for v in e["evidence"][:4]:
            print(f"   [{v['conf']}] {v['textEn']}")
