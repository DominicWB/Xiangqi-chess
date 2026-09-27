# Fairy-Stockfish for the xqchess variant

This folder holds the source of [Fairy-Stockfish](https://github.com/fairy-stockfish/Fairy-Stockfish), the open-source chess-variant engine derived from Stockfish, with three additions for this project. It is built into `engine/fairy-xqchess` (`engine\fairy-xqchess.exe` on Windows) by `python build.py` (or `make fairy`), or automatically by the tools, and it is run through `../fairy_engine.py`.

* Upstream version: commit `9f778da` (2026-09-23) of the `master` branch.
* Changes: `xqchess.patch` (about 470 added lines), already applied to `src/`. The Python bindings (`pyffish.cpp`), the JavaScript bindings (`ffishjs.cpp`, `Makefile_js`) and the bundled `variants.ini` were left out because the engine does not need them.

## What the patch adds

1. **A different royal piece for each side.** Upstream Fairy-Stockfish has one royal piece type, and both sides' royals move the same way. The new variant options `kingTypeWhite` and `kingTypeBlack` name the piece a side's royal moves like, for example `kingTypeWhite = wazir` for a Xiangqi general facing a Western king. Changes are in `variant.h`, `parser.cpp`, `position.h` / `position.cpp`, `evaluate.cpp`, `variant.cpp`, `ucioption.cpp` and `apiutil.h`: every use of the royal's movement now asks for the colour's own movement.
2. **The xqchess evaluation.** With `xqEval = true` the engine evaluates positions with `Eval::xq_evaluate()` in `evaluate.cpp` instead of its built-in evaluation. This is a port of the evaluation in `../engine/xqchess.cpp` and `../pyengine.py`, whose weights were fitted to this game's self-play results. The weights are read from the variant definition (`xqValue`, `xqValueEg`, `xqCentre`, `xqMobility`, `xqPawnAdv`, `xqSoldierAdv` and the single values `xqSeventh` … `xqFiftyDiv`), so re-tuned weights need no rebuild. So is the table of solved endings: `xqEndgame` lists `key:centipawns` pairs (the keys are explained in `endgame_known()` in `../pyengine.py`) and `xqTbWin` is the score of a known win. The `eval` command prints `xqchess evaluation <centipawns for the side to move>`.

3. **Snipers.** Pieces of the types listed in the variant option `sniperTypes` (both colours) or `sniperTypesWhite` / `sniperTypesBlack` can also take the first enemy piece along one of their capture lines *without moving*: a shot. For snipers chess the definition is simply

       [snipers:chess]
       sniperTypesWhite = b

   A shot is a new move type, `SNIPE` in `types.h`, written like a capture with an `s` after the squares: `c1h6s` (the bishop on c1 takes the piece on h6 and stays on c1), while `c1h6` is still the ordinary capture. The changes:
   * `movegen.cpp`: a sniper's shots are generated with its captures (they count as captures, also when escaping check);
   * `position.cpp`: `do_move()` / `undo_move()` remove and restore only the target (the sniper stays, so the hash keys, castling rights and the NNUE update only see the capture); `legal()` allows a shot unless the target was shielding the own king (a pinned sniper may shoot); `gives_check()` looks for the lines the vanished target opens; `see_ge()` values a shot at the target's value and ends an exchange as soon as a sniper can shoot the piece on the square (nothing is left there to take back);
   * `uci.cpp` and `apiutil.h`: the `s` suffix in coordinate notation, `*` instead of `x` in SAN;
   * `psqt.cpp`: `sniperBonusMg` / `sniperBonusEg` add a sniper's extra value to the classical evaluation (internal units; 208 is a pawn), and `evaluate.cpp` adds `xqSniperMg` / `xqSniperEg` in the xqchess evaluation;
   * `variant.h`, `parser.cpp`, `position.h`: the options and `Position::sniper_types()` / `sniper_pieces()`.

`../fairy_engine.py` writes the variant definitions for the current rule options and weights. Both evaluations agree exactly: `python build.py --check` compares them on test positions, and the full comparison covered about 850 positions under each of nine rule settings. For snipers chess (Western pieces only) it uses Fairy-Stockfish's own evaluation, which knows chess far better than the fitted one, with the sniper's extra value measured from self-play (see the README one level up).

## Licence

Fairy-Stockfish is free software under the GNU General Public License, version 3 or later: see `Copying.txt`, and `AUTHORS` for its authors. The patched source in `src/` and `xqchess.patch` are distributed under the same licence. The rest of the xqchess package is a separate program that runs this engine as a subprocess and talks to it over standard input and output.

`README-Fairy-Stockfish.md` is the upstream README.
