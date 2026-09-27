#!/usr/bin/env python3
"""pyengine.py -- pure-Python version of the xqchess engine.

Same rules, same evaluation and the same text protocol as engine/xqchess.cpp, so the
GUI and the scripts can use either.  It needs nothing but Python 3, but it searches
roughly 50-100 times fewer positions per second than the compiled engine (a few
plies less deep in the same time).

Run it on its own and type commands, exactly as for the C++ engine:
    python pyengine.py
    position startpos moves g2g4 e7e5
    go movetime 5000 multipv 3
    quit

Commands: position startpos|fen <FEN> [moves ...]; go [depth D] [nodes N] [movetime MS]
[multipv K]; stop; legal; perft D; divide D; eval; d; fen; isready; setoption Threads N;
setoption HorseBlock|ElephantEye|StalemateLoss|SoldierSideways|Snipers true|false;
setoption SoldierPromotion any|xiangqi|western|none; setoption Value <letter> <cp>;
selfplay games G [nodes N] [depth D] [randomplies R] [randmargin CP] [seed S]
[maxplies M] [resign CP] [samplevery K] [fen FEN]; quit
"""
import math
import random
import sys
import threading
import time

# ------------------------------------------------------------------ pieces --
(NONE, PAWN, KNIGHT, BISHOP, ROOK, QUEEN, KING,
 SOLDIER, HORSE, ELEPHANT, ADVISOR, CHARIOT, CANNON, GENERAL) = range(14)
NTYPES = 14
WHITE, BLACK = 0, 1
EMPTY, OFFB = 0, 64
PCHARS = ".PNBRQKSHEAJCG"
ROYAL = (KING, GENERAL)


def SQ(f, r):
    return (r + 2) * 12 + f + 2


FILE_ = [s % 12 - 2 for s in range(144)]
RANK_ = [s // 12 - 2 for s in range(144)]
N, S, E, W = 12, -12, 1, -1
ORTH = (N, S, E, W)
DIAG = (N + E, N + W, S + E, S + W)
KNIGHT_OFF = (2 * N + E, 2 * N + W, 2 * S + E, 2 * S + W, 2 * E + N, 2 * E + S, 2 * W + N, 2 * W + S)
PERP = {N: (E, W), S: (E, W), E: (N, S), W: (N, S)}
HORSE_STEPS = tuple((o, 2 * o + p) for o in ORTH for p in PERP[o])        # (leg, target) offsets
SQ64 = [SQ(f, r) for r in range(8) for f in range(8)]


class Rules:
    horse_block = True
    elephant_eye = False
    stalemate_loss = False
    soldier_sideways = True
    soldier_promo = 3            # 0 none, 1 Xiangqi pieces, 2 Western pieces, 3 any piece
    snipers = False              # White's bishops are snipers: they can also take without moving


R = Rules()
PROMO_LISTS = {
    1: (CHARIOT, CANNON, HORSE, ELEPHANT, ADVISOR),
    2: (QUEEN, ROOK, BISHOP, KNIGHT),
    3: (QUEEN, CHARIOT, ROOK, CANNON, HORSE, KNIGHT, BISHOP, ELEPHANT, ADVISOR),
}

# material values in centipawns, fitted to self-play results by tools/tune_eval.py (middlegame values);
# the order is NONE P N B R Q K S H E A J C G
VAL = [0, 60, 319, 330, 500, 900, 0, 115, 180, 51, 60, 461, 182, 0]
# endgame corrections to VAL: added in full when only pawns/soldiers are left, scaled down as pieces return
VAL_EG = [0, 32, -22, -6, 25, 3, 0, -9, -10, 18, 14, -41, -40, 0]


def crossed(s, c):
    return RANK_[s] >= 4 if c == WHITE else RANK_[s] <= 3


# ------------------------------------------------------------------- moves --
# F_SNIPE: a shot.  With the snipers rule a white bishop can take the first enemy piece along a
# diagonal without moving: it stays where it is and the piece leaves the board.  A shot is written
# with an "s" after the squares (c1h6s); c1h6 is the ordinary capture, the bishop moving to h6.
F_CAP, F_EP, F_CASTLE, F_DOUBLE, F_SNIPE = 1, 2, 4, 8, 16


def MV(f, t, promo=0, flags=0):
    return f | (t << 8) | (promo << 16) | (flags << 24)


def sq_name(s):
    return "abcdefgh"[FILE_[s]] + str(RANK_[s] + 1)


def move_str(m):
    if not m:
        return "0000"
    r = sq_name(m & 255) + sq_name((m >> 8) & 255)
    promo = (m >> 16) & 255
    if (m >> 24) & F_SNIPE:
        return r + "s"
    return r + PCHARS[promo].lower() if promo else r


# ----------------------------------------------------------------- zobrist --
_rng = random.Random(20260925)
ZP = [[_rng.getrandbits(64) for _ in range(144)] for _ in range(32)]
ZSIDE = _rng.getrandbits(64)
ZCASTLE = [_rng.getrandbits(64) for _ in range(16)]
ZEP = [_rng.getrandbits(64) for _ in range(144)]

CASTLE_MASK = [15] * 144
CASTLE_MASK[SQ(4, 0)] = 12; CASTLE_MASK[SQ(7, 0)] = 14; CASTLE_MASK[SQ(0, 0)] = 13
CASTLE_MASK[SQ(4, 7)] = 3; CASTLE_MASK[SQ(7, 7)] = 11; CASTLE_MASK[SQ(0, 7)] = 7

START_FEN = "rnbqkbnr/pppppppp/8/8/8/S2SS2S/1C4C1/JHEAGEHJ w kq - 0 1"


# ---------------------------------------------------------------- position --
class Pos:
    def __init__(self, fen=START_FEN):
        if not self.set_fen(fen):
            raise ValueError(f"invalid FEN: {fen}")

    def set_fen(self, fen):
        """Parse a FEN.  Returns False, leaving the position unchanged, if it is not valid."""
        parts = fen.split()
        if not parts or len(parts) > 6:
            return False
        parts += ["w", "-", "-", "0", "1"][len(parts) - 1:]
        b = [OFFB] * 144
        for s in SQ64:
            b[s] = EMPTY
        ksq = [-1, -1]
        royals = [0, 0]
        rows = parts[0].split("/")
        if len(rows) != 8:
            return False
        for i, row in enumerate(rows):
            r, f = 7 - i, 0
            for ch in row:
                if ch in "12345678":
                    f += int(ch)
                    if f > 8:
                        return False
                    continue
                t = PCHARS.find(ch.upper())
                if t <= 0 or not ch.isalpha() or f > 7:
                    return False
                if t == PAWN and r in (0, 7):             # pawns never stand on rank 1 or 8
                    return False
                c = WHITE if ch.isupper() else BLACK
                b[SQ(f, r)] = t | (c << 4)
                if t in ROYAL:
                    ksq[c] = SQ(f, r)
                    royals[c] += 1
                f += 1
            if f != 8:
                return False
        if royals != [1, 1] or parts[1] not in ("w", "b"):
            return False
        side = BLACK if parts[1] == "b" else WHITE
        castle = 0
        if parts[2] != "-":
            for ch in parts[2]:
                bit = {"K": 1, "Q": 2, "k": 4, "q": 8}.get(ch, 0)
                if not bit or castle & bit:
                    return False
                castle |= bit
        ep = -1
        if parts[3] != "-":
            e = parts[3]
            if len(e) != 2 or e[0] not in "abcdefgh" or e[1] != ("6" if side == WHITE else "3"):
                return False
            ep = SQ(ord(e[0]) - 97, int(e[1]) - 1)
            if b[ep] != EMPTY:
                return False
        if not (parts[4].isdigit() and parts[5].isdigit() and int(parts[5]) >= 1):
            return False
        old = self.__dict__.copy()
        self.b, self.ksq, self.side, self.castle, self.ep = b, ksq, side, castle, ep
        self.half, self.full, self.rev = int(parts[4]), int(parts[5]), 0
        if self.attacked(ksq[side ^ 1], side):          # the side not to move may not be in check
            self.__dict__.clear()
            self.__dict__.update(old)
            return False
        self.undo, self.hh = [], []
        self.hash = self.compute_hash()
        return True

    def compute_hash(self):
        h = 0
        for s in SQ64:
            if self.b[s]:
                h ^= ZP[self.b[s]][s]
        if self.side:
            h ^= ZSIDE
        h ^= ZCASTLE[self.castle]
        if self.ep >= 0:
            h ^= ZEP[self.ep]
        return h

    def fen(self):
        rows = []
        for r in range(7, -1, -1):
            row, e = "", 0
            for f in range(8):
                p = self.b[SQ(f, r)]
                if not p:
                    e += 1
                    continue
                if e:
                    row, e = row + str(e), 0
                ch = PCHARS[p & 15]
                row += ch if p >> 4 == WHITE else ch.lower()
            rows.append(row + (str(e) if e else ""))
        cs = "".join(ch for ch, bit in (("K", 1), ("Q", 2), ("k", 4), ("q", 8)) if self.castle & bit) or "-"
        ep = sq_name(self.ep) if self.ep >= 0 else "-"
        return f"{'/'.join(rows)} {'w' if self.side == WHITE else 'b'} {cs} {ep} {self.half} {self.full}"

    def board_text(self):
        out = []
        for r in range(7, -1, -1):
            cells = []
            for f in range(8):
                p = self.b[SQ(f, r)]
                ch = PCHARS[p & 15] if p else "."
                cells.append(ch.lower() if p and p >> 4 == BLACK else ch)
            out.append(f" {r + 1}  " + " ".join(cells) + " ")
        out += ["", "    a b c d e f g h", "", f" fen: {self.fen()}"]
        return "\n".join(out)

    # -- attack test: is square t attacked by colour `by`? (reverse look-up) --
    def attacked(self, t, by):
        b = self.b
        c16 = by << 4
        fwd = N if by == WHITE else S
        pawn = PAWN | c16
        if b[t - fwd + 1] == pawn or b[t - fwd - 1] == pawn:
            return True
        sold = SOLDIER | c16
        if b[t - fwd] == sold:
            return True
        if R.soldier_sideways:
            if b[t + 1] == sold and crossed(t + 1, by):
                return True
            if b[t - 1] == sold and crossed(t - 1, by):
                return True
        kn = KNIGHT | c16
        for o in KNIGHT_OFF:
            if b[t + o] == kn:
                return True
        kg, gen, adv, ele = KING | c16, GENERAL | c16, ADVISOR | c16, ELEPHANT | c16
        for d in ORTH:
            x = b[t + d]
            if x == kg or x == gen:
                return True
        eye = R.elephant_eye
        for d in DIAG:
            x = b[t + d]
            if x == kg or x == adv:
                return True
            if b[t + 2 * d] == ele and (not eye or x == EMPTY):
                return True
        hor = HORSE | c16
        block = R.horse_block
        for leg, tgt in HORSE_STEPS:
            h = t - tgt
            if b[h] == hor and (not block or b[h + leg] == EMPTY):
                return True
        rk, qn, ch, can, bs = ROOK | c16, QUEEN | c16, CHARIOT | c16, CANNON | c16, BISHOP | c16
        for d in ORTH:
            s = t + d
            while b[s] == EMPTY:
                s += d
            x = b[s]
            if x == OFFB:
                continue
            if x == rk or x == qn or x == ch:
                return True
            s += d
            while b[s] == EMPTY:
                s += d
            if b[s] == can:
                return True
        for d in DIAG:
            s = t + d
            while b[s] == EMPTY:
                s += d
            x = b[s]
            if x == bs or x == qn:
                return True
        return False

    def in_check(self, c):
        return self.attacked(self.ksq[c], c ^ 1)

    # -- pseudo-legal move generation --
    def gen_piece(self, s, ml, caps_only, shots=True):
        """Pseudo-legal moves of the piece on s, appended to ml (shots=False leaves out a sniper's shots)."""
        b = self.b
        p = b[s]
        c = p >> 4
        t = p & 15
        add = ml.append

        def enemy(q):
            return q != EMPTY and q != OFFB and (q >> 4) != c

        def push(q_sq):              # quiet or capture onto q_sq, if allowed
            q = b[q_sq]
            if q == EMPTY:
                if not caps_only:
                    add(s | (q_sq << 8))
            elif q != OFFB and (q >> 4) != c:
                add(s | (q_sq << 8) | (F_CAP << 24))

        if t == PAWN:
            fwd, start, last = (N, 1, 7) if c == WHITE else (S, 6, 0)
            to = s + fwd
            if b[to] == EMPTY:
                if RANK_[to] == last:
                    add(MV(s, to, QUEEN))
                    if not caps_only:
                        for pr in (ROOK, BISHOP, KNIGHT):
                            add(MV(s, to, pr))
                elif not caps_only:
                    add(MV(s, to))
                    if RANK_[s] == start and b[to + fwd] == EMPTY:
                        add(MV(s, to + fwd, 0, F_DOUBLE))
            for d in (E, W):
                q = s + fwd + d
                if enemy(b[q]):
                    if RANK_[q] == last:
                        add(MV(s, q, QUEEN, F_CAP))
                        if not caps_only:
                            for pr in (ROOK, BISHOP, KNIGHT):
                                add(MV(s, q, pr, F_CAP))
                    else:
                        add(MV(s, q, 0, F_CAP))
                elif q == self.ep and b[q] == EMPTY:
                    add(MV(s, q, 0, F_EP | F_CAP))
        elif t == SOLDIER:
            fwd, last = (N, 7) if c == WHITE else (S, 0)
            to = s + fwd
            q = b[to]
            if R.soldier_promo and q != OFFB and RANK_[to] == last and (q == EMPTY or enemy(q)):
                fl = F_CAP if q != EMPTY else 0
                pr = PROMO_LISTS[R.soldier_promo]
                add(MV(s, to, pr[0], fl))
                if not caps_only:
                    for x in pr[1:]:
                        add(MV(s, to, x, fl))
            else:
                push(to)
            if R.soldier_sideways and crossed(s, c):
                push(s + E)
                push(s + W)
        elif t == KNIGHT:
            for o in KNIGHT_OFF:
                push(s + o)
        elif t == HORSE:
            block = R.horse_block
            for leg, tgt in HORSE_STEPS:
                x = b[s + leg]
                if x == OFFB or (block and x != EMPTY):
                    continue
                push(s + tgt)
        elif t == ELEPHANT:
            eye = R.elephant_eye
            for d in DIAG:
                if eye and b[s + d] != EMPTY:
                    continue
                push(s + 2 * d)
        elif t == ADVISOR:
            for d in DIAG:
                push(s + d)
        elif t == GENERAL:
            for d in ORTH:
                push(s + d)
        elif t == KING:
            for d in ORTH:
                push(s + d)
            for d in DIAG:
                push(s + d)
        elif t == CANNON:
            for d in ORTH:
                q = s + d
                while b[q] == EMPTY:
                    if not caps_only:
                        add(s | (q << 8))
                    q += d
                if b[q] == OFFB:
                    continue
                q += d
                while b[q] == EMPTY:
                    q += d
                if enemy(b[q]):
                    add(s | (q << 8) | (F_CAP << 24))
        else:                                 # bishop, rook, queen, chariot
            dirs = DIAG if t == BISHOP else ORTH if t != QUEEN else ORTH + DIAG
            sniper = shots and t == BISHOP and c == WHITE and R.snipers
            for d in dirs:
                q = s + d
                while b[q] == EMPTY:
                    if not caps_only:
                        add(s | (q << 8))
                    q += d
                if enemy(b[q]):
                    add(s | (q << 8) | (F_CAP << 24))
                    if sniper:                # ... or shoot it and stay
                        add(s | (q << 8) | ((F_CAP | F_SNIPE) << 24))

    def gen(self, caps_only=False):
        ml = []
        b, side = self.b, self.side
        for s in SQ64:
            p = b[s]
            if p and p >> 4 == side:
                self.gen_piece(s, ml, caps_only)
        if not caps_only:
            self.gen_castles(ml)
        return ml

    def gen_castles(self, ml):
        c, b = self.side, self.b
        r0 = 0 if c == WHITE else 7
        opp = c ^ 1
        ks = SQ(4, r0)
        if b[ks] != KING | (c << 4):
            return
        kbit, qbit = (1, 2) if c == WHITE else (4, 8)
        rook = ROOK | (c << 4)
        if (self.castle & kbit and b[SQ(7, r0)] == rook and b[SQ(5, r0)] == EMPTY and b[SQ(6, r0)] == EMPTY
                and not self.attacked(ks, opp) and not self.attacked(SQ(5, r0), opp)
                and not self.attacked(SQ(6, r0), opp)):
            ml.append(MV(ks, SQ(6, r0), 0, F_CASTLE))
        if (self.castle & qbit and b[SQ(0, r0)] == rook and b[SQ(1, r0)] == EMPTY and b[SQ(2, r0)] == EMPTY
                and b[SQ(3, r0)] == EMPTY and not self.attacked(ks, opp) and not self.attacked(SQ(3, r0), opp)
                and not self.attacked(SQ(2, r0), opp)):
            ml.append(MV(ks, SQ(2, r0), 0, F_CASTLE))

    # -- make / unmake --
    def make(self, m):
        """Play m.  Returns False (move still made) if it leaves the mover in check."""
        b = self.b
        f, t, promo, fl = m & 255, (m >> 8) & 255, (m >> 16) & 255, m >> 24
        c = self.side
        p = b[f]
        h = self.hash
        self.hh.append(h)
        cap = b[t]
        if fl & F_EP:
            cs = t - (N if c == WHITE else S)
            cap = b[cs]
            h ^= ZP[cap][cs]
            b[cs] = EMPTY
        elif cap:
            h ^= ZP[cap][t]
        self.undo.append((m, p, cap, self.castle, self.ep, self.half, self.rev, self.hash))
        old_castle = self.castle
        pt = p & 15
        if fl & F_SNIPE:                     # a shot: the sniper stays, the target leaves the board
            b[t] = EMPTY
            h ^= ZCASTLE[self.castle]
            self.castle &= CASTLE_MASK[t]
            h ^= ZCASTLE[self.castle]
        else:
            h ^= ZP[p][f]
            b[f] = EMPTY
            np_ = promo | (c << 4) if promo else p
            b[t] = np_
            h ^= ZP[np_][t]
            if fl & F_CASTLE:
                r0 = RANK_[f]
                rf, rt = (SQ(7, r0), SQ(5, r0)) if FILE_[t] == 6 else (SQ(0, r0), SQ(3, r0))
                rp = b[rf]
                b[rf] = EMPTY
                b[rt] = rp
                h ^= ZP[rp][rf] ^ ZP[rp][rt]
            if pt == KING or pt == GENERAL:
                self.ksq[c] = t
            h ^= ZCASTLE[self.castle]
            self.castle &= CASTLE_MASK[f] & CASTLE_MASK[t]
            h ^= ZCASTLE[self.castle]
        if self.ep >= 0:
            h ^= ZEP[self.ep]
        self.ep = -1
        if fl & F_DOUBLE:
            self.ep = f + (N if c == WHITE else S)
            h ^= ZEP[self.ep]
        # a sideways soldier step can be undone, so it neither resets the fifty-move count nor ends
        # the repetition window (the same convention as Fairy-Stockfish)
        soldier_forward = pt == SOLDIER and FILE_[f] == FILE_[t]
        self.half = 0 if (pt == PAWN or soldier_forward or cap) else self.half + 1
        irreversible = cap or pt == PAWN or promo or self.castle != old_castle or soldier_forward
        self.rev = 0 if irreversible else self.rev + 1
        if c == BLACK:
            self.full += 1
        self.side = c ^ 1
        self.hash = h ^ ZSIDE
        return not self.attacked(self.ksq[c], c ^ 1)

    def unmake(self):
        m, p, cap, castle, ep, half, rev, h = self.undo.pop()
        self.hh.pop()
        b = self.b
        self.side ^= 1
        c = self.side
        f, t, fl = m & 255, (m >> 8) & 255, m >> 24
        b[f] = p                             # (after a shot the sniper is still there anyway)
        if fl & F_EP:
            b[t] = EMPTY
            b[t - (N if c == WHITE else S)] = cap
        else:
            b[t] = cap
        if fl & F_CASTLE:
            r0 = RANK_[f]
            rf, rt = (SQ(7, r0), SQ(5, r0)) if FILE_[t] == 6 else (SQ(0, r0), SQ(3, r0))
            b[rf] = b[rt]
            b[rt] = EMPTY
        pt = p & 15
        if pt == KING or pt == GENERAL:
            self.ksq[c] = f
        self.castle, self.ep, self.half, self.rev, self.hash = castle, ep, half, rev, h
        if c == BLACK:
            self.full -= 1

    def make_null(self):
        self.hh.append(self.hash)
        self.undo.append((0, 0, 0, self.castle, self.ep, self.half, self.rev, self.hash))
        if self.ep >= 0:
            self.hash ^= ZEP[self.ep]
        self.ep = -1
        self.half += 1
        self.rev = 0                     # never count repetitions across a null move
        self.side ^= 1
        self.hash ^= ZSIDE

    def unmake_null(self):
        _, _, _, _, ep, half, rev, h = self.undo.pop()
        self.hh.pop()
        self.side ^= 1
        self.ep, self.half, self.rev, self.hash = ep, half, rev, h

    def legal_moves(self):
        out = []
        for m in self.gen(False):
            if self.make(m):
                out.append(m)
            self.unmake()
        return out

    def repetitions(self):
        """Earlier occurrences of this position since the last irreversible move."""
        hh, h = self.hh, self.hash
        lim = min(self.rev, len(hh))
        return sum(1 for i in range(2, lim + 1, 2) if hh[-i] == h)

    def repetition_draw(self, root_size):
        """Draw by repetition as the search sees it: a repeat of a position reached inside the
        search (index >= root_size) counts at once; positions from before the search need two
        earlier occurrences, as in the game rule."""
        hh, h = self.hh, self.hash
        n = len(hh)
        cnt = 0
        for i in range(2, min(self.rev, n) + 1, 2):
            if hh[n - i] == h:
                if n - i >= root_size:
                    return True
                cnt += 1
                if cnt >= 2:
                    return True
        return False

    def has_legal_move(self):
        """Does the side to move have any legal move?  (Stops at the first one found.)"""
        b, side = self.b, self.side
        ml = []
        for s in SQ64:
            p = b[s]
            if p and p >> 4 == side:
                del ml[:]
                self.gen_piece(s, ml, False)
                for m in ml:
                    ok = self.make(m)
                    self.unmake()
                    if ok:
                        return True
        return False                     # (castling is never the only legal move)

    def only_royals(self):
        return all((self.b[s] & 15) in ROYAL for s in SQ64 if self.b[s])

    def insufficient_material(self):
        """A dead draw: only the two royals are left and they are alike (king against king, or
        general against general).  A king against a general is NOT a draw: the king can step
        diagonally next to the general, where the general cannot touch it, and it always forces
        mate (within 12 moves, see the endgame solver)."""
        return self.only_royals() and (self.b[self.ksq[0]] & 15) == (self.b[self.ksq[1]] & 15)


# -------------------------------------------------------------------- eval --
# Evaluation weights in centipawns -- identical to engine/xqchess.cpp.  The piece values (VAL)
# and the weights below were fitted to self-play results (tools/tune_eval.py); the endgame
# knowledge comes from the exact endgame solver in the C++ engine (command tb).
CENTER = [0] * 144
for _r in range(8):
    for _f in range(8):
        CENTER[SQ(_f, _r)] = int((3.5 - abs(_f - 3.5)) + (3.5 - abs(_r - 3.5)))
PAWN_ADV = [0, 0, 5, 15, 29, 54, 86, 0]
SOLD_ADV = (0, 0, 0, 5, 30, 40, 40, 25)
SOLD_ADV_PROMO = [0, 0, 0, 6, 49, 75, 139, 0]
CENW = [0, 0, 3, 1, 0, 1, 0, 0, 4, 2, 3, 0, 2, 0]
MOBW = [0, 0, 0, 3, 4, 1, 0, 0, 4, 3, 3, 2, 0, 0]
EVAL_K = 181.7      # expected score for White = 1 / (1 + exp(-score / EVAL_K)), score in centipawns
# Fairy-Stockfish's own evaluation, which it uses for snipers chess (fairy_engine.own_evaluation), has a
# scale of its own, fitted the same way to snipers-chess self-play (README, "Snipers chess")
EVAL_K_OWN = 107.0


def expected_score(cp, own=False):
    """White's expected score (win 1, draw 1/2) for an evaluation of cp centipawns from White's point of
    view; own=True for Fairy-Stockfish's own evaluation (snipers chess)."""
    return 1 / (1 + math.exp(-max(-5000, min(5000, cp)) / (EVAL_K_OWN if own else EVAL_K)))
EW = {"SEVENTH": 15, "KING_MG": 6, "KING_EG": 8, "BISHOP_PAIR": 23, "PAWN_CENTRE": 4, "TEMPO": 8,
     "GEN_MG": 9, "GEN_EG": 2, "GEN_BASE_MG": 2, "GEN_BASE_EG": -65, "OPEN_K": -7, "OPEN_G": 12,
     "MOPUP_EDGE": 15, "MOPUP_CLOSE": 6, "SCALE_NOPAWN": 48, "SCALE_XQ": 8, "FIFTY_DIV": 200,
     "TB_WIN": 1000,
     # the snipers rule: a sniper's value over an ordinary bishop (middlegame, and the endgame correction)
     "SNIPER_MG": 190, "SNIPER_EG": 0}


def tdiv(a, b):
    """Integer division rounding towards zero, as in C++."""
    q = abs(a) // b
    return q if a >= 0 else -q


def open_lines(b, s):
    """Empty squares along the four orthogonal lines from s: how exposed a royal is to checks."""
    n = 0
    for d in ORTH:
        q = s + d
        while b[q] == EMPTY:
            n += 1
            q += d
    return n


# ---------------------------------------------------------------- endgames --
# Every ending of a king against a general with at most two other pieces has been solved exactly
# (command "tb" of engine/xqchess.cpp; tools/endgame_table.py turns its results into this table).
# For each material signature the table holds the average result of the GENERAL'S side over the
# quiet positions (no capture, promotion or check to make), converted to centipawns on the
# evaluation's own scale (EVAL_K) and capped at +-TB_WIN.  With one pawn or soldier the average
# is taken separately for each rank it can stand on.  Key: see endgame_known().
# BEGIN EG_TABLE
EG_TABLE = {
    8192: -1000,  # GN vs K: 0.0%
    8224: -1000,  # GN vs KN: 0.0%
    8240: -1000,  # GN vs KB: 0.0%
    8256: -1000,  # GN vs KR: 0.0%
    8272: -1000,  # GN vs KQ: 0.0%
    8704: -40,  # GNN vs K: 44.5%
    12288: -17,  # GB vs K: 47.6%
    12320: -1000,  # GB vs KN: 0.4%
    12336: -490,  # GB vs KB: 6.3%
    12352: -1000,  # GB vs KR: 0.2%
    12368: -1000,  # GB vs KQ: 0.0%
    12800: -11,  # GBN vs K: 48.5%
    13056: -7,  # GBB vs K: 49.1%
    16384: -12,  # GR vs K: 48.4%
    16416: -21,  # GR vs KN: 47.1%
    16432: -40,  # GR vs KB: 44.5%
    16448: -1000,  # GR vs KR: 0.3%
    16464: -1000,  # GR vs KQ: 0.2%
    16896: 668,  # GRN vs K: 97.5%
    17152: 707,  # GRB vs K: 98.0%
    17408: 743,  # GRR vs K: 98.4%
    20480: 794,  # GQ vs K: 98.8%
    20512: 184,  # GQ vs KN: 73.4%
    20528: 93,  # GQ vs KB: 62.5%
    20544: 38,  # GQ vs KR: 55.2%
    20560: -502,  # GQ vs KQ: 5.9%
    20992: 835,  # GQN vs K: 99.0%
    21248: 854,  # GQB vs K: 99.1%
    21504: 865,  # GQR vs K: 99.2%
    21760: 944,  # GQQ vs K: 99.5%
    32768: -1000,  # GH vs K: 0.0%
    32800: -1000,  # GH vs KN: 0.0%
    32816: -1000,  # GH vs KB: 0.0%
    32832: -1000,  # GH vs KR: 0.0%
    32848: -1000,  # GH vs KQ: 0.0%
    33280: -47,  # GHN vs K: 43.6%
    33536: -12,  # GHB vs K: 48.4%
    33792: 646,  # GHR vs K: 97.2%
    34048: 817,  # GHQ vs K: 98.9%
    34816: -57,  # GHH vs K: 42.2%
    36864: -1000,  # GE vs K: 0.0%
    36896: -1000,  # GE vs KN: 0.0%
    36912: -1000,  # GE vs KB: 0.0%
    36928: -1000,  # GE vs KR: 0.0%
    36944: -1000,  # GE vs KQ: 0.0%
    37376: -89,  # GEN vs K: 38.0%
    37632: -13,  # GEB vs K: 48.2%
    37888: 5,  # GER vs K: 50.7%
    38144: 801,  # GEQ vs K: 98.8%
    38912: -269,  # GEH vs K: 18.5%
    39168: -521,  # GEE vs K: 5.4%
    40960: -1000,  # GA vs K: 0.0%
    40992: -1000,  # GA vs KN: 0.0%
    41008: -1000,  # GA vs KB: 0.0%
    41024: -1000,  # GA vs KR: 0.0%
    41040: -1000,  # GA vs KQ: 0.0%
    41472: -186,  # GAN vs K: 26.4%
    41728: -14,  # GAB vs K: 48.1%
    41984: -10,  # GAR vs K: 48.6%
    42240: 794,  # GAQ vs K: 98.8%
    43008: -301,  # GAH vs K: 16.0%
    43264: -732,  # GAE vs K: 1.8%
    43520: -267,  # GAA vs K: 18.7%
    45056: -12,  # GJ vs K: 48.4%
    45088: -21,  # GJ vs KN: 47.1%
    45104: -40,  # GJ vs KB: 44.5%
    45120: -1000,  # GJ vs KR: 0.3%
    45136: -1000,  # GJ vs KQ: 0.2%
    45568: 668,  # GJN vs K: 97.5%
    45824: 707,  # GJB vs K: 98.0%
    46080: 743,  # GJR vs K: 98.4%
    46336: 865,  # GJQ vs K: 99.2%
    47104: 646,  # GHJ vs K: 97.2%
    47360: 5,  # GEJ vs K: 50.7%
    47616: -10,  # GAJ vs K: 48.6%
    47872: 743,  # GJJ vs K: 98.4%
    49152: -1000,  # GC vs K: 0.0%
    49184: -1000,  # GC vs KN: 0.0%
    49200: -1000,  # GC vs KB: 0.0%
    49216: -1000,  # GC vs KR: 0.0%
    49232: -1000,  # GC vs KQ: 0.0%
    49664: -76,  # GCN vs K: 39.8%
    49920: -17,  # GCB vs K: 47.7%
    50176: 575,  # GCR vs K: 96.0%
    50432: 767,  # GCQ vs K: 98.6%
    51200: -162,  # GHC vs K: 29.1%
    51456: -170,  # GEC vs K: 28.1%
    51712: -971,  # GAC vs K: 0.5%
    51968: 575,  # GCJ vs K: 96.0%
    52224: -126,  # GCC vs K: 33.4%
    73744: -1000,  # GN vs KP, pawn on rank 7: 0.0%
    77840: -408,  # GB vs KP, pawn on rank 7: 9.6%
    81936: -15,  # GR vs KP, pawn on rank 7: 47.9%
    86032: 773,  # GQ vs KP, pawn on rank 7: 98.6%
    98320: -1000,  # GH vs KP, pawn on rank 7: 0.0%
    102416: -1000,  # GE vs KP, pawn on rank 7: 0.0%
    106512: -1000,  # GA vs KP, pawn on rank 7: 0.0%
    110608: -15,  # GJ vs KP, pawn on rank 7: 47.9%
    114704: -1000,  # GC vs KP, pawn on rank 7: 0.0%
    139280: -1000,  # GN vs KP, pawn on rank 6: 0.0%
    143376: -399,  # GB vs KP, pawn on rank 6: 10.0%
    147472: -16,  # GR vs KP, pawn on rank 6: 47.8%
    151568: 754,  # GQ vs KP, pawn on rank 6: 98.5%
    159744: -605,  # GS vs K, soldier on rank 3: 3.5%
    159776: -1000,  # GS vs KN, soldier on rank 3: 0.1%
    159792: -1000,  # GS vs KB, soldier on rank 3: 0.0%
    159808: -1000,  # GS vs KR, soldier on rank 3: 0.0%
    159824: -1000,  # GS vs KQ, soldier on rank 3: 0.0%
    160256: -69,  # GSN vs K, soldier on rank 3: 40.6%
    160512: 203,  # GSB vs K, soldier on rank 3: 75.3%
    160768: 568,  # GSR vs K, soldier on rank 3: 95.8%
    161024: 767,  # GSQ vs K, soldier on rank 3: 98.6%
    163856: -1000,  # GH vs KP, pawn on rank 6: 0.0%
    165632: -204,  # GSH vs K, soldier on rank 3: 24.5%
    167952: -1000,  # GE vs KP, pawn on rank 6: 0.0%
    169728: -337,  # GSE vs K, soldier on rank 3: 13.5%
    172048: -1000,  # GA vs KP, pawn on rank 6: 0.0%
    173824: -375,  # GSA vs K, soldier on rank 3: 11.3%
    176144: -16,  # GJ vs KP, pawn on rank 6: 47.8%
    177920: 568,  # GSJ vs K, soldier on rank 3: 95.8%
    180240: -1000,  # GC vs KP, pawn on rank 6: 0.0%
    182016: -395,  # GSC vs K, soldier on rank 3: 10.2%
    204816: -1000,  # GN vs KP, pawn on rank 5: 0.0%
    208912: -424,  # GB vs KP, pawn on rank 5: 8.8%
    213008: -24,  # GR vs KP, pawn on rank 5: 46.7%
    217104: 743,  # GQ vs KP, pawn on rank 5: 98.4%
    225280: -349,  # GS vs K, soldier on rank 4: 12.8%
    225312: -801,  # GS vs KN, soldier on rank 4: 1.2%
    225328: -1000,  # GS vs KB, soldier on rank 4: 0.1%
    225344: -1000,  # GS vs KR, soldier on rank 4: 0.0%
    225360: -1000,  # GS vs KQ, soldier on rank 4: 0.0%
    225792: 95,  # GSN vs K, soldier on rank 4: 62.8%
    226048: 341,  # GSB vs K, soldier on rank 4: 86.7%
    226304: 559,  # GSR vs K, soldier on rank 4: 95.6%
    226560: 760,  # GSQ vs K, soldier on rank 4: 98.5%
    229392: -1000,  # GH vs KP, pawn on rank 5: 0.0%
    231168: 17,  # GSH vs K, soldier on rank 4: 52.4%
    233488: -1000,  # GE vs KP, pawn on rank 5: 0.0%
    235264: -115,  # GSE vs K, soldier on rank 4: 34.6%
    237584: -1000,  # GA vs KP, pawn on rank 5: 0.0%
    239360: -162,  # GSA vs K, soldier on rank 4: 29.0%
    241680: -24,  # GJ vs KP, pawn on rank 5: 46.7%
    243456: 559,  # GSJ vs K, soldier on rank 4: 95.6%
    245776: -1000,  # GC vs KP, pawn on rank 5: 0.0%
    247552: -210,  # GSC vs K, soldier on rank 4: 23.9%
    270352: -1000,  # GN vs KP, pawn on rank 4: 0.0%
    274448: -444,  # GB vs KP, pawn on rank 4: 8.0%
    278544: -45,  # GR vs KP, pawn on rank 4: 43.8%
    282640: 735,  # GQ vs KP, pawn on rank 4: 98.3%
    290816: -157,  # GS vs K, soldier on rank 5: 29.6%
    290848: -468,  # GS vs KN, soldier on rank 5: 7.1%
    290864: -658,  # GS vs KB, soldier on rank 5: 2.6%
    290880: -1000,  # GS vs KR, soldier on rank 5: 0.0%
    290896: -1000,  # GS vs KQ, soldier on rank 5: 0.0%
    291328: 289,  # GSN vs K, soldier on rank 5: 83.1%
    291584: 522,  # GSB vs K, soldier on rank 5: 94.7%
    291840: 622,  # GSR vs K, soldier on rank 5: 96.9%
    292096: 780,  # GSQ vs K, soldier on rank 5: 98.7%
    294928: -1000,  # GH vs KP, pawn on rank 4: 0.0%
    296704: 247,  # GSH vs K, soldier on rank 5: 79.6%
    299024: -1000,  # GE vs KP, pawn on rank 4: 0.0%
    300800: 88,  # GSE vs K, soldier on rank 5: 61.8%
    303120: -1000,  # GA vs KP, pawn on rank 4: 0.0%
    304896: 32,  # GSA vs K, soldier on rank 5: 54.4%
    307216: -45,  # GJ vs KP, pawn on rank 4: 43.8%
    308992: 622,  # GSJ vs K, soldier on rank 5: 96.9%
    311312: -1000,  # GC vs KP, pawn on rank 4: 0.0%
    313088: -39,  # GSC vs K, soldier on rank 5: 44.6%
    335888: -1000,  # GN vs KP, pawn on rank 3: 0.0%
    339984: -502,  # GB vs KP, pawn on rank 3: 5.9%
    344080: -98,  # GR vs KP, pawn on rank 3: 36.9%
    348176: 539,  # GQ vs KP, pawn on rank 3: 95.1%
    356352: 23,  # GS vs K, soldier on rank 6: 53.1%
    356384: -239,  # GS vs KN, soldier on rank 6: 21.2%
    356400: -339,  # GS vs KB, soldier on rank 6: 13.4%
    356416: -1000,  # GS vs KR, soldier on rank 6: 0.1%
    356432: -1000,  # GS vs KQ, soldier on rank 6: 0.0%
    356864: 356,  # GSN vs K, soldier on rank 6: 87.6%
    357120: 552,  # GSB vs K, soldier on rank 6: 95.4%
    357376: 635,  # GSR vs K, soldier on rank 6: 97.0%
    357632: 794,  # GSQ vs K, soldier on rank 6: 98.8%
    360464: -1000,  # GH vs KP, pawn on rank 3: 0.0%
    362240: 331,  # GSH vs K, soldier on rank 6: 86.1%
    364560: -1000,  # GE vs KP, pawn on rank 3: 0.0%
    366336: 231,  # GSE vs K, soldier on rank 6: 78.0%
    368656: -1000,  # GA vs KP, pawn on rank 3: 0.0%
    370432: 175,  # GSA vs K, soldier on rank 6: 72.3%
    372752: -98,  # GJ vs KP, pawn on rank 3: 36.9%
    374528: 635,  # GSJ vs K, soldier on rank 6: 97.0%
    376848: -1000,  # GC vs KP, pawn on rank 3: 0.0%
    378624: 84,  # GSC vs K, soldier on rank 6: 61.3%
    401424: -1000,  # GN vs KP, pawn on rank 2: 0.0%
    405520: -429,  # GB vs KP, pawn on rank 2: 8.6%
    409616: -156,  # GR vs KP, pawn on rank 2: 29.8%
    413712: 303,  # GQ vs KP, pawn on rank 2: 84.1%
    421888: 272,  # GS vs K, soldier on rank 7: 81.8%
    421920: -77,  # GS vs KN, soldier on rank 7: 39.6%
    421936: -196,  # GS vs KB, soldier on rank 7: 25.4%
    421952: -1000,  # GS vs KR, soldier on rank 7: 0.3%
    421968: -1000,  # GS vs KQ, soldier on rank 7: 0.0%
    422400: 433,  # GSN vs K, soldier on rank 7: 91.5%
    422656: 588,  # GSB vs K, soldier on rank 7: 96.2%
    422912: 632,  # GSR vs K, soldier on rank 7: 97.0%
    423168: 794,  # GSQ vs K, soldier on rank 7: 98.8%
    426000: -1000,  # GH vs KP, pawn on rank 2: 0.0%
    427776: 396,  # GSH vs K, soldier on rank 7: 89.8%
    430096: -1000,  # GE vs KP, pawn on rank 2: 0.0%
    431872: 368,  # GSE vs K, soldier on rank 7: 88.3%
    434192: -1000,  # GA vs KP, pawn on rank 2: 0.0%
    435968: 339,  # GSA vs K, soldier on rank 7: 86.6%
    438288: -156,  # GJ vs KP, pawn on rank 2: 29.8%
    440064: 632,  # GSJ vs K, soldier on rank 7: 97.0%
    442384: -1000,  # GC vs KP, pawn on rank 2: 0.0%
    444160: 278,  # GSC vs K, soldier on rank 7: 82.2%
}
# END EG_TABLE


def eg_code(types):
    """At most two piece types as one number, hi * 16 + lo (hi >= lo; 0 = none)."""
    if not types:
        return 0
    if len(types) == 1:
        return types[0] * 16
    return max(types) * 16 + min(types)


def mopup(P, a):
    """Mating technique for side a: drive the other royal to the edge and into a corner, and come close."""
    d, k = P.ksq[a ^ 1], P.ksq[a]
    dist = abs(FILE_[d] - FILE_[k]) + abs(RANK_[d] - RANK_[k])
    return EW["MOPUP_EDGE"] * (7 - CENTER[d]) + EW["MOPUP_CLOSE"] * (14 - dist)


def endgame_known(P, types, mat, prr):
    """The known result of a king-against-general ending, in centipawns for the general's side,
    or None.  types: the non-royal piece types of each side; mat: their total value per side;
    prr: the rank of the only pawn/soldier, counted from its own side (0 if none).
      * A bare general always loses: a lone king mates it within 12 moves wherever the pieces
        stand (the general cannot step diagonally, so the king can stand diagonally next to it),
        and more material only helps.
      * With at most two other pieces the result comes from the solved table EG_TABLE."""
    g = WHITE if P.b[P.ksq[WHITE]] & 15 == GENERAL else BLACK
    k = g ^ 1
    if not types[g]:
        return -(EW["TB_WIN"] + tdiv(mat[k], 4) + mopup(P, k))
    if len(types[g]) + len(types[k]) > 2:
        return None
    v = EG_TABLE.get(65536 * prr + 256 * eg_code(types[g]) + eg_code(types[k]))
    if v is None:
        return None
    # the table's value, then the material as a tie-break (captures are progress) and the mating
    # technique of the side the table favours, both in proportion to how sure the result is
    return (v + tdiv((mat[g] - mat[k]) * abs(v), 8 * EW["TB_WIN"])
            + tdiv(mopup(P, g if v > 0 else k) * v, EW["TB_WIN"]))


def known_result(P):
    """endgame_known() for a position (None unless it is a king against a general with a known result)."""
    b = P.b
    if b[P.ksq[WHITE]] & 15 == b[P.ksq[BLACK]] & 15:
        return None
    types, mat, prr, npw = [[], []], [0, 0], 0, 0
    for s in SQ64:
        p = b[s]
        if p and (p & 15) not in ROYAL:
            t, c = p & 15, p >> 4
            types[c].append(t)
            mat[c] += VAL[t]
            if t == PAWN or t == SOLDIER:
                npw += 1
                prr = RANK_[s] if c == WHITE else 7 - RANK_[s]
    if npw > 1:
        g = WHITE if b[P.ksq[WHITE]] & 15 == GENERAL else BLACK
        return endgame_known(P, types, mat, 0) if not types[g] else None
    return endgame_known(P, types, mat, prr)


def can_mate_bare(cnt, royal):
    """Can a side with these piece counts force mate against a lone king/general?  (From the
    exact endgame solver: a lone king already mates a lone general, but a general needs a queen,
    or a chariot or rook together with another chariot or rook, a cannon, a horse, a knight or a
    bishop, to mate a lone king; soldiers may promote.)"""
    if royal == KING:
        return any(cnt[t] for t in range(1, NTYPES) if t not in ROYAL)
    if cnt[QUEEN]:
        return True
    j = cnt[CHARIOT] + cnt[ROOK]
    ch = cnt[CANNON] + cnt[HORSE] + cnt[KNIGHT] + cnt[BISHOP]
    if j >= 2 or (j >= 1 and ch >= 1):
        return True
    return bool(cnt[SOLDIER] and R.soldier_promo)


def evaluate(P):
    b = P.b
    sc = [0, 0]
    nonpawn = 0
    bishops = [0, 0]
    cnt = [[0] * NTYPES, [0] * NTYPES]
    piece_mat = [0, 0]
    eg_adj = [0, 0]
    pawns = [0, 0]
    nonroyal = [0, 0]
    pieces = []
    types, mat, prr = [[], []], [0, 0], 0
    for s in SQ64:
        p = b[s]
        if not p:
            continue
        t, c = p & 15, p >> 4
        cnt[c][t] += 1
        sc[c] += VAL[t]
        eg_adj[c] += VAL_EG[t]
        if t == BISHOP and c == WHITE and R.snipers:      # a sniper is worth more than a bishop
            sc[c] += EW["SNIPER_MG"]
            eg_adj[c] += EW["SNIPER_EG"]
        pieces.append((s, t, c))
        if t == KING or t == GENERAL:
            continue
        nonroyal[c] += 1
        types[c].append(t)
        mat[c] += VAL[t]
        if t == PAWN or t == SOLDIER:
            pawns[c] += 1
            prr = RANK_[s] if c == WHITE else 7 - RANK_[s]
        else:
            nonpawn += VAL[t]
            piece_mat[c] += VAL[t]
        if t == BISHOP:
            bishops[c] += 1
    g = WHITE if b[P.ksq[WHITE]] & 15 == GENERAL else BLACK
    if b[P.ksq[g]] & 15 == GENERAL and b[P.ksq[g ^ 1]] & 15 == KING:     # a king against a general
        npw = pawns[0] + pawns[1]
        known = endgame_known(P, types, mat, prr if npw == 1 else 0) if npw <= 1 or not types[g] else None
        if known is not None:
            e = known if g == WHITE else -known
            e = tdiv(e * (EW["FIFTY_DIV"] - min(P.half, 100)), EW["FIFTY_DIV"])
            return (e if P.side == WHITE else -e) + EW["TEMPO"]
    mop = 0
    for c in (WHITE, BLACK):                         # bare king/general: dead draw, or drive it to the edge
        if nonroyal[c ^ 1] or not nonroyal[c]:
            continue
        if not can_mate_bare(cnt[c], b[P.ksq[c]] & 15):
            return 0
        bonus = mopup(P, c)
        mop += bonus if c == WHITE else -bonus
    mg = min(nonpawn, 5000)
    sc[0] += tdiv(eg_adj[0] * (5000 - mg), 5000)
    sc[1] += tdiv(eg_adj[1] * (5000 - mg), 5000)
    ml = []
    for s, t, c in pieces:
        rr = RANK_[s] if c == WHITE else 7 - RANK_[s]
        cen = CENTER[s]
        if t == PAWN:
            v = PAWN_ADV[rr] + (EW["PAWN_CENTRE"] if FILE_[s] in (3, 4) else 0)
        elif t == SOLDIER:
            v = SOLD_ADV_PROMO[rr] if R.soldier_promo else SOLD_ADV[rr]
        elif t == ROOK or t == CHARIOT:
            v = EW["SEVENTH"] if rr == 6 else 0
        elif t == KING:
            v = tdiv(mg * (-EW["KING_MG"] * rr) + (5000 - mg) * (EW["KING_EG"] * cen), 5000) - EW["OPEN_K"] * open_lines(b, s)
        elif t == GENERAL:
            # the general is a weaker royal than the king (it cannot step diagonally, so it is
            # easily checked, chased and mated): its own terms, including open lines around it
            v = tdiv(mg * (EW["GEN_BASE_MG"] - EW["GEN_MG"] * rr) + (5000 - mg) * (EW["GEN_BASE_EG"] + EW["GEN_EG"] * cen),
                     5000) - EW["OPEN_G"] * open_lines(b, s)
        else:
            v = CENW[t] * cen
        w = MOBW[t]
        if w:
            del ml[:]
            P.gen_piece(s, ml, False, False)             # (a sniper's shots are not extra mobility)
            v += w * len(ml)
        sc[c] += v
    if bishops[0] >= 2:
        sc[0] += EW["BISHOP_PAIR"]
    if bishops[1] >= 2:
        sc[1] += EW["BISHOP_PAIR"]
    e = sc[0] - sc[1] + mop
    strong = WHITE if e >= 0 else BLACK
    scale = 128
    if not pawns[strong] and not mop:
        if piece_mat[strong] - piece_mat[strong ^ 1] < VAL[ROOK]:
            scale = EW["SCALE_NOPAWN"]
        if b[P.ksq[strong]] & 15 == GENERAL and not can_mate_bare(cnt[strong], GENERAL):
            scale = min(scale, EW["SCALE_XQ"])
    e = tdiv(e * scale, 128)
    e = tdiv(e * (EW["FIFTY_DIV"] - min(P.half, 100)), EW["FIFTY_DIV"])
    return (e if P.side == WHITE else -e) + EW["TEMPO"]


# ------------------------------------------------------------------ search --
INF, MATE, MAXPLY = 32000, 31000, 64
TT_EXACT, TT_LOWER, TT_UPPER = 1, 2, 3
TT = {}
TT_MAX = 1_000_000


def is_mate(s):
    return abs(s) > MATE - 2 * MAXPLY


class Stop(Exception):
    pass


class Searcher:
    def __init__(self, P, node_limit=0, time_limit_ms=0):
        self.P = P
        self.nodes = 0
        self.node_limit = node_limit
        self.deadline = time.perf_counter() + time_limit_ms / 1000 if time_limit_ms else 0
        self.can_stop = False
        self.killers = [[0, 0] for _ in range(MAXPLY + 2)]
        self.hist = {}
        self.pv = [[] for _ in range(MAXPLY + 2)]
        self.root_exclude = set()
        self.t0 = time.perf_counter()
        self.root_size = len(P.hh)       # history length at the root of the search
        self.abs_deadline = 0            # wall-clock deadline (time.time()), used by parallel workers
        self.ext_stop = None             # an Event that ends the search when set ('stop' command)

    def tick(self):
        self.nodes += 1
        if self.can_stop and (self.nodes & 511) == 0:
            if (self.node_limit and self.nodes >= self.node_limit) or \
                    (self.deadline and time.perf_counter() >= self.deadline) or \
                    (self.abs_deadline and time.time() >= self.abs_deadline) or \
                    (self.ext_stop is not None and self.ext_stop.is_set()):
                raise Stop

    def order(self, ml, tt_move, ply):
        b = self.P.b
        kl = self.killers[ply]
        hist = self.hist

        def key(m):
            if m == tt_move:
                return 1 << 30
            fl = m >> 24
            if fl & F_CAP:
                victim = PAWN if fl & F_EP else b[(m >> 8) & 255] & 15
                if fl & F_SNIPE:              # a shot risks nothing: before any capture of the same piece
                    return (1 << 28) + 16 * VAL[victim]
                return (1 << 28) + 16 * VAL[victim] - VAL[b[m & 255] & 15] // 8
            promo = (m >> 16) & 255
            if promo:
                return (1 << 28) + VAL[promo]
            if m == kl[0]:
                return (1 << 27) + 2
            if m == kl[1]:
                return (1 << 27) + 1
            return hist.get((b[m & 255], (m >> 8) & 255), 0)

        ml.sort(key=key, reverse=True)

    def qsearch(self, alpha, beta, ply):
        self.tick()
        P = self.P
        if P.half >= 100 or P.repetition_draw(self.root_size) or P.insufficient_material():
            return 0
        if ply >= MAXPLY:
            return evaluate(P)
        in_c = P.in_check(P.side)
        if not in_c and not P.has_legal_move():          # stalemate
            return -MATE + ply if R.stalemate_loss else 0
        best = -INF
        if not in_c:
            stand = evaluate(P)
            if stand >= beta:
                return stand
            if stand > alpha:
                alpha = stand
            best = stand
        ml = P.gen(not in_c)
        self.order(ml, 0, ply)
        legal = 0
        for m in ml:
            if not P.make(m):
                P.unmake()
                continue
            legal += 1
            s = -self.qsearch(-beta, -alpha, ply + 1)
            P.unmake()
            if s > best:
                best = s
                if s > alpha:
                    alpha = s
                    if s >= beta:
                        break
        if in_c and legal == 0:
            return -MATE + ply
        return best

    def has_pieces(self, c):
        b = self.P.b
        for s in SQ64:
            p = b[s]
            if p and p >> 4 == c and (p & 15) not in (PAWN, SOLDIER, KING, GENERAL):
                return True
        return False

    def search(self, depth, alpha, beta, ply, do_null):
        P = self.P
        self.pv[ply] = []
        pv_node = beta - alpha > 1
        if ply > 0:
            if P.half >= 100 or P.repetition_draw(self.root_size) or P.insufficient_material():
                return 0
            alpha = max(alpha, -MATE + ply)
            beta = min(beta, MATE - ply - 1)
            if alpha >= beta:
                return alpha
        if ply >= MAXPLY:
            return evaluate(P)
        in_c = P.in_check(P.side)
        if in_c:
            depth += 1
        if depth <= 0:
            return self.qsearch(alpha, beta, ply)
        self.tick()

        e = TT.get(P.hash)
        tt_move = 0
        if e is not None:
            e_depth, e_flag, e_score, tt_move = e
            if ply > 0 and not pv_node and e_depth >= depth:
                s = e_score
                if s > MATE - 2 * MAXPLY:
                    s -= ply
                elif s < -MATE + 2 * MAXPLY:
                    s += ply
                if e_flag == TT_EXACT or (e_flag == TT_LOWER and s >= beta) or (e_flag == TT_UPPER and s <= alpha):
                    return s

        if (not in_c and not pv_node and do_null and depth >= 3 and ply > 0 and self.has_pieces(P.side)
                and evaluate(P) >= beta):
            rn = 2 + depth // 6
            P.make_null()
            s = -self.search(depth - 1 - rn, -beta, -beta + 1, ply + 1, False)
            P.unmake_null()
            if s >= beta:
                return beta if is_mate(s) else s

        ml = P.gen(False)
        self.order(ml, tt_move, ply)
        legal = 0
        best = -INF
        best_move = 0
        orig_alpha = alpha
        for m in ml:
            if ply == 0 and m in self.root_exclude:
                continue
            quiet = not (m >> 24) & F_CAP and not (m >> 16) & 255
            if not P.make(m):
                P.unmake()
                continue
            legal += 1
            gives_check = P.in_check(P.side)
            if legal == 1:
                s = -self.search(depth - 1, -beta, -alpha, ply + 1, True)
            else:
                red = 0
                if depth >= 3 and legal > 3 and quiet and not in_c and not gives_check:
                    red = 1 + (legal > 10) + (depth > 7)
                s = -self.search(depth - 1 - red, -alpha - 1, -alpha, ply + 1, True)
                if s > alpha and red:
                    s = -self.search(depth - 1, -alpha - 1, -alpha, ply + 1, True)
                if alpha < s < beta:
                    s = -self.search(depth - 1, -beta, -alpha, ply + 1, True)
            P.unmake()
            if s > best:
                best, best_move = s, m
                if s > alpha:
                    alpha = s
                    self.pv[ply] = [m] + self.pv[ply + 1]
                    if s >= beta:
                        if quiet:
                            kl = self.killers[ply]
                            if kl[0] != m:
                                kl[1], kl[0] = kl[0], m
                            key = (P.b[m & 255], (m >> 8) & 255)
                            self.hist[key] = self.hist.get(key, 0) + depth * depth
                        break
        if legal == 0:
            if ply == 0 and self.root_exclude:
                return -INF
            if in_c or R.stalemate_loss:
                return -MATE + ply
            return 0
        st = best
        if st > MATE - 2 * MAXPLY:
            st += ply
        elif st < -MATE + 2 * MAXPLY:
            st -= ply
        if len(TT) > TT_MAX:
            TT.clear()
        TT[P.hash] = (depth, TT_LOWER if best >= beta else TT_EXACT if best > orig_alpha else TT_UPPER,
                      st, best_move)
        return best


def score_str(s):
    if is_mate(s):
        return f"mate {(MATE - s + 1) // 2}" if s > 0 else f"mate {-((MATE + s) // 2)}"
    return f"cp {s}"


def think(P, depth=64, nodes=0, movetime=0, multipv=1, verbose=True, out=sys.stdout, threads=1):
    """Iterative deepening.  Returns [(move, score, pv)] for the deepest completed depth, best first.
    threads > 1 searches the root moves in parallel worker processes (not with a node limit)."""
    if threads > 1 and not nodes and len(P.legal_moves()) > 1:
        try:
            return think_parallel(P, threads, depth, movetime, multipv, verbose, out)
        except Exception as e:                    # e.g. processes not allowed: fall back to one core
            out.write(f"info string parallel search unavailable ({e}); using one core\n")
    S = Searcher(P, nodes, movetime)
    S.ext_stop = STOP
    done = []
    undo_len = len(P.undo)
    for d in range(1, min(depth, MAXPLY - 4) + 1):
        S.can_stop = d > 1
        cur = []
        S.root_exclude = set()
        try:
            for _ in range(multipv):
                s = S.search(d, -INF, INF, 0, False)
                if s == -INF or not S.pv[0]:
                    break
                cur.append((S.pv[0][0], s, list(S.pv[0])))
                S.root_exclude.add(S.pv[0][0])
        except Stop:
            # a Stop leaves the moves of the interrupted search on the board: take them back
            while len(P.undo) > undo_len:
                if P.undo[-1][0]:
                    P.unmake()
                else:
                    P.unmake_null()
            break
        if not cur:
            break
        cur.sort(key=lambda x: -x[1])
        done = cur
        if verbose:
            ms = int((time.perf_counter() - S.t0) * 1000)
            for k, (m, s, pv) in enumerate(done, 1):
                out.write(f"info depth {d} multipv {k} score {score_str(s)} nodes {S.nodes} time {ms} "
                          f"nps {S.nodes * 1000 // ms if ms else 0} pv {' '.join(move_str(x) for x in pv)}\n")
            out.flush()
        if multipv == 1 and is_mate(done[0][1]) and d > 2 * (MATE - abs(done[0][1])) + 2:
            break
    return done


# ------------------------------------------------------- parallel search --
# Root splitting: every root move is searched in a pool of worker processes (Python threads
# cannot run Python code in parallel).  Moves that might be in the top `multipv` get an exact
# score; the others are searched with a raised lower bound, so a quick fail-low proves they are
# worse.  Each worker keeps its own transposition table between searches.
STOP = threading.Event()           # set by the 'stop' command to end the current search early
_pool = None
_pool_size = 0
_mp_stop = None
_W_STOP = None                     # the pool's shared stop event, inside a worker


def _worker_init(ev):
    global _W_STOP
    _W_STOP = ev


def _rules_snapshot():
    return (R.horse_block, R.elephant_eye, R.stalemate_loss, R.soldier_sideways, R.soldier_promo, R.snipers,
            tuple(VAL))


_W_HEUR = {"fen": None, "killers": None, "hist": None}


def _root_task(args):
    snap, fen, hh, m, depth, alpha, beta, deadline, key, reduce = args
    if snap != _rules_snapshot():
        R.horse_block, R.elephant_eye, R.stalemate_loss, R.soldier_sideways, R.soldier_promo, R.snipers, vals = snap
        VAL[:] = vals
        TT.clear()
    P = Pos(fen)
    P.hh = list(hh)
    S = Searcher(P)
    if _W_HEUR["fen"] == fen:                     # keep move-ordering statistics for this root position
        S.killers, S.hist = _W_HEUR["killers"], _W_HEUR["hist"]
    else:
        _W_HEUR.update(fen=fen, killers=S.killers, hist=S.hist)
    P.make(m)
    S.abs_deadline, S.ext_stop, S.can_stop = deadline, _W_STOP, True
    try:
        if alpha > -INF:
            # scout: a null-window search (reduced for late quiet moves) proves most moves are no better
            s = -S.search(depth - 1 - reduce, -alpha - 1, -alpha, 1, True)
            if s <= alpha:
                return key, m, s, [], S.nodes
        s = -S.search(depth - 1, -beta, -alpha, 1, True)
    except Stop:
        return key, m, None, [], S.nodes
    return key, m, s, [m] + S.pv[1], S.nodes


def _get_pool(n):
    global _pool, _pool_size, _mp_stop
    if _pool is None or _pool_size != n:
        if _pool is not None:
            _pool.terminate()
        import multiprocessing as mp
        ctx = mp.get_context("spawn")             # behaves the same on Windows, macOS and Linux
        _mp_stop = ctx.Event()
        _pool = ctx.Pool(n, initializer=_worker_init, initargs=(_mp_stop,))
        _pool_size = n
    return _pool


def close_pool():
    global _pool
    if _pool is not None:
        _pool.terminate()
        _pool = None


def think_parallel(P, threads, depth=64, movetime=0, multipv=1, verbose=True, out=sys.stdout):
    import queue
    t0 = time.perf_counter()
    deadline = time.time() + movetime / 1000 if movetime else 0
    pool = _get_pool(threads)
    _mp_stop.clear()
    root = P.legal_moves()
    K = min(multipv, len(root))
    fen, snap = P.fen(), _rules_snapshot()
    hh = tuple(P.hh[-P.half:]) if P.half else ()
    results = queue.Queue()
    prev, done, total_nodes, key = {}, [], 0, 0
    for d in range(1, min(depth, MAXPLY - 4) + 1):
        key += 1
        order = sorted(root, key=lambda m: -prev.get(m, -INF))
        prevk = sorted(prev.values(), reverse=True)[K - 1] if len(prev) >= K else None
        exact, upper, used_lb = {}, {}, {}
        pending = [(m, -INF if i < K else None) for i, m in enumerate(order)]
        aborted = False
        while pending:
            inflight = 0
            while pending or inflight:
                while pending and inflight < threads and not aborted:
                    m, lb = pending.pop(0)
                    if lb is None:                  # raised lower bound for probably-worse moves
                        ex = sorted((v[0] for v in exact.values()), reverse=True)
                        lb = ex[K - 1] if len(ex) >= K else (prevk - 50 if prevk is not None else -INF)
                    used_lb[m] = lb
                    quiet = not (m >> 24) & F_CAP and not (m >> 16) & 255
                    idx = order.index(m)
                    red = (1 + (idx > 10) + (d > 7)) if (d >= 3 and idx > 3 and quiet and lb > -INF) else 0
                    pool.apply_async(_root_task, ((snap, fen, hh, m, d, lb, INF, deadline, key, red),),
                                     callback=results.put, error_callback=lambda e: results.put(("error", e)))
                    inflight += 1
                if aborted:
                    pending = []
                if not inflight:
                    break
                try:
                    r = results.get(timeout=0.05)
                except queue.Empty:
                    if STOP.is_set() and not aborted:
                        _mp_stop.set()
                        aborted = True
                    continue
                if r[0] == "error":
                    raise r[1]
                k, m, sc, pv, n = r
                inflight -= 1
                total_nodes += n
                if k != key:
                    continue
                if sc is None:
                    aborted = True
                    _mp_stop.set()
                elif sc > used_lb[m]:
                    exact[m] = (sc, pv)
                    upper.pop(m, None)
                else:
                    upper[m] = sc
            if aborted:
                break
            # moves that failed low against a guessed bound may still belong in the top K
            ex = sorted((v[0] for v in exact.values()), reverse=True)
            kth = ex[K - 1] if len(ex) >= K else None
            pending = [(m, kth if kth is not None else -INF) for m, b in upper.items()
                       if kth is None or b > kth]
            for m, _ in pending:
                upper.pop(m)
        if aborted or len(exact) < 1:
            break
        prev = {m: v[0] for m, v in exact.items()}
        prev.update(upper)
        done = sorted(((m, v[0], v[1]) for m, v in exact.items()), key=lambda x: -x[1])[:K]
        if verbose:
            ms = int((time.perf_counter() - t0) * 1000)
            for k, (m, sc, pv) in enumerate(done, 1):
                out.write(f"info depth {d} multipv {k} score {score_str(sc)} nodes {total_nodes} time {ms} "
                          f"nps {total_nodes * 1000 // ms if ms else 0} pv {' '.join(move_str(x) for x in pv)}\n")
            out.flush()
        if multipv == 1 and is_mate(done[0][1]) and d > 2 * (MATE - abs(done[0][1])) + 2:
            break
        if deadline and time.time() >= deadline:
            break
    _mp_stop.set()                               # make sure no worker keeps searching
    return done


def perft(P, d):
    if d == 0:
        return 1
    n = 0
    for m in P.gen(False):
        if P.make(m):
            n += perft(P, d - 1)
        P.unmake()
    return n


def parse_move(P, s):
    for m in P.legal_moves():
        if move_str(m) == s:
            return m
    return None


def game_over(P):
    """Returns (reason, result) or ("", "") while the game goes on."""
    if not P.legal_moves():
        mate = P.in_check(P.side)
        if mate or R.stalemate_loss:
            return ("checkmate" if mate else "stalemate"), ("0-1" if P.side == WHITE else "1-0")
        return "stalemate", "1/2-1/2"
    if P.repetitions() >= 2:
        return "repetition", "1/2-1/2"
    if P.half >= 100:
        return "fifty-move", "1/2-1/2"
    if P.insufficient_material():
        return "insufficient", "1/2-1/2"
    return "", ""


def selfplay(args, out=sys.stdout):
    opts = {"games": 1, "nodes": 20000, "depth": 64, "randomplies": 4, "seed": 1, "maxplies": 300,
            "resign": 0, "samplevery": 2, "randmargin": 100, "randdepth": 4}
    fen = START_FEN
    i = 0
    while i < len(args):
        if args[i] == "fen":
            fen = " ".join(args[i + 1:])
            break
        if args[i] in opts and i + 1 < len(args):
            try:
                opts[args[i]] = int(args[i + 1])
            except ValueError:
                pass
        i += 2
    # keep every setting in a usable range
    lo = {"games": 0, "nodes": 0, "depth": 1, "randomplies": 0, "maxplies": 1, "resign": 0,
          "samplevery": 0, "randdepth": 1}
    for k, v in lo.items():
        opts[k] = max(v, opts[k])
    opts["depth"] = min(opts["depth"], MAXPLY - 4)
    opts["randdepth"] = min(opts["randdepth"], MAXPLY - 4)
    P = Pos()
    if not P.set_fen(fen):
        out.write('{"error":"bad fen"}\n')
        out.flush()
        return
    rng = random.Random(opts["seed"])
    for g in range(opts["games"]):
        if STOP.is_set():
            return
        P = Pos(fen)
        TT.clear()
        moves, samples = [], []
        bad = [0, 0]
        ply = 0
        while True:
            if STOP.is_set():                  # 'stop': abandon the game in progress
                return
            reason, result = game_over(P)
            if reason:
                break
            if ply >= opts["maxplies"]:
                reason, result = "maxplies", "1/2-1/2"
                break
            if ply < opts["randomplies"]:
                legal = P.legal_moves()
                if opts["randmargin"] < 0:
                    m = rng.choice(legal)
                else:
                    lines = think(P, depth=opts["randdepth"], multipv=len(legal), verbose=False)
                    ok = [ln[0] for ln in lines if ln[1] >= lines[0][1] - opts["randmargin"]]
                    m = rng.choice(ok or legal)
            else:
                lines = think(P, depth=opts["depth"], nodes=opts["nodes"], verbose=False)
                if not lines:                  # only if the search was stopped at once
                    if STOP.is_set():
                        return
                    lines = [(P.legal_moves()[0], 0, [])]
                m, score = lines[0][0], lines[0][1]
                wscore = score if P.side == WHITE else -score
                quiet = not (m >> 24) & F_CAP and not (m >> 16) & 255 and not P.in_check(P.side)
                if quiet and not is_mate(score) and opts["samplevery"] > 0 and ply % opts["samplevery"] == 0:
                    cnt = [[0] * NTYPES for _ in range(2)]
                    for s in SQ64:
                        if P.b[s]:
                            cnt[P.b[s] >> 4][P.b[s] & 15] += 1
                    samples.append([ply, wscore] + cnt[0][1:] + cnt[1][1:])
                if opts["resign"] > 0:
                    bad[P.side] = bad[P.side] + 1 if score <= -opts["resign"] else 0
                    if bad[P.side] >= 3:
                        reason, result = "adjudicated", ("0-1" if P.side == WHITE else "1-0")
                        break
            moves.append(move_str(m))
            P.make(m)
            ply += 1
        import json
        out.write(json.dumps({"game": g, "result": result, "reason": reason, "plies": ply, "start": fen,
                              "final": P.fen(), "moves": " ".join(moves), "samples": samples},
                             separators=(",", ":")) + "\n")
        out.flush()


# -------------------------------------------------------------------- main --
def parse_bool(v):
    return v.lower() in ("true", "1", "on", "yes")


def main():
    P = Pos()
    out = sys.stdout
    opts = {"threads": 1}
    worker = [None]

    def wait():
        if worker[0] is not None and worker[0].is_alive():
            worker[0].join()

    def run_go(kw):
        try:
            lines = think(P, threads=opts["threads"], **kw)
            out.write(f"bestmove {move_str(lines[0][0]) if lines else '0000'}\n")
        except Exception as e:
            out.write(f"info string search error: {e}\nbestmove 0000\n")
        out.flush()

    for line in sys.stdin:
        tok = line.split()
        if not tok:
            continue
        cmd = tok[0]
        if cmd == "stop":                     # end the running search early (it still prints bestmove)
            STOP.set()
            continue
        if cmd == "isready":
            out.write("readyok\n")
            out.flush()
            continue
        if cmd == "quit":                     # finishes a running search first (send 'stop' to cut it short)
            wait()
            break
        wait()                                # everything else waits for a running search to finish
        if cmd == "position":
            # build the new position on the side; the current one only changes if it all parses
            rest = tok[1:]
            NP = Pos()
            if rest and rest[0] == "fen":
                mi = rest.index("moves") if "moves" in rest else len(rest)
                if not NP.set_fen(" ".join(rest[1:mi])):
                    out.write("error bad fen\n")
                    out.flush()
                    continue
                mvs = rest[mi + 1:]
            elif rest and rest[0] == "startpos":
                mvs = rest[2:] if len(rest) > 1 and rest[1] == "moves" else []
            else:
                out.write("error position needs startpos or fen\n")
                out.flush()
                continue
            ok = True
            for mv in mvs:
                m = parse_move(NP, mv)
                if m is None:
                    out.write(f"error illegal move {mv}\n")
                    ok = False
                    break
                NP.make(m)
            if ok:
                P = NP
            out.flush()
        elif cmd == "go":
            kw = {"depth": 64, "nodes": 0, "movetime": 0, "multipv": 1}
            given = False
            for i in range(1, len(tok) - 1):
                if tok[i] in kw:
                    try:
                        kw[tok[i]] = int(tok[i + 1])
                        given = given or tok[i] != "multipv"
                    except ValueError:
                        pass
            if not given:
                kw["movetime"] = 2000
            kw["depth"] = max(1, min(kw["depth"], MAXPLY - 4))
            kw["multipv"] = max(1, min(kw["multipv"], 256))
            kw["nodes"], kw["movetime"] = max(0, kw["nodes"]), max(0, kw["movetime"])
            reason, result = game_over(P)
            if reason:
                out.write(f"info gameover {result} {reason}\nbestmove 0000\n")
                out.flush()
                continue
            STOP.clear()
            worker[0] = threading.Thread(target=run_go, args=(kw,), daemon=True)
            worker[0].start()
        elif cmd == "legal":
            out.write("legal " + " ".join(sorted(move_str(m) for m in P.legal_moves())) + "\n")
            out.flush()
        elif cmd in ("perft", "divide"):
            d = int(tok[1]) if len(tok) > 1 and tok[1].isdigit() else 1
            d = min(d, 8)
            t0 = time.perf_counter()
            if cmd == "divide":
                tot = 0
                for m in P.legal_moves():
                    P.make(m)
                    c = perft(P, d - 1)
                    P.unmake()
                    tot += c
                    out.write(f"{move_str(m)} {c}\n")
            else:
                tot = perft(P, d)
            out.write(f"perft {d} {tot} ({time.perf_counter() - t0:.2f}s)\n")
            out.flush()
        elif cmd == "eval":
            out.write(f"eval {evaluate(P)}\n")
            out.flush()
        elif cmd == "d":
            out.write(P.board_text() + "\n")
            out.flush()
        elif cmd == "fen":
            out.write(f"fen {P.fen()}\n")
            out.flush()
        elif cmd == "setoption":
            name, v = (tok[1:3] + ["", ""])[:2]
            if name == "HorseBlock":
                R.horse_block = parse_bool(v)
            elif name == "ElephantEye":
                R.elephant_eye = parse_bool(v)
            elif name == "StalemateLoss":
                R.stalemate_loss = parse_bool(v)
            elif name == "SoldierSideways":
                R.soldier_sideways = parse_bool(v)
            elif name == "Snipers":
                R.snipers = parse_bool(v)
            elif name == "SoldierPromotion":
                modes = {"none": 0, "false": 0, "xiangqi": 1, "western": 2, "any": 3, "true": 3}
                if v in modes:
                    R.soldier_promo = modes[v]
                else:
                    out.write("error SoldierPromotion must be any, xiangqi, western or none\n")
            elif name == "Value":
                t = PCHARS.find(v.upper()) if len(v) == 1 else -1
                if t <= 0:
                    out.write(f"error unknown piece {v}\n")
                elif len(tok) < 4 or not tok[3].isdigit() or int(tok[3]) > 20000:
                    out.write(f"error bad value for {v}\n")
                else:
                    VAL[t] = int(tok[3])
            elif name == "Hash":
                pass
            elif name == "Threads":
                opts["threads"] = max(1, min(256, int(v))) if v.isdigit() else opts["threads"]
            else:
                out.write(f"error unknown option {name}\n")
            TT.clear()
            out.flush()
        elif cmd == "selfplay":               # runs in the background; 'stop' ends it
            STOP.clear()
            worker[0] = threading.Thread(target=selfplay, args=(tok[1:],), daemon=True)
            worker[0].start()
        elif cmd == "isready":
            out.write("readyok\n")
            out.flush()
        else:
            out.write(f"error unknown command {cmd}\n")
            out.flush()


if __name__ == "__main__":
    try:
        main()
    finally:
        close_pool()
