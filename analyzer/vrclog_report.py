"""vrclog_report — CLI: VRC race log txt -> single-file WCL-style HTML report.

Usage:
  py vrclog_report.py <vrclog_....txt> [--out report.html] [--ai fast_lane.ai]
                      [--corners corners.json] [--ac-root <path>]
                      [--season-link season.html]

Defaults: fast_lane.ai auto-resolved from the log's track id under the AC install;
corners json auto-picked from ./corners/<trackFull with '-'>.json; output written
next to the input log as <log>.report.html.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import vrclog_parser
import track_model
import detectors
import attribution
import report_html
import thresholds as th

HERE = os.path.dirname(os.path.abspath(__file__))
AC_ROOT_DEFAULT = track_model.default_ac_root()


def prepare_track(rd, ai_path=None, corners_path=None, ac_root=AC_ROOT_DEFAULT,
                  verbose=True):
    """Resolve fast_lane.ai + corners for a parsed log -> (TrackModel, on-line median).

    Shared by vrclog_report and season_report. Unknown tracks get an auto-generated
    numbered corner skeleton written to ./corners/ for later hand-curation.
    """
    ai_path = ai_path or track_model.resolve_ai_path(rd.meta, ac_root)
    if not ai_path or not os.path.isfile(ai_path):
        raise FileNotFoundError(f"fast_lane.ai not found (try --ai): {ai_path}")
    tm = track_model.load_fast_lane(ai_path)
    tm, med = track_model.pick_z_sign(tm, rd)
    corners = corners_path or os.path.join(
        HERE, "corners", rd.meta.get("trackFull", "").replace("/", "-") + ".json")
    if os.path.isfile(corners):
        tm.load_corners(corners)
        if verbose:
            print(f"[2/5] track model: {tm.total:.0f} m, {len(tm.corners)} named corners "
                  f"(on-line check {med:.2f} m)"
                  + ("" if tm.sides_ok else " — payload sides broken, constant width"))
    else:
        # first time on this track: auto-detect corners, number them sequentially,
        # and write an editable skeleton next to the tool (edit n/name, re-run)
        segs = tm.detect_segments()
        tm.corners = [{"n": str(i + 1), "name": "", "s0": round(c["s0"], 4),
                       "s1": round(c["s1"], 4), "dir": c["dir"]}
                      for i, c in enumerate(segs)]
        tm.straights = []
        os.makedirs(os.path.dirname(corners) or ".", exist_ok=True)
        with open(corners, "w", encoding="utf-8") as f:
            json.dump({"track": rd.meta.get("trackFull", ""),
                       "comment": "AUTO-GENERATED skeleton: sequential numbers from the "
                                  "radius profile of fast_lane.ai. Edit n/name to match the "
                                  "official numbering (template: spa-layout_f1_2025.json) "
                                  "and re-run for nicer labels.",
                       "corners": tm.corners, "straights": []}, f,
                      ensure_ascii=False, indent=2)
        if verbose:
            print(f"[2/5] track model: {tm.total:.0f} m — auto-generated {len(tm.corners)} "
                  f"corners -> {corners} (labels = T1..T{len(tm.corners)}; edit names anytime)")
    return tm, med


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log")
    ap.add_argument("--out")
    ap.add_argument("--ai")
    ap.add_argument("--corners")
    ap.add_argument("--ac-root", default=AC_ROOT_DEFAULT)
    ap.add_argument("--season-link", metavar="HREF",
                    help="add a header link back to a season dashboard page")
    a = ap.parse_args()

    t0 = time.time()
    rd = vrclog_parser.parse(a.log)
    print(f"[1/5] parsed {os.path.basename(a.log)}: {rd.n_cars} cars, "
          f"{rd.duration/60:.1f} min, END={rd.end.get('reason', 'MISSING')}")
    fixed = getattr(rd, "aligned_cars", []) or getattr(rd, "absent_cars", [])
    if fixed:
        print(f"      note: resampled/filled cars {fixed} (uneven F grids in log)")
    session = rd.meta.get("sessionName", "race")
    if session != "race":
        print(f"      note: session = {session} — thresholds are race-calibrated; "
              f"race-only evidence disabled, results ranked by best lap")

    try:
        tm, med = prepare_track(rd, a.ai, a.corners, a.ac_root)
    except FileNotFoundError as e:
        sys.exit(str(e))

    an = detectors.analyze(rd, tm)
    print(f"[3/5] detection: {len(an.episodes)} episodes, {len(an.contacts)} contacts, "
          f"{len(an.dnfs)} DNF, {len(an.cautions)} cautions")

    attribution.attribute(an)
    cards = sum(1 for e in an.episodes if e["severity"] >= th.CARD_MIN_SEVERITY)
    print(f"[4/5] attribution done: {cards} incident cards")

    payload, rep_bin = report_html.build_payload(rd, an, tm)
    if a.season_link:
        payload["seasonLink"] = a.season_link
    out = a.out or (os.path.splitext(a.log)[0] + ".report.html")
    report_html.render(payload, rep_bin, os.path.join(HERE, "report_template.html"), out)
    size = os.path.getsize(out) / 1e6
    print(f"[5/5] report written: {out} ({size:.1f} MB) in {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
