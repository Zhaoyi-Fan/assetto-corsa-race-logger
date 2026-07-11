"""thresholds — every tunable knob of incident detection/attribution in one place.

Calibrated on: vrclog_20260709_000356 spa race (first real log). Iterate log-driven:
change here, re-run detectors.py against a log, compare against what you remember.
"""

# ---- loss-of-control conditions (F tier, ~15 Hz) -------------------------------------
SPIN_BETA_DEG      = 55.0   # |car slip angle| beyond this = spinning
SLIDE_BETA_DEG     = 16.0   # ... beyond this (but < spin) = big slide
SLIDE_MIN_DUR_S    = 0.45   # slide must persist this long to count
SLIDE_MIN_SPEED    = 60.0   # km/h; slower slides are noise (hairpin rotation etc.)
SPIN_MIN_DUR_S     = 0.30
SPIN_MIN_SPEED     = 30.0   # km/h at onset

OFF_WHEELS         = 4      # wheelsOutside >= this = excursion...
OFF_SURF_FRAC      = 0.75   # ...or this fraction of wheels on grass/gravel/sand
OFF_MIN_DUR_S      = 0.35
OFF_MIN_SPEED      = 25.0   # km/h at entry (slower + no stuck = crawling, ignore)

STUCK_SPEED        = 3.0    # km/h
STUCK_MIN_DUR_S    = 5.0    # stationary this long inside an excursion = stuck

BETA_SPEED_GATE    = 15.0   # km/h; slip angle is noise below this speed

# understeer/oversteer classification at excursion onset
UNDERSTEER_BETA    = 12.0   # |beta| below this at onset -> understeer candidate
OVERSTEER_BETA     = 30.0   # |beta| above this at onset -> oversteer/spin exit
UNDERSTEER_ND      = 1.00   # front ndSlip (1 s pre-onset mean) must exceed this
UNDERSTEER_RATIO   = 1.15   # ...and exceed rear by this factor

# ---- events ---------------------------------------------------------------------------
CONTACT_GROUP_S    = 3.0    # COLL events of the same pair within this window = one contact
SIDE_BY_SIDE_M     = 1.4    # |lateral offset difference| at/above this at contact = side-by-side
                            # (below = same line -> rear-end); ~F1 car width is 2.0 m
SCRAPE_SPEED       = 110.0  # raw=0 COLL at/above this speed = floor scrape (ignore)
WALL_MIN_DEPTH     = 0.008  # raw=0 contacts shallower than this are noise

# ---- episode building -------------------------------------------------------------------
MERGE_GAP_S        = 4.0    # same-car phases closer than this merge into one episode
CONTACT_LINK_S     = 6.0    # contact links two cars' episodes within +- this window
PILEUP_WINDOW_S    = 12.0   # episodes within this window...
PILEUP_SPLINE      = 0.020  # ...and this spline distance, 3+ cars = pileup
DNF_LOOKBACK_S     = 90.0   # RETIRE attaches to this car's episode within this lookback

# ---- attribution ---------------------------------------------------------------------------
ATTR_CONTACT_S     = 7.0    # contact within this window before loss = candidate cause
ATTR_KERB_S        = 0.6    # kerb + vertical-G spike within this window before loss
KERB_ACC_Y_G       = 2.0    # |acc_y| spike threshold
OFFLINE_M          = 2.5    # median |lateral offset - usable side| beyond edge... simplified:
                            # |offset| > side_of_that_side + this = badly off line
DIRTY_AIR_GAP_S    = 0.65   # time gap to car ahead below this...
DIRTY_AIR_DUR_S    = 2.0    # ...for this long right before loss, while in a corner
COLD_TYRE_LAP      = 1      # lap number <= this...
COLD_TYRE_TEMP     = 76.0   # ...and front core temp below this = cold-tyre evidence
WORN_TYRE          = 0.35   # wear beyond this (0..1 scale of usable wear window)
FLAT_SPOT          = 0.15
AVOIDANCE_DIST_M   = 160.0  # stationary car ahead within this during caution = avoidance
AI_LINE_MIN_EPISODES = 3    # distinct AI episodes at one corner -> line-suspect note

# ---- report ----------------------------------------------------------------------------------
CARD_MIN_SEVERITY  = 25.0   # episodes below this appear in the table but not as cards
