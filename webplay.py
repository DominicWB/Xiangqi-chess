#!/usr/bin/env python3
"""webplay.py -- play White against the engines in your web browser.

    python webplay.py                  # starts a small web server on this computer and opens the page
    python webplay.py --port 9000      # another port (the default is 8765)
    python webplay.py --no-browser     # just start the server
    python webplay.py --stop           # stop a server that is running
    python webplay.py --host 0.0.0.0   # let other computers on your network play too (see README.md)

The page offers challenges (snipers chess, and the Xiangqi-against-chess setups), eight strength
levels and the engines that have been built; you always play White.  From a Jupyter notebook,
%run webplay.py starts the server as a program of its own and opens the page; the cell finishes at
once.  Only the Python standard library is needed.  Each game runs its own engine, the same ones
the other tools use: Fairy-Stockfish if it has been built, otherwise the xqchess or Python engine.

API (JSON over POST, used by web/app.js): /api/new, /api/state, /api/move, /api/reply, /api/undo,
/api/resign, /api/level, /api/hint, /api/pgn, /api/shutdown; GET /api/info lists the challenges,
levels and engines.
"""
import argparse
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PORT = 8765


def launch(argv=(), wait=15.0):
    """Start the web server as a program of its own (e.g. from a Jupyter notebook) and return its
    process, once it is serving (or has stopped: then its messages are printed)."""
    log = os.path.join(tempfile.gettempdir(), "xqchess-webplay.log")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    env = dict(os.environ, XQCHESS_WEBPLAY_LAUNCHED="1")
    with open(log, "w") as fh:
        p = subprocess.Popen([sys.executable, os.path.join(HERE, "webplay.py")] + list(argv), env=env,
                             stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT, creationflags=flags)
    end = time.time() + wait
    text = ""
    while time.time() < end and p.poll() is None and "Play the engines at" not in text:
        time.sleep(0.1)
        with open(log) as fh:
            text = fh.read()
    time.sleep(0.2)
    with open(log) as fh:
        text = fh.read().strip()
    if p.poll() is None:
        print(text or "The web server is starting.")
        print(f"It runs on its own: stop it with  %run webplay.py --stop  (its messages go to {log}).")
    else:
        print(text)
        if p.returncode:
            print(f"The web server did not start (exit code {p.returncode}); its messages are above.")
    return p


if __name__ == "__main__" and "ipykernel" in sys.modules and "--in-process" not in sys.argv \
        and "--stop" not in sys.argv:
    # %run webplay.py in a Jupyter notebook: serving from the kernel would keep the cell busy
    launch(sys.argv[1:])
    sys.exit(0)                         # (%run ignores a zero exit status)

import contextlib                                                   # noqa: E402
import datetime                                                     # noqa: E402
import json                                                         # noqa: E402
import math                                                         # noqa: E402
import random                                                       # noqa: E402
import re                                                           # noqa: E402
import secrets                                                      # noqa: E402
import threading                                                    # noqa: E402
import urllib.parse                                                 # noqa: E402
import urllib.request                                               # noqa: E402
import webbrowser                                                   # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

import fairy_engine                                                 # noqa: E402
import gamefile                                                     # noqa: E402
import pyengine as pe                                               # noqa: E402
from xqchess import ENGINE_LABELS, ENGINE_PATH, SETUPS, Engine, fairy_available, find_compiler  # noqa: E402

WEB_DIR = os.path.join(HERE, "web")
PIECE_DIR = os.path.join(HERE, "pieces")
MAX_GAMES = 12                          # games (and engine processes) kept at once; the oldest go first
IDLE_HOURS = 6                          # a game untouched this long is closed
HINT_MS = 1500                          # the engine's thinking time for a hint
MAX_BODY = 64 * 1024                    # largest request accepted, in bytes

# The challenges: you play White.  "note" is how engine-against-engine games went (README.md).
CHALLENGES = [
    {"id": "snipers", "title": "Chess, but my bishops are snipers",
     "text": "Ordinary chess, except that your bishops can also take the first enemy piece along a "
             "diagonal without moving: they shoot it from where they stand.",
     "note": "Engine against engine, White won all 200 games.",
     "fen": SETUPS["snipers"], "rules": {"Snipers": True}, "icon": "wSniper"},
    {"id": "snipers-norook", "title": "Snipers, without your a1 rook",
     "text": "Sniper bishops again, but you start without the rook on a1.",
     "note": "Engine against engine, White scored 78%.",
     "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/1NBQKBNR w Kkq - 0 1", "rules": {"Snipers": True},
     "icon": "wSniper"},
    {"id": "snipers-norook-noknight", "title": "Snipers, without your a1 rook and b1 knight",
     "text": "Sniper bishops, but you give the engine the odds of a rook and a knight.",
     "note": "Engine against engine, White scored 26%.",
     "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/2BQKBNR w Kkq - 0 1", "rules": {"Snipers": True},
     "icon": "wSniper"},
    {"id": "xq-noqueen-noknights", "title": "Xiangqi army against chess without queen and knights",
     "text": "You lead the Xiangqi army (general, advisor, elephants, horses, chariots, cannons and "
             "soldiers) against a chess army that has no queen and no knights.",
     "note": "Engine against engine, about even (White 45%).",
     "fen": SETUPS["noqueen-noknights"], "rules": {}, "icon": "wC"},
    {"id": "xq-noqueen", "title": "Xiangqi army against chess without the queen",
     "text": "The Xiangqi army against a chess army without its queen.",
     "note": "Engine against engine, White scored 2.5%.",
     "fen": SETUPS["noqueen"], "rules": {}, "icon": "wC"},
    {"id": "xq-default", "title": "Xiangqi army against the full chess army",
     "text": "The Xiangqi army against a complete chess army.",
     "note": "Engine against engine, White lost every game.",
     "fen": SETUPS["default"], "rules": {}, "icon": "wC"},
]
CHALLENGE_BY_ID = {c["id"]: c for c in CHALLENGES}

# Strength levels: thinking time per move (milliseconds), and how far below the best move (in
# centipawns) the engine may pick a move; lower levels choose at random among the moves within
# that margin, preferring the better ones.
LEVELS = {1: (100, 400), 2: (150, 250), 3: (200, 150), 4: (300, 90), 5: (500, 50), 6: (800, 25),
          7: (1500, 0), 8: (3000, 0)}
LEVEL_NAMES = {1: "Beginner", 2: "Novice", 3: "Casual", 4: "Club player", 5: "Strong club player",
               6: "Expert", 7: "Master", 8: "Full strength"}
ENGINE_SHORT = {"fairy": "Fairy-Stockfish", "native": "xqchess engine", "python": "Python engine"}
PIECE_VALUE = {"P": 1, "N": 3, "B": 3, "R": 5, "Q": 9, "S": 1, "H": 3, "E": 2, "A": 2, "J": 5, "C": 4}
RESULT_TEXT = {"checkmate": "by checkmate", "stalemate": "by stalemate", "repetition": "by repetition",
               "fifty-move": "by the fifty-move rule", "insufficient": "only the kings are left",
               "resigned": "You resigned"}


class GameError(Exception):
    pass


# pyengine's rules are one set for the whole program, so a request switches them to its game's
# rules while it works on the position
PE_LOCK = threading.RLock()


@contextlib.contextmanager
def rules_of(rules):
    with PE_LOCK:
        saved = dict(pe.R.__dict__)
        pe.R.__dict__.clear()
        gamefile.apply_rules(rules)
        try:
            yield
        finally:
            pe.R.__dict__.clear()
            pe.R.__dict__.update(saved)


def fairy_ready():
    with PE_LOCK:                       # the first check runs the binary under pyengine's default rules
        return fairy_available()


def board_letters(fen):
    return [ch for ch in fen.split()[0] if ch.isalpha()]


def san(P, m, legal):
    """Short algebraic notation for move m, one of `legal` (P's legal moves): Nf3, exd5, e8=Q+,
    O-O, Nbd2, B*h6 (a sniper's shot: the bishop stays where it is), Sd4 (a soldier; pawns have no
    letter)."""
    f, t, promo, fl = m & 255, (m >> 8) & 255, (m >> 16) & 255, m >> 24
    p = P.b[f]
    typ = p & 15
    if fl & pe.F_CASTLE:
        s = "O-O" if pe.FILE_[t] == 6 else "O-O-O"
    else:
        shot = bool(fl & pe.F_SNIPE)
        sep = "*" if shot else "x" if fl & pe.F_CAP else ""
        if typ == pe.PAWN:
            s = (pe.sq_name(f)[0] if sep else "") + sep + pe.sq_name(t)
        else:
            rivals = {o & 255 for o in legal if (o >> 8) & 255 == t and o & 255 != f and P.b[o & 255] == p
                      and bool((o >> 24) & pe.F_SNIPE) == shot}
            dis = ""
            if rivals:
                if all(pe.FILE_[r] != pe.FILE_[f] for r in rivals):
                    dis = pe.sq_name(f)[0]
                elif all(pe.RANK_[r] != pe.RANK_[f] for r in rivals):
                    dis = pe.sq_name(f)[1]
                else:
                    dis = pe.sq_name(f)
            s = pe.PCHARS[typ] + dis + sep + pe.sq_name(t)
        if promo:
            s += "=" + pe.PCHARS[promo]
    P.make(m)
    if P.in_check(P.side):
        s += "+" if P.has_legal_move() else "#"
    P.unmake()
    return s


def available_engines():
    out = []
    fairy = fairy_ready()
    out.append({"kind": "fairy", "label": ENGINE_LABELS["fairy"], "short": ENGINE_SHORT["fairy"],
                "available": fairy, "note": "Strongest." if fairy else
                "Not built yet (or built from older files): run  python build.py"})
    native = os.path.exists(ENGINE_PATH) or find_compiler() is not None
    out.append({"kind": "native", "label": ENGINE_LABELS["native"], "short": ENGINE_SHORT["native"],
                "available": native, "note": "Quick and fairly strong." if native else
                "Not built yet: run  python build.py"})
    out.append({"kind": "python", "label": ENGINE_LABELS["python"], "short": ENGINE_SHORT["python"],
                "available": True, "note": "Needs nothing built, but much weaker in the same thinking time."})
    return out


class Game:
    """One game: the position (pyengine, under the game's rules) and its own engine process."""

    def __init__(self, challenge, fen, rules, level, kind, threads):
        self.id = secrets.token_hex(8)
        self.challenge, self.start, self.rules = challenge, fen, dict(rules)
        self.level, self.threads = level, threads
        self.moves, self.texts = [], []
        self.lock = threading.Lock()
        self.resigned = False
        self.eval = None                  # White's evaluation after the engine's last search
        self.last_engine = None
        self.touched = time.time()
        with rules_of(self.rules):
            try:
                self.P = pe.Pos(fen)
            except Exception:
                raise GameError("that is not a position these rules allow (each side needs one king or "
                                "general, and the side not to move may not be in check)")
            self.fens = [self.P.fen()]
        if kind == "fairy" and not fairy_ready():
            raise GameError("Fairy-Stockfish has not been built from the current files (run  python build.py)")
        self.engine = Engine(rules=self.rules, kind=kind, threads=threads)
        self.kind = self.engine.kind
        try:
            self.engine.position(self.start, [])
        except ValueError as e:
            self.close()
            raise GameError(f"the engine does not accept that position ({e})")

    def close(self):
        try:
            self.engine.close()
        except Exception:
            pass

    # --------------------------------------------------------------- state
    def over(self):
        """(reason, result) of a finished game, or None.  Call with the game's rules in force."""
        if self.resigned:
            return "resigned", "0-1"
        reason, result = pe.game_over(self.P)
        return (reason, result) if reason else None

    def state(self):
        """Everything the page shows.  Call with the game's rules in force."""
        P = self.P
        fen = P.fen()
        over = self.over()
        legal = []
        if not over and P.side == pe.WHITE:
            ml = P.legal_moves()
            for m in ml:
                s = pe.move_str(m)
                shot = bool((m >> 24) & pe.F_SNIPE)
                legal.append({"uci": s, "from": s[:2], "to": s[2:4], "shot": shot,
                              "promo": s[4] if len(s) == 5 and not shot else "", "san": san(P, m, ml)})
        check = pe.sq_name(P.ksq[P.side]) if P.in_check(P.side) else None
        royal = lambda c: "G" if P.b[P.ksq[c]] & 15 == pe.GENERAL else "K"
        # pieces each side has lost since the start (promotions make this approximate)
        start, now = board_letters(self.start), board_letters(fen)
        lost = {"w": [], "b": []}
        for ch in sorted(set(start)):
            if ch.upper() in "KG":
                continue
            n = start.count(ch) - now.count(ch)
            lost["w" if ch.isupper() else "b"] += [ch.upper()] * max(0, n)
        value = lambda letters: sum(PIECE_VALUE.get(ch.upper(), 0) * (1 if ch.isupper() else -1) for ch in letters)
        result = None
        if over:
            reason, res = over
            you = {"1-0": "win", "0-1": "loss"}.get(res, "draw")
            headline = {"win": "You won!", "loss": "You lost", "draw": "Draw"}[you]
            text = RESULT_TEXT.get(reason, reason)
            if reason == "checkmate":
                text = "by checkmate" if you == "win" else "The engine checkmated you"
            elif reason == "stalemate" and you != "draw":
                text = "by stalemate (stalemate loses under these rules)"
            result = {"reason": reason, "result": res, "you": you, "headline": headline, "text": text}
        c = self.challenge
        return {
            "id": self.id, "fen": fen, "start": self.start, "turn": "w" if P.side == pe.WHITE else "b",
            "moves": list(self.moves), "texts": list(self.texts), "fens": list(self.fens), "legal": legal,
            "check": check, "lastMove": self._last_move(), "royals": {"w": royal(pe.WHITE), "b": royal(pe.BLACK)},
            "snipers": bool(pe.R.snipers), "lost": lost, "material": value(now) - value(start), "over": result,
            "eval": self.eval, "level": self.level, "levelName": LEVEL_NAMES[self.level],
            "engine": {"kind": self.kind, "label": ENGINE_LABELS[self.kind], "short": ENGINE_SHORT[self.kind]},
            "challenge": {k: c.get(k, "") for k in ("id", "title", "text", "note", "icon")},
            "engineMove": self.last_engine,
        }

    def _last_move(self):
        if not self.moves:
            return None
        s = self.moves[-1]
        return {"uci": s, "from": s[:2], "to": s[2:4], "shot": s.endswith("s"),
                "by": "w" if (len(self.moves) % 2 == 1) == (self.start.split()[1] == "w") else "b"}

    def _make(self, m, s):
        self.texts.append(san(self.P, m, self.P.legal_moves()))
        self.P.make(m)
        self.moves.append(s)
        self.fens.append(self.P.fen())

    def _search(self, movetime, multipv):
        """The engine's lines for the current position; an engine that has stopped is started again."""
        for attempt in (1, 2):
            try:
                self.engine.position(self.start, self.moves)
                return self.engine.analyse(movetime=movetime, multipv=multipv)
            except (RuntimeError, OSError) as e:
                if attempt == 2:
                    raise GameError(f"the engine stopped working ({e})")
                self.close()
                self.engine = Engine(rules=self.rules, kind=self.kind, threads=self.threads)

    # --------------------------------------------------------------- actions
    def play(self, s):
        with rules_of(self.rules):
            if self.over():
                raise GameError("the game is over")
            if self.P.side != pe.WHITE:
                raise GameError("it is the engine's move")
            m = pe.parse_move(self.P, s)
            if m is None:
                raise GameError(f"{s} is not a legal move here")
            self._make(m, s)
            self.last_engine = None
            return self.state()

    def reply(self):
        """The engine chooses and plays Black's move."""
        with rules_of(self.rules):
            if self.over() or self.P.side != pe.BLACK:
                return self.state()
            n_legal = len(self.P.legal_moves())
        movetime, margin = LEVELS[self.level]
        lines = self._search(movetime, min(6, n_legal) if margin else 1)
        scored = [(ln["pv"][0], score_of(ln), ln) for ln in lines if ln.get("pv")]
        with rules_of(self.rules):
            mv = None
            if scored:
                best = max(sc for _, sc, _ in scored)
                ok = [(mv, sc) for mv, sc, _ in scored if sc >= best - margin]
                weights = [math.exp(-(best - sc) / max(1.0, margin / 2)) for _, sc in ok]
                mv = random.choices([mv for mv, _ in ok], weights)[0]
                top = scored[0][2]
                cp = -score_of(top)                    # the engine is Black: White's point of view
                own = self.kind == "fairy" and fairy_engine.own_evaluation(*fairy_engine.royals(self.start))
                mate = -top["mate"] if "mate" in top else None
                self.eval = {"cp": cp, "mate": mate, "expected": pe.expected_score(cp, own) if mate is None
                             else (1.0 if mate > 0 else 0.0), "depth": top.get("depth")}
            m = pe.parse_move(self.P, mv) if mv else None
            if m is None:                              # (cannot happen: both follow the same rules)
                legal = self.P.legal_moves()
                if not legal:
                    return self.state()
                m = legal[0]
                mv = pe.move_str(m)
            self._make(m, mv)
            self.last_engine = {"uci": mv, "san": self.texts[-1]}
            return self.state()

    def hint(self):
        """The engine's choice for White (your move)."""
        with rules_of(self.rules):
            if self.over() or self.P.side != pe.WHITE:
                raise GameError("it is not your move")
        lines = self._search(HINT_MS, 1)
        mv = lines[0]["pv"][0] if lines and lines[0].get("pv") else None
        with rules_of(self.rules):
            m = pe.parse_move(self.P, mv) if mv else None
            if m is None:
                raise GameError("the engine found no move")
            return {"uci": mv, "from": mv[:2], "to": mv[2:4], "shot": mv.endswith("s"),
                    "san": san(self.P, m, self.P.legal_moves())}

    def undo(self):
        """Take back your last move (and the engine's answer to it)."""
        with rules_of(self.rules):
            self.resigned = False
            n = 0
            while self.moves and (n == 0 or self.P.side != pe.WHITE):
                self.P.unmake()
                self.moves.pop()
                self.texts.pop()
                self.fens.pop()
                n += 1
            self.last_engine = None
            return self.state()

    def resign(self):
        with rules_of(self.rules):
            if not self.over():
                self.resigned = True
            return self.state()

    def set_level(self, level):
        self.level = level
        with rules_of(self.rules):
            return self.state()

    def current(self):
        with rules_of(self.rules):
            return self.state()

    def pgn(self):
        """The game as a PGN file that the analysis board (gui.py) can open."""
        with rules_of(self.rules):
            start = pe.Pos(self.start)
            headers = {"Site": "webplay.py", "Challenge": self.challenge["title"],
                       "White": f"You ({gamefile.army(start, pe.WHITE)})",
                       "Black": f"{ENGINE_SHORT[self.kind]}, level {self.level} {LEVEL_NAMES[self.level]} "
                                f"({gamefile.army(start, pe.BLACK)})"}
            if self.resigned:
                headers.update({"Result": "0-1", "Termination": "White resigned"})
            text = gamefile.to_pgn(self.start, self.moves, headers)
        name = f"{self.challenge['id']}-{datetime.date.today().isoformat()}.pgn"
        return {"pgn": text, "filename": name}


def score_of(line):
    if "mate" in line:
        n = line["mate"]
        return 30000 - n if n > 0 else -30000 - n
    return line.get("score_cp", 0)


# ------------------------------------------------------------------ games
GAMES = {}
GAMES_LOCK = threading.Lock()


def close_later(games):
    """Stop the engines of games that are no longer needed, without making the request wait."""
    for g in games:
        threading.Thread(target=g.close, daemon=True).start()


def new_game(body, threads):
    try:
        level = int(body.get("level", 8))
    except (TypeError, ValueError):
        level = 0
    if level not in LEVELS:
        raise GameError("the level must be 1 to 8")
    kind = body.get("engine", "auto")
    if kind not in ("auto", "fairy", "native", "python"):
        raise GameError("unknown engine")
    if body.get("fen"):
        fen = " ".join(str(body["fen"]).split())
        if len(fen) > 200:
            raise GameError("that position is too long to be a FEN")
        rules = {"Snipers": True} if body.get("snipers") else {}
        challenge = {"id": "custom", "title": "Your position" + (" (snipers)" if rules else ""),
                     "text": "A position you set up." + (" White's bishops are snipers." if rules else ""),
                     "note": "", "icon": "wSniper" if rules else "wK", "fen": fen, "rules": rules}
    else:
        challenge = CHALLENGE_BY_ID.get(body.get("challenge", "snipers"))
        if challenge is None:
            raise GameError("unknown challenge")
        fen, rules = challenge["fen"], challenge["rules"]
    game = Game(challenge, fen, rules, level, kind, threads)
    with GAMES_LOCK:
        old = GAMES.pop(str(body.get("replace", "")), None)
        now = time.time()
        stale = [g for g in GAMES.values() if now - g.touched > IDLE_HOURS * 3600]
        while len(GAMES) - len(stale) >= MAX_GAMES:
            stale.append(min((g for g in GAMES.values() if g not in stale), key=lambda g: g.touched))
        for g in stale:
            GAMES.pop(g.id, None)
        GAMES[game.id] = game
    close_later(([old] if old else []) + stale)
    return game


def find_game(body):
    with GAMES_LOCK:
        game = GAMES.get(str(body.get("id", "")))
    if game is None:
        raise KeyError("no such game (the server may have been restarted)")
    game.touched = time.time()
    return game


def info():
    engines = available_engines()
    default = next((e["kind"] for e in engines if e["available"]), "python")
    challenges = []
    for c in CHALLENGES:
        d = {k: c[k] for k in ("id", "title", "text", "note", "icon", "fen")}
        d["snipers"] = bool(c["rules"].get("Snipers"))
        challenges.append(d)
    return {"challenges": challenges,
            "levels": [{"level": k, "name": LEVEL_NAMES[k], "ms": LEVELS[k][0], "margin": LEVELS[k][1]}
                       for k in sorted(LEVELS)],
            "engines": engines, "defaultEngine": default, "defaultLevel": 8}


# ------------------------------------------------------------------ HTTP
STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8")}
GAME_ROUTES = ("/api/state", "/api/move", "/api/reply", "/api/undo", "/api/resign", "/api/level", "/api/hint",
               "/api/pgn")


class Handler(BaseHTTPRequestHandler):
    server_version = "xqchess-webplay"
    threads = 1

    def log_message(self, fmt, *args):         # quiet: only errors are reported
        pass

    def _send(self, code, body, ctype, cache="no-cache"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj), "application/json; charset=utf-8")

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path in STATIC:
                name, ctype = STATIC[path]
                file = os.path.join(WEB_DIR, name)
                if not os.path.exists(file):
                    self._send(404, f"{file} is missing: unpack the whole zip again.", "text/plain; charset=utf-8")
                    return
                with open(file, "rb") as fh:
                    self._send(200, fh.read(), ctype)
            elif re.fullmatch(r"/pieces/[A-Za-z]{2,12}\.png", path) and os.path.exists(os.path.join(PIECE_DIR, path[8:])):
                with open(os.path.join(PIECE_DIR, path[8:]), "rb") as fh:
                    self._send(200, fh.read(), "image/png", cache="max-age=86400")
            elif path == "/favicon.ico":
                self.send_response(302)
                self.send_header("Location", "/pieces/wSniper.png")
                self.end_headers()
            elif path == "/api/info":
                self._json(200, info())
            else:
                self._json(404, {"error": "not found"})
        except (ConnectionError, BrokenPipeError):
            pass

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise GameError("request too large")
            body = json.loads(self.rfile.read(n) or b"{}") if n > 0 else {}
            if not isinstance(body, dict):
                raise GameError("expected a JSON object")
            if path == "/api/new":
                game = new_game(body, self.threads)
                with game.lock:
                    self._json(200, game.current())
            elif path == "/api/shutdown":
                if self.client_address[0] not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
                    self._json(403, {"error": "the server can only be stopped from the computer it runs on"})
                    return
                self._json(200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            elif path in GAME_ROUTES:
                game = find_game(body)
                with game.lock:
                    if path == "/api/state":
                        out = game.current()
                    elif path == "/api/move":
                        out = game.play(str(body.get("move", "")))
                    elif path == "/api/reply":
                        out = game.reply()
                    elif path == "/api/undo":
                        out = game.undo()
                    elif path == "/api/resign":
                        out = game.resign()
                    elif path == "/api/hint":
                        out = game.hint()
                    elif path == "/api/pgn":
                        out = game.pgn()
                    else:
                        try:
                            level = int(body.get("level", 0))
                        except (TypeError, ValueError):
                            level = 0
                        if level not in LEVELS:
                            raise GameError("the level must be 1 to 8")
                        out = game.set_level(level)
                self._json(200, out)
            else:
                self._json(404, {"error": "not found"})
        except (ConnectionError, BrokenPipeError):
            pass
        except KeyError as e:
            self._json(404, {"error": str(e).strip("'\"")})
        except (GameError, ValueError) as e:
            self._json(400, {"error": str(e)})
        except Exception as e:                     # an engine that could not be started, for instance
            self._json(500, {"error": f"{type(e).__name__}: {e}"})


def server_running(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/info", timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default="127.0.0.1",
                    help="address to listen on: 127.0.0.1 (default) is this computer only; 0.0.0.0 lets "
                         "other computers on the network connect")
    ap.add_argument("--no-browser", action="store_true", help="don't open the page in the web browser")
    ap.add_argument("--threads", type=int, default=max(1, (os.cpu_count() or 2) // 2),
                    help="CPU cores for each game's engine (default: half the logical processors)")
    ap.add_argument("--stop", action="store_true", help="stop the server running on --port")
    ap.add_argument("--in-process", action="store_true",
                    help="in a Jupyter notebook: serve from the kernel instead of a program of its own")
    a = ap.parse_args()
    url = f"http://{'127.0.0.1' if a.host in ('0.0.0.0', '::', '') else a.host}:{a.port}/"
    if a.stop:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{a.port}/api/shutdown", data=b"{}", method="POST",
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5).read()
            print(f"Stopped the server on port {a.port}.")
        except Exception:
            print(f"No server is running on port {a.port}.")
        return
    if server_running(a.port):
        print(f"The server is already running: {url}")
        if not a.no_browser:
            webbrowser.open(url)
        return
    Handler.threads = max(1, a.threads)
    try:
        httpd = ThreadingHTTPServer((a.host, a.port), Handler)
    except OSError as e:
        print(f"Cannot use port {a.port} ({e}); try another with --port.")
        sys.exit(1)
    httpd.daemon_threads = True
    threading.Thread(target=available_engines, daemon=True).start()   # checks the engines before the page asks
    if os.environ.get("XQCHESS_WEBPLAY_LAUNCHED"):                   # started by launch(), which says how to stop it
        print(f"Play the engines at {url}", flush=True)
    else:
        print(f"Play the engines at {url}   (stop the server with Ctrl+C, or: python webplay.py --stop)", flush=True)
    if a.host not in ("127.0.0.1", "localhost", "::1"):
        print("Other computers on the network can connect too, and each game they start runs an engine here.",
              flush=True)
    if not a.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        with GAMES_LOCK:
            games = list(GAMES.values())
            GAMES.clear()
        for g in games:
            g.close()
        print("Server stopped.", flush=True)


if __name__ == "__main__":
    main()
