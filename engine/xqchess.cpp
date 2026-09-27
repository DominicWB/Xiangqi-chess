// xqchess.cpp -- search engine for "Western chess army vs Xiangqi army" on an 8x8 board.
//
// Either colour may field either army; the piece letters decide.  FEN letters
// (upper case = White, lower case = Black):
//   Western : P pawn, N knight, B bishop, R rook, Q queen, K king
//   Xiangqi : S soldier, H horse, E elephant, A advisor, J chariot (ju), C cannon, G general
//
// Xiangqi pieces on the 8x8 board (defaults; see setoption below):
//   General  one step orthogonally (no palace).
//   Advisor  one step diagonally (no palace).
//   Elephant exactly two steps diagonally, jumping; with ElephantEye=true it is
//            blocked if the intermediate square is occupied (off by default).
//   Horse    one step orthogonally then one step diagonally outward; blocked
//            ("hobbled") if the orthogonally adjacent square is occupied (HorseBlock=true).
//   Chariot  moves like a rook.
//   Cannon   moves like a rook without capturing; captures by jumping exactly one
//            piece (the screen) of either colour along a rank or file.
//   Soldier  moves and captures one step forward; once it has reached its fifth rank
//            (ranks 5-8 for White, 1-4 for Black) it may also move/capture sideways
//            (SoldierSideways=true).  On reaching the last rank it must promote, like a
//            pawn: SoldierPromotion=any (default) -> any piece except a king/general
//            (queen, chariot, rook, cannon, horse, knight, bishop, elephant, advisor);
//            =xiangqi -> chariot, cannon, horse, elephant or advisor; =western -> queen,
//            rook, bishop or knight; =none -> no promotion (it then stays on the last
//            rank and can only move sideways).
// Western pieces follow normal chess rules (castling, en passant, promotion to Q/R/B/N).
// Snipers (setoption Snipers true): White's bishops can also take the first enemy piece along a
// diagonal without moving (a shot, written c1h6s); c1h6 is still the ordinary capture.
// Game ends: checkmate; stalemate (draw, or loss for the stalemated side with
// StalemateLoss=true); threefold repetition; 50-move rule; insufficient material, which is only
// two alike royals (king against king, general against general).  A king against a lone general
// is not a draw: the king always wins.
//
// Commands on stdin (one per line):
//   position startpos | fen <FEN> [moves m1 m2 ...]
//   go [depth D] [nodes N] [movetime MS] [multipv K]   (searches in the background)
//   stop                 end the running search now (it still prints bestmove)
//   legal                list legal moves in the current position
//   perft D | divide D   move-generation counts
//   eval                 static evaluation (centipawns, side to move)
//   d                    print the board
//   setoption <Name> <value>      HorseBlock, ElephantEye, StalemateLoss, SoldierSideways, Snipers (true/false),
//                                 SoldierPromotion (any/xiangqi/western/none), Hash (MB), Threads (cores), Value <letter> <cp>
//   selfplay games G [nodes N] [depth D] [randomplies R] [randmargin CP] [randdepth D]
//            [seed S] [maxplies M] [resign CP] [samplevery K] [fen <FEN>]   one JSON line per game
//   tb <White's pieces> <Black's pieces>   solve an ending of at most 4 pieces exactly, e.g. tb GA k
//   tbprobe              exact result of the current position (at most 4 pieces)
//   quit
// Moves are written in coordinate form: e2e4, b8c6, e7e8q.

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <iterator>
#include <map>
#include <memory>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

using namespace std;
typedef uint64_t U64;

// ---------------------------------------------------------------- pieces ----
enum { NONE = 0, PAWN, KNIGHT, BISHOP, ROOK, QUEEN, KING,
       SOLDIER, HORSE, ELEPHANT, ADVISOR, CHARIOT, CANNON, GENERAL, NTYPES };
const int WHITE = 0, BLACK = 1;
const int EMPTY = 0, OFFB = 64;            // piece code = type + 16*colour
const char *PCHARS = ".PNBRQKSHEAJCG";

inline int mk(int c, int t) { return t | (c << 4); }
inline int ptype(int p) { return p & 15; }
inline int pcolor(int p) { return p >> 4; }
inline bool isPiece(int p) { return p != EMPTY && p != OFFB; }
inline bool isRoyal(int t) { return t == KING || t == GENERAL; }

// 12x12 mailbox with a 2-square border.
inline int SQ(int f, int r) { return (r + 2) * 12 + f + 2; }
inline int FILE_(int s) { return s % 12 - 2; }
inline int RANK_(int s) { return s / 12 - 2; }
const int N = 12, S = -12, E = 1, W = -1;
const int ORTH[4] = {N, S, E, W};
const int DIAG[4] = {N + E, N + W, S + E, S + W};
const int KNIGHT_OFF[8] = {2 * N + E, 2 * N + W, 2 * S + E, 2 * S + W,
                           2 * E + N, 2 * E + S, 2 * W + N, 2 * W + S};
inline void perp(int o, int &p1, int &p2) {
    if (o == N || o == S) { p1 = E; p2 = W; } else { p1 = N; p2 = S; }
}
int SQ64[64];

// ---------------------------------------------------------------- rules -----
struct Rules {
    bool horseBlock = true;
    bool elephantEye = false;
    bool stalemateLoss = false;
    bool soldierSideways = true;
    int soldierPromo = 3;          // 0 none, 1 Xiangqi pieces, 2 Western pieces, 3 any
    bool snipers = false;          // White's bishops are snipers: they can also take without moving
} R;
const int PROMO_XIANGQI[5] = {CHARIOT, CANNON, HORSE, ELEPHANT, ADVISOR};
const int PROMO_WESTERN[4] = {QUEEN, ROOK, BISHOP, KNIGHT};
const int PROMO_ANY[9] = {QUEEN, CHARIOT, ROOK, CANNON, HORSE, KNIGHT, BISHOP, ELEPHANT, ADVISOR};

// Material values in centipawns, fitted to self-play results by tools/tune_eval.py (middlegame values).
int VAL[NTYPES] = {0, 60, 319, 330, 500, 900, 0, 115, 180, 51, 60, 461, 182, 0};
// endgame corrections to VAL: added in full when only pawns/soldiers are left, scaled down as pieces return
int VAL_EG[NTYPES] = {0, 32, -22, -6, 25, 3, 0, -9, -10, 18, 14, -41, -40, 0};

inline bool crossed(int s, int c) { return c == WHITE ? RANK_(s) >= 4 : RANK_(s) <= 3; }
inline int relRank(int s, int c) { return c == WHITE ? RANK_(s) : 7 - RANK_(s); }

// ---------------------------------------------------------------- moves -----
typedef uint32_t Move;
const int MAXMOVES = 384;                  // room for any pseudo-legal move list
enum { F_CAP = 1, F_EP = 2, F_CASTLE = 4, F_DOUBLE = 8, F_SNIPE = 16 };   // F_SNIPE: a shot
inline Move MV(int f, int t, int promo = 0, int flags = 0) {
    return (Move)(f | (t << 8) | (promo << 16) | (flags << 24));
}
inline int FROM(Move m) { return m & 255; }
inline int TO(Move m) { return (m >> 8) & 255; }
inline int PROMO(Move m) { return (m >> 16) & 255; }
inline int FLAGS(Move m) { return (m >> 24) & 255; }

string sqName(int s) { return string(1, char('a' + FILE_(s))) + char('1' + RANK_(s)); }
string moveStr(Move m) {
    if (!m) return "0000";
    string r = sqName(FROM(m)) + sqName(TO(m));
    if (FLAGS(m) & F_SNIPE) r += 's';
    else if (PROMO(m)) r += char(tolower(PCHARS[PROMO(m)]));
    return r;
}

// ---------------------------------------------------------------- zobrist ---
U64 ZP[32][144], ZSIDE, ZCASTLE[16], ZEP[144];
U64 rngState = 0x9E3779B97F4A7C15ULL;
U64 splitmix() {
    U64 z = (rngState += 0x9E3779B97F4A7C15ULL);
    z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ULL;
    z = (z ^ (z >> 27)) * 0x94D049BB133111EBULL;
    return z ^ (z >> 31);
}
void initZobrist() {
    for (int p = 0; p < 32; p++) for (int s = 0; s < 144; s++) ZP[p][s] = splitmix();
    ZSIDE = splitmix();
    for (int i = 0; i < 16; i++) ZCASTLE[i] = splitmix();
    for (int s = 0; s < 144; s++) ZEP[s] = splitmix();
    for (int r = 0; r < 8; r++) for (int f = 0; f < 8; f++) SQ64[r * 8 + f] = SQ(f, r);
}

// ---------------------------------------------------------------- position --
struct Undo { Move m; int moved, captured, castle, ep, half, rev; U64 hash; };

int castleMask[144];

struct Pos {
    int b[144];
    int side, castle, ep, half, full;
    int rev;          // plies since the last irreversible move (window for repetition checks)
    int ksq[2];
    U64 hash;
    vector<Undo> undo;
    vector<U64> hh;   // hashes of all earlier positions (game + search path)

    void clear() {
        for (int i = 0; i < 144; i++) b[i] = OFFB;
        for (int i = 0; i < 64; i++) b[SQ64[i]] = EMPTY;
        side = WHITE; castle = 0; ep = -1; half = 0; full = 1; rev = 0;
        ksq[0] = ksq[1] = -1; undo.clear(); hh.clear();
    }
    U64 computeHash() const {
        U64 h = 0;
        for (int i = 0; i < 64; i++) { int s = SQ64[i]; if (b[s]) h ^= ZP[b[s]][s]; }
        if (side) h ^= ZSIDE;
        h ^= ZCASTLE[castle];
        if (ep >= 0) h ^= ZEP[ep];
        return h;
    }
    // Parse a FEN.  The position is only replaced if the whole FEN is valid.
    bool setFen(const string &fenStr) {
        vector<string> fld;
        { istringstream ss(fenStr); string x; while (ss >> x) fld.push_back(x); }
        if (fld.empty() || fld.size() > 6) return false;
        Pos T; T.clear();
        int r = 7, f = 0, royals[2] = {0, 0};
        for (char ch : fld[0]) {
            if (ch == '/') { if (f != 8 || r == 0) return false; r--; f = 0; continue; }
            if (ch >= '1' && ch <= '8') { f += ch - '0'; if (f > 8) return false; continue; }
            const char *p = strchr(PCHARS + 1, toupper((unsigned char)ch));
            if (!p || !isalpha((unsigned char)ch) || f > 7) return false;
            int t = int(p - PCHARS), c = isupper((unsigned char)ch) ? WHITE : BLACK;
            if (t == PAWN && (r == 0 || r == 7)) return false;          // pawns never stand on rank 1 or 8
            T.b[SQ(f, r)] = mk(c, t);
            if (isRoyal(t)) { T.ksq[c] = SQ(f, r); royals[c]++; }
            f++;
        }
        if (r != 0 || f != 8 || royals[0] != 1 || royals[1] != 1) return false;
        string stm = fld.size() > 1 ? fld[1] : "w";
        if (stm != "w" && stm != "b") return false;
        T.side = stm == "b" ? BLACK : WHITE;
        string cas = fld.size() > 2 ? fld[2] : "-";
        T.castle = 0;
        if (cas != "-") for (char ch : cas) {
            int bit = ch == 'K' ? 1 : ch == 'Q' ? 2 : ch == 'k' ? 4 : ch == 'q' ? 8 : 0;
            if (!bit || (T.castle & bit)) return false;
            T.castle |= bit;
        }
        string eps = fld.size() > 3 ? fld[3] : "-";
        T.ep = -1;
        if (eps != "-") {
            if (eps.size() != 2 || eps[0] < 'a' || eps[0] > 'h') return false;
            int er = eps[1] - '1';
            if (er != (T.side == WHITE ? 5 : 2)) return false;             // square the pawn skipped over
            T.ep = SQ(eps[0] - 'a', er);
            if (T.b[T.ep] != EMPTY) return false;
        }
        auto parseInt = [](const string &x, int lo, int &out) {
            if (x.empty() || x.size() > 6 || x.find_first_not_of("0123456789") != string::npos) return false;
            out = atoi(x.c_str());
            return out >= lo;
        };
        T.half = 0; T.full = 1;
        if (fld.size() > 4 && !parseInt(fld[4], 0, T.half)) return false;
        if (fld.size() > 5 && !parseInt(fld[5], 1, T.full)) return false;
        if (T.attacked(T.ksq[T.side ^ 1], T.side)) return false;     // side not to move is in check
        T.hash = T.computeHash();
        *this = T;
        return true;
    }
    string fen() const {
        string o;
        for (int r = 7; r >= 0; r--) {
            int e = 0;
            for (int f = 0; f < 8; f++) {
                int p = b[SQ(f, r)];
                if (!p) { e++; continue; }
                if (e) { o += char('0' + e); e = 0; }
                char ch = PCHARS[ptype(p)];
                o += pcolor(p) == WHITE ? ch : char(tolower(ch));
            }
            if (e) o += char('0' + e);
            if (r) o += '/';
        }
        o += side == WHITE ? " w " : " b ";
        string cs;
        if (castle & 1) cs += 'K'; if (castle & 2) cs += 'Q';
        if (castle & 4) cs += 'k'; if (castle & 8) cs += 'q';
        o += cs.empty() ? "-" : cs;
        o += " " + (ep >= 0 ? sqName(ep) : string("-"));
        o += " " + to_string(half) + " " + to_string(full);
        return o;
    }
    void print() const {
        for (int r = 7; r >= 0; r--) {
            printf(" %d  ", r + 1);
            for (int f = 0; f < 8; f++) {
                int p = b[SQ(f, r)];
                char ch = p ? PCHARS[ptype(p)] : '.';
                if (p && pcolor(p) == BLACK) ch = tolower(ch);
                printf("%c ", ch);
            }
            printf("\n");
        }
        printf("\n    a b c d e f g h\n\n fen: %s\n", fen().c_str());
    }

    // Is square t attacked by colour `by`?  (Reverse look-up from the target.)
    bool attacked(int t, int by) const {
        const int fwd = by == WHITE ? N : S;
        const int pawn = mk(by, PAWN), sold = mk(by, SOLDIER);
        if (b[t - fwd + E] == pawn || b[t - fwd + W] == pawn) return true;
        if (b[t - fwd] == sold) return true;
        if (R.soldierSideways) {
            if (b[t + E] == sold && crossed(t + E, by)) return true;
            if (b[t + W] == sold && crossed(t + W, by)) return true;
        }
        const int kn = mk(by, KNIGHT);
        for (int o : KNIGHT_OFF) if (b[t + o] == kn) return true;
        const int kg = mk(by, KING), gen = mk(by, GENERAL), adv = mk(by, ADVISOR), ele = mk(by, ELEPHANT);
        for (int d : ORTH) if (b[t + d] == kg || b[t + d] == gen) return true;
        for (int d : DIAG) {
            if (b[t + d] == kg || b[t + d] == adv) return true;
            if (b[t + 2 * d] == ele && (!R.elephantEye || b[t + d] == EMPTY)) return true;
        }
        const int hor = mk(by, HORSE);
        for (int o : ORTH) {
            int p1, p2; perp(o, p1, p2);
            for (int p : {p1, p2}) {
                int h = t - 2 * o - p;
                if (b[h] == hor && (!R.horseBlock || b[h + o] == EMPTY)) return true;
            }
        }
        const int rk = mk(by, ROOK), qn = mk(by, QUEEN), ch = mk(by, CHARIOT), can = mk(by, CANNON), bs = mk(by, BISHOP);
        for (int d : ORTH) {
            int s = t + d;
            while (b[s] == EMPTY) s += d;
            if (b[s] == OFFB) continue;
            if (b[s] == rk || b[s] == qn || b[s] == ch) return true;
            s += d;                                   // b[s-d] is the screen
            while (b[s] == EMPTY) s += d;
            if (b[s] == can) return true;
        }
        for (int d : DIAG) {
            int s = t + d;
            while (b[s] == EMPTY) s += d;
            if (b[s] == bs || b[s] == qn) return true;
        }
        return false;
    }
    bool inCheck(int c) const { return attacked(ksq[c], c ^ 1); }

    // ---- move generation (pseudo-legal) ----
    inline void add(Move *ml, int &n, int f, int t, int promo = 0, int fl = 0) const {
        if (isPiece(b[t])) fl |= F_CAP;
        ml[n++] = MV(f, t, promo, fl);
    }
    // Pseudo-legal moves of the piece on s (shots = false leaves out a sniper's shots).
    void genPiece(int s, Move *ml, int &n, bool capsOnly, bool shots = true) const {
        const int p = b[s], c = pcolor(p), t = ptype(p);
        auto enemy = [&](int q) { return isPiece(q) && pcolor(q) != c; };
        auto target = [&](int q) { return q == EMPTY ? !capsOnly : enemy(q); };
        switch (t) {
        case PAWN: {
            const int fwd = c == WHITE ? N : S, start = c == WHITE ? 1 : 6, last = c == WHITE ? 7 : 0;
            int to = s + fwd;
            if (b[to] == EMPTY) {
                if (RANK_(to) == last) {
                    add(ml, n, s, to, QUEEN);
                    if (!capsOnly) { add(ml, n, s, to, ROOK); add(ml, n, s, to, BISHOP); add(ml, n, s, to, KNIGHT); }
                } else if (!capsOnly) {
                    add(ml, n, s, to);
                    if (RANK_(s) == start && b[to + fwd] == EMPTY) add(ml, n, s, to + fwd, 0, F_DOUBLE);
                }
            }
            for (int d : {E, W}) {
                int q = s + fwd + d;
                if (enemy(b[q])) {
                    if (RANK_(q) == last) {
                        add(ml, n, s, q, QUEEN);
                        if (!capsOnly) { add(ml, n, s, q, ROOK); add(ml, n, s, q, BISHOP); add(ml, n, s, q, KNIGHT); }
                    } else add(ml, n, s, q);
                } else if (q == ep && b[q] == EMPTY) {
                    add(ml, n, s, q, 0, F_EP | F_CAP);
                }
            }
            break;
        }
        case SOLDIER: {
            const int fwd = c == WHITE ? N : S, last = c == WHITE ? 7 : 0;
            const int to = s + fwd, q = b[to];
            if (R.soldierPromo && q != OFFB && RANK_(to) == last && (q == EMPTY || enemy(q))) {
                // mandatory promotion; in capture-only mode just the strongest piece
                const int *pr = R.soldierPromo == 3 ? PROMO_ANY : R.soldierPromo == 2 ? PROMO_WESTERN : PROMO_XIANGQI;
                const int np = R.soldierPromo == 3 ? 9 : R.soldierPromo == 2 ? 4 : 5;
                add(ml, n, s, to, pr[0]);
                if (!capsOnly) for (int k = 1; k < np; k++) add(ml, n, s, to, pr[k]);
            } else if (target(q)) add(ml, n, s, to);
            if (R.soldierSideways && crossed(s, c))
                for (int d : {E, W}) if (target(b[s + d])) add(ml, n, s, s + d);
            break;
        }
        case KNIGHT:
            for (int o : KNIGHT_OFF) if (target(b[s + o])) add(ml, n, s, s + o);
            break;
        case HORSE:
            for (int o : ORTH) {
                if (b[s + o] == OFFB) continue;
                if (R.horseBlock && b[s + o] != EMPTY) continue;
                int p1, p2; perp(o, p1, p2);
                for (int pp : {p1, p2}) { int q = s + 2 * o + pp; if (target(b[q])) add(ml, n, s, q); }
            }
            break;
        case ELEPHANT:
            for (int d : DIAG) {
                int q = s + 2 * d;
                if (R.elephantEye && b[s + d] != EMPTY) continue;
                if (target(b[q])) add(ml, n, s, q);
            }
            break;
        case ADVISOR:
            for (int d : DIAG) if (target(b[s + d])) add(ml, n, s, s + d);
            break;
        case GENERAL:
            for (int d : ORTH) if (target(b[s + d])) add(ml, n, s, s + d);
            break;
        case KING:
            for (int d : ORTH) if (target(b[s + d])) add(ml, n, s, s + d);
            for (int d : DIAG) if (target(b[s + d])) add(ml, n, s, s + d);
            break;
        case BISHOP: case ROOK: case QUEEN: case CHARIOT: {
            const int *dirs[2] = {ORTH, DIAG};
            const bool sniper = shots && t == BISHOP && c == WHITE && R.snipers;
            for (int k = 0; k < 2; k++) {
                if (k == 0 && t == BISHOP) continue;
                if (k == 1 && (t == ROOK || t == CHARIOT)) continue;
                for (int i = 0; i < 4; i++) {
                    int d = dirs[k][i], q = s + d;
                    while (b[q] == EMPTY) { if (!capsOnly) add(ml, n, s, q); q += d; }
                    if (enemy(b[q])) {
                        add(ml, n, s, q);
                        if (sniper) add(ml, n, s, q, 0, F_SNIPE);      // ... or shoot it and stay
                    }
                }
            }
            break;
        }
        case CANNON:
            for (int d : ORTH) {
                int q = s + d;
                while (b[q] == EMPTY) { if (!capsOnly) add(ml, n, s, q); q += d; }
                if (b[q] == OFFB) continue;
                q += d;
                while (b[q] == EMPTY) q += d;
                if (enemy(b[q])) add(ml, n, s, q);
            }
            break;
        }
    }
    int gen(Move *ml, bool capsOnly) const {
        int n = 0;
        for (int i = 0; i < 64; i++) {
            int s = SQ64[i];
            if (isPiece(b[s]) && pcolor(b[s]) == side) genPiece(s, ml, n, capsOnly);
        }
        if (!capsOnly) genCastles(ml, n);
        return n;
    }
    void genCastles(Move *ml, int &n) const {
        const int c = side, r0 = c == WHITE ? 0 : 7, opp = c ^ 1;
        const int ks = SQ(4, r0);
        if (b[ks] != mk(c, KING)) return;
        const int kbit = c == WHITE ? 1 : 4, qbit = c == WHITE ? 2 : 8;
        if ((castle & kbit) && b[SQ(7, r0)] == mk(c, ROOK) && b[SQ(5, r0)] == EMPTY && b[SQ(6, r0)] == EMPTY &&
            !attacked(ks, opp) && !attacked(SQ(5, r0), opp) && !attacked(SQ(6, r0), opp))
            ml[n++] = MV(ks, SQ(6, r0), 0, F_CASTLE);
        if ((castle & qbit) && b[SQ(0, r0)] == mk(c, ROOK) && b[SQ(1, r0)] == EMPTY && b[SQ(2, r0)] == EMPTY &&
            b[SQ(3, r0)] == EMPTY && !attacked(ks, opp) && !attacked(SQ(3, r0), opp) && !attacked(SQ(2, r0), opp))
            ml[n++] = MV(ks, SQ(2, r0), 0, F_CASTLE);
    }

    // ---- make / unmake ----
    // Returns false (and leaves the move made) if it leaves the mover in check.
    bool make(Move m) {
        const int f = FROM(m), t = TO(m), fl = FLAGS(m), c = side;
        const int p = b[f];
        Undo u{m, p, b[t], castle, ep, half, rev, hash};
        hh.push_back(hash);
        if (fl & F_EP) {
            int cs = t - (c == WHITE ? N : S);
            u.captured = b[cs];
            hash ^= ZP[b[cs]][cs];
            b[cs] = EMPTY;
        } else if (b[t]) hash ^= ZP[b[t]][t];
        if (fl & F_SNIPE) {                  // a shot: the sniper stays, the target leaves the board
            b[t] = EMPTY;
            hash ^= ZCASTLE[castle];
            castle &= castleMask[t];
            hash ^= ZCASTLE[castle];
        } else {
            hash ^= ZP[p][f];
            b[f] = EMPTY;
            int np = PROMO(m) ? mk(c, PROMO(m)) : p;
            b[t] = np;
            hash ^= ZP[np][t];
            if (fl & F_CASTLE) {
                int r0 = RANK_(f), rf, rt;
                if (FILE_(t) == 6) { rf = SQ(7, r0); rt = SQ(5, r0); } else { rf = SQ(0, r0); rt = SQ(3, r0); }
                int rp = b[rf];
                b[rf] = EMPTY; b[rt] = rp;
                hash ^= ZP[rp][rf] ^ ZP[rp][rt];
            }
            if (isRoyal(ptype(p))) ksq[c] = t;
            hash ^= ZCASTLE[castle];
            castle &= castleMask[f] & castleMask[t];
            hash ^= ZCASTLE[castle];
        }
        if (ep >= 0) hash ^= ZEP[ep];
        ep = -1;
        if (fl & F_DOUBLE) { ep = f + (c == WHITE ? N : S); hash ^= ZEP[ep]; }
        int pt = ptype(p);
        // A sideways soldier step can be undone, so it neither resets the fifty-move count nor
        // ends the repetition window (the same convention as Fairy-Stockfish).
        const bool soldierForward = pt == SOLDIER && FILE_(f) == FILE_(t);
        half = (pt == PAWN || soldierForward || u.captured) ? 0 : half + 1;
        const bool irreversible = u.captured || pt == PAWN || PROMO(m) || castle != u.castle || soldierForward;
        rev = irreversible ? 0 : rev + 1;
        if (c == BLACK) full++;
        side ^= 1;
        hash ^= ZSIDE;
        undo.push_back(u);
        return !attacked(ksq[c], c ^ 1);
    }
    void unmake() {
        Undo u = undo.back(); undo.pop_back(); hh.pop_back();
        side ^= 1;
        const int c = side, m = u.m, f = FROM(m), t = TO(m), fl = FLAGS(m);
        const int p = u.moved;         // pawn or soldier if this was a promotion
        b[f] = p;                      // (after a shot the sniper is still there anyway)
        if (fl & F_EP) { b[t] = EMPTY; b[t - (c == WHITE ? N : S)] = u.captured; }
        else b[t] = u.captured;
        if (fl & F_CASTLE) {
            int r0 = RANK_(f), rf, rt;
            if (FILE_(t) == 6) { rf = SQ(7, r0); rt = SQ(5, r0); } else { rf = SQ(0, r0); rt = SQ(3, r0); }
            b[rf] = b[rt]; b[rt] = EMPTY;
        }
        if (isRoyal(ptype(p))) ksq[c] = f;
        castle = u.castle; ep = u.ep; half = u.half; rev = u.rev; hash = u.hash;
        if (c == BLACK) full--;
    }
    void makeNull() {
        Undo u{0, 0, 0, castle, ep, half, rev, hash};
        hh.push_back(hash);
        if (ep >= 0) hash ^= ZEP[ep];
        ep = -1; half++; rev = 0;          // never count repetitions across a null move
        side ^= 1; hash ^= ZSIDE;
        undo.push_back(u);
    }
    void unmakeNull() {
        Undo u = undo.back(); undo.pop_back(); hh.pop_back();
        side ^= 1; ep = u.ep; half = u.half; rev = u.rev; hash = u.hash;
    }
    int legalMoves(Move *out) {
        Move ml[MAXMOVES]; int n = gen(ml, false), k = 0;
        for (int i = 0; i < n; i++) { if (make(ml[i])) out[k++] = ml[i]; unmake(); }
        return k;
    }
    // Earlier occurrences of the current position since the last irreversible move.
    int repetitions() const {
        int cnt = 0, lim = min<int>(rev, (int)hh.size());
        for (int i = 2; i <= lim; i += 2) if (hh[hh.size() - i] == hash) cnt++;
        return cnt;
    }
    // Draw by repetition as the search sees it: a repeat of a position reached inside the
    // search (index >= rootSize) counts at once, since the side that repeated can repeat again;
    // positions from before the search need two earlier occurrences, as in the game rule.
    bool repetitionDraw(size_t rootSize) const {
        int cnt = 0, lim = min<int>(rev, (int)hh.size());
        for (int i = 2; i <= lim; i += 2) {
            size_t idx = hh.size() - i;
            if (hh[idx] == hash && (idx >= rootSize || ++cnt >= 2)) return true;
        }
        return false;
    }
    // Does the side to move have any legal move?  (Stops at the first one found.)
    bool hasLegalMove() {
        Move ml[64];
        for (int i = 0; i < 64; i++) {
            int s = SQ64[i];
            if (!isPiece(b[s]) || pcolor(b[s]) != side) continue;
            int n = 0;
            genPiece(s, ml, n, false);
            for (int k = 0; k < n; k++) { bool ok = make(ml[k]); unmake(); if (ok) return true; }
        }
        return false;      // (castling is never the only legal move: the king could step instead)
    }
    bool onlyRoyals() const {
        for (int i = 0; i < 64; i++) { int p = b[SQ64[i]]; if (isPiece(p) && !isRoyal(ptype(p))) return false; }
        return true;
    }
    // A dead draw: only the two royals are left and they are alike (king against king, or general
    // against general).  A king against a general is NOT a draw: the king can step diagonally next
    // to the general, where the general cannot touch it, and it always forces mate (within 12 moves).
    bool insufficientMaterial() const { return onlyRoyals() && ptype(b[ksq[WHITE]]) == ptype(b[ksq[BLACK]]); }
};

void initCastleMask() {
    for (int i = 0; i < 144; i++) castleMask[i] = 15;
    castleMask[SQ(4, 0)] = ~3 & 15; castleMask[SQ(7, 0)] = ~1 & 15; castleMask[SQ(0, 0)] = ~2 & 15;
    castleMask[SQ(4, 7)] = ~12 & 15; castleMask[SQ(7, 7)] = ~4 & 15; castleMask[SQ(0, 7)] = ~8 & 15;
}

// ---------------------------------------------------------------- eval ------
// Evaluation weights in centipawns.  The piece values (VAL, near the top of the file) and the
// weights below were fitted to the results of self-play games (tools/tune_eval.py); the
// endgame knowledge comes from the exact endgame solver (command tb, below).
int CENTER[144];
int PAWN_ADV[8] = {0, 0, 5, 15, 29, 54, 86, 0};
const int SOLD_ADV[8] = {0, 0, 0, 5, 30, 40, 40, 25};        // soldiers that never promote
int SOLD_ADV_PROMO[8] = {0, 0, 0, 6, 49, 75, 139, 0};         // soldiers that promote on the last rank
int CENW[NTYPES] = {0, 0, 3, 1, 0, 1, 0, 0, 4, 2, 3, 0, 2, 0};   // centralisation, per piece type
int MOBW[NTYPES] = {0, 0, 0, 3, 4, 1, 0, 0, 4, 3, 3, 2, 0, 0};   // mobility, per piece type
int SEVENTH = 15, KING_MG = 6, KING_EG = 8, BISHOP_PAIR = 23, PAWN_CENTRE = 4, TEMPO = 8;
int GEN_MG = 9, GEN_EG = 2, GEN_BASE_MG = 2, GEN_BASE_EG = -65;   // the general's own royal terms
int OPEN_K = -7, OPEN_G = 12;                     // per empty square on the lines around the king / general
int MOPUP_EDGE = 15, MOPUP_CLOSE = 6;           // mating technique against a bare king/general
int SCALE_NOPAWN = 48, SCALE_XQ = 8;           // /128: drawish endings without pawns/soldiers
int FIFTY_DIV = 200;                            // score shrinks as the fifty-move count grows
int TB_WIN = 1000;                              // a known win (solved endings), before the tie-breaks
int SNIPER_MG = 190, SNIPER_EG = 0;             // the snipers rule: a sniper's value over a bishop
const double EVAL_K = 181.7;   // expected score = 1 / (1 + exp(-score / EVAL_K)); fitted with the weights

void initEval() {
    for (int s = 0; s < 144; s++) CENTER[s] = 0;
    for (int r = 0; r < 8; r++) for (int f = 0; f < 8; f++) {
        double df = 3.5 - std::fabs(f - 3.5), dr = 3.5 - std::fabs(r - 3.5);
        CENTER[SQ(f, r)] = int(df + dr);                     // 0 in a corner .. 6 in the centre
    }
}

// ---- endgames ----
// Every ending of a king against a general with at most two other pieces has been solved exactly
// (command "tb"; tools/endgame_table.py turns its results into this table).  For each material
// signature the table holds the average result of the GENERAL'S side over the quiet positions (no
// capture, promotion or check to make), converted to centipawns on the evaluation's own scale
// (EVAL_K) and capped at +-TB_WIN.  With one pawn or soldier the average is taken separately for
// each rank it can stand on.  Key: 65536 * (rank of the pawn/soldier counted from its own side,
// 0 if none) + 256 * code of the general's side's pieces + code of the king's side's pieces,
// where the code of up to two piece types is hi * 16 + lo (hi >= lo, 0 = none).
struct EgEntry { int key, cp; };
// BEGIN EG_TABLE
const EgEntry EG_TABLE[] = {
    {8192, -1000},  // GN vs K: 0.0%
    {8224, -1000},  // GN vs KN: 0.0%
    {8240, -1000},  // GN vs KB: 0.0%
    {8256, -1000},  // GN vs KR: 0.0%
    {8272, -1000},  // GN vs KQ: 0.0%
    {8704, -40},  // GNN vs K: 44.5%
    {12288, -17},  // GB vs K: 47.6%
    {12320, -1000},  // GB vs KN: 0.4%
    {12336, -490},  // GB vs KB: 6.3%
    {12352, -1000},  // GB vs KR: 0.2%
    {12368, -1000},  // GB vs KQ: 0.0%
    {12800, -11},  // GBN vs K: 48.5%
    {13056, -7},  // GBB vs K: 49.1%
    {16384, -12},  // GR vs K: 48.4%
    {16416, -21},  // GR vs KN: 47.1%
    {16432, -40},  // GR vs KB: 44.5%
    {16448, -1000},  // GR vs KR: 0.3%
    {16464, -1000},  // GR vs KQ: 0.2%
    {16896, 668},  // GRN vs K: 97.5%
    {17152, 707},  // GRB vs K: 98.0%
    {17408, 743},  // GRR vs K: 98.4%
    {20480, 794},  // GQ vs K: 98.8%
    {20512, 184},  // GQ vs KN: 73.4%
    {20528, 93},  // GQ vs KB: 62.5%
    {20544, 38},  // GQ vs KR: 55.2%
    {20560, -502},  // GQ vs KQ: 5.9%
    {20992, 835},  // GQN vs K: 99.0%
    {21248, 854},  // GQB vs K: 99.1%
    {21504, 865},  // GQR vs K: 99.2%
    {21760, 944},  // GQQ vs K: 99.5%
    {32768, -1000},  // GH vs K: 0.0%
    {32800, -1000},  // GH vs KN: 0.0%
    {32816, -1000},  // GH vs KB: 0.0%
    {32832, -1000},  // GH vs KR: 0.0%
    {32848, -1000},  // GH vs KQ: 0.0%
    {33280, -47},  // GHN vs K: 43.6%
    {33536, -12},  // GHB vs K: 48.4%
    {33792, 646},  // GHR vs K: 97.2%
    {34048, 817},  // GHQ vs K: 98.9%
    {34816, -57},  // GHH vs K: 42.2%
    {36864, -1000},  // GE vs K: 0.0%
    {36896, -1000},  // GE vs KN: 0.0%
    {36912, -1000},  // GE vs KB: 0.0%
    {36928, -1000},  // GE vs KR: 0.0%
    {36944, -1000},  // GE vs KQ: 0.0%
    {37376, -89},  // GEN vs K: 38.0%
    {37632, -13},  // GEB vs K: 48.2%
    {37888, 5},  // GER vs K: 50.7%
    {38144, 801},  // GEQ vs K: 98.8%
    {38912, -269},  // GEH vs K: 18.5%
    {39168, -521},  // GEE vs K: 5.4%
    {40960, -1000},  // GA vs K: 0.0%
    {40992, -1000},  // GA vs KN: 0.0%
    {41008, -1000},  // GA vs KB: 0.0%
    {41024, -1000},  // GA vs KR: 0.0%
    {41040, -1000},  // GA vs KQ: 0.0%
    {41472, -186},  // GAN vs K: 26.4%
    {41728, -14},  // GAB vs K: 48.1%
    {41984, -10},  // GAR vs K: 48.6%
    {42240, 794},  // GAQ vs K: 98.8%
    {43008, -301},  // GAH vs K: 16.0%
    {43264, -732},  // GAE vs K: 1.8%
    {43520, -267},  // GAA vs K: 18.7%
    {45056, -12},  // GJ vs K: 48.4%
    {45088, -21},  // GJ vs KN: 47.1%
    {45104, -40},  // GJ vs KB: 44.5%
    {45120, -1000},  // GJ vs KR: 0.3%
    {45136, -1000},  // GJ vs KQ: 0.2%
    {45568, 668},  // GJN vs K: 97.5%
    {45824, 707},  // GJB vs K: 98.0%
    {46080, 743},  // GJR vs K: 98.4%
    {46336, 865},  // GJQ vs K: 99.2%
    {47104, 646},  // GHJ vs K: 97.2%
    {47360, 5},  // GEJ vs K: 50.7%
    {47616, -10},  // GAJ vs K: 48.6%
    {47872, 743},  // GJJ vs K: 98.4%
    {49152, -1000},  // GC vs K: 0.0%
    {49184, -1000},  // GC vs KN: 0.0%
    {49200, -1000},  // GC vs KB: 0.0%
    {49216, -1000},  // GC vs KR: 0.0%
    {49232, -1000},  // GC vs KQ: 0.0%
    {49664, -76},  // GCN vs K: 39.8%
    {49920, -17},  // GCB vs K: 47.7%
    {50176, 575},  // GCR vs K: 96.0%
    {50432, 767},  // GCQ vs K: 98.6%
    {51200, -162},  // GHC vs K: 29.1%
    {51456, -170},  // GEC vs K: 28.1%
    {51712, -971},  // GAC vs K: 0.5%
    {51968, 575},  // GCJ vs K: 96.0%
    {52224, -126},  // GCC vs K: 33.4%
    {73744, -1000},  // GN vs KP, pawn on rank 7: 0.0%
    {77840, -408},  // GB vs KP, pawn on rank 7: 9.6%
    {81936, -15},  // GR vs KP, pawn on rank 7: 47.9%
    {86032, 773},  // GQ vs KP, pawn on rank 7: 98.6%
    {98320, -1000},  // GH vs KP, pawn on rank 7: 0.0%
    {102416, -1000},  // GE vs KP, pawn on rank 7: 0.0%
    {106512, -1000},  // GA vs KP, pawn on rank 7: 0.0%
    {110608, -15},  // GJ vs KP, pawn on rank 7: 47.9%
    {114704, -1000},  // GC vs KP, pawn on rank 7: 0.0%
    {139280, -1000},  // GN vs KP, pawn on rank 6: 0.0%
    {143376, -399},  // GB vs KP, pawn on rank 6: 10.0%
    {147472, -16},  // GR vs KP, pawn on rank 6: 47.8%
    {151568, 754},  // GQ vs KP, pawn on rank 6: 98.5%
    {159744, -605},  // GS vs K, soldier on rank 3: 3.5%
    {159776, -1000},  // GS vs KN, soldier on rank 3: 0.1%
    {159792, -1000},  // GS vs KB, soldier on rank 3: 0.0%
    {159808, -1000},  // GS vs KR, soldier on rank 3: 0.0%
    {159824, -1000},  // GS vs KQ, soldier on rank 3: 0.0%
    {160256, -69},  // GSN vs K, soldier on rank 3: 40.6%
    {160512, 203},  // GSB vs K, soldier on rank 3: 75.3%
    {160768, 568},  // GSR vs K, soldier on rank 3: 95.8%
    {161024, 767},  // GSQ vs K, soldier on rank 3: 98.6%
    {163856, -1000},  // GH vs KP, pawn on rank 6: 0.0%
    {165632, -204},  // GSH vs K, soldier on rank 3: 24.5%
    {167952, -1000},  // GE vs KP, pawn on rank 6: 0.0%
    {169728, -337},  // GSE vs K, soldier on rank 3: 13.5%
    {172048, -1000},  // GA vs KP, pawn on rank 6: 0.0%
    {173824, -375},  // GSA vs K, soldier on rank 3: 11.3%
    {176144, -16},  // GJ vs KP, pawn on rank 6: 47.8%
    {177920, 568},  // GSJ vs K, soldier on rank 3: 95.8%
    {180240, -1000},  // GC vs KP, pawn on rank 6: 0.0%
    {182016, -395},  // GSC vs K, soldier on rank 3: 10.2%
    {204816, -1000},  // GN vs KP, pawn on rank 5: 0.0%
    {208912, -424},  // GB vs KP, pawn on rank 5: 8.8%
    {213008, -24},  // GR vs KP, pawn on rank 5: 46.7%
    {217104, 743},  // GQ vs KP, pawn on rank 5: 98.4%
    {225280, -349},  // GS vs K, soldier on rank 4: 12.8%
    {225312, -801},  // GS vs KN, soldier on rank 4: 1.2%
    {225328, -1000},  // GS vs KB, soldier on rank 4: 0.1%
    {225344, -1000},  // GS vs KR, soldier on rank 4: 0.0%
    {225360, -1000},  // GS vs KQ, soldier on rank 4: 0.0%
    {225792, 95},  // GSN vs K, soldier on rank 4: 62.8%
    {226048, 341},  // GSB vs K, soldier on rank 4: 86.7%
    {226304, 559},  // GSR vs K, soldier on rank 4: 95.6%
    {226560, 760},  // GSQ vs K, soldier on rank 4: 98.5%
    {229392, -1000},  // GH vs KP, pawn on rank 5: 0.0%
    {231168, 17},  // GSH vs K, soldier on rank 4: 52.4%
    {233488, -1000},  // GE vs KP, pawn on rank 5: 0.0%
    {235264, -115},  // GSE vs K, soldier on rank 4: 34.6%
    {237584, -1000},  // GA vs KP, pawn on rank 5: 0.0%
    {239360, -162},  // GSA vs K, soldier on rank 4: 29.0%
    {241680, -24},  // GJ vs KP, pawn on rank 5: 46.7%
    {243456, 559},  // GSJ vs K, soldier on rank 4: 95.6%
    {245776, -1000},  // GC vs KP, pawn on rank 5: 0.0%
    {247552, -210},  // GSC vs K, soldier on rank 4: 23.9%
    {270352, -1000},  // GN vs KP, pawn on rank 4: 0.0%
    {274448, -444},  // GB vs KP, pawn on rank 4: 8.0%
    {278544, -45},  // GR vs KP, pawn on rank 4: 43.8%
    {282640, 735},  // GQ vs KP, pawn on rank 4: 98.3%
    {290816, -157},  // GS vs K, soldier on rank 5: 29.6%
    {290848, -468},  // GS vs KN, soldier on rank 5: 7.1%
    {290864, -658},  // GS vs KB, soldier on rank 5: 2.6%
    {290880, -1000},  // GS vs KR, soldier on rank 5: 0.0%
    {290896, -1000},  // GS vs KQ, soldier on rank 5: 0.0%
    {291328, 289},  // GSN vs K, soldier on rank 5: 83.1%
    {291584, 522},  // GSB vs K, soldier on rank 5: 94.7%
    {291840, 622},  // GSR vs K, soldier on rank 5: 96.9%
    {292096, 780},  // GSQ vs K, soldier on rank 5: 98.7%
    {294928, -1000},  // GH vs KP, pawn on rank 4: 0.0%
    {296704, 247},  // GSH vs K, soldier on rank 5: 79.6%
    {299024, -1000},  // GE vs KP, pawn on rank 4: 0.0%
    {300800, 88},  // GSE vs K, soldier on rank 5: 61.8%
    {303120, -1000},  // GA vs KP, pawn on rank 4: 0.0%
    {304896, 32},  // GSA vs K, soldier on rank 5: 54.4%
    {307216, -45},  // GJ vs KP, pawn on rank 4: 43.8%
    {308992, 622},  // GSJ vs K, soldier on rank 5: 96.9%
    {311312, -1000},  // GC vs KP, pawn on rank 4: 0.0%
    {313088, -39},  // GSC vs K, soldier on rank 5: 44.6%
    {335888, -1000},  // GN vs KP, pawn on rank 3: 0.0%
    {339984, -502},  // GB vs KP, pawn on rank 3: 5.9%
    {344080, -98},  // GR vs KP, pawn on rank 3: 36.9%
    {348176, 539},  // GQ vs KP, pawn on rank 3: 95.1%
    {356352, 23},  // GS vs K, soldier on rank 6: 53.1%
    {356384, -239},  // GS vs KN, soldier on rank 6: 21.2%
    {356400, -339},  // GS vs KB, soldier on rank 6: 13.4%
    {356416, -1000},  // GS vs KR, soldier on rank 6: 0.1%
    {356432, -1000},  // GS vs KQ, soldier on rank 6: 0.0%
    {356864, 356},  // GSN vs K, soldier on rank 6: 87.6%
    {357120, 552},  // GSB vs K, soldier on rank 6: 95.4%
    {357376, 635},  // GSR vs K, soldier on rank 6: 97.0%
    {357632, 794},  // GSQ vs K, soldier on rank 6: 98.8%
    {360464, -1000},  // GH vs KP, pawn on rank 3: 0.0%
    {362240, 331},  // GSH vs K, soldier on rank 6: 86.1%
    {364560, -1000},  // GE vs KP, pawn on rank 3: 0.0%
    {366336, 231},  // GSE vs K, soldier on rank 6: 78.0%
    {368656, -1000},  // GA vs KP, pawn on rank 3: 0.0%
    {370432, 175},  // GSA vs K, soldier on rank 6: 72.3%
    {372752, -98},  // GJ vs KP, pawn on rank 3: 36.9%
    {374528, 635},  // GSJ vs K, soldier on rank 6: 97.0%
    {376848, -1000},  // GC vs KP, pawn on rank 3: 0.0%
    {378624, 84},  // GSC vs K, soldier on rank 6: 61.3%
    {401424, -1000},  // GN vs KP, pawn on rank 2: 0.0%
    {405520, -429},  // GB vs KP, pawn on rank 2: 8.6%
    {409616, -156},  // GR vs KP, pawn on rank 2: 29.8%
    {413712, 303},  // GQ vs KP, pawn on rank 2: 84.1%
    {421888, 272},  // GS vs K, soldier on rank 7: 81.8%
    {421920, -77},  // GS vs KN, soldier on rank 7: 39.6%
    {421936, -196},  // GS vs KB, soldier on rank 7: 25.4%
    {421952, -1000},  // GS vs KR, soldier on rank 7: 0.3%
    {421968, -1000},  // GS vs KQ, soldier on rank 7: 0.0%
    {422400, 433},  // GSN vs K, soldier on rank 7: 91.5%
    {422656, 588},  // GSB vs K, soldier on rank 7: 96.2%
    {422912, 632},  // GSR vs K, soldier on rank 7: 97.0%
    {423168, 794},  // GSQ vs K, soldier on rank 7: 98.8%
    {426000, -1000},  // GH vs KP, pawn on rank 2: 0.0%
    {427776, 396},  // GSH vs K, soldier on rank 7: 89.8%
    {430096, -1000},  // GE vs KP, pawn on rank 2: 0.0%
    {431872, 368},  // GSE vs K, soldier on rank 7: 88.3%
    {434192, -1000},  // GA vs KP, pawn on rank 2: 0.0%
    {435968, 339},  // GSA vs K, soldier on rank 7: 86.6%
    {438288, -156},  // GJ vs KP, pawn on rank 2: 29.8%
    {440064, 632},  // GSJ vs K, soldier on rank 7: 97.0%
    {442384, -1000},  // GC vs KP, pawn on rank 2: 0.0%
    {444160, 278},  // GSC vs K, soldier on rank 7: 82.2%
};
// END EG_TABLE
const int EG_TABLE_N = int(sizeof(EG_TABLE) / sizeof(EG_TABLE[0]));

static int egLookup(int key, int &cp) {                // binary search; the table is sorted by key
    int lo = 0, hi = EG_TABLE_N - 1;
    while (lo <= hi) {
        const int mid = (lo + hi) / 2;
        if (EG_TABLE[mid].key == key) { cp = EG_TABLE[mid].cp; return 1; }
        if (EG_TABLE[mid].key < key) lo = mid + 1; else hi = mid - 1;
    }
    return 0;
}
static inline int egCode(const int *t, int n) { return n == 0 ? 0 : n == 1 ? t[0] * 16 : max(t[0], t[1]) * 16 + min(t[0], t[1]); }

// Mating technique for side a: drive the other royal to the edge and into a corner, and come close.
static inline int mopup(const Pos &P, int a) {
    const int d = P.ksq[a ^ 1], k = P.ksq[a];
    const int dist = abs(FILE_(d) - FILE_(k)) + abs(RANK_(d) - RANK_(k));
    return MOPUP_EDGE * (7 - CENTER[d]) + MOPUP_CLOSE * (14 - dist);
}

// The known result of a king-against-general ending, in centipawns for the general's side.
// types/ntypes: the non-royal piece types of each side (only the first two are kept); mat: their
// total value per side; prr: the rank of the only pawn/soldier from its own side (0 if none).
//  * A bare general always loses: a lone king mates it within 12 moves wherever the pieces stand
//    (the general cannot step diagonally, so the king can stand diagonally next to it), and more
//    material only helps.
//  * With at most two other pieces the result comes from the solved table EG_TABLE.
static bool endgameKnown(const Pos &P, const int types[2][2], const int ntypes[2], const int mat[2], int prr, int &out) {
    const int g = ptype(P.b[P.ksq[WHITE]]) == GENERAL ? WHITE : BLACK, k = g ^ 1;
    if (!ntypes[g]) { out = -(TB_WIN + mat[k] / 4 + mopup(P, k)); return true; }
    if (ntypes[g] + ntypes[k] > 2) return false;
    int v;
    if (!egLookup(65536 * prr + 256 * egCode(types[g], ntypes[g]) + egCode(types[k], ntypes[k]), v)) return false;
    // the table's value, then the material as a tie-break (captures are progress) and the mating
    // technique of the side the table favours, both in proportion to how sure the result is
    out = v + (mat[g] - mat[k]) * abs(v) / (8 * TB_WIN) + mopup(P, v > 0 ? g : k) * v / TB_WIN;
    return true;
}

// Can side c force mate against a lone king/general?  From the exact endgame solver: a lone
// king already mates a lone general, but a general needs a queen, or a chariot or rook together
// with another chariot or rook, a cannon, a horse, a knight or a bishop, to mate a lone king;
// soldiers may promote.
bool canMateBare(const int cnt[NTYPES], int royal) {
    if (royal == KING) {
        for (int t = 1; t < NTYPES; t++) if (!isRoyal(t) && cnt[t]) return true;
        return false;
    }
    if (cnt[QUEEN]) return true;
    int J = cnt[CHARIOT] + cnt[ROOK], CH = cnt[CANNON] + cnt[HORSE] + cnt[KNIGHT] + cnt[BISHOP];
    if (J >= 2 || (J >= 1 && CH >= 1)) return true;
    if (cnt[SOLDIER] && R.soldierPromo) return true;       // a soldier can still promote
    return false;
}

// empty squares along the four orthogonal lines from s: how exposed a royal is to checks
static inline int openLines(const Pos &P, int s) {
    int n = 0;
    for (int d : ORTH) for (int q = s + d; P.b[q] == EMPTY; q += d) n++;
    return n;
}

int evaluate(Pos &P) {
    int sc[2] = {0, 0}, nonpawn = 0, bishops[2] = {0, 0}, cnt[2][NTYPES] = {};
    int pieceMat[2] = {0, 0}, pawns[2] = {0, 0}, nonroyal[2] = {0, 0}, egAdj[2] = {0, 0};
    int types[2][2] = {{0, 0}, {0, 0}}, mat[2] = {0, 0}, prr = 0;
    for (int i = 0; i < 64; i++) {
        int p = P.b[SQ64[i]];
        if (!p) continue;
        int t = ptype(p), c = pcolor(p);
        cnt[c][t]++;
        sc[c] += VAL[t];
        egAdj[c] += VAL_EG[t];
        if (t == BISHOP && c == WHITE && R.snipers) { sc[c] += SNIPER_MG; egAdj[c] += SNIPER_EG; }   // a sniper
        if (isRoyal(t)) continue;
        if (nonroyal[c] < 2) types[c][nonroyal[c]] = t;
        nonroyal[c]++;
        mat[c] += VAL[t];
        if (t == PAWN || t == SOLDIER) { pawns[c]++; prr = relRank(SQ64[i], c); }
        else { nonpawn += VAL[t]; pieceMat[c] += VAL[t]; }
        if (t == BISHOP) bishops[c]++;
    }
    const int g = ptype(P.b[P.ksq[WHITE]]) == GENERAL ? WHITE : BLACK;
    if (ptype(P.b[P.ksq[g]]) == GENERAL && ptype(P.b[P.ksq[g ^ 1]]) == KING) {   // a king against a general
        const int npw = pawns[0] + pawns[1];
        int known;
        if ((npw <= 1 || !nonroyal[g]) && endgameKnown(P, types, nonroyal, mat, npw == 1 ? prr : 0, known)) {
            int e = g == WHITE ? known : -known;
            e = e * (FIFTY_DIV - min(P.half, 100)) / FIFTY_DIV;
            return (P.side == WHITE ? e : -e) + TEMPO;
        }
    }
    // bare king/general: either a dead draw, or drive it to the edge
    int mop = 0;
    for (int c = 0; c < 2; c++) {
        if (nonroyal[c ^ 1] || !nonroyal[c]) continue;
        if (!canMateBare(cnt[c], ptype(P.b[P.ksq[c]]))) return 0;
        const int bonus = mopup(P, c);
        mop += c == WHITE ? bonus : -bonus;
    }
    const int mg = min(nonpawn, 5000);          // phase weight 0..5000
    for (int c = 0; c < 2; c++) sc[c] += egAdj[c] * (5000 - mg) / 5000;
    Move ml[64];
    for (int i = 0; i < 64; i++) {
        int s = SQ64[i], p = P.b[s];
        if (!p) continue;
        int c = pcolor(p), t = ptype(p), rr = relRank(s, c), cen = CENTER[s];
        int v = CENW[t] * cen;
        switch (t) {
        case PAWN: v = PAWN_ADV[rr] + ((FILE_(s) == 3 || FILE_(s) == 4) ? PAWN_CENTRE : 0); break;
        case SOLDIER: v = R.soldierPromo ? SOLD_ADV_PROMO[rr] : SOLD_ADV[rr]; break;
        case ROOK: case CHARIOT: v = rr == 6 ? SEVENTH : 0; break;
        case KING: v = (mg * (-KING_MG * rr) + (5000 - mg) * (KING_EG * cen)) / 5000 - OPEN_K * openLines(P, s); break;
        case GENERAL:       // a weaker royal than the king: easily checked, chased and mated
            v = (mg * (GEN_BASE_MG - GEN_MG * rr) + (5000 - mg) * (GEN_BASE_EG + GEN_EG * cen)) / 5000 - OPEN_G * openLines(P, s);
            break;
        }
        if (MOBW[t]) { int n = 0; P.genPiece(s, ml, n, false, false); v += MOBW[t] * n; }   // (shots are not mobility)
        sc[c] += v;
    }
    if (bishops[0] >= 2) sc[0] += BISHOP_PAIR;
    if (bishops[1] >= 2) sc[1] += BISHOP_PAIR;
    int e = sc[0] - sc[1] + mop;
    // drawish endings: the stronger side has no pawns/soldiers and only a small edge in pieces,
    // or it is the general's side without enough material to mate even a bare king
    const int strong = e >= 0 ? WHITE : BLACK;
    int scale = 128;
    if (!pawns[strong] && !mop) {
        if (pieceMat[strong] - pieceMat[strong ^ 1] < VAL[ROOK]) scale = SCALE_NOPAWN;
        if (ptype(P.b[P.ksq[strong]]) == GENERAL && !canMateBare(cnt[strong], GENERAL)) scale = min(scale, SCALE_XQ);
    }
    e = e * scale / 128;
    e = e * (FIFTY_DIV - min(P.half, 100)) / FIFTY_DIV;      // no progress: drift towards a draw
    return (P.side == WHITE ? e : -e) + TEMPO;
}

// ---------------------------------------------------------------- search ----
const int INF = 32000, MATE = 31000, MAXPLY = 128;
inline bool isMate(int s) { return abs(s) > MATE - 2 * MAXPLY; }

// Transposition table shared by all search threads.  Lock-free: each entry stores
// key^data next to data, so an entry torn by two threads writing at once fails the
// key check and is simply ignored.
struct TTE { std::atomic<U64> key{0}, data{0}; };
enum { TT_EXACT = 1, TT_LOWER = 2, TT_UPPER = 3 };
std::unique_ptr<TTE[]> TT;
U64 ttMask = 0;
void ttResize(int mb) {
    size_t n = 1;
    while (n * 2 * sizeof(TTE) <= (size_t)mb * 1024 * 1024) n *= 2;
    TT.reset(new TTE[n]);
    ttMask = n - 1;
}
void ttClear() {
    for (size_t i = 0; i <= ttMask; i++) { TT[i].key.store(0, std::memory_order_relaxed); TT[i].data.store(0, std::memory_order_relaxed); }
}
inline bool ttProbe(U64 h, Move &m, int &score, int &depth, int &flag) {
    TTE &e = TT[h & ttMask];
    U64 k = e.key.load(std::memory_order_relaxed), d = e.data.load(std::memory_order_relaxed);
    if ((k ^ d) != h) return false;
    m = (Move)(d & 0xffffffffULL);
    score = (int16_t)((d >> 32) & 0xffff);
    depth = (int8_t)((d >> 48) & 0xff);
    flag = (int)((d >> 56) & 0xff);
    return true;
}
inline void ttStore(U64 h, Move m, int score, int depth, int flag) {
    U64 d = (U64)m | ((U64)(uint16_t)(int16_t)score << 32) | ((U64)(uint8_t)(int8_t)depth << 48) | ((U64)flag << 56);
    TTE &e = TT[h & ttMask];
    e.key.store(h ^ d, std::memory_order_relaxed);
    e.data.store(d, std::memory_order_relaxed);
}

std::atomic<bool> gStop{false};      // set by the 'stop' command
int gThreads = 1;                    // search threads (setoption Threads N)

struct Searcher {
    Pos &P;
    long long nodes = 0, nodeLimit = 0, timeLimitMs = 0;
    chrono::steady_clock::time_point t0;
    bool stop = false, canStop = false;
    std::atomic<bool> *ext = nullptr;          // external stop flag
    std::atomic<long long> pubNodes{0};        // node count visible to other threads
    Move killers[MAXPLY][2];
    int hist[32][144];
    Move pv[MAXPLY][MAXPLY];
    int pvLen[MAXPLY];
    vector<Move> rootExclude;
    size_t rootSize;                       // P.hh.size() at the root of the search

    explicit Searcher(Pos &p) : P(p), rootSize(p.hh.size()) { resetHeuristics(); }
    void resetHeuristics() { memset(killers, 0, sizeof killers); memset(hist, 0, sizeof hist); }

    long long elapsedMs() const {
        return chrono::duration_cast<chrono::milliseconds>(chrono::steady_clock::now() - t0).count();
    }
    void checkLimits() {
        if (!canStop) return;
        if (nodeLimit && nodes >= nodeLimit) stop = true;
        if ((nodes & 1023) == 0) {
            pubNodes.store(nodes, std::memory_order_relaxed);
            if (ext && ext->load(std::memory_order_relaxed)) stop = true;
            if (timeLimitMs && elapsedMs() >= timeLimitMs) stop = true;
        }
    }

    int moveScore(Move m, Move ttMove, int ply) {
        if (m == ttMove) return 1 << 30;
        if (FLAGS(m) & F_CAP) {
            int victim = (FLAGS(m) & F_EP) ? PAWN : ptype(P.b[TO(m)]);
            if (FLAGS(m) & F_SNIPE) return (1 << 28) + 16 * VAL[victim];   // a shot risks nothing
            return (1 << 28) + 16 * VAL[victim] - VAL[ptype(P.b[FROM(m)])] / 8;
        }
        if (PROMO(m)) return (1 << 28) + VAL[PROMO(m)];
        if (m == killers[ply][0]) return (1 << 27) + 2;
        if (m == killers[ply][1]) return (1 << 27) + 1;
        return hist[P.b[FROM(m)]][TO(m)];
    }
    void sortMoves(Move *ml, int n, Move ttMove, int ply) {
        int sc[MAXMOVES];
        for (int i = 0; i < n; i++) sc[i] = moveScore(ml[i], ttMove, ply);
        for (int i = 1; i < n; i++) {           // insertion sort, descending
            Move m = ml[i]; int s = sc[i], j = i - 1;
            while (j >= 0 && sc[j] < s) { ml[j + 1] = ml[j]; sc[j + 1] = sc[j]; j--; }
            ml[j + 1] = m; sc[j + 1] = s;
        }
    }

    int qsearch(int alpha, int beta, int ply) {
        nodes++; checkLimits();
        if (stop) return 0;
        if (P.half >= 100 || P.repetitionDraw(rootSize) || P.insufficientMaterial()) return 0;
        if (ply >= MAXPLY - 1) return evaluate(P);
        bool inC = P.inCheck(P.side);
        int best = -INF;
        if (!inC && !P.hasLegalMove()) return R.stalemateLoss ? -MATE + ply : 0;   // stalemate
        if (!inC) {
            int stand = evaluate(P);
            if (stand >= beta) return stand;
            if (stand > alpha) alpha = stand;
            best = stand;
        }
        Move ml[MAXMOVES];
        int n = P.gen(ml, !inC), legal = 0;
        sortMoves(ml, n, 0, ply);
        for (int i = 0; i < n; i++) {
            if (!P.make(ml[i])) { P.unmake(); continue; }
            legal++;
            int s = -qsearch(-beta, -alpha, ply + 1);
            P.unmake();
            if (stop) return 0;
            if (s > best) {
                best = s;
                if (s > alpha) { alpha = s; if (s >= beta) break; }
            }
        }
        if (inC && legal == 0) return -MATE + ply;
        return best;
    }

    bool hasPieces(int c) const {
        for (int i = 0; i < 64; i++) {
            int p = P.b[SQ64[i]];
            if (isPiece(p) && pcolor(p) == c) { int t = ptype(p); if (t != PAWN && t != SOLDIER && !isRoyal(t)) return true; }
        }
        return false;
    }

    int search(int depth, int alpha, int beta, int ply, bool doNull) {
        pvLen[ply] = ply;
        const bool pvNode = beta - alpha > 1;
        if (ply > 0) {
            if (P.half >= 100 || P.repetitionDraw(rootSize) || P.insufficientMaterial()) return 0;
            alpha = max(alpha, -MATE + ply);                   // mate-distance pruning
            beta = min(beta, MATE - ply - 1);
            if (alpha >= beta) return alpha;
        }
        if (ply >= MAXPLY - 1) return evaluate(P);
        const bool inC = P.inCheck(P.side);
        if (inC) depth++;
        if (depth <= 0) return qsearch(alpha, beta, ply);
        nodes++; checkLimits();
        if (stop) return 0;

        Move ttMove = 0;
        int eScore, eDepth, eFlag;
        if (ttProbe(P.hash, ttMove, eScore, eDepth, eFlag)) {
            if (ply > 0 && !pvNode && eDepth >= depth) {
                int s = eScore;
                if (s > MATE - 2 * MAXPLY) s -= ply; else if (s < -MATE + 2 * MAXPLY) s += ply;
                if (eFlag == TT_EXACT || (eFlag == TT_LOWER && s >= beta) || (eFlag == TT_UPPER && s <= alpha)) return s;
            }
        }

        if (!inC && !pvNode && doNull && depth >= 3 && ply > 0 && hasPieces(P.side) && evaluate(P) >= beta) {
            int Rn = 2 + depth / 6;
            P.makeNull();
            int s = -search(depth - 1 - Rn, -beta, -beta + 1, ply + 1, false);
            P.unmakeNull();
            if (stop) return 0;
            if (s >= beta) return isMate(s) ? beta : s;
        }

        Move ml[MAXMOVES];
        int n = P.gen(ml, false);
        sortMoves(ml, n, ttMove, ply);
        int legal = 0, best = -INF, origAlpha = alpha;
        Move bestMove = 0;
        for (int i = 0; i < n; i++) {
            Move m = ml[i];
            if (ply == 0 && find(rootExclude.begin(), rootExclude.end(), m) != rootExclude.end()) continue;
            const bool quiet = !(FLAGS(m) & F_CAP) && !PROMO(m);
            if (!P.make(m)) { P.unmake(); continue; }
            legal++;
            const bool givesCheck = P.inCheck(P.side);
            int s;
            if (legal == 1) {
                s = -search(depth - 1, -beta, -alpha, ply + 1, true);
            } else {
                int red = 0;
                if (depth >= 3 && legal > 3 && quiet && !inC && !givesCheck) red = 1 + (legal > 10) + (depth > 7);
                s = -search(depth - 1 - red, -alpha - 1, -alpha, ply + 1, true);
                if (s > alpha && red) s = -search(depth - 1, -alpha - 1, -alpha, ply + 1, true);
                if (s > alpha && s < beta) s = -search(depth - 1, -beta, -alpha, ply + 1, true);
            }
            P.unmake();
            if (stop) return 0;
            if (s > best) {
                best = s; bestMove = m;
                if (s > alpha) {
                    alpha = s;
                    pv[ply][ply] = m;
                    for (int j = ply + 1; j < pvLen[ply + 1]; j++) pv[ply][j] = pv[ply + 1][j];
                    pvLen[ply] = max(pvLen[ply + 1], ply + 1);
                    if (s >= beta) {
                        if (quiet) {
                            if (killers[ply][0] != m) { killers[ply][1] = killers[ply][0]; killers[ply][0] = m; }
                            int &h = hist[P.b[FROM(m)]][TO(m)];
                            h += depth * depth; if (h > (1 << 26)) for (auto &row : hist) for (int &x : row) x /= 2;
                        }
                        break;
                    }
                }
            }
        }
        if (legal == 0) {
            if (ply == 0 && !rootExclude.empty()) return -INF;      // no more root moves for multipv
            if (inC || R.stalemateLoss) return -MATE + ply;
            return 0;
        }
        int st = best;
        if (st > MATE - 2 * MAXPLY) st += ply; else if (st < -MATE + 2 * MAXPLY) st -= ply;
        ttStore(P.hash, bestMove, st, depth, best >= beta ? TT_LOWER : (best > origAlpha ? TT_EXACT : TT_UPPER));
        return best;
    }
};

struct Line { Move move; int score; vector<Move> pv; };
struct Limits { int depth = 64; long long nodes = 0, movetime = 0; int multipv = 1; bool verbose = true; };

string scoreStr(int s) {
    if (isMate(s)) {
        int m = s > 0 ? (MATE - s + 1) / 2 : -(MATE + s) / 2;
        return "mate " + to_string(m);
    }
    return "cp " + to_string(s);
}

// Iterative deepening with "lazy SMP": with gThreads > 1, helper threads search the same
// position (at staggered depths) and share their results through the transposition table;
// the main thread's search is the one reported.  Node-limited searches use one thread.
vector<Line> think(Pos &P, const Limits &L, int *depthOut = nullptr) {
    std::unique_ptr<Searcher> Sp(new Searcher(P));
    Searcher &S = *Sp;
    S.t0 = chrono::steady_clock::now();
    S.nodeLimit = L.nodes; S.timeLimitMs = L.movetime;
    S.ext = &gStop;
    std::atomic<bool> helpersStop{false};
    vector<std::unique_ptr<Pos>> hpos;
    vector<std::unique_ptr<Searcher>> hs;
    vector<std::thread> threads;
    int nh = L.nodes ? 0 : gThreads - 1;
    for (int i = 0; i < nh; i++) {
        hpos.emplace_back(new Pos(P));
        hs.emplace_back(new Searcher(*hpos.back()));
        Searcher *H = hs.back().get();
        H->ext = &helpersStop; H->canStop = true; H->t0 = S.t0;
        threads.emplace_back([H, i]() {
            for (int d = 1 + (i & 1); d <= MAXPLY - 10 && !H->stop; d++) H->search(d, -INF, INF, 0, false);
        });
    }
    auto totalNodes = [&]() {
        long long n = S.nodes;
        for (auto &h : hs) n += h->pubNodes.load(std::memory_order_relaxed);
        return n;
    };
    vector<Line> done;
    int maxDepth = min(L.depth, MAXPLY - 10);
    for (int d = 1; d <= maxDepth; d++) {
        S.canStop = d > 1;
        vector<Line> cur;
        S.rootExclude.clear();
        for (int k = 0; k < L.multipv; k++) {
            int s = S.search(d, -INF, INF, 0, false);
            if (S.stop || s == -INF || S.pvLen[0] == 0) break;
            Line ln{S.pv[0][0], s, vector<Move>(S.pv[0], S.pv[0] + S.pvLen[0])};
            cur.push_back(ln);
            S.rootExclude.push_back(ln.move);
        }
        if (S.stop && d > 1) break;
        if (cur.empty()) break;
        stable_sort(cur.begin(), cur.end(), [](const Line &a, const Line &b) { return a.score > b.score; });
        done = cur;
        if (depthOut) *depthOut = d;
        if (L.verbose) {
            long long ms = S.elapsedMs(), n = totalNodes();
            for (size_t k = 0; k < done.size(); k++) {
                printf("info depth %d multipv %zu score %s nodes %lld time %lld nps %lld pv", d, k + 1,
                       scoreStr(done[k].score).c_str(), n, ms, ms ? n * 1000 / ms : 0);
                for (Move m : done[k].pv) printf(" %s", moveStr(m).c_str());
                printf("\n");
            }
            fflush(stdout);
        }
        if (L.multipv == 1 && isMate(done[0].score) && d > 2 * (MATE - abs(done[0].score)) + 2) break;
    }
    helpersStop = true;
    for (auto &t : threads) t.join();
    return done;
}

// ---------------------------------------------------------------- perft -----
U64 perft(Pos &P, int d) {
    if (d == 0) return 1;
    Move ml[MAXMOVES]; int n = P.gen(ml, false); U64 c = 0;
    for (int i = 0; i < n; i++) { if (P.make(ml[i])) c += perft(P, d - 1); P.unmake(); }
    return c;
}

bool parseMove(Pos &P, const string &s, Move &out) {
    Move ml[MAXMOVES]; int n = P.legalMoves(ml);
    for (int i = 0; i < n; i++) if (moveStr(ml[i]) == s) { out = ml[i]; return true; }
    return false;
}

// Game-level termination check.  Returns "" if the game goes on.
string gameOver(Pos &P, string &result) {
    Move ml[MAXMOVES];
    int n = P.legalMoves(ml);
    if (n == 0) {
        bool mate = P.inCheck(P.side);
        if (mate || R.stalemateLoss) { result = P.side == WHITE ? "0-1" : "1-0"; return mate ? "checkmate" : "stalemate"; }
        result = "1/2-1/2"; return "stalemate";
    }
    if (P.repetitions() >= 2) { result = "1/2-1/2"; return "repetition"; }
    if (P.half >= 100) { result = "1/2-1/2"; return "fifty-move"; }
    if (P.insufficientMaterial()) { result = "1/2-1/2"; return "insufficient"; }
    return "";
}

// ---------------------------------------------------------------- selfplay --
// Xiangqi army White (soldiers a3 d3 e3 h3), Western army Black.
const char *START_FEN = "rnbqkbnr/pppppppp/8/8/8/S2SS2S/1C4C1/JHEAGEHJ w kq - 0 1";

void selfplay(istringstream &ss) {
    int games = 1, depth = 64, randomPlies = 4, maxPlies = 300, resign = 0, sampleEvery = 2;
    long long nodes = 20000; U64 seed = 1;
    int randMargin = 100, randDepth = 4;
    string fen = START_FEN, tok;
    while (ss >> tok) {
        if (tok == "games") ss >> games;
        else if (tok == "nodes") ss >> nodes;
        else if (tok == "depth") ss >> depth;
        else if (tok == "randomplies") ss >> randomPlies;
        else if (tok == "seed") ss >> seed;
        else if (tok == "maxplies") ss >> maxPlies;
        else if (tok == "resign") ss >> resign;
        else if (tok == "samplevery") ss >> sampleEvery;
        else if (tok == "randmargin") ss >> randMargin;
        else if (tok == "randdepth") ss >> randDepth;
        else if (tok == "fen") { getline(ss, fen); fen.erase(0, fen.find_first_not_of(' ')); }
    }
    // keep every setting in a usable range
    games = max(0, games); depth = max(1, min(depth, MAXPLY - 10)); nodes = max(0LL, nodes);
    randomPlies = max(0, randomPlies); maxPlies = max(1, maxPlies); resign = max(0, resign);
    sampleEvery = max(0, sampleEvery); randDepth = max(1, min(randDepth, MAXPLY - 10));
    { Pos test; if (!test.setFen(fen)) { printf("{\"error\":\"bad fen\"}\n"); fflush(stdout); return; } }
    U64 rng = seed * 0x2545F4914F6CDD1DULL + 12345;
    auto rnd = [&]() { rng ^= rng << 13; rng ^= rng >> 7; rng ^= rng << 17; return rng; };
    Pos P;
    for (int g = 0; g < games && !gStop; g++) {
        P.setFen(fen);
        ttClear();
        string result, reason, moves;
        vector<string> samples;
        int badStreak[2] = {0, 0};
        int ply = 0;
        for (;; ply++) {
            if (gStop) return;                       // 'stop': abandon the game in progress
            reason = gameOver(P, result);
            if (!reason.empty()) break;
            if (ply >= maxPlies) { result = "1/2-1/2"; reason = "maxplies"; break; }
            Move m; int score = 0;
            if (ply < randomPlies) {
                // Random but sound: pick uniformly among moves scoring within randMargin
                // of the best one in a shallow all-moves search (randMargin < 0: any legal move).
                Move ml[MAXMOVES]; int n = P.legalMoves(ml);
                if (randMargin < 0) m = ml[rnd() % n];
                else {
                    Limits L; L.depth = randDepth; L.multipv = n; L.verbose = false;
                    vector<Line> lines = think(P, L);
                    vector<Move> ok;
                    for (auto &ln : lines) if (ln.score >= lines[0].score - randMargin) ok.push_back(ln.move);
                    m = ok.empty() ? ml[rnd() % n] : ok[rnd() % ok.size()];
                }
            } else {
                Limits L; L.depth = depth; L.nodes = nodes; L.verbose = false;
                vector<Line> lines = think(P, L);
                if (lines.empty()) {                   // only if the search was stopped at once
                    if (gStop) return;
                    Move ml[MAXMOVES]; P.legalMoves(ml); lines.push_back(Line{ml[0], 0, {}});
                }
                m = lines[0].move; score = lines[0].score;
                int wscore = P.side == WHITE ? score : -score;
                bool quiet = !(FLAGS(m) & F_CAP) && !PROMO(m) && !P.inCheck(P.side);
                if (quiet && !isMate(score) && sampleEvery > 0 && ply % sampleEvery == 0) {
                    int cnt[2][NTYPES] = {};
                    for (int i = 0; i < 64; i++) { int p = P.b[SQ64[i]]; if (p) cnt[pcolor(p)][ptype(p)]++; }
                    string smp = "[" + to_string(ply) + "," + to_string(wscore);
                    for (int c = 0; c < 2; c++) for (int t = 1; t < NTYPES; t++) smp += "," + to_string(cnt[c][t]);
                    samples.push_back(smp + "]");
                }
                if (resign > 0) {
                    if (score <= -resign) badStreak[P.side]++; else badStreak[P.side] = 0;
                    if (badStreak[P.side] >= 3) { result = P.side == WHITE ? "0-1" : "1-0"; reason = "adjudicated"; break; }
                }
            }
            if (!moves.empty()) moves += ' ';
            moves += moveStr(m);
            P.make(m);
        }
        printf("{\"game\":%d,\"result\":\"%s\",\"reason\":\"%s\",\"plies\":%d,\"start\":\"%s\",\"final\":\"%s\",\"moves\":\"%s\",\"samples\":[",
               g, result.c_str(), reason.c_str(), ply, fen.c_str(), P.fen().c_str(), moves.c_str());
        for (size_t i = 0; i < samples.size(); i++) printf("%s%s", i ? "," : "", samples[i].c_str());
        printf("]}\n");
        fflush(stdout);
    }
}

// ---------------------------------------------------------------- endgames --
// Exact solver for endings of two to four pieces, e.g. "tb GA k" (general + advisor against a
// king), "tb GJ kr" or "tb GS kp".  Either side may win: every position is set up, the checkmates
// of both sides are found, and wins and losses are propagated backwards ("retrograde analysis")
// with "unmoves".  A capture or a promotion leads into another ending, which is solved first.
// Whatever is never resolved is a draw.  No fifty-move rule is applied.  Soldiers stand on the
// third to seventh rank of their side (the eighth too if they cannot promote), pawns on the second
// to seventh; en passant is ignored, so only one side may have pawns.
struct TbX {
    int np = 0;
    int pc[4] = {0, 0, 0, 0};                 // piece codes (colour and type)
    size_t N = 0;
    vector<uint8_t> val[2];                   // for the side to move: 0 draw/unknown, 1 win, 2 loss, 3 illegal
    vector<uint8_t> dtm[2];                   // plies to mate (at most 255)
    vector<bool> quiet[2];                    // not in check, and no capture or promotion to make
    size_t index(const int *sq) const { size_t i = 0; for (int j = 0; j < np; j++) i = i * 64 + sq[j]; return i; }
    void decode(size_t idx, int *sq) const { for (int j = np - 1; j >= 0; j--) { sq[j] = int(idx % 64); idx /= 64; } }
};

// Can a pawn or soldier stand on this square?  (Other pieces can stand anywhere.)
static bool tbxSquareOk(int pc, int s64) {
    const int t = ptype(pc), rr = pcolor(pc) == WHITE ? s64 / 8 : 7 - s64 / 8;
    if (t == PAWN) return rr >= 1 && rr <= 6;
    if (t == SOLDIER) return rr >= 2 && (rr <= 6 || !R.soldierPromo);
    return true;
}

// squares a piece of type t and colour c on square s64 could have come from with a move that
// neither captures nor promotes
static void unmoves(const Pos &P, int t, int c, int s64, vector<int> &out) {
    out.clear();
    const int s = SQ64[s64];
    const int fwd = c == WHITE ? N : S;
    auto add = [&](int q) { if (P.b[q] == EMPTY) out.push_back(RANK_(q) * 8 + FILE_(q)); };
    auto slide = [&](const int *dirs, int nd) {
        for (int k = 0; k < nd; k++) { int q = s + dirs[k]; while (P.b[q] == EMPTY) { out.push_back(RANK_(q) * 8 + FILE_(q)); q += dirs[k]; } }
    };
    switch (t) {
    case QUEEN: slide(ORTH, 4); slide(DIAG, 4); break;
    case ROOK: case CHARIOT: case CANNON: slide(ORTH, 4); break;
    case BISHOP: slide(DIAG, 4); break;
    case KNIGHT: for (int o : KNIGHT_OFF) add(s - o); break;
    case HORSE:
        for (int o : ORTH) { int p1, p2; perp(o, p1, p2);
            for (int p : {p1, p2}) { int f = s - 2 * o - p; if (P.b[f] == EMPTY && (!R.horseBlock || P.b[f + o] == EMPTY)) out.push_back(RANK_(f) * 8 + FILE_(f)); } }
        break;
    case ELEPHANT: for (int d : DIAG) { int f = s - 2 * d; if (P.b[f] == EMPTY && (!R.elephantEye || P.b[s - d] == EMPTY)) out.push_back(RANK_(f) * 8 + FILE_(f)); } break;
    case ADVISOR: for (int d : DIAG) add(s - d); break;
    case GENERAL: for (int d : ORTH) add(s - d); break;
    case KING: for (int d : ORTH) add(s - d); for (int d : DIAG) add(s - d); break;
    case SOLDIER:
        if (P.b[s - fwd] == EMPTY && tbxSquareOk(mk(c, SOLDIER), RANK_(s - fwd) * 8 + FILE_(s - fwd))) add(s - fwd);
        if (R.soldierSideways && crossed(s, c)) { add(s - E); add(s - W); }
        break;
    case PAWN: {
        const int rr = c == WHITE ? RANK_(s) : 7 - RANK_(s);
        if (rr >= 2) add(s - fwd);
        if (rr == 3 && P.b[s - fwd] == EMPTY) add(s - 2 * fwd);
        break;
    }
    }
}

static string tbxKey(vector<int> pcs) {
    sort(pcs.begin(), pcs.end());
    string k;
    for (int p : pcs) k += char(pcolor(p) == WHITE ? PCHARS[ptype(p)] : tolower(PCHARS[ptype(p)]));
    return k;
}

// Squares of the pieces of table T in the current position P (duplicates matched in order).
static bool tbxSquares(const Pos &P, const TbX &T, int *sq) {
    bool used[64] = {};
    for (int j = 0; j < T.np; j++) {
        sq[j] = -1;
        for (int i = 0; i < 64; i++)
            if (!used[i] && P.b[SQ64[i]] == T.pc[j]) { used[i] = true; sq[j] = i; break; }
        if (sq[j] < 0) return false;
    }
    return true;
}

TbX &tbxSolve(vector<int> pcs, map<string, TbX> &cache) {
    sort(pcs.begin(), pcs.end());
    const string key = tbxKey(pcs);
    auto found = cache.find(key);
    if (found != cache.end()) return found->second;
    for (size_t j = 0; j < pcs.size(); j++)             // endings after a capture come first
        if (!isRoyal(ptype(pcs[j]))) { vector<int> rest = pcs; rest.erase(rest.begin() + j); tbxSolve(rest, cache); }
    TbX &T = cache[key];
    T.np = int(pcs.size());
    for (int j = 0; j < T.np; j++) T.pc[j] = pcs[j];
    T.N = 1; for (int j = 0; j < T.np; j++) T.N *= 64;
    vector<uint8_t> cnt[2];
    for (int s = 0; s < 2; s++) { T.val[s].assign(T.N, 3); T.dtm[s].assign(T.N, 0); cnt[s].assign(T.N, 0); T.quiet[s].assign(T.N, false); }
    vector<vector<uint64_t>> buckets(257);             // entry = (index << 2) | (side << 1) | isWinCandidate
    Pos P;
    int sq[4], sub[4];
    Move ml[MAXMOVES];
    auto setup = [&](const int *q) {
        P.clear();
        for (int j = 0; j < T.np; j++) {
            P.b[SQ64[q[j]]] = T.pc[j];
            if (isRoyal(ptype(T.pc[j]))) P.ksq[pcolor(T.pc[j])] = SQ64[q[j]];
        }
    };
    for (size_t idx = 0; idx < T.N; idx++) {
        T.decode(idx, sq);
        bool clash = false;
        for (int a = 0; a < T.np && !clash; a++) {
            if (!tbxSquareOk(T.pc[a], sq[a])) { clash = true; break; }
            for (int b = a + 1; b < T.np; b++) if (sq[a] == sq[b]) { clash = true; break; }
        }
        if (clash) continue;
        setup(sq);
        for (int side = 0; side < 2; side++) {
            P.side = side;
            if (P.attacked(P.ksq[side ^ 1], side)) continue;      // the side not to move is in check
            T.val[side][idx] = 0;
            P.hash = P.computeHash(); P.undo.clear(); P.hh.clear();
            int n = P.legalMoves(ml), quiet = 0, bestWin = 1000, worstLoss = 0;
            bool escape = false;
            if (n == 0) {
                if (P.inCheck(side) || R.stalemateLoss) { T.val[side][idx] = 2; buckets[0].push_back((uint64_t(idx) << 2) | (side << 1)); }
                else cnt[side][idx] = 255;                        // stalemate: a draw
                continue;
            }
            const bool chk = P.inCheck(side);
            for (int k = 0; k < n; k++) {
                if (!(FLAGS(ml[k]) & F_CAP) && !PROMO(ml[k])) { quiet++; continue; }   // stays in this ending
                P.make(ml[k]);
                vector<int> rest;
                for (int i = 0; i < 64; i++) if (P.b[SQ64[i]]) rest.push_back(P.b[SQ64[i]]);
                TbX &S = tbxSolve(rest, cache);
                int v = 0, d = 0;
                if (tbxSquares(P, S, sub)) { size_t j = S.index(sub); v = S.val[P.side][j]; d = S.dtm[P.side][j]; }
                P.unmake();
                if (v == 2) bestWin = min(bestWin, d + 1);           // the opponent is lost after this capture
                else if (v == 1) worstLoss = max(worstLoss, d + 1);  // a losing capture
                else escape = true;                                   // a capture into a drawn ending
            }
            T.quiet[side][idx] = !chk && quiet == n;
            if (bestWin < 1000) {                                     // a winning capture: never a loss
                cnt[side][idx] = 255;
                buckets[min(bestWin, 255)].push_back((uint64_t(idx) << 2) | (side << 1) | 1);
            } else if (escape) cnt[side][idx] = 255;
            else if (quiet == 0) {                                    // every move is a losing capture
                T.val[side][idx] = 2; T.dtm[side][idx] = uint8_t(min(worstLoss, 255));
                buckets[min(worstLoss, 255)].push_back((uint64_t(idx) << 2) | (side << 1));
            } else {
                cnt[side][idx] = uint8_t(quiet);                      // quiet moves not yet known to lose
                T.dtm[side][idx] = uint8_t(min(worstLoss, 255));      // the longest losing capture, if any
            }
        }
    }
    vector<int> from;
    for (int d = 0; d < 256; d++) {
        for (size_t e = 0; e < buckets[d].size(); e++) {
            uint64_t entry = buckets[d][e];
            size_t idx = size_t(entry >> 2);
            int side = int((entry >> 1) & 1);
            if (entry & 1) {                                          // a win by a capture
                if (T.val[side][idx] != 0) continue;                  // already won more quickly
                T.val[side][idx] = 1; T.dtm[side][idx] = uint8_t(d);
            }
            const int v = T.val[side][idx];
            if (v != 1 && v != 2) continue;
            T.decode(idx, sq);
            setup(sq);
            const int mover = side ^ 1;                               // the side that has just moved
            for (int j = 0; j < T.np; j++) {
                if (pcolor(T.pc[j]) != mover) continue;
                unmoves(P, ptype(T.pc[j]), mover, sq[j], from);
                const int keep = sq[j];
                for (int f : from) {
                    sq[j] = f;
                    const size_t y = T.index(sq);
                    uint8_t &pv = T.val[mover][y];
                    if (pv != 0) continue;                              // illegal or already resolved
                    if (v == 2) {                                       // the side to move here is lost: the mover wins
                        pv = 1; T.dtm[mover][y] = uint8_t(min(d + 1, 255));
                        buckets[min(d + 1, 255)].push_back((uint64_t(y) << 2) | (mover << 1));
                    } else if (cnt[mover][y] != 255 && cnt[mover][y] > 0 && --cnt[mover][y] == 0) {
                        const int dd = min(max(d + 1, int(T.dtm[mover][y])), 255);   // the longest resistance
                        pv = 2; T.dtm[mover][y] = uint8_t(dd);
                        buckets[dd].push_back((uint64_t(y) << 2) | (mover << 1));
                    }
                }
                sq[j] = keep;
            }
        }
        vector<uint64_t>().swap(buckets[d]);
    }
    return T;
}

static map<string, TbX> tbCache;                     // solved endings, reused by later commands

// keep the solved endings for later commands, but not too many four-piece ones (70 MB each)
static void tbTrimCache() {
    int four = 0;
    for (auto &kv : tbCache) four += kv.second.np == 4;
    if (four > 16)
        for (auto it = tbCache.begin(); it != tbCache.end(); ) it = it->second.np == 4 ? tbCache.erase(it) : next(it);
}

// "tbprobe": the exact result of the current position (at most four pieces), for the side to move
void tbProbe(const Pos &P0) {
    vector<int> pcs;
    int royals[2] = {0, 0}, pawns[2] = {0, 0};
    for (int i = 0; i < 64; i++) {
        const int p = P0.b[SQ64[i]];
        if (!p) continue;
        pcs.push_back(p);
        royals[pcolor(p)] += isRoyal(ptype(p));
        pawns[pcolor(p)] += ptype(p) == PAWN;
    }
    if (pcs.size() > 4 || royals[0] != 1 || royals[1] != 1 || (pawns[0] && pawns[1])) {
        printf("tbprobe none (only positions with at most 4 pieces, pawns on one side only)\n"); fflush(stdout); return;
    }
    TbX &T = tbxSolve(pcs, tbCache);
    int sq[4];
    const int v = tbxSquares(P0, T, sq) ? T.val[P0.side][T.index(sq)] : 3, d = v == 3 ? 0 : T.dtm[P0.side][T.index(sq)];
    if (v == 1) printf("tbprobe win mate in %d\n", (d + 1) / 2);
    else if (v == 2) printf("tbprobe loss mated in %d\n", d / 2);
    else if (v == 0) printf("tbprobe draw\n");
    else printf("tbprobe none (not a position of the tables)\n");
    fflush(stdout);
    tbTrimCache();
}

void tbCommand(const string &white, const string &black) {
    auto typeOf = [](char ch) { const char *p = strchr(PCHARS + 1, toupper((unsigned char)ch)); return p && *p ? int(p - PCHARS) : 0; };
    vector<int> pcs;
    int royals[2] = {0, 0}, pawns[2] = {0, 0};
    for (int c = 0; c < 2; c++)
        for (char ch : (c == WHITE ? white : black)) {
            const int t = typeOf(ch);
            if (!t) { printf("error unknown piece letter %c\n", ch); fflush(stdout); return; }
            royals[c] += isRoyal(t);
            pawns[c] += t == PAWN;
            pcs.push_back(mk(c, t));
        }
    if (royals[0] != 1 || royals[1] != 1 || pcs.size() > 4 || (pawns[0] && pawns[1])) {
        printf("error usage: tb <white pieces> <black pieces>, with one king or general each, at most 4 pieces "
               "and pawns on one side only, e.g. tb GA k\n");
        fflush(stdout); return;
    }
    auto t0 = chrono::steady_clock::now();
    TbX &T = tbxSolve(pcs, tbCache);
    double sec = chrono::duration<double>(chrono::steady_clock::now() - t0).count();
    const char *w = white.c_str(), *bl = black.c_str();
    auto pct = [](long long a, long long n) { return n ? 100.0 * a / n : 0.0; };
    for (int side = 0; side < 2; side++) {
        long long legal[2] = {0, 0}, won[2] = {0, 0}, lost[2] = {0, 0};     // all positions, quiet positions
        int longest = 0;
        for (size_t i = 0; i < T.N; i++) {
            const int v = T.val[side][i];
            if (v == 3) continue;
            for (int q = 0; q < 2; q++) {
                if (q && !T.quiet[side][i]) continue;
                legal[q]++; won[q] += v == 1; lost[q] += v == 2;
            }
            if (v == 1 || v == 2) longest = max(longest, int(T.dtm[side][i]));
        }
        for (int q = 0; q < 2; q++) {
            const long long n = legal[q], ww = side == WHITE ? won[q] : lost[q], bw = side == WHITE ? lost[q] : won[q];
            if (!q)
                printf("tb %s vs %s, %s to move: White wins %.1f%%, Black wins %.1f%%, draws %.1f%%, longest forced mate %d moves (%lld positions, %.0fs)\n",
                       w, bl, side == WHITE ? "White" : "Black", pct(ww, n), pct(bw, n), pct(n - ww - bw, n), (longest + 1) / 2, n, sec);
            else    // the side to move is not in check and has no capture or promotion: what an evaluation sees
                printf("tb %s vs %s, %s to move, quiet positions: White wins %.1f%%, Black wins %.1f%%, draws %.1f%% (%lld positions)\n",
                       w, bl, side == WHITE ? "White" : "Black", pct(ww, n), pct(bw, n), pct(n - ww - bw, n), n);
        }
    }
    // with one pawn or soldier: the quiet positions again, by the rank it stands on
    int pj = -1, npawns = 0;
    for (int j = 0; j < T.np; j++) if (ptype(T.pc[j]) == PAWN || ptype(T.pc[j]) == SOLDIER) { pj = j; npawns++; }
    if (npawns == 1) {
        long long n[8][2] = {}, ww[8][2] = {}, bw[8][2] = {};
        const int shift = 6 * (T.np - 1 - pj);
        for (int side = 0; side < 2; side++)
            for (size_t i = 0; i < T.N; i++) {
                const int v = T.val[side][i];
                if (v == 3 || !T.quiet[side][i]) continue;
                const int r = int((i >> shift) & 63) / 8;
                n[r][side]++;
                ww[r][side] += (v == 1 && side == WHITE) || (v == 2 && side == BLACK);
                bw[r][side] += (v == 1 && side == BLACK) || (v == 2 && side == WHITE);
            }
        for (int r = 0; r < 8; r++) {
            if (!n[r][0] && !n[r][1]) continue;
            printf("tb %s vs %s, %s on rank %d, quiet positions: White to move: White wins %.1f%%, Black wins %.1f%%, draws %.1f%%; "
                   "Black to move: White wins %.1f%%, Black wins %.1f%%, draws %.1f%%\n", w, bl,
                   ptype(T.pc[pj]) == PAWN ? "pawn" : "soldier", r + 1,
                   pct(ww[r][0], n[r][0]), pct(bw[r][0], n[r][0]), pct(n[r][0] - ww[r][0] - bw[r][0], n[r][0]),
                   pct(ww[r][1], n[r][1]), pct(bw[r][1], n[r][1]), pct(n[r][1] - ww[r][1] - bw[r][1], n[r][1]));
        }
    }
    fflush(stdout);
    tbTrimCache();
}

// ---------------------------------------------------------------- main ------
bool parseBool(const string &v) { return v == "true" || v == "1" || v == "on" || v == "yes"; }

int main() {
    initZobrist(); initCastleMask(); initEval(); ttResize(32);
    Pos P; P.setFen(START_FEN);
    string line;
    std::thread searchThread;
    auto waitSearch = [&]() { if (searchThread.joinable()) searchThread.join(); };
    while (getline(cin, line)) {
        istringstream ss(line);
        string cmd; ss >> cmd;
        if (cmd.empty()) continue;
        if (cmd == "stop") { gStop = true; continue; }          // ends a running search early
        if (cmd == "isready") { printf("readyok\n"); fflush(stdout); continue; }
        waitSearch();                                           // other commands wait for the search
        if (cmd == "quit") break;
        else if (cmd == "position") {
            // Build the new position on the side; the current one only changes if it all parses.
            string what; ss >> what;
            string fen = START_FEN, tok;
            if (what == "fen") {
                fen.clear();
                while (ss >> tok && tok != "moves") fen += (fen.empty() ? "" : " ") + tok;
            } else if (what == "startpos") ss >> tok;
            else { printf("error position needs startpos or fen\n"); fflush(stdout); continue; }
            Pos NP;
            if (!NP.setFen(fen)) { printf("error bad fen\n"); fflush(stdout); continue; }
            bool ok = true;
            if (tok == "moves") {
                string mv;
                while (ss >> mv) {
                    Move m;
                    if (!parseMove(NP, mv, m)) { printf("error illegal move %s\n", mv.c_str()); ok = false; break; }
                    NP.make(m);
                }
            }
            if (ok) P = NP;
            fflush(stdout);
        } else if (cmd == "go") {
            Limits L; string tok;
            bool any = false;
            while (ss >> tok) {
                if (tok == "depth") { ss >> L.depth; any = true; }
                else if (tok == "nodes") { ss >> L.nodes; any = true; }
                else if (tok == "movetime") { ss >> L.movetime; any = true; }
                else if (tok == "multipv") ss >> L.multipv;
            }
            if (!any) L.movetime = 2000;
            L.depth = max(1, min(L.depth, MAXPLY - 10)); L.multipv = max(1, min(L.multipv, 256));
            L.nodes = max(0LL, L.nodes); L.movetime = max(0LL, L.movetime);
            string res; string why = gameOver(P, res);
            if (!why.empty()) { printf("info gameover %s %s\nbestmove 0000\n", res.c_str(), why.c_str()); fflush(stdout); continue; }
            gStop = false;
            searchThread = std::thread([&P, L]() {
                vector<Line> lines = think(P, L);
                printf("bestmove %s\n", lines.empty() ? "0000" : moveStr(lines[0].move).c_str());
                fflush(stdout);
            });
        } else if (cmd == "legal") {
            Move ml[MAXMOVES]; int n = P.legalMoves(ml);
            vector<string> v; for (int i = 0; i < n; i++) v.push_back(moveStr(ml[i]));
            sort(v.begin(), v.end());
            printf("legal"); for (auto &s : v) printf(" %s", s.c_str()); printf("\n"); fflush(stdout);
        } else if (cmd == "perft" || cmd == "divide") {
            int d = 1; ss >> d;
            d = max(0, min(d, 12));
            auto t0 = chrono::steady_clock::now();
            U64 tot = 0;
            if (cmd == "divide") {
                Move ml[MAXMOVES]; int n = P.legalMoves(ml);
                for (int i = 0; i < n; i++) { P.make(ml[i]); U64 c = perft(P, d - 1); P.unmake(); tot += c; printf("%s %llu\n", moveStr(ml[i]).c_str(), (unsigned long long)c); }
            } else tot = perft(P, d);
            double sec = chrono::duration<double>(chrono::steady_clock::now() - t0).count();
            printf("perft %d %llu (%.2fs)\n", d, (unsigned long long)tot, sec); fflush(stdout);
        } else if (cmd == "eval") {
            printf("eval %d\n", evaluate(P)); fflush(stdout);
        } else if (cmd == "d") {
            P.print(); fflush(stdout);
        } else if (cmd == "fen") {
            printf("fen %s\n", P.fen().c_str()); fflush(stdout);
        } else if (cmd == "setoption") {
            string name, v; ss >> name >> v;
            if (name == "HorseBlock") R.horseBlock = parseBool(v);
            else if (name == "ElephantEye") R.elephantEye = parseBool(v);
            else if (name == "StalemateLoss") R.stalemateLoss = parseBool(v);
            else if (name == "SoldierSideways") R.soldierSideways = parseBool(v);
            else if (name == "Snipers") R.snipers = parseBool(v);
            else if (name == "SoldierPromotion") {
                if (v == "none" || v == "false") R.soldierPromo = 0;
                else if (v == "western") R.soldierPromo = 2;
                else if (v == "xiangqi") R.soldierPromo = 1;
                else if (v == "any" || v == "true") R.soldierPromo = 3;
                else printf("error SoldierPromotion must be any, xiangqi, western or none\n");
            }
            else if (name == "Hash") ttResize(max(1, min(4096, atoi(v.c_str()))));
            else if (name == "Threads") gThreads = max(1, min(256, atoi(v.c_str())));
            else if (name == "Value") {
                int cp;
                const char *p = v.size() == 1 ? strchr(PCHARS + 1, toupper((unsigned char)v[0])) : nullptr;
                if (!p || !*p) printf("error unknown piece %s\n", v.c_str());
                else if (!(ss >> cp) || cp < 0 || cp > 20000) printf("error bad value for %s\n", v.c_str());
                else VAL[p - PCHARS] = cp;
            } else printf("error unknown option %s\n", name.c_str());
            // Rule changes alter the meaning of existing TT entries and solved endings.
            ttClear();
            tbCache.clear();
            fflush(stdout);
        } else if (cmd == "tbprobe") {
            if (R.snipers) { printf("tbprobe none (the snipers rule is not supported)\n"); fflush(stdout); }
            else tbProbe(P);
        } else if (cmd == "tb" || cmd == "tb4") {        // e.g. "tb GA k": exact solution of a small ending
            string w, bl; ss >> w >> bl;
            if (R.snipers) { printf("error %s does not support the snipers rule\n", cmd.c_str()); fflush(stdout); }
            else tbCommand(w, bl);
        } else if (cmd == "selfplay") {                  // runs in the background; 'stop' ends it
            gStop = false;
            string args = line.substr(line.find("selfplay") + 8);
            searchThread = std::thread([args]() { istringstream as(args); selfplay(as); });
        } else if (cmd == "isready") {
            printf("readyok\n"); fflush(stdout);
        } else {
            printf("error unknown command %s\n", cmd.c_str()); fflush(stdout);
        }
    }
    waitSearch();
    return 0;
}
