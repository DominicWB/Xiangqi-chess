#!/usr/bin/env python3
"""Fit the snipers-chess sniper bonus to self-play results.

Each sampled position of the games records the engine's search score for White and the material.
If a sniper had been worth c centipawns more than a bishop instead of the c0 the games were played
with, the score would have been about  score + (c - c0) * (White's snipers).  The bonus c and the
scale K are chosen so that

    P(White scores) = 1 / (1 + exp(-(score + (c - c0) * snipers) / K))

predicts the games' results best (each game weighted equally).  K is the scale that turns the
engine's scores into White's expected score (pyengine.EVAL_K, EVAL_K_OWN).  The games need
varying numbers of snipers and varying results: use selfplay.py --handicap.

    python3 tools/fit_sniper_bonus.py C0 games.jsonl [more.jsonl ...]
"""
import argparse
import json

import numpy as np
from scipy.optimize import minimize


def load(paths, min_ply=8, max_score=2500):
    rows = []
    for path in paths:
        with open(path) as fh:
            for line in fh:
                g = json.loads(line)
                result = {"1-0": 1.0, "0-1": 0.0}.get(g["result"], 0.5)
                smp = [s for s in g.get("samples", []) if s[0] >= min_ply and abs(s[1]) < max_score]
                for s in smp:
                    snipers = s[2 + 2]                   # White's bishops (counts are P N B R Q ... per colour)
                    rows.append((s[1], snipers, result, 1.0 / len(smp)))
    return np.array(rows, float)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("c0", type=float, help="the sniper bonus the games were played with, in centipawns")
    ap.add_argument("files", nargs="+")
    a = ap.parse_args()
    data = load(a.files)
    if not len(data):
        raise SystemExit("no samples")
    score, snipers, y, w = data.T

    def nll(p):
        c, k = p
        z = (score + (c - a.c0) * snipers) / k
        return float((w * (np.logaddexp(0, z) - y * z)).sum() / w.sum())

    print(f"{len(data)} positions from {w.sum():.0f} games")
    best = minimize(nll, [a.c0, 150.0], method="Nelder-Mead")
    print(f"best fit: a sniper is worth {best.x[0]:.0f} centipawns more than a bishop, scale K = {best.x[1]:.0f}")
    for k in (181.7,):
        c = minimize(lambda p: nll([p[0], k]), [a.c0], method="Nelder-Mead").x[0]
        print(f"with the xqchess scale K = {k}: {c:.0f} centipawns")


if __name__ == "__main__":
    main()
