"""
Controls for host-side processing (temperature compensation, filtering,
outlier rejection) and the noise meter. They only show and collect values;
SettingsController validates and applies them like every other setting.
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QPushButton, QSpinBox, QWidget,
)

from . import processing
from .settings import SCHEMA

FILTER_LABELS = {
    "none": "None",
    "moving_average": "Moving average",
    "median": "Median",
    "ema": "Exponential (EMA)",
    "lowpass": "Low-pass (Butterworth)",
    "notch": "Notch (mains)",
}
OUTLIER_LABELS = {"none": "None", "hampel": "Hampel (local median)",
                  "sigma_clip": "Sigma clip"}
MAGNETS = [("NdFeB (−0.12 %/°C)", -0.12), ("SmCo (−0.03 %/°C)", -0.03),
           ("Ferrite (−0.20 %/°C)", -0.20), ("Custom", None)]


def _hint(text):
    label = QLabel(text)
    label.setProperty("role", "hint")
    label.setWordWrap(True)
    return label


def _dspin(key, decimals=3, suffix=""):
    s = SCHEMA[key]
    box = QDoubleSpinBox()
    box.setDecimals(decimals)
    box.setRange(s["min"], s["max"])
    box.setSuffix(suffix)
    box.setKeyboardTracking(False)     # validate on Enter, not per keystroke
    return box


def _ispin(key, suffix=""):
    s = SCHEMA[key]
    box = QSpinBox()
    box.setRange(int(s["min"]), int(s["max"]))
    box.setSuffix(suffix)
    box.setKeyboardTracking(False)
    return box


def _set_row_visible(form, field, visible):
    label = form.labelForField(field)
    if label is not None:
        label.setVisible(visible)
    field.setVisible(visible)


class ProcessingPanel(QGroupBox):
    """Temperature compensation, outlier rejection and filtering."""

    changed = Signal()
    set_ref_now = Signal()

    def __init__(self):
        super().__init__("Processing (host side)")
        form = QFormLayout(self)
        self.form = form

        # -- temperature compensation -----------------------------------
        self.tc_box = QCheckBox("Temperature compensation")
        self.tc_box.setToolTip(
            "Normalise the field to the reference temperature:\n"
            "B_comp = B / (1 + a·(T − T_ref)).\n"
            "The TMAG5170 is configured with MAG_TEMPCO = 0 %/°C "
            "(no compensation), so nothing does this unless you enable it.")
        form.addRow(self.tc_box)

        self.tc_source = QComboBox()
        self.tc_source.addItem("RTD probe (on the magnet)", "rtd")
        self.tc_source.addItem("TMAG5170 die", "die")
        form.addRow("Temperature", self.tc_source)

        self.magnet = QComboBox()
        for label, value in MAGNETS:
            self.magnet.addItem(label, value)
        self.coeff = _dspin("temp_coeff_pct", 3, " %/°C")
        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.magnet, 1)
        rl.addWidget(self.coeff)
        form.addRow("Magnet", row)

        self.tref = _dspin("temp_ref_C", 2, " °C")
        self.tref_now = QPushButton("Use current")
        self.tref_now.setToolTip("Set T_ref to the temperature measured now.")
        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.tref, 1)
        rl.addWidget(self.tref_now)
        form.addRow("T_ref", row)
        self._tc_rows = [self.tc_source, self.magnet.parentWidget(),
                         self.tref.parentWidget()]

        # -- outliers -----------------------------------------------------
        self.outlier = QComboBox()
        for key in processing.OUTLIERS:
            self.outlier.addItem(OUTLIER_LABELS[key], key)
        form.addRow("Outliers", self.outlier)
        self.out_window = _ispin("outlier_window", " samples")
        form.addRow("  window", self.out_window)
        self.out_k = _dspin("outlier_k", 1, " σ")
        form.addRow("  threshold", self.out_k)

        # -- filter -------------------------------------------------------
        self.filter = QComboBox()
        for key in processing.FILTERS:
            self.filter.addItem(FILTER_LABELS[key], key)
        form.addRow("Filter", self.filter)
        self.f_window = _ispin("filter_window", " samples")
        form.addRow("  window", self.f_window)
        self.f_tau = _dspin("filter_tau_s", 3, " s")
        form.addRow("  time constant", self.f_tau)
        self.f_cutoff = _dspin("filter_cutoff_hz", 3, " Hz")
        form.addRow("  cutoff", self.f_cutoff)
        self.f_order = QComboBox()
        for o in SCHEMA["filter_order"]["choices"]:
            self.f_order.addItem(str(o), o)
        form.addRow("  order", self.f_order)
        self.notch_hz = _dspin("notch_hz", 1, " Hz")
        form.addRow("  notch at", self.notch_hz)
        self.notch_q = _dspin("notch_q", 1)
        form.addRow("  Q", self.notch_q)

        if not processing.HAVE_SCIPY:
            for combo in (self.filter, self.outlier):
                model = combo.model()
                for i in range(combo.count()):
                    if not processing.available(combo.itemData(i)):
                        model.item(i).setEnabled(False)
                        model.item(i).setToolTip("needs scipy: "
                                                 "pip install scipy")

        self.status = _hint("")
        form.addRow(self.status)
        form.addRow(_hint("Applies to the views, statistics, noise meter "
                          "and field map. Recording and export stay raw."))

        # -- signals ------------------------------------------------------
        for w in (self.tc_box,):
            w.toggled.connect(self._emit)
        for w in (self.tc_source, self.outlier, self.filter, self.f_order):
            w.currentIndexChanged.connect(self._emit)
        for w in (self.coeff, self.tref, self.out_window, self.out_k,
                  self.f_window, self.f_tau, self.f_cutoff, self.notch_hz,
                  self.notch_q):
            w.valueChanged.connect(self._emit)
        self.magnet.currentIndexChanged.connect(self._magnet_picked)
        self.tref_now.clicked.connect(self.set_ref_now.emit)
        self._quiet = False
        self._update_rows()

    # -- values ----------------------------------------------------------

    def values(self):
        return {
            "temp_comp": self.tc_box.isChecked(),
            "temp_comp_source": self.tc_source.currentData(),
            "temp_coeff_pct": round(self.coeff.value(), 4),
            "temp_ref_C": round(self.tref.value(), 2),
            "filter": self.filter.currentData(),
            "filter_window": self.f_window.value(),
            "filter_tau_s": round(self.f_tau.value(), 4),
            "filter_cutoff_hz": round(self.f_cutoff.value(), 4),
            "filter_order": self.f_order.currentData(),
            "notch_hz": round(self.notch_hz.value(), 2),
            "notch_q": round(self.notch_q.value(), 2),
            "outlier": self.outlier.currentData(),
            "outlier_window": self.out_window.value(),
            "outlier_k": round(self.out_k.value(), 2),
        }

    def set_values(self, v):
        self._quiet = True
        try:
            if "temp_comp" in v:
                self.tc_box.setChecked(bool(v["temp_comp"]))
            for combo, key in ((self.tc_source, "temp_comp_source"),
                               (self.filter, "filter"),
                               (self.outlier, "outlier"),
                               (self.f_order, "filter_order")):
                if key in v:
                    i = combo.findData(v[key])
                    if i >= 0:
                        combo.setCurrentIndex(i)
            for box, key in ((self.coeff, "temp_coeff_pct"),
                             (self.tref, "temp_ref_C"),
                             (self.out_window, "outlier_window"),
                             (self.out_k, "outlier_k"),
                             (self.f_window, "filter_window"),
                             (self.f_tau, "filter_tau_s"),
                             (self.f_cutoff, "filter_cutoff_hz"),
                             (self.notch_hz, "notch_hz"),
                             (self.notch_q, "notch_q")):
                if key in v:
                    box.setValue(v[key])
            if "temp_coeff_pct" in v:
                self._sync_magnet(v["temp_coeff_pct"])
        finally:
            self._quiet = False
        self._update_rows()

    def show_status(self, text):
        self.status.setText(text)
        self.status.setVisible(bool(text))

    # -- internals ---------------------------------------------------------

    def _emit(self, *_):
        self._update_rows()
        if not self._quiet:
            self._sync_magnet(self.coeff.value())
            self.changed.emit()

    def _magnet_picked(self, *_):
        value = self.magnet.currentData()
        if value is not None and not self._quiet:
            self.coeff.setValue(value)          # emits changed

    def _sync_magnet(self, coeff):
        quiet, self._quiet = self._quiet, True
        idx = next((i for i in range(self.magnet.count())
                    if self.magnet.itemData(i) is not None
                    and abs(self.magnet.itemData(i) - coeff) < 1e-6),
                   self.magnet.count() - 1)
        self.magnet.blockSignals(True)
        self.magnet.setCurrentIndex(idx)
        self.magnet.blockSignals(False)
        self._quiet = quiet

    def _update_rows(self):
        tc = self.tc_box.isChecked()
        for w in self._tc_rows:
            _set_row_visible(self.form, w, tc)
        f = self.filter.currentData()
        _set_row_visible(self.form, self.f_window,
                         f in ("moving_average", "median"))
        _set_row_visible(self.form, self.f_tau, f == "ema")
        _set_row_visible(self.form, self.f_cutoff, f == "lowpass")
        _set_row_visible(self.form, self.f_order, f == "lowpass")
        _set_row_visible(self.form, self.notch_hz, f == "notch")
        _set_row_visible(self.form, self.notch_q, f == "notch")
        o = self.outlier.currentData()
        _set_row_visible(self.form, self.out_window, o == "hampel")
        _set_row_visible(self.form, self.out_k, o != "none")


class NoisePanel(QGroupBox):
    """Record a block of fresh samples and report the noise per channel,
    next to what the datasheet predicts."""

    measure_requested = Signal(float)

    def __init__(self):
        super().__init__("Noise measurement")
        form = QFormLayout(self)
        self.duration = QDoubleSpinBox()
        self.duration.setRange(1.0, 600.0)
        self.duration.setValue(10.0)
        self.duration.setSuffix(" s")
        self.duration.setDecimals(1)
        self.button = QPushButton("Measure")
        self.button.setProperty("role", "primary")
        self.button.clicked.connect(
            lambda: self.measure_requested.emit(self.duration.value()))
        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.duration, 1)
        rl.addWidget(self.button)
        form.addRow("Duration", row)
        self.result = QLabel(
            "Keep the sensor still. σ is computed after removing the "
            "linear drift.")
        self.result.setProperty("role", "hint")
        self.result.setWordWrap(True)
        self.result.setTextFormat(Qt.RichText)
        form.addRow(self.result)

    def busy(self, seconds_left):
        self.button.setEnabled(False)
        self.result.setText(f"measuring… {seconds_left:.0f} s left — keep "
                            "the sensor still")

    def show_error(self, text):
        self.button.setEnabled(True)
        self.result.setText(text)

    def show_report(self, rep):
        self.button.setEnabled(True)
        if "error" in rep:
            self.result.setText(rep["error"])
            return
        rows = []
        for key in ("bx", "by", "bz", "mag", "temp", "rtd"):
            c = rep["channels"].get(key)
            if not c:
                continue
            exp = c.get("expected_std")
            flag = ""
            if c.get("vs_expected") and c["vs_expected"] > 2:
                flag = " ⚠"
            proc = c.get("std_processed")
            rows.append(
                f"<tr><td>{key}</td><td align=right>{c['std']:.2f}</td>"
                f"<td align=right>{'' if proc is None else f'{proc:.2f}'}</td>"
                f"<td align=right>{'' if exp is None else f'{exp:.0f}'}{flag}"
                f"</td><td>{c['unit']}</td></tr>")
        self.result.setText(
            f"{rep['samples']} samples in {rep['seconds']} s "
            f"({rep['rate_hz']} Hz)"
            "<table cellspacing=4><tr><td></td><td>σ raw</td>"
            "<td>σ proc.</td><td>expected</td><td></td></tr>"
            + "".join(rows) + "</table>"
            "<span>expected = datasheet + 10 µT print step; ⚠ = more than "
            "2× expected (environment, vibration or a moving sensor)</span>")
