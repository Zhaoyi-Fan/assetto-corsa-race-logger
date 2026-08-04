# assetto-corsa-race-logger

**"Warcraft Logs for Assetto Corsa": a full-field race recorder (CSP Lua app) plus an offline
analysis toolchain that turns each session into a self-contained, shareable, interactive HTML
race report — incident detection, cause attribution, lap-pace analytics and a scrubbable 2D
replay. 中文说明: [README.zh-CN.md](README.zh-CN.md)**

---

## Why this exists

Offline championship racing against AI has a debrief problem. When a race goes sideways you get
no answers from the game: *who* hit whom at turn 1, *why* did that AI spin three laps in a row at
the same corner, was your off caused by a kerb strike, cold tyres, dirty air — or just you?
Replays are ephemeral, telemetry apps only watch the player, and nothing records the other 15 cars.

In MMO raiding this problem was solved years ago by Warcraft Logs: record everything, analyze
offline, share a link. This project applies the same idea to Assetto Corsa:

1. **Record everything, always.** A lightweight in-game app continuously samples the full physics
   state of *every* car (offline AI run full local physics, so their data is as complete as the
   player's) plus every discrete event — collisions, laps, pit stops, resets, flags,
   race-start reaction times.
2. **Analyze offline.** A Python pipeline detects losses of control (spin / slide / understeer-off /
   oversteer-off / stuck), groups them into multi-car *episodes*, and builds an evidence-based
   cause chain for each one.
3. **Share one file.** The output is a single HTML file with zero external dependencies — drop it
   into Discord and anyone can open it, scrub the replay, and see exactly what happened. UI is
   bilingual (English / 中文, toggle in the top-right corner).

Built for (and calibrated on) the VRC Formula Alpha 2025 offline league; works with any car and
track content.

## Live example

**[▶ Open a real race report](https://zhaoyi-fan.github.io/assetto-corsa-race-logger/examples/spa-race.report.html)** —
a 15-car, 5-lap league race at Spa (Formula Alpha 2025): standing-start reaction times for the
whole field, 8 car-to-car contacts, 7 incident cards and 3 DNFs. Everything in it — replay, lap
charts, attribution — is one self-contained HTML file exactly as the tool generates it.

The raw log it was built from ships in [`examples/vrclog_spa_race_example.zip`](examples/vrclog_spa_race_example.zip)
(19 MB unzipped). Reproduce the report yourself:

```
unzip examples/vrclog_spa_race_example.zip -d examples
python analyzer/vrclog_report.py examples/vrclog_20260803_231630_spa-layout_f1_2025_race_r13.txt
```

(Regeneration reads the track's AI line from your AC install, so it needs the spa
`layout_f1_2025` track mod; viewing the shipped report needs nothing.)

## What you get

| Component | What it does |
|---|---|
| `app/vrc_race_logger` | CSP Lua app: 15 Hz per-car physics stream, 1 Hz status stream, weather stream, event log. Crash-safe chunked writes. Read-only — no physics or content modification, league-safe. ~2 MB/min for a 16-car grid. |
| `analyzer/vrclog_report.py` | One command: log → `*.report.html` (typically ~3.5 MB, generated in ~1 s). |
| `analyzer/season_report.py` | Aggregates a folder of logs into a season dashboard: calendar, driver incident league table, cross-race corner hotspots. |
| `docs/log_format_spec.md` | The full plain-text log format contract (schema 1), field by field. |

### The report

* **Overview** — results, best laps, start reaction times (green light → first movement,
  best highlighted, jump starts flagged), pit-stop table (lap / stationary time / tyre change),
  positions-by-lap chart, loss-of-control corner hotspots.
* **Lap times** — per-driver lap trend (in/out laps and outliers marked), cumulative gap to
  leader by lap, clean-lap pace statistics (average, standard deviation) with a click-to-toggle
  legend.
* **Driving** — MoTeC-style distance-domain lap comparison against a selectable reference lap
  (session best / own best / any driver's any lap): speed + throttle/brake traces with corner
  bands, a cumulative Δt line showing *where* time is lost, a corner-by-corner table (brake
  point, entry/apex speed, back-to-throttle point, corner time, brake-point consistency σ),
  driving-style metrics (coasting, full-throttle, trail-braking, time over the grip peak),
  a G-G diagram and a micro-sector heatmap across all laps. Click any chart to jump into the
  replay at that exact moment.
* **Timeline** — one swimlane per driver: incident dots (click → jump into replay), pit-stop
  blocks, DNF markers, caution bands, and a grip/rain strip along the bottom.
* **Incidents** — one card per episode: severity score, per-car narrative chain
  (`contact with X → spun → off track → stuck 12s → retired`), ranked evidence with confidence
  values, speed/brake sparkline, one-click jump into the replay.
* **Replay** — 2D top-down map with the real track ribbon, all cars with heading + fading trails,
  follow-cam, live standings with gaps, per-wheel telemetry (ndSlip, surface type, inputs, β)
  with a live scrolling input trace, a full-race throttle/brake ribbon under the scrubber,
  lap-tick scrubber, 1–32× playback. Mouse wheel / pinch zoom, drag to pan, touch-friendly.

### Incident attribution

Each episode's evidence list is built from the data, tagged and confidence-scored, e.g.:

| Tag | Meaning |
|---|---|
| `contact` | rear-end / side-by-side contact right before the loss (with closing speed) |
| `wall` | barrier impact — classified as likely cause (before the loss) or consequence (after) |
| `kerb` | kerb strike with a vertical-G spike within 0.6 s of the loss |
| `offline` | driving far off the racing line before the loss |
| `dirty_air` | glued to the car ahead through a corner for 2 s+ |
| `cold_tyres` / `worn_tyres` / `flat_spot` | tyre-state evidence from the 1 Hz stream |
| `prior_incident` | same car was in another incident less than 60 s earlier |
| `avoidance` | swerving around a stationary wreck under caution |
| `rain` / `grip` | weather / track-grip context |
| `ai_line` | 3+ separate AI incidents at the same corner this race — AI-line/hint suspect |
| `no_external` | none of the above — driver/AI error at the limit |

Stuck-AI retirements (the game teleporting a beached car to the pits) are recognized by their
event signature and narrated as such, not as a mechanical DNF.

## Quick start

### 1. Install the recorder (in-game app)

Requirements: Assetto Corsa + a recent [Custom Shaders Patch](https://acstuff.club/patch/) build
(Lua apps supported).

1. Copy `app/vrc_race_logger` into `<AC root>\apps\lua\`.
2. In game, add **VRC Race Logger** from the apps taskbar (category: Lua apps).
3. Done — it records race and qualifying sessions by default (practice opt-in), whether or not
   the window is open. The window shows status, sample counts and a manual *Finalize & save now*
   button.

Logs are written to `<AC root>\logs\vrclog_<timestamp>_<track>_<session>.txt`. During the session
data is streamed to a `.parts` folder and merged at session end; if the game crashes, the next
launch salvages the recording automatically. Sessions larger than 256 MB stay as `.parts`
directories (the analyzer reads those directly).

### 2. Install the analyzer

```
pip install numpy        # the only dependency (Python 3.10+)
```

### 3. Generate a report

```
python analyzer/vrclog_report.py "<AC root>\logs\vrclog_20260709_..._race.txt"
```

The report is written next to the log as `<log>.report.html`. Open it in any modern browser
(Chrome 80+ / Firefox 113+ / Safari 16.4+ — the replay stream is zlib-compressed and inflated
with `DecompressionStream`).

Options:

| Flag | Default | Purpose |
|---|---|---|
| `--out PATH` | next to the log | output file |
| `--ac-root PATH` | `AC_ROOT` env var, else auto-detected Steam path | where to find track data |
| `--ai PATH` | auto from the log's track id | explicit `fast_lane.ai` |
| `--corners PATH` | `analyzer/corners/<track>-<layout>.json` | corner-name config |

### 4. Season dashboard (optional)

```
python analyzer/season_report.py "<AC root>\logs" --out season.html
```

Accepts directories, globs, single logs or `.parts` folders. Skips sub-5-minute stubs (aborted
starts / restarts) by default (`--min-minutes`). Race rows link to per-race reports when they sit
next to their logs.

## New tracks: corner names

First run on an unknown track auto-detects corner segments from the AI line's curvature profile,
numbers them T1..Tn, and writes an editable skeleton to `analyzer/corners/<track>-<layout>.json`.
Edit the `n` / `name` fields to match the official numbering (see
`corners/spa-layout_f1_2025.json` for a fully curated example — 21 tracks ship curated), re-run,
and every label in the report upgrades. Detection, attribution and the replay do not depend on
corner names — they only make the labels nicer.

## Tuning detection

Every threshold lives in `analyzer/thresholds.py`, commented, in one place: slip-angle limits,
off-track dwell times, contact grouping windows, evidence windows, severity gates. The shipped
values were calibrated against real 16-car league races. If a different car class produces false
positives (e.g. slides flagged on a drift car), tune there and re-run — reports regenerate in a
second.

## Development

```
python analyzer/tests/test_pipeline.py
```

runs an end-to-end test on a fully synthetic race: a generated stadium track (v7 `fast_lane.ai`
binary) and a scripted log containing a rear-end → spin → off → stuck → auto-recovered-DNF
sequence, a reverse-gear decoy, a wall hit, a caution, a car with missing ticks and a car with no
data at all — asserting parser alignment, detection, attribution and report rendering (30 checks).

Technical notes:

* The report is one file: template + JSON payload + base64(zlib(replay binary)) — no CDN, no
  tracking, works from `file://`.
* Replay stream: 28 bytes/sample/car at 15 Hz (position, heading, speed, inputs, gear,
  per-wheel slip + surface, slip angle, spline), decoded in the browser into typed arrays.
* All log-sourced strings (driver / car / track names) are HTML-escaped before hitting the DOM —
  a report shared to a league is rendered in other people's browsers, and mod content is not
  trusted markup.
* Logs contain the driver names that appeared in your session; reports embed them. Share
  accordingly.

## Known limitations

* Detection thresholds are race-calibrated. Qualifying/practice logs work (the CLI notes it,
  results rank by best lap, race-only evidence is disabled) but out-laps may over-trigger.
* The CSP lap-validity flag is unreliable in offline race sessions and is deliberately ignored.
* Leagues that run 0% mechanical damage get no damage-channel evidence; the analyzer compensates
  with the `prior_incident` heuristic.
* Live timing gaps in the replay leaderboard are distance/speed estimates, not official timing.

## License

MIT — see [LICENSE](LICENSE).
