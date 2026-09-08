"""
theme.py -- one place for every colour the instrument uses.

The series colours are not decorative. They were chosen as a set and checked
for lightness band, chroma, contrast against the dark chart surface, and
separation under simulated colour-vision deficiency, so the four field traces
stay tellable apart by everyone looking at them. Changing one in isolation
breaks that guarantee -- re-check the set if you do.

Field and temperature live on separate stacked plots rather than sharing one
frame with two y-scales. A second y-axis makes the same pixel height mean two
different things, which is the fastest way to draw a misleading chart; two
plots with a shared x-axis show the same relationship without the lie.
"""

# -- surfaces ------------------------------------------------------------
WINDOW = "#121211"        # application background, behind everything
SURFACE = "#1a1a19"       # chart surface -- the colours were checked on this
PANEL = "#1f1f1e"         # dock and group backgrounds, one step lifted
PANEL_HI = "#262624"      # hover / selected row
BORDER = "#33332f"
GRID = "#2b2b28"          # recessive: visible, never competing with data

# -- ink -----------------------------------------------------------------
TEXT = "#ffffff"
TEXT_2 = "#c3c2b7"
TEXT_MUTED = "#8a8a80"

# -- status (reserved -- never reused for a series) -----------------------
GOOD = "#0ca30c"
WARNING = "#fab219"
SERIOUS = "#ec835a"
CRITICAL = "#d03b3b"

# -- series --------------------------------------------------------------
# Field: four traces sharing one plot, so they are checked pairwise.
# Temperature: its own plot, so its two only need to differ from each other.
BX = "#3987e5"      # blue
BY = "#d95926"      # orange
BZ = "#199e70"      # aqua
BMAG = "#c98500"    # yellow
TDIE = "#d55181"    # magenta
TRTD = "#9085e9"    # violet

ACCENT = BX

# key, label, colour, unit, plot
CHANNELS = [
    ("bx",   "Bx",    BX,   "mT",   "field"),
    ("by",   "By",    BY,   "mT",   "field"),
    ("bz",   "Bz",    BZ,   "mT",   "field"),
    ("mag",  "|B|",   BMAG, "mT",   "field"),
    ("temp", "T die", TDIE, "°C", "temp"),
    ("rtd",  "T rtd", TRTD, "°C", "temp"),
]

FIELD_CHANNELS = [c for c in CHANNELS if c[4] == "field"]
TEMP_CHANNELS = [c for c in CHANNELS if c[4] == "temp"]
COLOR = {key: colour for key, _, colour, _, _ in CHANNELS}
LABEL = {key: label for key, label, _, _, _ in CHANNELS}
UNIT = {key: unit for key, _, _, unit, _ in CHANNELS}

MONO = "DejaVu Sans Mono, Consolas, Menlo, monospace"


def stylesheet():
    """Qt stylesheet for the whole application."""
    return f"""
    QWidget {{
        background: {WINDOW};
        color: {TEXT};
        font-size: 12px;
    }}
    QMainWindow::separator {{ background: {BORDER}; width: 1px; height: 1px; }}

    QGroupBox {{
        background: {PANEL};
        border: 1px solid {BORDER};
        border-radius: 6px;
        margin-top: 16px;
        padding: 10px 10px 8px 10px;
        font-weight: 600;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 10px;
        padding: 0 4px;
        color: {TEXT_MUTED};
        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: 1px;
    }}

    QLabel {{ background: transparent; }}
    QLabel[role="hint"] {{ color: {TEXT_MUTED}; font-size: 11px; }}
    QLabel[role="readout"] {{
        font-family: {MONO}; font-size: 12px; color: {TEXT_2};
        background: transparent;
    }}

    QPushButton {{
        background: {PANEL_HI};
        border: 1px solid {BORDER};
        border-radius: 5px;
        padding: 6px 12px;
        color: {TEXT};
    }}
    QPushButton:hover {{ border-color: {ACCENT}; }}
    QPushButton:pressed {{ background: {BORDER}; }}
    QPushButton:disabled {{ color: {TEXT_MUTED}; background: {PANEL}; }}
    QPushButton[role="primary"] {{
        background: {ACCENT}; border-color: {ACCENT}; color: #ffffff;
        font-weight: 600;
    }}
    QPushButton[role="primary"]:hover {{ background: #4a94ea; }}
    QPushButton[role="danger"] {{
        background: {CRITICAL}; border-color: {CRITICAL}; color: #ffffff;
        font-weight: 600;
    }}
    QPushButton:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}

    QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit {{
        background: {WINDOW};
        border: 1px solid {BORDER};
        border-radius: 5px;
        padding: 5px 8px;
        selection-background-color: {ACCENT};
    }}
    QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QLineEdit:focus {{
        border-color: {ACCENT};
    }}
    QComboBox::drop-down {{ border: none; width: 18px; }}
    QComboBox QAbstractItemView {{
        background: {PANEL};
        border: 1px solid {BORDER};
        selection-background-color: {ACCENT};
        outline: none;
    }}

    QCheckBox, QRadioButton {{ spacing: 8px; background: transparent; }}
    QCheckBox::indicator, QRadioButton::indicator {{
        width: 14px; height: 14px;
        border: 1px solid {TEXT_MUTED};
        background: {WINDOW};
    }}
    QCheckBox::indicator {{ border-radius: 3px; }}
    QRadioButton::indicator {{ border-radius: 7px; }}
    QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
        background: {ACCENT}; border-color: {ACCENT};
    }}

    QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 6px;
                        background: {SURFACE}; }}
    QTabBar::tab {{
        background: transparent;
        color: {TEXT_MUTED};
        padding: 7px 16px;
        margin-right: 2px;
        border: 1px solid transparent;
        border-top-left-radius: 6px; border-top-right-radius: 6px;
    }}
    QTabBar::tab:hover {{ color: {TEXT_2}; }}
    QTabBar::tab:selected {{
        color: {TEXT}; background: {SURFACE};
        border-color: {BORDER}; border-bottom-color: {SURFACE};
    }}

    QTableWidget {{
        background: {PANEL};
        alternate-background-color: {WINDOW};
        gridline-color: {BORDER};
        border: 1px solid {BORDER};
        border-radius: 6px;
        font-family: {MONO};
        font-size: 11px;
    }}
    QHeaderView::section {{
        background: {WINDOW};
        color: {TEXT_MUTED};
        border: none;
        border-bottom: 1px solid {BORDER};
        padding: 5px;
        font-family: -apple-system, "Segoe UI", sans-serif;
        font-size: 10px;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }}
    QTableWidget::item {{ padding: 3px 6px; }}

    QPlainTextEdit {{
        background: {SURFACE};
        border: 1px solid {BORDER};
        border-radius: 6px;
        font-family: {MONO};
        font-size: 11px;
        color: {TEXT_2};
    }}

    QDockWidget {{ titlebar-close-icon: none; }}
    QDockWidget::title {{
        background: {WINDOW};
        padding: 7px 10px;
        border-bottom: 1px solid {BORDER};
        color: {TEXT_MUTED};
        font-size: 10px;
        text-transform: uppercase;
        letter-spacing: 1px;
    }}

    QToolBar {{
        background: {WINDOW};
        border-bottom: 1px solid {BORDER};
        padding: 6px 8px;
        spacing: 6px;
    }}
    QStatusBar {{
        background: {WINDOW};
        border-top: 1px solid {BORDER};
        color: {TEXT_MUTED};
        font-family: {MONO};
        font-size: 11px;
    }}
    QStatusBar::item {{ border: none; }}

    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
    QScrollBar::handle:vertical {{
        background: {BORDER}; border-radius: 5px; min-height: 30px;
    }}
    QScrollBar::handle:vertical:hover {{ background: {TEXT_MUTED}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; }}
    QScrollBar::handle:horizontal {{
        background: {BORDER}; border-radius: 5px; min-width: 30px;
    }}

    QToolTip {{
        background: {PANEL};
        color: {TEXT};
        border: 1px solid {BORDER};
        padding: 5px;
    }}
    QSplitter::handle {{ background: {BORDER}; }}
    """
