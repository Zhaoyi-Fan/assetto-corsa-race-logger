# VRC Race Logger — log format spec (schema 1 + schema 2) + app notes

**App:** `<AC root>\apps\lua\vrc_race_logger\`
(read-only CSP Lua app, `[CORE] LAZY = NONE` → loads at AC start, runs with window closed).
**Output:** `<AC root>\logs\vrclog_<YYYYMMDD_HHMMSS>_<trackFullID>_<session>[ _rN ].txt`
— one plain-text file per session (chunked to `*.parts\part_NNNNNN.txt` during the session,
merged on finalize; `_rN` suffix = Nth session restart). `logs\_active_recording.txt` is the
crash pointer (2 lines: partsDir, finalPath); if present at next AC launch, leftovers are merged
automatically with `END reason=salvaged`.

**App V1.4 additions (2026-09-22) — SCHEMA 2** (new line types, so the number bumps; every
schema-1 line keeps its exact shape, a schema-1 parser only has to skip `E`, `ZONES`, `ENERGY`
and the new `EV` kinds):
- Hybrid energy telemetry for every car: `E` stream (default 10 Hz, change-only), energy
  events `DEPLOY` / `HARVEST` / `SM` / `OT`, per-lap `ELAP` summaries — see the
  "Energy telemetry" section. Data source per car ID is declared in `ENERGY` header lines:
  cars with a known CAN profile (VRC Formula Alpha 2026 Pro, `vrc_formula_alpha_2026_csp`)
  read the physics script's private channels; everything else gets native CSP ERS fields.
- `ZONES` header line: the layout's `drs_zones.ini` embedded as JSON (SM / overtake / power
  zones for the 2026 package, plain DRS zones for older eras) so reports draw the zones the
  race was actually run with, whatever the track files look like later.
- META gains `energy` (bool) and `energyHz`; CAR gains `energy` ("can" | "native" — the
  intended profile; the `ENERGY` line says what actually resolved).

**App V1.3 additions (2026-07-24, additive only — schema stays 1):**
- Race-start reaction tracking: one `GREEN` event at lights-out + per-car `LAUNCH` events
  (first throttle / first movement deltas). Race sessions only; see GREEN/LAUNCH notes in the
  EV section. Pre-1.3 files simply lack these lines — parsers must treat that as "no start data".

**App V1.2 lifecycle changes (format unchanged, schema stays 1):**
- Finalize waits for in-flight async chunk writes before merging and verifies the on-disk
  chunk count; if chunks are missing (write never landed / timeout), the `.parts` dir AND the
  pointer are kept so the next launch salvages them — no more silent tail truncation.
  Shutdown with writes pending also defers to salvage instead of merging a partial set.
- Sessions > 256 MB are NOT merged: the final artifact is the `.parts` directory itself
  (parsers must accept `.parts` dirs — the Python analyzer already does).
- "Start new log now" UI button force-records the current session even if its type is
  filtered out (one-shot, expires when the session index changes).
- All float fields of the F line now pass the NaN/inf guard (`num()`), same as S/CAR lines.

**This document is the parsing contract for the Python analyzer. Update it in lockstep with
`SCHEMA_VERSION` in the Lua.**

## General rules

- Line-based, comma-separated, first token = line type. JSON payloads (META/CAR/END) are a single
  `{...}` object occupying the remainder of the line after the fixed prefix — split on the first
  1 (META/END) or 2 (CAR) commas only.
- `t` on every stream/event line = **milliseconds since session start** (integer;
  `sim.time - simTime0`). Absolute anchors in META: `simTime0` (ms since AC start) and
  `systemTime` (unix seconds). Gaps in `t` are normal: sampling is suspended while paused /
  watching replay / in main menu.
- Wheel order everywhere: **0=FL, 1=FR, 2=RL, 3=RR**.
- Car index = AC car index (0 = player in offline races). Driver/car names in CAR lines.
- NaN/±inf raw values are written as `0` (guarded in the app).

## Header lines (start of file)

```
VRCLOG,<schema:int>,<appVersion>
META,{json}
CAR,<idx>,{json}          × carsCount
```

META keys: `schema, date ("YYYY-MM-DD HH:MM:SS" local), track, trackFull ("track/layout"),
trackName, trackLengthM, sessionIndex, sessionType (ac.SessionType int), sessionName
(practice|qualify|race|hotlap|timeattack|drift|drag|session), laps, durationMin, timedRace (bool),
cars, fastHz, slowHz (=1), weatherEvery (=5 s), simTime0, systemTime, restart (int),
airTemp, roadTemp, grip, rain`.

CAR keys: `driver, car (folder ID), skin, ai (bool), aiLevel (0..1, -1=human),
aiAggression (launcher ×0.95, -1=human), ballast (kg), restrictor, maxFuel, compound (index)`,
schema 2: `+ energy ("can" | "native")`.

Schema 2 header lines (after the CAR lines; `ENERGY` may also appear later in the file):

```
ZONES,{"file":"<abs path>","exists":true,"sections":{"ZONE_0":{"START":0.97,"END":0.02,...},
       "ZONE_OVERTAKE":{...},"ZONE_POWER_REDUCTION_0":{...},...}}     (exists:false → no file)
ENERGY,{"car":"<carID>","profile":"can","key":"<carID>_CAN","inputs":122,
        "idx":{"kW":162,"kIn":5,...},"error":""}                        one per car ID
ENERGY,{"car":"<carID>","profile":"native"}                              cars without a CAN profile
```

`ZONES.sections` keeps the ini's section order and raw keys; numeric values are numbers,
anything else a string. The 2026 package's section kinds seen so far: `ZONE_n` (straight
mode, `START` / `END` / `START_LOW_GRIP`), `ZONE_OVERTAKE` (`DETECTION`, `START`,
`DETECTION_GAP_S`), `ZONE_ALT_POWER_CURVE_n`, `ZONE_POWER_REDUCTION_n` (`POWER_REDUCTION_KW`),
`ZONE_POWER_RESET_n`, `ZONE_SPEED_THRESHOLD_n` (`SPEED_THRESHOLD_KMH`), each with
`SESSION_TYPE` = ALL | RACE | QUALIFY.
`ENERGY.idx` maps the canonical E-line fields to `scriptControllerInputs` indices resolved by
NAME at runtime from the car's published struct (indices differ between car versions — FA25
CSP uses 211 where FA26 Pro uses 140 — so never hard-code them). A listed car whose struct
never resolved reports `profile:"native"` with the reason in `error`.

## F — fast stream (default 15 Hz, configurable 5–30; per active car)

```
F,t,car,posX,posY,posZ,compass,speedKmh,gas,brake,steer,gear,vLocX,vLocZ,yawRate,
  accX,accY,accZ,nd0,nd1,nd2,nd3,wheelsOut,surfHex,spline
```

| field | unit / notes |
|---|---|
| posX/Y/Z | m, AC world frame, Y up (%.2f) |
| compass | heading deg; **real range −180…180** (stub claims 0–360 — wrong on this build); for viewer arrows prefer velocity direction when speed > ~5 km/h |
| speedKmh | km/h |
| gas, brake | 0–1 (AI physics inputs are real) |
| steer | steering wheel angle, deg (+ = right) |
| gear | int, 0 = N, -1 = R |
| vLocX, vLocZ | local velocity m/s: X sideways, Z forward → **car slip angle β = atan2(vLocX, vLocZ)** |
| yawRate | localAngularVelocity.y, rad/s |
| accX/Y/Z | G-forces: X lateral, Y vertical (kerb strikes), Z longitudinal |
| nd0..nd3 | per-wheel ndSlip, normalized slip (>1 = past grip peak), clamped ≤ 99.99 |
| wheelsOut | int count of wheels outside allowed track (0–4) |
| surfHex | hex int, 4 nibbles [FL][FR][RL][RR], values = ac.SurfaceExtendedType: 0 base, 1 extraturf, 2 grass, 3 gravel, 4 kerb, 5 old, 6 sand, 7 ice, 8 snow |
| spline | track progress 0–1 (car.splinePosition) |

Lateral offset from the AI line is **not** logged — compute offline from posXYZ vs fast_lane.ai
(existing parsers), which also allows comparing against any line version.

## S — slow stream (1 Hz, per active car)

```
S,t,car,fuel,tc0,tc1,tc2,tc3,pr0,pr1,pr2,pr3,wear0,wear1,wear2,wear3,
  dmg0,dmg1,dmg2,dmg3,dmg4,engineLife,gearboxDmg,racePos,lap,flags,
  kersCharge,flatMax,sd0,sd1,sd2,sd3,compound
```

fuel L; tcN tyre core °C; prN pressure psi; wearN 0–1; dmgN = AC damage zones (accumulated impact
km/h, zones front/rear/left/right/5th unused); engineLife 0–1000 (breaks at 0); gearboxDmg 0–1;
racePos 1-based; lap = completed laps; kersCharge 0–1 (ERS battery); flatMax = max tyreFlatSpot of
4 wheels; sdN suspensionDamage per wheel; compound = current tyre set index.

`flags` bitmask: 1 inPitlane, 2 inPitBox, 4 retired, 8 raceFinished, 16 aiGoingToPits,
32 aiRainTyres, 64 drsAvailable, 128 drsActive, 256 currentLapValid.

## W — weather/sim stream (every 5 s)

```
W,t,airTemp,roadTemp,grip,rainIntensity,rainWetness,windKmh,windDirDeg,flagType
```
grip = roadGrip 0–1; flagType = ac.FlagType int (caution/yellow handling as in DRS work).

## EV — events (callback-driven, never missed between samples)

```
EV,t,COLL,car,collidedWithRaw,nearestCar,depth,speedKmh,nearestSpeedKmh,relSpeedKmh,posX,posZ,spline,lap
EV,t,LAP,car,lapTimeMs,valid(0|1),cuts,lapCount,split1Ms[,split2Ms,...]   ← valid/cuts UNRELIABLE in race sessions (constant 0/1 artifact, see below); lapTime+splits are good
EV,t,JUMP,car,resetCounter          ← teleport/reset (DRS-saga ghost cars: treat surrounding F data as suspect)
EV,t,PIT_IN,car   / EV,t,PIT_OUT,car    (pitlane boundary, fast-tick precision)
EV,t,BOX_IN,car   / EV,t,BOX_OUT,car    (parked in pit box → stop duration)
EV,t,RETIRE,car
EV,t,FINISH,car,racePosition
EV,t,FLAG,flagType                       (transitions only)
EV,t,GREEN,carsMoving                    ← V1.3+: race-start green light (see below)
EV,t,LAUNCH,car,kind,deltaMs             ← V1.3+: per-car start reaction (see below)
```

COLL notes — semantics **verified on real data (2026-07-09 spa race, 6,901 events)**:
- `collidedWithRaw`: **0 = track, otherwise = other car index + 1** (93/98 car-car events matched
  `raw−1 == nearestCar`; the 5 mismatches were multi-car melees where the nearest car wasn't the
  contact partner — trust `raw−1` for identity, use `nearestCar` as cross-check).
- `nearestCar` = nearest other car within 10 m at event time (−1 = none); `relSpeedKmh` = |v_car − v_nearest|.
- ⭐ **Floor-scrape flood**: F1 floors bottoming at speed fire raw=0 events near-continuously —
  first race: 6,803 raw=0 events at avg 302 km/h, clustered at spa spline ~0.10–0.15 (Eau Rouge
  compression), 0.2–0.3, 0.5–0.6 (Pouhon), 0.8–0.9. Useful as a *bottoming map*, not collisions —
  analyzer must treat raw=0 + high speed + no speed loss as scrape, not crash.
- Dedup since **app V1.1**: car-car pairs 0.25 s, track contacts (raw=0) 2.0 s per car.
  (V1.0 files, e.g. the 2026-07-09 ones, have raw=0 deduped at 0.25 s → the flood is IN the data.)
- Multi-car pileups appear as several COLL lines (one per car, each with its own nearest).

GREEN/LAUNCH notes — **race-start reaction, app V1.3+ (2026-07-24), race sessions only**:
- `GREEN` fires once when `sim.timeToSessionStart` crosses 0 during a recorded race session.
  Its `t` is interpolated *inside* the frame (`sim.time + timeToSessionStart`, clamped at −50 ms),
  so precision beats both the frame rate and the 15 Hz F grid. `carsMoving` = cars already above
  2 km/h at that moment — **≥ half the field ⇒ rolling start**, standing-start reactions don't
  exist (report hides the column; per-car LAUNCH kind 1 lines carry −1).
- `LAUNCH` kind **0 = first throttle** (gas > 0.05): `deltaMs` from green, **0 = throttle already
  applied at green** (pre-loaded / revving — normal for standing starts, AC physics-locks the
  field until green so this is not a false start).
- `LAUNCH` kind **1 = first movement** = the headline reaction-time number: first frame with
  speed > 1 km/h **and** > 1 cm displacement from the green-light position (both required, so a
  lone numeric speed blip while parked can't fire it). Detection latency ≈ 40–80 ms at F1 launch
  accel + up to one render frame; the bias is identical for every car, so rankings are fair.
  `deltaMs = −1` ⇒ already moving at green (jump start, or every car on a rolling start).
- Cars that never move within **60 s** of green get **no kind-1 line** (stalled/AFK — analyzer
  shows "—"). A missing GREEN line altogether = pre-1.3 log, joined mid-race, or no countdown
  seen: parser must expose "no start data", never guess.

## Energy telemetry (schema 2, app V1.4+)

Verified on 2026-09-22 (ks_silverstone f12026, VRC FA26 Pro, 1 player + 10 AI): offline AI
cars expose the physics script's CAN channels exactly like the player, so AI deployment
strategy is observable. Two data layers per car:

- **native** (every car): `soc` = `kersCharge` 0–1, `kIn` = `kersInput`, `strat` =
  `mgukDelivery + 1`, flag bit 64 = `kersCharging`. Everything else blank.
- **can** (car IDs with a profile): the fields below from `scriptControllerInputs`.

### E — energy stream (per car, cfg.energyHz default 10, 5–15; change-only)

```
E,t,car,kW,soc,depMJ,regMJ,kIn,regen,maxKW,maxKWLim,strat,split,latch,puMode,flags
```

| field | unit / notes |
|---|---|
| kW | MGU-K electrical power, **+ = deploy, − = harvest** (`rearMotorPowerKW`, ±350 for the FA26 Pro); blank for native cars |
| soc | ES state of charge 0–1 (native `kersCharge`). FA26 Pro ES = 4 + 4·soc MJ (the car's own `kersChargeESOC`, 4–8 MJ window, `kersMaxKJ` = 4000) |
| depMJ, regMJ | `kersDeployMJ` / `kersRegenMJ` — **per-lap counters that reset at the line** (so the per-lap value is the maximum inside the lap, never last−first); blank for native |
| kIn | deploy request 0–1 (CAN `kersInput`, native fallback) |
| regen | regen level 0–1 (`kersRegen`); blank for native |
| maxKW | current deploy cap kW (`mgukMaxPower`; AI strats showed 200, player 350/250/150/0, −250 while limited); blank for native |
| maxKWLim | regulatory power limit kW (`mgukMaxPowerLimit`: 350, 250 inside power-reduction zones, ramps in between); blank for native |
| strat | deployment strategy 1-based (`deploymentStrat`; native: `mgukDelivery + 1`) |
| split | deploy-map segment along the lap 0–14, monotonic with spline for the player, constant 0 for AI (`deploymentSplit`); blank for native |
| latch | straight-mode latch (`drsLatch`): 0 idle, 1–3 arming stages (player only), **2 = armed past the detection line (AI)**, **4 = wing open** (⇔ `drsMode` 2 ⇔ extra switches 7+8); blank for native |
| puMode | `puMode` (1-based); blank for native |
| flags | bitmask: 1 hybrid boost, 2 hybrid anti, 4 overtake active, 8 overtake pending, 16 power limited, 32 power-limit pending, 64 kersCharging (native; NOT a reliable harvest signal — only 4 in 10 harvest samples had it set, use kW), 128 SM wing open (latch == 4) |

Change-only writing: a line is written at the E tick only when the quantised state changed
(kW 1 kW, soc 0.01, MJ 0.1, kIn/regen 0.05, everything else exact) or 2 s passed since the
car's last line (heartbeat). Readers must **hold the last value** between lines. Expected
volume: ~0.4 MB/min for 20 FA26 cars at 10 Hz (vs 2.5–3 MB/min for F).

### Energy events (can cars only, edge-detected every frame)

```
EV,t,DEPLOY,car,state,spline,speedKmh,soc,kW    state 1 when kW rises ≥ +10, 0 when it falls < +5
EV,t,HARVEST,car,state,spline,speedKmh,soc,kW   state 1 when kW falls ≤ −10, 0 when it rises > −5
EV,t,SM,car,latch,spline,speedKmh,soc,kW        every latch change (…→2 armed →4 open →0)
EV,t,OT,car,state,spline,speedKmh,soc,kW        0 off / 1 pending / 2 active (overtake mode)
EV,t,ELAP,car,lapCount,depMJ,regMJ,socLine,socMin,socMax,deployMs,harvestMs,smMs,otMs,plimMs
```

- Hysteresis (10 / 5 kW) plus a 100 ms minimum gap per car and kind keep chatter out; a reader
  should still treat every event as a *state sample* (state value carried in the line) rather
  than assume strict on/off pairing.
- The first frame of a session only initialises the state machines (no SM/OT event for the
  state a car is already in; a car already deploying does get a DEPLOY 1).
- `ELAP` fires on every `lapCount` change (so lap 1's line summarises the out-lap / lap 0):
  `depMJ` / `regMJ` = the per-lap counters' maxima before the reset, `socLine` = soc at the
  line, `socMin` / `socMax` inside the lap, and time in ms spent deploying / harvesting /
  with the SM wing open / in overtake mode / power-limited, accumulated per render frame.
  Native cars get `EV,t,ELAP,car,lapCount,,,socLine,socMin,socMax,,,,,`.
- Power-limited and boost / anti states are NOT events (they flicker several times per
  straight) — read them from the E flags. `split` is position-derived, so no SPLIT event.

Baseline seen in the verification runs (Silverstone f12026, race, 10 AI): player ±350 kW,
8.2 MJ deployed per lap (11.3 on lap 1), harvest pinned at the 8.0 / 8.5 MJ per-lap cap, SoC
at the line 0.24; AI capped at 200 kW (`maxKW` 200), 2.1–2.3 MJ per lap, SoC at the line
0.88–0.92, power-limited ~55 s of a 96 s lap — the gap the league's AI-deployment tuning is
about. Two field notes for readers of the raw events:
- **AI deployment is pulsed**: corner-exit bursts of 100–300 ms (164 → −63 → 128 → 200 kW
  within 0.7 s), plus a −0.x…−7 kW trickle between corners. Expect 14–23 DEPLOY/HARVEST
  episodes per AI lap against ~7 for the player; the events are faithful, but the per-lap
  time budgets in `ELAP` are the robust numbers.
- The player's `isOvertakeActive` flag stayed set for a whole lap (≈90 s) after activation in
  this build, while AI cars showed ~5 s bursts; `OT` events and `otMs` report the flag as the
  car publishes it.
- SM latch 4 can fire more than once per zone (ZONE_0 wraps the start/finish line and re-opens
  right after it), so `smN` per lap is 4–6 on a 4-zone layout.

## Trailer

```
END,t,{"reason":...,"lines":N,"chunks":N}
```
reason ∈ results | session_change | session_restart | disabled | manual | shutdown | salvaged.
**Salvaged files have `END,0,...` and no counters** — analyzer must tolerate a missing/short trailer
and (belt-and-braces) accept a stray `*.parts\` dir as input by concatenating `part_*.txt` in order.

## Volume & rates

15 Hz × 20 cars ≈ ~135 B/F-line → ~2.5–3 MB/min ≈ 80–90 MB per 30-min race. NTFS-compress or zip
after analysis if hoarding. S/W/EV are noise in comparison.

## First-run verification results (2026-07-09, spa layout_f1_2025, 16 cars, 12.8 min)

Sampling cadence exact: 184,880 F lines vs 184,800 theoretical (770 s × 15 Hz × 16), S = 770×16
exactly, W = 770/5 exactly. ~2.1 MB/min. Checklist outcomes:

1. ✅ Records without touching the window (`LAZY = NONE` behaves as documented).
2. ✅ `os.date` fine (stamped filenames).
3. ✅ LAP splits correct: 3 splits at spa, sum == lapTimeMs.
4. ⚠️ COLL flood from floor scrapes → fixed in V1.1 (split dedup, see COLL notes).
5. ⏳ Results-screen auto-finalize UNTESTED (user ended via manual button, `reason=manual`) —
   check on the next naturally-finished race.
6. ✅ No reported FPS issues.
7. ✅ `collidedWith` = other index + 1 (0 = track) — confirmed.
8. ✅ Restart flow works (`_r1`, `_r2` files). ⏳ Crash-salvage untested.

### Analyzer-relevant signatures discovered in the first race

- **Stuck-AI retirement** (stock AC removes stranded AI): `JUMP` + `PIT_IN` + `BOX_IN` + `RETIRE`
  all at the same tick, zero damage, preceded by ~10–15 s of ~0 km/h at a frozen spline.
  8/15 AI DNF'd this way (first: car 11 at t=41.5 s after a La Source lap-1 melee at spline
  ~0.052–0.055, t≈21–25 s). Analyzer: retirement subtype `stuck_removed`, cause = the incident
  that stranded the car, NOT mechanical.
- **Damage fields all zero** when the champ runs 0% mechanical damage — don't use dmg/engineLife
  for cause inference on such configs; rely on kinematics + contacts.
- ⚠️ **`LAP valid/cuts` is a FALSE SIGNAL in race sessions (at least on this setup) — analyzer
  must ignore it.** All 61 laps reported `valid=0, cuts=1` with ZERO variance: car 3 (Russell)
  never put a single wheel outside or on an invalid surface all race yet got cuts=1 every lap;
  the player's lap with a 7 s four-wheels-in-sand excursion ALSO got exactly cuts=1 (didn't
  accumulate). Constant ⇒ uninformative. (Initial reading "systematic cut at T13 / line off
  track" was WRONG and is retracted — see correction in the claudelogs research log Round 5.)
- **wheelsOut≥3 histograms measure TIME spent out, not passes**: the 1,881 AI samples at spline
  0.665–0.67 were crashed cars sitting in the Les Fagnes sand trap at ~1.5 km/h (surf `6666` =
  4×Sand), i.e. the stuck→DNF cars — NOT an every-lap line violation. Always check speed/surf
  before interpreting density peaks.
- **surfHex is the VISUAL surface hint** (`surfaceExtendedType`), not the validity flag —
  `surfaceValidTrack` is NOT logged in schema 1. Schema-2 candidate: per-wheel validTrack bitmask
  (+ maybe live lapCutsCount) if real track-limit analysis is ever needed.
- resetCounter was 5 for every DNF teleport (grid placement resets count too) — treat it as a
  change detector, not an absolute.
- FLAG values observed: 1 (green/start) and 2 (caution cycles around each incident) — 13 events.

## Analyzer + HTML report (BUILT 2026-07-09, v1)

`E:\Codex_Workspace\projects\VRC\tools\race_report\` — see its README.md for usage/modules.
One command: `py vrclog_report.py <log.txt>` → `<log>.report.html` (single-file, ~7 MB for a
13-min 16-car race, pipeline ~1 s). Tabs: 总览 (results, lap chart, AI-failure hotspot corners),
时间轴 (cautions + severity pips + pits), 事故卡片 (chain + confidence-scored evidence + speed/
brake spark + jump-to-replay), 回放 (canvas map from fast_lane.ai ribbon, follow-cam, live
leaderboard, per-wheel ndSlip/surface telemetry, scrubber with pips, 1-8× playback).

Verified on the first real race log (browser-tested, zero console errors). Detection calibrated
against the manually-established facts of that race; key detected story: La Source L1 melee
(6 cars, KiboOst DNF), Les Fagnes L1 pileup (player + Lawson + Tsunvazo, 2 DNF), Les Combes 8-car
pileup (Piastri rear-ended Norris), Chicane Norris-into-Tsunoda at 98 km/h rel (both eventually
DNF), 4 separate AI failures at Les Fagnes → ai_line-suspect note fires for that corner.

Parsing/semantics rules the analyzer enforces (from first-run verification): LAP valid/cuts
ignored; raw=0 COLL at ≥110 km/h = floor scrape (not crash); damage channels unused (league runs
0% mech damage) — "recent prior incident within 60 s" is the proxy; wheelsOut histograms are
time-spent, always joined against speed/surface.
