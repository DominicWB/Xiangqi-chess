#!/usr/bin/env python3
"""Build the table of solved endings used by the evaluation (EG_TABLE).

Every ending of a king against a general with at most two other pieces is solved exactly by the
engine's endgame solver (command "tb", e.g. "tb GA k").  For each material signature this script
takes the solver's results for the quiet positions (no capture, promotion or check to make, which
is what an evaluation sees), averages the general's side's score over both sides to move, and turns
it into centipawns on the evaluation's own scale:  cp = EVAL_K * ln(E / (1 - E)),  capped at
+-TB_WIN.  With one pawn or soldier the average is taken for each rank it stands on.

    python3 tools/endgame_table.py                     # solve everything (about an hour) and write
                                                       #   tools/endgame_table.json
    python3 tools/endgame_table.py --log run1.txt ...  # use saved "tb" output instead of solving again
    python3 tools/endgame_table.py --apply             # write the table into pyengine.py and
                                                       #   engine/xqchess.cpp (then rebuild)
The general's side is White and the king's side Black in every "tb" command; the table is keyed by
side, not colour, so it serves either colouring.
"""
import argparse
import json
import math
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, ROOT)
import pyengine as pe                                                    # noqa: E402

GEN_PIECES = "AEHCJQRBN"        # what the general's side can have (soldiers promote to any piece)
KING_PIECES = "qrbn"            # what the king's side can have (pawns promote to Western pieces)


def signatures():
    """(white pieces, black pieces) of every ending to solve: general's side first."""
    sigs = [("G" + x, "k") for x in GEN_PIECES]
    sigs += [("G" + x + y, "k") for i, x in enumerate(GEN_PIECES) for y in GEN_PIECES[i:]]
    sigs += [("G" + x, "k" + y) for x in GEN_PIECES for y in KING_PIECES]
    sigs += [("GS", "k")] + [("GS" + x, "k") for x in GEN_PIECES] + [("GS", "k" + y) for y in KING_PIECES]
    sigs += [("G" + x, "kp") for x in GEN_PIECES]
    return sigs


LINE_ALL = re.compile(r"tb (\w+) vs (\w+), (White|Black) to move, quiet positions: White wins ([\d.]+)%, "
                      r"Black wins ([\d.]+)%, draws ([\d.]+)%")
LINE_RANK = re.compile(r"tb (\w+) vs (\w+), (pawn|soldier) on rank (\d), quiet positions: White to move: "
                       r"White wins ([\d.]+)%, Black wins ([\d.]+)%, draws ([\d.]+)%; Black to move: "
                       r"White wins ([\d.]+)%, Black wins ([\d.]+)%, draws ([\d.]+)%")
LINE_HEAD = re.compile(r"tb (\w+) vs (\w+), (White|Black) to move: White wins ([\d.]+)%, Black wins ([\d.]+)%, "
                       r"draws ([\d.]+)%, longest forced mate (\d+) moves")


def parse(lines, found):
    for line in lines:
        m = LINE_ALL.match(line)
        if m:
            w, b, side = m.group(1), m.group(2), m.group(3)
            found.setdefault((w, b), {})[f"quiet_{side[0].lower()}"] = [float(m.group(i)) for i in (4, 5, 6)]
            continue
        m = LINE_HEAD.match(line)
        if m:
            w, b, side = m.group(1), m.group(2), m.group(3)
            d = found.setdefault((w, b), {})
            d[f"all_{side[0].lower()}"] = [float(m.group(i)) for i in (4, 5, 6)]
            d["longest"] = max(d.get("longest", 0), int(m.group(7)))
            continue
        m = LINE_RANK.match(line)
        if m:
            w, b, rank = m.group(1), m.group(2), int(m.group(4))
            found.setdefault((w, b), {}).setdefault("ranks", {})[rank] = [
                [float(m.group(i)) for i in (5, 6, 7)], [float(m.group(i)) for i in (8, 9, 10)]]


def solve(engine, sigs, found):
    todo = [s for s in sigs if s not in found or "quiet_b" not in found[s]]
    if not todo:
        return
    print(f"solving {len(todo)} endings with {engine} (four-piece ones take up to a few minutes each)")
    p = subprocess.Popen([engine], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
    for w, b in todo:
        p.stdin.write(f"tb {w} {b}\n")
        p.stdin.flush()
        lines = []
        while True:
            line = p.stdout.readline()
            if not line:
                raise SystemExit("the engine stopped")
            line = line.strip()
            if line.startswith("error"):
                raise SystemExit(line)
            if line.startswith("tb "):
                lines.append(line)
                print("  " + line, flush=True)
            if line.startswith(f"tb {w} vs {b}, Black to move, quiet positions"):
                break
        # rank lines (if any) follow the Black-to-move line; read them with a marker command
        p.stdin.write("isready\n")
        p.stdin.flush()
        while True:
            line = p.stdout.readline().strip()
            if line == "readyok":
                break
            if line.startswith("tb "):
                lines.append(line)
                print("  " + line, flush=True)
        parse(lines, found)
    p.stdin.write("quit\n")
    p.stdin.flush()


def to_cp(e, k, cap):
    if e <= 0:
        return -cap
    if e >= 1:
        return cap
    return max(-cap, min(cap, int(round(k * math.log(e / (1 - e))))))


def score(wdl, general_white=True):
    """Expected score of the general's side from [White wins, Black wins, draws] percentages."""
    w, b, d = wdl
    return (w if general_white else b) / 100 + 0.5 * d / 100


def build(found, k, cap):
    entries = []
    for (w, b), d in sorted(found.items()):
        if "quiet_w" not in d or "quiet_b" not in d:
            continue
        gen, kng = [pe.PCHARS.index(ch) for ch in w[1:]], [pe.PCHARS.index(ch.upper()) for ch in b[1:]]
        base = 256 * pe.eg_code(gen) + pe.eg_code(kng)
        if "ranks" in d:
            pawn_white = "S" in w or "P" in w
            for rank, (wm, bm) in sorted(d["ranks"].items()):
                rr = rank - 1 if pawn_white else 8 - rank
                e = (score(wm) + score(bm)) / 2
                entries.append({"white": w, "black": b, "rank": rank, "key": 65536 * rr + base, "score": round(e, 4),
                                "cp": to_cp(e, k, cap)})
        else:
            e = (score(d["quiet_w"]) + score(d["quiet_b"])) / 2
            entries.append({"white": w, "black": b, "key": base, "score": round(e, 4), "cp": to_cp(e, k, cap)})
    return sorted(entries, key=lambda x: x["key"])


def apply_table(entries):
    def label(x):
        s = f"G{x['white'][1:]} vs K{x['black'][1:].upper()}"
        return s + (f", {'soldier' if 'S' in x['white'] else 'pawn'} on rank {x['rank']}" if "rank" in x else "")
    py = ["EG_TABLE = {"]
    for x in entries:
        py.append(f"    {x['key']}: {x['cp']},  # {label(x)}: {100 * x['score']:.1f}%")
    py.append("}")
    cpp = ["const EgEntry EG_TABLE[] = {"]
    for x in entries:
        cpp.append(f"    {{{x['key']}, {x['cp']}}},  // {label(x)}: {100 * x['score']:.1f}%")
    cpp.append("};")
    for path, lines, marker in ((os.path.join(ROOT, "pyengine.py"), py, "#"),
                                (os.path.join(ROOT, "engine", "xqchess.cpp"), cpp, "//")):
        src = open(path).read()
        a = src.index(f"{marker} BEGIN EG_TABLE\n") + len(f"{marker} BEGIN EG_TABLE\n")
        z = src.index(f"{marker} END EG_TABLE")
        open(path, "w").write(src[:a] + "\n".join(lines) + "\n" + src[z:])
        print(f"wrote {len(entries)} entries to {os.path.relpath(path, ROOT)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engine", default=os.path.join(ROOT, "engine", "xqchess.exe" if os.name == "nt" else "xqchess"))
    ap.add_argument("--log", nargs="*", default=[], help="saved output of tb commands to use instead of solving")
    ap.add_argument("--out", default=os.path.join(HERE, "endgame_table.json"))
    ap.add_argument("--no-solve", action="store_true", help="only use --log and the existing --out file")
    ap.add_argument("--apply", action="store_true", help="write the table in --out into pyengine.py and engine/xqchess.cpp")
    ap.add_argument("--list", action="store_true", help="print the solver's results stored in --out")
    a = ap.parse_args()
    if a.list:
        with open(a.out) as fh:
            raw = json.load(fh)["raw"]
        print("Results over all positions, general's side (upper case) to move | king's side to move:")
        print("percentages of positions won by the general's side / won by the king's side / drawn\n")
        order = lambda x: (len(x["white"]) + len(x["black"]), "S" in x["white"] + x["black"].upper(),
                           "P" in x["black"].upper(), x["white"], x["black"])
        for x in sorted(raw, key=order):
            if "all_w" not in x or "all_b" not in x or x["white"][0] != "G" or x["black"][0] != "k":
                continue
            (gw, kw, dw), (gb, kb, db) = x["all_w"], x["all_b"]
            print(f"  {x['white']:<4} vs {x['black'].upper():<4} {gw:5.1f} / {kw:5.1f} / {dw:5.1f}   |"
                  f"  {gb:5.1f} / {kb:5.1f} / {db:5.1f}    longest mate {x.get('longest', 0)} moves")
        return
    if a.apply:
        with open(a.out) as fh:
            apply_table(json.load(fh)["entries"])
        return
    found = {}
    if os.path.exists(a.out):
        with open(a.out) as fh:
            for x in json.load(fh).get("raw", []):
                found[(x["white"], x["black"])] = {k: (({int(r): v for r, v in x[k].items()}) if k == "ranks" else x[k])
                                                   for k in x if k not in ("white", "black")}
    for fn in a.log:
        with open(fn) as fh:
            parse([l.strip() for l in fh], found)
    sigs = signatures()
    if not a.no_solve:
        solve(a.engine, sigs, found)
    entries = build({s: found[s] for s in sigs if s in found}, pe.EVAL_K, pe.EW["TB_WIN"])
    raw = [dict(white=w, black=b, **d) for (w, b), d in sorted(found.items())]
    with open(a.out, "w") as fh:
        json.dump({"eval_k": pe.EVAL_K, "tb_win": pe.EW["TB_WIN"], "entries": entries, "raw": raw}, fh, indent=1)
    missing = [f"{w} {b}" for w, b in sigs if (w, b) not in found]
    print(f"{len(entries)} table entries written to {os.path.relpath(a.out)}" + (f"; missing: {', '.join(missing)}" if missing else ""))


if __name__ == "__main__":
    main()
