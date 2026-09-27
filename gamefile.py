"""gamefile.py -- save and load games as PGN-style text files.

A saved game looks like ordinary chess PGN, with the start position and the rule
settings stored as tags:

    [Event "Chess vs Xiangqi"]
    [Date "2026.09.26"]
    [White "Xiangqi army"]
    [Black "Western army"]
    [Result "*"]
    [SetUp "1"]
    [FEN "rnb1kbnr/pppppppp/8/8/8/S2SS2S/1C4C1/JHEAGEHJ w kq - 0 1"]
    [HorseBlock "true"]
    ...
    1. Cg2-g4 e7-e5 2. Cb2-c2 Nb8-c6 *

Moves are written as piece letter + from-square + '-' or 'x' + to-square (+ '=Q' for a
promotion, '+' for check); pawns have no letter.  Under the snipers rule a shot (the bishop
takes without moving) is written with '*', e.g. Bc1*h6.  When reading, plain coordinate moves
such as g2g4, b7b8q or c1h6s (a shot) are accepted too.

Use from Python (e.g. a notebook):
    import gamefile, pyengine as pe
    g = gamefile.load("mygame.pgn")          # also applies the file's rule settings
    P = pe.Pos(g["fen"])
    for mv in g["moves"]:
        P.make(pe.parse_move(P, mv))
"""
import datetime
import re

import pyengine as pe

RULE_TAGS = ("HorseBlock", "ElephantEye", "StalemateLoss", "SoldierSideways", "SoldierPromotion", "Snipers")
PROMO_NAMES = {0: "none", 1: "xiangqi", 2: "western", 3: "any"}


def move_text(P, m):
    """Readable text for move m in position P, e.g. Cg4-e4, e7xd6, Sb7-b8=Q+, O-O, Bc1*h6 (a shot)."""
    f, t, promo, fl = m & 255, (m >> 8) & 255, (m >> 16) & 255, m >> 24
    letter = pe.PCHARS[P.b[f] & 15]
    if fl & pe.F_CASTLE:
        s = "O-O" if pe.FILE_[t] == 6 else "O-O-O"
    else:
        sep = "*" if fl & pe.F_SNIPE else "x" if fl & pe.F_CAP else "-"
        s = ("" if letter == "P" else letter) + pe.sq_name(f) + sep + pe.sq_name(t)
        if promo:
            s += "=" + pe.PCHARS[promo]
    P.make(m)
    if P.in_check(P.side):
        s += "+"
    P.unmake()
    return s


def current_rules():
    R = pe.R
    return {"HorseBlock": R.horse_block, "ElephantEye": R.elephant_eye, "StalemateLoss": R.stalemate_loss,
            "SoldierSideways": R.soldier_sideways, "SoldierPromotion": PROMO_NAMES[R.soldier_promo],
            "Snipers": R.snipers}


def apply_rules(rules):
    """Set pyengine's rules from a dict like the one current_rules() returns (missing keys unchanged)."""
    tf = lambda v: str(v).lower() in ("true", "1", "yes", "on")
    if "HorseBlock" in rules: pe.R.horse_block = tf(rules["HorseBlock"])
    if "ElephantEye" in rules: pe.R.elephant_eye = tf(rules["ElephantEye"])
    if "StalemateLoss" in rules: pe.R.stalemate_loss = tf(rules["StalemateLoss"])
    if "SoldierSideways" in rules: pe.R.soldier_sideways = tf(rules["SoldierSideways"])
    if "Snipers" in rules: pe.R.snipers = tf(rules["Snipers"])
    if "SoldierPromotion" in rules:
        inv = {v: k for k, v in PROMO_NAMES.items()}
        pe.R.soldier_promo = inv.get(str(rules["SoldierPromotion"]).lower(), pe.R.soldier_promo)


def army(P, colour):
    return "Xiangqi army" if P.b[P.ksq[colour]] & 15 == pe.GENERAL else "Western army"


def to_pgn(start_fen, moves, headers=None):
    """PGN text for a game given as coordinate moves from start_fen (current rules are recorded)."""
    P = pe.Pos(start_fen)
    texts = []
    for mv in moves:
        m = pe.parse_move(P, mv)
        if m is None:
            raise ValueError(f"illegal move {mv} in game")
        texts.append(move_text(P, m))
        P.make(m)
    reason, result = pe.game_over(P)
    start = pe.Pos(start_fen)
    western = army(start, pe.WHITE) == army(start, pe.BLACK) == "Western army"
    event = "Snipers chess" if western and pe.R.snipers else "Chess" if western else "Chess vs Xiangqi"
    tags = {"Event": event, "Date": datetime.date.today().strftime("%Y.%m.%d"),
            "White": army(start, pe.WHITE), "Black": army(start, pe.BLACK), "Result": result or "*",
            "SetUp": "1", "FEN": start_fen}
    if reason:
        tags["Termination"] = reason
    tags.update({k: str(v).lower() for k, v in current_rules().items()})
    tags.update(headers or {})
    out = [f'[{k} "{v}"]' for k, v in tags.items()] + [""]
    num, white = start.full, start.side == pe.WHITE
    tokens = [] if white or not texts else [f"{num}..."]
    for t in texts:
        if white:
            tokens.append(f"{num}.")
        tokens.append(t)
        if not white:
            num += 1
        white = not white
    tokens.append(tags["Result"])
    line = ""
    for tok in tokens:                       # wrap at 80 columns
        if len(line) + len(tok) + 1 > 80:
            out.append(line)
            line = tok
        else:
            line = f"{line} {tok}" if line else tok
    out.append(line)
    return "\n".join(out) + "\n"


def save(path, start_fen, moves, headers=None):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(to_pgn(start_fen, moves, headers))


def parse_pgn(text):
    """Split PGN text into (tags dict, list of move tokens)."""
    tags = dict(re.findall(r'\[(\w+)\s+"([^"]*)"\]', text))
    body = re.sub(r"\[[^\]]*\]", " ", text)
    body = re.sub(r"\{[^}]*\}", " ", body)            # comments
    body = re.sub(r";[^\n]*", " ", body)
    toks = []
    for tok in body.split():
        tok = re.sub(r"^\d+\.(\.\.)?", "", tok)       # move numbers, also "12.e4"
        if not tok or tok in ("1-0", "0-1", "1/2-1/2", "*"):
            continue
        toks.append(tok)
    return tags, toks


def _norm(s):
    return s.rstrip("+#!?").replace("x", "-").replace("0-0-0", "O-O-O").replace("0-0", "O-O").upper()


def resolve(start_fen, tokens):
    """Turn move tokens into coordinate moves under the current rules.
    Returns (moves, error) -- error is None, or a message naming the first unreadable move."""
    P = pe.Pos(start_fen)
    moves = []
    for i, tok in enumerate(tokens):
        legal = P.legal_moves()
        found = None
        low = tok.lower().rstrip("+#!?")
        for m in legal:
            if pe.move_str(m) == low:
                found = m
                break
        if found is None:
            want = _norm(tok)
            for m in legal:
                if _norm(move_text(P, m)) == want:
                    found = m
                    break
        if found is None:
            return moves, f"move {i + 1} ('{tok}') is not legal in that position"
        moves.append(pe.move_str(found))
        P.make(found)
    return moves, None


def load(path, apply=True):
    """Read a saved game.  Returns {'fen', 'moves', 'tags', 'error'}.  With apply=True the
    file's rule settings are applied to pyengine first (they decide which moves are legal)."""
    with open(path, encoding="utf-8") as fh:
        tags, toks = parse_pgn(fh.read())
    if apply:
        rules = {k: v for k, v in tags.items() if k in RULE_TAGS}
        rules.setdefault("Snipers", "false")          # files from before the snipers rule
        apply_rules(rules)
    fen = tags.get("FEN", pe.START_FEN)
    moves, err = resolve(fen, toks)
    return {"fen": fen, "moves": moves, "tags": tags, "error": err}
