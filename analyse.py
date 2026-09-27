#!/usr/bin/env python3
"""Find the best moves in a position.

Examples
    python3 analyse.py                                   # start position, 5 s, top 5 moves
    python3 analyse.py --moves e3e4 e7e5 --time 10
    python3 analyse.py --fen "rnbqkbnr/pppp1ppp/8/4p3/4S3/S2S3S/1C4C1/JHEAGEHJ w kq - 0 2" --depth 14
    python3 analyse.py --setup noqueen --multipv 8      # Black (Western army) without its queen
    python3 analyse.py --elephant-eye --value H=280 C=330
"""
import argparse
import os
import sys

import fairy_engine
import pyengine as pe
from xqchess import ENGINE_LABELS, Engine, SETUP_RULES, SETUPS


def add_rule_args(ap):
    g = ap.add_argument_group("rule and evaluation options")
    g.add_argument("--no-horse-block", action="store_true", help="horse cannot be hobbled (moves like a knight)")
    g.add_argument("--elephant-eye", action="store_true", help="elephant is blocked by a piece on its midpoint")
    g.add_argument("--stalemate-loss", action="store_true", help="stalemated side loses (Xiangqi rule) instead of a draw")
    g.add_argument("--no-soldier-sideways", action="store_true", help="soldiers never move sideways")
    g.add_argument("--soldier-promotion", choices=["any", "xiangqi", "western", "none"], default="any",
                   help="what a soldier may promote to on the last rank: any piece except a king/general "
                        "(default), only Xiangqi pieces, only Q/R/B/N, or no promotion")
    g.add_argument("--snipers", action="store_true",
                   help="White's bishops are snipers: they can also take the first enemy piece along a "
                        "diagonal without moving (on with --setup snipers)")
    g.add_argument("--value", nargs="*", default=[], metavar="L=CP",
                   help="override engine piece values, e.g. H=280 C=330 (letters: PNBRQ SHEAJC)")


def rules_from_args(a):
    rules = {}
    if a.no_horse_block: rules["HorseBlock"] = False
    if a.elephant_eye: rules["ElephantEye"] = True
    if a.stalemate_loss: rules["StalemateLoss"] = True
    if a.no_soldier_sideways: rules["SoldierSideways"] = False
    if a.soldier_promotion != "any": rules["SoldierPromotion"] = a.soldier_promotion
    if a.snipers or (getattr(a, "setup", None) in SETUP_RULES and not getattr(a, "fen", None)):
        rules.update(SETUP_RULES.get(a.setup, {}))
        if a.snipers: rules["Snipers"] = True
    if a.value:
        rules["Value"] = {kv.split("=")[0].upper(): int(kv.split("=")[1]) for kv in a.value}
    return rules


def fmt_score(line, white_to_move, own=False):
    if "mate" in line:
        m = line["mate"]
        return f"mate {m}" if m > 0 else f"mated {-m}"
    cp = line["score_cp"]
    white = cp if white_to_move else -cp
    # expected score for White (win 1, draw 1/2), from the scale fitted to self-play results
    # (own=True: Fairy-Stockfish's own evaluation, used for snipers chess, has a scale of its own)
    expected = 100 * pe.expected_score(white, own)
    return f"{cp / 100:+.2f}  (White {white / 100:+.2f}, {expected:3.0f}%)"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fen", help="position (default: the start position)")
    ap.add_argument("--setup", choices=list(SETUPS), default="default",
                    help="start position when --fen is not given (default: Xiangqi White, Western Black; "
                         "noqueen: the same without Black's queen)")
    ap.add_argument("--moves", nargs="*", default=[], help="moves played from the position, e.g. e2e4 b7e7")
    ap.add_argument("--time", type=float, help="seconds to think (default 5 unless --depth/--nodes)")
    ap.add_argument("--depth", type=int)
    ap.add_argument("--nodes", type=int)
    ap.add_argument("--multipv", type=int, default=5, help="number of best moves to report")
    ap.add_argument("--quiet", action="store_true", help="don't print search progress")
    ap.add_argument("--engine", choices=["auto", "fairy", "native", "python"], default="auto",
                    help="fairy: Fairy-Stockfish with this game's rules (strongest); native: the compiled "
                         "xqchess engine; python: the pure-Python engine; auto (default): Fairy-Stockfish "
                         "if it has been built, else native, else Python")
    ap.add_argument("--threads", type=int, default=os.cpu_count() or 1, help="CPU cores to use (default: all)")
    add_rule_args(ap)
    a = ap.parse_args()

    fen = a.fen or SETUPS[a.setup]
    movetime = int(a.time * 1000) if a.time else (None if (a.depth or a.nodes) else 5000)
    with Engine(rules=rules_from_args(a), kind=a.engine, threads=a.threads) as e:
        e.position(fen, a.moves)
        print(e.board())
        legal = e.legal()
        if not legal:
            print("\nNo legal moves: the game is over.")
            return
        cur = e.fen()
        white_to_move = cur.split()[1] == "w"
        pe.R.snipers = bool(rules_from_args(a).get("Snipers"))         # (decides which evaluation Fairy uses)
        own = e.kind == "fairy" and fairy_engine.own_evaluation(*fairy_engine.royals(cur))
        print(f"\n{'White' if white_to_move else 'Black'} to move, {len(legal)} legal moves.  "
              f"Engine: {ENGINE_LABELS[e.kind]}.\n")

        def progress(info):
            if not a.quiet and info["multipv"] == 1:
                print(f"  depth {info['depth']:2d}  best {info['pv'][0]:6s} {fmt_score(info, white_to_move, own)}"
                      f"  nodes {info['nodes']:,}", file=sys.stderr)

        lines = e.analyse(depth=a.depth, nodes=a.nodes, movetime=movetime,
                          multipv=min(a.multipv, len(legal)), on_info=progress)
        if not lines:
            print("search returned nothing")
            return
        print(f"\nBest moves (depth {lines[0]['depth']}; score in pawns for the side to move, then from White's "
              f"point of view with White's expected score):")
        for i, ln in enumerate(lines, 1):
            print(f"{i:2d}. {ln['pv'][0]:6s} {fmt_score(ln, white_to_move, own):32s} {' '.join(ln['pv'][:12])}")


if __name__ == "__main__":
    main()
