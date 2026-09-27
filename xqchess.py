"""Python front end for the xqchess engine (Western chess army vs Xiangqi army, 8x8).

    from xqchess import Engine
    with Engine() as e:
        e.position(fen)                      # or e.position() for the start position
        for line in e.analyse(depth=10, multipv=5):
            print(line)
"""
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# on Windows, start helper programs without a console window of their own (a program started from one
# without a console, such as the web server or the board launched from a notebook, would get one)
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
ENGINE_PATH = os.path.join(HERE, "engine", "xqchess.exe" if os.name == "nt" else "xqchess")

SETUPS = {
    # Xiangqi army White (moves first, soldiers a3 d3 e3 h3), Western army Black
    "default": "rnbqkbnr/pppppppp/8/8/8/S2SS2S/1C4C1/JHEAGEHJ w kq - 0 1",
    # the same without Black's queen
    "noqueen": "rnb1kbnr/pppppppp/8/8/8/S2SS2S/1C4C1/JHEAGEHJ w kq - 0 1",
    # without Black's queen and both knights: about even with the strongest engine
    "noqueen-noknights": "r1b1kb1r/pppppppp/8/8/8/S2SS2S/1C4C1/JHEAGEHJ w kq - 0 1",
    # without Black's queen and both bishops: White (Xiangqi) scores about 80%
    "noqueen-nobishops": "rn2k1nr/pppppppp/8/8/8/S2SS2S/1C4C1/JHEAGEHJ w kq - 0 1",
    # first version: Western army White, Xiangqi soldiers a6 c6 f6 h6 (use --soldier-promotion none
    # to reproduce the first round of results exactly)
    "v1": "jheagehj/1c4c1/s1s2s1s/8/8/8/PPPPPPPP/RNBQKBNR w KQ - 0 1",
    "v1-swapped": "rnbqkbnr/pppppppp/8/8/8/S1S2S1S/1C4C1/JHEAGEHJ w kq - 0 1",
    # snipers chess: the ordinary chess position, with the rule that White's bishops are snipers
    # (they can also take the first enemy piece along a diagonal without moving; see SETUP_RULES)
    "snipers": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
}
START_FEN = SETUPS["default"]
# rule options that belong to a setup (the tools and the analysis board switch them on with it)
SETUP_RULES = {"snipers": {"Snipers": True}}

PIECE_NAMES = {
    "P": "pawn", "N": "knight", "B": "bishop", "R": "rook", "Q": "queen", "K": "king",
    "S": "soldier", "H": "horse", "E": "elephant", "A": "advisor", "J": "chariot",
    "C": "cannon", "G": "general",
}
# Order of piece types in the engine (index 1..13), used by self-play samples.
TYPE_LETTERS = "PNBRQKSHEAJCG"

RULE_OPTIONS = ("HorseBlock", "ElephantEye", "StalemateLoss", "SoldierSideways", "SoldierPromotion", "Snipers")


# ---------------------------------------------------------------- compiling --
NO_COMPILER = ("no C++ compiler found. Install one (on Windows the free Build Tools for Visual Studio with "
               "\"Desktop development with C++\", on macOS the Xcode command line tools, on Linux g++), then run  "
               "python build.py  (see \"Building the engines\" in README.md)")
_MSVC = {}


def _which(name, env=None):
    """Full path of a program on the PATH of `env` (default: this program's environment), or None."""
    env = os.environ if env is None else env
    path = next((v for k, v in env.items() if k.upper() == "PATH"), None)
    return shutil.which(name, path=path)


def _parse_set_output(text):
    """The environment listed by the Windows command prompt's `set` command, as a dict."""
    env = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key:
            env[key] = value
    return env


def _msvc_environment():
    """Windows: the environment in which the Visual Studio C++ compiler runs, as its "x64 Native Tools
    Command Prompt" sets it up (from Build Tools for Visual Studio or a full Visual Studio, found with
    the Visual Studio installer's vswhere), or None.  Worked out once; it takes a few seconds."""
    if "env" in _MSVC:
        return _MSVC["env"]
    _MSVC["env"] = None
    if os.name != "nt":
        return None
    try:
        base = os.environ.get("ProgramFiles(x86)") or os.environ.get("ProgramFiles") or r"C:\Program Files (x86)"
        vswhere = os.path.join(base, "Microsoft Visual Studio", "Installer", "vswhere.exe")
        if not os.path.exists(vswhere):
            return None
        r = subprocess.run([vswhere, "-latest", "-products", "*", "-requires",
                            "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath"],
                           capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW)
        found = [line.strip() for line in r.stdout.splitlines() if line.strip()]
        vcvars = os.path.join(found[0], "VC", "Auxiliary", "Build", "vcvars64.bat") if found else ""
        if not os.path.exists(vcvars):
            return None
        # run vcvars64.bat in a command prompt, then list the environment it leaves (cmd /u: in UTF-16)
        r = subprocess.run(f'cmd /u /s /c ""{vcvars}" >nul 2>&1 && set"', capture_output=True, timeout=300,
                           creationflags=NO_WINDOW)
        env = _parse_set_output(r.stdout.decode("utf-16-le", errors="replace"))
        if _which("cl", env):
            _MSVC["env"] = env
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return _MSVC["env"]


def find_compiler():
    """The C++ compiler to build the engines with, as (kind, full path, environment to run it in):
    kind "gcc" (g++ or clang++) or "msvc" (Microsoft's cl).  Tried in turn: the compiler named by the
    CXX environment variable, then g++, clang++ and cl on the PATH, and on Windows the Visual Studio
    build tools, which need not be on the PATH.  None if there is none."""
    for name in filter(None, [os.environ.get("CXX"), "g++", "clang++", "cl"]):
        path = _which(name)
        if path:
            kind = "msvc" if os.path.splitext(os.path.basename(path))[0].lower() == "cl" else "gcc"
            return kind, path, dict(os.environ)
    env = _msvc_environment()
    return ("msvc", _which("cl", env), env) if env else None


def _find_compiler():
    """The command (without the source file) that compiles the xqchess engine to ENGINE_PATH, or None."""
    comp = find_compiler()
    if comp is None:
        return None
    kind, cxx, _ = comp
    if kind == "msvc":
        return [cxx, "/nologo", "/O2", "/EHsc", "/std:c++17", "/Fe:" + ENGINE_PATH]
    cmd = [cxx, "-O2", "-std=c++17", "-pthread"]
    if os.name == "nt" and "clang" not in os.path.basename(cxx).lower():
        cmd.append("-static")                   # no MinGW runtime DLLs needed next to the exe
    return cmd + ["-o", ENGINE_PATH]


def _works(path):
    try:
        r = subprocess.run([path], input="quit\n", text=True, capture_output=True, timeout=10, creationflags=NO_WINDOW)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# positions whose static evaluation must be the same in the compiled and the Python engine
_CHECK_FENS = [
    "rnb1kbnr/pppppppp/8/8/8/S2SS2S/1C4C1/JHEAGEHJ w kq - 0 1",
    "r1b1kb1r/pp1ppppp/8/2p5/8/S2SS2S/2C3C1/JHEAGEHJ w kq c6 0 2",
    "8/8/1S4Jp/4S3/8/8/4kr1G/8 w - - 10 45",
    "4k3/8/8/8/8/8/2H5/3JG3 b - - 10 1",
    "8/8/8/4k3/4A3/4G3/8/8 b - - 3 58",          # solved ending: king against general + advisor
    "8/8/8/4k3/8/4G3/8/8 w - - 0 1",             # a bare general loses
    "8/8/8/4k3/8/4S3/8/4G3 w - - 0 1",           # solved ending with a soldier
    "8/8/8/4k3/4A3/4G3/8/J7 b - - 0 1",          # a drawn solved ending scores close to 0
]


_SNIPER_FEN = "rnbqkbnr/1ppppppp/p7/8/8/4P3/PPPP1PPP/RNBQKBNR w KQkq - 0 2"     # Bf1 can shoot a6


def _matches_python(path):
    """True if the binary evaluates like pyengine.py, i.e. it was built from the current source
    (a binary from an older version would silently give different scores)."""
    try:
        import importlib.util                    # a private copy, so rule settings in use elsewhere don't matter
        spec = importlib.util.spec_from_file_location("_pyengine_check", PYENGINE)
        pe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pe)
        text = "".join(f"position fen {f}\neval\n" for f in _CHECK_FENS)
        # the snipers rule (added later): the sniper's value, and a shot among the legal moves
        text += f"setoption Snipers true\nposition fen {_SNIPER_FEN}\neval\nlegal\nquit\n"
        r = subprocess.run([path], input=text, text=True, capture_output=True, timeout=10, creationflags=NO_WINDOW)
        got = [int(x) for x in r.stdout.split() if x.lstrip("-").isdigit()]
        want = [pe.evaluate(pe.Pos(f)) for f in _CHECK_FENS]
        pe.R.snipers = True
        want.append(pe.evaluate(pe.Pos(_SNIPER_FEN)))
        return got == want and " f1a6s" in r.stdout
    except Exception:
        return False


def build_engine(force=False):
    """Return the engine binary, compiling engine/xqchess.cpp first if needed and possible.

    Nothing prebuilt is shipped.  A binary that is older than xqchess.cpp, or that evaluates
    differently from pyengine.py (built from an earlier version), is rebuilt; force=True rebuilds
    it anyway."""
    src = os.path.join(HERE, "engine", "xqchess.cpp")
    exists = os.path.exists(ENGINE_PATH) and _works(ENGINE_PATH)
    stale = exists and (os.path.getmtime(ENGINE_PATH) < os.path.getmtime(src) or not _matches_python(ENGINE_PATH))
    if exists and not stale and not force:
        return ENGINE_PATH
    comp = find_compiler()
    if comp is None:
        if exists and not force:
            print("warning: the compiled engine is out of date (built from an older xqchess.cpp) and no C++ "
                  "compiler was found to rebuild it; using it anyway, but its scores come from the old "
                  "evaluation. Rebuild it with  python build.py", file=sys.stderr)
            return ENGINE_PATH
        raise RuntimeError(NO_COMPILER)
    subprocess.check_call(_find_compiler() + [src], cwd=os.path.join(HERE, "engine"), env=comp[2],
                          creationflags=NO_WINDOW)
    return ENGINE_PATH


# ------------------------------------------------------------ Fairy-Stockfish --
FAIRY_SRC = os.path.join(HERE, "fairy", "src")
FAIRY_PATH = os.path.join(HERE, "engine", "fairy-xqchess.exe" if os.name == "nt" else "fairy-xqchess")
_fairy_checked = {}


def _fairy_sources():
    import glob
    return sorted(sum((glob.glob(os.path.join(FAIRY_SRC, *d, "*.cpp"))
                       for d in ((), ("nnue",), ("nnue", "features"), ("syzygy",))), []))


def _fairy_flags(cxx):
    import platform
    flags = [cxx, "-O3", "-std=c++17", "-DNDEBUG", "-DNNUE_EMBEDDING_OFF", "-DUSE_POPCNT"]
    if sys.maxsize > 2 ** 32:
        flags.append("-DIS_64BIT")
    if platform.machine().lower() in ("x86_64", "amd64"):
        flags.append("-mpopcnt")
    if os.name == "nt" and "clang" not in os.path.basename(cxx).lower():
        flags.append("-static")             # no MinGW runtime DLLs needed next to the exe
    elif sys.platform.startswith("linux"):
        flags.append("-DUSE_PTHREADS")      # 8 MB search-thread stacks, as upstream does
    return flags + ["-pthread"]


def build_fairy(force=False, quiet=False):
    """Return the Fairy-Stockfish binary, compiling fairy/src first if needed and possible.
    A binary that does not evaluate exactly like pyengine.py (built from older sources) is
    rebuilt.  Building takes a minute or two (quiet=True: without saying so)."""
    if not force and _fairy_checked.get(FAIRY_PATH):
        return FAIRY_PATH
    import fairy_engine
    if not force and fairy_engine.binary_works(FAIRY_PATH):
        _fairy_checked[FAIRY_PATH] = True
        return FAIRY_PATH
    if not os.path.isdir(FAIRY_SRC):
        raise RuntimeError("fairy/src is missing")
    comp = find_compiler()
    if comp is None:
        raise RuntimeError(NO_COMPILER)
    kind, cxx, env = comp
    if not quiet:
        print("building Fairy-Stockfish (takes a minute or two)...", file=sys.stderr, flush=True)
    if kind == "gcc":
        import concurrent.futures
        import tempfile
        objdir = tempfile.mkdtemp(prefix="fairy-build-")
        flags = _fairy_flags(cxx)
        srcs = _fairy_sources()
        objs = [os.path.join(objdir, f"{i}.o") for i in range(len(srcs))]
        with concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count() or 2) as ex:
            for r in ex.map(lambda so: subprocess.run(flags + ["-c", "-o", so[1], so[0]], capture_output=True, text=True,
                                                      env=env, creationflags=NO_WINDOW),
                            zip(srcs, objs)):
                if r.returncode:
                    raise RuntimeError("compiling Fairy-Stockfish failed:\n" + r.stderr[-3000:])
        subprocess.check_call(flags + ["-o", FAIRY_PATH] + objs, env=env, creationflags=NO_WINDOW)
        shutil.rmtree(objdir, ignore_errors=True)
    else:
        objdir = os.path.join(HERE, "engine", "obj") + os.sep
        os.makedirs(objdir, exist_ok=True)
        subprocess.check_call([cxx, "/nologo", "/O2", "/EHsc", "/std:c++17", "/MP", "/DNDEBUG",
                               "/DNNUE_EMBEDDING_OFF", "/DUSE_POPCNT", "/Fo:" + objdir, "/Fe:" + FAIRY_PATH]
                              + _fairy_sources() + ["/link", "/STACK:8388608"], env=env, creationflags=NO_WINDOW)
    if not fairy_engine.binary_works(FAIRY_PATH):
        raise RuntimeError("the Fairy-Stockfish build did not produce a working engine")
    _fairy_checked[FAIRY_PATH] = True
    return FAIRY_PATH


def fairy_available():
    """True if an up-to-date Fairy-Stockfish binary exists (never builds one)."""
    if FAIRY_PATH not in _fairy_checked:
        import fairy_engine
        _fairy_checked[FAIRY_PATH] = fairy_engine.binary_works(FAIRY_PATH)
    return _fairy_checked[FAIRY_PATH]


def rule_commands(rules):
    """rules: dict like {'HorseBlock': True, 'Value': {'H': 280}} -> list of setoption lines."""
    cmds = []
    for k, v in (rules or {}).items():
        if k == "Value":
            for letter, cp in v.items():
                cmds.append(f"setoption Value {letter} {int(cp)}")
        else:
            cmds.append(f"setoption {k} {str(v).lower()}")
    return cmds


PYENGINE = os.path.join(HERE, "pyengine.py")


ENGINE_KINDS = ["auto", "fairy", "native", "python"]
ENGINE_LABELS = {"fairy": "Fairy-Stockfish (xqchess build)", "native": "xqchess engine (C++)",
                 "python": "Python engine"}


def engine_kind(cmd):
    """Which engine a command line from engine_command() starts."""
    if cmd[-1].endswith("pyengine.py"):
        return "python"
    return "fairy" if any(str(c).endswith("fairy_engine.py") for c in cmd) else "native"


def engine_command(kind="auto"):
    """Command line that starts an engine.

    kind = "fairy":  Fairy-Stockfish with this game's rules and evaluation (strongest; built
                     from fairy/src if needed), run through fairy_engine.py;
           "native": the compiled xqchess engine (built from engine/xqchess.cpp if needed);
           "python": the pure-Python engine (pyengine.py; same rules, ~60x slower);
           "auto":   Fairy-Stockfish if it has been built, else the native engine if it exists
                     or can be compiled, otherwise Python."""
    fairy_cmd = lambda: [sys.executable, "-u", os.path.join(HERE, "fairy_engine.py"), FAIRY_PATH]
    if kind == "fairy":
        build_fairy()
        return fairy_cmd()
    if kind == "auto":
        try:
            if fairy_available():
                return fairy_cmd()
        except Exception:
            pass
    if kind in ("auto", "native"):
        try:
            return [build_engine()]
        except (RuntimeError, OSError, subprocess.CalledProcessError) as e:
            if kind == "native":
                raise
            print(f"note: compiled engine unavailable ({e}); using the Python engine", file=sys.stderr)
    return [sys.executable, "-u", PYENGINE]


class Engine:
    def __init__(self, rules=None, path=None, kind="auto", threads=1, cmd=None):
        """rules: rule options (see rule_commands); path: an engine binary, or else kind: "fairy",
        "native", "python" or "auto" (see engine_command); cmd: a full command line instead."""
        cmd = list(cmd) if cmd else [path] if path else engine_command(kind)
        self.kind = engine_kind(cmd)
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1,
                                  creationflags=NO_WINDOW)
        for c in rule_commands(rules):
            self.send(c)
        if threads > 1:
            self.send(f"setoption Threads {threads}")

    def send(self, line):
        self.p.stdin.write(line + "\n")
        self.p.stdin.flush()

    def readline(self):
        line = self.p.stdout.readline()
        if not line:
            raise RuntimeError("engine exited")
        return line.rstrip("\n")

    def sync(self):
        self.send("isready")
        out = []
        while True:
            l = self.readline()
            if l == "readyok":
                return out
            out.append(l)

    def position(self, fen=None, moves=()):
        cmd = f"position fen {fen}" if fen else "position startpos"
        if moves:
            cmd += " moves " + " ".join(moves)
        self.send(cmd)
        errs = [l for l in self.sync() if l.startswith("error")]
        if errs:
            raise ValueError("; ".join(errs))

    def fen(self):
        self.send("fen")
        return self.readline()[4:]

    def legal(self):
        self.send("legal")
        return self.readline().split()[1:]

    def board(self):
        self.send("d")
        return "\n".join(self.sync())

    def evaluate(self):
        self.send("eval")
        return int(self.readline().split()[1])

    def perft(self, depth):
        self.send(f"perft {depth}")
        return int(self.readline().split()[2])

    def analyse(self, depth=None, nodes=None, movetime=None, multipv=1, on_info=None):
        """Search the current position. Returns the lines of the deepest completed iteration:
        [{'depth', 'multipv', 'score_cp' or 'mate', 'pv': [...]}, ...] best first.
        Scores are from the side to move's point of view."""
        cmd = "go"
        if depth: cmd += f" depth {depth}"
        if nodes: cmd += f" nodes {nodes}"
        if movetime: cmd += f" movetime {movetime}"
        if not (depth or nodes or movetime): cmd += " movetime 2000"
        cmd += f" multipv {multipv}"
        self.send(cmd)
        latest = {}
        while True:
            l = self.readline()
            if l.startswith("bestmove"):
                break
            if l.startswith("info gameover"):
                continue
            m = re.match(r"info depth (\d+) multipv (\d+) score (cp|mate) (-?\d+) nodes (\d+) time (\d+) nps (\d+) pv ?(.*)", l)
            if m:
                d, k = int(m.group(1)), int(m.group(2))
                info = {"depth": d, "multipv": k, "nodes": int(m.group(5)), "time_ms": int(m.group(6)),
                        "pv": m.group(8).split()}
                info["mate" if m.group(3) == "mate" else "score_cp"] = int(m.group(4))
                if k == 1:
                    latest = {}
                latest[k] = info
                if on_info:
                    on_info(info)
        return [latest[k] for k in sorted(latest)]

    def close(self):
        try:
            self.send("quit")
            self.p.wait(timeout=5)
        except Exception:
            self.p.kill()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
