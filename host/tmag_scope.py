"""
tmag_scope.py -- instrument front end for the TMAG5170 + MAX31865 board.

    python tmag_scope.py            # pick a port in the UI
    python tmag_scope.py --fake     # synthetic source, no board needed

Owns no serial code: it asks sensor.SensorReader for samples, and sends
commands back the same way. Everything below the acquisition model is
display, so a different toolkit could sit on the same model unchanged.

Four views over one rolling buffer:
    Strip chart   field and temperature against time, on separate y-scales
                  in separate frames -- never one frame with two axes
    XY vector     Bx against By, which draws a circle for a rotating magnet
                  and an ellipse the moment an axis gain is wrong
    Spectrum      where the energy sits; mechanical vibration shows here
                  long before it is visible in the strip chart
    Distribution  per-channel histogram with sigma, for noise-floor work

Requires: pip install PySide6 pyqtgraph numpy pandas openpyxl pyserial
"""

import argparse
import sys
import time
from datetime import datetime

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QSettings, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDockWidget, QDoubleSpinBox,
    QFileDialog, QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QMainWindow, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QSizePolicy, QSpinBox, QSplitter,
    QTabWidget, QTableWidget, QTableWidgetItem, QToolBar, QVBoxLayout,
    QWidget,
)

import sensor
import theme

pg.setConfigOptions(antialias=True)

FIELD_KEYS = [c[0] for c in theme.FIELD_CHANNELS]
TEMP_KEYS = [c[0] for c in theme.TEMP_CHANNELS]
ALL_KEYS = FIELD_KEYS + TEMP_KEYS

BUFFER_SAMPLES = 400_000        # ~22 min at 300 Hz, ~1.5 s of RAM at 8 bytes


# ===================================================================== model

class Ring:
    """Fixed-capacity rolling store with contiguous reads.

    Every column is written twice, half a buffer apart, so any recent window
    is one slice with no copy and no np.roll -- the difference between a
    plot that keeps up at 300 Hz and one that does not.
    """

    def __init__(self, capacity, columns):
        self.capacity = capacity
        self.columns = list(columns)
        self._buf = np.zeros((2 * capacity, len(self.columns)), dtype=np.float64)
        self.written = 0

    def clear(self):
        self.written = 0

    def append(self, block):
        """block: (k, ncols) array, oldest row first."""
        k = len(block)
        if k == 0:
            return
        if k >= self.capacity:              # a single burst larger than the
            block = block[-self.capacity:]  # buffer: keep only what fits
            k = self.capacity

        start = self.written % self.capacity
        idx = (start + np.arange(k)) % self.capacity
        self._buf[idx] = block
        self._buf[idx + self.capacity] = block
        self.written += k

    def __len__(self):
        return min(self.written, self.capacity)

    def tail(self, count):
        """The most recent `count` rows, contiguous. Never copies."""
        m = min(count, len(self))
        if m == 0:
            return self._buf[:0]
        end = self.written % self.capacity + self.capacity
        return self._buf[end - m:end]


class Acquisition:
    """The rolling buffer plus everything derived from it.

    Deliberately free of Qt: the views ask it for arrays, it never asks the
    views for anything. Swapping the front end means rewriting the widgets,
    not this.
    """

    COLUMNS = ["t"] + ALL_KEYS

    def __init__(self, capacity=BUFFER_SAMPLES):
        self.ring = Ring(capacity, self.COLUMNS)
        self.reader = None
        self.t0 = None
        self.tare = {k: 0.0 for k in FIELD_KEYS}
        self._recent = []               # (host_time,) for the measured rate

    # -- lifecycle -------------------------------------------------------

    def attach(self, reader):
        self.reader = reader
        self.t0 = time.perf_counter()
        self.ring.clear()
        self._recent.clear()

    def detach(self):
        if self.reader is not None:
            self.reader.stop()
            self.reader = None

    def clear(self):
        self.ring.clear()
        self._recent.clear()
        self.t0 = time.perf_counter()

    # -- ingest ----------------------------------------------------------

    def pump(self):
        """Drain the reader and timestamp what arrived. Returns the count.

        A drain can return several samples at once, especially right after
        connecting to a board that is already streaming. Stamping them all
        with the same instant gives a zero-width x-range, which makes both
        the rate calculation and autoscaling degenerate, so spread them
        across the interval since the previous pump instead.
        """
        if self.reader is None:
            return 0
        samples = self.reader.drain()
        if not samples:
            return 0

        now = time.perf_counter() - self.t0
        if len(self.ring):
            prev = self.ring.tail(1)[0, 0]
        else:
            prev = max(0.0, now - 0.05)
        span = max(now - prev, 1e-6)
        n = len(samples)

        block = np.empty((n, len(self.COLUMNS)), dtype=np.float64)
        block[:, 0] = prev + span * (np.arange(1, n + 1) / n)
        block[:, 1:] = np.asarray(samples, dtype=np.float64)
        self.ring.append(block)

        self._recent.append((time.perf_counter(), n))
        cutoff = time.perf_counter() - 2.0
        while self._recent and self._recent[0][0] < cutoff:
            self._recent.pop(0)
        return n

    # -- derived ---------------------------------------------------------

    def measured_rate(self):
        """Samples per second actually reaching this process, over ~2 s.
        A host-side figure: it says nothing about how fast the sensor
        converts, only about what survives the trip."""
        if len(self._recent) < 2:
            return None
        span = self._recent[-1][0] - self._recent[0][0]
        if span <= 0:
            return None
        return sum(n for _, n in self._recent[1:]) / span

    def window(self, seconds, smooth=1):
        """(t, {key: values}) for the last `seconds`, tare and smoothing
        applied. Returns empty arrays when nothing has arrived yet."""
        n = len(self.ring)
        if n == 0:
            return np.empty(0), {k: np.empty(0) for k in ALL_KEYS}

        rate = self.measured_rate() or 20.0
        want = int(max(seconds * rate * 1.4, 64))
        rows = self.ring.tail(min(want, n))

        t = rows[:, 0]
        if len(t):
            keep = t >= t[-1] - seconds
            rows = rows[keep]
            t = rows[:, 0]

        cols = {key: rows[:, i + 1].copy() for i, key in enumerate(ALL_KEYS)}

        # Tare shifts the components; the magnitude is then recomputed from
        # them rather than shifted itself -- |B| minus an offset is not the
        # magnitude of anything.
        if any(self.tare.values()):
            for key in ("bx", "by", "bz"):
                cols[key] -= self.tare[key]
            cols["mag"] = np.sqrt(cols["bx"] ** 2 + cols["by"] ** 2
                                  + cols["bz"] ** 2)

        if smooth > 1 and len(t) > smooth:
            kernel = np.ones(smooth) / smooth
            trim = smooth - 1
            t = t[trim:]
            for key in cols:
                cols[key] = np.convolve(cols[key], kernel, mode="valid")

        return t, cols

    def set_tare(self, seconds=1.0):
        """Zero the field against whatever is present right now."""
        _, cols = self.window(seconds)
        if not len(cols["bx"]):
            return False
        prev = self.tare
        self.tare = {k: prev[k] + float(np.mean(cols[k])) for k in FIELD_KEYS}
        self.tare["mag"] = 0.0
        return True

    def clear_tare(self):
        self.tare = {k: 0.0 for k in FIELD_KEYS}

    @property
    def tared(self):
        return any(abs(v) > 1e-9 for v in self.tare.values())


# ===================================================================== views

_STYLED = []      # (widget, xlabel, ylabel) -- re-run when the mode changes
_LEGENDS = []     # legends need their own colours re-applied


def _style(plot, xlabel=None, ylabel=None, remember=True):
    """Chrome that reads as structure, not decoration.

    The axis line is the frame of the measurement; the grid is only a ruler
    behind it. Giving them two different weights and two different colours
    is what lets the eye find the axes without hunting -- a single hairline
    for both makes the plot look like graph paper with no edges.
    """
    item = plot.getPlotItem() if hasattr(plot, "getPlotItem") else plot
    p = theme.palette()
    item.showGrid(x=True, y=True, alpha=0.11)
    item.getViewBox().setDefaultPadding(0.02)
    font = QFont("DejaVu Sans Mono", 9)
    for side in ("left", "bottom", "right", "top"):
        axis = item.getAxis(side)
        axis.setPen(pg.mkPen(p.AXIS, width=2))
        axis.setTextPen(pg.mkPen(p.TEXT_2))
        axis.setStyle(tickFont=font, tickLength=-6, tickTextOffset=6)
        # Off, or pyqtgraph silently folds a x0.001 multiplier into the axis
        # label when values are small and the tick numbers stop being mT.
        axis.enableAutoSIPrefix(False)
    if xlabel:
        item.setLabel("bottom", xlabel, **{"color": p.TEXT_2,
                                           "font-size": "10pt"})
    if ylabel:
        item.setLabel("left", ylabel, **{"color": p.TEXT_2,
                                         "font-size": "10pt"})
    if remember:
        plot.setBackground(p.SURFACE)
        _STYLED.append((plot, xlabel, ylabel))
    return item


def _legend(item):
    legend = item.addLegend(offset=(-8, 8), verSpacing=-4)
    _LEGENDS.append(legend)
    _dress_legend(legend)
    return legend


def _dress_legend(legend):
    p = theme.palette()
    legend.setLabelTextColor(p.TEXT_2)
    legend.setBrush(pg.mkBrush(QColor(p.PANEL)))
    legend.setPen(pg.mkPen(p.BORDER))


def restyle_plots():
    """Re-apply the active mode to every plot frame and legend built so far."""
    for plot, xlabel, ylabel in _STYLED:
        plot.setBackground(theme.SURFACE)
        _style(plot, xlabel, ylabel, remember=False)
    for legend in _LEGENDS:
        _dress_legend(legend)


def _pen(colour, width=2):
    # 2px lines: heavy enough to read against the grid, thin enough that
    # four of them crossing stays legible.
    return pg.mkPen(colour, width=width)


def _fill(colour, alpha=48):
    tint = QColor(colour)
    tint.setAlpha(alpha)
    return tint


def fit_circle(x, y):
    """Kasa algebraic circle fit -> (cx, cy, r, rms residual) or None.

    A magnet turning in the XY plane should trace a circle centred on zero.
    The fitted centre is therefore the residual offset on each axis, the
    radius is the planar field strength, and the residual says whether the
    trail is a circle at all -- unequal gain between X and Y draws an
    ellipse, which a circle fit cannot follow and reports as a large
    residual. All three are numbers you cannot read off a strip chart.
    """
    if len(x) < 12:
        return None
    a = np.column_stack([2 * x, 2 * y, np.ones(len(x))])
    b = x ** 2 + y ** 2
    try:
        (cx, cy, c), *_ = np.linalg.lstsq(a, b, rcond=None)
    except np.linalg.LinAlgError:
        return None
    inner = c + cx ** 2 + cy ** 2
    if inner <= 0:
        return None
    r = float(np.sqrt(inner))
    residual = float(np.sqrt(np.mean(
        (np.hypot(x - cx, y - cy) - r) ** 2)))
    return float(cx), float(cy), r, residual


class StripChartView(QWidget):
    """Field and temperature against time.

    Two frames, one shared time axis. Temperature could technically be
    squeezed onto a right-hand axis of the field plot, but then a pixel of
    height means millitesla in one trace and degrees in the next, and any
    apparent correlation between them is an artefact of whatever scaling
    happened to be chosen. Separate frames, shared x, no lie.
    """

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.readout = QLabel("move the cursor over the plot to read values")
        self.readout.setProperty("role", "readout")
        self.readout.setContentsMargins(10, 6, 10, 6)
        layout.addWidget(self.readout)

        splitter = QSplitter(Qt.Vertical)
        splitter.setChildrenCollapsible(False)
        layout.addWidget(splitter, 1)

        self.field_plot = pg.PlotWidget()
        self.temp_plot = pg.PlotWidget()
        field = _style(self.field_plot, None, "B [mT]")
        temp = _style(self.temp_plot, "time [s]", "T [°C]")
        temp.setXLink(field)
        field.getAxis("bottom").setStyle(showValues=False)
        splitter.addWidget(self.field_plot)
        splitter.addWidget(self.temp_plot)
        splitter.setSizes([420, 190])

        _legend(field)
        _legend(temp)

        self.field_item, self.temp_item = field, temp
        self.zero = pg.InfiniteLine(angle=0, movable=False,
                                    pen=pg.mkPen(theme.ZERO, width=1))
        field.addItem(self.zero, ignoreBounds=True)

        self.curves = {}
        for key, label, _unit, which in theme.CHANNELS:
            target = field if which == "field" else temp
            curve = target.plot([], [], pen=_pen(theme.color(key)), name=label)
            # Peak-preserving decimation: at 300 Hz over a 60 s window there
            # are more samples than pixels, and drawing them all costs frame
            # time while hiding the spikes that matter.
            curve.setDownsampling(auto=True, method="peak")
            curve.setClipToView(True)
            self.curves[key] = curve

        self.vline = pg.InfiniteLine(angle=90, movable=False,
                                     pen=pg.mkPen(theme.TEXT_MUTED, width=1,
                                                  style=Qt.DashLine))
        self.hline = pg.InfiniteLine(angle=0, movable=False,
                                     pen=pg.mkPen(theme.TEXT_MUTED, width=1,
                                                  style=Qt.DashLine))
        field.addItem(self.vline, ignoreBounds=True)
        field.addItem(self.hline, ignoreBounds=True)
        self._proxy = pg.SignalProxy(self.field_plot.scene().sigMouseMoved,
                                     rateLimit=30, slot=self._on_move)
        self._last = (np.empty(0), {})

    def restyle(self):
        for key, curve in self.curves.items():
            curve.setPen(_pen(theme.color(key)))
        cursor = pg.mkPen(theme.TEXT_MUTED, width=1, style=Qt.DashLine)
        self.vline.setPen(cursor)
        self.hline.setPen(cursor)
        self.zero.setPen(pg.mkPen(theme.ZERO, width=1))

    def _on_move(self, event):
        pos = event[0]
        item = self.field_plot.getPlotItem()
        if not item.sceneBoundingRect().contains(pos):
            return
        point = item.getViewBox().mapSceneToView(pos)
        self.vline.setPos(point.x())
        self.hline.setPos(point.y())

        t, cols = self._last
        if not len(t):
            return
        i = int(np.searchsorted(t, point.x()))
        i = max(0, min(i, len(t) - 1))
        parts = [f"t {t[i]:8.3f} s"]
        for key in ALL_KEYS:
            if self.curves[key].isVisible() and len(cols.get(key, ())):
                parts.append(f"{theme.LABEL[key]} {cols[key][i]:+8.2f} "
                             f"{theme.UNIT[key]}")
        self.readout.setText("   ".join(parts))

    def set_visible_channels(self, visible):
        for key, curve in self.curves.items():
            curve.setVisible(key in visible)

    def refresh(self, t, cols):
        self._last = (t, cols)
        for key, curve in self.curves.items():
            if curve.isVisible():
                curve.setData(t, cols[key])


class VectorView(QWidget):
    """Bx against By -- the plot that makes an axis problem obvious.

    A magnet turning in the XY plane traces a circle. A gain error on one
    axis makes it an ellipse; a stuck axis makes it a line; an offset moves
    it off centre. None of those are visible in a strip chart at a glance.
    """

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.readout = QLabel("|B| --   angle --")
        self.readout.setProperty("role", "readout")
        self.readout.setContentsMargins(10, 6, 10, 6)
        layout.addWidget(self.readout)

        self.plot = pg.PlotWidget()
        item = _style(self.plot, "Bx [mT]", "By [mT]")
        item.setAspectLocked(True)      # a circle must look like a circle
        layout.addWidget(self.plot, 1)

        # Reference geometry must not drive autorange: a +/-100 mT ring drawn
        # around a 20 mT circle would waste nine tenths of the plot.
        self.scale_ring = pg.PlotDataItem(
            [], [], pen=pg.mkPen(theme.GRID, width=1, style=Qt.DashLine))
        item.addItem(self.scale_ring, ignoreBounds=True)
        self.fit_ring = pg.PlotDataItem(
            [], [], pen=pg.mkPen(theme.TEXT_MUTED, width=1, style=Qt.DashLine))
        item.addItem(self.fit_ring, ignoreBounds=True)
        self.trail = item.plot([], [], pen=_pen(theme.color("mag"), 1.5))
        self.head = pg.ScatterPlotItem(
            size=11, brush=pg.mkBrush(theme.color("bx")),
            pen=pg.mkPen(theme.SURFACE, width=2))
        item.addItem(self.head)
        self.zero_lines = [pg.InfiniteLine(angle=90, pen=pg.mkPen(theme.ZERO)),
                           pg.InfiniteLine(angle=0, pen=pg.mkPen(theme.ZERO))]
        for line in self.zero_lines:
            item.addItem(line, ignoreBounds=True)

        self.trail_points = 600
        self._range_mt = None
        self._angles = np.linspace(0, 2 * np.pi, 241)

    def restyle(self):
        self.scale_ring.setPen(pg.mkPen(theme.GRID, width=1, style=Qt.DashLine))
        self.fit_ring.setPen(pg.mkPen(theme.TEXT_MUTED, width=1,
                                      style=Qt.DashLine))
        self.trail.setPen(_pen(theme.color("mag"), 1.5))
        self.head.setBrush(pg.mkBrush(theme.color("bx")))
        self.head.setPen(pg.mkPen(theme.SURFACE, width=2))
        for line in self.zero_lines:
            line.setPen(pg.mkPen(theme.ZERO))

    def set_range(self, range_mt):
        if range_mt == self._range_mt:
            return
        self._range_mt = range_mt
        if not range_mt:
            self.scale_ring.setData([], [])
            return
        a = np.linspace(0, 2 * np.pi, 361)
        self.scale_ring.setData(range_mt * np.cos(a), range_mt * np.sin(a))

    def refresh(self, t, cols):
        bx, by = cols["bx"], cols["by"]
        if not len(bx):
            return
        n = min(self.trail_points, len(bx))
        self.trail.setData(bx[-n:], by[-n:])
        self.head.setData([bx[-1]], [by[-1]])
        angle = np.degrees(np.arctan2(by[-1], bx[-1]))
        planar = float(np.hypot(bx[-1], by[-1]))
        text = (f"Bxy {planar:7.2f} mT   angle {angle:+7.1f}°   "
                f"|B| {cols['mag'][-1]:7.2f} mT   trail {n} pts")

        fit = fit_circle(bx[-n:], by[-n:])
        if fit is None:
            self.fit_ring.setData([], [])
        else:
            cx, cy, r, residual = fit
            self.fit_ring.setData(cx + r * np.cos(self._angles),
                                  cy + r * np.sin(self._angles))
            text += (f"   ·   fit r {r:6.2f} mT   centre "
                     f"({cx:+.2f}, {cy:+.2f})   residual {residual:.3f} mT")
        self.readout.setText(text)


class SpectrumView(QWidget):
    """Where the energy sits.

    A 7 Hz vibration riding on the field is a barely-visible thickening of
    the strip chart trace and an unmistakable peak here. Uses the measured
    mean sample rate for the frequency axis -- host timestamps carry jitter,
    so treat the axis as approximate and the peak positions as indicative
    rather than calibrated.
    """

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QWidget()
        head_layout = QHBoxLayout(header)
        head_layout.setContentsMargins(10, 6, 10, 6)
        self.readout = QLabel("waiting for enough samples")
        self.readout.setProperty("role", "readout")
        head_layout.addWidget(self.readout, 1)

        head_layout.addWidget(_hint("segment averaging"))
        self.segments_box = QComboBox()
        for k in (1, 2, 4, 8, 16):
            self.segments_box.addItem(f"{k}x", k)
        self.segments_box.setCurrentIndex(2)
        self.segments_box.setToolTip(
            "Welch averaging: split the window into overlapping segments and "
            "average their spectra. Trades frequency resolution for a much "
            "quieter noise floor, which is what you want when looking for a "
            "small peak rather than measuring one precisely.")
        head_layout.addWidget(self.segments_box)
        layout.addWidget(header)

        self.plot = pg.PlotWidget()
        item = _style(self.plot, "frequency [Hz]", "amplitude [mT]")
        item.setLogMode(x=False, y=True)
        _legend(item)
        layout.addWidget(self.plot, 1)

        self.curves = {}
        for key, label, _unit, which in theme.CHANNELS:
            if which != "field":
                continue
            self.curves[key] = item.plot(
                [], [], pen=_pen(theme.color(key), 1.6), name=label)

    def set_visible_channels(self, visible):
        for key, curve in self.curves.items():
            curve.setVisible(key in visible)

    def restyle(self):
        for key, curve in self.curves.items():
            curve.setPen(_pen(theme.color(key), 1.6))

    @staticmethod
    def _welch(values, dt, segments):
        """Amplitude spectrum, averaged over overlapping Hann segments.

        One transform of the whole window has the finest resolution and a
        noise floor that jumps around by several dB between frames, which
        makes a small peak hard to see. Averaging K half-overlapping
        segments divides that variance by roughly K at the cost of K times
        coarser bins -- the right trade when hunting for a peak rather than
        measuring one. K = 1 is the plain periodogram.
        """
        n = len(values)
        seg_len = n if segments <= 1 else max(int(2 * n / (segments + 1)), 32)
        seg_len = min(seg_len, n)
        step = max(seg_len // 2, 1)

        window = np.hanning(seg_len)
        norm = 2.0 / np.sum(window)
        power = None
        count = 0
        for start in range(0, n - seg_len + 1, step):
            chunk = values[start:start + seg_len]
            spectrum = np.abs(np.fft.rfft((chunk - chunk.mean()) * window)) * norm
            power = spectrum ** 2 if power is None else power + spectrum ** 2
            count += 1
        if count == 0:
            return None, None
        return np.fft.rfftfreq(seg_len, dt), np.sqrt(power / count)

    def refresh(self, t, cols):
        n = len(t)
        if n < 64:
            self.readout.setText(f"waiting for enough samples ({n}/64)")
            return

        dt = float(np.mean(np.diff(t)))
        if dt <= 0:
            return
        fs = 1.0 / dt
        segments = self.segments_box.currentData()

        peak_txt = ""
        resolution = None
        for key, curve in self.curves.items():
            if not curve.isVisible():
                continue
            freq, spectrum = self._welch(cols[key], dt, segments)
            if freq is None:
                continue
            resolution = freq[1] if len(freq) > 1 else None
            curve.setData(freq[1:], np.maximum(spectrum[1:], 1e-6))
            if key == "bx" and len(spectrum) > 2:
                j = int(np.argmax(spectrum[1:])) + 1
                peak_txt = f"   Bx peak {freq[j]:.2f} Hz @ {spectrum[j]:.3f} mT"

        res_txt = f"{resolution:.3f} Hz" if resolution else "--"
        self.readout.setText(
            f"n {n}   fs ≈ {fs:.1f} Hz   Nyquist {fs / 2:.1f} Hz   "
            f"bin {res_txt}{peak_txt}")


class DistributionView(QWidget):
    """Per-channel histogram over the visible window.

    This is the noise-floor view: point the sensor at nothing in particular,
    let it sit, and sigma is the number that characterises the channel at
    the current averaging setting. Doubling CONV_AVG should visibly narrow
    these, and if it does not, the noise is not coming from the ADC.
    """

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.readout = QLabel("")
        self.readout.setProperty("role", "readout")
        self.readout.setContentsMargins(10, 6, 10, 6)
        layout.addWidget(self.readout)

        self.plot = pg.PlotWidget()
        item = _style(self.plot, "value [mT]", "count")
        _legend(item)
        layout.addWidget(self.plot, 1)

        self.curves = {}
        for key, label, _unit, which in theme.CHANNELS:
            if which != "field":
                continue
            self.curves[key] = item.plot(
                [], [], stepMode="center", fillLevel=0,
                brush=pg.mkBrush(_fill(theme.color(key))),
                pen=_pen(theme.color(key), 1.5), name=label)

        self.bins = 60

    def set_visible_channels(self, visible):
        for key, curve in self.curves.items():
            curve.setVisible(key in visible)

    def restyle(self):
        for key, curve in self.curves.items():
            curve.setPen(_pen(theme.color(key), 1.5))
            curve.setBrush(pg.mkBrush(_fill(theme.color(key))))

    def refresh(self, t, cols):
        if len(t) < 16:
            self.readout.setText(f"waiting for enough samples ({len(t)}/16)")
            return
        parts = []
        for key, curve in self.curves.items():
            if not curve.isVisible():
                continue
            values = cols[key]
            counts, edges = np.histogram(values, bins=self.bins)
            curve.setData(edges, counts)
            parts.append(f"{theme.LABEL[key]} µ {values.mean():+7.2f} "
                         f"σ {values.std():.3f}")
        self.readout.setText(f"n {len(t)}   " + "   ".join(parts))


# ================================================================== controls

def _hint(text):
    label = QLabel(text)
    label.setProperty("role", "hint")
    label.setWordWrap(True)
    # Without this a wrapped label reports its single-line height to the
    # layout and the last line gets clipped off mid-sentence.
    label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.MinimumExpanding)
    label.setMinimumHeight(label.fontMetrics().height() * 2)
    return label


class ConnectionPanel(QGroupBox):
    connect_requested = Signal(object, int, bool)   # port, baud, fake
    disconnect_requested = Signal()

    def __init__(self):
        super().__init__("Connection")
        form = QFormLayout(self)
        form.setLabelAlignment(Qt.AlignLeft)

        self.port_box = QComboBox()
        self.baud_box = QComboBox()
        self.baud_box.addItems(["9600", "19200", "38400", "57600", "115200",
                                "230400", "460800", "921600"])
        self.baud_box.setCurrentText(str(sensor.BAUD))

        refresh = QPushButton("Refresh ports")
        refresh.clicked.connect(self.refresh_ports)

        self.fake_box = QCheckBox("Simulate (no board)")

        self.connect_button = QPushButton("Connect")
        self.connect_button.setProperty("role", "primary")
        self.connect_button.clicked.connect(self._toggle)

        form.addRow("Port", self.port_box)
        form.addRow("", refresh)
        form.addRow("Baud", self.baud_box)
        form.addRow("", self.fake_box)
        form.addRow("", self.connect_button)
        form.addRow(_hint("Uartlite baud is fixed at synthesis — changing it "
                          "here without rebuilding gives line noise."))

        self.connected = False
        self.refresh_ports()

    def refresh_ports(self):
        """A port-enumeration failure must never take the window down --
        pyserial may be missing, or the OS may deny the device list."""
        self.port_box.clear()
        try:
            ports = [p.device for p in sensor.list_ports()]
        except ImportError:
            self.fake_box.setChecked(True)
            return
        except Exception:                            # noqa: BLE001
            return
        self.port_box.addItems(ports)
        if not ports:
            self.fake_box.setChecked(True)

    def _toggle(self):
        if self.connected:
            self.disconnect_requested.emit()
            return
        fake = self.fake_box.isChecked()
        port = self.port_box.currentText() or None
        if not fake and port is None:
            QMessageBox.warning(
                self, "No port selected",
                "Pick a port from the list, or tick Simulate to run without "
                "hardware.\n\nIf the list is empty, check the USB cable and "
                "press Refresh ports.")
            return
        self.connect_requested.emit(port, int(self.baud_box.currentText()), fake)

    def set_connected(self, on):
        self.connected = on
        self.connect_button.setText("Disconnect" if on else "Connect")
        self.connect_button.setProperty("role", "danger" if on else "primary")
        self.connect_button.style().unpolish(self.connect_button)
        self.connect_button.style().polish(self.connect_button)
        for widget in (self.port_box, self.baud_box, self.fake_box):
            widget.setEnabled(not on)


class AcquisitionPanel(QGroupBox):
    """Controls that change the board, not the picture.

    Each one sends a line to the firmware and waits for the CONFIG echo, so
    what is displayed is what the board reports rather than what was asked
    for. If a control does nothing, the firmware did not accept it -- the
    log pane will say why.
    """
    command = Signal(str)

    def __init__(self):
        super().__init__("Acquisition (firmware)")
        form = QFormLayout(self)

        self.rate_box = QSpinBox()
        self.rate_box.setRange(1, 5000)
        self.rate_box.setValue(5)
        self.rate_box.setSuffix(" Hz")

        apply_rate = QPushButton("Apply")
        apply_rate.clicked.connect(
            lambda: self.command.emit(f"R {self.rate_box.value()}"))

        rate_row = QWidget()
        rate_layout = QHBoxLayout(rate_row)
        rate_layout.setContentsMargins(0, 0, 0, 0)
        rate_layout.addWidget(self.rate_box, 1)
        rate_layout.addWidget(apply_rate)

        self.avg_box = QComboBox()
        for mult in (1, 2, 4, 8, 16, 32):
            self.avg_box.addItem(f"{mult}x", mult)
        self.avg_box.setCurrentIndex(5)
        self.avg_box.activated.connect(
            lambda: self.command.emit(f"A {self.avg_box.currentData()}"))

        self.range_box = QComboBox()
        for mt in (25, 50, 100):
            self.range_box.addItem(f"±{mt} mT", mt)
        self.range_box.setCurrentIndex(2)
        self.range_box.activated.connect(
            lambda: self.command.emit(f"G {self.range_box.currentData()}"))

        self.stream_box = QCheckBox("Streaming")
        self.stream_box.setChecked(True)
        self.stream_box.toggled.connect(
            lambda on: self.command.emit(f"P {1 if on else 0}"))

        form.addRow("Sample rate", rate_row)
        form.addRow("Averaging", self.avg_box)
        form.addRow("Range", self.range_box)
        form.addRow("", self.stream_box)

        self.warning = _hint("")
        self.warning.setVisible(False)
        form.addRow(self.warning)

        self.reported = _hint("board has not reported a config yet")
        form.addRow(self.reported)

    def show_config(self, config, limits):
        if config is None:
            return
        actual = (1_000_000 / config.period_us) if config.period_us else None
        self.reported.setText(
            f"board reports: {config.conv_avg}x averaging, ±{config.range_mt} mT, "
            f"loop {sensor.format_rate(actual)}, rtd {sensor.format_rate(config.rtd_hz)}")

        # Blocking the reader on a port that cannot carry the request is the
        # commonest way to make this board look broken, so say so up front.
        want = self.rate_box.value()
        if limits and limits.uart and want > limits.uart:
            self.warning.setText(
                f"⚠ {want} Hz exceeds what {sensor.format_rate(limits.uart)} of "
                f"UART can carry. Lines will be dropped or truncated. Raise the "
                f"Uartlite baud in Vivado and rebuild, or ask for less.")
            self.warning.setStyleSheet(f"color: {theme.WARNING};")
            self.warning.setVisible(True)
        elif limits and want > limits.rate:
            self.warning.setText(
                f"⚠ {want} Hz is above the {limits.bottleneck} ceiling "
                f"({sensor.format_rate(limits.rate)}); the extra is wasted.")
            self.warning.setStyleSheet(f"color: {theme.SERIOUS};")
            self.warning.setVisible(True)
        else:
            self.warning.setVisible(False)

    def sync_from_config(self, config):
        """Reflect what the board actually reports, without re-sending."""
        if config is None:
            return
        if config.period_us:
            self.rate_box.blockSignals(True)
            self.rate_box.setValue(int(round(1_000_000 / config.period_us)))
            self.rate_box.blockSignals(False)
        if config.conv_avg:
            i = self.avg_box.findData(config.conv_avg)
            if i >= 0:
                self.avg_box.setCurrentIndex(i)
        if config.range_mt:
            i = self.range_box.findData(config.range_mt)
            if i >= 0:
                self.range_box.setCurrentIndex(i)


class DisplayPanel(QGroupBox):
    """Everything here changes the picture only. No command reaches the
    board from this panel, and nothing here can lose a sample."""
    changed = Signal()

    def __init__(self):
        super().__init__("Display")
        form = QFormLayout(self)

        self.window_box = QDoubleSpinBox()
        self.window_box.setRange(0.5, 600.0)
        self.window_box.setValue(20.0)
        self.window_box.setSuffix(" s")
        self.window_box.setDecimals(1)

        self.fps_box = QSpinBox()
        self.fps_box.setRange(1, 60)
        self.fps_box.setValue(30)
        self.fps_box.setSuffix(" fps")

        self.smooth_box = QSpinBox()
        self.smooth_box.setRange(1, 200)
        self.smooth_box.setValue(1)
        self.smooth_box.setPrefix("N = ")

        self.autoscale_box = QCheckBox("Autoscale Y")
        self.autoscale_box.setChecked(True)

        for widget in (self.window_box, self.fps_box, self.smooth_box):
            widget.valueChanged.connect(lambda *_: self.changed.emit())
        self.autoscale_box.toggled.connect(lambda *_: self.changed.emit())

        form.addRow("Window", self.window_box)
        form.addRow("Refresh", self.fps_box)
        form.addRow("Moving avg", self.smooth_box)
        form.addRow("", self.autoscale_box)
        form.addRow(_hint("Smoothing is display-only. Recording and export "
                          "always write raw samples."))


class ChannelPanel(QGroupBox):
    changed = Signal()

    def __init__(self):
        super().__init__("Channels")
        layout = QVBoxLayout(self)
        self.boxes = {}
        self.swatches = {}
        for key, label, unit, _which in theme.CHANNELS:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(8)

            swatch = QFrame()
            swatch.setFixedSize(12, 12)
            self.swatches[key] = swatch

            box = QCheckBox(f"{label}  [{unit}]")
            box.setChecked(True)
            box.toggled.connect(lambda *_: self.changed.emit())
            self.boxes[key] = box

            row_layout.addWidget(swatch)
            row_layout.addWidget(box, 1)
            layout.addWidget(row)
        self.restyle()

    def restyle(self):
        for key, swatch in self.swatches.items():
            swatch.setStyleSheet(f"background: {theme.color(key)}; "
                                 f"border-radius: 3px; border: none;")

    def visible(self):
        return {key for key, box in self.boxes.items() if box.isChecked()}


class HealthPanel(QGroupBox):
    """Every ceiling between the sensor die and this window.

    The point is not the individual numbers, it is which one is smallest.
    Asking for a faster rate helps only when the binding ceiling is the
    firmware loop; the other three need a different change entirely.
    """

    ROWS = [
        ("sensor", "conv", "averaging setting — change CONV_AVG"),
        ("spi", "spi", "SPI clock — raise SCK in Vivado"),
        ("uart", "uart", "serial link — raise Uartlite baud and rebuild"),
        ("loop", "loop", "firmware main loop — the Sample rate control"),
    ]

    def __init__(self):
        super().__init__("Delivery ceilings")
        grid = QGridLayout(self)
        grid.setVerticalSpacing(4)
        self.values = {}
        self.names = {}

        for row, (name, attr, why) in enumerate(self.ROWS):
            label = QLabel(name)
            label.setProperty("role", "readout")
            value = QLabel("--")
            value.setProperty("role", "readout")
            value.setAlignment(Qt.AlignRight)
            note = _hint(why)
            grid.addWidget(label, row, 0)
            grid.addWidget(value, row, 1)
            grid.addWidget(note, row, 2)
            grid.setColumnStretch(2, 1)
            self.values[attr] = value
            self.names[attr] = label

        self.summary = QLabel("waiting for a config line")
        self.summary.setProperty("role", "readout")
        self.summary.setWordWrap(True)
        grid.addWidget(self.summary, len(self.ROWS), 0, 1, 3)

        self.measured = QLabel("measured   --")
        self.measured.setProperty("role", "readout")
        grid.addWidget(self.measured, len(self.ROWS) + 1, 0, 1, 3)

    def update_limits(self, limits, measured):
        self.measured.setText(
            f"measured at the host   {sensor.format_rate(measured)}")
        if limits is None:
            self.summary.setText("waiting for a config line")
            for value in self.values.values():
                value.setText("--")
            return

        for name, attr, _ in self.ROWS:
            hz = getattr(limits, attr)
            binding = limits.bottleneck == name
            self.values[attr].setText(sensor.format_rate(hz))
            colour = theme.WARNING if binding else theme.TEXT_2
            weight = "600" if binding else "400"
            self.values[attr].setStyleSheet(
                f"color: {colour}; font-weight: {weight};")
            self.names[attr].setStyleSheet(
                f"color: {colour}; font-weight: {weight};")

        self.summary.setText(
            f"binding ceiling: {limits.bottleneck} at "
            f"{sensor.format_rate(limits.rate)}")
        self.summary.setStyleSheet(f"color: {theme.WARNING};")

        if measured and limits.conv:
            used = 100.0 * measured / limits.conv
            self.measured.setText(
                f"measured at the host   {sensor.format_rate(measured)}   "
                f"({used:.1f}% of finished conversions reach the plot; "
                f"1 in {limits.conv / max(measured, 1e-9):.0f})")


class StatsTable(QTableWidget):
    """Last, min, max, mean, sigma and peak-to-peak over the visible window.

    Numbers a trace cannot give you: whether a channel is drifting, how much
    it moves, and what its noise looks like at the current averaging.
    """

    COLUMNS = ["last", "min", "max", "mean", "σ", "p-p"]

    def __init__(self):
        super().__init__(len(theme.CHANNELS), len(self.COLUMNS) + 1)
        self.setHorizontalHeaderLabels(["channel"] + self.COLUMNS)
        self.verticalHeader().setVisible(False)
        self.setAlternatingRowColors(True)
        self.setEditTriggers(QTableWidget.NoEditTriggers)
        self.setSelectionMode(QTableWidget.NoSelection)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)

        for row, (key, label, unit, _which) in enumerate(theme.CHANNELS):
            # A coloured swatch in the name cell, never coloured numerals:
            # identity belongs to the mark, values stay in plain ink.
            item = QTableWidgetItem(f"■ {label}  {unit}")
            self.setItem(row, 0, item)
            for col in range(1, len(self.COLUMNS) + 1):
                cell = QTableWidgetItem("--")
                cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.setItem(row, col, cell)

        self.restyle()
        self.setMinimumHeight(210)

    def restyle(self):
        for row, (key, *_rest) in enumerate(theme.CHANNELS):
            self.item(row, 0).setForeground(QColor(theme.color(key)))
            for col in range(1, len(self.COLUMNS) + 1):
                self.item(row, col).setForeground(QColor(theme.TEXT_2))

    def refresh(self, cols):
        for row, (key, *_rest) in enumerate(theme.CHANNELS):
            values = cols.get(key)
            if values is None or not len(values):
                continue
            stats = [values[-1], values.min(), values.max(), values.mean(),
                     values.std(), values.max() - values.min()]
            for col, value in enumerate(stats, start=1):
                self.item(row, col).setText(f"{value:+.3f}")


# =================================================================== window

class MainWindow(QMainWindow):

    def __init__(self, autostart_fake=False):
        super().__init__()
        self.setWindowTitle("TMAG5170 · magnetic field and temperature")
        self.resize(1500, 900)

        self.acq = Acquisition()
        self.paused = False
        self.no_data_warned = False
        self.started_at = None
        self._notes_seen = 0
        self._state_text, self._state_key = "Idle", "muted"

        self._build_views()
        self._build_docks()
        self._build_toolbar()
        self._build_menu()
        self._build_statusbar()
        # Captured before anything is dragged or closed, so "Reset layout"
        # has something to go back to.
        self._default_state = self.saveState()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self._apply_display()

        if autostart_fake:
            self.connection.fake_box.setChecked(True)
            self._connect(None, sensor.BAUD, True)

    # -- construction ----------------------------------------------------

    def _build_views(self):
        self.tabs = QTabWidget()
        self.strip = StripChartView()
        self.vector = VectorView()
        self.spectrum = SpectrumView()
        self.distribution = DistributionView()
        self.tabs.addTab(self.strip, "Strip chart")
        self.tabs.addTab(self.vector, "XY vector")
        self.tabs.addTab(self.spectrum, "Spectrum")
        self.tabs.addTab(self.distribution, "Distribution")
        self.setCentralWidget(self.tabs)

    def _build_docks(self):
        # -- right: controls ---------------------------------------------
        side = QWidget()
        layout = QVBoxLayout(side)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        self.connection = ConnectionPanel()
        self.acquisition = AcquisitionPanel()
        self.display = DisplayPanel()
        self.channels = ChannelPanel()

        self.connection.connect_requested.connect(self._connect)
        self.connection.disconnect_requested.connect(self._disconnect)
        self.acquisition.command.connect(self._send_command)
        self.display.changed.connect(self._apply_display)
        self.channels.changed.connect(self._apply_channels)

        for panel in (self.connection, self.acquisition, self.display,
                      self.channels):
            layout.addWidget(panel)
        layout.addStretch(1)

        # The panels are taller than most windows. Without a scroll area the
        # bottom ones simply cannot be reached -- they are not clipped in a
        # way the user can do anything about.
        scroll = QScrollArea()
        scroll.setWidget(side)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.NoFrame)

        self.controls_dock = QDockWidget("Controls", self)
        self.controls_dock.setObjectName("dock_controls")
        self.controls_dock.setWidget(scroll)
        self.controls_dock.setFeatures(QDockWidget.DockWidgetMovable
                                       | QDockWidget.DockWidgetClosable)
        self.controls_dock.setMinimumWidth(370)
        self.addDockWidget(Qt.RightDockWidgetArea, self.controls_dock)

        # -- bottom: stats, health, log ----------------------------------
        self.stats = StatsTable()
        self.health = HealthPanel()
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.log.setPlaceholderText(
            "Firmware diagnostics ('#' lines) appear here.")

        self.stats_dock = QDockWidget("Statistics", self)
        self.stats_dock.setObjectName("dock_stats")
        self.stats_dock.setWidget(self.stats)
        self.health_dock = QDockWidget("Throughput", self)
        self.health_dock.setObjectName("dock_health")
        self.health_dock.setWidget(self.health)
        self.log_dock = QDockWidget("Board log", self)
        self.log_dock.setObjectName("dock_log")
        self.log_dock.setWidget(self.log)

        for dock in (self.stats_dock, self.health_dock, self.log_dock):
            dock.setFeatures(QDockWidget.DockWidgetMovable
                             | QDockWidget.DockWidgetClosable
                             | QDockWidget.DockWidgetFloatable)
            self.addDockWidget(Qt.BottomDockWidgetArea, dock)
        self.tabifyDockWidget(self.stats_dock, self.health_dock)
        self.tabifyDockWidget(self.health_dock, self.log_dock)
        self.stats_dock.raise_()
        # Tall enough for all six channel rows plus the header, and wide
        # enough that the controls column is not squeezed by it.
        self.resizeDocks([self.stats_dock], [270], Qt.Vertical)
        self.resizeDocks([self.controls_dock], [380], Qt.Horizontal)

    def _build_toolbar(self):
        bar = QToolBar()
        # saveState() needs a name for every toolbar and dock, or "Reset
        # layout" silently drops them.
        bar.setObjectName("main_toolbar")
        bar.setMovable(False)
        self.addToolBar(bar)

        def action(text, slot, shortcut=None, checkable=False, tip=""):
            act = QAction(text, self)
            act.setCheckable(checkable)
            act.triggered.connect(slot)
            if shortcut:
                act.setShortcut(QKeySequence(shortcut))
                tip = f"{tip}  ({shortcut})" if tip else shortcut
            act.setToolTip(tip)
            bar.addAction(act)
            return act

        self.pause_action = action(
            "Pause", self._toggle_pause, "Space", checkable=True,
            tip="Freeze the display. Acquisition and recording continue.")
        bar.addSeparator()
        action("Tare", self._tare, "T",
               tip="Zero the field against the last second of samples.")
        action("Clear tare", self._clear_tare)
        bar.addSeparator()
        action("Clear buffer", self._clear_buffer)
        self.record_action = action(
            "Record…", self._toggle_record, "Ctrl+R", checkable=True,
            tip="Stream raw samples to CSV as they arrive.")
        action("Export…", self._export, "Ctrl+E",
               tip="Write the whole buffer to an Excel workbook.")

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        bar.addWidget(spacer)

        self.tare_badge = QLabel("")
        self.tare_badge.setProperty("role", "readout")
        self.tare_badge.setContentsMargins(0, 0, 12, 0)
        bar.addWidget(self.tare_badge)

    def _build_menu(self):
        """Closing a panel has to be reversible. Qt gives every dock a
        toggleViewAction that checks and unchecks itself as the dock opens
        and closes, so this menu can never drift out of step with reality."""
        bar = self.menuBar()

        view_menu = bar.addMenu("&View")

        self.theme_action = QAction("&Light theme", self)
        self.theme_action.setCheckable(True)
        self.theme_action.setChecked(theme.mode() == "light")
        self.theme_action.setShortcut(QKeySequence("Ctrl+D"))
        self.theme_action.toggled.connect(
            lambda on: self._apply_theme("light" if on else "dark"))
        view_menu.addAction(self.theme_action)
        view_menu.addSeparator()

        charts = view_menu.addMenu("&Chart")
        for index in range(self.tabs.count()):
            action = QAction(self.tabs.tabText(index), self)
            action.setShortcut(QKeySequence(f"Ctrl+{index + 1}"))
            action.triggered.connect(
                lambda _checked=False, i=index: self.tabs.setCurrentIndex(i))
            charts.addAction(action)

        panels = view_menu.addMenu("&Panels")
        for dock in (self.controls_dock, self.stats_dock, self.health_dock,
                     self.log_dock):
            panels.addAction(dock.toggleViewAction())

        view_menu.addSeparator()
        reset = QAction("&Reset layout", self)
        reset.triggered.connect(self._reset_layout)
        view_menu.addAction(reset)

    def _reset_layout(self):
        for dock in (self.controls_dock, self.stats_dock, self.health_dock,
                     self.log_dock):
            dock.show()
        self.restoreState(self._default_state)
        self.stats_dock.raise_()

    def _apply_theme(self, mode):
        """One switch for the whole application: the stylesheet, every plot
        frame and legend, the traces, the swatches and the table ink."""
        theme.set_mode(mode)
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(theme.stylesheet())
        restyle_plots()
        for view in (self.strip, self.vector, self.spectrum,
                     self.distribution):
            view.restyle()
        self.channels.restyle()
        self.stats.restyle()
        self._set_state(self._state_text, self._state_key)
        QSettings("TMAG5170 Scope", "tmag_scope").setValue("theme", mode)

    def _build_statusbar(self):
        self.state_label = QLabel("Idle")
        self.rate_label = QLabel("")
        self.buffer_label = QLabel("")
        self.record_label = QLabel("")
        for widget in (self.state_label, self.rate_label, self.buffer_label,
                       self.record_label):
            self.statusBar().addWidget(widget)
            widget.setContentsMargins(0, 0, 18, 0)

    # -- connection ------------------------------------------------------

    def _connect(self, port, baud, fake):
        try:
            reader = sensor.SensorReader(port=port, baud=baud, fake=fake).start()
        except Exception as e:                       # noqa: BLE001
            QMessageBox.critical(self, "Could not start", str(e))
            return

        self.acq.attach(reader)
        self.started_at = time.perf_counter()
        self.no_data_warned = False
        self._notes_seen = 0
        self.log.clear()
        self.connection.set_connected(True)
        self._set_state("Simulating" if fake else f"Reading {port}", "good")
        self.timer.start()

    def _disconnect(self):
        self.timer.stop()
        self.acq.detach()
        self.connection.set_connected(False)
        self._set_state("Stopped", "muted")
        self.record_action.setChecked(False)
        self.record_label.setText("")

    def _send_command(self, text):
        if self.acq.reader is None:
            self._append_log(f"# not connected -- '{text}' not sent")
            return
        if self.acq.reader.send(text):
            self._append_log(f"> {text}")
        else:
            self._append_log(f"# failed to send '{text}'")

    # -- toolbar ---------------------------------------------------------

    def _toggle_pause(self, on):
        self.paused = on
        self.pause_action.setText("Resume" if on else "Pause")
        if self.acq.reader is not None:
            self._set_state("Paused (still acquiring)" if on else "Reading",
                            "warning" if on else "good")

    def _tare(self):
        if not self.acq.set_tare(1.0):
            QMessageBox.information(self, "Nothing to tare",
                                    "No samples collected yet.")
            return
        self._update_tare_badge()

    def _clear_tare(self):
        self.acq.clear_tare()
        self._update_tare_badge()

    def _update_tare_badge(self):
        if not self.acq.tared:
            self.tare_badge.setText("")
            return
        offsets = "  ".join(f"{theme.LABEL[k]} {self.acq.tare[k]:+.2f}"
                            for k in FIELD_KEYS if k != "mag")
        self.tare_badge.setText(f"TARE  {offsets} mT")
        self.tare_badge.setStyleSheet(f"color: {theme.WARNING};")

    def _clear_buffer(self):
        self.acq.clear()
        self.stats.refresh({})

    def _toggle_record(self, on):
        reader = self.acq.reader
        if reader is None:
            self.record_action.setChecked(False)
            QMessageBox.information(self, "Not connected",
                                    "Connect to a source before recording.")
            return
        if not on:
            reader.stop_recording()
            self.record_label.setText("")
            self._append_log("# recording stopped")
            return

        default = datetime.now().strftime("tmag_%Y%m%d_%H%M%S.csv")
        path, _ = QFileDialog.getSaveFileName(self, "Record to CSV", default,
                                              "CSV files (*.csv)")
        if not path:
            self.record_action.setChecked(False)
            return
        reader.start_recording(path)
        self.record_label.setText("● REC")
        self.record_label.setStyleSheet(f"color: {theme.CRITICAL};")
        self._append_log(f"# recording to {path}")

    def _export(self):
        """Always writes every column, raw -- neither the tare offset nor
        the display smoothing touches what lands in the file."""
        n = len(self.acq.ring)
        if n == 0:
            QMessageBox.information(self, "Nothing to export",
                                    "No samples collected yet.")
            return

        default = datetime.now().strftime("tmag_%Y%m%d_%H%M%S.xlsx")
        path, _ = QFileDialog.getSaveFileName(self, "Export to Excel", default,
                                              "Excel files (*.xlsx)")
        if not path:
            return

        import pandas as pd
        rows = self.acq.ring.tail(n)
        frame = pd.DataFrame({
            "Time [s]": rows[:, 0],
            "Bx [mT]": rows[:, 1],
            "By [mT]": rows[:, 2],
            "Bz [mT]": rows[:, 3],
            "|B| [mT]": rows[:, 4],
            "T die [degC]": rows[:, 5],
            "T rtd [degC]": rows[:, 6],
        })
        config = self.acq.reader.config if self.acq.reader else None
        meta = pd.DataFrame({
            "key": ["exported", "samples", "averaging", "range_mt",
                    "period_us", "rtd_hz", "tare_bx", "tare_by", "tare_bz"],
            "value": [datetime.now().isoformat(timespec="seconds"), n,
                      getattr(config, "conv_avg", None),
                      getattr(config, "range_mt", None),
                      getattr(config, "period_us", None),
                      getattr(config, "rtd_hz", None),
                      self.acq.tare["bx"], self.acq.tare["by"],
                      self.acq.tare["bz"]],
        })
        try:
            with pd.ExcelWriter(path) as writer:
                frame.to_excel(writer, sheet_name="samples", index=False)
                meta.to_excel(writer, sheet_name="run", index=False)
        except Exception as e:                       # noqa: BLE001
            QMessageBox.critical(self, "Export failed", str(e))
            return
        QMessageBox.information(self, "Data saved",
                                f"{n} samples written to:\n{path}")

    # -- display ---------------------------------------------------------

    def _apply_display(self):
        self.timer.setInterval(int(1000 / self.display.fps_box.value()))
        for view in (self.strip.field_plot, self.strip.temp_plot,
                     self.spectrum.plot, self.distribution.plot):
            view.enableAutoRange("y", self.display.autoscale_box.isChecked())

    def _apply_channels(self):
        visible = self.channels.visible()
        self.strip.set_visible_channels(visible)
        self.spectrum.set_visible_channels(visible)
        self.distribution.set_visible_channels(visible)

    STATE_COLOURS = {"good": "GOOD", "warning": "WARNING",
                     "serious": "SERIOUS", "critical": "CRITICAL",
                     "muted": "TEXT_MUTED"}

    def _set_state(self, text, key="muted"):
        """Stored by name rather than by hex, so a theme change re-resolves
        it instead of leaving the last mode's colour behind."""
        self._state_text, self._state_key = text, key
        colour = getattr(theme, self.STATE_COLOURS.get(key, "TEXT_MUTED"))
        self.state_label.setText(text)
        self.state_label.setStyleSheet(f"color: {colour};")

    def _append_log(self, text):
        self.log.appendPlainText(text)

    # -- loop ------------------------------------------------------------

    def _tick(self):
        """Drain, redraw, reschedule.

        Wrapped whole: a Qt timer has no supervisor, and an exception here
        would otherwise vanish into stderr while the reader thread kept
        filling the queue. One bad frame should cost one frame, not the
        session.
        """
        try:
            self._tick_once()
        except Exception as e:                       # noqa: BLE001
            self._set_state(f"Display error: {e}", "critical")
            import traceback
            traceback.print_exc()

    def _tick_once(self):
        reader = self.acq.reader
        if reader is None:
            return

        if reader.error:
            message = reader.error
            self._disconnect()
            self._set_state(message.split("\n")[0], "critical")
            QMessageBox.critical(self, "Serial error", message)
            return

        arrived = self.acq.pump()
        self._drain_notes(reader)

        if arrived:
            if self.no_data_warned:
                self.no_data_warned = False
                self._set_state(f"Reading {reader.port or 'simulated'}", "good")
        elif not self.no_data_warned and len(self.acq.ring) == 0:
            # Port opened but nothing usable yet. Status area only -- a modal
            # dialog here would block this very callback.
            if time.perf_counter() - self.started_at > 4.0:
                why = reader.diagnose_no_data()
                if why:
                    self.no_data_warned = True
                    self._set_state(why.split("\n")[0], "serious")
                    self._append_log("# " + why.replace("\n\n", " "))

        config = reader.config
        limits = reader.limits()
        measured = self.acq.measured_rate()
        self.health.update_limits(limits, measured)
        self.acquisition.show_config(config, limits)
        if config is not None:
            self.vector.set_range(config.range_mt)

        self.rate_label.setText(f"host {sensor.format_rate(measured)}")
        self.buffer_label.setText(
            f"buffer {len(self.acq.ring):,}/{self.acq.ring.capacity:,}")

        if self.paused:
            return

        t, cols = self.acq.window(self.display.window_box.value(),
                                  self.display.smooth_box.value())
        if not len(t):
            return

        # Only the visible view is redrawn: three idle plots updating behind
        # a tab is the easiest frame time in the program to give back.
        current = self.tabs.currentWidget()
        current.refresh(t, cols)
        self.stats.refresh(cols)

    def _drain_notes(self, reader):
        while reader.notes:
            try:
                self._append_log(reader.notes.popleft())
            except IndexError:
                return

    # -- teardown --------------------------------------------------------

    def closeEvent(self, event):
        self.timer.stop()
        self.acq.detach()
        super().closeEvent(event)


def main():
    parser = argparse.ArgumentParser(description="TMAG5170 instrument GUI")
    parser.add_argument("--fake", action="store_true",
                        help="start against the synthetic source immediately")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setApplicationName("TMAG5170 Scope")
    # Remembered across runs -- being asked to re-pick the theme every
    # launch is exactly the sort of thing that makes a tool feel unfinished.
    saved = QSettings("TMAG5170 Scope", "tmag_scope").value("theme", "dark")
    theme.set_mode(saved if saved in theme.MODES else "dark")
    app.setStyleSheet(theme.stylesheet())

    window = MainWindow(autostart_fake=args.fake)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()