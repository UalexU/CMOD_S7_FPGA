"""
gui.py -- live plot of the TMAG5170 magnetic field and die temperature,
plus Excel export.

Owns no serial code. It asks sensor.SensorReader for samples and draws them.

    python gui.py

Requires: pip install pyserial matplotlib pandas openpyxl
"""

import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import pandas as pd
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

import sensor

WINDOW_POINTS = 200     # samples visible on the plot
POLL_MS = 50            # how often the GUI drains the queue
RATE_WINDOW_S = 2.0     # averaging window for the measured arrival rate

# axis: "mag" -> left-hand mT axis, "temp" -> right-hand degC axis.
# Temperature gets its own axis because mT and degC share no scale; putting
# ~25 degC on a +/-100 mT axis would flatten it into a meaningless streak.
CHANNELS = [
    ("bx",   "Bx",  "tab:blue",   "mag"),
    ("by",   "By",  "tab:orange", "mag"),
    ("bz",   "Bz",  "tab:green",  "mag"),
    ("mag",  "|B|", "tab:red",    "mag"),
    ("temp", "T",   "tab:purple", "temp"),
]

MAG_CHANNELS = [c for c in CHANNELS if c[3] == "mag"]


class App:
    def __init__(self, root):
        self.root = root
        self.reader = None
        self.start_time = None

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
        tk.Radiobutton(frame, text="Field + temperature", variable=self.temp_mode,
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
        self.temp_label = tk.Label(frame, text="T --.-- C", font=("TkDefaultFont", 11))
        self.temp_label.pack(pady=(12, 0))

        # -- throughput ---------------------------------------------------
        # Measured is what actually arrives here; reported is what the
        # firmware calculated its own ceilings to be. A large gap between
        # them means something upstream is slower than it thinks it is.
        tk.Label(frame, text="Throughput:").pack(pady=(14, 2))
        self.conv_label = tk.Label(frame, text="conversion  --- Hz",
                                   font=("TkFixedFont", 9), justify="left",
                                   fg="#1a5f1a")
        self.conv_label.pack(anchor="w", padx=20)
        self.rate_label = tk.Label(frame, text="measured  --.- Hz",
                                   font=("TkFixedFont", 9), justify="left")
        self.rate_label.pack(anchor="w", padx=20)
        # How many finished conversions go unread. Every set you skip is a
        # measurement the sensor made and then overwrote.
        self.ratio_label = tk.Label(frame, text="", font=("TkFixedFont", 9),
                                    justify="left", wraplength=150, fg="gray")
        self.ratio_label.pack(anchor="w", padx=20)
        self.limits_label = tk.Label(frame, text="firmware  (waiting)",
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
        self.lines["temp"].set_visible(on)

        if on:
            self.temp_label.pack(pady=(12, 0))
            self.ax.set_title("TMAG5170 magnetic field and temperature")
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
        ports = [p.device for p in sensor.list_ports()]
        self.port_box["values"] = ports
        self.port_box.set(ports[0] if ports else "")

    def start(self):
        if self.reader is not None:
            return

        fake = bool(self.fake_var.get())
        port = self.port_box.get() or None
        if not fake and port is None:
            messagebox.showerror("No port", "Select a COM port, or tick Simulate.")
            return

        self.reader = sensor.SensorReader(
            port=port, baud=int(self.baud_box.get()), fake=fake
        ).start()
        self.start_time = time.perf_counter()

        self.start_button.config(state=tk.DISABLED)
        self.stop_button.config(state=tk.NORMAL)
        self.status.config(text="Simulating" if fake else f"Reading {port}", fg="green")

        self.poll()

    def stop(self):
        if self.reader is not None:
            self.reader.stop()
            self.reader = None
        self.start_button.config(state=tk.NORMAL)
        self.stop_button.config(state=tk.DISABLED)
        self.status.config(text="Stopped", fg="gray")

    def measured_rate(self):
        """Samples per second over the last RATE_WINDOW_S of arrivals.
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
        rate = self.measured_rate()
        self.rate_label.config(
            text="measured  --.- Hz" if rate is None
            else f"measured {rate:6.1f} Hz")

        lim = self.reader.limits if self.reader else None
        if lim is None:
            self.conv_label.config(text="conversion  --- Hz")
            self.ratio_label.config(text="")
            self.limits_label.config(text="firmware  (waiting)", fg="gray")
            return

        # Conversion rate, with the settings that produced it.
        detail = ""
        if lim.conv_avg is not None and lim.axes is not None:
            channels = f"{lim.axes} axes" + (" + T" if lim.temp else "")
            detail = f"  ({lim.conv_avg}x avg, {channels})"
        self.conv_label.config(text=f"conversion {lim.sensor:5d} Hz{detail}")

        # Fraction of finished conversions that actually reach the plot.
        if rate is not None and rate > 0 and lim.sensor > 0:
            skipped = lim.sensor / rate
            self.ratio_label.config(
                text=(f"reading 1 of every {skipped:.0f}\n"
                      f"conversions ({100.0 / skipped:.1f}% used)"))
        else:
            self.ratio_label.config(text="")

        self.limits_label.config(
            text=(f"firmware ceilings\n"
                  f"  sensor {lim.sensor:6d} Hz\n"
                  f"  spi    {lim.spi:6d} Hz\n"
                  f"  uart   {lim.uart:6d} Hz\n"
                  f"  loop   {lim.loop:6d} Hz\n"
                  f"  -> {lim.bottleneck} @ {lim.rate} Hz"),
            fg="black")

    def clear(self):
        self.times.clear()
        for values in self.series.values():
            values.clear()
        self.temp_label.config(text="T --.-- C")
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
            "|B| [mT]":  self.series["mag"],
            "T [degC]":  self.series["temp"],
        })
        df.to_excel(filename, index=False)
        messagebox.showinfo("Data Saved", f"{len(df)} samples saved to:\n{filename}")

    # -- loop ------------------------------------------------------------

    def poll(self):
        """Drain the queue and redraw. Never blocks -- the reader thread
        does the waiting."""
        if self.reader is None:
            return

        if self.reader.error:
            messagebox.showerror("Serial error", self.reader.error)
            self.stop()
            return

        samples = self.reader.drain()
        if samples:
            now = time.perf_counter() - self.start_time
            for s in samples:
                self.times.append(now)
                for key, _, _, _ in CHANNELS:
                    self.series[key].append(getattr(s, key))
            self.temp_label.config(text=f"T {samples[-1].temp:.2f} C")
            self.redraw()

        self.update_throughput()
        self.root.after(POLL_MS, self.poll)

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