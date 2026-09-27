#!/usr/bin/env python3
"""Check how well an evaluation predicts game results, on games it was not tuned on.

For every quiet position of the given games (from move 5 on; not in check, not just after a
capture) the evaluation is turned into an expected score for White,
    expected = 1 / (1 + exp(-eval / EVAL_K)),
and compared with the game's actual result.  Several engine versions can be compared on the
same positions by passing their pyengine.py files with --engine.

    python3 tools/validate_eval.py games.jsonl
    python3 tools/validate_eval.py games.jsonl --engine old/pyengine.py --engine pyengine.py

(games.jsonl: games recorded by selfplay.py that the evaluation was not tuned on.)
"""
import argparse
import importlib.util
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))


def load_engine(path, tag):
    spec = importlib.util.spec_from_file_location(f"engine_{tag}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def positions(games, engines):
    """Yield (result for White, plies left, [White-view eval per engine]) for quiet positions."""
    for g in games:
        res = {"1-0": 1.0, "0-1": 0.0}.get(g["result"], 0.5)
        ps = [e.Pos(g["start"]) for e in engines]
        mv = g["moves"].split()
        prev_cap = False
        for i, x in enumerate(mv):
            e0, P0 = engines[0], ps[0]
            m = e0.parse_move(P0, x)
            if m is None:
                break
            quiet = not (m >> 24) & e0.F_CAP and not (m >> 16) & 255
            if i >= 8 and quiet and not prev_cap and not P0.in_check(P0.side):
                sgn = 1 if P0.side == e0.WHITE else -1
                yield res, len(mv) - i, [sgn * e.evaluate(P) for e, P in zip(engines, ps)]
            prev_cap = bool((m >> 24) & e0.F_CAP)
            for e, P in zip(engines, ps):
                P.make(e.parse_move(P, x))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", help="self-play games (.jsonl)")
    ap.add_argument("--engine", action="append", default=[], metavar="[LABEL=]PATH",
                    help="pyengine.py of an engine version to test (repeat to compare; default: this one)")
    a = ap.parse_args()
    specs = a.engine or ["pyengine.py=" + os.path.join(HERE, "..", "pyengine.py")]
    names = [s.split("=", 1)[0] if "=" in s else s for s in specs]
    paths = [s.split("=", 1)[1] if "=" in s else s for s in specs]
    engines = [load_engine(p, k) for k, p in enumerate(paths)]

    games, seen = [], set()
    for fn in a.files:
        with open(fn) as fh:
            for line in fh:
                g = json.loads(line)
                key = (g["start"], g["moves"])
                if key not in seen:                      # repeated games would count twice
                    seen.add(key)
                    games.append(g)
    rows = list(positions(games, engines))
    y = [r[0] for r in rows]
    n = len(rows)
    print(f"{n} quiet positions from {len(games)} distinct games\n")
    for k, (name, eng) in enumerate(zip(names, engines)):
        K = getattr(eng, "EVAL_K", None)
        ev = [r[2][k] for r in rows]
        if K is None:                                    # older engines had no fitted scale: fit one
            K = min((sum((yy - 1 / (1 + math.exp(-e / kk))) ** 2 for yy, e in zip(y, ev)), kk)
                    for kk in [30 * 1.05 ** j for j in range(110)])[1]
        p = [1 / (1 + math.exp(-e / K)) for e in ev]
        ll = -sum(yy * math.log(max(pp, 1e-6)) + (1 - yy) * math.log(max(1 - pp, 1e-6)) for yy, pp in zip(y, p)) / n
        print(f"{name}: scale K = {K:.0f}, log-loss {ll:.3f}, mean expected score {sum(p) / n:.3f}, "
              f"actual {sum(y) / n:.3f}")
        print("   expected score    positions   mean expected   actual")
        for lo, hi in zip([0, .1, .25, .4, .6, .75, .9], [.1, .25, .4, .6, .75, .9, 1.0001]):
            idx = [i for i in range(n) if lo <= p[i] < hi]
            if idx:
                print(f"   {lo:4.2f} - {min(hi, 1):4.2f}   {len(idx):11d}   {sum(p[i] for i in idx) / len(idx):13.3f}"
                      f"   {sum(y[i] for i in idx) / len(idx):6.3f}")
        draw = [i for i in range(n) if y[i] == 0.5]
        late = [i for i in draw if rows[i][1] <= 40]
        if draw:
            print(f"   drawn games: mean evaluation {sum(ev[i] for i in draw) / len(draw) / 100:+.2f} pawns"
                  f" (White's view); in their last 20 moves {sum(ev[i] for i in late) / max(len(late), 1) / 100:+.2f},"
                  f" and {sum(ev[i] >= 150 for i in late) / max(len(late), 1):.0%} of those positions score +1.5 or more")
        print()


if __name__ == "__main__":
    main()
