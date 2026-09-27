#!/usr/bin/env python3
"""Fit the evaluation weights to self-play results ("Texel tuning").

Every quiet position of every game is described by the terms the evaluation adds up
(material, pawn/soldier advancement, centralisation, mobility, ...).  The weights are chosen
so that  sigmoid(eval / K)  predicts the game's final result (1, 1/2 or 0 for White) as well
as possible.  Whole games are held out to measure the improvement honestly.

    python3 tools/tune_eval.py games1.jsonl games2.jsonl --out tools/tuned_weights.json   (games from selfplay.py)

The printed weights are then copied into engine/xqchess.cpp and pyengine.py (see --apply).
"""
import argparse
import fnmatch
import json
import multiprocessing as mp
import os
import sys
import zlib

import numpy as np
from scipy.optimize import minimize

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import pyengine as pe                                           # noqa: E402

T = pe
VAL_TYPES = [T.PAWN, T.KNIGHT, T.BISHOP, T.ROOK, T.QUEEN, T.SOLDIER, T.HORSE, T.ELEPHANT, T.ADVISOR, T.CHARIOT, T.CANNON]
CEN_TYPES = [T.KNIGHT, T.BISHOP, T.QUEEN, T.HORSE, T.ELEPHANT, T.ADVISOR, T.CANNON]
MOB_TYPES = [T.KNIGHT, T.BISHOP, T.ROOK, T.QUEEN, T.HORSE, T.ELEPHANT, T.ADVISOR, T.CHARIOT, T.CANNON]
PAWN_RR = [2, 3, 4, 5, 6]
SOLD_RR = [3, 4, 5, 6]
NAMES = ([f"VAL_{T.PCHARS[t]}" for t in VAL_TYPES] + [f"PAWN_ADV_{r}" for r in PAWN_RR] +
         [f"SOLD_ADV_{r}" for r in SOLD_RR] + [f"CENW_{T.PCHARS[t]}" for t in CEN_TYPES] +
         ["SEVENTH", "KING_MG", "KING_EG"] + [f"MOBW_{T.PCHARS[t]}" for t in MOB_TYPES] +
         ["BISHOP_PAIR", "PAWN_CENTRE", "TEMPO"] +
         # the general is a different royal from the king: its own rank/centre weights, a
         # constant (its value relative to a king) and a penalty for open lines around it
         ["GEN_MG", "GEN_EG", "GEN_BASE_MG", "GEN_BASE_EG", "OPEN_K", "OPEN_G"] +
         # endgame corrections to the piece values (added in proportion to how far the game
         # has progressed towards the endgame)
         [f"EG_{T.PCHARS[t]}" for t in VAL_TYPES])
NF = len(NAMES)
# weights that may take either sign, and the spread used by the pull towards the current values
SIGNED = {"GEN_BASE_MG", "GEN_BASE_EG", "OPEN_K", "OPEN_G"} | {n for n in NAMES if n.startswith("EG_")}
SD0 = {"GEN_BASE_MG": 100.0, "GEN_BASE_EG": 100.0, "OPEN_K": 10.0, "OPEN_G": 10.0, "GEN_MG": 10.0, "GEN_EG": 10.0}
SD0.update({n: 100.0 for n in NAMES if n.startswith("EG_")})


def open_lines(P, s):
    """Empty squares along the four orthogonal lines from s (how exposed a royal is to checks)."""
    n = 0
    for d in T.ORTH:
        q = s + d
        while P.b[q] == T.EMPTY:
            n += 1
            q += d
    return n


def current_weights():
    w = [T.VAL[t] for t in VAL_TYPES] + [T.PAWN_ADV[r] for r in PAWN_RR] + [T.SOLD_ADV_PROMO[r] for r in SOLD_RR]
    w += [T.CENW[t] for t in CEN_TYPES] + [T.EW["SEVENTH"], T.EW["KING_MG"], T.EW["KING_EG"]]
    w += [T.MOBW[t] for t in MOB_TYPES] + [T.EW["BISHOP_PAIR"], T.EW["PAWN_CENTRE"], T.EW["TEMPO"]]
    w += [T.EW.get(n, T.EW["KING_MG"] if n == "GEN_MG" else T.EW["KING_EG"] if n == "GEN_EG" else 0)
          for n in NAMES[len(w):NAMES.index("EG_P")]]
    w += [getattr(T, "VAL_EG", [0] * T.NTYPES)[t] for t in VAL_TYPES]
    return np.array(w, float)


def features(P):
    """Terms of the linear part of the evaluation, White minus Black, plus what the endgame
    scaling needs.  Mirrors pyengine.evaluate()."""
    f = np.zeros(NF)
    b = P.b
    cnt = [[0] * T.NTYPES, [0] * T.NTYPES]
    pawns, nonroyal, bishops = [0, 0], [0, 0], [0, 0]
    ml = []
    kings = []
    for s in T.SQ64:
        p = b[s]
        if not p:
            continue
        t, c = p & 15, p >> 4
        sgn = 1 if c == T.WHITE else -1
        cnt[c][t] += 1
        rr = T.RANK_[s] if c == T.WHITE else 7 - T.RANK_[s]
        cen = T.CENTER[s]
        if t in (T.KING, T.GENERAL):
            kings.append((sgn, rr, cen, t))
            f[NAMES.index("OPEN_K" if t == T.KING else "OPEN_G")] -= sgn * open_lines(P, s)
            continue
        nonroyal[c] += 1
        f[VAL_TYPES.index(t)] += sgn
        if t == T.PAWN:
            pawns[c] += 1
            if rr in PAWN_RR:
                f[11 + PAWN_RR.index(rr)] += sgn
            if T.FILE_[s] in (3, 4):
                f[NAMES.index("PAWN_CENTRE")] += sgn
        elif t == T.SOLDIER:
            pawns[c] += 1
            if rr in SOLD_RR:
                f[16 + SOLD_RR.index(rr)] += sgn
        elif t in (T.ROOK, T.CHARIOT):
            if rr == 6:
                f[NAMES.index("SEVENTH")] += sgn
        else:
            f[NAMES.index(f"CENW_{T.PCHARS[t]}")] += sgn * cen
        if t == T.BISHOP:
            bishops[c] += 1
        if t in MOB_TYPES:
            del ml[:]
            P.gen_piece(s, ml, False, False)
            f[NAMES.index(f"MOBW_{T.PCHARS[t]}")] += sgn * len(ml)
    f[NAMES.index("BISHOP_PAIR")] = (bishops[0] >= 2) - (bishops[1] >= 2)
    f[NAMES.index("TEMPO")] = 1 if P.side == T.WHITE else -1
    royal = [b[P.ksq[0]] & 15, b[P.ksq[1]] & 15]
    extra = {"cnt": cnt, "pawns": pawns, "nonroyal": nonroyal, "kings": kings, "half": P.half, "royal": royal}
    return f, extra


def position_rows(args):
    """Replay one game; return (features, extra, result, game id) for its quiet positions."""
    gid, g = args
    res = {"1-0": 1.0, "0-1": 0.0}.get(g["result"], 0.5)
    P = T.Pos(g["start"])
    if g.get("reason") == "insufficient" and P.b[P.ksq[0]] & 15 != P.b[P.ksq[1]] & 15:
        # recorded before the rule was corrected: a king against a bare general is not a draw,
        # the king always mates it (see the endgame solver), so the king's side won this game
        res = 1.0 if P.b[P.ksq[0]] & 15 == T.KING else 0.0
    rows = []
    prev_capture = False
    moves = g["moves"].split()
    for i, mv in enumerate(moves):
        m = T.parse_move(P, mv)
        if m is None:
            break
        quiet = not (m >> 24) & T.F_CAP and not (m >> 16) & 255
        if i >= 8 and quiet and not prev_capture and not P.in_check(P.side):
            f, extra = features(P)
            # bare royals and the solved endings are handled exactly, not by these weights
            if extra["nonroyal"][0] and extra["nonroyal"][1] and T.known_result(P) is None:
                rows.append((f, extra, res, gid))
        prev_capture = bool((m >> 24) & T.F_CAP)
        P.make(m)
    return rows


class Data:
    """Positions as arrays, so the evaluation can be recomputed quickly for any weights."""
    def __init__(self, F, extras):
        if F.shape[1] < NF:                          # rows cached before newer features were added
            F = np.hstack([F, np.zeros((F.shape[0], NF - F.shape[1]))])
        self.F = F
        mat_types = [t for t in VAL_TYPES if t not in (T.PAWN, T.SOLDIER)]
        self.mat_idx = [VAL_TYPES.index(t) for t in mat_types]
        self.CW = np.array([[x["cnt"][0][t] for t in mat_types] for x in extras], float)
        self.CB = np.array([[x["cnt"][1][t] for t in mat_types] for x in extras], float)
        self.pawnW = np.array([x["pawns"][0] for x in extras])
        self.pawnB = np.array([x["pawns"][1] for x in extras])
        self.xqW = np.array([x["royal"][0] == T.GENERAL and not T.can_mate_bare(x["cnt"][0], T.GENERAL) for x in extras])
        self.xqB = np.array([x["royal"][1] == T.GENERAL and not T.can_mate_bare(x["cnt"][1], T.GENERAL) for x in extras])
        self.half = np.minimum(np.array([x["half"] for x in extras], float), 100)
        kk = lambda x, typ, fn: sum(fn(sg, rr, cen) for sg, rr, cen, t in x["kings"] if t == typ)
        self.Krr = np.array([kk(x, T.KING, lambda sg, rr, cen: sg * -rr) for x in extras], float)
        self.Kcen = np.array([kk(x, T.KING, lambda sg, rr, cen: sg * cen) for x in extras], float)
        self.Grr = np.array([kk(x, T.GENERAL, lambda sg, rr, cen: sg * -rr) for x in extras], float)
        self.Gcen = np.array([kk(x, T.GENERAL, lambda sg, rr, cen: sg * cen) for x in extras], float)
        self.Gsgn = np.array([kk(x, T.GENERAL, lambda sg, rr, cen: sg) for x in extras], float)

    def subset(self, m):
        d = Data.__new__(Data)
        for k, v in self.__dict__.items():
            d.__dict__[k] = v[m] if isinstance(v, np.ndarray) else v
        return d


def features_at(w, D):
    """Feature matrix with the king terms filled in for the game phase implied by w."""
    val = w[D.mat_idx]
    MW, MB = D.CW @ val, D.CB @ val
    mg = np.minimum(MW + MB, 5000)
    Fk = D.F.copy()
    Fk[:, NAMES.index("KING_MG")] = D.Krr * mg / 5000
    Fk[:, NAMES.index("KING_EG")] = D.Kcen * (5000 - mg) / 5000
    Fk[:, NAMES.index("GEN_MG")] = D.Grr * mg / 5000
    Fk[:, NAMES.index("GEN_EG")] = D.Gcen * (5000 - mg) / 5000
    Fk[:, NAMES.index("GEN_BASE_MG")] = D.Gsgn * mg / 5000
    Fk[:, NAMES.index("GEN_BASE_EG")] = D.Gsgn * (5000 - mg) / 5000
    for j, t in enumerate(VAL_TYPES):
        Fk[:, NAMES.index(f"EG_{T.PCHARS[t]}")] = D.F[:, j] * (5000 - mg) / 5000
    return Fk, MW, MB


def multiplier(lin, MW, MB, D, prm, rook):
    strongW = lin >= 0
    nop = np.where(strongW, D.pawnW == 0, D.pawnB == 0)
    adv = np.where(strongW, MW - MB, MB - MW)
    sc = np.ones_like(lin)
    sc[nop & (adv < rook)] = prm["SCALE_NOPAWN"] / 128
    xq = np.where(strongW, D.xqW, D.xqB)
    sc = np.where(nop & xq, np.minimum(sc, prm["SCALE_XQ"] / 128), sc)
    return sc * (prm["FIFTY_DIV"] - D.half) / prm["FIFTY_DIV"]


def evaluate_all(w, D, prm):
    Fk, MW, MB = features_at(w, D)
    tempo = NAMES.index("TEMPO")
    lin = Fk @ w - Fk[:, tempo] * w[tempo]
    mult = multiplier(lin, MW, MB, D, prm, w[VAL_TYPES.index(T.ROOK)])
    return lin * mult + Fk[:, tempo] * w[tempo], Fk, mult


def loss(e, y, k):
    p = 1 / (1 + np.exp(-e / k))
    return float(np.mean((y - p) ** 2))


def logloss(e, y, k):
    p = np.clip(1 / (1 + np.exp(-e / k)), 1e-6, 1 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def fit_k(e, y):
    ks = np.exp(np.linspace(np.log(30), np.log(3000), 200))
    return float(ks[np.argmin([loss(e, y, k) for k in ks])])


def report(tag, e, y, k):
    print(f"  {tag}: squared error {loss(e, y, k):.4f}, log-loss {logloss(e, y, k):.4f}")


def calibration(e, y, k, title):
    print(f"\n  {title}: predicted vs actual White score by predicted-score bucket")
    p = 1 / (1 + np.exp(-e / k))
    edges = [0, .1, .25, .4, .6, .75, .9, 1.0001]
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (p >= lo) & (p < hi)
        if m.sum():
            print(f"    predicted {lo:4.2f}-{min(hi, 1):4.2f}: {m.sum():6d} positions, mean predicted {p[m].mean():.3f}, "
                  f"actual {y[m].mean():.3f}")


def apply_weights(path):
    """Write tuned weights into both engines (they must stay identical)."""
    import re
    with open(path) as fh:
        tw = json.load(fh)
    w = dict(zip(tw["names"], tw["weights"]))
    val = [0] * T.NTYPES
    for t in range(T.NTYPES):
        val[t] = w.get(f"VAL_{T.PCHARS[t]}", 0)
    pawn = list(T.PAWN_ADV)
    for r in PAWN_RR:
        pawn[r] = w[f"PAWN_ADV_{r}"]
    sold = list(T.SOLD_ADV_PROMO)
    for r in SOLD_RR:
        sold[r] = w[f"SOLD_ADV_{r}"]
    cenw = [w.get(f"CENW_{T.PCHARS[t]}", 0) if t in CEN_TYPES else 0 for t in range(T.NTYPES)]
    mobw = [w.get(f"MOBW_{T.PCHARS[t]}", 0) if t in MOB_TYPES else 0 for t in range(T.NTYPES)]
    sc = tw["scaling"]
    fifty = min(int(sc["FIFTY_DIV"]), 100000)
    arr = lambda v: "{" + ", ".join(str(int(x)) for x in v) + "}"
    lst = lambda v: "[" + ", ".join(str(int(x)) for x in v) + "]"
    cpp_path = os.path.join(HERE, "..", "engine", "xqchess.cpp")
    c = open(cpp_path).read()
    c = re.sub(r"int VAL\[NTYPES\] = \{[^}]*\};", f"int VAL[NTYPES] = {arr(val)};", c)
    c = re.sub(r"int PAWN_ADV\[8\] = \{[^}]*\};", f"int PAWN_ADV[8] = {arr(pawn)};", c)
    c = re.sub(r"int SOLD_ADV_PROMO\[8\] = \{[^}]*\};", f"int SOLD_ADV_PROMO[8] = {arr(sold)};", c)
    c = re.sub(r"int CENW\[NTYPES\] = \{[^}]*\};", f"int CENW[NTYPES] = {arr(cenw)};", c)
    c = re.sub(r"int MOBW\[NTYPES\] = \{[^}]*\};", f"int MOBW[NTYPES] = {arr(mobw)};", c)
    c = re.sub(r"int SEVENTH = -?\d+, KING_MG = -?\d+, KING_EG = -?\d+, BISHOP_PAIR = -?\d+, PAWN_CENTRE = -?\d+, TEMPO = -?\d+;",
               f"int SEVENTH = {w['SEVENTH']}, KING_MG = {w['KING_MG']}, KING_EG = {w['KING_EG']}, "
               f"BISHOP_PAIR = {w['BISHOP_PAIR']}, PAWN_CENTRE = {w['PAWN_CENTRE']}, TEMPO = {w['TEMPO']};", c)
    veg = [w.get(f"EG_{T.PCHARS[t]}", 0) for t in range(T.NTYPES)]
    c = re.sub(r"int VAL_EG\[NTYPES\] = \{[^}]*\};", f"int VAL_EG[NTYPES] = {arr(veg)};", c)
    for name in ("GEN_MG", "GEN_EG", "GEN_BASE_MG", "GEN_BASE_EG", "OPEN_K", "OPEN_G"):
        c = re.sub(rf"\b{name} = -?\d+", f"{name} = {w.get(name, 0)}", c, count=1)
    c = re.sub(r"int SCALE_NOPAWN = \d+, SCALE_XQ = \d+;", f"int SCALE_NOPAWN = {sc['SCALE_NOPAWN']}, SCALE_XQ = {sc['SCALE_XQ']};", c)
    c = re.sub(r"int FIFTY_DIV = \d+;", f"int FIFTY_DIV = {fifty};", c)
    c = re.sub(r"const double EVAL_K = [\d.]+;", f"const double EVAL_K = {tw['K']};", c)
    open(cpp_path, "w").write(c)
    py_path = os.path.join(HERE, "..", "pyengine.py")
    p = open(py_path).read()
    p = re.sub(r"VAL = \[[^\]]*\]", f"VAL = {lst(val)}", p, count=1)
    p = re.sub(r"PAWN_ADV = \[[^\]]*\]", f"PAWN_ADV = {lst(pawn)}", p, count=1)
    p = re.sub(r"SOLD_ADV_PROMO = \[[^\]]*\]", f"SOLD_ADV_PROMO = {lst(sold)}", p, count=1)
    p = re.sub(r"CENW = \[[^\]]*\]", f"CENW = {lst(cenw)}", p, count=1)
    p = re.sub(r"MOBW = \[[^\]]*\]", f"MOBW = {lst(mobw)}", p, count=1)
    for name in ("SEVENTH", "KING_MG", "KING_EG", "BISHOP_PAIR", "PAWN_CENTRE", "TEMPO",
                 "GEN_MG", "GEN_EG", "GEN_BASE_MG", "GEN_BASE_EG", "OPEN_K", "OPEN_G"):
        p = re.sub(rf'"{name}": -?\d+', f'"{name}": {w.get(name, 0)}', p, count=1)
    p = re.sub(r"VAL_EG = \[[^\]]*\]", f"VAL_EG = {lst(veg)}", p, count=1)
    p = re.sub(r'"SCALE_NOPAWN": \d+', f'"SCALE_NOPAWN": {sc["SCALE_NOPAWN"]}', p, count=1)
    p = re.sub(r'"SCALE_XQ": \d+', f'"SCALE_XQ": {sc["SCALE_XQ"]}', p, count=1)
    p = re.sub(r'"FIFTY_DIV": \d+', f'"FIFTY_DIV": {fifty}', p, count=1)
    p = re.sub(r"EVAL_K = [\d.]+", f"EVAL_K = {tw['K']}", p, count=1)
    open(py_path, "w").write(p)
    print(f"applied {path} to engine/xqchess.cpp and pyengine.py (rebuild the C++ engine)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", help="self-play games (.jsonl)")
    ap.add_argument("--out", default=os.path.join(HERE, "tuned_weights.json"))
    ap.add_argument("--holdout", type=float, default=0.2, help="fraction of games held out for validation")
    ap.add_argument("--l2", type=float, default=2e-3, help="pull towards the current weights")
    ap.add_argument("--anchor", type=float, default=1e-3,
                    help="strength of the prior that keeps knight/bishop/rook/queen near the standard chess values "
                         "320/330/500/900 (sd 10%%); 0 = no anchor")
    ap.add_argument("--cache", help="keep the extracted positions in this file (pickle) to skip replaying")
    ap.add_argument("--tie", default="VAL_R=VAL_J,MOBW_R=MOBW_J",
                    help="comma-separated pairs of weights forced to be equal (default: rook = chariot)")
    ap.add_argument("--freeze", default="",
                    help="comma-separated weight names (shell-style patterns, e.g. 'EG_*') kept at their current values")
    ap.add_argument("--apply", metavar="WEIGHTS.json",
                    help="don't fit: write these tuned weights into engine/xqchess.cpp and pyengine.py")
    a = ap.parse_args()
    if a.apply:
        apply_weights(a.apply)
        return
    if not a.files:
        ap.error("no game files given")

    games = []
    for fn in a.files:
        with open(fn) as fh:
            for line in fh:
                g = json.loads(line)
                if g.get("rules", {}) not in ({}, None):        # only games under the default rules
                    continue
                games.append(g)
    print(f"{len(games)} games")
    import pickle
    if a.cache and os.path.exists(a.cache):
        with open(a.cache, "rb") as fh:
            rows = pickle.load(fh)
    else:
        with mp.Pool(os.cpu_count()) as pool:
            chunks = pool.map(position_rows, list(enumerate(games)), chunksize=8)
        rows = [r for c in chunks for r in c]
        if a.cache:
            with open(a.cache, "wb") as fh:
                pickle.dump(rows, fh)
    F = np.array([r[0] for r in rows])
    extras = [r[1] for r in rows]
    y = np.array([r[2] for r in rows])
    gid = np.array([r[3] for r in rows])
    test = np.array([zlib.crc32(str(g).encode()) % 1000 < a.holdout * 1000 for g in gid])
    print(f"{len(rows)} quiet positions ({(~test).sum()} for fitting, {test.sum()} held out)")
    D = Data(F, extras)
    Dtr, Dte = D.subset(~test), D.subset(test)
    ytr, yte = y[~test], y[test]

    w0 = current_weights()
    prm0 = {"SCALE_NOPAWN": T.EW["SCALE_NOPAWN"], "SCALE_XQ": T.EW["SCALE_XQ"], "FIFTY_DIV": T.EW["FIFTY_DIV"]}
    e0tr = evaluate_all(w0, Dtr, prm0)[0]
    k0 = fit_k(e0tr, ytr)
    e0 = evaluate_all(w0, Dte, prm0)[0]
    print(f"\nCurrent weights (best scale K = {k0:.0f}):")
    report("fit set ", e0tr, ytr, k0)
    report("held out", e0, yte, k0)

    # --- fit the linear weights (scaling fixed per round), then the scale constants
    w, prm, k = w0.copy(), dict(prm0), k0
    sd = np.maximum(np.abs(w0), 5.0)
    for n_, v_ in SD0.items():
        sd[NAMES.index(n_)] = max(sd[NAMES.index(n_)], v_)
    tempo = NAMES.index("TEMPO")
    # tied weights: w = A @ v with fewer free parameters v
    groups = {i: i for i in range(NF)}
    for pair in filter(None, a.tie.split(",")):
        x, y2 = pair.split("=")
        groups[NAMES.index(y2)] = NAMES.index(x)
    free = sorted(set(groups.values()))
    A = np.zeros((NF, len(free)))
    for i in range(NF):
        A[i, free.index(groups[i])] = 1.0
    to_free = lambda full: np.array([full[f] for f in free])
    for rnd in range(4):
        _, Fk, mult = evaluate_all(w, Dtr, prm)
        G = Fk * mult[:, None]
        G[:, tempo] = Fk[:, tempo]

        GA = G @ A
        anchor_mu = {"VAL_N": 320, "VAL_B": 330, "VAL_R": 500, "VAL_Q": 900}
        am = np.zeros(NF)
        asd = np.ones(NF)
        amask = np.zeros(NF)
        for n_, mu in anchor_mu.items():
            i = NAMES.index(n_)
            am[i], asd[i], amask[i] = mu, 0.1 * mu, 1.0

        def obj(z):
            v, logk = z[:-1], z[-1]
            kk = np.exp(logk)
            full = A @ v
            e = GA @ v
            p = 1 / (1 + np.exp(-e / kk))
            r = p - ytr
            pen1 = a.l2 * np.sum(((full - w0) / sd) ** 2 * (1 - amask)) / NF
            pen2 = a.anchor * np.sum(amask * ((full - am) / asd) ** 2)
            val = np.mean(r ** 2) + pen1 + pen2
            dp = r * p * (1 - p)
            gv = (2 / len(ytr)) * (GA.T @ (dp / kk))
            gv += A.T @ (2 * a.l2 * (full - w0) / sd ** 2 / NF * (1 - amask) + 2 * a.anchor * amask * (full - am) / asd ** 2)
            gk = (2 / len(ytr)) * np.sum(dp * (-e / kk))
            return val, np.append(gv, gk)
        bounds = [(10, None) if NAMES[f].startswith("VAL_") else (None, None) if NAMES[f] in SIGNED else (0, None)
                  for f in free] + [(np.log(20), np.log(20000))]
        for j, f in enumerate(free):
            if any(fnmatch.fnmatch(NAMES[f], pat) for pat in filter(None, a.freeze.split(","))):
                bounds[j] = (w0[f], w0[f])
        z = minimize(obj, np.append(to_free(w), np.log(k)), jac=True, method="L-BFGS-B", bounds=bounds).x
        w, k = A @ z[:-1], float(np.exp(z[-1]))
        best = None
        for sn in (16, 24, 32, 48, 64, 96, 128):
            for sx in (8, 16, 24, 32, 48, 64, 128):
                for fd in (120, 150, 200, 300, 10 ** 6):
                    p2 = {"SCALE_NOPAWN": sn, "SCALE_XQ": sx, "FIFTY_DIV": fd}
                    l2 = loss(evaluate_all(w, Dtr, p2)[0], ytr, k)
                    if best is None or l2 < best[0]:
                        best = (l2, p2)
        prm = best[1]
        print(f"  round {rnd + 1}: fit-set squared error {best[0]:.4f}, scaling {prm}")

    # express in centipawns: anchored fits already are; otherwise make a pawn = 100
    c = 1.0 if a.anchor > 0 else 100.0 / w[0]
    w_cp, k_cp = w * c, k * c
    e1tr = evaluate_all(w_cp, Dtr, prm)[0]
    e1 = evaluate_all(w_cp, Dte, prm)[0]
    print(f"\nTuned weights (scale K = {k_cp:.0f}):")
    report("fit set ", e1tr, ytr, k_cp)
    report("held out", e1, yte, k_cp)
    calibration(e0, yte, k0, "Held-out positions, CURRENT evaluation")
    calibration(e1, yte, k_cp, "Held-out positions, TUNED evaluation")
    print("\n  mean evaluation (pawns, White's view) of held-out positions from DRAWN games:")
    d = yte == 0.5
    print(f"    current {np.mean(e0[d]) / 100:+.2f}   tuned {np.mean(e1[d]) / 100:+.2f}")
    out = {"names": NAMES, "weights": [int(round(x)) for x in w_cp], "scaling": prm, "K": round(k_cp, 1),
           "old_weights": [int(x) for x in w0]}
    print("\n  " + "  ".join(f"{n}={v}" for n, v in zip(NAMES, out["weights"])))
    with open(a.out, "w") as fh:
        json.dump(out, fh, indent=1)
    print(f"\nwritten to {a.out}")


if __name__ == "__main__":
    main()
