-- VRC Race Logger — V1.0 (continuous full-field recorder, "Warcraft Logs for AC")
-- ============================================================================
-- WHAT THIS RECORDS (design: knowledge_base + claudelogs 2026-07-08 research log):
--   * F lines (~15 Hz, configurable): per car — position, heading, speed, inputs
--     (AI inputs are real physics inputs offline), local velocity (slip angle source),
--     yaw rate, G-forces, per-wheel ndSlip, wheels outside, per-wheel surface type,
--     spline position. This is the replay + incident-forensics stream.
--   * S lines (1 Hz): per car — fuel, tyre core temps / pressures / wear, damage zones,
--     engine/gearbox life, race position, lap, DRS/ERS, suspension damage, state flags.
--   * W lines (every 5 s): ambient/road temp, grip, rain, wind, race flag.
--   * EV lines (event-driven, never missed between samples): collisions (with nearest-car
--     attribution + relative speed), laps (time/validity/cuts/splits), car resets/jumps,
--     pit in/out, box in/out, finish/retire, flag changes, race-start reaction (green
--     light moment + per-car first-throttle / first-movement deltas).
--   * Header: META json (track, session, rates) + one CAR json per entry (driver, car,
--     skin, aiLevel, aiAggression, ballast, restrictor).
--
-- FILE LIFECYCLE (CSP io has no append mode -> chunked writes, merged at the end):
--   logs\vrclog_<stamp>_<track>_<session>.parts\part_000001.txt   during the session
--   logs\vrclog_<stamp>_<track>_<session>.txt                     merged single file
--   logs\_active_recording.txt                                    crash pointer; if AC
--     died mid-race, next launch merges the leftover parts automatically (salvage).
--   Merge triggers: session change, session restart, results screen, manual button,
--   AC shutdown (best effort synchronous).
--
-- Sampling only runs live (skips pause / replay / main menu). Time base for every line:
--   milliseconds since session start (sim.time based); absolute start stored in META.
-- Read-only w.r.t. physics and content files -> league-safe. No ALLOW_APPS needed.
-- ============================================================================

local SCHEMA_VERSION = 1
local APP_VERSION    = '1.3'

local SESSION_NAMES = {
  [0] = 'session', [1] = 'practice', [2] = 'qualify', [3] = 'race',
  [4] = 'hotlap', [5] = 'timeattack', [6] = 'drift', [7] = 'drag',
}

local cfg = ac.storage({
  enabled     = true,   -- master switch
  recRace     = true,   -- record race sessions
  recQuali    = true,   -- record qualify sessions
  recPractice = false,  -- record practice/hotlap/other sessions
  fastHz      = 15,     -- F-tier sample rate (5..30); S is fixed 1 Hz, W every 5 s
  debug       = false,  -- extra status details in the window
})

local FLUSH_BYTES   = 1.5 * 1024 * 1024  -- flush buffer to a part file at this size...
local FLUSH_SECONDS = 10                 -- ...or at least this often
-- Sessions bigger than this stay as a .parts directory instead of being concatenated
-- into one txt on the render thread (the analyzer reads .parts directories directly).
local MERGE_MAX_BYTES = 256 * 1024 * 1024
local MERGE_WAIT_MAX_S = 5.0             -- max time to wait for in-flight chunk writes
-- Collision dedup, split by type after the first real race (2026-07-09 spa): F1 floors
-- scraping the track at speed fire collidedWith==0 events near-continuously (6.8k events,
-- avg 302 km/h) — rate-limit those per car; real car-car contacts keep the tight window.
local COLL_DEDUP_CAR_S   = 0.25  -- min gap between logged car-car collisions of same pair
local COLL_DEDUP_TRACK_S = 2.0   -- min gap per car for track/floor contacts (raw == 0)
-- Race-start reaction tracking (V1.3). Green light = sim.timeToSessionStart crossing
-- zero (interpolated inside the frame, so precision beats the frame rate). Reaction =
-- green -> first movement (both thresholds must hold, so a lone numeric speed blip
-- while parked can't fire it); throttle delta logged separately. Cars already moving
-- at green (rolling start / jump start) get delta -1.
local START_GAS_MIN    = 0.05  -- gas above this = throttle applied
local START_SPEED_KMH  = 1.0   -- movement: speed above this...
local START_DIST_M     = 0.01  -- ...AND at least this far from the green-light spot
local START_MOVING_KMH = 2.0   -- already faster than this at green = moving (delta -1)
local START_WINDOW_S   = 60    -- stop waiting for launches this long after green

-- ---- paths ------------------------------------------------------------------
local logsDir     = ac.getFolder(ac.FolderID.Root) .. '\\logs'
local pointerFile = logsDir .. '\\_active_recording.txt'

-- ---- recording state (reset per session) -------------------------------------
local recording   = false
local basePath    = nil    -- logs\vrclog_...  (no extension)
local partsDir    = nil
local finalPath   = nil
local t0          = 0      -- sim.time at session start (ms)
local chunkIdx    = 0
local buf, bufN   = {}, 0
local bufBytes    = 0
local flushAcc    = 0
local fastAcc     = 0
local slowAcc     = 0
local weatherAcc  = 0
local linesTotal  = 0
local bytesTotal  = 0
local writeErrors = 0
local lastEvent   = ''
local pendingWrites = 0    -- async chunk writes still in flight
local pendingMerge  = nil  -- deferred finalize: {parts, final, chunks, bytes, reason, waited}
local forceRecordIndex = -1 -- session index forced recordable via the UI button

-- session bookkeeping
local curSessionIndex    = -1
local finalizedForIndex  = -1
local restartPending     = false
local restartCount       = 0
local salvageDone        = false
local prevResultsScreen  = false
local prevFlag           = -1

-- per-car transition trackers
local prevInPitlane, prevInPit, prevRetired, prevFinished = {}, {}, {}, {}
local lastCollAt = {}   -- [key "i:otherRaw:nearest"] = sim.time seconds of last logged hit
-- race-start reaction tracker; nil = inactive (non-race session / green handled / window over)
-- {prev = last timeToSessionStart, green = sim.time of green light, cars = {[i] = {...}}}
local startTrack = nil

local statusText = 'Idle'
local fmt = string.format

-- ---- small helpers ------------------------------------------------------------

local function num(v)  -- NaN/inf guard so a glitched channel can't corrupt a line
  if v ~= v or v == math.huge or v == -math.huge then return 0 end
  return v
end

local function jstr(s) -- minimal JSON string escaper (driver names etc.)
  s = tostring(s or '')
  s = s:gsub('[\\"%c]', function(c)
    if c == '\\' then return '\\\\' end
    if c == '"' then return '\\"' end
    return fmt('\\u%04x', string.byte(c))
  end)
  return '"' .. s .. '"'
end

local function relT(sim)
  return math.floor(sim.time - t0 + 0.5)
end

local function dateStamp(pattern) -- os.date is standard LuaJIT; dateGlobal is the CSP fallback
  local f = os.date or os.dateGlobal
  return f(pattern)
end

local function put(line)
  bufN = bufN + 1
  buf[bufN] = line
  bufBytes = bufBytes + #line + 1
  linesTotal = linesTotal + 1
end

local function flushChunk(sync)
  if bufN == 0 then return end
  chunkIdx = chunkIdx + 1
  local path = fmt('%s\\part_%06d.txt', partsDir, chunkIdx)
  local data = table.concat(buf, '\n', 1, bufN) .. '\n'
  buf, bufN, bufBytes = {}, 0, 0
  bytesTotal = bytesTotal + #data
  if sync then
    if not io.save(path, data, true) then writeErrors = writeErrors + 1 end
  else
    pendingWrites = pendingWrites + 1
    io.saveAsync(path, data, function(err)
      pendingWrites = pendingWrites - 1
      if err then writeErrors = writeErrors + 1; ac.log('[VRCLOG] chunk write failed: ' .. tostring(err)) end
    end, true)
  end
end

-- Merge contiguous part files into the final single txt. Parts are contiguous by
-- construction, so no directory scan is needed (works for salvage too).
-- expectedChunks (optional): abort instead of silently truncating when fewer parts
-- are on disk than were written (e.g. an async write never landed).
local function mergeParts(fromPartsDir, toFinalPath, endLine, expectedChunks)
  local pieces, i = {}, 1
  while true do
    local p = fmt('%s\\part_%06d.txt', fromPartsDir, i)
    if not io.fileExists(p) then break end
    pieces[#pieces + 1] = io.load(p)
    i = i + 1
  end
  if expectedChunks ~= nil and #pieces < expectedChunks then
    ac.log(fmt('[VRCLOG] merge aborted: %d of %d chunks on disk', #pieces, expectedChunks))
    return false
  end
  if #pieces == 0 and endLine == nil then return false end
  pieces[#pieces + 1] = endLine
  if not io.save(toFinalPath, table.concat(pieces), true) then
    ac.log('[VRCLOG] merge failed for ' .. toFinalPath)
    return false
  end
  for k = 1, i - 1 do io.deleteFile(fmt('%s\\part_%06d.txt', fromPartsDir, k)) end
  io.deleteDir(fromPartsDir)
  return true
end

-- ---- header writers -------------------------------------------------------------

local function writeHeader(sim, session)
  put(fmt('VRCLOG,%d,%s', SCHEMA_VERSION, APP_VERSION))
  put(fmt('META,{"schema":%d,"date":%s,"track":%s,"trackFull":%s,"trackName":%s,'
      .. '"trackLengthM":%.1f,"sessionIndex":%d,"sessionType":%d,"sessionName":%s,'
      .. '"laps":%d,"durationMin":%.1f,"timedRace":%s,"cars":%d,"fastHz":%d,"slowHz":1,'
      .. '"weatherEvery":5,"simTime0":%.0f,"systemTime":%.0f,"restart":%d,'
      .. '"airTemp":%.1f,"roadTemp":%.1f,"grip":%.3f,"rain":%.3f}',
    SCHEMA_VERSION,
    jstr(dateStamp('%Y-%m-%d %H:%M:%S')),
    jstr(ac.getTrackID()), jstr(ac.getTrackFullID('/')), jstr(ac.getTrackName()),
    sim.trackLengthM, sim.currentSessionIndex,
    session and session.type or 0, jstr(SESSION_NAMES[session and session.type or 0] or 'session'),
    session and session.laps or 0, session and session.durationMinutes or 0,
    (session and session.isTimedRace) and 'true' or 'false',
    sim.carsCount, math.floor(cfg.fastHz + 0.5), t0, sim.systemTime, restartCount,
    sim.ambientTemperature, sim.roadTemperature, sim.roadGrip, sim.rainIntensity))
  for i = 0, sim.carsCount - 1 do
    local c = ac.getCar(i)
    if c ~= nil then
      put(fmt('CAR,%d,{"driver":%s,"car":%s,"skin":%s,"ai":%s,"aiLevel":%.3f,'
          .. '"aiAggression":%.3f,"ballast":%.1f,"restrictor":%.1f,"maxFuel":%.1f,'
          .. '"compound":%d}',
        i, jstr(ac.getDriverName(i)), jstr(ac.getCarID(i)), jstr(ac.getCarSkinID(i)),
        c.isAIControlled and 'true' or 'false', num(c.aiLevel), num(c.aiAggression),
        num(c.ballast), num(c.restrictor), num(c.maxFuel), c.compoundIndex))
    end
  end
end

-- ---- session lifecycle ------------------------------------------------------------

local function shouldRecordSession(session)
  local t = session and session.type or 0
  if t == 3 then return cfg.recRace end
  if t == 2 then return cfg.recQuali end
  return cfg.recPractice
end

local function startRecording(sim)
  local session = ac.getSession(sim.currentSessionIndex)
  io.createDir(logsDir)
  local stamp = dateStamp('%Y%m%d_%H%M%S')
  local track = ac.getTrackFullID('-'):gsub('[^%w_%-%.]', '-')
  local sname = SESSION_NAMES[session and session.type or 0] or 'session'
  local base  = fmt('%s\\vrclog_%s_%s_%s', logsDir, stamp, track, sname)
  if restartCount > 0 then base = base .. '_r' .. restartCount end
  basePath, partsDir, finalPath = base, base .. '.parts', base .. '.txt'
  io.createDir(partsDir)

  t0 = sim.time
  chunkIdx, buf, bufN, bufBytes = 0, {}, 0, 0
  flushAcc, fastAcc, slowAcc, weatherAcc = 0, 0, 0, 0
  linesTotal, bytesTotal, writeErrors = 0, 0, 0
  prevInPitlane, prevInPit, prevRetired, prevFinished = {}, {}, {}, {}
  lastCollAt = {}
  prevFlag = sim.raceFlagType
  -- arm start-reaction tracking for race sessions only; stays inert until a real
  -- positive->zero countdown transition is seen (so joining mid-race logs nothing)
  startTrack = (session and session.type == 3) and {prev = nil, green = nil, cars = {}} or nil

  writeHeader(sim, session)
  -- crash pointer: if AC dies, next launch finds this and merges the leftovers
  io.save(pointerFile, partsDir .. '\n' .. finalPath, true)

  recording = true
  curSessionIndex = sim.currentSessionIndex
  lastEvent = 'started ' .. sname
  ac.log('[VRCLOG] recording -> ' .. finalPath)
end

-- Merge step of finalize. Only runs when no async chunk writes are in flight
-- (otherwise mergeParts could truncate at the first not-yet-written chunk).
local function completeMerge(parts, final, expectedChunks, totalBytes, reason)
  if totalBytes >= MERGE_MAX_BYTES then
    -- endurance-size session: the analyzer reads .parts directories directly,
    -- so skip concatenating hundreds of MB on the render thread
    io.deleteFile(pointerFile)
    statusText = 'Saved as parts (large session): ' .. io.getFileName(parts)
    ac.log('[VRCLOG] finalized (' .. reason .. '), kept as parts: ' .. parts)
  elseif mergeParts(parts, final, nil, expectedChunks) then
    io.deleteFile(pointerFile)
    statusText = 'Saved: ' .. io.getFileName(final)
    ac.log('[VRCLOG] finalized (' .. reason .. '): ' .. final)
  else
    -- parts + pointer stay on disk -> next launch salvages whatever landed
    statusText = 'MERGE INCOMPLETE (parts + pointer kept): ' .. io.getFileName(parts)
    ac.log('[VRCLOG] merge incomplete for ' .. final .. ' — salvage will retry next launch')
  end
end

local function finalize(reason, sim)
  if not recording then return end
  recording = false
  local t = sim and math.max(0, relT(sim)) or 0
  local endLine = fmt('END,%d,{"reason":%s,"lines":%d,"chunks":%d}\n',
    t, jstr(reason), linesTotal, chunkIdx + 1)
  put(endLine:sub(1, #endLine - 1))
  flushChunk(true)
  if pendingWrites > 0 then
    -- async chunk writes still in flight: defer the merge; update() completes it
    -- once the io worker drains (startRecording is blocked until then)
    pendingMerge = {parts = partsDir, final = finalPath, chunks = chunkIdx,
                    bytes = bytesTotal, reason = reason, waited = 0}
    statusText = 'Finishing writes: ' .. io.getFileName(finalPath)
  else
    completeMerge(partsDir, finalPath, chunkIdx, bytesTotal, reason)
  end
  lastEvent = 'finalized: ' .. reason
end

-- Salvage a previous crashed session (pointer file present -> merge leftovers).
local function salvageIfNeeded()
  salvageDone = true
  if not io.fileExists(pointerFile) then return end
  local data = io.load(pointerFile)
  if data == nil then io.deleteFile(pointerFile); return end
  local oldParts, oldFinal = data:match('([^\n]+)\n([^\n]+)')
  if oldParts and oldFinal and io.dirExists(oldParts) then
    local endLine = fmt('END,0,{"reason":"salvaged"}\n')
    if mergeParts(oldParts, oldFinal, endLine) then
      ac.log('[VRCLOG] salvaged crashed recording -> ' .. oldFinal)
      lastEvent = 'salvaged previous crash log'
    end
  end
  io.deleteFile(pointerFile)
end

-- ---- samplers ---------------------------------------------------------------------

local function sampleFast(sim, t)
  for i = 0, sim.carsCount - 1 do
    local c = ac.getCar(i)
    if c ~= nil and c.isActive then
      local w = c.wheels
      local function sx(k) return math.max(0, math.min(15, w[k].surfaceExtendedType)) end
      local surf = sx(0) * 4096 + sx(1) * 256 + sx(2) * 16 + sx(3)
      put(fmt('F,%d,%d,%.2f,%.2f,%.2f,%.1f,%.1f,%.3f,%.3f,%.1f,%d,%.2f,%.2f,%.3f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%d,%X,%.5f',
        t, i,
        num(c.position.x), num(c.position.y), num(c.position.z),
        num(c.compass), num(c.speedKmh),
        num(c.gas), num(c.brake), num(c.steer), c.gear,
        num(c.localVelocity.x), num(c.localVelocity.z),
        num(c.localAngularVelocity.y),
        num(c.acceleration.x), num(c.acceleration.y), num(c.acceleration.z),
        math.min(99.99, num(w[0].ndSlip)), math.min(99.99, num(w[1].ndSlip)),
        math.min(99.99, num(w[2].ndSlip)), math.min(99.99, num(w[3].ndSlip)),
        c.wheelsOutside, surf, num(c.splinePosition)))

      -- state transitions logged as events at fast-tick precision
      local pl, pb, rt, fin = c.isInPitlane, c.isInPit, c.isRetired, c.isRaceFinished
      if prevInPitlane[i] ~= nil then
        if pl ~= prevInPitlane[i] then put(fmt('EV,%d,%s,%d', t, pl and 'PIT_IN' or 'PIT_OUT', i)) end
        if pb ~= prevInPit[i]     then put(fmt('EV,%d,%s,%d', t, pb and 'BOX_IN' or 'BOX_OUT', i)) end
        if rt and not prevRetired[i]  then put(fmt('EV,%d,RETIRE,%d', t, i)); lastEvent = 'RETIRE car ' .. i end
        if fin and not prevFinished[i] then put(fmt('EV,%d,FINISH,%d,%d', t, i, c.racePosition)) end
      end
      prevInPitlane[i], prevInPit[i], prevRetired[i], prevFinished[i] = pl, pb, rt, fin
    end
  end
end

local function sampleSlow(sim, t)
  for i = 0, sim.carsCount - 1 do
    local c = ac.getCar(i)
    if c ~= nil and c.isActive then
      local w = c.wheels
      local flags = (c.isInPitlane and 1 or 0) + (c.isInPit and 2 or 0)
                  + (c.isRetired and 4 or 0) + (c.isRaceFinished and 8 or 0)
                  + (c.isAIGoingToPits and 16 or 0) + (c.isAIUsingRainTyres and 32 or 0)
                  + (c.drsAvailable and 64 or 0) + (c.drsActive and 128 or 0)
                  + (c.isLapValid and 256 or 0)
      local flat = math.max(num(w[0].tyreFlatSpot), num(w[1].tyreFlatSpot),
                            num(w[2].tyreFlatSpot), num(w[3].tyreFlatSpot))
      put(fmt('S,%d,%d,%.1f,%.1f,%.1f,%.1f,%.1f,%.1f,%.1f,%.1f,%.1f,%.4f,%.4f,%.4f,%.4f,%.1f,%.1f,%.1f,%.1f,%.1f,%.0f,%.3f,%d,%d,%d,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%d',
        t, i, num(c.fuel),
        num(w[0].tyreCoreTemperature), num(w[1].tyreCoreTemperature),
        num(w[2].tyreCoreTemperature), num(w[3].tyreCoreTemperature),
        num(w[0].tyrePressure), num(w[1].tyrePressure),
        num(w[2].tyrePressure), num(w[3].tyrePressure),
        num(w[0].tyreWear), num(w[1].tyreWear), num(w[2].tyreWear), num(w[3].tyreWear),
        num(c.damage[0]), num(c.damage[1]), num(c.damage[2]), num(c.damage[3]), num(c.damage[4]),
        num(c.engineLifeLeft), num(c.gearboxDamage),
        c.racePosition, c.lapCount, flags,
        num(c.kersCharge), flat,
        num(w[0].suspensionDamage), num(w[1].suspensionDamage),
        num(w[2].suspensionDamage), num(w[3].suspensionDamage),
        c.compoundIndex))
    end
  end
end

local function sampleWeather(sim, t)
  put(fmt('W,%d,%.1f,%.1f,%.3f,%.3f,%.3f,%.1f,%.0f,%d',
    t, sim.ambientTemperature, sim.roadTemperature, sim.roadGrip,
    sim.rainIntensity, sim.rainWetness, sim.windSpeedKmh, sim.windDirectionDeg,
    sim.raceFlagType))
end

-- ---- event callbacks (registered once at app load; guarded by `recording`) ---------

ac.onCarCollision(-1, function(carIndex)
  if not recording then return end
  local sim = ac.getSim()
  local c = ac.getCar(carIndex)
  if c == nil then return end
  -- nearest other car within 10 m = attribution candidate (collidedWith: 0 means track)
  local nearest, nearestD2, nearestSpd = -1, 100, 0
  for j = 0, sim.carsCount - 1 do
    if j ~= carIndex then
      local o = ac.getCar(j)
      if o ~= nil and o.isActive then
        local dx = o.position.x - c.position.x
        local dy = o.position.y - c.position.y
        local dz = o.position.z - c.position.z
        local d2 = dx * dx + dy * dy + dz * dz
        if d2 < nearestD2 then
          nearestD2, nearest, nearestSpd = d2, j, o.speedKmh
        end
      end
    end
  end
  -- collidedWith semantics (verified on real data): 0 = track, otherwise other car index + 1
  local isTrack = c.collidedWith == 0
  local key = isTrack and ('T' .. carIndex) or (carIndex .. ':' .. c.collidedWith)
  local nowS = sim.time / 1000
  local dedup = isTrack and COLL_DEDUP_TRACK_S or COLL_DEDUP_CAR_S
  if lastCollAt[key] ~= nil and nowS - lastCollAt[key] < dedup then return end
  lastCollAt[key] = nowS
  local rel = 0
  if nearest >= 0 then
    local o = ac.getCar(nearest)
    local vx = c.velocity.x - o.velocity.x
    local vy = c.velocity.y - o.velocity.y
    local vz = c.velocity.z - o.velocity.z
    rel = math.sqrt(vx * vx + vy * vy + vz * vz) * 3.6
  end
  put(fmt('EV,%d,COLL,%d,%d,%d,%.3f,%.1f,%.1f,%.1f,%.2f,%.2f,%.5f,%d',
    relT(sim), carIndex, c.collidedWith, nearest, num(c.collisionDepth),
    c.speedKmh, nearestSpd, rel, c.position.x, c.position.z,
    c.splinePosition, c.lapCount))
  lastEvent = fmt('COLL car %d (near %d)', carIndex, nearest)
end)

ac.onLapCompleted(-1, function(carIndex, lapTime, valid, cuts, lapsCount, splits)
  if not recording then return end
  local sim = ac.getSim()
  -- splits container might be 0-based (CSP array) or 1-based (plain table); handle both
  local s = ''
  pcall(function()
    if splits ~= nil then
      local n = #splits
      if splits[0] ~= nil then
        for k = 0, n - 1 do s = s .. ',' .. tostring(splits[k]) end
      else
        for k = 1, n do s = s .. ',' .. tostring(splits[k]) end
      end
    end
  end)
  put(fmt('EV,%d,LAP,%d,%d,%d,%d,%d%s',
    relT(sim), carIndex, lapTime, valid and 1 or 0, cuts, lapsCount, s))
end)

ac.onCarJumped(-1, function(carIndex)
  if not recording then return end
  local sim = ac.getSim()
  local c = ac.getCar(carIndex)
  put(fmt('EV,%d,JUMP,%d,%d', relT(sim), carIndex, c and c.resetCounter or 0))
  lastEvent = 'JUMP car ' .. carIndex
end)

ac.onSessionStart(function(sessionIndex, restarted)
  if restarted then restartPending = true end
end)

ac.onRelease(function()
  if recording then finalize('shutdown', ac.getSim()) end
  if pendingMerge then
    -- writes still in flight at shutdown: leave parts + pointer on disk;
    -- next launch salvages them (safer than merging a possibly-partial set)
    ac.log('[VRCLOG] shutdown with chunk writes pending — parts left for salvage')
  end
end)

-- ---- main loop -----------------------------------------------------------------------

function script.update(dt)
  local sim = ac.getSim()

  if not salvageDone then
    local ok, err = pcall(salvageIfNeeded)
    if not ok then ac.log('[VRCLOG] salvage error: ' .. tostring(err)) end
  end

  -- deferred merge from a finalize that had async writes in flight
  if pendingMerge then
    if pendingWrites == 0 or pendingMerge.waited > MERGE_WAIT_MAX_S then
      local pm = pendingMerge
      pendingMerge = nil
      -- on timeout the chunk-count check inside makes this fail safe (parts kept)
      completeMerge(pm.parts, pm.final, pm.chunks, pm.bytes, pm.reason)
    else
      pendingMerge.waited = pendingMerge.waited + dt
    end
  end

  -- session rotation / lifecycle ------------------------------------------------------
  if recording and (sim.currentSessionIndex ~= curSessionIndex or restartPending) then
    finalize(restartPending and 'session_restart' or 'session_change', sim)
    finalizedForIndex = -1  -- allow the new session to start below
  end
  if restartPending then
    restartCount = restartCount + 1
    restartPending = false
    finalizedForIndex = -1
  end
  if sim.currentSessionIndex ~= curSessionIndex then restartCount = 0 end

  if recording then
    if not prevResultsScreen and sim.isLookingAtSessionResults then
      finalize('results', sim)
      finalizedForIndex = sim.currentSessionIndex
    elseif not cfg.enabled then
      finalize('disabled', sim)
      finalizedForIndex = sim.currentSessionIndex
    end
  end
  prevResultsScreen = sim.isLookingAtSessionResults

  if not recording then
    if pendingMerge then return end  -- finish the previous file before a new one
    if cfg.enabled and sim.currentSessionIndex ~= finalizedForIndex
        and not sim.isReplayOnlyMode and not sim.isInMainMenu
        and (shouldRecordSession(ac.getSession(sim.currentSessionIndex))
             or sim.currentSessionIndex == forceRecordIndex) then
      startRecording(sim)
    else
      if cfg.enabled then
        statusText = (sim.currentSessionIndex == finalizedForIndex) and statusText or 'Waiting (session type not recorded)'
      else
        statusText = 'Disabled'
      end
      return
    end
  end

  -- live sampling -----------------------------------------------------------------------
  if sim.isPaused or sim.isReplayActive or sim.isInMainMenu or sim.dt <= 0 then return end

  local t = relT(sim)

  -- race-start reaction ------------------------------------------------------------------
  if startTrack ~= nil then
    if startTrack.green == nil then
      local tts = sim.timeToSessionStart
      if startTrack.prev ~= nil and startTrack.prev > 0 and tts <= 0 then
        -- green light happened inside this frame; tts is the (negative) overshoot, so
        -- sim.time + tts recovers the exact moment (clamped in case the field parks at -1)
        startTrack.green = sim.time + math.max(tts, -50)
        local greenT = math.max(0, math.floor(startTrack.green - t0 + 0.5))
        local moving = 0
        for i = 0, sim.carsCount - 1 do
          local c = ac.getCar(i)
          if c ~= nil and c.isActive then
            local isMoving = c.speedKmh > START_MOVING_KMH
            if isMoving then moving = moving + 1 end
            startTrack.cars[i] = {x = c.position.x, z = c.position.z,
                                  gasDone = c.gas > START_GAS_MIN, moveDone = isMoving}
          end
        end
        put(fmt('EV,%d,GREEN,%d', greenT, moving))
        for i, st in pairs(startTrack.cars) do
          -- throttle already applied at green = preloaded (delta 0); already moving at
          -- green (rolling start / jump start) = no standing reaction exists (delta -1)
          if st.gasDone  then put(fmt('EV,%d,LAUNCH,%d,0,0', greenT, i)) end
          if st.moveDone then put(fmt('EV,%d,LAUNCH,%d,1,-1', greenT, i)) end
        end
        lastEvent = fmt('GREEN (%d cars already moving)', moving)
      else
        startTrack.prev = tts
      end
    else
      local sinceGreen = sim.time - startTrack.green
      local allDone = true
      for i, st in pairs(startTrack.cars) do
        if not (st.gasDone and st.moveDone) then
          local c = ac.getCar(i)
          if c == nil or not c.isActive then
            st.gasDone, st.moveDone = true, true  -- car vanished: stop waiting for it
          else
            local delta = math.floor(sinceGreen + 0.5)
            if not st.gasDone and c.gas > START_GAS_MIN then
              st.gasDone = true
              put(fmt('EV,%d,LAUNCH,%d,0,%d', t, i, delta))
            end
            if not st.moveDone and c.speedKmh > START_SPEED_KMH then
              local dx, dz = c.position.x - st.x, c.position.z - st.z
              if dx * dx + dz * dz > START_DIST_M * START_DIST_M then
                st.moveDone = true
                put(fmt('EV,%d,LAUNCH,%d,1,%d', t, i, delta))
                lastEvent = fmt('LAUNCH car %d +%d ms', i, delta)
              end
            end
            if not (st.gasDone and st.moveDone) then allDone = false end
          end
        end
      end
      if allDone or sinceGreen > START_WINDOW_S * 1000 then startTrack = nil end
    end
  end

  local fastPeriod = 1 / math.max(5, math.min(30, cfg.fastHz))

  fastAcc = fastAcc + sim.dt
  if fastAcc >= fastPeriod then
    fastAcc = fastAcc % fastPeriod
    sampleFast(sim, t)
  end

  slowAcc = slowAcc + sim.dt
  if slowAcc >= 1 then
    slowAcc = slowAcc % 1
    sampleSlow(sim, t)
  end

  weatherAcc = weatherAcc + sim.dt
  if weatherAcc >= 5 then
    weatherAcc = weatherAcc % 5
    sampleWeather(sim, t)
  end

  if sim.raceFlagType ~= prevFlag then
    put(fmt('EV,%d,FLAG,%d', t, sim.raceFlagType))
    prevFlag = sim.raceFlagType
  end

  -- flush policy --------------------------------------------------------------------------
  flushAcc = flushAcc + dt
  if bufBytes >= FLUSH_BYTES or (flushAcc >= FLUSH_SECONDS and bufN > 0) then
    flushAcc = 0
    flushChunk(false)
  end

  statusText = fmt('REC %s  |  %.1f min  |  %d lines  |  %.1f MB  |  %d chunks%s',
    io.getFileName(finalPath or ''), t / 60000, linesTotal,
    (bytesTotal + bufBytes) / (1024 * 1024), chunkIdx,
    writeErrors > 0 and ('  |  ' .. writeErrors .. ' WRITE ERRORS') or '')
end

-- ---- UI -----------------------------------------------------------------------------------

function script.windowMain(dt)
  ui.text('VRC Race Logger — V' .. APP_VERSION .. ' (schema ' .. SCHEMA_VERSION .. ')')
  ui.separator()

  if ui.checkbox('Enable logging', cfg.enabled) then cfg.enabled = not cfg.enabled end
  if ui.checkbox('Record race sessions', cfg.recRace) then cfg.recRace = not cfg.recRace end
  if ui.checkbox('Record qualify sessions', cfg.recQuali) then cfg.recQuali = not cfg.recQuali end
  if ui.checkbox('Record practice/other sessions', cfg.recPractice) then cfg.recPractice = not cfg.recPractice end
  cfg.fastHz = math.floor(ui.slider('##fasthz', cfg.fastHz, 5, 30, 'Fast tier: %.0f Hz', true) + 0.5)

  ui.separator()
  ui.textColored(statusText, recording and rgbm(0.6, 1, 0.6, 1) or rgbm(0.85, 0.85, 0.85, 1))
  if lastEvent ~= '' then ui.text('Last event: ' .. lastEvent) end

  if cfg.debug then
    ui.text(fmt('buffer: %d lines / %.0f KB  |  pending writes: %d', bufN, bufBytes / 1024, pendingWrites))
    ui.text('parts dir: ' .. tostring(partsDir))
  end
  if ui.checkbox('Debug details', cfg.debug) then cfg.debug = not cfg.debug end

  ui.separator()
  if recording then
    if ui.button('Finalize & save now') then
      local sim = ac.getSim()
      finalize('manual', sim)
      finalizedForIndex = sim.currentSessionIndex  -- don't auto-restart this session
    end
  else
    if ui.button('Start new log now') then
      finalizedForIndex = -1
      -- one-shot override: record THIS session even if its type is filtered out
      -- (self-expires once the session index moves on)
      forceRecordIndex = ac.getSim().currentSessionIndex
      if not cfg.enabled then cfg.enabled = true end
    end
  end
  ui.sameLine()
  if ui.button('Open logs folder') then
    io.createDir(logsDir)
    os.openInExplorer(logsDir)
  end
end
