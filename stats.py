#!/usr/bin/env python3
"""Summarise self-play games: results, Elo gap, and fitted piece values.

    python3 stats.py games.jsonl [more.jsonl ...] [--plot values.png] [--bootstrap 200]

Piece values are estimated from the games themselves (not the engine's built-in
values): for every sampled quiet position we record the material on the board
and the final game result, then fit

    P(White scores) = sigmoid(b0 + sum_t w_t * (#White t - #Black t))

by maximum likelihood.  w_t / w_pawn is the value of piece type t in pawns.
Confidence intervals come from resampling whole games (bootstrap).  Values are
only identifiable for piece types whose count actually varies across positions,
so the estimates are much better on games from `selfplay.py --handicap`.
"""
import argparse
import json
import math
from collections import Counter, defaultdict

import numpy as np
from scipy.optimize import minimize

from xqchess import PIECE_NAMES, TYPE_LETTERS

ROYAL = {"K", "G"}
# X is a sniper: in games with the snipers rule, White's bishops are counted as X, not as B
FEATURES = [t for t in TYPE_LETTERS if t not in ROYAL] + ["X"]
NAMES = dict(PIECE_NAMES, X="sniper")


def snipers_rule(g):
    return str(g.get("rules", {}).get("Snipers", False)).lower() in ("true", "1")


def rescore(g):
    """Games recorded before the insufficient-material rule was corrected were drawn when only a
    king and a general were left.  The king always mates a lone general, so they count as wins for
    the king's side (reason "bare general (rescored)")."""
    if g.get("reason") == "insufficient" and g.get("result") == "1/2-1/2":
        board = g.get("final", "").split(" ")[0]
        if ("K" in board and "g" in board) or ("G" in board and "k" in board):
            g["result"] = "1-0" if "K" in board else "0-1"
            g["reason"] = "bare general (rescored)"
    return g


def load(paths):
    games = []
    for p in paths:
        with open(p) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    games.append(rescore(json.loads(line)))
    return games


def white_score(g):
    return {"1-0": 1.0, "0-1": 0.0}.get(g["result"], 0.5)


def elo(s):
    s = min(max(s, 1e-4), 1 - 1e-4)
    return -400 * math.log10(1 / s - 1)


def score_summary(scores):
    s = np.asarray(scores, float)
    m = s.mean()
    se = s.std(ddof=1) / math.sqrt(len(s)) if len(s) > 1 else float("nan")
    return m, se


def design(games, min_ply):
    """Rows: one per sample.  Returns X (n x f), y (n), game index (n)."""
    X, y, gi = [], [], []
    for k, g in enumerate(games):
        res = white_score(g)
        for smp in g.get("samples", []):
            ply, counts = smp[0], smp[2:]
            if ply < min_ply:
                continue
            w = dict(zip(TYPE_LETTERS, counts[:13]), X=0)
            b = dict(zip(TYPE_LETTERS, counts[13:26]), X=0)
            if snipers_rule(g):
                w["X"], w["B"] = w["B"], 0
            X.append([w[t] - b[t] for t in FEATURES])
            y.append(res)
            gi.append(k)
    return np.asarray(X, float), np.asarray(y, float), np.asarray(gi, int)


def fit_logistic(X, y, weights, l2=1e-3):
    n, f = X.shape
    Xb = np.hstack([np.ones((n, 1)), X])

    def nll(beta):
        z = Xb @ beta
        # soft-label cross-entropy, numerically stable
        loss = weights * (np.logaddexp(0, z) - y * z)
        grad = Xb.T @ (weights * (1 / (1 + np.exp(-z)) - y))
        reg = l2 * np.sum(beta[1:] ** 2)
        return loss.sum() / weights.sum() + reg, grad / weights.sum() + np.r_[0, 2 * l2 * beta[1:]]

    r = minimize(nll, np.zeros(f + 1), jac=True, method="L-BFGS-B")
    return r.x


def piece_values(games, min_ply, n_boot, seed=0):
    X, y, gi = design(games, min_ply)
    if len(y) < 50 or y.std() < 1e-9:
        return None
    # each game gets total weight 1, however many samples it contributed
    per_game = Counter(gi.tolist())
    w = np.array([1.0 / per_game[k] for k in gi])
    varying = [j for j in range(X.shape[1]) if X[:, j].std() > 1e-9]
    Xv = X[:, varying]
    beta = fit_logistic(Xv, y, w)

    rng = np.random.default_rng(seed)
    by_game = defaultdict(list)
    for row, k in enumerate(gi):
        by_game[k].append(row)
    keys = list(by_game)
    boots = []
    for _ in range(n_boot):
        pick = rng.choice(keys, size=len(keys), replace=True)
        rows = np.concatenate([by_game[k] for k in pick])
        boots.append(fit_logistic(Xv[rows], y[rows], w[rows]))
    boots = np.array(boots) if boots else None

    out = {"n_samples": len(y), "n_games": len(keys), "intercept": beta[0], "types": {},
           "beta": beta, "varying": varying, "boots": boots}
    pawn_col = FEATURES.index("P")
    pawn_j = varying.index(pawn_col) + 1 if pawn_col in varying else None
    for jj, j in enumerate(varying, start=1):
        t = FEATURES[j]
        raw = beta[jj]
        entry = {"raw": raw, "raw_ci": None, "pawns": None, "pawns_ci": None}
        if boots is not None:
            entry["raw_ci"] = tuple(np.percentile(boots[:, jj], [5, 95]))
        if pawn_j:
            entry["pawns"] = raw / beta[pawn_j]
            if boots is not None:
                ratio = boots[:, jj] / boots[:, pawn_j]
                entry["pawns_ci"] = tuple(np.percentile(ratio, [5, 95]))
        out["types"][t] = entry
    out["not_identifiable"] = [FEATURES[j] for j in range(len(FEATURES)) if j not in varying]
    return out


def material_features(fen, snipers=False):
    counts = Counter(fen.split()[0])
    if snipers:
        counts["X"], counts["B"] = counts["B"], 0
    return [counts[t] - counts[t.lower()] for t in FEATURES]


def predict(pv, fen, snipers=False):
    """Model-predicted White score for the material in `fen`, with a 90% bootstrap interval."""
    x = material_features(fen, snipers)

    def score(beta):
        z = beta[0] + sum(beta[jj] * x[j] for jj, j in enumerate(pv["varying"], start=1))
        return 1 / (1 + math.exp(-z))

    p = score(pv["beta"])
    if pv.get("boots") is None:
        return p, None
    bs = [score(b) for b in pv["boots"]]
    return p, (float(np.percentile(bs, 5)), float(np.percentile(bs, 95)))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+")
    ap.add_argument("--min-ply", type=int, default=8, help="ignore samples before this ply")
    ap.add_argument("--bootstrap", type=int, default=200)
    ap.add_argument("--plot", help="write a PNG of the fitted piece values")
    ap.add_argument("--hide", default="", metavar="LETTERS",
                    help="piece letters to leave out of the plot, e.g. Q when it only appears via promotion")
    ap.add_argument("--json", help="write the numbers to a JSON file")
    ap.add_argument("--predict", nargs="*", default=[], metavar="FEN",
                    help="also print the fitted model's predicted White score for these positions "
                         "(quote each FEN; material only, so use it to compare candidate setups)")
    a = ap.parse_args()

    games = load(a.files)
    if not games:
        raise SystemExit("no games")
    report = {"n_games": len(games), "setups": {}}

    print(f"{len(games)} games\n")
    print("Results by setup (score = points per game: win 1, draw 1/2, loss 0)")
    by_setup = defaultdict(list)
    for g in games:
        key = (g.get("setup", "?"), g.get("start_fen", "?"), g.get("handicap", 0), g.get("nodes", 0),
               json.dumps(g.get("rules", {}), sort_keys=True), g.get("engine", "native"))
        by_setup[key].append(g)
    army = []
    for (setup, fen, hc, nodes, rules, engine), gs in by_setup.items():
        res = Counter(g["result"] for g in gs)
        ws = [white_score(g) for g in gs]
        wiw = gs[0].get("western_is_white", True)
        # both armies Western (chess, snipers chess): there is no Western-against-Xiangqi score
        mixed = any(ch in "SHEAJCGsheajcg" for ch in fen.split()[0])
        if mixed:
            army += [s if wiw else 1 - s for s in ws]
        m, se = score_summary(ws)
        lo, hi = m - 1.96 * se, m + 1.96 * se
        reasons = Counter(g["reason"] for g in gs)
        plies = np.array([g["plies"] for g in gs])
        label = setup + (f" (handicap {hc})" if hc else "") + (f", {nodes:,} nodes/move" if nodes else "")
        if engine != "native":
            label += f", {engine} engine"
        if rules != "{}":
            label += f", options {rules}"
        print(f"\n  {label}: {len(gs)} games   start {fen}")
        if mixed:
            print(f"    White {'= Western army' if wiw else '= Xiangqi army'}")
        print(f"    +{res['1-0']} ={res['1/2-1/2']} -{res['0-1']}   White score {m:.3f} "
              f"(95% CI {max(lo, 0):.3f}-{min(hi, 1):.3f}),  Elo gap {elo(m):+.0f} "
              f"[{elo(max(lo, 1e-4)):+.0f}, {elo(min(hi, 1 - 1e-4)):+.0f}]")
        if mixed:
            print(f"    Western army score {m if wiw else 1 - m:.3f},  Xiangqi army score {1 - m if wiw else m:.3f}")
        print("    endings: " + ", ".join(f"{k} {v}" for k, v in reasons.most_common()))
        print(f"    length: median {int(np.median(plies))} plies, range {plies.min()}-{plies.max()}")
        distinct = len(set((g.get("start"), g["moves"]) for g in gs))
        if distinct < len(gs):
            print(f"    distinct games: {distinct} of {len(gs)}" +
                  ("   ** many games are repeats: the confidence interval is too narrow **" if distinct < 0.9 * len(gs) else ""))
        report["setups"][label] = {"games": len(gs), "white_score": m, "white_score_se": se,
                                   "western_is_white": wiw, "results": dict(res), "reasons": dict(reasons)}
    if len(by_setup) > 1 and len(army) > 1:
        m, se = score_summary(army)
        print(f"\n  All games, Western-army score {m:.3f} +/- {1.96 * se:.3f}  (Elo {elo(m):+.0f})")
        report["western_army_score"] = m

    pv = piece_values(games, a.min_ply, a.bootstrap)
    if pv is None:
        print("\nPiece values not fitted: too few samples, or every game had the same result "
              "(run selfplay.py with --handicap to create material imbalances).")
    else:
        print(f"\nFitted piece values from game outcomes ({pv['n_samples']} positions, {pv['n_games']} games; "
              f"90% bootstrap CI)")
        print(f"  {'piece':10s} {'pawns':>7s}   {'90% CI':>15s}   {'logit/piece':>11s}")
        for t, e in sorted(pv["types"].items(), key=lambda kv: -(kv[1]["pawns"] or kv[1]["raw"])):
            p = f"{e['pawns']:7.2f}" if e["pawns"] is not None else "    n/a"
            ci = f"{e['pawns_ci'][0]:6.2f} - {e['pawns_ci'][1]:5.2f}" if e["pawns_ci"] else ""
            army_name = "Western" if t in "PNBRQX" else "Xiangqi"
            print(f"  {NAMES[t]:10s} {p}   {ci:>15s}   {e['raw']:11.3f}   ({army_name})")
        if pv["not_identifiable"]:
            print("  never varied in these games (no estimate): " +
                  ", ".join(NAMES[t] for t in pv["not_identifiable"]))
        print(f"  intercept (White's edge with equal material, logit): {pv['intercept']:+.3f}")
        fens = ([games[0]["start_fen"]] if games[0].get("start_fen") else []) + list(a.predict)
        if fens:
            print("\nModel-predicted White score from material alone (90% bootstrap CI):")
            report["predictions"] = {}
            for fen in fens:
                p, ci = predict(pv, fen, snipers_rule(games[0]))
                cis = f"  ({ci[0]:.3f}-{ci[1]:.3f})" if ci else ""
                print(f"  {p:.3f}{cis}   {fen.split()[0]}")
                report["predictions"][fen] = {"white_score": p, "ci90": ci}
        report["piece_values"] = {k: v for k, v in pv.items() if k not in ("beta", "varying", "boots")}
        if a.plot:
            plot(pv, a.plot, hide=a.hide.upper())
            print(f"\nplot written to {a.plot}")
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(report, fh, indent=2, default=float)


def plot(pv, path, hide=""):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    items = [(t, e) for t, e in pv["types"].items() if e["pawns"] is not None and t not in hide]
    items.sort(key=lambda kv: kv[1]["pawns"])
    names = [NAMES[t] for t, _ in items]
    vals = [e["pawns"] for _, e in items]
    lo = [e["pawns"] - e["pawns_ci"][0] if e["pawns_ci"] else 0 for _, e in items]
    hi = [e["pawns_ci"][1] - e["pawns"] if e["pawns_ci"] else 0 for _, e in items]
    colours = ["#2a6fb0" if t in "PNBRQ" else "#c0392b" for t, _ in items]
    fig, ax = plt.subplots(figsize=(7, 0.45 * len(items) + 1.2))
    ax.barh(names, vals, xerr=[lo, hi], color=colours, capsize=3)
    ax.set_xlabel("value in pawns (fitted from game results, 90% CI)")
    ax.axvline(0, color="#888", lw=0.8)
    from matplotlib.patches import Patch
    ax.legend(handles=[Patch(color="#2a6fb0", label="Western"), Patch(color="#c0392b", label="Xiangqi")],
              loc="lower right", frameon=False)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(path, dpi=150)


if __name__ == "__main__":
    main()
