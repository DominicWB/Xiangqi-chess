#!/usr/bin/env python3
"""gui.py -- click moves on a board and watch the engine's best moves update live.

    python gui.py                       # default setup; compiled engine if available, else Python
    python gui.py --setup noqueen
    python gui.py --fen "rnb1kbnr/pppppppp/8/8/8/S2SS2S/1C4C1/JHEAGEHJ w kq - 0 1"
    python gui.py --engine python       # force the pure-Python engine (no compiler needed)
    python gui.py --square 60           # smaller board
    python gui.py --load mygame.pgn     # open a saved game (Game menu: Open/Save game)
    python gui.py --setup snipers       # snipers chess: White's bishops can also take without moving

From a Jupyter notebook, %run gui.py (with the same options) opens the board as a program of its
own: the cell finishes at once, and every start reads the current files, so an update needs no
kernel restart.  Add --in-process to run it inside the kernel instead.

Click a piece, then click where it should go (legal targets are marked).  The panel on the
right lists the engine's best moves for the position on the board, best first, with the
evaluation from White's point of view; double-click a line to play its first move.  The bar left
of the board shows the evaluation at a glance, and Game > Edit position (Ctrl+E) sets up any
position.  Only needs the Python standard library (tkinter ships with the python.org installers).
"""
import argparse
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time


def launch(argv=(), wait=3.0):
    """Open the analysis board as a program of its own and return its process, for example from a
    Jupyter notebook: the call returns at once, the window does not depend on the notebook's kernel,
    and each start reads the current files.  argv holds gui.py's options, e.g. ["--setup", "snipers"].
    If the board stops within `wait` seconds, its messages are printed."""
    log = os.path.join(tempfile.gettempdir(), "xqchess-gui.log")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with open(log, "w") as fh:
        p = subprocess.Popen([sys.executable, os.path.abspath(__file__)] + list(argv), stdin=subprocess.DEVNULL,
                             stdout=fh, stderr=subprocess.STDOUT, creationflags=flags)
    end = time.time() + wait
    while time.time() < end and p.poll() is None:
        time.sleep(0.1)
    if p.poll() is None:
        print(f"The analysis board is opening in a window of its own (its messages go to {log}).")
    else:
        with open(log) as fh:
            print(fh.read())
        print(f"The analysis board did not start (exit code {p.returncode}); its messages are above.")
    return p


if __name__ == "__main__" and "ipykernel" in sys.modules and "--in-process" not in sys.argv:
    # %run gui.py in a Jupyter notebook: a Tk window run by the kernel keeps the cell busy, may open
    # behind the browser, and would use modules the kernel imported before an update
    launch(sys.argv[1:])
    sys.exit(0)                         # (%run ignores a zero exit status)

import tkinter as tk                                                # noqa: E402
from tkinter import filedialog, messagebox, simpledialog, ttk       # noqa: E402

import fairy_engine                                                 # noqa: E402
import gamefile                                                     # noqa: E402

import pyengine as pe                                               # noqa: E402
from pieceart import PieceArt                                       # noqa: E402
from position_editor import PositionEditor                          # noqa: E402
from xqchess import ENGINE_KINDS, ENGINE_LABELS, FAIRY_PATH, PIECE_NAMES, SETUP_RULES, SETUPS, \
    build_fairy, engine_command, engine_kind, fairy_available       # noqa: E402

LIGHT, DARK = "#ecd3b2", "#b38461"
BOARD_BG = "#f7f1e6"
SEL, LASTMOVE, CHECK = "#f4e36b", "#cfd67a", "#e0625a"
ARROW = "#2f8f3a"
INFO_RE = re.compile(r"info depth (\d+) multipv (\d+) score (cp|mate) (-?\d+) nodes (\d+) time (\d+) nps (\d+) pv ?(.*)")


# ------------------------------------------------------------ engine process --
class EngineProc:
    """An engine subprocess searching in the background.  A search that is no longer wanted
    is ended with 'stop' and its remaining output is skipped.  If the engine does not answer
    a stop within a few seconds it is restarted."""

    def __init__(self, kind, rule_cmds):
        self.kind, self.rule_cmds = kind, list(rule_cmds)
        self.q = queue.Queue()
        self.gen = 0
        self.p = None
        self.cmd = engine_command(kind)
        self.kind = engine_kind(self.cmd)
        self.label = ENGINE_LABELS[self.kind]
        self._start()

    def _start(self):
        self.gen += 1
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.p = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, bufsize=1, creationflags=flags)
        threading.Thread(target=self._reader, args=(self.p, self.gen), daemon=True).start()
        for c in self.rule_cmds:
            self._send(c)
        self.searches = 0            # 'go' commands still waiting for their bestmove
        self.skip = 0                # how many of those were stopped (their output is ignored)
        self.stop_time = 0.0
        self.current = None          # the commands of the search we want, for a restart

    @property
    def busy(self):
        return self.searches > self.skip

    def _reader(self, p, gen):
        for line in p.stdout:
            self.q.put((gen, line.rstrip()))

    def _send(self, s):
        try:
            self.p.stdin.write(s + "\n")
            self.p.stdin.flush()
        except OSError:
            pass

    def set_rules(self, rule_cmds):
        self.rule_cmds = list(rule_cmds)
        self.stop()
        for c in self.rule_cmds:
            self._send(c)

    def stop(self):
        if self.busy:
            self._send("stop")
            self.skip += 1
            self.stop_time = time.time()
        self.current = None

    def go(self, start_fen, moves, args):
        self.stop()
        self.current = [f"position fen {start_fen}" + (" moves " + " ".join(moves) if moves else ""), "go " + args]
        for c in self.current:
            self._send(c)
        self.searches += 1

    def lines(self):
        # an engine that ignores 'stop' (or has hung) is restarted and the wanted search resent
        if self.skip and time.time() - self.stop_time > 4:
            wanted = self.current
            try:
                self.p.kill()
            except OSError:
                pass
            self._start()
            if wanted:
                self.current = wanted
                for c in wanted:
                    self._send(c)
                self.searches = 1
        out = []
        while True:
            try:
                gen, line = self.q.get_nowait()
            except queue.Empty:
                return out
            if gen != self.gen:
                continue
            if line.startswith("bestmove"):
                self.searches -= 1
                if self.skip:
                    self.skip -= 1
                    continue
            elif self.skip:
                continue                  # output of a stopped search
            out.append(line)

    def close(self):
        try:
            self.stop()
            self._send("quit")
            self.p.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            self.p.kill()
        except OSError:
            pass


# --------------------------------------------------------- evaluation bar --
class Tooltip:
    """A small yellow note that follows the mouse over a widget; text() gives its text ("" = none)."""

    def __init__(self, widget, text):
        self.widget, self.text, self.tip = widget, text, None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Motion>", self._move, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, e):
        self._hide()
        text = self.text()
        if not text:
            return
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        tk.Label(self.tip, text=text, background="#ffffe0", relief="solid", borderwidth=1, padx=5, pady=2,
                 justify="left").pack()
        self._move(e)

    def _move(self, e):
        if self.tip is not None:
            self.tip.wm_geometry(f"+{e.x_root + 14}+{e.y_root + 10}")

    def _hide(self, _e=None):
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None


class EvalBar:
    """The bar left of the board, as on chess.com.  Its white part is White's expected score from
    the position (the "White" column of the analysis): half-and-half is level, and the bar fills
    with the colour of the side that is better.  The number at that side's end is the evaluation
    in pawns (M3: mate in 3; after the game, the result).  It turns over with the board, and it
    moves smoothly to each new evaluation."""

    WIDTH = 34
    LIGHT, DARK = "#fbfbf8", "#403d39"                 # chess.com's colours
    MUTED_LIGHT, MUTED_DARK = "#e4e0da", "#aaa49c"     # no evaluation to show

    def __init__(self, parent, board_px, margin, bg):
        self.c = tk.Canvas(parent, width=self.WIDTH, height=board_px + 2 * margin, highlightthickness=0, bg=bg)
        self.top, self.h = margin, board_px
        self.frac, self.target = 0.5, 0.5
        self.label, self.white_better, self.muted, self.flipped = "", True, True, False
        self.tip = ""
        self._job = None
        self.c.bind("<Configure>", lambda e: self._draw())
        Tooltip(self.c, lambda: self.tip)
        self._draw()

    def show(self, frac, label, white_better, tip):
        self.label, self.white_better, self.muted, self.tip = label, white_better, False, tip
        self._go(frac)

    def clear(self):
        """No evaluation (analysis switched off): a neutral grey bar."""
        self.label, self.muted, self.tip = "", True, "No evaluation: analysis is switched off."
        self._go(0.5)

    def set_flipped(self, flipped):
        self.flipped = flipped
        self._draw()

    def _go(self, frac):
        self.target = min(1.0, max(0.0, frac))
        if self._job is None:
            self._step()
        else:
            self._draw()

    def _step(self):
        diff = self.target - self.frac
        if abs(diff) < 0.002:
            self.frac, self._job = self.target, None
        else:
            self.frac += diff * 0.25                   # ease out over about a quarter of a second
            self._job = self.c.after(16, self._step)
        self._draw()

    def _draw(self):
        c, top, h = self.c, self.top, self.h
        c.delete("all")
        x0, x1 = 4, self.WIDTH - 4
        light, dark = (self.MUTED_LIGHT, self.MUTED_DARK) if self.muted else (self.LIGHT, self.DARK)
        white_h = round(h * self.frac)
        if self.flipped:                               # White's end at the top
            c.create_rectangle(x0, top, x1, top + white_h, fill=light, outline="")
            c.create_rectangle(x0, top + white_h, x1, top + h, fill=dark, outline="")
        else:
            c.create_rectangle(x0, top, x1, top + h - white_h, fill=dark, outline="")
            c.create_rectangle(x0, top + h - white_h, x1, top + h, fill=light, outline="")
        mid = top + h / 2                              # a tick at level
        c.create_line(x0, mid, x0 + 5, mid, fill="#c9302c", width=2)
        c.create_line(x1 - 5, mid, x1, mid, fill="#c9302c", width=2)
        c.create_rectangle(x0, top, x1, top + h, outline="#8c7f6e")
        if self.label:
            at_white_end = self.white_better
            bottom = at_white_end != self.flipped
            y = top + h - 9 if bottom else top + 9
            c.create_text((x0 + x1) / 2, y, text=self.label, fill=self.DARK if at_white_end else self.LIGHT,
                          font=("TkDefaultFont", 8, "bold"))


# --------------------------------------------------------------------- GUI --
class App:
    def __init__(self, root, args):
        self.root = root
        self.sq = args.square
        self.flipped = False
        self.start_fen = args.fen or SETUPS[args.setup]
        self.line, self.line_san, self.ply = [], [], 0   # the whole game, and the move being viewed
        self.saved = []                                   # earlier continuations replaced by a new move
        self.selected = None
        self.analysis = {}                  # multipv index -> info dict
        self.task = None                    # "analysis" or "move"
        self.game_result = ""
        self.comp_paused = False            # stepped back through the game: the computer waits

        # rules, shared by the in-process move generator and the engine subprocess
        self.v_horse = tk.BooleanVar(value=True)
        self.v_eye = tk.BooleanVar(value=False)
        self.v_stale = tk.BooleanVar(value=False)
        self.v_side = tk.BooleanVar(value=True)
        self.v_promo = tk.StringVar(value="any")
        # snipers chess: White's bishops can also take without moving (on with the "snipers" setup)
        self.v_snipers = tk.BooleanVar(value=bool(SETUP_RULES.get(args.setup, {}).get("Snipers")) and not args.fen)
        self.v_style = tk.StringVar(value="pictures")     # pictures | chinese | letters
        self.v_evalbar = tk.BooleanVar(value=True)
        self.v_analyse = tk.BooleanVar(value=True)
        self.v_lines = tk.IntVar(value=5)
        self.v_secs = tk.IntVar(value=args.seconds or 30)
        self.v_comp = tk.StringVar(value="nobody")
        self.v_comp_secs = tk.IntVar(value=5)
        # cores for everything the engine does, analysis and the computer's own moves.  By default
        # half the logical processors (about the physical cores): the computer playing itself keeps
        # them all busy for as long as the game lasts.
        self.v_cores = tk.IntVar(value=getattr(args, "threads", None) or max(1, (os.cpu_count() or 2) // 2))
        self._cores_applied = self._cores()
        self._cores_job = None
        self._apply_rules_inprocess()

        self._pick_fonts()
        self._build_widgets()
        # show the window before the engine starts (checking it, or building it after an update,
        # takes a moment), and in front of the other windows
        self.engine_label.config(text="Starting the engine...")
        self._bring_to_front()
        self.root.update_idletasks()
        self.engine = EngineProc(args.engine, self._rule_cmds())
        label = f"Engine: {self.engine.label}"
        if self.engine.kind != "fairy" and args.engine == "auto" and os.path.exists(FAIRY_PATH):
            label += ".  Fairy-Stockfish needs rebuilding after an update: choose it in the Engine menu."
        self.engine_label.config(text=label)
        self.v_engine.set(self.engine.kind)
        self.building = None
        if not self._reset(self.start_fen):
            messagebox.showerror("Bad FEN", self.start_fen)
            self._reset(SETUPS["default"])
        self.game_path = None
        if getattr(args, "load", None):
            self._open_game(args.load)
        self.root.bind("<Control-s>", lambda e: self._save_game())
        self.root.bind("<Control-o>", lambda e: self._open_game())
        self.root.bind("<Control-e>", lambda e: self._edit_position())
        self.root.after(100, self._poll)
        self.root.protocol("WM_DELETE_WINDOW", self._quit)

    # ---------------------------------------------------------------- setup --
    def _bring_to_front(self):
        """Raise the window above the others.  Windows does not let a program started by another one
        in the background (a notebook's kernel, say) take the foreground, so without this its window
        can open hidden behind the browser."""
        r = self.root
        try:
            r.lift()
            r.attributes("-topmost", True)
            r.after(1500, lambda: r.attributes("-topmost", False))
            r.focus_force()
        except tk.TclError:
            pass

    def _pick_fonts(self):
        self.art = PieceArt(self.root)                 # fonts and piece pictures (pieceart.py)
        if not self.art.images(self.sq):
            self.v_style.set(self.art.default_style())

    def _build_widgets(self):
        r = self.root
        r.title("Chess vs Xiangqi analysis board")
        menubar = tk.Menu(r)
        game = tk.Menu(menubar, tearoff=0)
        for name in SETUPS:
            label = f"New game: {name}" + (" (White's bishops are snipers)" if name == "snipers" else "")
            game.add_command(label=label, command=lambda n=name: self._new_setup(n))
        game.add_separator()
        game.add_command(label="Open game...", accelerator="Ctrl+O", command=self._open_game)
        game.add_command(label="Save game...", accelerator="Ctrl+S", command=self._save_game)
        game.add_separator()
        game.add_command(label="Edit position...", accelerator="Ctrl+E", command=self._edit_position)
        game.add_command(label="Load FEN...", command=self._load_fen)
        game.add_command(label="Copy FEN", command=self._copy_fen)
        game.add_separator()
        game.add_command(label="Quit", command=self._quit)
        menubar.add_cascade(label="Game", menu=game)
        rules = tk.Menu(menubar, tearoff=0)
        rules.add_checkbutton(label="Horse can be blocked", variable=self.v_horse, command=self._rules_changed)
        rules.add_checkbutton(label="Elephant blocked on its midpoint", variable=self.v_eye,
                              command=self._rules_changed)
        rules.add_checkbutton(label="Soldiers move sideways after the 5th rank", variable=self.v_side,
                              command=self._rules_changed)
        rules.add_checkbutton(label="Stalemate is a loss", variable=self.v_stale, command=self._rules_changed)
        promo = tk.Menu(rules, tearoff=0)
        for val, lab in (("any", "Any piece (incl. queen)"), ("xiangqi", "Xiangqi pieces only"),
                         ("western", "Queen, rook, bishop, knight"), ("none", "No promotion")):
            promo.add_radiobutton(label=lab, value=val, variable=self.v_promo, command=self._rules_changed)
        rules.add_cascade(label="Soldier promotion", menu=promo)
        rules.add_separator()
        rules.add_checkbutton(label="White's bishops are snipers (they can also take without moving)",
                              variable=self.v_snipers, command=self._rules_changed)
        menubar.add_cascade(label="Rules", menu=rules)
        view = tk.Menu(menubar, tearoff=0)
        view.add_command(label="Flip board", command=self._flip)
        view.add_checkbutton(label="Evaluation bar", variable=self.v_evalbar, command=self._evalbar_toggled)
        for val, lab in (("pictures", "Pictures"), ("chinese", "Xiangqi discs with Chinese characters"),
                         ("letters", "Letters")):
            view.add_radiobutton(label=f"Pieces: {lab}", value=val, variable=self.v_style, command=self._draw)
        menubar.add_cascade(label="View", menu=view)
        self.v_engine = tk.StringVar(value="")
        eng = tk.Menu(menubar, tearoff=0)
        for kind in ("fairy", "native", "python"):
            eng.add_radiobutton(label=ENGINE_LABELS[kind], value=kind, variable=self.v_engine,
                                command=self._engine_changed)
        menubar.add_cascade(label="Engine", menu=eng)
        r.config(menu=menubar)

        outer = ttk.Frame(r, padding=8)
        outer.grid(sticky="nsew")
        r.columnconfigure(0, weight=1)
        r.rowconfigure(0, weight=1)

        size = 8 * self.sq + 2 * 22
        self.evalbar = EvalBar(outer, 8 * self.sq, 22, BOARD_BG)
        self.evalbar.c.grid(row=0, column=0, rowspan=3, sticky="n")
        self.canvas = tk.Canvas(outer, width=size, height=size, highlightthickness=0, bg=BOARD_BG)
        self.canvas.grid(row=0, column=1, rowspan=3, sticky="n")
        self.canvas.bind("<Button-1>", self._click)

        side = ttk.Frame(outer, padding=(10, 0, 0, 0))
        side.grid(row=0, column=2, sticky="nsew")
        outer.columnconfigure(2, weight=1)
        outer.rowconfigure(0, weight=1)

        self.status = ttk.Label(side, text="", font=("TkDefaultFont", 12, "bold"))
        self.status.grid(row=0, column=0, columnspan=4, sticky="w")

        bar = ttk.Frame(side)
        bar.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 6))
        nav = (("|<", lambda: self._goto(0)), ("<", lambda: self._goto(self.ply - 1)),
               (">", lambda: self._goto(self.ply + 1)), (">|", lambda: self._goto(len(self.line))))
        for i, (txt, cmd) in enumerate(nav):
            ttk.Button(bar, text=txt, width=3, command=cmd).grid(row=0, column=i, padx=(0, 2))
        for i, (txt, cmd) in enumerate((("Flip", self._flip), ("Play best move", self._play_best),
                                        ("New", lambda: self._new(self.start_fen0)),
                                        ("Edit position", self._edit_position)), start=4):
            ttk.Button(bar, text=txt, command=cmd).grid(row=0, column=i, padx=(6 if i == 4 else 0, 4))
        self.restore_btn = ttk.Button(bar, text="Back to previous line", command=self._restore_line)

        ctl = ttk.Frame(side)
        ctl.grid(row=2, column=0, columnspan=4, sticky="w")
        ttk.Checkbutton(ctl, text="Analyse", variable=self.v_analyse, command=self._schedule).grid(row=0, column=0)
        ttk.Label(ctl, text="  lines").grid(row=0, column=1)
        ttk.Spinbox(ctl, from_=1, to=12, width=3, textvariable=self.v_lines, command=self._schedule).grid(row=0, column=2)
        ttk.Label(ctl, text="  max seconds").grid(row=0, column=3)
        ttk.Spinbox(ctl, from_=1, to=3600, width=5, textvariable=self.v_secs, command=self._schedule).grid(row=0, column=4)
        ttk.Label(ctl, text="  cores").grid(row=0, column=5)
        sb = ttk.Spinbox(ctl, from_=1, to=max(64, os.cpu_count() or 1), width=3, textvariable=self.v_cores,
                         command=self._cores_changed)
        sb.grid(row=0, column=6)
        sb.bind("<Return>", lambda e: self._cores_changed())
        # a number typed into the box counts too, without pressing Return
        self.v_cores.trace_add("write", lambda *a: self._cores_typed())
        ttk.Label(ctl, text="Computer plays").grid(row=1, column=0, sticky="w", pady=(4, 0))
        cb = ttk.Combobox(ctl, values=["nobody", "White", "Black", "both"], textvariable=self.v_comp, width=7,
                          state="readonly")
        cb.grid(row=1, column=1, columnspan=2, sticky="w", pady=(4, 0))
        cb.bind("<<ComboboxSelected>>", lambda e: self._computer_plays_changed())
        ttk.Label(ctl, text="  seconds/move").grid(row=1, column=3, pady=(4, 0))
        ttk.Spinbox(ctl, from_=1, to=600, width=5, textvariable=self.v_comp_secs).grid(row=1, column=4, pady=(4, 0))

        ttk.Label(side, text="Best moves (evaluation in pawns and White's expected score; "
                             "double-click to play)").grid(row=3, column=0, columnspan=4, sticky="w", pady=(10, 2))
        self.tree = ttk.Treeview(side, columns=("eval", "pct", "move", "line"), show="headings", height=8)
        self.tree.heading("eval", text="Eval")
        self.tree.heading("pct", text="White")
        self.tree.column("pct", width=55, anchor="e", stretch=False)
        self.tree.heading("move", text="Move")
        self.tree.heading("line", text="Expected continuation")
        self.tree.column("eval", width=70, anchor="e", stretch=False)
        self.tree.column("move", width=90, stretch=False)
        self.tree.column("line", width=330)
        self.tree.grid(row=4, column=0, columnspan=4, sticky="nsew")
        self.tree.bind("<Double-1>", self._tree_play)
        self.depth_label = ttk.Label(side, text="")
        self.depth_label.grid(row=5, column=0, columnspan=4, sticky="w")

        ttk.Label(side, text="Moves (click a move, or use the arrow keys / Home / End, to step through the game)"
                  ).grid(row=6, column=0, columnspan=4, sticky="w", pady=(10, 2))
        self.movelist = tk.Text(side, height=8, width=52, wrap="word", font=("TkFixedFont", 10), cursor="arrow")
        self.movelist.grid(row=7, column=0, columnspan=4, sticky="nsew")
        self.movelist.tag_configure("cur", background="#f4e36b")
        self.movelist.tag_configure("future", foreground="#8a8a8a")
        self.movelist.config(state="disabled")
        self.movelist.bind("<Button-1>", self._movelist_click)
        for key, fn in (("<Left>", lambda: self._goto(self.ply - 1)), ("<Right>", lambda: self._goto(self.ply + 1)),
                        ("<Home>", lambda: self._goto(0)), ("<End>", lambda: self._goto(len(self.line)))):
            r.bind(key, lambda e, fn=fn: None if self._typing() else fn())
        side.rowconfigure(7, weight=1)
        side.columnconfigure(3, weight=1)

        ttk.Label(side, text="FEN").grid(row=8, column=0, sticky="w", pady=(8, 0))
        self.fen_var = tk.StringVar()
        fen_entry = ttk.Entry(side, textvariable=self.fen_var, width=56)
        fen_entry.grid(row=9, column=0, columnspan=3, sticky="ew")
        fen_entry.bind("<Return>", lambda e: self._new(self.fen_var.get().strip()))
        ttk.Button(side, text="Load", command=lambda: self._new(self.fen_var.get().strip())).grid(row=9, column=3,
                                                                                                  sticky="w")
        self.engine_label = ttk.Label(side, text="", foreground="#666", wraplength=520, justify="left")
        self.engine_label.grid(row=10, column=0, columnspan=4, sticky="w", pady=(6, 0))

    # ---------------------------------------------------------------- rules --
    def _apply_rules_inprocess(self):
        pe.R.horse_block = self.v_horse.get()
        pe.R.elephant_eye = self.v_eye.get()
        pe.R.stalemate_loss = self.v_stale.get()
        pe.R.soldier_sideways = self.v_side.get()
        pe.R.soldier_promo = {"none": 0, "xiangqi": 1, "western": 2, "any": 3}[self.v_promo.get()]
        pe.R.snipers = self.v_snipers.get()

    def _rule_cmds(self):
        b = lambda v: "true" if v.get() else "false"
        return [f"setoption HorseBlock {b(self.v_horse)}", f"setoption ElephantEye {b(self.v_eye)}",
                f"setoption StalemateLoss {b(self.v_stale)}", f"setoption SoldierSideways {b(self.v_side)}",
                f"setoption SoldierPromotion {self.v_promo.get()}", f"setoption Snipers {b(self.v_snipers)}",
                f"setoption Threads {self._cores()}"]

    def _engine_changed(self):
        kind = self.v_engine.get()
        if kind == self.engine.kind or self.building:
            self.v_engine.set(self.engine.kind)
            return
        if kind == "fairy" and not fairy_available():
            self.v_engine.set(self.engine.kind)
            if not messagebox.askyesno(
                    "Fairy-Stockfish",
                    "Fairy-Stockfish has not been built yet.\n\nBuild it now?  This takes a minute or two and "
                    "needs a C++ compiler (see \"Building the engines\" in README.md).  You can keep using the "
                    "board meanwhile."):
                return
            self.engine_label.config(text=f"Engine: {self.engine.label} (building Fairy-Stockfish...)")
            result = {}

            def work():
                try:
                    build_fairy()
                    result["ok"] = True
                except Exception as e:                   # noqa: BLE001 - shown to the user
                    result["error"] = str(e)

            self.building = threading.Thread(target=work, daemon=True)
            self.building.start()

            def wait():
                if self.building.is_alive():
                    self.root.after(500, wait)
                    return
                self.building = None
                if "error" in result:
                    self.engine_label.config(text=f"Engine: {self.engine.label}")
                    messagebox.showerror("Fairy-Stockfish", "The build failed:\n\n" + result["error"][-1500:])
                else:
                    self.v_engine.set("fairy")
                    self._switch_engine("fairy")

            self.root.after(500, wait)
            return
        self._switch_engine(kind)

    def _switch_engine(self, kind):
        try:
            new = EngineProc(kind, self._rule_cmds())
        except Exception as e:                            # noqa: BLE001 - shown to the user
            self.v_engine.set(self.engine.kind)
            messagebox.showerror("Engine", f"Could not start the {ENGINE_LABELS[kind]}:\n\n{e}")
            return
        self.engine.close()
        self.engine = new
        self.v_engine.set(new.kind)
        self.engine_label.config(text=f"Engine: {new.label}")
        self.analysis = {}
        self._schedule()

    def _cores(self):
        try:
            return max(1, int(self.v_cores.get()))
        except (tk.TclError, ValueError):
            return 1

    def _cores_typed(self):
        if self._cores_job is not None:
            self.root.after_cancel(self._cores_job)
        self._cores_job = self.root.after(600, self._cores_changed)

    def _cores_changed(self):
        """The cores box applies to the analysis and to the computer's moves alike."""
        self._cores_job = None
        if self._cores() == self._cores_applied:
            return
        self._cores_applied = self._cores()
        self.engine.set_rules(self._rule_cmds())
        self._schedule()

    def _rules_changed(self):
        self._apply_rules_inprocess()
        self.engine.set_rules(self._rule_cmds())
        # replay the game under the new rules, keeping it up to the first move that is now illegal
        line, k = self.line, self.ply
        P = pe.Pos()
        P.set_fen(self.start_fen)
        self.P, self.line, self.line_san, self.ply, self.saved = P, [], [], 0, []
        for mv in line:
            m = pe.parse_move(P, mv)
            if m is None:
                break
            self.line_san.append(self._san(m))
            P.make(m)
            self.line.append(mv)
            self.ply += 1
        if len(self.line) < len(line):
            messagebox.showinfo("Rules changed", f"Move {len(self.line) + 1} ({line[len(self.line)]}) is illegal "
                                                 "under the new rules; the game has been cut there.")
        self.ply_target = min(k, len(self.line))
        while self.ply > self.ply_target:
            self.P.unmake()
            self.ply -= 1
        self._after_change()

    # ----------------------------------------------------------- game state --
    def _reset(self, fen, keep_origin=False):
        P = pe.Pos()
        if not P.set_fen(fen):
            return False
        self.P = P
        self.start_fen = fen
        if not keep_origin:
            self.start_fen0 = fen
        self.line, self.line_san, self.ply, self.saved = [], [], 0, []
        self.selected = None
        self.comp_paused = False
        self._after_change()
        return True

    @property
    def moves(self):
        """Moves from the start position to the position on the board."""
        return self.line[:self.ply]

    def _new(self, fen):
        if not self._reset(fen):
            messagebox.showerror("Bad FEN", f"Could not read this position:\n{fen}")

    def _new_setup(self, name):
        """Game > New game: a setup's position, with the rules that belong to it (snipers chess
        switches the snipers rule on, the other setups switch it off)."""
        snipers = bool(SETUP_RULES.get(name, {}).get("Snipers"))
        if self.v_snipers.get() != snipers:
            self.v_snipers.set(snipers)
            self._apply_rules_inprocess()
            self.engine.set_rules(self._rule_cmds())
        self._new(SETUPS[name])

    def _legal(self):
        return {pe.move_str(m): m for m in self.P.legal_moves()}

    def _play(self, mstr):
        """Play a move from the position on the board.  If it is the game's next move we just step
        forward; otherwise the rest of the game is replaced (and kept for 'Back to previous line')."""
        if self.ply < len(self.line) and self.line[self.ply] == mstr:
            self._goto(self.ply + 1, stepping=False)
            return True
        legal = self._legal()
        if mstr not in legal:
            return False
        if self.ply < len(self.line):
            self.saved.append((self.line[:], self.line_san[:], self.ply))
            del self.line[self.ply:], self.line_san[self.ply:]
        self.line_san.append(self._san(legal[mstr]))
        self.P.make(legal[mstr])
        self.line.append(mstr)
        self.ply += 1
        self.selected = None
        self._after_change()
        return True

    def _goto(self, k, stepping=True):
        """Show the position after k moves of the game (0 = start), without changing the game.
        Stepping back through the game pauses the computer there until "Computer plays" is chosen
        again, so that looking at earlier moves does not start a new line by itself."""
        k = max(0, min(k, len(self.line)))
        if stepping:
            self.comp_paused = k < len(self.line)
        if k == self.ply:
            return
        while self.ply > k:
            self.P.unmake()
            self.ply -= 1
        while self.ply < k:
            self.P.make(pe.parse_move(self.P, self.line[self.ply]))
            self.ply += 1
        self.selected = None
        self._after_change()

    def _restore_line(self):
        if self.saved:
            line, san, branch = self.saved.pop()
            self._goto(0)
            self.line, self.line_san = line, san
            self._goto(branch)
            self._after_change()

    def _movelist_click(self, e):
        idx = self.movelist.index(f"@{e.x},{e.y}")
        for tag in self.movelist.tag_names(idx):
            if tag.startswith("go"):
                self._goto(int(tag[2:]))
                break
        return "break"

    def _typing(self):
        w = self.root.focus_get()
        return w is not None and w.winfo_class() in ("TEntry", "Entry", "TSpinbox", "Spinbox", "TCombobox")

    def _flip(self):
        self.flipped = not self.flipped
        self.evalbar.set_flipped(self.flipped)
        self._draw()

    def _evalbar_toggled(self):
        if self.v_evalbar.get():
            self.evalbar.c.grid()
            self._update_evalbar()
        else:
            self.evalbar.c.grid_remove()

    def _edit_position(self):
        """Game > Edit position: set up a position (position_editor.py); OK starts a new game from it."""
        if getattr(self, "editor", None) is not None and self.editor.winfo_exists():
            self.editor.lift()
            return
        self.editor = PositionEditor(self.root, self.art, self.P.fen(), style=self.v_style.get(),
                                     flipped=self.flipped, square=min(self.sq, 60), on_ok=self._new,
                                     snipers=self.v_snipers.get())

    def _after_change(self):
        reason, result = pe.game_over(self.P)
        self.game_result = f"{result} ({reason})" if reason else ""
        self.analysis = {}
        self._show_analysis()
        self._draw()
        self._update_text()
        self._schedule()

    # ------------------------------------------------------------- engine ----
    def _computer_to_move(self):
        if self.comp_paused and self.ply < len(self.line):   # stepped back: analyse until told to play
            return False
        comp = self.v_comp.get()
        return comp == "both" or (comp == "White" and self.P.side == pe.WHITE) or \
            (comp == "Black" and self.P.side == pe.BLACK)

    def _computer_plays_changed(self):
        """Choosing in "Computer plays" starts the computer from the position shown, even an earlier
        one: its moves then start a new line (the old one is kept for "Back to previous line")."""
        self.comp_paused = False
        self._schedule()

    def _schedule(self):
        self.analysis = {}
        if self.game_result:
            self.engine.stop()
            self.task = None
            self._show_analysis()
            return
        if self._computer_to_move():
            self.task = "move"
            self.engine.go(self.start_fen, self.moves, f"movetime {max(1, self.v_comp_secs.get()) * 1000}")
        elif self.v_analyse.get():
            self.task = "analysis"
            self.engine.go(self.start_fen, self.moves,
                           f"movetime {max(1, self.v_secs.get()) * 1000} multipv {max(1, self.v_lines.get())}")
        else:
            self.task = None
            self.engine.stop()
        self._show_analysis()

    def _poll(self):
        changed = False
        for line in self.engine.lines():
            m = INFO_RE.match(line)
            if m:
                d, k = int(m.group(1)), int(m.group(2))
                info = {"depth": d, "nodes": int(m.group(5)), "time": int(m.group(6)), "pv": m.group(8).split(),
                        "mate" if m.group(3) == "mate" else "cp": int(m.group(4))}
                if k == 1:
                    self.analysis = {kk: v for kk, v in self.analysis.items() if v["depth"] == d}
                self.analysis[k] = info
                changed = True
            elif line.startswith("bestmove"):
                if self.task == "move":
                    self.task = None
                    mv = line.split()[1]
                    if mv != "0000":
                        self._play(mv)
                        continue
                changed = True
        if changed:
            self._show_analysis()
        self.root.after(100, self._poll)

    def _play_best(self):
        if 1 in self.analysis and self.analysis[1]["pv"]:
            self._play(self.analysis[1]["pv"][0])
        elif not self.game_result:
            self.task = "move"
            self.engine.go(self.start_fen, self.moves, f"movetime {max(1, self.v_comp_secs.get()) * 1000}")
            self._show_analysis()

    def _tree_play(self, _event):
        sel = self.tree.selection()
        if sel:
            k = int(sel[0])
            if k in self.analysis and self.analysis[k]["pv"]:
                self._play(self.analysis[k]["pv"][0])

    # ------------------------------------------------------------- display ---
    def _san(self, m, P=None):
        """Readable move text such as Cg4-e4, e7xd6, Sd7-d8=Q, with + for check."""
        return gamefile.move_text(P or self.P, m)

    def _pv_text(self, pv):
        P = pe.Pos()
        P.set_fen(self.P.fen())
        out = []
        for i, ms in enumerate(pv):
            m = next((x for x in P.legal_moves() if pe.move_str(x) == ms), None)
            if m is None:
                break
            num = P.full
            if P.side == pe.WHITE:
                out.append(f"{num}.")
            elif i == 0:
                out.append(f"{num}...")
            out.append(self._san(m, P))
            P.make(m)
        return " ".join(out)

    def _eval_text(self, info):
        white = self.P.side == pe.WHITE
        if "mate" in info:
            n = info["mate"] if white else -info["mate"]
            return f"#{n}" if n > 0 else f"#-{-n}"
        cp = info["cp"] if white else -info["cp"]
        return f"{cp / 100:+.2f}"

    def _expected_text(self, info):
        """White's expected score (win 1, draw 1/2) implied by the evaluation, calibrated on self-play."""
        white = self.P.side == pe.WHITE
        if "mate" in info:
            n = info["mate"] if white else -info["mate"]
            return "100%" if n > 0 else "0%"
        cp = info["cp"] if white else -info["cp"]
        return f"{100 * pe.expected_score(cp, self._own_eval()):.0f}%"

    def _own_eval(self):
        """True when the engine is Fairy-Stockfish using its own evaluation (snipers chess), whose
        centipawns convert to an expected score on a scale of their own."""
        return self.engine.kind == "fairy" and fairy_engine.own_evaluation(*fairy_engine.royals(self.P.fen()))

    def _show_analysis(self):
        self.tree.delete(*self.tree.get_children())
        for k in sorted(self.analysis):
            info = self.analysis[k]
            if not info["pv"]:
                continue
            first = self._pv_text(info["pv"][:1])
            self.tree.insert("", "end", iid=str(k), values=(self._eval_text(info), self._expected_text(info),
                                                            first.split()[-1] if first else "",
                                                            self._pv_text(info["pv"][:12])))
        if self.analysis:
            info = self.analysis[min(self.analysis)]
            what = "Computer thinking" if self.task == "move" else "Analysing" if self.engine.busy else "Done"
            nps = info["nodes"] * 1000 // info["time"] if info["time"] else 0
            self.depth_label.config(text=f"{what}: depth {info['depth']}, {info['nodes']:,} positions "
                                         f"({nps:,}/s), {info['time'] / 1000:.1f}s")
        else:
            self.depth_label.config(text="Computer thinking..." if self.task == "move" else
                                    "Analysing..." if self.task == "analysis" else "")
        self._draw_arrow()
        self._update_evalbar()

    def _update_evalbar(self):
        """Show the best line's evaluation (or the game's result) in the bar left of the board."""
        if not self.v_evalbar.get():
            return
        bar = self.evalbar
        if self.game_result:
            res = self.game_result.split()[0]
            frac, label = {"1-0": (1.0, "1-0"), "0-1": (0.0, "0-1")}.get(res, (0.5, "½"))
            bar.show(frac, label, frac >= 0.5, f"Game over: {self.game_result}")
            return
        info = self.analysis.get(1)
        if info is None:
            if not (self.task or self.v_analyse.get()):
                bar.clear()
            return                                     # analysis on its way: keep the last value meanwhile
        white = self.P.side == pe.WHITE
        if "mate" in info:
            n = info["mate"] if white else -info["mate"]
            who = "White" if n > 0 else "Black"
            bar.show(1.0 if n > 0 else 0.0, f"M{abs(n)}", n > 0,
                     f"{who} mates in {abs(n)}" + (" move" if abs(n) == 1 else " moves") +
                     f"  (depth {info['depth']})")
            return
        cp = info["cp"] if white else -info["cp"]
        expected = pe.expected_score(cp, self._own_eval())
        pawns = abs(cp) / 100
        bar.show(min(0.97, max(0.03, expected)), f"{pawns:.1f}" if pawns < 9.95 else f"{pawns:.0f}", cp >= 0,
                 f"Evaluation {cp / 100:+.2f} for White\nWhite's expected score {100 * expected:.0f}%  "
                 f"(depth {info['depth']})")

    def _update_text(self):
        side = "White" if self.P.side == pe.WHITE else "Black"
        army = "Xiangqi" if self.P.b[self.P.ksq[self.P.side]] & 15 == pe.GENERAL else "Western"
        if self.game_result:
            txt = f"Game over: {self.game_result}"
        else:
            txt = f"{side} ({army} army) to move" + ("  -  check!" if self.P.in_check(self.P.side) else "")
        if self.ply < len(self.line):
            txt += f"   (viewing move {self.ply} of {len(self.line)})"
        self.status.config(text=txt)
        if self.saved:
            self.restore_btn.grid(row=0, column=8, padx=(6, 0))
        else:
            self.restore_btn.grid_remove()
        # move list: each move is clickable; the current one is highlighted
        start = pe.Pos()
        start.set_fen(self.start_fen)
        num, white = start.full, start.side == pe.WHITE
        ml = self.movelist
        ml.config(state="normal")
        ml.delete("1.0", "end")
        start_tag = "go0"
        ml.insert("end", "[start] ", (start_tag, "cur") if self.ply == 0 else (start_tag,))
        if not white and self.line_san:
            ml.insert("end", f"{num}... ")
        for i, san in enumerate(self.line_san, start=1):
            if white:
                ml.insert("end", f"{num}. ", ("future",) if i > self.ply else ())
            tags = [f"go{i}"] + (["cur"] if i == self.ply else []) + (["future"] if i > self.ply else [])
            ml.insert("end", san, tuple(tags))
            ml.insert("end", " ")
            if not white:
                num += 1
            white = not white
        ml.config(state="disabled")
        cur = ml.tag_ranges("cur")
        ml.see(cur[0] if cur else "end")
        self.fen_var.set(self.P.fen())

    # board geometry
    def _xy(self, f, r):
        if self.flipped:
            f, r = 7 - f, 7 - r
        return 22 + f * self.sq, 22 + (7 - r) * self.sq

    def _square_at(self, x, y):
        f, r = (x - 22) // self.sq, 7 - (y - 22) // self.sq
        if not (0 <= f < 8 and 0 <= r < 8):
            return None
        return (7 - f, 7 - r) if self.flipped else (f, r)

    def _draw(self):
        c = self.canvas
        c.delete("all")
        sq = self.sq
        last = self.moves[-1] if self.moves else None
        last_sq = set()
        if last:
            last_sq = {(ord(last[0]) - 97, int(last[1]) - 1), (ord(last[2]) - 97, int(last[3]) - 1)}
        check_sq = None
        if self.P.in_check(self.P.side):
            k = self.P.ksq[self.P.side]
            check_sq = (pe.FILE_[k], pe.RANK_[k])
        targets = set()
        if self.selected:
            for ms in self._legal():
                if (ord(ms[0]) - 97, int(ms[1]) - 1) == self.selected:
                    targets.add((ord(ms[2]) - 97, int(ms[3]) - 1))
        for r in range(8):
            for f in range(8):
                x, y = self._xy(f, r)
                col = LIGHT if (f + r) % 2 else DARK
                if (f, r) in last_sq:
                    col = LASTMOVE
                if (f, r) == self.selected:
                    col = SEL
                if (f, r) == check_sq:
                    col = CHECK
                c.create_rectangle(x, y, x + sq, y + sq, fill=col, outline="")
        for i in range(8):
            fx, _ = self._xy(i, 0)
            c.create_text(fx + sq / 2, 22 + 8 * sq + 11, text="abcdefgh"[i], fill="#555")
            _, ry = self._xy(0, i)
            c.create_text(11, ry + sq / 2, text=str(i + 1), fill="#555")
        for s in pe.SQ64:
            p = self.P.b[s]
            if p:
                self._draw_piece(pe.FILE_[s], pe.RANK_[s], p)
        for f, r in targets:
            x, y = self._xy(f, r)
            occupied = self.P.b[pe.SQ(f, r)] != pe.EMPTY
            rad = sq * (0.46 if occupied else 0.14)
            c.create_oval(x + sq / 2 - rad, y + sq / 2 - rad, x + sq / 2 + rad, y + sq / 2 + rad,
                          outline="#3b6ea5" if occupied else "", width=3,
                          fill="" if occupied else "#3b6ea5", stipple="" if occupied else "gray50")
        self._draw_arrow()

    def _draw_piece(self, f, r, p):
        x, y = self._xy(f, r)
        col = p >> 4
        sniper = pe.R.snipers and p == pe.BISHOP | (pe.WHITE << 4)
        self.art.draw(self.canvas, x + self.sq / 2, y + self.sq / 2, self.sq, p & 15, col, self.v_style.get(),
                      own_royal=self.P.b[self.P.ksq[col]] & 15, sniper=sniper)

    def _draw_arrow(self):
        c = self.canvas
        c.delete("arrow")
        if self.task != "analysis" or 1 not in self.analysis or not self.analysis[1]["pv"]:
            return
        ms = self.analysis[1]["pv"][0]
        (x0, y0), (x1, y1) = self._xy(ord(ms[0]) - 97, int(ms[1]) - 1), self._xy(ord(ms[2]) - 97, int(ms[3]) - 1)
        h = self.sq / 2
        shot = ms.endswith("s")                        # a sniper's shot: dashed, and a crosshair on the target
        c.create_line(x0 + h, y0 + h, x1 + h, y1 + h, fill=ARROW, width=max(4, self.sq // 12), arrow=tk.LAST,
                      arrowshape=(self.sq / 3, self.sq / 2.6, self.sq / 8), tags="arrow", capstyle="round",
                      dash=(max(6, self.sq // 6), max(4, self.sq // 10)) if shot else ())
        if shot:
            r = self.sq * 0.3
            c.create_oval(x1 + h - r, y1 + h - r, x1 + h + r, y1 + h + r, outline=ARROW, width=3, tags="arrow")

    # -------------------------------------------------------------- input ----
    def _click(self, ev):
        if self.game_result or (self._computer_to_move() and self.task == "move"):
            return
        sq = self._square_at(ev.x, ev.y)
        if sq is None:
            return
        legal = self._legal()
        if self.selected:
            frm = "abcdefgh"[self.selected[0]] + str(self.selected[1] + 1)
            to = "abcdefgh"[sq[0]] + str(sq[1] + 1)
            options = [ms for ms in legal if ms[:4] == frm + to]
            if options:
                if len(options) == 1:
                    ms = options[0]
                elif any(o.endswith("s") for o in options):
                    ms = self._ask_shot(options, frm, to)
                else:
                    ms = self._ask_promotion(options)
                if ms:
                    self._play(ms)
                return
        p = self.P.b[pe.SQ(*sq)]
        if p and p >> 4 == self.P.side and any((ord(ms[0]) - 97, int(ms[1]) - 1) == sq for ms in legal):
            self.selected = None if self.selected == sq else sq
        else:
            self.selected = None
        self._draw()

    def _ask_shot(self, options, frm, to):
        """A sniper can take by moving or shoot from where it stands: ask which (keys S, M, Esc)."""
        shot = next(o for o in options if o.endswith("s"))
        move = next(o for o in options if not o.endswith("s"))
        win = tk.Toplevel(self.root)
        win.title("Sniper")
        win.transient(self.root)
        choice = {"m": None}

        def pick(ms):
            choice["m"] = ms
            win.destroy()

        ttk.Label(win, text=f"The sniper on {frm} takes on {to}:", padding=6).pack()
        ttk.Button(win, text=f"Shoot: stay on {frm}  (S)", command=lambda: pick(shot)).pack(fill="x", padx=8, pady=2)
        ttk.Button(win, text=f"Move to {to}  (M)", command=lambda: pick(move)).pack(fill="x", padx=8, pady=2)
        ttk.Button(win, text="Cancel", command=win.destroy).pack(fill="x", padx=8, pady=(6, 8))
        for key, ms in (("s", shot), ("S", shot), ("m", move), ("M", move)):
            win.bind(f"<KeyPress-{key}>", lambda e, ms=ms: pick(ms))
        win.bind("<Escape>", lambda e: win.destroy())
        self.shot_dialog = win
        win.grab_set()
        win.focus_set()
        self.root.wait_window(win)
        return choice["m"]

    def _ask_promotion(self, options):
        win = tk.Toplevel(self.root)
        win.title("Promote to")
        win.transient(self.root)
        choice = {"m": None}
        ttk.Label(win, text="Promote to:", padding=6).pack()
        for ms in options:
            name = PIECE_NAMES[ms[4].upper()]
            ttk.Button(win, text=name.capitalize(),
                       command=lambda ms=ms: (choice.update(m=ms), win.destroy())).pack(fill="x", padx=8, pady=2)
        ttk.Button(win, text="Cancel", command=win.destroy).pack(fill="x", padx=8, pady=(6, 8))
        win.grab_set()
        self.root.wait_window(win)
        return choice["m"]

    def _save_game(self):
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Save game", defaultextension=".pgn",
            initialdir=os.path.dirname(self.game_path) if self.game_path else os.getcwd(),
            initialfile=os.path.basename(self.game_path) if self.game_path else "game.pgn",
            filetypes=[("Game files (PGN)", "*.pgn"), ("All files", "*.*")])
        if not path:
            return
        try:
            gamefile.save(path, self.start_fen, self.line)
        except Exception as e:
            messagebox.showerror("Save game", f"Could not save the game:\n{e}")
            return
        self.game_path = path
        self.root.title(f"Chess vs Xiangqi analysis board - {os.path.basename(path)}")

    def _open_game(self, path=None):
        if path is None:
            path = filedialog.askopenfilename(
                parent=self.root, title="Open game",
                initialdir=os.path.dirname(self.game_path) if self.game_path else os.getcwd(),
                filetypes=[("Game files (PGN)", "*.pgn"), ("All files", "*.*")])
            if not path:
                return
        try:
            g = gamefile.load(path)                  # also sets the file's rules in pyengine
        except Exception as e:
            messagebox.showerror("Open game", f"Could not read {path}:\n{e}")
            return
        # show the file's rule settings in the Rules menu and pass them to the engine
        R = pe.R
        self.v_horse.set(R.horse_block)
        self.v_eye.set(R.elephant_eye)
        self.v_stale.set(R.stalemate_loss)
        self.v_side.set(R.soldier_sideways)
        self.v_promo.set(gamefile.PROMO_NAMES[R.soldier_promo])
        self.v_snipers.set(R.snipers)
        self.engine.set_rules(self._rule_cmds())
        P = pe.Pos()
        if not P.set_fen(g["fen"]):
            messagebox.showerror("Open game", f"Bad start position in {path}:\n{g['fen']}")
            return
        self.P, self.start_fen, self.start_fen0 = P, g["fen"], g["fen"]
        self.line, self.line_san, self.ply, self.saved = [], [], 0, []
        for mv in g["moves"]:
            m = pe.parse_move(P, mv)
            self.line_san.append(self._san(m))
            P.make(m)
            self.line.append(mv)
            self.ply += 1
        self.selected = None
        self.game_path = path
        self.root.title(f"Chess vs Xiangqi analysis board - {os.path.basename(path)}")
        self._after_change()
        if g["error"]:
            messagebox.showwarning("Open game", f"Loaded {len(self.line)} moves; stopped because "
                                                f"{g['error']}.")

    def _load_fen(self):
        fen = simpledialog.askstring("Load FEN", "Position (FEN):", initialvalue=self.P.fen(), parent=self.root)
        if fen:
            self._new(fen.strip())

    def _copy_fen(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.P.fen())

    def _quit(self):
        self.engine.close()
        self.root.destroy()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--setup", choices=list(SETUPS), default="default")
    ap.add_argument("--fen", help="start from this position instead")
    ap.add_argument("--engine", choices=ENGINE_KINDS, default="auto",
                    help="fairy: Fairy-Stockfish with this game's rules (strongest); native: the compiled "
                         "xqchess engine; python: the pure-Python engine; auto (default): Fairy-Stockfish "
                         "if it has been built, else native, else Python")
    ap.add_argument("--square", type=int, default=72, help="square size in pixels")
    ap.add_argument("--seconds", type=int, help="maximum analysis time per position (default 30)")
    ap.add_argument("--load", metavar="FILE.pgn", help="open a saved game")
    ap.add_argument("--threads", type=int, help="CPU cores for the engine (default: half the logical processors)")
    ap.add_argument("--in-process", action="store_true",
                    help="in a Jupyter notebook: run inside the kernel instead of as a program of its own")
    ap.add_argument("--screenshot", help=argparse.SUPPRESS)       # used for automated testing
    ap.add_argument("--script", help=argparse.SUPPRESS)
    args = ap.parse_args()
    root = tk.Tk()
    app = App(root, args)
    if args.script:                                               # e.g. "g2g4,e7e5" then wait
        for mv in args.script.split(","):
            app._play(mv)
    if args.screenshot:
        def shot():
            root.update()
            subprocess.run(["import", "-window", "root", args.screenshot])
            app._quit()
        root.after(8000, shot)
    root.mainloop()


if __name__ == "__main__":
    main()
