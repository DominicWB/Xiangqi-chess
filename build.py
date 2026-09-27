#!/usr/bin/env python3
"""build.py -- compile the engines from source (no compiled programs are distributed).

    python build.py              both engines
    python build.py native       only the xqchess engine:  engine/xqchess     (a few seconds)
    python build.py fairy        only Fairy-Stockfish:     engine/fairy-xqchess (a minute or two)
    python build.py --check      only check the engines that are there, without building
    python build.py --force      rebuild even engines that are up to date

(On Windows the programs get the extension .exe.)  In a Jupyter notebook: %run build.py

The compiler used is the one named by the CXX environment variable, or else g++, clang++ or
Microsoft's cl if one is on the PATH.  On Windows it also finds the free Build Tools for Visual
Studio (with "Desktop development with C++") by itself, so a plain Command Prompt is enough.
After building, each engine is checked: move counts (perft) in three positions, and its
evaluation of test positions against pyengine.py.

The analysis board, the web page and the other tools also build a missing or out-of-date
engine by themselves when a compiler is available; this script does it up front and reports.
"""
import argparse
import os
import sys
import time

import xqchess
from xqchess import ENGINE_PATH, FAIRY_PATH, SETUPS

CHESS = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
# (what, rule options, position, depth, number of move sequences)
PERFT_CHECKS = [
    ("chess from the start", {}, CHESS, 5, 4865609),
    ("snipers chess from the start", {"Snipers": True}, CHESS, 5, 4898029),
    ("the Xiangqi army against the chess army", {}, SETUPS["default"], 4, 323652),
]


def rel(path):
    return os.path.relpath(path, xqchess.HERE)


def check(kind, name):
    """Run the checks on one engine (the program as it is: nothing is rebuilt); True if all pass."""
    ok = True
    cmd = [ENGINE_PATH] if kind == "native" else [sys.executable, "-u", os.path.join(xqchess.HERE, "fairy_engine.py"),
                                                    FAIRY_PATH]
    for what, rules, fen, depth, want in PERFT_CHECKS:
        try:
            with xqchess.Engine(rules=rules, cmd=cmd) as e:
                e.position(fen)
                got = e.perft(depth)
        except Exception as ex:                     # an engine that does not start or answer
            got = f"error: {ex}"
        good = got == want
        ok &= good
        print(f"  {'ok  ' if good else 'FAIL'} perft {depth}, {what}: {got}" + ("" if good else f" (expected {want})"))
    if kind == "native":
        good = xqchess._matches_python(ENGINE_PATH)
    else:
        import fairy_engine
        good = fairy_engine.binary_works(FAIRY_PATH)
    ok &= good
    print(f"  {'ok  ' if good else 'FAIL'} its evaluation agrees with pyengine.py")
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("which", nargs="?", choices=["all", "native", "fairy"], default="all",
                    help="which engine to build (default: both)")
    ap.add_argument("--check", action="store_true", help="only check the engines that are there")
    ap.add_argument("--force", action="store_true", help="rebuild engines that are already up to date")
    a = ap.parse_args()
    targets = [("native", "The xqchess engine", ENGINE_PATH), ("fairy", "Fairy-Stockfish", FAIRY_PATH)]
    targets = [t for t in targets if a.which in ("all", t[0])]

    if not a.check:
        print("Looking for a C++ compiler...", flush=True)
        comp = xqchess.find_compiler()
        if comp is None:
            print("No C++ compiler was found.\n")
            print("  Windows: install the free Build Tools for Visual Studio and choose the workload\n"
                  "           \"Desktop development with C++\" (or install g++ from MSYS2 or WinLibs).\n"
                  "  macOS:   run  xcode-select --install\n"
                  "  Linux:   install g++ (e.g. sudo apt install g++)\n")
            print("Then run  python build.py  again.  Until then everything works with the Python engine.")
            sys.exit(1)
        print(f"Using {comp[1]}\n", flush=True)

    ok = True
    for kind, name, path in targets:
        print(f"{name} ({rel(path)})", flush=True)
        if a.check:
            if not os.path.exists(path):
                print("  not built yet\n")
                ok = False
                continue
        else:
            if kind == "fairy":
                print("  building: this takes a minute or two...", flush=True)
            before = os.path.getmtime(path) if os.path.exists(path) else None
            t0 = time.time()
            try:
                if kind == "native":
                    xqchess.build_engine(force=a.force)
                else:
                    xqchess.build_fairy(force=a.force, quiet=True)
            except Exception as ex:
                print(f"  FAILED: {ex}\n")
                ok = False
                continue
            if before is not None and os.path.getmtime(path) == before:
                print("  already up to date (--force rebuilds it)")
            else:
                print(f"  built in {time.time() - t0:.0f} s")
        ok &= check(kind, name)
        print()
    if ok:
        print("All done.  The analysis board (python gui.py) and the web page (python webplay.py) use the "
              "engines automatically.")
    else:
        print("Something failed: see above.  The Python engine works without anything built.")
    return 0 if ok else 1


if __name__ == "__main__":
    code = main()
    if code:
        sys.exit(code)
