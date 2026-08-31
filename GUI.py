"""
gui.py -- live plot of the TMAG5170 magnetic field, and Excel export.

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

CHANNELS = [
    ("bx",  "Bx",  "tab:blue"),
    ("by",  "By",  "tab:orange"),
    ("bz",  "Bz",  "tab:green"),
    ("mag", "|B|", "tab:red"),
]


class App:
    def __init__(self, root):
        self.root = root
        self.reader = None
        self.start_time = None

        self.times = []
        self.series = {key: [] for key, _, _ in CHANNELS}

        self._build_plot()
        self._build_controls()

    # -- layout ----------------------------------------------------------

    def _build_plot(self):
        fig = Figure(figsize=(7, 4), dpi=100)
        self.ax = fig.add_subplot(111)
        self.ax.set_title("TMAG5170 magnetic field")
        self.ax.set_xlabel("Time [s]")
        self.ax.set_ylabel("B [mT]")
        self.ax.grid(True)

        self.lines = {}
        for key, label, color in CHANNELS:
            line, = self.ax.plot([], [], color=color, label=label, linewidth=1.4)
            self.lines[key] = line
        self.ax.legend(loc="upper right")

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

        tk.Label(frame, text="Show:").pack(pady=(10, 2))
        self.visible = {}
        for key, label, _ in CHANNELS:
            var = tk.IntVar(value=1)
            self.visible[key] = var
            tk.Checkbutton(frame, text=label, variable=var,
                           command=self.apply_visibility).pack(anchor="w", padx=20)

        tk.Button(frame, text="Clear", command=self.clear, width=14).pack(pady=(15, 4))
        tk.Button(frame, text="Export to Excel", command=self.export,
                  width=14).pack(pady=(0, 10))

        self.status = tk.Label(frame, text="Idle", fg="gray", wraplength=140)
        self.status.pack(pady=(10, 0))

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

    def clear(self):
        self.times.clear()
        for values in self.series.values():
            values.clear()
        self.redraw()

    def apply_visibility(self):
        for key, line in self.lines.items():
            line.set_visible(self.visible[key].get() == 1)
        self.canvas.draw_idle()

    def export(self):
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
                for key, _, _ in CHANNELS:
                    self.series[key].append(getattr(s, key))
            self.redraw()

        self.root.after(POLL_MS, self.poll)

    def redraw(self):
        t = self.times[-WINDOW_POINTS:]
        for key, line in self.lines.items():
            line.set_data(t, self.series[key][-WINDOW_POINTS:])
        self.ax.relim()
        self.ax.autoscale_view()
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