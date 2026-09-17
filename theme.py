"""
theme.py -- one place for every colour the instrument uses, in both modes.

The series colours are not decorative. Each mode's set was chosen and checked
together for lightness band, chroma, contrast against that mode's chart
surface, and separation under simulated colour-vision deficiency, so the four
field traces stay tellable apart by everyone looking at them. Changing one in
isolation breaks that guarantee -- re-check the set if you do.

Light mode's aqua, yellow and magenta sit just under 3:1 against the light
surface. That is allowed only because identity never rests on colour alone
here: every plot carries a legend and the statistics table names each channel
in text. Do not remove those.

Field and temperature live on separate stacked plots rather than sharing one
frame with two y-scales. A second y-axis makes the same pixel height mean two
different things, which is the fastest way to draw a misleading chart; two
plots with a shared x-axis show the same relationship without the lie.

    theme.set_mode("light")     # or "dark"
    theme.color("bx")           # the active mode's colour for a channel
    theme.SURFACE               # any palette field, resolved on the active mode
"""

MONO = "DejaVu Sans Mono, Consolas, Menlo, monospace"

# key, label, unit, which plot
CHANNELS = [
    ("bx",   "Bx",    "mT",  "field"),
    ("by",   "By",    "mT",  "field"),
    ("bz",   "Bz",    "mT",  "field"),
    ("mag",  "|B|",   "mT",  "field"),
    ("temp", "T die", "°C",  "temp"),
    ("rtd",  "T rtd", "°C",  "temp"),
]

FIELD_CHANNELS = [c for c in CHANNELS if c[3] == "field"]
TEMP_CHANNELS = [c for c in CHANNELS if c[3] == "temp"]
LABEL = {key: label for key, label, _, _ in CHANNELS}
UNIT = {key: unit for key, _, unit, _ in CHANNELS}


class Palette:
    """Every colour for one mode. Field names are read through the module,
    so call sites never have to know which mode is active."""

    def __init__(self, mode, **colours):
        self.mode = mode
        self.__dict__.update(colours)

    def color(self, key):
        return self.SERIES[key]


# Status colours are fixed in both modes and never reused for a series, so a
# warning can never be mistaken for a trace.
_STATUS = dict(GOOD="#0ca30c", WARNING="#fab219",
               SERIOUS="#ec835a", CRITICAL="#d03b3b")

DARK = Palette(
    "dark",
    WINDOW="#121211", SURFACE="#1a1a19", PANEL="#1f1f1e", PANEL_HI="#262624",
    BORDER="#33332f", GRID="#2b2b28", AXIS="#8a8a80", ZERO="#4a4a44",
    TEXT="#ffffff", TEXT_2="#c3c2b7", TEXT_MUTED="#8a8a80",
    SHADOW="rgba(0, 0, 0, 0.5)",
    SERIES={"bx": "#3987e5", "by": "#d95926", "bz": "#199e70",
            "mag": "#c98500", "temp": "#d55181", "rtd": "#9085e9"},
    **_STATUS,
)

LIGHT = Palette(
    "light",
    WINDOW="#f2f1ed", SURFACE="#fcfcfb", PANEL="#ffffff", PANEL_HI="#eceae4",
    BORDER="#d5d3ca", GRID="#e2e0d8", AXIS="#6b6a62", ZERO="#b6b4aa",
    TEXT="#0b0b0b", TEXT_2="#52514e", TEXT_MUTED="#7a7972",
    SHADOW="rgba(0, 0, 0, 0.12)",
    SERIES={"bx": "#2a78d6", "by": "#eb6834", "bz": "#1baf7a",
            "mag": "#eda100", "temp": "#e87ba4", "rtd": "#4a3aa7"},
    **_STATUS,
)

MODES = {"dark": DARK, "light": LIGHT}
_active = DARK


def set_mode(mode):
    global _active
    _active = MODES.get(mode, DARK)
    return _active


def mode():
    return _active.mode


def palette():
    return _active


def color(key):
    return _active.SERIES[key]


def __getattr__(name):
    """theme.SURFACE and friends resolve against whichever mode is active,
    so a toggle does not mean rewriting every call site."""
    try:
        return getattr(_active, name)
    except AttributeError:
        raise AttributeError(f"module 'theme' has no attribute {name!r}") from None


def stylesheet():
    """Qt stylesheet for the whole application, in the active mode."""
    p = _active
    accent = p.SERIES["bx"]
    return f"""
    QWidget {{
        background: {p.WINDOW};
        color: {p.TEXT};
        font-size: 12px;
    }}
    QMainWindow::separator {{ background: {p.BORDER}; width: 1px; height: 1px; }}

    QMenuBar {{ background: {p.WINDOW}; border-bottom: 1px solid {p.BORDER};
                padding: 2px 4px; }}
    QMenuBar::item {{ background: transparent; padding: 5px 10px;
                      border-radius: 4px; }}
    QMenuBar::item:selected {{ background: {p.PANEL_HI}; }}
    QMenu {{ background: {p.PANEL}; border: 1px solid {p.BORDER};
             padding: 4px; }}
    QMenu::item {{ padding: 6px 24px 6px 24px; border-radius: 4px; }}
    QMenu::item:selected {{ background: {accent}; color: #ffffff; }}
    QMenu::separator {{ height: 1px; background: {p.BORDER}; margin: 4px 8px; }}

    QGroupBox {{
        background: {p.PANEL};
        border: 1px solid {p.BORDER};
        border-radius: 6px;
        margin-top: 16px;
        padding: 10px 10px 8px 10px;
        font-weight: 600;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 10px;
        padding: 0 4px;
        color: {p.TEXT_MUTED};
        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: 1px;
    }}

    QLabel {{ background: transparent; }}
    QLabel[role="hint"] {{ color: {p.TEXT_MUTED}; font-size: 11px; }}
    QLabel[role="readout"] {{
        font-family: {MONO}; font-size: 12px; color: {p.TEXT_2};
        background: transparent;
    }}

    QPushButton {{
        background: {p.PANEL_HI};
        border: 1px solid {p.BORDER};
        border-radius: 5px;
        padding: 6px 12px;
        color: {p.TEXT};
    }}
    QPushButton:hover {{ border-color: {accent}; }}
    QPushButton:pressed {{ background: {p.BORDER}; }}
    QPushButton:disabled {{ color: {p.TEXT_MUTED}; background: {p.PANEL}; }}
    QPushButton[role="primary"] {{
        background: {accent}; border-color: {accent}; color: #ffffff;
        font-weight: 600;
    }}
    QPushButton[role="danger"] {{
        background: {p.CRITICAL}; border-color: {p.CRITICAL}; color: #ffffff;
        font-weight: 600;
    }}
    QPushButton:checked {{ background: {accent}; border-color: {accent};
                           color: #ffffff; }}

    QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit {{
        background: {p.WINDOW};
        border: 1px solid {p.BORDER};
        border-radius: 5px;
        padding: 5px 8px;
        color: {p.TEXT};
        selection-background-color: {accent};
    }}
    QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QLineEdit:focus {{
        border-color: {accent};
    }}
    QComboBox::drop-down {{ border: none; width: 18px; }}
    QComboBox QAbstractItemView {{
        background: {p.PANEL};
        border: 1px solid {p.BORDER};
        selection-background-color: {accent};
        outline: none;
    }}

    QCheckBox, QRadioButton {{ spacing: 8px; background: transparent; }}
    QCheckBox::indicator, QRadioButton::indicator {{
        width: 14px; height: 14px;
        border: 1px solid {p.TEXT_MUTED};
        background: {p.WINDOW};
    }}
    QCheckBox::indicator {{ border-radius: 3px; }}
    QRadioButton::indicator {{ border-radius: 7px; }}
    QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
        background: {accent}; border-color: {accent};
    }}

    QTabWidget::pane {{ border: 1px solid {p.BORDER}; border-radius: 6px;
                        background: {p.SURFACE}; }}
    QTabBar::tab {{
        background: transparent;
        color: {p.TEXT_MUTED};
        padding: 7px 16px;
        margin-right: 2px;
        border: 1px solid transparent;
        border-top-left-radius: 6px; border-top-right-radius: 6px;
    }}
    QTabBar::tab:hover {{ color: {p.TEXT_2}; }}
    QTabBar::tab:selected {{
        color: {p.TEXT}; background: {p.SURFACE};
        border-color: {p.BORDER}; border-bottom-color: {p.SURFACE};
    }}

    QTableWidget {{
        background: {p.PANEL};
        alternate-background-color: {p.WINDOW};
        gridline-color: {p.BORDER};
        border: 1px solid {p.BORDER};
        border-radius: 6px;
        font-family: {MONO};
        font-size: 11px;
    }}
    QHeaderView::section {{
        background: {p.WINDOW};
        color: {p.TEXT_MUTED};
        border: none;
        border-bottom: 1px solid {p.BORDER};
        padding: 5px;
        font-family: -apple-system, "Segoe UI", sans-serif;
        font-size: 10px;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }}
    QTableWidget::item {{ padding: 3px 6px; }}

    QPlainTextEdit {{
        background: {p.SURFACE};
        border: 1px solid {p.BORDER};
        border-radius: 6px;
        font-family: {MONO};
        font-size: 11px;
        color: {p.TEXT_2};
    }}

    QDockWidget {{ color: {p.TEXT_MUTED}; }}
    QDockWidget::title {{
        background: {p.WINDOW};
        padding: 7px 10px;
        border-bottom: 1px solid {p.BORDER};
        color: {p.TEXT_MUTED};
        font-size: 10px;
        text-transform: uppercase;
        letter-spacing: 1px;
    }}

    QToolBar {{
        background: {p.WINDOW};
        border-bottom: 1px solid {p.BORDER};
        padding: 6px 8px;
        spacing: 6px;
    }}
    QToolButton {{ padding: 5px 10px; border-radius: 5px; color: {p.TEXT}; }}
    QToolButton:hover {{ background: {p.PANEL_HI}; }}
    QToolButton:checked {{ background: {accent}; color: #ffffff; }}

    QStatusBar {{
        background: {p.WINDOW};
        border-top: 1px solid {p.BORDER};
        color: {p.TEXT_MUTED};
        font-family: {MONO};
        font-size: 11px;
    }}
    QStatusBar::item {{ border: none; }}

    QScrollArea {{ border: none; background: {p.WINDOW}; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
    QScrollBar::handle:vertical {{
        background: {p.BORDER}; border-radius: 5px; min-height: 30px;
    }}
    QScrollBar::handle:vertical:hover {{ background: {p.TEXT_MUTED}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; }}
    QScrollBar::handle:horizontal {{
        background: {p.BORDER}; border-radius: 5px; min-width: 30px;
    }}

    QToolTip {{
        background: {p.PANEL};
        color: {p.TEXT};
        border: 1px solid {p.BORDER};
        padding: 5px;
    }}
    QSplitter::handle {{ background: {p.BORDER}; }}
    """