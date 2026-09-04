"""
gui.py -- live plot of the TMAG5170 magnetic field and die temperature,
plus Excel export.

Owns no serial code. It asks sensor.SensorReader for samples and draws them.

    python gui.py

Requires: pip install pyserial matplotlib pandas openpyxl
"""

import time
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import pandas as pd
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

import sensor

WINDOW_POINTS = 200     # samples visible on the plot
POLL_MS = 50            # how often the GUI drains the queue
RATE_WINDOW_S = 2.0     # averaging window for the GUI sample rate
NO_DATA_GRACE_S = 4.0   # how long to wait before complaining about silence

# axis: "mag" -> left-hand mT axis, "temp" -> right-hand degC axis.
# Temperature gets its own axis because mT and degC share no scale; putting
# ~25 degC on a +/-100 mT axis would flatten it into a meaningless streak.
CHANNELS = [
    ("bx",   "Bx",    "tab:blue",   "mag"),
    ("by",   "By",    "tab:orange", "mag"),
    ("bz",   "Bz",    "tab:green",  "mag"),
    ("mag",  "|B|",   "tab:red",    "mag"),
    ("temp", "T die", "tab:purple", "temp"),
    ("rtd",  "T rtd", "tab:brown",  "temp"),
]

# Both temperature traces share the right-hand axis: same units, and seeing
# them on one scale is the point -- the die sensor and the RTD measuring the
# same thing should agree.
TEMP_CHANNELS = [c for c in CHANNELS if c[3] == "temp"]

MAG_CHANNELS = [c for c in CHANNELS if c[3] == "mag"]


class App:
    def __init__(self, root):
        self.root = root
        self.reader = None
        self.start_time = None
        self.no_data_warned = False

        self.times = []
        self.series = {key: [] for key, _, _, _ in CHANNELS}

        self._build_plot()
        self._build_controls()
        self.apply_temp_mode()      # start in whatever mode the toggle says

    # -- layout ----------------------------------------------------------

    def _build_plot(self):
        fig = Figure(figsize=(7, 4), dpi=100)
        self.ax = fig.add_subplot(111)
        self.ax.set_xlabel("Time [s]")
        self.ax.set_ylabel("B [mT]")
        self.ax.grid(True)

        # Shares the x-axis, own y-axis on the right.
        self.ax_temp = self.ax.twinx()
        self.ax_temp.set_ylabel("T [degC]")

        self.lines = {}
        for key, label, color, axis in CHANNELS:
            target = self.ax_temp if axis == "temp" else self.ax
            style = "--" if axis == "temp" else "-"
            line, = target.plot([], [], color=color, label=label,
                                linewidth=1.4, linestyle=style)
            self.lines[key] = line

        self.canvas = FigureCanvasTkAgg(fig, master=self.root)
        self.canvas.get_tk_widget().pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.canvas.draw()

    def _build_controls(self):
        frame = tk.Frame(self.root)
        frame.pack(side=tk.RIGHT, fill=tk.Y, padx=20)

        tk.Label(frame, text="COM Port:").pack(pady=(10, 2))
        self.port_box = ttk.Combobox(frame, state="readonly", width=12)
        self.port_box.pack(padx=10, pady=(0, 4))
        tk.Button(frame, text="Refresh Ports", command=self.refresh_ports).pack(pady=(0, 10))
        self.refresh_ports()

        tk.Label(frame, text="Baud Rate:").pack(pady=(10, 2))
        self.baud_box = ttk.Combobox(
            frame,
            values=["9600", "19200", "38400", "57600", "115200"],
            state="readonly", width=12,
        )
        self.baud_box.set(str(sensor.BAUD))
        self.baud_box.pack(padx=10, pady=(0, 10))

        self.fake_var = tk.IntVar(value=0)
        tk.Checkbutton(frame, text="Simulate (no board)",
                       variable=self.fake_var).pack(pady=(0, 10))

        self.start_button = tk.Button(frame, text="Start", command=self.start, width=14)
        self.start_button.pack(pady=(0, 4))
        self.stop_button = tk.Button(frame, text="Stop", command=self.stop,
                                     width=14, state=tk.DISABLED)
        self.stop_button.pack(pady=(0, 10))

        # -- view mode ---------------------------------------------------
        # Display only. Temperature keeps being received and stored either
        # way, so toggling never loses data and the export stays complete.
        tk.Label(frame, text="View:").pack(pady=(10, 2))
        self.temp_mode = tk.IntVar(value=1)
        tk.Radiobutton(frame, text="Field + temperatures", variable=self.temp_mode,
                       value=1, command=self.apply_temp_mode).pack(anchor="w", padx=20)
        tk.Radiobutton(frame, text="Field only", variable=self.temp_mode,
                       value=0, command=self.apply_temp_mode).pack(anchor="w", padx=20)

        # -- per-channel visibility (magnetic axes only) -----------------
        tk.Label(frame, text="Show:").pack(pady=(10, 2))
        self.visible = {}
        for key, label, _, _ in MAG_CHANNELS:
            var = tk.IntVar(value=1)
            self.visible[key] = var
            tk.Checkbutton(frame, text=label, variable=var,
                           command=self.apply_visibility).pack(anchor="w", padx=20)

        # Latest temperature as a number -- easier to read off than the trace.
        self.temp_label = tk.Label(frame, text="die --.-- C\nrtd --.-- C",
                                   font=("TkFixedFont", 10), justify="left")
        self.temp_label.pack(pady=(12, 0))

        # -- throughput ---------------------------------------------------
        # Measured is what actually arrives here; reported is what the
        # firmware calculated its own ceilings to be. A large gap between
        # them means something upstream is slower than it thinks it is.
        tk.Label(frame, text="Rates:").pack(pady=(14, 2))
        # Sensor-side: how often a new value exists on the chip. Coloured
        # to group these apart from the host-side rate below them.
        self.conv_label = tk.Label(frame, text="mode  --",
                                   font=("TkFixedFont", 9), justify="left",
                                   fg="#1a5f1a")
        self.conv_label.pack(anchor="w", padx=20)
        # Host-side: how fast finished lines reach this program. A different
        # quantity from the two above, so it gets its own label and colour.
        self.rate_label = tk.Label(frame, text="GUI sampled   --",
                                   font=("TkFixedFont", 9), justify="left",
                                   fg="#1a3f7a")
        self.rate_label.pack(anchor="w", padx=20, pady=(4, 0))
        # How many finished conversions go unread. Every set you skip is a
        # measurement the sensor made and then overwrote.
        self.ratio_label = tk.Label(frame, text="", font=("TkFixedFont", 9),
                                    justify="left", wraplength=150, fg="gray")
        self.ratio_label.pack(anchor="w", padx=20)
        self.limits_label = tk.Label(frame, text="config  (waiting)",
                                     font=("TkFixedFont", 9), justify="left",
                                     wraplength=150, fg="gray")
        self.limits_label.pack(anchor="w", padx=20)

        tk.Button(frame, text="Clear", command=self.clear, width=14).pack(pady=(15, 4))
        tk.Button(frame, text="Export to Excel", command=self.export,
                  width=14).pack(pady=(0, 10))

        self.status = tk.Label(frame, text="Idle", fg="gray", wraplength=140)
        self.status.pack(pady=(10, 0))

    # -- view mode -------------------------------------------------------

    def apply_temp_mode(self):
        """Show or hide the whole temperature axis, not just its line --
        a lone empty right-hand axis with a degC label is worse than none."""
        on = bool(self.temp_mode.get())

        self.ax_temp.set_visible(on)
        for key, _, _, _ in TEMP_CHANNELS:
            self.lines[key].set_visible(on)

        if on:
            self.temp_label.pack(pady=(12, 0))
            self.ax.set_title("TMAG5170 magnetic field and temperatures")
        else:
            self.temp_label.pack_forget()
            self.ax.set_title("TMAG5170 magnetic field")

        self._rebuild_legend()
        self.canvas.draw_idle()

    def _rebuild_legend(self):
        """One legend covering both axes. Built by hand because matplotlib
        would otherwise draw a separate overlapping legend per axis."""
        shown = [(key, label) for key, label, _, _ in CHANNELS
                 if self.lines[key].get_visible()]
        if shown:
            self.ax.legend([self.lines[k] for k, _ in shown],
                           [lbl for _, lbl in shown],
                           loc="upper right")
        elif self.ax.get_legend() is not None:
            self.ax.get_legend().remove()

    # -- actions ---------------------------------------------------------

    def refresh_ports(self):
        """Never let a port-enumeration failure take the window down --
        pyserial may be missing, or the OS may deny the device list."""
        try:
            ports = [p.device for p in sensor.list_ports()]
        except ImportError:
            self.port_box["values"] = []
            self.port_box.set("")
            self.set_status("pyserial not installed\n(pip install pyserial)",
                            "red")
            return
        except Exception as e:                       # noqa: BLE001
            self.port_box["values"] = []
            self.port_box.set("")
            self.set_status(f"Could not list ports:\n{e}", "red")
            return

        self.port_box["values"] = ports
        self.port_box.set(ports[0] if ports else "")
        if not ports:
            self.set_status("No serial ports found.\nIs the board plugged in?",
                            "orange")

    def set_status(self, text, colour="gray"):
        self.status.config(text=text, fg=colour)

    def fail(self, title, message):
        """Fatal: tell the user once, put it in the status area so it stays
        readable after the dialog is dismissed, and stop cleanly."""
        self.set_status(message.split("\n")[0], "red")
        self.stop()
        messagebox.showerror(title, message)

    def start(self):
        if self.reader is not None:
            return

        fake = bool(self.fake_var.get())
        port = self.port_box.get() or None
        if not fake and port is None:
            messagebox.showerror(
                "No port selected",
                "Pick a COM port from the list, or tick Simulate to run "
                "without hardware.\n\nIf the list is empty, check the USB "
                "cable and press Refresh Ports.")
            return

        try:
            self.reader = sensor.SensorReader(
                port=port, baud=int(self.baud_box.get()), fake=fake
            ).start()
        except Exception as e:                       # noqa: BLE001
            self.reader = None
            messagebox.showerror("Could not start", str(e))
            return

        self.start_time = time.perf_counter()
        self.no_data_warned = False

        self.start_button.config(state=tk.DISABLED)
        self.stop_button.config(state=tk.NORMAL)
        self.set_status("Simulating" if fake else f"Reading {port}", "green")

        self.poll()

    def stop(self):
        if self.reader is not None:
            self.reader.stop()
            self.reader = None
        self.start_button.config(state=tk.NORMAL)
        self.stop_button.config(state=tk.DISABLED)
        if self.status.cget("fg") != "red":      # keep an error message visible
            self.set_status("Stopped", "gray")

    def gui_sample_rate(self):
        """How fast samples are reaching this program, over the last
        RATE_WINDOW_S. This is a host-side measurement -- it says nothing
        about how fast the sensor itself is converting.
        Returns None until there is enough history to mean anything."""
        if len(self.times) < 2:
            return None
        cutoff = self.times[-1] - RATE_WINDOW_S
        recent = [t for t in self.times if t >= cutoff]
        if len(recent) < 2:
            return None
        span = recent[-1] - recent[0]
        if span <= 0:
            return None
        return (len(recent) - 1) / span

    def update_throughput(self):
        rate = self.gui_sample_rate()
        self.rate_label.config(
            text="GUI sampled   --" if rate is None
            else f"GUI sampled   {sensor.format_rate(rate)}")

        cfg = self.reader.config if self.reader else None
        if cfg is None:
            self.conv_label.config(text="mode  --")
            self.rate_label.config(text="GUI sampled   --")
            self.ratio_label.config(text="")
            self.limits_label.config(text="config  (waiting)", fg="gray")
            return

        # Averaging mode and the conversion rate it produces. The rate is
        # computed here from conv_avg rather than sent over the wire.
        conv = sensor.conversion_rate_hz(cfg.conv_avg, cfg.axes or 3,
                                         bool(cfg.temp))
        channels = f"{cfg.axes} axes" if cfg.axes else "axes"
        if cfg.temp:
            channels += " + T"

        self.conv_label.config(
            text=(f"mode          {cfg.conv_avg}x averaging\n"
                  f"tmag conv     {sensor.format_rate(conv)}\n"
                  f"rtd conv      {sensor.format_rate(cfg.rtd_hz)}"))

        self.limits_label.config(
            text=(f"{channels}\n"
                  f"range +/-{cfg.range_mt} mT"
                  if cfg.range_mt else channels),
            fg="black")

        # Fraction of finished conversions that actually reach the plot.
        if rate and conv:
            skipped = conv / rate
            self.ratio_label.config(
                text=(f"GUI reads 1 of every {skipped:.0f}\n"
                      f"sensor conversions "
                      f"({100.0 / skipped:.1f}% used)"))
        else:
            self.ratio_label.config(text="")

        self.limits_label.config(
            text=(f"delivery ceilings\n"
                  f"  spi   {sensor.format_rate(lim.spi)}\n"
                  f"  uart  {sensor.format_rate(lim.uart)}\n"
                  f"  loop  {sensor.format_rate(lim.loop)}\n"
                  f"  -> {lim.bottleneck} @ {sensor.format_rate(lim.rate)}"),
            fg="black")

    def clear(self):
        self.times.clear()
        for values in self.series.values():
            values.clear()
        self.temp_label.config(text="die --.-- C\nrtd --.-- C")
        self.update_throughput()
        self.redraw()

    def apply_visibility(self):
        for key, _, _, _ in MAG_CHANNELS:
            self.lines[key].set_visible(self.visible[key].get() == 1)
        self._rebuild_legend()
        self.canvas.draw_idle()

    def export(self):
        """Always writes every column, including temperature, regardless of
        what the plot is currently showing."""
        if not self.times:
            messagebox.showinfo("Nothing to export", "No samples collected yet.")
            return

        filename = filedialog.asksaveasfilename(
            defaultextension=".xlsx",
            filetypes=[("Excel files", "*.xlsx")],
        )
        if not filename:
            return

        df = pd.DataFrame({
            "Time [s]":  self.times,
            "Bx [mT]":   self.series["bx"],
            "By [mT]":   self.series["by"],
            "Bz [mT]":   self.series["bz"],
            "|B| [mT]":     self.series["mag"],
            "T die [degC]": self.series["temp"],
            "T rtd [degC]": self.series["rtd"],
        })
        df.to_excel(filename, index=False)
        messagebox.showinfo("Data Saved", f"{len(df)} samples saved to:\n{filename}")

    # -- loop ------------------------------------------------------------

    def poll(self):
        """Drain the queue and redraw, then reschedule itself.

        The reschedule lives in a finally block on purpose. tkinter's after()
        chain has no supervisor: if this callback raises, the traceback goes
        to stderr and the callback is simply never queued again -- the GUI
        goes quiet while the reader thread keeps filling the queue, and only
        Stop/Start revives it. Re-arming unconditionally means one bad frame
        costs one frame, not the session.
        """
        if self.reader is None:
            return                      # stopped: let the chain end here

        try:
            self._poll_once()
        except Exception as e:           # noqa: BLE001
            # Surface it instead of letting it vanish into stderr.
            self.set_status(f"Plot error: {e}", "red")
            traceback.print_exc()
        finally:
            if self.reader is not None:
                self.root.after(POLL_MS, self.poll)

    def _poll_once(self):
        if self.reader.error:
            self.fail("Serial error", self.reader.error)
            return

        samples = self.reader.drain()

        if samples:
            self._append(samples)
            last = samples[-1]
            self.temp_label.config(
                text=f"die {last.temp:6.2f} C\nrtd {last.rtd:6.2f} C")
            self.redraw()

            if self.no_data_warned:      # data arrived late; clear the warning
                self.no_data_warned = False
                self.set_status(f"Reading {self.reader.port or 'simulated'}",
                                "green")
        elif not self.no_data_warned and not self.times:
            # Port opened but nothing usable yet. Warn in the status area
            # only -- a modal dialog here would block this very callback.
            if time.perf_counter() - self.start_time > NO_DATA_GRACE_S:
                why = self.reader.diagnose_no_data()
                if why:
                    self.no_data_warned = True
                    self.set_status(why.split("\n")[0], "orange")

        self.update_throughput()

    def _append(self, samples):
        """Timestamp a batch and store it.

        A drain can return several samples at once, especially right after
        connecting to a board that is already streaming. Stamping them all
        with the same instant gives a zero-width x-range, which makes both
        the rate calculation and matplotlib's autoscaling degenerate, so
        spread them across the interval since the previous poll instead.
        """
        now = time.perf_counter() - self.start_time
        prev = self.times[-1] if self.times else max(0.0, now - POLL_MS / 1000)
        span = max(now - prev, 1e-6)
        n = len(samples)

        for i, s in enumerate(samples, start=1):
            self.times.append(prev + span * i / n)
            for key, _, _, _ in CHANNELS:
                self.series[key].append(getattr(s, key))

    def redraw(self):
        t = self.times[-WINDOW_POINTS:]
        for key, line in self.lines.items():
            line.set_data(t, self.series[key][-WINDOW_POINTS:])

        # Both axes need rescaling; the temperature axis is not automatic.
        for axis in (self.ax, self.ax_temp):
            axis.relim()
            axis.autoscale_view()

        self.canvas.draw_idle()

    def on_close(self):
        self.stop()
        self.root.destroy()


def main():
    root = tk.Tk()
    root.title("TMAG5170 Magnetic Field")
    root.geometry("1100x600")
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()