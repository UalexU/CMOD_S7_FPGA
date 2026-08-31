"""
Load the bitstream + ELF onto the Zynq over JTAG, from Python.

This does NOT run your C code. It drives XSDB (the same engine Vitis uses
behind the Run button) and tells it to push files onto the board.

Nothing is hardcoded to one machine: paths are found relative to this
script, and the Vitis install is discovered from the environment.

Usage:
    python run_board.py                  # bitstream + ELF (after power-on)
    python run_board.py --fast           # ELF only (code-only rebuild)
    python run_board.py --app MyApp      # pick the app explicitly
    python run_board.py --show           # print what it found, run nothing

Overrides, if discovery guesses wrong:
    --vitis  <path>   or  set VITIS_BIN=C:\\Xilinx\\2025.1\\Vitis\\bin
    --ws     <path>   workspace root (default: this script's folder)

Close any active Vitis debug session first -- only one program can own
the JTAG cable at a time.
"""

import argparse
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent      # the workspace root
TARGET = "*Cortex-A9*#0"                            # Zynq-7000, first ARM core
                                                    # UltraScale+: *Cortex-A53*#0
                                                    # MicroBlaze:  *MicroBlaze*#0

# Where Vitis tends to live, newest version.
INSTALL_ROOTS = [
    r"C:\Xilinx",
    r"C:\Program Files\Xilinx",
    "/tools/Xilinx",
    "/opt/Xilinx",
    str(pathlib.Path.home() / "Xilinx"),
]


# ------------------------------------------------------------- discovery

def find_vitis():
    """Locate xsdb/xsct. Four strategies, cheapest first."""
    names = ("xsdb.bat", "xsct.bat") if os.name == "nt" else ("xsdb", "xsct")

    def first_in(folder):
        folder = pathlib.Path(folder)
        for n in names:
            if (folder / n).is_file():
                return folder / n
        return None

    # 1. explicit override
    if os.environ.get("VITIS_BIN"):
        hit = first_in(os.environ["VITIS_BIN"])
        if hit:
            return hit

    # 2. Xilinx sets this when its settings script has been sourced
    for var in ("XILINX_VITIS", "XILINX_SDK"):
        if os.environ.get(var):
            hit = first_in(pathlib.Path(os.environ[var]) / "bin")
            if hit:
                return hit

    # 3. already on PATH
    for n in names:
        found = shutil.which(n)
        if found:
            return pathlib.Path(found)

    # 4. hunt through the usual install locations, newest version first
    candidates = []
    for root in INSTALL_ROOTS:
        root = pathlib.Path(root)
        if root.is_dir():
            candidates += list(root.glob("*/Vitis/bin"))
    for folder in sorted(candidates, key=lambda p: p.parts[-3], reverse=True):
        hit = first_in(folder)
        if hit:
            return hit

    return None


def find_app(ws, name=None):
    """The application folder: the one holding build/<something>.elf.

    A workspace usually holds several built apps. Without --app we take the
    most recently built one, which is almost always the one you just
    compiled -- and say so, so the choice is never silent.
    """
    if name:
        app = ws / name
        if not app.is_dir():
            sys.exit(f"No such application folder: {app}")
        return app

    elves = [e for e in ws.glob("*/build/*.elf") if e.is_file()]
    if not elves:
        sys.exit(f"No built application found under {ws}\n"
                 f"Build it in Vitis first, or pass --app <folder>.")

    elves.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    app = elves[0].parent.parent

    others = sorted({e.parent.parent.name for e in elves} - {app.name})
    if others:
        print(f"note: picking the most recently built app; also available: "
              f"{'  '.join(others)}  (use --app to choose)")
    return app


def newest(paths):
    """Most recently modified match, or None."""
    paths = [p for p in paths if p.is_file()]
    return max(paths, key=lambda p: p.stat().st_mtime) if paths else None


def find_artifacts(ws, app):
    """The three files XSDB needs. Globbed, so project names don't matter."""
    elf = newest(app.glob("build/*.elf"))
    ps7 = newest(app.glob("_ide/psinit/ps7_init.tcl"))
    bit = newest(app.glob("_ide/bitstream/*.bit"))

    # Fall back to the platform's exported copies.
    if bit is None:
        bit = newest(ws.glob("*/export/*/hw/*.bit")) or newest(ws.glob("*/hw/*.bit"))
    if ps7 is None:
        ps7 = newest(ws.glob("*/export/*/hw/ps7_init.tcl")) or newest(ws.glob("*/hw/ps7_init.tcl"))

    return bit, ps7, elf


# ---------------------------------------------------------------- script

def build_tcl(bit, ps7, elf, skip_bitstream):
    """The same steps Vitis performs, written out explicitly."""
    lines = [
        "connect",
        f'targets -set -nocase -filter {{name =~ "{TARGET}"}}',
        "rst -system",
        "after 3000",
        f'targets -set -nocase -filter {{name =~ "{TARGET}"}}',

        # 1. configure the PS: clocks, DDR, MIO pin routing
        f'source "{ps7.as_posix()}"',
        "ps7_init",
        "ps7_post_config",
    ]

    # 2. build the peripherals in the PL (the slow ~4 MB step)
    if not skip_bitstream:
        lines.append(f'fpga -file "{bit.as_posix()}"')

    # 3. load the program and start it
    lines += [
        f'targets -set -nocase -filter {{name =~ "{TARGET}"}}',
        f'dow "{elf.as_posix()}"',
        "con",
        "exit",
    ]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(description="Program the Zynq over JTAG.")
    ap.add_argument("--fast",  action="store_true",
                    help="skip the bitstream (only safe if the board has not "
                         "lost power since it was last configured)")
    ap.add_argument("--show",  action="store_true",
                    help="print what was found and the script, then exit")
    ap.add_argument("--app",   help="application folder name (default: autodetect)")
    ap.add_argument("--ws",    help="workspace root (default: this script's folder)")
    ap.add_argument("--vitis", help="folder containing xsdb/xsct")
    args = ap.parse_args()

    ws = pathlib.Path(args.ws).resolve() if args.ws else HERE
    if not ws.is_dir():
        sys.exit(f"Workspace not found: {ws}")

    if args.vitis:
        os.environ["VITIS_BIN"] = args.vitis
    xsdb = find_vitis()
    if xsdb is None:
        sys.exit("Could not find xsdb or xsct.\n"
                 "Pass --vitis <path-to-Vitis/bin>, or set VITIS_BIN,\n"
                 "or run this from a shell where Vitis settings are sourced.")

    app = find_app(ws, args.app)
    bit, ps7, elf = find_artifacts(ws, app)

    print(f"workspace : {ws}")
    print(f"app       : {app.name}")
    print(f"xsdb      : {xsdb}")
    print(f"bitstream : {bit if bit else '(none found)'}")
    print(f"ps7_init  : {ps7 if ps7 else '(none found)'}")
    print(f"elf       : {elf if elf else '(none found)'}")

    missing = [n for n, p in (("ELF", elf), ("ps7_init.tcl", ps7)) if p is None]
    if not args.fast and bit is None:
        missing.append("bitstream")
    if missing:
        sys.exit("Missing: " + ", ".join(missing))

    tcl = build_tcl(bit, ps7, elf, args.fast)
    print(f"\n--- script ---\n{tcl}--------------")

    if args.show:
        return

    with tempfile.NamedTemporaryFile("w", suffix=".tcl", delete=False) as fh:
        fh.write(tcl)
        tcl_path = fh.name

    try:
        result = subprocess.run([str(xsdb), tcl_path],
                                capture_output=True, text=True, timeout=180)
        print(result.stdout)
        if result.returncode != 0:
            print(result.stderr, file=sys.stderr)
            sys.exit("XSDB failed -- see output above")
        print("Board is running.")
    finally:
        pathlib.Path(tcl_path).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
