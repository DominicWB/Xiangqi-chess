#!/usr/bin/env python3
"""Play many engine-vs-engine games in parallel and save them as JSON lines.

Each game starts with a few random plies (for variety) and is then played by the
engine at a fixed node budget per move.  Every output line is one game:
    {"setup", "western_is_white", "result", "reason", "plies", "moves", "final", "samples", ...}
"samples" holds [ply, engine score for White, piece counts...] for quiet positions and is
what stats.py uses to estimate piece values.

Examples
    python3 selfplay.py --games 200 --out games.jsonl
    python3 selfplay.py --games 200 --setup noqueen --out games_noqueen.jsonl
    python3 selfplay.py --games 500 --nodes 50000 --workers 8 --elephant-eye --out eye.jsonl
"""
import argparse
import json
import os
import queue
import random
import subprocess
import sys
import threading
import time

from analyse import add_rule_args, rules_from_args
from xqchess import SETUPS, engine_command, engine_kind, rule_commands


def handicap_fen(fen, k, rng):
    """Remove 0..k[0] random non-royal pieces from White and 0..k[1] from Black."""
    parts = fen.split()
    grid = {}
    for i, row in enumerate(parts[0].split("/")):
        f = 0
        for ch in row:
            if ch.isdigit():
                f += int(ch)
            else:
                grid[(f, 7 - i)] = ch
                f += 1
    for is_white, kk in ((True, k[0]), (False, k[1])):
        own = [sq for sq, p in grid.items() if p.isupper() == is_white and p.upper() not in "KG"]
        for sq in rng.sample(own, rng.randint(0, min(kk, len(own)))):
            del grid[sq]
    rows = []
    for r in range(7, -1, -1):
        row, e = "", 0
        for f in range(8):
            p = grid.get((f, r))
            if p is None:
                e += 1
            else:
                row += (str(e) if e else "") + p
                e = 0
        rows.append(row + (str(e) if e else ""))
    parts[0] = "/".join(rows)
    return " ".join(parts)


def distinct_openings(bases, plies, margin, rules, engine_kind, seed):
    """For each start position, play `plies` random moves chosen among those scoring within
    `margin` centipawns of the best (depth-4 search), making every resulting position distinct.
    If the sound moves run out of variety, the margin is widened for that game.
    Returns (list of FENs, {FEN: opening moves})."""
    import gamefile
    import pyengine as pe
    from xqchess import Engine
    gamefile.apply_rules(rules)
    rng = random.Random(seed * 7919 + 1)
    cache, seen, fens, openings = {}, set(), [], {}
    widened = 0
    with Engine(rules=rules, kind=engine_kind) as eng:
        for base in bases:
            m = margin
            for attempt in range(300):
                moves, P = [], pe.Pos(base)
                for _ in range(plies):
                    key = (base, tuple(moves))
                    if key not in cache:
                        legal = [pe.move_str(x) for x in P.legal_moves()]
                        if not legal or pe.game_over(P)[0]:
                            cache[key] = []
                        else:
                            eng.position(base, moves)
                            lines = eng.analyse(depth=4, multipv=len(legal))
                            cache[key] = [(ln["pv"][0], ln.get("score_cp", 100000 if ln.get("mate", 0) > 0 else -100000))
                                          for ln in lines if ln["pv"]]
                    cand = cache[key]
                    if not cand:
                        break
                    ok = [mv for mv, sc in cand if sc >= cand[0][1] - m]
                    mv = rng.choice(ok)
                    moves.append(mv)
                    P.make(pe.parse_move(P, mv))
                fen = P.fen()
                if fen not in seen:
                    break
                if attempt % 10 == 9:
                    m = int(m * 1.5) + 10
            widened += m > margin
            seen.add(fen)
            fens.append(fen)
            openings[fen] = moves
    if widened:
        print(f"note: {widened} of {len(bases)} openings needed a wider margin than {margin} to be distinct",
              file=sys.stderr)
    return fens, openings


def western_is_white(fen):
    board = fen.split()[0]
    return "K" in board


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--games", type=int, default=100)
    ap.add_argument("--nodes", type=int, default=20000, help="search nodes per move (strength/speed knob)")
    ap.add_argument("--depth", type=int, default=64, help="optional depth cap per move")
    ap.add_argument("--random-plies", type=int, default=4, help="random opening plies for variety")
    ap.add_argument("--random-margin", type=int, default=100,
                    help="random opening moves are drawn from moves within this many centipawns of the best "
                         "(depth-4 search); -1 = any legal move")
    ap.add_argument("--maxplies", type=int, default=300, help="game is drawn after this many plies")
    ap.add_argument("--resign", type=int, default=0,
                    help="adjudicate a loss after 3 own moves scoring <= -CP (0 = play to the end)")
    ap.add_argument("--setup", choices=list(SETUPS), default="default",
                    help="default: Xiangqi army White, Western army Black; noqueen: the same without "
                         "Black's queen; v1/v1-swapped: the first version's layout")
    ap.add_argument("--fen", help="custom start position (overrides --setup)")
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 2)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", default="games.jsonl")
    ap.add_argument("--append", action="store_true", help="append to --out instead of overwriting")
    ap.add_argument("--repeatable-openings", action="store_true",
                    help="old behaviour: let each engine pick its random opening moves (games may repeat)")
    ap.add_argument("--handicap", default="0", metavar="K[,K2]",
                    help="remove 0..K random non-royal pieces from each side at the start of every game "
                         "(K,K2: up to K from White and K2 from Black); creates the material imbalances "
                         "stats.py needs to estimate piece values")
    ap.add_argument("--engine", choices=["auto", "fairy", "native", "python"], default="auto",
                    help="fairy: Fairy-Stockfish with this game's rules (strongest); native: the compiled "
                         "xqchess engine; python: the pure-Python engine; auto (default): Fairy-Stockfish "
                         "if it has been built, else native, else Python")
    add_rule_args(ap)
    a = ap.parse_args()

    fen = a.fen or SETUPS[a.setup]
    setup = "custom" if a.fen else a.setup
    rules = rules_from_args(a)
    exe = engine_command(a.engine)
    if exe[-1].endswith("pyengine.py"):
        print("note: using the pure-Python engine; self-play will be about 60x slower than with the "
              "compiled engine (consider fewer --nodes)", file=sys.stderr)
    workers = max(1, min(a.workers, a.games))
    per = [a.games // workers + (i < a.games % workers) for i in range(workers)]

    rng = random.Random(a.seed)
    hc = [int(x) for x in a.handicap.split(",")]
    hc = (hc[0], hc[-1])
    a.handicap = a.handicap if any(hc) else 0
    game_fens = [handicap_fen(fen, hc, rng) if a.handicap else fen for _ in range(a.games)]
    openings = {}
    if a.random_plies and not a.repeatable_openings:
        # play the random opening moves here, so that every game starts from a different position
        game_fens, openings = distinct_openings(game_fens, a.random_plies, a.random_margin, rules, a.engine, a.seed)
    procs, start = [], 0
    for i, n in enumerate(per):
        p = subprocess.Popen(exe, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
        rp = 0 if openings else a.random_plies
        common = (f"nodes {a.nodes} depth {a.depth} randomplies {rp} randmargin {a.random_margin} "
                  f"maxplies {a.maxplies} resign {a.resign}")
        if a.handicap or openings:
            cmds = [f"selfplay games 1 {common} seed {a.seed * 100000 + start + k + 1} fen {game_fens[start + k]}"
                    for k in range(n)]
        else:
            cmds = [f"selfplay games {n} {common} seed {a.seed * 1000 + i + 1} fen {fen}"]
        start += n
        # write commands from a thread so a full stdout pipe can never deadlock us
        text = "\n".join(rule_commands(rules) + cmds + ["quit"]) + "\n"
        threading.Thread(target=lambda p=p, text=text: (p.stdin.write(text), p.stdin.flush()), daemon=True).start()
        procs.append(p)

    meta = {"setup": setup, "start_fen": fen, "western_is_white": western_is_white(fen), "nodes": a.nodes,
            "engine": engine_kind(exe),
            "random_plies": a.random_plies, "random_margin": a.random_margin, "handicap": a.handicap, "rules": rules}
    # One reader thread per engine process feeds a shared queue.
    q = queue.Queue()

    def reader(proc):
        for line in proc.stdout:
            q.put(line)
        q.put(None)

    for p in procs:
        threading.Thread(target=reader, args=(p,), daemon=True).start()
    t0, done, score, finished = time.time(), 0, 0.0, 0
    with open(a.out, "a" if a.append else "w") as out:
        while finished < len(procs):
            line = q.get()
            if line is None:
                finished += 1
                continue
            if not line.startswith("{"):
                if line.startswith("error"):
                    print(line.strip(), file=sys.stderr)
                continue
            g = json.loads(line)
            if "error" in g:                     # e.g. a bad FEN: report it, it is not a game
                print(f"error: {g['error']}", file=sys.stderr)
                continue
            g.pop("game", None)
            g.update(meta)
            if openings and g.get("start") in openings:
                g["opening"] = openings[g["start"]]
            out.write(json.dumps(g) + "\n")
            out.flush()
            done += 1
            score += {"1-0": 1, "0-1": 0}.get(g["result"], 0.5)
            el = time.time() - t0
            print(f"\r{done}/{a.games} games  White score {score / done:.3f}  "
                  f"{el:.0f}s elapsed, ~{el / done * (a.games - done):.0f}s left   ", end="", file=sys.stderr)
    for p in procs:
        p.wait()
    print(f"\nwrote {done} games to {a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
