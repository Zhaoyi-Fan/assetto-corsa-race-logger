"""track_model — fast_lane.ai v7 -> centerline, ribbon, corners, lateral offsets.

Format (proven by projects\\VRC\\scripts\\inspect_ac_ai_line.py):
  16 B header (<ii = version 7, count); count x <ffffi (x,y,z,cumlen,idx);
  <i count; count x <18f payload (f0 speed, f4 radius, f5 sideL, f6 sideR).
Note: some AC assets have Z sign opposite to other data sources. We auto-detect
the convention against actual logged car positions (pick_z_sign) instead of
hardcoding it.
"""
from __future__ import annotations

import json
import os
import struct
import numpy as np


class TrackModel:
    def __init__(self, pts, cum, payload):
        self.pts = pts                      # (N,3) x,y,z
        # stored cumlen is unreliable in some generators' files (seen: all zeros on
        # fn_barcelona) -> rebuild from point geometry unless sane
        seg = np.linalg.norm(np.diff(pts[:, [0, 2]], axis=0), axis=1)
        geo_cum = np.concatenate([[0.0], np.cumsum(seg)])
        if cum[-1] <= 0 or np.any(np.diff(cum) < 0) or abs(cum[-1] - geo_cum[-1]) > geo_cum[-1] * 0.2:
            cum = geo_cum
        self.cum = cum                      # (N,) meters
        self.total = float(cum[-1])
        self.s = cum / self.total           # (N,) normalized 0..1
        self.speed = payload[:, 0]
        self.radius = payload[:, 4]         # payload radius (unpopulated on some lines)
        self.side_l = payload[:, 5]
        self.side_r = payload[:, 6]
        # some line generators leave payload sides unpopulated too (like radius/cumlen);
        # dead sides collapse the map ribbon AND poison the off-line evidence, so fall
        # back to a constant half-width when they look broken
        self.sides_ok = float(np.median(self.side_l + self.side_r)) >= 2.0
        if not self.sides_ok:
            self.side_l = np.full(len(pts), 5.0)
            self.side_r = np.full(len(pts), 5.0)
        self.corners = []                   # [{n,name,s0,s1,dir}]
        self._build_frames()
        self._build_geom_radius()

    # -- geometry -------------------------------------------------------------------
    def _build_frames(self):
        p = self.pts[:, [0, 2]]             # xz
        d = np.roll(p, -1, axis=0) - p
        L = np.linalg.norm(d, axis=1)
        L[L == 0] = 1e-6
        self.tan = d / L[:, None]           # (N,2) unit tangent (forward)
        # left-of-travel normal; sign convention validated empirically per track
        self.left = np.stack([self.tan[:, 1], -self.tan[:, 0]], axis=1)

    def _build_geom_radius(self):
        """Curvature radius from the polyline itself (generator-independent)."""
        h = np.arctan2(self.tan[:, 0], self.tan[:, 1])
        dh = np.diff(h, append=h[:1])
        dh = np.abs((dh + np.pi) % (2 * np.pi) - np.pi)
        ds = np.diff(self.cum, append=self.cum[-1:] + 1.0)
        ds[ds <= 0] = 1e-3
        r = ds / np.maximum(dh, 1e-9)
        # smooth over ~15 m so point noise doesn't fragment corners
        step = max(np.median(ds), 0.5)
        w = max(3, int(round(15.0 / step)) | 1)
        k = np.ones(w) / w
        pad = w // 2
        r = np.convolve(np.pad(1 / np.clip(r, 5, 5000), pad, mode="wrap"), k, "valid")
        self.geom_radius = 1 / np.clip(r, 1 / 5000, 1 / 5)

    def flip_z(self):
        self.pts = self.pts * np.array([1.0, 1.0, -1.0])
        self._build_frames()

    def idx_at(self, s):
        """Fractional interpolation base index for normalized spline s (array ok)."""
        s = np.mod(np.asarray(s, dtype=np.float64), 1.0)
        i = np.searchsorted(self.s, s, side="right") - 1
        return np.clip(i, 0, len(self.s) - 1)

    def pos_at(self, s):
        i = self.idx_at(s)
        return self.pts[i][:, [0, 2]] if np.ndim(i) else self.pts[i, [0, 2]]

    def lateral_offset(self, x, z, s):
        """Signed offset (m) of world (x,z) from the line at spline s. + = left of travel."""
        i = self.idx_at(s)
        dx = x - self.pts[i, 0]
        dz = z - self.pts[i, 2]
        return dx * self.left[i, 0] + dz * self.left[i, 1]

    def dist_to_line(self, x, z, s):
        i = self.idx_at(s)
        return np.hypot(x - self.pts[i, 0], z - self.pts[i, 2])

    # -- corners ----------------------------------------------------------------------
    def detect_segments(self, r_max=250.0, min_len=15.0, merge_gap=45.0):
        """Corner candidate segments from the geometric curvature profile."""
        r = self.geom_radius
        mask = r < r_max
        segs, i, n = [], 0, len(mask)
        while i < n:
            if mask[i]:
                j = i
                while j + 1 < n and mask[j + 1]:
                    j += 1
                segs.append([i, j])
                i = j + 1
            else:
                i += 1
        # merge close segments
        merged = []
        for a, b in segs:
            if merged and self.cum[a] - self.cum[merged[-1][1]] < merge_gap:
                merged[-1][1] = b
            else:
                merged.append([a, b])
        out = []
        for a, b in merged:
            if self.cum[b] - self.cum[a] < min_len:
                continue
            sl = slice(a, b + 1)
            # turn direction from heading change
            h0, h1 = self.tan[a], self.tan[min(b, len(self.tan) - 1)]
            cross = h0[0] * h1[1] - h0[1] * h1[0]
            out.append({"s0": float(self.s[a]), "s1": float(self.s[b]),
                        "m0": float(self.cum[a]), "m1": float(self.cum[b]),
                        "r_min": float(self.geom_radius[sl].min()),
                        "v_min": float(self.speed[sl].min()),
                        # AC xz frame: positive cross = right turn (validated: La Source = R)
                        "dir": "R" if cross > 0 else "L"})
        return out

    def load_corners(self, json_path):
        with open(json_path, "r", encoding="utf-8") as f:
            d = json.load(f)
        self.corners = d["corners"]
        self.straights = d.get("straights", [])

    @staticmethod
    def _corner_label(c):
        return f"T{c['n']} {c['name']}" if c.get("name") else f"T{c['n']}"

    def nearest_corner(self, s):
        """(corner dict, 'in'|'before'|'after') for a spline position; (None, '') if
        the track has no corners loaded."""
        s = float(s) % 1.0
        for c in self.corners:
            if c["s0"] <= s <= c["s1"]:
                return c, "in"
        best, bd, where = None, 9e9, ""
        for c in self.corners:
            d0 = (s - c["s1"]) % 1.0     # distance past this corner's exit
            d1 = (c["s0"] - s) % 1.0     # distance before next corner's entry
            if d0 < bd:
                bd, best, where = d0, c, "after"
            if d1 < bd:
                bd, best, where = d1, c, "before"
        return best, where

    def corner_key(self, s):
        """Language-independent grouping key for hotspot aggregation (corner only,
        no before/after modifier so approach+apex incidents count together)."""
        c, _ = self.nearest_corner(s)
        return f"T{c['n']}" if c is not None else f"s={float(s) % 1.0:.3f}"

    def corner_at(self, s, lang="zh"):
        """Human label for a spline position (corner, named straight, or nearest)."""
        s = float(s) % 1.0
        c, where = self.nearest_corner(s)
        if c is not None and where == "in":
            return self._corner_label(c)
        for st in getattr(self, "straights", []):
            if st["s0"] <= s <= st["s1"]:
                return f"{st['name']} 直道" if lang == "zh" else f"{st['name']} straight"
        if c is None:
            return f"s={s:.3f}"
        lbl = self._corner_label(c)
        if lang == "zh":
            return f"{lbl} {'出弯后' if where == 'after' else '入弯前'}"
        return f"after {lbl}" if where == "after" else f"before {lbl}"

    # -- map polylines -------------------------------------------------------------------
    def ribbon(self, step_m=6.0, margin=1.3):
        """Decimated centerline + edge polylines for the report map (x,z lists)."""
        keep = [0]
        for i in range(1, len(self.cum)):
            if self.cum[i] - self.cum[keep[-1]] >= step_m:
                keep.append(i)
        k = np.asarray(keep)
        c = self.pts[k][:, [0, 2]]
        le = c + self.left[k] * (self.side_l[k] + margin)[:, None]
        re = c - self.left[k] * (self.side_r[k] + margin)[:, None]
        return c, le, re


def load_fast_lane(path):
    data = open(path, "rb").read()
    version, count = struct.unpack_from("<ii", data, 0)
    if version != 7:
        raise ValueError(f"unexpected AI spline version {version}")
    pts = np.frombuffer(data, dtype=np.dtype([("x", "<f4"), ("y", "<f4"), ("z", "<f4"),
                                              ("cum", "<f4"), ("id", "<i4")]),
                        count=count, offset=16)
    payload_off = 16 + count * 20 + 4
    payload = np.frombuffer(data, dtype="<f4", count=count * 18,
                            offset=payload_off).reshape(count, 18)
    xyz = np.stack([pts["x"], pts["y"], pts["z"]], axis=1).astype(np.float64)
    return TrackModel(xyz, pts["cum"].astype(np.float64), payload.astype(np.float64))


def pick_z_sign(tm, rd):
    """Auto-detect the z-axis convention: logged car positions must sit ON the line."""
    absent = set(getattr(rd, "absent_cars", []))
    f = next((rd.F[ci] for ci in range(rd.n_cars)
              if ci not in absent and len(rd.F[ci]["t"])), None)
    if f is None:
        return tm, float("inf")
    n = len(f["t"])
    sel = slice(n // 4, n // 2, 7) if n >= 8 else slice(0, n)  # mid-race, on track
    d_as_is = np.median(tm.dist_to_line(f["x"][sel], f["z"][sel], f["spline"][sel]))
    tm.flip_z()
    d_flip = np.median(tm.dist_to_line(f["x"][sel], f["z"][sel], f["spline"][sel]))
    if d_as_is <= d_flip:
        tm.flip_z()  # restore
    return tm, float(min(d_as_is, d_flip))


def default_ac_root():
    """AC install root: AC_ROOT env var, else first drive with a standard Steam path."""
    cands = [os.environ.get("AC_ROOT", "")] + [
        f"{d}:\\Program Files (x86)\\Steam\\steamapps\\common\\assettocorsa"
        for d in "CDEFG"]
    for c in cands:
        if c and os.path.isdir(c):
            return c
    return cands[1]


def resolve_ai_path(meta, ac_root=None):
    tf = meta.get("trackFull", "")
    parts = tf.split("/")
    base = os.path.join(ac_root or default_ac_root(), "content", "tracks", *parts)
    p = os.path.join(base, "ai", "fast_lane.ai")
    return p if os.path.isfile(p) else None


def sibling_corners(meta, ai_path, corners_dir, ac_root=None):
    """Curated corners json of ANOTHER layout of the same track whose fast_lane.ai is
    byte-identical to this layout's -> path, or None.

    Layout families that only change zones / textures (the 2026 `f12026` layouts reuse the
    2025 AI lines) get the curated corner names for free instead of an auto-numbered skeleton.
    """
    import glob
    import hashlib
    tf = meta.get("trackFull", "")
    track = tf.split("/")[0]
    if not track or "/" not in tf or not ai_path or not os.path.isfile(ai_path):
        return None
    with open(ai_path, "rb") as f:
        want = hashlib.md5(f.read()).hexdigest()
    root = ac_root or default_ac_root()
    for cand in sorted(glob.glob(os.path.join(corners_dir, track + "-*.json"))):
        layout = os.path.basename(cand)[len(track) + 1:-5]
        if layout == tf.split("/", 1)[1]:
            continue
        p = os.path.join(root, "content", "tracks", track, layout, "ai", "fast_lane.ai")
        if not os.path.isfile(p):
            continue
        with open(p, "rb") as f:
            if hashlib.md5(f.read()).hexdigest() == want:
                return cand
    return None


if __name__ == "__main__":
    import sys
    tm = load_fast_lane(sys.argv[1])
    print(f"points={len(tm.s)} total={tm.total:.1f} m")
    if len(sys.argv) > 2:  # calibrate against a log
        import vrclog_parser
        rd = vrclog_parser.parse(sys.argv[2])
        tm, med = pick_z_sign(tm, rd)
        print(f"z-convention median on-line distance: {med:.2f} m")
        # empirical sign check via pit boxes (stuck cars parked): report offsets of
        # retired cars at end of race
        for ev in rd.events:
            if ev["type"] == "RETIRE":
                ci = ev["car"]
                f = rd.F[ci]
                off = tm.lateral_offset(f["x"][-1], f["z"][-1], f["spline"][-1])
                print(f"  retired car {ci} final offset {off:+.1f} m (pit box side)")
                break
    segs = tm.detect_segments()
    print(f"detected {len(segs)} corner segments:")
    for i, c in enumerate(segs):
        print(f"  seg{i:2d} s {c['s0']:.4f}-{c['s1']:.4f}  m {c['m0']:6.0f}-{c['m1']:6.0f}"
              f"  rMin {c['r_min']:6.1f}  vMin {c['v_min']:5.1f}  {c['dir']}")
