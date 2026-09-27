#!/usr/bin/env python3
"""Fairy-Stockfish behind the xqchess engine protocol.

Fairy-Stockfish is a Stockfish derivative that plays many chess variants.  The copy in
fairy/src has three additions (see fairy/README.md): each side's royal piece can move
differently (a general steps like a wazir, a king like a king), the evaluation fitted to this
game's self-play results (the same one as engine/xqchess.cpp and pyengine.py), and snipers,
pieces that can also take without moving (snipers chess: White's bishops).  For snipers chess,
with Western pieces only, Fairy-Stockfish evaluates with its own chess evaluation instead.

This script is what the GUI and the tools start when --engine fairy is chosen.  It reads the
same line commands as the other two engines (see "Engine protocol" in README.md), translates
them to UCI for Fairy-Stockfish, and translates the answers back.  The rules of each position
are written into a small variant file for Fairy-Stockfish; the evaluation weights come from
pyengine.py, so re-tuned weights are picked up without rebuilding.

pyengine.py keeps track of the game position, so that "legal", "fen", "d", "eval" and the
game-over checks give exactly the same answers as with the other engines.
"""
import os
import queue
import random
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pyengine as pe                                               # noqa: E402

FAIRY_PATH = os.path.join(HERE, "engine", "fairy-xqchess.exe" if os.name == "nt" else "fairy-xqchess")

# Our piece letters for the non-royal pieces, and the matching pyengine types
LETTERS = "pnbrqsheajc"
TYPES = [pe.PAWN, pe.KNIGHT, pe.BISHOP, pe.ROOK, pe.QUEEN, pe.SOLDIER, pe.HORSE, pe.ELEPHANT, pe.ADVISOR,
         pe.CHARIOT, pe.CANNON]
PROMO_SETS = {3: "qjrchnbea", 1: "jchea", 2: "qrbn", 0: ""}
# snipers chess (both armies Western) uses Fairy-Stockfish's own evaluation, where a sniper is worth
# this much more than a bishop (middlegame, endgame; internal units, 208 = one pawn)
SNIPER_BONUS = (400, 400)
START_FENS = {                                  # a valid start position for each kind of position
    ("g", "k"): "rnbqkbnr/pppppppp/8/8/8/S2SS2S/1C4C1/JHEAGEHJ w kq - 0 1",
    ("k", "g"): "jheagehj/1c4c1/s1s2s1s/8/8/8/PPPPPPPP/RNBQKBNR w KQ - 0 1",
    ("k", "k"): "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
    ("g", "g"): "jheagehj/1c4c1/s1s2s1s/8/8/S1S2S1S/1C4C1/JHEAGEHJ w - - 0 1",
    # Western pieces only (chess, e.g. snipers chess): a leaner variant, about 40% faster to search
    ("K", "K"): "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
}
WESTERN = set("PNBRQKpnbrqk")


def to_fairy_fen(fen):
    """Fairy-Stockfish has one royal piece type (letter k); a general is a k that moves like a wazir."""
    board, _, rest = fen.partition(" ")
    return board.replace("G", "K").replace("g", "k") + (" " + rest if rest else "")


def royals(fen):
    """The kind of position, which picks the variant definition: each side's royal (g general, k king),
    or ("K", "K") when there are only Western pieces (only they can ever appear then)."""
    board = fen.split()[0]
    if all(ch in WESTERN or ch.isdigit() or ch == "/" for ch in board):
        return ("K", "K")
    return ("g" if "G" in board else "k"), ("g" if "g" in board else "k")


def own_evaluation(white_royal, black_royal):
    """Fairy-Stockfish's own (classical) evaluation is used for snipers chess, where both armies are
    Western; everywhere else the evaluation fitted to this game (xqEval)."""
    return pe.R.snipers and white_royal in "kK" and black_royal in "kK"


def variant_section(name, white_royal, black_royal):
    """Fairy-Stockfish variant definition for the current rules (pyengine.R) and weights."""
    R = pe.R
    promo = PROMO_SETS[R.soldier_promo]
    if (white_royal, black_royal) == ("K", "K"):          # chess pieces, castling, e.p. and promotion
        return "\n".join([f"[{name}:chess]", "stalemateValue = " + ("loss" if R.stalemate_loss else "draw")]
                         + _rules_and_evaluation(white_royal, black_royal)) + "\n"
    lines = [f"[{name}]",
             "pawn = p", "knight = n", "bishop = b", "rook = r", "queen = q", "cannon = c",
             "customPiece1 = j:R",                                      # chariot: moves like a rook
             "fers = a",                                                # advisor
             "elephant = e" if R.elephant_eye else "alfil = e",         # nA (eye can be blocked) or A
             "horse = h" if R.horse_block else "customPiece2 = h:N",    # nN (leg can be blocked) or N
             "soldier = s" if R.soldier_sideways else "shogiPawn = s",  # fsW after crossing, or fW
             "king = k"]
    for colour, royal in (("White", white_royal), ("Black", black_royal)):
        if royal == "g":
            lines += [f"kingType{colour} = wazir",
                      f"promotionPawnTypes{colour} = {'s' if promo else '-'}",
                      f"promotionPieceTypes{colour} = {promo or '-'}"]
        else:
            lines += [f"promotionPawnTypes{colour} = p", f"promotionPieceTypes{colour} = qrbn"]
    if R.soldier_sideways:
        lines.append("soldierPromotionRank = 5")
    lines += ["nMoveRuleTypes = ps",                    # pawn moves and forward soldier steps reset it
              "stalemateValue = " + ("loss" if R.stalemate_loss else "draw"),
              "startFen = " + to_fairy_fen(START_FENS[(white_royal, black_royal)])]
    return "\n".join(lines + _rules_and_evaluation(white_royal, black_royal)) + "\n"


def _rules_and_evaluation(white_royal, black_royal):
    """The snipers rule and the evaluation, the part common to every variant definition."""
    R = pe.R
    lines = []
    if R.snipers:                                       # White's bishops can also shoot (fairy/README.md)
        lines.append("sniperTypesWhite = b")
    if own_evaluation(white_royal, black_royal):
        # Fairy-Stockfish's evaluation knows chess far better than the fitted one; the sniper's
        # extra value is given in its internal units (100 centipawns = PawnValueEg = 208)
        return lines + [f"sniperBonusMg = {SNIPER_BONUS[0]}", f"sniperBonusEg = {SNIPER_BONUS[1]}"]
    # the evaluation fitted to this game (tools/tune_eval.py), as in pyengine.evaluate()
    per = lambda table: " ".join(f"{l}:{table[t]}" for l, t in zip(LETTERS, TYPES))
    w = pe.EW
    lines += ["xqEval = true",
              "xqValue = " + per(pe.VAL), "xqValueEg = " + per(pe.VAL_EG),
              "xqCentre = " + per(pe.CENW), "xqMobility = " + per(pe.MOBW),
              "xqPawnAdv = " + " ".join(map(str, pe.PAWN_ADV)),
              "xqSoldierAdv = " + " ".join(map(str, pe.SOLD_ADV_PROMO if R.soldier_promo else pe.SOLD_ADV)),
              "xqSoldierPromotion = " + ("true" if R.soldier_promo else "false"),
              f"xqSniperMg = {w['SNIPER_MG']}", f"xqSniperEg = {w['SNIPER_EG']}"]
    for key, name_ in (("SEVENTH", "Seventh"), ("KING_MG", "KingMg"), ("KING_EG", "KingEg"), ("GEN_MG", "GenMg"),
                       ("GEN_EG", "GenEg"), ("GEN_BASE_MG", "GenBaseMg"), ("GEN_BASE_EG", "GenBaseEg"),
                       ("OPEN_K", "OpenK"), ("OPEN_G", "OpenG"), ("BISHOP_PAIR", "BishopPair"),
                       ("PAWN_CENTRE", "PawnCentre"), ("TEMPO", "Tempo"), ("MOPUP_EDGE", "MopupEdge"),
                       ("MOPUP_CLOSE", "MopupClose"), ("SCALE_NOPAWN", "ScaleNoPawn"), ("SCALE_XQ", "ScaleXq"),
                       ("FIFTY_DIV", "FiftyDiv")):
        lines.append(f"xq{name_} = {w[key]}")
    # the solved endings (pyengine.EG_TABLE), as key:centipawns
    lines += [f"xqTbWin = {w['TB_WIN']}",
              "xqEndgame = " + " ".join(f"{k}:{v}" for k, v in sorted(pe.EG_TABLE.items()))]
    return lines


def score_to_int(kind, value):
    """UCI score -> centipawns for the side to move (mates as +/-(30000 - plies))."""
    if kind == "mate":
        return 30000 - 2 * value if value > 0 else -30000 - 2 * value
    return value


class Fairy:
    """A Fairy-Stockfish process set up for this game."""

    def __init__(self, path=FAIRY_PATH):
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.p = subprocess.Popen([path], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  text=True, bufsize=1, creationflags=flags)
        self.q = queue.Queue()
        threading.Thread(target=self._reader, daemon=True).start()
        self.send("uci")
        self.expect("uciok")
        self.dir = tempfile.mkdtemp(prefix="xqchess-fairy-")
        self.config = 0
        self.names = {}
        self.variant = None
        self.options = {}
        self.load_rules()

    def _reader(self):
        for line in self.p.stdout:
            self.q.put(line.rstrip("\n"))
        self.q.put(None)

    def send(self, s):
        self.p.stdin.write(s + "\n")
        self.p.stdin.flush()

    def get(self, timeout=None):
        line = self.q.get(timeout=timeout)
        if line is None:
            raise RuntimeError("Fairy-Stockfish exited")
        return line

    def expect(self, prefix, timeout=60):
        while True:
            line = self.get(timeout)
            if line.startswith(prefix):
                return line

    def sync(self):
        self.send("isready")
        self.expect("readyok")

    def load_rules(self):
        """Write variant definitions for the current rules and weights, and load them."""
        self.config += 1
        path = os.path.join(self.dir, f"rules{self.config}.ini")
        self.names = {pair: f"xqchess{self.config}{pair[0]}{pair[1]}" for pair in START_FENS}
        with open(path, "w") as fh:
            fh.write("\n".join(variant_section(name, *pair) for pair, name in self.names.items()))
        self.send(f"setoption name VariantPath value {path}")
        self.variant = None
        self.sync()

    def set_option(self, name, value):
        if self.options.get(name) != value:
            self.send(f"setoption name {name} value {value}")
            self.options[name] = value

    def position(self, start_fen, moves):
        name = self.names[royals(start_fen)]
        if name != self.variant:
            self.send(f"setoption name UCI_Variant value {name}")
            self.variant = name
        self.send(f"position fen {to_fairy_fen(start_fen)}" + (" moves " + " ".join(moves) if moves else ""))

    def quit(self):
        try:
            self.send("quit")
            self.p.wait(timeout=5)
        except Exception:
            self.p.kill()
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)


class Adapter:
    """Reads xqchess engine commands on stdin and answers on stdout."""

    def __init__(self, path=FAIRY_PATH):
        self.path = path
        self.fs = Fairy(path)
        self.out_lock = threading.Lock()
        self.start_fen, self.moves = pe.Pos().fen(), []
        self.P = pe.Pos(self.start_fen)
        self.worker = None
        self.stop_flag = threading.Event()

    def out(self, text):
        with self.out_lock:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()

    def wait(self):
        if self.worker is not None and self.worker.is_alive():
            self.worker.join()

    # ------------------------------------------------------------------ search
    def search(self, start_fen, moves, depth=None, nodes=None, movetime=None, multipv=1, forward=True):
        """Run one Fairy-Stockfish search.  With forward=True its output is passed on in this
        engine's format; returns (bestmove, {multipv: (score, pv)})."""
        fs = self.fs
        fs.set_option("MultiPV", multipv)
        fs.position(start_fen, moves)
        go = "go"
        if depth is not None:
            go += f" depth {depth}"
        if nodes:
            go += f" nodes {nodes}"
        if movetime:
            go += f" movetime {movetime}"
        fs.send(go)
        lines = {}
        while True:
            line = fs.get()
            if line.startswith("bestmove"):
                best = line.split()[1]
                return ("0000" if best == "(none)" else best), lines
            if not line.startswith("info depth") or " pv " not in line or "bound" in line:
                continue
            t = line.split()
            f = {t[i]: t[i + 1] for i in range(1, len(t) - 1) if t[i] in
                 ("depth", "multipv", "nodes", "time", "nps")}
            k = t.index("score")
            kind, val = t[k + 1], int(t[k + 2])
            pv = t[t.index("pv") + 1:]
            k_ = int(f.get("multipv", 1))
            lines[k_] = (score_to_int(kind, val), pv)
            if forward:
                self.out(f"info depth {f['depth']} multipv {k_} score {kind} {val} nodes {f.get('nodes', 0)} "
                         f"time {f.get('time', 0)} nps {f.get('nps', 0)} pv {' '.join(pv)}")

    def go(self, kw):
        try:
            best, _ = self.search(self.start_fen, self.moves, forward=True, **kw)
        except Exception as e:                    # Fairy-Stockfish died: report it and start a new one
            self.out(f"info string Fairy-Stockfish error: {e}")
            best = "0000"
            self.restart()
        self.out(f"bestmove {best}")

    def restart(self):
        try:
            self.fs.p.kill()
        except Exception:
            pass
        options = dict(self.fs.options)
        options.pop("MultiPV", None)
        self.fs = Fairy(self.path)
        for name, value in options.items():
            self.fs.set_option(name, value)

    # --------------------------------------------------------------- self-play
    def selfplay(self, args):
        """Engine-vs-engine games with the same options and output as the xqchess engine."""
        opt = {"games": 1, "nodes": 20000, "depth": 64, "randomplies": 4, "seed": 1, "maxplies": 300,
               "resign": 0, "samplevery": 2, "randmargin": 100, "randdepth": 4}
        fen = pe.Pos().fen()
        i = 0
        while i < len(args):
            if args[i] == "fen":
                fen = " ".join(args[i + 1:])
                break
            if args[i] in opt and i + 1 < len(args):
                try:
                    opt[args[i]] = int(args[i + 1])
                except ValueError:
                    pass
                i += 2
            else:
                i += 1
        try:
            pe.Pos(fen)
        except ValueError:
            self.out('{"error":"bad fen"}')
            return
        rng = random.Random(opt["seed"])
        depth = max(1, min(opt["depth"], 200))
        for g in range(max(0, opt["games"])):
            if self.stop_flag.is_set():
                return
            self.fs.send("ucinewgame")
            P, moves, samples = pe.Pos(fen), [], []
            bad = {pe.WHITE: 0, pe.BLACK: 0}
            ply, result, reason = 0, "", ""
            while True:
                if self.stop_flag.is_set():
                    return
                reason, result = pe.game_over(P)
                if reason:
                    break
                if ply >= max(1, opt["maxplies"]):
                    result, reason = "1/2-1/2", "maxplies"
                    break
                legal = P.legal_moves()
                if ply < opt["randomplies"]:
                    if opt["randmargin"] < 0:
                        mv = pe.move_str(rng.choice(legal))
                    else:
                        _, lines = self.search(fen, moves, depth=max(1, opt["randdepth"]),
                                               multipv=len(legal), forward=False)
                        if not lines:
                            mv = pe.move_str(rng.choice(legal))
                        else:
                            top = max(sc for sc, _ in lines.values())
                            ok = [pv[0] for sc, pv in lines.values() if pv and sc >= top - opt["randmargin"]]
                            mv = rng.choice(ok) if ok else pe.move_str(rng.choice(legal))
                else:
                    best, lines = self.search(fen, moves, depth=depth, nodes=max(0, opt["nodes"]) or None,
                                              forward=False)
                    if self.stop_flag.is_set():
                        return
                    mv = best if best != "0000" else pe.move_str(legal[0])
                    score = lines[1][0] if 1 in lines else 0
                    m = pe.parse_move(P, mv)
                    quiet = m is not None and not (m >> 24) & pe.F_CAP and not (m >> 16) & 255 \
                        and not P.in_check(P.side)
                    if quiet and abs(score) < 29000 and opt["samplevery"] > 0 and ply % opt["samplevery"] == 0:
                        cnt = [[0] * pe.NTYPES, [0] * pe.NTYPES]
                        for s in pe.SQ64:
                            p = P.b[s]
                            if p:
                                cnt[p >> 4][p & 15] += 1
                        wscore = score if P.side == pe.WHITE else -score
                        samples.append([ply, wscore] + cnt[0][1:] + cnt[1][1:])
                    if opt["resign"] > 0:
                        bad[P.side] = bad[P.side] + 1 if score <= -opt["resign"] else 0
                        if bad[P.side] >= 3:
                            result, reason = ("0-1" if P.side == pe.WHITE else "1-0"), "adjudicated"
                            break
                m = pe.parse_move(P, mv)
                if m is None:                          # cannot happen: both follow the same rules
                    result, reason = ("0-1" if P.side == pe.WHITE else "1-0"), f"illegal move {mv}"
                    break
                P.make(m)
                moves.append(mv)
                ply += 1
            smp = ",".join("[" + ",".join(map(str, s)) + "]" for s in samples)
            self.out(f'{{"game":{g},"result":"{result}","reason":"{reason}","plies":{ply},"start":"{fen}",'
                     f'"final":"{P.fen()}","moves":"{" ".join(moves)}","samples":[{smp}]}}')

    # -------------------------------------------------------------- main loop
    def run(self):
        for line in sys.stdin:
            tok = line.split()
            if not tok:
                continue
            cmd = tok[0]
            if cmd == "stop":
                if self.worker is not None and self.worker.is_alive():
                    self.stop_flag.set()
                    self.fs.send("stop")
                continue
            if cmd == "isready":
                self.out("readyok")
                continue
            self.wait()                           # other commands wait for a running search
            if cmd == "quit":
                break
            try:
                self.command(cmd, tok)
            except Exception as e:                # never die on a bad command
                self.out(f"error {cmd}: {e}")
        self.wait()
        self.fs.quit()

    def command(self, cmd, tok):
        if cmd == "position":
            rest = tok[1:]
            if rest and rest[0] == "fen":
                mi = rest.index("moves") if "moves" in rest else len(rest)
                fen, mvs = " ".join(rest[1:mi]), rest[mi + 1:]
                try:
                    NP = pe.Pos(fen)
                except ValueError:
                    self.out("error bad fen")
                    return
            elif rest and rest[0] == "startpos":
                NP = pe.Pos()
                fen, mvs = NP.fen(), (rest[2:] if len(rest) > 1 and rest[1] == "moves" else [])
            else:
                self.out("error position needs startpos or fen")
                return
            for mv in mvs:
                m = pe.parse_move(NP, mv)
                if m is None:
                    self.out(f"error illegal move {mv}")
                    return
                NP.make(m)
            self.start_fen, self.moves, self.P = fen, list(mvs), NP
        elif cmd == "go":
            kw = {}
            for i in range(1, len(tok) - 1):
                if tok[i] in ("depth", "nodes", "movetime", "multipv"):
                    try:
                        kw[tok[i]] = int(tok[i + 1])
                    except ValueError:
                        pass
            if not any(k in kw for k in ("depth", "nodes", "movetime")):
                kw["movetime"] = 2000
            kw["multipv"] = max(1, min(kw.get("multipv", 1), 256))
            if "depth" in kw:
                kw["depth"] = max(1, min(kw["depth"], 200))
            kw["nodes"] = max(0, kw.get("nodes", 0)) or None
            kw["movetime"] = max(0, kw.get("movetime", 0)) or None
            reason, result = pe.game_over(self.P)
            if reason:
                self.out(f"info gameover {result} {reason}")
                self.out("bestmove 0000")
                return
            self.stop_flag.clear()
            self.worker = threading.Thread(target=self.go, args=(kw,), daemon=True)
            self.worker.start()
        elif cmd == "legal":                      # Fairy-Stockfish's own move generator
            self.fs.position(self.start_fen, self.moves)
            self.fs.send("go perft 1")
            moves = []
            while True:
                line = self.fs.get()
                if line.startswith("Nodes searched"):
                    break
                if ":" in line and not line.startswith("info"):
                    moves.append(line.split(":")[0].strip())
            self.out("legal " + " ".join(sorted(moves)))
        elif cmd in ("perft", "divide"):
            d = max(0, min(int(tok[1]) if len(tok) > 1 and tok[1].isdigit() else 1, 12))
            t0 = time.perf_counter()
            self.fs.position(self.start_fen, self.moves)
            self.fs.send(f"go perft {d}")
            total = 0
            while True:
                line = self.fs.get()
                if line.startswith("Nodes searched"):
                    total = int(line.split(":")[1])
                    break
                if cmd == "divide" and ":" in line and not line.startswith("info"):
                    mv, n = line.split(":")
                    self.out(f"{mv.strip()} {n.strip()}")
            self.out(f"perft {d} {total} ({time.perf_counter() - t0:.2f}s)")
        elif cmd == "eval":
            self.out(f"eval {pe.evaluate(self.P)}")
        elif cmd == "d":
            self.out(self.P.board_text())
        elif cmd == "fen":
            self.out(f"fen {self.P.fen()}")
        elif cmd == "setoption":
            self.setoption(tok[1:])
        elif cmd == "selfplay":
            self.stop_flag.clear()

            def work(args):
                try:
                    self.selfplay(args)
                except Exception as e:            # Fairy-Stockfish died: report it and start a new one
                    self.out(f'{{"error":"Fairy-Stockfish: {str(e).replace(chr(34), chr(39))}"}}')
                    self.restart()

            self.worker = threading.Thread(target=work, args=(tok[1:],), daemon=True)
            self.worker.start()
        elif cmd in ("tb", "tb4"):
            self.out(f"error {cmd} is only available in the xqchess engine (engine/xqchess)")
        else:
            self.out(f"error unknown command {cmd}")

    def setoption(self, args):
        name, v = (args + ["", ""])[:2]
        R = pe.R
        rules = True
        if name == "HorseBlock":
            R.horse_block = pe.parse_bool(v)
        elif name == "ElephantEye":
            R.elephant_eye = pe.parse_bool(v)
        elif name == "StalemateLoss":
            R.stalemate_loss = pe.parse_bool(v)
        elif name == "SoldierSideways":
            R.soldier_sideways = pe.parse_bool(v)
        elif name == "Snipers":
            R.snipers = pe.parse_bool(v)
        elif name == "SoldierPromotion":
            modes = {"none": 0, "false": 0, "xiangqi": 1, "western": 2, "any": 3, "true": 3}
            if v not in modes:
                self.out("error SoldierPromotion must be any, xiangqi, western or none")
                return
            R.soldier_promo = modes[v]
        elif name == "Value":
            t = pe.PCHARS.find(v.upper()) if len(v) == 1 else -1
            if t <= 0:
                self.out(f"error unknown piece {v}")
                return
            if len(args) < 3 or not args[2].isdigit() or int(args[2]) > 20000:
                self.out(f"error bad value for {v}")
                return
            pe.VAL[t] = int(args[2])
        elif name == "Threads":
            rules = False
            if v.isdigit():
                self.fs.set_option("Threads", max(1, min(256, int(v))))
        elif name == "Hash":
            rules = False
            if v.isdigit():
                self.fs.set_option("Hash", max(1, min(4096, int(v))))
        else:
            self.out(f"error unknown option {name}")
            return
        if rules:
            self.fs.load_rules()
            try:                                  # the current position may now be illegal
                P = pe.Pos(self.start_fen)
                for mv in self.moves:
                    P.make(pe.parse_move(P, mv))
                self.P = P
            except (ValueError, TypeError):
                self.start_fen, self.moves, self.P = pe.Pos().fen(), [], pe.Pos()


# positions whose evaluation must be the same in Fairy-Stockfish and pyengine.py
CHECK_FENS = [
    "rnb1kbnr/pppppppp/8/8/8/S2SS2S/1C4C1/JHEAGEHJ w kq - 0 1",
    "r1b1kb1r/pp1ppppp/8/2p5/8/S2SS2S/2C3C1/JHEAGEHJ w kq c6 0 2",
    "8/8/1S4Jp/4S3/8/8/4kr1G/8 w - - 10 45",
    "4k3/8/8/8/8/8/2H5/3JG3 b - - 10 1",
    "4k3/8/8/8/8/8/8/Q3G3 w - - 0 1",
    "8/8/8/4k3/4A3/4G3/8/8 b - - 3 58",          # solved ending: king against general + advisor
    "8/8/8/4k3/8/4G3/8/8 w - - 0 1",             # a bare general loses
    "8/8/8/4k3/8/4S3/8/4G3 w - - 0 1",           # solved ending with a soldier
    "8/8/8/4k3/4A3/4G3/8/J7 b - - 0 1",          # a drawn solved ending scores close to 0
]


# with the snipers rule: a shot must be among the legal moves, and the sniper's value in the evaluation
SNIPER_CHECK_FEN = "4k3/8/7p/8/1b6/8/3B4/4K3 w - - 0 1"         # a pinned sniper can still shoot h6
SNIPER_EVAL_FEN = "jheagehj/1c4c1/s1s2s1s/8/8/8/PPPPPPPP/RNBQKBNR w KQ - 0 1"


def binary_works(path=FAIRY_PATH, timeout=20):
    """True if the Fairy-Stockfish binary runs and evaluates exactly like pyengine.py
    (i.e. it was built from the current fairy/src)."""
    if not os.path.exists(path):
        return False
    saved = dict(pe.R.__dict__)                   # check under the default rules
    pe.R.__dict__.clear()
    fs = None
    try:
        fs = Fairy(path)
        for fen in CHECK_FENS:
            fs.position(fen, [])
            fs.send("eval")
            line = fs.expect("xqchess evaluation", timeout)
            if int(line.split()[2]) != pe.evaluate(pe.Pos(fen)):
                return False
        # the snipers rule (added later): an older build knows nothing of shots
        pe.R.snipers = True
        fs.load_rules()
        fs.position(SNIPER_EVAL_FEN, [])
        fs.send("eval")
        line = fs.expect("xqchess evaluation", timeout)
        if int(line.split()[2]) != pe.evaluate(pe.Pos(SNIPER_EVAL_FEN)):
            return False
        fs.position(SNIPER_CHECK_FEN, [])
        fs.send("go perft 1")
        moves = []
        while True:
            line = fs.get(timeout)
            if line.startswith("Nodes searched"):
                break
            moves.append(line.split(":")[0])
        return "d2h6s" in moves
    except Exception:
        return False
    finally:
        if fs is not None:
            fs.quit()
        pe.R.__dict__.clear()                     # back to the rules in use before the check
        pe.R.__dict__.update(saved)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else FAIRY_PATH
    if not os.path.exists(path):
        print(f"error Fairy-Stockfish not found at {path}; build it (see README)", flush=True)
        sys.exit(1)
    Adapter(path).run()


if __name__ == "__main__":
    main()
