#!/usr/bin/env python3
"""Play two engines against each other.

Each opening is played twice with the colours swapped, so neither engine benefits from the
armies being unequal.  Games are adjudicated with pyengine's rules (checkmate, stalemate,
threefold repetition, fifty-move rule, bare royals) or drawn after --maxplies.

An engine is "fairy", "native" or "python" (as for the other tools), or the path of an engine
binary that speaks the xqchess engine protocol (e.g. an older build of engine/xqchess).

    python3 tools/match.py --a engine/xqchess --b old/xqchess --openings games.jsonl --pairs 100 --nodes 20000
    python3 tools/match.py --a fairy --b native --movetime 100 --openings games.jsonl

The openings are positions taken from games recorded by selfplay.py (games.jsonl above).
"""
import argparse
import json
import math
import os
import random
import subprocess
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import pyengine as pe                                           # noqa: E402


class Player:
    def __init__(self, spec):
        from xqchess import engine_command
        cmd = engine_command(spec) if spec in ("fairy", "native", "python", "auto") else [spec]
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)

    def move(self, start, moves, go):
        self.p.stdin.write(f"position fen {start}" + (" moves " + " ".join(moves) if moves else "") + "\n")
        self.p.stdin.write(go + "\n")
        self.p.stdin.flush()
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("engine died")
            if line.startswith("bestmove"):
                return line.split()[1]

    def close(self):
        try:
            self.p.stdin.write("quit\n")
            self.p.stdin.flush()
            self.p.wait(timeout=5)
        except Exception:
            self.p.kill()


def play(white, black, start, go, maxplies):
    P = pe.Pos(start)
    moves = []
    for ply in range(maxplies):
        reason, result = pe.game_over(P)
        if reason:
            return result, reason, moves
        eng = white if P.side == pe.WHITE else black
        mv = eng.move(start, moves, go)
        m = pe.parse_move(P, mv)
        if m is None:                                   # illegal move: that side loses
            return ("0-1" if P.side == pe.WHITE else "1-0"), f"illegal move {mv}", moves
        P.make(m)
        moves.append(mv)
    return "1/2-1/2", "maxplies", moves


def openings_from(files, n, ply, seed):
    """Start positions: the position after `ply` plies of recorded self-play games."""
    rng = random.Random(seed)
    games = []
    for fn in files:
        with open(fn) as fh:
            games += [json.loads(l) for l in fh]
    rng.shuffle(games)
    out = []
    for g in games:
        mv = g["moves"].split()
        if len(mv) <= ply + 10:
            continue
        P = pe.Pos(g["start"])
        for x in mv[:ply]:
            P.make(pe.parse_move(P, x))
        if not pe.game_over(P)[0]:
            out.append(P.fen())
        if len(out) >= n:
            break
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--a", required=True, help="engine A: fairy, native, python or an engine binary")
    ap.add_argument("--b", required=True, help="engine B: fairy, native, python or an engine binary")
    ap.add_argument("--openings", nargs="+", required=True, help="self-play .jsonl files to take openings from")
    ap.add_argument("--pairs", type=int, default=50, help="openings; each is played twice")
    ap.add_argument("--ply", type=int, default=8, help="take the position after this many plies")
    ap.add_argument("--nodes", type=int, default=20000, help="search nodes per move")
    ap.add_argument("--movetime", type=int, default=0,
                    help="milliseconds per move instead of a node count (fairer between different engines)")
    ap.add_argument("--maxplies", type=int, default=300)
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 2)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", help="write the games here (jsonl)")
    a = ap.parse_args()

    starts = openings_from(a.openings, a.pairs, a.ply, a.seed)
    go = f"go movetime {a.movetime}" if a.movetime else f"go nodes {a.nodes}"
    jobs = [(fen, swap) for fen in starts for swap in (False, True)]
    results = []
    lock = threading.Lock()

    def worker(my_jobs):
        pa, pb = Player(a.a), Player(a.b)
        try:
            for fen, swap in my_jobs:
                white, black = (pb, pa) if swap else (pa, pb)
                res, reason, moves = play(white, black, fen, go, a.maxplies)
                score_a = {"1-0": 1.0, "0-1": 0.0}.get(res, 0.5)
                if swap:
                    score_a = 1 - score_a
                with lock:
                    results.append({"start": fen, "a_is_white": not swap, "result": res, "reason": reason,
                                    "score_a": score_a, "moves": " ".join(moves)})
                    n = len(results)
                    s = sum(r["score_a"] for r in results)
                    print(f"\r{n}/{len(jobs)} games, A scores {s / n:.3f}   ", end="", file=sys.stderr, flush=True)
        finally:
            pa.close()
            pb.close()

    threads = [threading.Thread(target=worker, args=(jobs[i::a.workers],)) for i in range(a.workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print(file=sys.stderr)
    sc = [r["score_a"] for r in results]
    n = len(sc)
    m = sum(sc) / n
    se = math.sqrt(sum((x - m) ** 2 for x in sc) / (n - 1) / n) if n > 1 else 0
    w = sum(x == 1 for x in sc)
    d = sum(x == 0.5 for x in sc)
    elo = lambda p: -400 * math.log10(1 / min(max(p, 1e-3), 1 - 1e-3) - 1)
    print(f"A vs B: +{w} ={d} -{n - w - d}  score {m:.3f} (95% CI {m - 1.96 * se:.3f}-{m + 1.96 * se:.3f}), "
          f"Elo {elo(m):+.0f} [{elo(m - 1.96 * se):+.0f}, {elo(m + 1.96 * se):+.0f}]")
    if a.out:
        with open(a.out, "w") as fh:
            for r in results:
                fh.write(json.dumps(r) + "\n")


if __name__ == "__main__":
    main()
