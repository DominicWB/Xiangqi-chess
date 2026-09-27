"""position_editor.py -- set up any position, modelled on the "Setup board" window of Scid vs. PC.

On the board:
    left click       place the chosen piece (clicking the same piece again removes it)
    right click      clear the square, and choose the piece that stood there
    middle click     choose the piece on that square without changing anything
    drag             move a piece; dragging it off the board removes it
The piece to place is chosen in the palette (both armies, both colours), with the mouse wheel, or
by typing its letter: upper case for White, lower case for Black.
Ctrl+Z undoes, Return is OK and Escape is Cancel.

Setup holds the position being built and has no tkinter code, so it can be tested on its own;
PositionEditor is the dialog.
"""
try:
    import tkinter as tk
    from tkinter import ttk
    Toplevel = tk.Toplevel
except ImportError:                                    # Setup works without tkinter (e.g. in tests)
    tk = ttk = None
    Toplevel = object

import pyengine as pe
from xqchess import PIECE_NAMES, SETUPS

FILES = "abcdefgh"
WESTERN_ROW, XIANGQI_ROW = "KQRBNP", "GAEHJCS"
# palette rows: (colour, piece letters)
PALETTE = [(pe.WHITE, WESTERN_ROW), (pe.WHITE, XIANGQI_ROW), (pe.BLACK, WESTERN_ROW), (pe.BLACK, XIANGQI_ROW)]
# every piece in palette order, for the mouse wheel
CHOICES = [pe.PCHARS.index(ch) | (c << 4) for c, row in PALETTE for ch in row]
CASTLING = (("K", "White O-O"), ("Q", "White O-O-O"), ("k", "Black O-O"), ("q", "Black O-O-O"))
PLURAL_ORDER = [pe.QUEEN, pe.CHARIOT, pe.ROOK, pe.CANNON, pe.HORSE, pe.KNIGHT, pe.BISHOP, pe.ELEPHANT, pe.ADVISOR,
                pe.PAWN, pe.SOLDIER]
LIGHT, DARK = "#ecd3b2", "#b38461"


def sq_name(sq):
    return FILES[sq % 8] + str(sq // 8 + 1)


def piece_name(p):
    """ "white general", "black pawn", ... for a piece code; "empty square" for 0."""
    if not p:
        return "empty square"
    return ("white " if p >> 4 == pe.WHITE else "black ") + PIECE_NAMES[pe.PCHARS[p & 15]]


def piece_letter(p):
    ch = pe.PCHARS[p & 15]
    return ch if p >> 4 == pe.WHITE else ch.lower()


class Setup:
    """A position under construction.  board holds 64 piece codes (colour << 4 | type, 0 = empty),
    indexed rank * 8 + file; castle is a set of the letters K Q k q; ep is a square index or None."""

    def __init__(self):
        self.board = [0] * 64
        self.side = pe.WHITE
        self.castle = set()
        self.ep = None
        self.half = 0
        self.full = 1

    def copy(self):
        s = Setup()
        s.board, s.side, s.castle, s.ep, s.half, s.full = self.board[:], self.side, set(self.castle), self.ep, \
            self.half, self.full
        return s

    def __eq__(self, other):
        return isinstance(other, Setup) and self.fen() == other.fen()

    # -------------------------------------------------------------------- FEN --
    @staticmethod
    def from_fen(text):
        """Read a FEN.  Any arrangement of pieces is accepted (the editor is for building positions);
        ValueError is raised only if the text cannot be read at all."""
        parts = text.split()
        if not parts:
            raise ValueError("empty FEN")
        rows = parts[0].split("/")
        if len(rows) != 8:
            raise ValueError("a FEN needs 8 ranks separated by /")
        s = Setup()
        for i, row in enumerate(rows):
            r, f = 7 - i, 0
            for ch in row:
                if ch.isdigit():
                    f += int(ch)
                elif ch.isalpha() and ch.upper() in pe.PCHARS[1:]:
                    if f < 8:
                        s.board[r * 8 + f] = pe.PCHARS.index(ch.upper()) | ((pe.WHITE if ch.isupper() else pe.BLACK) << 4)
                    f += 1
                else:
                    raise ValueError(f"unknown piece letter {ch!r}")
            if f != 8:
                raise ValueError(f"rank {8 - i} has {f} squares instead of 8")
        if len(parts) > 1:
            if parts[1] not in ("w", "b"):
                raise ValueError("the side to move must be w or b")
            s.side = pe.WHITE if parts[1] == "w" else pe.BLACK
        if len(parts) > 2 and parts[2] != "-":
            if any(ch not in "KQkq" for ch in parts[2]):
                raise ValueError("castling rights must be letters from KQkq, or -")
            s.castle = set(parts[2])
        if len(parts) > 3 and parts[3] != "-":
            e = parts[3]
            if len(e) != 2 or e[0] not in FILES or e[1] not in "12345678":
                raise ValueError("the en passant square must be like e3 or e6, or -")
            s.ep = FILES.index(e[0]) + 8 * (int(e[1]) - 1)
        for k, name in ((4, "half"), (5, "full")):
            if len(parts) > k:
                if not parts[k].isdigit():
                    raise ValueError("the move counters must be numbers")
                setattr(s, name, int(parts[k]))
        s.full = max(1, s.full)
        s.castle &= s.castling_possible()
        if s.ep not in s.ep_squares():
            s.ep = None
        return s

    def board_fen(self):
        rows = []
        for r in range(7, -1, -1):
            row, empty = "", 0
            for f in range(8):
                p = self.board[r * 8 + f]
                if not p:
                    empty += 1
                    continue
                if empty:
                    row += str(empty)
                    empty = 0
                row += piece_letter(p)
            rows.append(row + (str(empty) if empty else ""))
        return "/".join(rows)

    def fen(self):
        """The FEN, with only the castling rights and en passant square that the board allows."""
        castle = "".join(ch for ch in "KQkq" if ch in self.castle and ch in self.castling_possible()) or "-"
        ep = sq_name(self.ep) if self.ep is not None and self.ep in self.ep_squares() else "-"
        return f"{self.board_fen()} {'wb'[self.side]} {castle} {ep} {self.half} {self.full}"

    # ------------------------------------------------------------------ rules --
    def royals(self, c):
        """Squares of colour c's kings and generals."""
        return [sq for sq, p in enumerate(self.board) if p and p >> 4 == c and (p & 15) in pe.ROYAL]

    def royal_type(self, c):
        rs = self.royals(c)
        return self.board[rs[0]] & 15 if len(rs) == 1 else None

    def castling_possible(self):
        """Castling rights the board allows: a king on e1/e8 and a rook in the corner."""
        out = set()
        for c, r, k, q in ((pe.WHITE, 0, "K", "Q"), (pe.BLACK, 7, "k", "q")):
            if self.board[r * 8 + 4] == pe.KING | (c << 4):
                if self.board[r * 8 + 7] == pe.ROOK | (c << 4):
                    out.add(k)
                if self.board[r * 8] == pe.ROOK | (c << 4):
                    out.add(q)
        return out

    def ep_squares(self):
        """Possible en passant squares: the side that just moved has a pawn that may have come two
        squares from its first rank, with the square it passed and its starting square empty."""
        out = []
        mover = self.side ^ 1
        pawn = pe.PAWN | (mover << 4)
        r_pawn, r_ep, r_start = (3, 2, 1) if mover == pe.WHITE else (4, 5, 6)
        for f in range(8):
            if self.board[r_pawn * 8 + f] == pawn and not self.board[r_ep * 8 + f] and not self.board[r_start * 8 + f]:
                out.append(r_ep * 8 + f)
        return out

    def place(self, sq, p):
        """Put piece p (0 = clear) on square sq.  A second royal of the same colour replaces the
        first.  Returns an error message if the piece cannot stand there, else ""."""
        if p and (p & 15) == pe.PAWN and sq // 8 in (0, 7):
            return "A pawn cannot stand on the first or last rank."
        if p and (p & 15) in pe.ROYAL:
            for other in self.royals(p >> 4):
                self.board[other] = 0
        self.board[sq] = p
        return ""

    def _attacked_royals(self):
        """Which colours' royals are attacked, under the current rule options (pyengine.R)."""
        P = pe.Pos()
        P.b = [pe.OFFB] * 144
        for s in pe.SQ64:
            P.b[s] = pe.EMPTY
        for sq, p in enumerate(self.board):
            if p:
                P.b[pe.SQ(sq % 8, sq // 8)] = p
        out = []
        for c in (pe.WHITE, pe.BLACK):
            (k,) = self.royals(c)
            out.append(P.attacked(pe.SQ(k % 8, k // 8), c ^ 1))
        return out

    def problems(self):
        """Why this is not a legal position, as short sentences; [] if it is."""
        out = []
        for c, name in ((pe.WHITE, "White"), (pe.BLACK, "Black")):
            n = len(self.royals(c))
            if n == 0:
                out.append(f"{name} needs a king or a general.")
            elif n > 1:
                out.append(f"{name} has more than one king or general.")
        pawns = [sq_name(sq) for sq, p in enumerate(self.board) if p and (p & 15) == pe.PAWN and sq // 8 in (0, 7)]
        if pawns:
            out.append(f"A pawn cannot stand on the first or last rank ({', '.join(pawns)}).")
        if out:
            return out
        attacked = self._attacked_royals()
        waiting = self.side ^ 1
        if attacked[waiting]:
            names = ("White", "Black")
            out.append(f"{names[waiting]} is in check, but it is {names[self.side]}'s move.")
        elif not pe.Pos().set_fen(self.fen()):
            out.append("This is not a legal position.")
        return out

    def material(self):
        """E.g. "White: general, advisor, 2 soldiers.  Black: king." """
        text = []
        for c, name in ((pe.WHITE, "White"), (pe.BLACK, "Black")):
            counts = [0] * pe.NTYPES
            for p in self.board:
                if p and p >> 4 == c:
                    counts[p & 15] += 1
            words = [PIECE_NAMES[pe.PCHARS[t]] for t in pe.ROYAL for _ in range(counts[t])]
            for t in PLURAL_ORDER:
                n = counts[t]
                if n:
                    words.append(PIECE_NAMES[pe.PCHARS[t]] if n == 1 else f"{n} {PIECE_NAMES[pe.PCHARS[t]]}s")
            text.append(f"{name}: {', '.join(words) if words else 'nothing'}.")
        return "  ".join(text)

    # ------------------------------------------------------------ transforms --
    def swapped(self):
        """The same position with the colours exchanged: the board turned upside down, every
        piece changing colour, and the side to move and castling rights exchanged too."""
        s = Setup()
        for sq, p in enumerate(self.board):
            if p:
                s.board[(7 - sq // 8) * 8 + sq % 8] = (p & 15) | ((p >> 4 ^ 1) << 4)
        s.side = self.side ^ 1
        s.castle = {ch.swapcase() for ch in self.castle}
        s.ep = None if self.ep is None else (7 - self.ep // 8) * 8 + self.ep % 8
        s.half, s.full = self.half, self.full
        return s

    def mirrored(self):
        """The board reflected left to right (castling rights are lost: kings and rooks move)."""
        s = self.copy()
        s.board = [self.board[(sq // 8) * 8 + 7 - sq % 8] for sq in range(64)]
        s.ep = None if self.ep is None else (self.ep // 8) * 8 + 7 - self.ep % 8
        s.castle &= s.castling_possible()
        return s

    def emptied(self):
        """Only the royals left (a missing one comes back: a general on e1 for White, a king on e8
        for Black, as in the starting position)."""
        s = self.copy()
        s.board = [p if p and (p & 15) in pe.ROYAL else 0 for p in self.board]
        for c, t, home in ((pe.WHITE, pe.GENERAL, 4), (pe.BLACK, pe.KING, 60)):
            if not s.royals(c):
                sq = home if not s.board[home] else next(q for q in range(64) if not s.board[q])
                s.board[sq] = t | (c << 4)
        s.castle, s.ep = set(), None
        return s


# ------------------------------------------------------------------ dialog --
class PositionEditor(Toplevel):
    """The editing window.  on_ok(fen) is called with the new position when OK is pressed."""

    recent = []                           # FENs used this session, newest first (for the FEN box)

    def __init__(self, master, art, fen, style="pictures", flipped=False, square=60, on_ok=None, snipers=False):
        super().__init__(master)
        self.withdraw()
        self.title("Edit position")
        self.transient(master)
        self.art, self.style, self.flipped, self.sq, self.on_ok = art, style, flipped, square, on_ok
        self.snipers = snipers                         # the snipers rule: White's bishops are drawn as snipers
        self.ps = max(34, int(square * 0.72))          # palette cell size
        self.m = 20                                    # board margin for the coordinates
        try:
            self.setup = Setup.from_fen(fen)
        except ValueError:
            self.setup = Setup().emptied()
        self.undo_stack = []
        self.selected = pe.PAWN | (pe.WHITE << 4)
        self.press = None                              # (square, x, y) of a left-button press
        self.dragging = False
        self.aqua = self.tk.call("tk", "windowingsystem") == "aqua"
        self._build()
        self._refresh()
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.deiconify()
        self.update_idletasks()
        # centre on the main window
        x = master.winfo_rootx() + max(0, (master.winfo_width() - self.winfo_reqwidth()) // 2)
        y = master.winfo_rooty() + max(0, (master.winfo_height() - self.winfo_reqheight()) // 3)
        self.geometry(f"+{x}+{y}")
        self._grab()
        self.board_canvas.focus_set()

    def _grab(self):
        try:
            self.grab_set()                            # modal: the main window waits
        except tk.TclError:                            # not mapped yet
            self.after(50, self._grab)

    # ------------------------------------------------------------- widgets --
    def _build(self):
        pad = ttk.Frame(self, padding=8)
        pad.grid(sticky="nsew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        size = 8 * self.sq + 2 * self.m
        self.board_canvas = tk.Canvas(pad, width=size, height=size, highlightthickness=0, bg="#f7f1e6")
        self.board_canvas.grid(row=0, column=0, rowspan=2, sticky="nw")
        hint = ("Left click: place the chosen piece (again: remove it)    Right click: clear\n"
                "Middle click: choose the piece on a square    Drag: move a piece (off the board: remove)\n"
                "Mouse wheel or letter keys choose the piece: upper case White, lower case Black")
        ttk.Label(pad, text=hint, foreground="#666", justify="left").grid(row=2, column=0, sticky="w", pady=(4, 0))

        right = ttk.Frame(pad, padding=(12, 0, 0, 0))
        right.grid(row=0, column=1, sticky="nw")
        ttk.Label(right, text="Pieces", font=("TkDefaultFont", 10, "bold")).grid(row=0, column=0, sticky="w")
        lw = 46                                         # room for the row labels
        pw, ph = lw + 7 * self.ps + 4, 4 * self.ps + 8 + 4
        self.palette = tk.Canvas(right, width=pw, height=ph, highlightthickness=0, bg="#f7f1e6")
        self.palette.grid(row=1, column=0, sticky="w", pady=(2, 2))
        self.palette_lw = lw
        self.chosen_label = ttk.Label(right, text="")
        self.chosen_label.grid(row=2, column=0, sticky="w")

        opts = ttk.Frame(right)
        opts.grid(row=3, column=0, sticky="w", pady=(10, 0))
        ttk.Label(opts, text="Side to move").grid(row=0, column=0, sticky="w")
        self.v_side = tk.StringVar(value="w")
        sf = ttk.Frame(opts)
        sf.grid(row=0, column=1, columnspan=3, sticky="w", padx=(8, 0))
        for i, (val, lab) in enumerate((("w", "White"), ("b", "Black"))):
            ttk.Radiobutton(sf, text=lab, value=val, variable=self.v_side, command=self._options_changed).grid(
                row=0, column=i, padx=(0, 8))
        ttk.Label(opts, text="Castling").grid(row=1, column=0, sticky="nw", pady=(6, 0))
        cf = ttk.Frame(opts)
        cf.grid(row=1, column=1, columnspan=3, sticky="w", padx=(8, 0), pady=(6, 0))
        self.v_castle, self.castle_buttons = {}, {}
        for i, (flag, lab) in enumerate(CASTLING):
            v = tk.BooleanVar(value=False)
            b = ttk.Checkbutton(cf, text=lab, variable=v, command=self._options_changed)
            b.grid(row=i // 2, column=i % 2, sticky="w", padx=(0, 8))
            self.v_castle[flag], self.castle_buttons[flag] = v, b
        ttk.Label(opts, text="En passant").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.v_ep = tk.StringVar(value="-")
        self.ep_box = ttk.Combobox(opts, textvariable=self.v_ep, width=4, state="readonly", values=["-"])
        self.ep_box.grid(row=2, column=1, sticky="w", padx=(8, 0), pady=(6, 0))
        self.ep_box.bind("<<ComboboxSelected>>", lambda e: self._options_changed())
        ttk.Label(opts, text="Half-move clock").grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.v_half = tk.StringVar(value="0")
        ttk.Spinbox(opts, from_=0, to=999, width=5, textvariable=self.v_half, command=self._options_changed).grid(
            row=3, column=1, sticky="w", padx=(8, 0), pady=(6, 0))
        ttk.Label(opts, text="Move number").grid(row=4, column=0, sticky="w", pady=(6, 0))
        self.v_full = tk.StringVar(value="1")
        ttk.Spinbox(opts, from_=1, to=9999, width=5, textvariable=self.v_full, command=self._options_changed).grid(
            row=4, column=1, sticky="w", padx=(8, 0), pady=(6, 0))
        for v in (self.v_half, self.v_full):
            v.trace_add("write", lambda *a: self.after_idle(self._options_changed))

        btns = ttk.Frame(right)
        btns.grid(row=4, column=0, sticky="w", pady=(12, 0))
        start = ttk.Menubutton(btns, text="Start position")
        menu = tk.Menu(start, tearoff=0)
        for name, fen in SETUPS.items():
            menu.add_command(label=name, command=lambda f=fen: self.load_fen(f))
        start["menu"] = menu
        for i, (w, tip) in enumerate((
                (ttk.Button(btns, text="Empty board", command=lambda: self._change(self.setup.emptied())), None),
                (start, None),
                (ttk.Button(btns, text="Swap colours", command=lambda: self._change(self.setup.swapped())), None),
                (ttk.Button(btns, text="Mirror left-right", command=lambda: self._change(self.setup.mirrored())), None),
                (ttk.Button(btns, text="Flip view", command=self.flip), None),
                (ttk.Button(btns, text="Undo", command=self.undo), None))):
            w.grid(row=i // 2, column=i % 2, sticky="ew", padx=(0, 6), pady=2)

        fenrow = ttk.Frame(pad)
        fenrow.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        fenrow.columnconfigure(1, weight=1)
        ttk.Label(fenrow, text="FEN").grid(row=0, column=0, padx=(0, 6))
        self.v_fen = tk.StringVar()
        self.fen_box = ttk.Combobox(fenrow, textvariable=self.v_fen, values=PositionEditor.recent)
        self.fen_box.grid(row=0, column=1, sticky="ew")
        self.fen_box.bind("<Return>", lambda e: (self.load_fen(self.v_fen.get()), "break")[1])
        self.fen_box.bind("<<ComboboxSelected>>", lambda e: self.load_fen(self.v_fen.get()))
        ttk.Button(fenrow, text="Set", width=5, command=lambda: self.load_fen(self.v_fen.get())).grid(
            row=0, column=2, padx=(4, 0))
        ttk.Button(fenrow, text="Copy", width=6, command=self.copy_fen).grid(row=0, column=3, padx=(4, 0))
        ttk.Button(fenrow, text="Paste", width=6, command=self.paste_fen).grid(row=0, column=4, padx=(4, 0))

        bottom = ttk.Frame(pad)
        bottom.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        bottom.columnconfigure(0, weight=1)
        self.status = ttk.Label(bottom, text="", wraplength=560, justify="left")
        self.status.grid(row=0, column=0, sticky="w")
        self.ok_button = ttk.Button(bottom, text="OK", width=9, command=self.ok)
        self.ok_button.grid(row=0, column=1, padx=(8, 4))
        ttk.Button(bottom, text="Cancel", width=9, command=self.cancel).grid(row=0, column=2)

        c = self.board_canvas
        c.bind("<ButtonPress-1>", self._press)
        c.bind("<B1-Motion>", self._motion)
        c.bind("<ButtonRelease-1>", self._release)
        right_btn, middle_btn = ("<Button-2>", "<Button-3>") if self.aqua else ("<Button-3>", "<Button-2>")
        c.bind(right_btn, self._right_click)
        c.bind(middle_btn, self._middle_click)
        if self.aqua:
            c.bind("<Control-Button-1>", self._right_click)
        self.palette.bind("<Button-1>", self._palette_click)
        for w in (c, self.palette):
            w.bind("<MouseWheel>", lambda e: self._cycle(-1 if e.delta > 0 else 1))
            w.bind("<Button-4>", lambda e: self._cycle(-1))
            w.bind("<Button-5>", lambda e: self._cycle(1))
        self.bind("<Key>", self._key)
        self.bind("<Escape>", lambda e: self.cancel())
        self.bind("<Return>", self._return_key)
        self.bind("<Control-z>", lambda e: self.undo())
        self.bind("<Control-Z>", lambda e: self.undo())

    # ------------------------------------------------------------ geometry --
    def _xy(self, sq):
        f, r = sq % 8, sq // 8
        if self.flipped:
            f, r = 7 - f, 7 - r
        return self.m + f * self.sq, self.m + (7 - r) * self.sq

    def _square_at(self, x, y):
        f, r = (x - self.m) // self.sq, 7 - (y - self.m) // self.sq
        if not (0 <= f < 8 and 0 <= r < 8) or x < self.m or y < self.m:
            return None
        if self.flipped:
            f, r = 7 - f, 7 - r
        return r * 8 + f

    def _palette_cells(self):
        """(x, y, choice) of every palette cell."""
        out = []
        for i, (c, row) in enumerate(PALETTE):
            y = 2 + i * self.ps + (8 if i >= 2 else 0)
            for j, ch in enumerate(row):
                out.append((self.palette_lw + j * self.ps, y, pe.PCHARS.index(ch) | (c << 4)))
        return out

    # ------------------------------------------------------------- drawing --
    def _sniper(self, p):
        return self.snipers and p == pe.BISHOP | (pe.WHITE << 4)

    def _draw_board(self, hide=None):
        c, sq, s = self.board_canvas, self.sq, self.setup
        c.delete("all")
        for q in range(64):
            x, y = self._xy(q)
            c.create_rectangle(x, y, x + sq, y + sq, fill=LIGHT if (q % 8 + q // 8) % 2 else DARK, outline="")
        for i in range(8):
            fx, _ = self._xy(i)
            c.create_text(fx + sq / 2, self.m + 8 * sq + self.m / 2, text=FILES[i], fill="#555")
            _, ry = self._xy(i * 8)
            c.create_text(self.m / 2, ry + sq / 2, text=str(i + 1), fill="#555")
        royal = [s.royal_type(pe.WHITE), s.royal_type(pe.BLACK)]
        for q, p in enumerate(s.board):
            if p and q != hide:
                x, y = self._xy(q)
                self.art.draw(c, x + sq / 2, y + sq / 2, sq, p & 15, p >> 4, self.style, royal[p >> 4],
                              sniper=self._sniper(p))

    def _draw_palette(self):
        c, ps = self.palette, self.ps
        c.delete("all")
        for i, label in ((0, "White"), (2, "Black")):
            c.create_text(4, 2 + i * ps + (8 if i >= 2 else 0) + ps, text=label, anchor="w", fill="#555",
                          font=("TkDefaultFont", 9, "bold"))
        for x, y, choice in self._palette_cells():
            chosen = choice == self.selected
            c.create_rectangle(x + 1, y + 1, x + ps - 1, y + ps - 1, fill="#f4e36b" if chosen else "#e9dcc5",
                               outline="#b08d3a" if chosen else "#d8c8ad", width=2 if chosen else 1)
            # every palette piece is captioned with its letter, so no look-alike badges here
            self.art.draw(c, x + ps / 2, y + ps / 2, ps - 6, choice & 15, choice >> 4, self.style, badges=False,
                          sniper=self._sniper(choice))
            c.create_text(x + ps - 3, y + ps - 2, text=piece_letter(choice), anchor="se", fill="#6b5a45",
                          font=("TkDefaultFont", max(7, ps // 6), "bold"))
        name = piece_name(self.selected) + (" (a sniper)" if self._sniper(self.selected) else "")
        self.chosen_label.config(text=f"Placing: {name}  (key {piece_letter(self.selected)})")

    # --------------------------------------------------------------- state --
    def _change(self, new_setup):
        """Replace the position (keeping the old one for Undo) and show it."""
        if new_setup != self.setup:
            self.undo_stack.append(self.setup.copy())
            del self.undo_stack[:-200]
        self.setup = new_setup
        self._refresh()

    def _refresh(self, fen_box=True):
        """Show the position: board, options, FEN and whether it is legal."""
        s = self.setup
        s.castle &= s.castling_possible()
        if s.ep is not None and s.ep not in s.ep_squares():
            s.ep = None
        self.v_side.set("wb"[s.side])
        possible = s.castling_possible()
        for flag, _ in CASTLING:
            self.v_castle[flag].set(flag in s.castle)
            self.castle_buttons[flag].state(["!disabled"] if flag in possible else ["disabled"])
        self.ep_box.config(values=["-"] + [sq_name(q) for q in s.ep_squares()])
        self.v_ep.set(sq_name(s.ep) if s.ep is not None else "-")
        if self.v_half.get() != str(s.half):
            self.v_half.set(str(s.half))
        if self.v_full.get() != str(s.full):
            self.v_full.set(str(s.full))
        if fen_box:
            self.v_fen.set(s.fen())
        problems = s.problems()
        if problems:
            self.status.config(text=" ".join(problems), foreground="#b3261e")
            self.ok_button.state(["disabled"])
        else:
            self.status.config(text=s.material(), foreground="#2d6a2d")
            self.ok_button.state(["!disabled"])
        self._draw_board()
        self._draw_palette()

    def _options_changed(self):
        """Side to move, castling, en passant or the counters were changed in their widgets."""
        new = self.setup.copy()
        new.side = pe.WHITE if self.v_side.get() == "w" else pe.BLACK
        new.castle = {flag for flag, _ in CASTLING if self.v_castle[flag].get()}
        ep = self.v_ep.get()
        new.ep = None if ep in ("", "-") else FILES.index(ep[0]) + 8 * (int(ep[1]) - 1)
        for name, var, low in (("half", self.v_half, 0), ("full", self.v_full, 1)):
            try:
                setattr(new, name, max(low, int(var.get())))
            except (ValueError, tk.TclError):
                pass                                   # half-typed: keep the old number for now
        if new.side != self.setup.side:
            new.ep = None                              # an e.p. square belongs to one side to move
        self._change(new)

    # --------------------------------------------------------------- mouse --
    def _put(self, sq, p):
        new = self.setup.copy()
        err = new.place(sq, p)
        if err:
            self.bell()
            self.status.config(text=err, foreground="#b3261e")
            return
        self._change(new)

    def _press(self, e):
        self.board_canvas.focus_set()
        sq = self._square_at(e.x, e.y)
        self.press = None if sq is None else (sq, e.x, e.y)
        self.dragging = False

    def _motion(self, e):
        if not self.press:
            return
        sq, x0, y0 = self.press
        p = self.setup.board[sq]
        if not p:
            return
        if not self.dragging:
            if abs(e.x - x0) + abs(e.y - y0) < 5:
                return
            self.dragging = True
            self._draw_board(hide=sq)
            self.art.draw(self.board_canvas, e.x, e.y, self.sq, p & 15, p >> 4, self.style,
                          self.setup.royal_type(p >> 4), tags=("drag",), sniper=self._sniper(p))
            self.drag_xy = (e.x, e.y)
            return
        self.board_canvas.move("drag", e.x - self.drag_xy[0], e.y - self.drag_xy[1])
        self.drag_xy = (e.x, e.y)

    def _release(self, e):
        if not self.press:
            return
        sq = self.press[0]
        self.press = None
        if not self.dragging:                          # a click: place the chosen piece, or remove it
            p = 0 if self.setup.board[sq] == self.selected else self.selected
            self._put(sq, p)
            return
        self.dragging = False
        target = self._square_at(e.x, e.y)
        p = self.setup.board[sq]
        new = self.setup.copy()
        new.board[sq] = 0
        if target is not None:
            err = new.place(target, p)
            if err:
                self.bell()
                self._refresh()
                self.status.config(text=err, foreground="#b3261e")
                return
        self._change(new)

    def _right_click(self, e):
        sq = self._square_at(e.x, e.y)
        if sq is None:
            return
        p = self.setup.board[sq]
        if p:
            self.selected = p
            self._put(sq, 0)

    def _middle_click(self, e):
        sq = self._square_at(e.x, e.y)
        if sq is not None and self.setup.board[sq]:
            self.selected = self.setup.board[sq]
            self._draw_palette()

    def _palette_click(self, e):
        for x, y, choice in self._palette_cells():
            if x <= e.x < x + self.ps and y <= e.y < y + self.ps:
                self.selected = choice
                self._draw_palette()
                return

    def _cycle(self, step):
        i = CHOICES.index(self.selected) if self.selected in CHOICES else 0
        self.selected = CHOICES[(i + step) % len(CHOICES)]
        self._draw_palette()

    def _typing(self):
        w = self.focus_get()
        return w is not None and w.winfo_class() in ("TEntry", "Entry", "TSpinbox", "Spinbox", "TCombobox")

    def _return_key(self, _e):
        if self._typing():                            # Return in a number box just applies it
            self._options_changed()
            self.board_canvas.focus_set()
        else:
            self.ok()

    def _key(self, e):
        if self._typing():
            return
        ch = e.char
        if ch and ch.isalpha() and ch.upper() in pe.PCHARS[1:]:
            self.selected = pe.PCHARS.index(ch.upper()) | ((pe.WHITE if ch.isupper() else pe.BLACK) << 4)
        else:
            return
        self._draw_palette()

    # ------------------------------------------------------------- actions --
    def load_fen(self, text):
        try:
            new = Setup.from_fen(text.strip())
        except ValueError as err:
            self.bell()
            self.status.config(text=f"Cannot read that FEN: {err}.", foreground="#b3261e")
            return False
        self._change(new)
        return True

    def copy_fen(self):
        self.clipboard_clear()
        self.clipboard_append(self.setup.fen())

    def paste_fen(self):
        try:
            text = self.clipboard_get()
        except tk.TclError:
            self.bell()
            return
        self.v_fen.set(text.strip())
        self.load_fen(text)

    def flip(self):
        self.flipped = not self.flipped
        self._draw_board()

    def undo(self):
        if self.undo_stack:
            self.setup = self.undo_stack.pop()
            self._refresh()

    def ok(self):
        if self.setup.problems():
            self.bell()
            return
        fen = self.setup.fen()
        if fen in PositionEditor.recent:
            PositionEditor.recent.remove(fen)
        PositionEditor.recent.insert(0, fen)
        del PositionEditor.recent[20:]
        self.grab_release()
        self.destroy()
        if self.on_ok:
            self.on_ok(fen)

    def cancel(self):
        self.grab_release()
        self.destroy()
