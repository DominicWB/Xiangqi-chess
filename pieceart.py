"""pieceart.py -- drawing the pieces on a tkinter canvas, for the analysis board (gui.py) and the
position editor (position_editor.py).

Three styles:
  * "pictures": the images in pieces/ (resized exactly with Pillow if it is installed, otherwise
    the nearest ready-made size).  The general is drawn as a king, the soldier as a pawn and the
    horse as a knight, so a small letter ("badge") marks the look-alikes that could be misread:
    the general, and a knight, horse, pawn or soldier standing in the other army;
  * "chinese": Xiangqi discs with Chinese characters, Western pieces as chess symbols;
  * "letters": every piece as its letter.

Under the snipers rule White's bishops are drawn as snipers: the picture pieces/wSniper.png, or in
the other styles a bishop with a red crosshair.
"""
import os
import tkinter as tk
import tkinter.font as tkfont

import pyengine as pe

WESTERN_GLYPH = {pe.KING: ("♔", "♚"), pe.QUEEN: ("♕", "♛"), pe.ROOK: ("♖", "♜"),
                 pe.BISHOP: ("♗", "♝"), pe.KNIGHT: ("♘", "♞"), pe.PAWN: ("♙", "♟")}
XQ_CHAR = {pe.GENERAL: ("帥", "將"), pe.ADVISOR: ("仕", "士"), pe.ELEPHANT: ("相", "象"), pe.HORSE: ("傌", "馬"),
           pe.CHARIOT: ("俥", "車"), pe.CANNON: ("炮", "砲"), pe.SOLDIER: ("兵", "卒")}
CJK_FONTS = ["Microsoft YaHei", "Microsoft JhengHei", "SimSun", "SimHei", "PingFang SC", "Heiti SC",
             "Noto Sans CJK SC", "Noto Serif CJK SC", "WenQuanYi Zen Hei", "Arial Unicode MS"]
SYMBOL_FONTS = ["Segoe UI Symbol", "DejaVu Sans", "Arial Unicode MS", "Apple Symbols", "FreeSerif"]
PIECE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pieces")
BADGE_INK = "#b3261e"
STYLES = ("pictures", "chinese", "letters")


def badge(t, own_royal):
    """The letter to write on a piece picture that could be mistaken for another piece, or "".
    own_royal is the royal of the piece's own side (KING or GENERAL), which tells its army."""
    if t == pe.GENERAL:
        return "G"                                     # drawn as a king
    if own_royal == pe.GENERAL:                        # Western look-alikes in the Xiangqi army
        return {pe.KNIGHT: "N", pe.PAWN: "P"}.get(t, "")
    if own_royal == pe.KING:                           # Xiangqi look-alikes in the Western army
        return {pe.HORSE: "H", pe.SOLDIER: "S"}.get(t, "")
    return ""


class PieceArt:
    """Fonts and images for drawing pieces, shared by every board of the application."""

    def __init__(self, root):
        self.root = root
        fams = set(tkfont.families(root))
        self.cjk_font = next((f for f in CJK_FONTS if f in fams), None)
        self.sym_font = next((f for f in SYMBOL_FONTS if f in fams), "TkDefaultFont")
        self._images = {}

    def images(self, size):
        """Piece pictures of about `size` pixels, keyed "wK", "bG", ...; {} if they can't be loaded."""
        if size not in self._images:
            self._images[size] = self._load(size)
        return self._images[size]

    def _load(self, size):
        names = [c + t for c in "wb" for t in "PNBRQKSHEAJCG"]
        extra = [n for n in ("wSniper",) if os.path.exists(os.path.join(PIECE_DIR, n + ".png"))]
        names += extra
        try:
            from PIL import Image, ImageTk
            return {n: ImageTk.PhotoImage(Image.open(os.path.join(PIECE_DIR, n + ".png")).resize(
                (size, size), Image.LANCZOS), master=self.root) for n in names}
        except Exception:
            pass
        try:
            # without Pillow: a ready-made size, shrunk by a whole factor if that fits better
            sizes = sorted(int(d) for d in os.listdir(PIECE_DIR) if d.isdigit())
            options = [(s // k, s, k) for s in sizes for k in (1, 2, 3)]
            fitting = [o for o in options if o[0] <= size]
            _, best, k = max(fitting) if fitting else min(options)
            out = {}
            for n in names:
                path = os.path.join(PIECE_DIR, str(best), n + ".png")
                if n in extra and not os.path.exists(path):
                    continue
                img = tk.PhotoImage(file=path, master=self.root)
                out[n] = img.subsample(k, k) if k > 1 else img
            return out
        except Exception:
            return {}

    def default_style(self):
        return "pictures" if self.images(60) else "chinese" if self.cjk_font else "letters"

    def draw(self, c, cx, cy, size, t, col, style, own_royal=None, tags=(), badges=True, sniper=False):
        """Draw a piece of type t and colour col centred at (cx, cy) on canvas c, in a square of
        `size` pixels.  own_royal (KING or GENERAL) is the royal of the piece's side; it decides
        the badge on look-alike pictures (badges=False leaves them out).  sniper=True draws a
        bishop as a sniper (the snipers rule)."""
        tags = tuple(tags)
        if style == "pictures":
            images = self.images(size)
            if images:
                name = "wb"[col] + pe.PCHARS[t]
                if sniper and "wb"[col] + "Sniper" in images:
                    c.create_image(cx, cy, image=images["wb"[col] + "Sniper"], tags=tags)
                    return
                c.create_image(cx, cy, image=images[name], tags=tags)
                if sniper:
                    self.crosshair(c, cx, cy, size, tags)
                letter = badge(t, own_royal) if badges else ""
                if letter:
                    c.create_text(cx + size / 2 - 4, cy - size / 2 + 3, text=letter, anchor="ne", fill=BADGE_INK,
                                  font=("TkDefaultFont", max(7, size // 6), "bold"), tags=tags)
                return
            style = "chinese"                           # no pictures available
        letters = style == "letters" or not self.cjk_font
        if t in XQ_CHAR:
            rad = size * 0.42
            ink = "#b3261e" if col == pe.WHITE else "#1d1d1d"
            c.create_oval(cx - rad, cy - rad, cx + rad, cy + rad, fill="#f6e7c1", outline=ink, width=2, tags=tags)
            c.create_oval(cx - rad * 0.82, cy - rad * 0.82, cx + rad * 0.82, cy + rad * 0.82, outline=ink, width=1,
                          tags=tags)
            if letters:
                c.create_text(cx, cy, text=pe.PCHARS[t], fill=ink, font=("TkDefaultFont", int(size * 0.36), "bold"),
                              tags=tags)
            else:
                c.create_text(cx, cy, text=XQ_CHAR[t][col], fill=ink, font=(self.cjk_font, int(size * 0.36), "bold"),
                              tags=tags)
        elif letters:
            fill, ink = ("#ffffff", "#000000") if col == pe.WHITE else ("#222222", "#ffffff")
            rad = size * 0.36
            c.create_rectangle(cx - rad, cy - rad, cx + rad, cy + rad, fill=fill, outline="#000", width=2, tags=tags)
            c.create_text(cx, cy, text=pe.PCHARS[t], fill=ink, font=("TkDefaultFont", int(size * 0.36), "bold"),
                          tags=tags)
        else:
            glyph = int(size * 0.62)
            outline_glyph, solid_glyph = WESTERN_GLYPH[t]
            if col == pe.WHITE:
                c.create_text(cx, cy + 2, text=solid_glyph, fill="#ffffff", font=(self.sym_font, glyph), tags=tags)
                c.create_text(cx, cy + 2, text=outline_glyph, fill="#000000", font=(self.sym_font, glyph), tags=tags)
            else:
                c.create_text(cx, cy + 2, text=solid_glyph, fill="#000000", font=(self.sym_font, glyph), tags=tags)
        if sniper:
            self.crosshair(c, cx, cy, size, tags)

    def crosshair(self, c, cx, cy, size, tags=()):
        """A sniper's mark (where there is no sniper picture): a small red crosshair in the top right
        corner of the square, drawn over the piece."""
        r = max(4, size // 7)
        x, y = cx + size / 2 - r - 3, cy - size / 2 + r + 3
        w = max(1, size // 36)
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            c.create_line(x + dx * r * 0.35, y + dy * r * 0.35, x + dx * r * 1.3, y + dy * r * 1.3,
                          fill=BADGE_INK, width=w, tags=tags)
        c.create_oval(x - r, y - r, x + r, y + r, outline=BADGE_INK, width=w, tags=tags)
