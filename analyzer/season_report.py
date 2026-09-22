"""season_report — aggregate many VRC race logs into one season dashboard HTML.

Usage:
  py season_report.py <log|dir|glob> [more...] [--out season.html]
                      [--ac-root <path>] [--min-minutes 5]

Accepts merged .txt logs, .parts directories, directories to scan for vrclog_*.txt,
and glob patterns. Each log runs through the same parse->detect->attribute pipeline
as vrclog_report (no replay packing, so it's fast); logs shorter than --min-minutes
are skipped as aborted stubs. Output is a self-contained bilingual HTML with:
  * race calendar (linked to per-race reports when found next to the log)
  * driver table: starts, incident cards, caused rear-ends, solo errors, DNFs
  * cross-race AI loss-of-control hotspots (track + corner)
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time

import vrclog_parser
import vrclog_report
import detectors
import attribution
import energy
import thresholds as th

AC_ROOT_DEFAULT = vrclog_report.AC_ROOT_DEFAULT


def collect_logs(inputs):
    logs = []
    for item in inputs:
        if os.path.isdir(item) and item.endswith(".parts"):
            logs.append(item)
        elif os.path.isdir(item):
            logs += sorted(glob.glob(os.path.join(item, "vrclog_*.txt")))
            logs += sorted(glob.glob(os.path.join(item, "vrclog_*.parts")))
        elif os.path.isfile(item):
            logs.append(item)
        else:
            hits = sorted(glob.glob(item))
            if not hits:
                print(f"[warn] no logs match: {item}")
            logs += hits
    seen, out = set(), []
    for p in logs:
        ap = os.path.abspath(p)
        if ap not in seen:
            seen.add(ap)
            out.append(ap)
    return out


def analyze_log(path, ac_root):
    rd = vrclog_parser.parse(path)
    tm, _ = vrclog_report.prepare_track(rd, ac_root=ac_root, verbose=False)
    an = detectors.analyze(rd, tm)
    attribution.attribute(an)
    return rd, tm, an


def summarize(path, rd, an, out_dir):
    cards = [e for e in an.episodes if e["severity"] >= th.CARD_MIN_SEVERITY]
    leader_laps = max([len(rd.laps_of(ci)) for ci in range(rd.n_cars)], default=0)
    report = os.path.splitext(path)[0] + ".report.html"
    link = None
    if os.path.isfile(report):
        try:
            link = os.path.relpath(report, out_dir).replace("\\", "/")
        except ValueError:  # different drive on Windows — relative link impossible
            link = "file:///" + os.path.abspath(report).replace("\\", "/")
    race = {
        "file": os.path.basename(path),
        "date": rd.meta.get("date", ""),
        "track": rd.meta.get("trackName") or rd.meta.get("trackFull", "?"),
        "trackFull": rd.meta.get("trackFull", "?"),
        "session": rd.meta.get("sessionName", "?"),
        "cars": rd.n_cars,
        "laps": leader_laps,
        "minutes": round(rd.duration / 60, 1),
        "cards": len(cards),
        "dnfs": len(set(an.dnfs)),
        "report": link,
    }
    # hybrid energy digest (schema-2 logs only; None otherwise)
    try:
        race["energy"] = energy.season_summary(energy.analyze(rd))
    except Exception as e:
        print(f"[warn] {os.path.basename(path)}: energy digest skipped ({e})")
        race["energy"] = None

    drivers = {}
    for ci in range(rd.n_cars):
        d = drivers.setdefault(rd.driver(ci), {
            "starts": 0, "cards": 0, "caused": 0, "solo": 0, "dnf": 0,
            "ai": rd.is_ai(ci)})
        d["starts"] += 1
    for e in cards:
        solo_cars = {v["car"] for v in e["evidence"] if v["tag"] == "no_external"}
        for ci in e["cars"]:
            d = drivers[rd.driver(ci)]
            d["cards"] += 1
            if len(e["cars"]) == 1 and ci in solo_cars:
                d["solo"] += 1
    for g in an.contacts:
        if g["mode"] == "rear":
            drivers[rd.driver(g["behind"])]["caused"] += 1
    for ci in set(an.dnfs):
        drivers[rd.driver(ci)]["dnf"] += 1

    hotspots = {}
    for key, v in an.corner_fail.items():
        hk = (race["trackFull"], key)
        hotspots[hk] = {"track": race["track"], "corner": v["label"], "n": len(v["ids"])}
    return race, drivers, hotspots


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VRC Season Report</title>
<style>
:root{--bg:#101318;--bg2:#171b22;--bg3:#1f242e;--line:#2a3140;--fg:#e8ebf0;
  --dim:#9aa3b2;--acc:#3b7ddd;--bad:#e8433f;font-size:15px}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--fg);font-family:"Segoe UI",system-ui,sans-serif}
header{padding:14px 22px;border-bottom:1px solid var(--line);background:var(--bg2);position:relative}
h1{font-size:1.25rem;font-weight:600} h2{font-size:1.02rem;margin:20px 0 8px}
#langbtn{position:absolute;top:14px;right:22px;background:var(--bg3);color:var(--fg);
  border:1px solid var(--line);border-radius:6px;padding:5px 12px;cursor:pointer}
main{padding:10px 22px 30px;max-width:1100px}
table{border-collapse:collapse;width:100%;font-size:.87rem}
th,td{padding:5px 10px;text-align:left;border-bottom:1px solid var(--line)}
th{color:var(--dim);font-weight:500;font-size:.78rem}
a{color:var(--acc);text-decoration:none} a:hover{text-decoration:underline}
.hint{color:var(--dim);font-size:.78rem}
.bar{display:inline-block;height:9px;background:var(--bad);border-radius:3px;
  vertical-align:1px;margin-right:7px}
.num{font-variant-numeric:tabular-nums}
</style></head><body>
<header><h1 id="title"></h1>
<button id="langbtn">EN</button>
<div class="hint" id="sub"></div></header>
<main>
<h2 id="h-races"></h2><table id="races"></table>
<h2 id="h-drivers"></h2><div class="hint" id="drivers-hint"></div><table id="drivers"></table>
<h2 id="h-hot"></h2><table id="hot"></table>
<h2 id="h-energy"></h2><div class="hint" id="energy-hint"></div><table id="energy"></table>
</main>
<script>
"use strict";
const DATA = /*__SEASON_JSON__*/null;
const esc = s => String(s).replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
let LANG = (() => { try { return localStorage.getItem("vrcLang") || "en"; } catch(e){ return "en"; } })();
const I18N = {
  zh: {title:"VRC 赛季报告", sub:"{r} 场 · 生成于 {d}", races:"赛历", drivers:"车手榜",
    driversHint:"事故卡 = 卷入严重度≥25 的事件；追尾致因 = 作为后车的追尾接触；独立失误 = 单车事故且无外因",
    hot:"跨场失控热点（AI）",
    cDate:"日期", cTrack:"赛道", cSession:"session", cCars:"车", cLaps:"圈", cMin:"分钟",
    cCards:"事故卡", cDnf:"DNF", cReport:"报告",
    cDriver:"车手", cStarts:"场次", cCaused:"追尾致因", cSolo:"独立失误", cPerRace:"卡/场",
    cCorner:"弯角", cN:"次", open:"打开",
    energy:"混动能量（每赛道，logger V1.4+ 日志）",
    energyHint:"每圈中位数：AI = 各 AI 车中位数的中位数；漂移 = 末圈过线 SoC − 首圈过线 SoC；没有能量数据的场次不列",
    cAiCars:"AI 车", cAiDep:"AI 部署 MJ/圈", cPlDep:"玩家 部署 MJ/圈", cAiReg:"AI 回收 MJ/圈",
    cAiKw:"AI 峰值 kW", cAiDrift:"AI SoC 漂移", cPlDrift:"玩家 SoC 漂移", cAiSm:"AI SM s/圈", cPlSm:"玩家 SM s/圈"},
  en: {title:"VRC Season Report", sub:"{r} sessions · generated {d}", races:"Calendar", drivers:"Drivers",
    driversHint:"cards = involved in severity≥25 episodes; caused = rear-ended someone; solo = single-car incident with no external cause",
    hot:"Cross-race loss-of-control hotspots (AI)",
    cDate:"Date", cTrack:"Track", cSession:"Session", cCars:"Cars", cLaps:"Laps", cMin:"Min",
    cCards:"Cards", cDnf:"DNF", cReport:"Report",
    cDriver:"Driver", cStarts:"Starts", cCaused:"Caused", cSolo:"Solo errors", cPerRace:"Cards/race",
    cCorner:"Corner", cN:"Count", open:"open",
    energy:"Hybrid energy per track (logger V1.4+ logs)",
    energyHint:"per-lap medians: AI = median over the AI cars' medians; drift = last-lap SoC at the line − first-lap SoC; sessions without energy data are not listed",
    cAiCars:"AI cars", cAiDep:"AI deploy MJ/lap", cPlDep:"Player deploy MJ/lap", cAiReg:"AI harvest MJ/lap",
    cAiKw:"AI peak kW", cAiDrift:"AI SoC drift", cPlDrift:"Player SoC drift", cAiSm:"AI SM s/lap", cPlSm:"Player SM s/lap"},
};
const T = (k, v) => { let s = I18N[LANG][k] || k;
  if (v) for (const key in v) s = s.replace("{"+key+"}", v[key]); return s; };
function render(){
  document.documentElement.lang = LANG;
  document.getElementById("langbtn").textContent = "LANG: " + (LANG === "zh" ? "EN" : "中文");
  document.getElementById("title").textContent = T("title");
  document.title = T("title");
  document.getElementById("sub").textContent = T("sub", {r: DATA.races.length, d: DATA.generated});
  document.getElementById("h-races").textContent = T("races");
  document.getElementById("h-drivers").textContent = T("drivers");
  document.getElementById("drivers-hint").textContent = T("driversHint");
  document.getElementById("h-hot").textContent = T("hot");
  document.getElementById("races").innerHTML =
    `<tr><th>${T("cDate")}</th><th>${T("cTrack")}</th><th>${T("cSession")}</th>
     <th>${T("cCars")}</th><th>${T("cLaps")}</th><th>${T("cMin")}</th>
     <th>${T("cCards")}</th><th>${T("cDnf")}</th><th>${T("cReport")}</th></tr>` +
    DATA.races.map(r => `<tr><td class="num">${esc(r.date)}</td><td>${esc(r.track)}</td>
      <td>${esc(r.session)}</td><td>${r.cars}</td><td>${r.laps}</td>
      <td class="num">${r.minutes}</td><td>${r.cards}</td><td>${r.dnfs}</td>
      <td>${r.report ? `<a href="${esc(r.report)}">${T("open")}</a>` : "—"}</td></tr>`).join("");
  const dmax = Math.max(1, ...DATA.drivers.map(d => d.cards));
  document.getElementById("drivers").innerHTML =
    `<tr><th>${T("cDriver")}</th><th>${T("cStarts")}</th><th>${T("cCards")}</th>
     <th>${T("cCaused")}</th><th>${T("cSolo")}</th><th>DNF</th><th>${T("cPerRace")}</th></tr>` +
    DATA.drivers.map(d => `<tr><td>${esc(d.name)}${d.ai ? "" : " ★"}</td>
      <td>${d.starts}</td>
      <td><span class="bar" style="width:${d.cards/dmax*120}px"></span>${d.cards}</td>
      <td>${d.caused}</td><td>${d.solo}</td><td>${d.dnf}</td>
      <td class="num">${(d.cards / Math.max(1, d.starts)).toFixed(2)}</td></tr>`).join("");
  const hmax = Math.max(1, ...DATA.hotspots.map(h => h.n));
  document.getElementById("hot").innerHTML =
    `<tr><th>${T("cTrack")}</th><th>${T("cCorner")}</th><th>${T("cN")}</th></tr>` +
    DATA.hotspots.map(h => `<tr><td>${esc(h.track)}</td><td>${esc(h.corner)}</td>
      <td><span class="bar" style="width:${h.n/hmax*120}px"></span>${h.n}</td></tr>`).join("");
  const en = DATA.races.filter(r => r.energy && r.energy.aiCars);
  const f = (v, nd) => (v === null || v === undefined) ? "—" : Number(v).toFixed(nd);
  const pct = v => (v === null || v === undefined) ? "—" : (v >= 0 ? "+" : "") + (v * 100).toFixed(0) + "%";
  document.getElementById("h-energy").style.display = en.length ? "" : "none";
  document.getElementById("energy-hint").style.display = en.length ? "" : "none";
  document.getElementById("h-energy").textContent = T("energy");
  document.getElementById("energy-hint").textContent = T("energyHint");
  document.getElementById("energy").innerHTML = !en.length ? "" :
    `<tr><th>${T("cDate")}</th><th>${T("cTrack")}</th><th>${T("cAiCars")}</th><th>${T("cAiDep")}</th>
     <th>${T("cPlDep")}</th><th>${T("cAiReg")}</th><th>${T("cAiKw")}</th><th>${T("cAiDrift")}</th>
     <th>${T("cPlDrift")}</th><th>${T("cAiSm")}</th><th>${T("cPlSm")}</th></tr>` +
    en.map(r => `<tr><td class="num">${esc(r.date)}</td><td>${esc(r.track)}</td><td>${r.energy.aiCars}</td>
      <td class="num">${f(r.energy.aiDep, 2)}</td><td class="num">${f(r.energy.plDep, 2)}</td>
      <td class="num">${f(r.energy.aiReg, 2)}</td><td class="num">${f(r.energy.aiKwMax, 0)}</td>
      <td class="num">${pct(r.energy.aiSocDrift)}</td><td class="num">${pct(r.energy.plSocDrift)}</td>
      <td class="num">${f(r.energy.aiSm, 1)}</td><td class="num">${f(r.energy.plSm, 1)}</td></tr>`).join("");
}
document.getElementById("langbtn").onclick = () => {
  LANG = LANG === "zh" ? "en" : "zh";
  try { localStorage.setItem("vrcLang", LANG); } catch(e){}
  render();
};
render();
</script></body></html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("--out", default="season.html")
    ap.add_argument("--ac-root", default=AC_ROOT_DEFAULT)
    ap.add_argument("--min-minutes", type=float, default=5.0,
                    help="skip logs shorter than this (aborted/restart stubs)")
    a = ap.parse_args()

    logs = collect_logs(a.inputs)
    if not logs:
        sys.exit("no logs found")
    out_dir = os.path.dirname(os.path.abspath(a.out)) or "."

    races, driver_tot, hot_tot = [], {}, {}
    for path in logs:
        try:
            rd = vrclog_parser.parse(path)
        except Exception as e:
            print(f"[skip] {os.path.basename(path)}: parse failed ({e})")
            continue
        if rd.duration < a.min_minutes * 60:
            print(f"[skip] {os.path.basename(path)}: {rd.duration/60:.1f} min "
                  f"< {a.min_minutes} min (stub)")
            continue
        try:
            rd, tm, an = analyze_log(path, a.ac_root)
        except Exception as e:
            print(f"[skip] {os.path.basename(path)}: analysis failed ({e})")
            continue
        race, drivers, hotspots = summarize(path, rd, an, out_dir)
        races.append(race)
        for name, d in drivers.items():
            tot = driver_tot.setdefault(name, {
                "name": name, "ai": d["ai"], "starts": 0, "cards": 0,
                "caused": 0, "solo": 0, "dnf": 0})
            for k in ("starts", "cards", "caused", "solo", "dnf"):
                tot[k] += d[k]
        for hk, h in hotspots.items():
            tot = hot_tot.setdefault(hk, {"track": h["track"], "corner": h["corner"], "n": 0})
            tot["n"] += h["n"]
        print(f"[ok]   {os.path.basename(path)}: {race['session']} {race['track']}, "
              f"{race['cards']} cards, {race['dnfs']} DNF")

    if not races:
        sys.exit("no usable logs")
    races.sort(key=lambda r: r["date"])
    payload = {
        "generated": time.strftime("%Y-%m-%d %H:%M"),
        "races": races,
        "drivers": sorted(driver_tot.values(), key=lambda d: (-d["cards"], d["name"])),
        "hotspots": sorted(hot_tot.values(), key=lambda h: -h["n"])[:30],
    }
    page = PAGE.replace("/*__SEASON_JSON__*/null",
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"season report: {a.out} ({os.path.getsize(a.out)/1024:.0f} KB, "
          f"{len(races)} sessions, {len(driver_tot)} drivers)")


if __name__ == "__main__":
    main()
