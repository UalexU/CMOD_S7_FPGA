import tkinter as tk
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from tkinter import filedialog, messagebox, ttk
import pandas as pd
import random
import serial
import serial.tools.list_ports
import time

def com_handle():
    com_port = com_text_box.get()
    return com_port
    
def refresh_ports():
    ports = [port.device for port in serial.tools.list_ports.comports()]
    com_text_box["values"] = ports
    if ports:
        com_text_box.set(ports[0])
    else:
        com_text_box.set("")
    
def baud_handle():
    baud_rate = baud_text_box.get()
    return baud_rate
    
def start_sensors():
    PORT = com_handle()
    BAUDRATE = int(baud_handle())
    ser = serial.Serial(PORT, baudrate=BAUDRATE, timeout=1)
    print(f"Successfully connected to {PORT} at {BAUDRATE} baud.")
    time.sleep(1)
    print("initializing sensors")
    ser.write(b"S")
    start_time = time.perf_counter()
    ser.reset_input_buffer()
    ser.reset_output_buffer()
    update_plot(0, start_time, ser)

def toggle_sensor1():
    sensor1_line.set_visible(sensor1_checkbox_enable.get() == 1)
    canvas.draw_idle()
    
def toggle_sensor2():
    sensor2_line.set_visible(sensor2_checkbox_enable.get() == 1)
    canvas.draw_idle()

def export_data():
    print("saving data")
    filename = filedialog.asksaveasfilename(
        defaultextension=".xlsx",
        filetypes=[("Excel files", "*.xlsx")]
    )

    if not filename: #if user cancels save dialog box
        return

    data = {
        "Time [Seconds]": x_data,
        "Sensor 1 [C]": y1,
        "Sensor 2 [C]": y2
    }

    df = pd.DataFrame(data)
    df.to_excel(filename, index=False)

    messagebox.showinfo(
        "Data Saved",
        f"Data saved to:\n{filename}"
    )
    
def update_plot(n, start_time, ser):
    
    current_time = time.perf_counter() - start_time
    ydata = single_read(ser)

    y1.append(ydata[0])
    y2.append(ydata[1])
    x_data.append(current_time)
    
    x_data_plot = x_data[-30:]
    y1_plot = y1[-30:]
    y2_plot = y2[-30:]

    sensor1_line.set_data(x_data_plot, y1_plot)
    sensor2_line.set_data(x_data_plot, y2_plot)

    ax.relim()
    ax.autoscale_view()

    canvas.draw_idle()
    window.after(100, update_plot, n + 1, start_time, ser)
    
def single_read(ser):
    timeout = 0
    while timeout < 10:
        raw_data = ser.readline()
        cleaned_data = raw_data.decode('utf-8', errors='ignore').strip()
        if cleaned_data:
            print(cleaned_data)
            split_data = cleaned_data.split(",")
            values = [int(x)/10 for x in split_data]
            return values
        else:
            timeout = timeout + 1
    raise ValueError('No data received - timed out.')
    
# Initialize the main UI window context
window = tk.Tk()
window.title("Temp Reading GUI")
window.geometry("1000x800")

fig = Figure(figsize=(6, 3), dpi=100)
ax = fig.add_subplot(111)

# Define your data and plot it
x_data = []
y1 = []
y2 = []
sensor1_line, = ax.plot(x_data, y1, marker='o', color='b', label='Sensor 1', visible=1)
sensor2_line, = ax.plot(x_data, y2, marker='o', color='r', label='Sensor 2', visible=1)
ax.set_title("Sensor Temp")
ax.set_xlabel("Time [Seconds]")
ax.set_ylabel("Temp [C]")
ax.legend(loc="upper right")
ax.set_ylim(15, 45)
ax.grid(True)

# 3. Embed the Figure into the Tkinter Canvas
canvas = FigureCanvasTkAgg(fig, master=window)

# Right-side controls
control_frame = tk.Frame(window)
control_frame.pack(side=tk.RIGHT, fill=tk.Y, padx=20)

# Plot
canvas_widget = canvas.get_tk_widget()
canvas_widget.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

# Create a frame for the checkboxes
control_frame = tk.Frame(window)
control_frame.pack(side=tk.RIGHT, fill=tk.Y, padx=20)

com_label = tk.Label(control_frame, text="COM Port:")
com_label.pack(pady=(10, 2))
com_label = tk.Label(control_frame, text="COM Port:")
com_label.pack(pady=(10, 2))
ports = [port.device for port in serial.tools.list_ports.comports()]
com_text_box = ttk.Combobox(
    control_frame,
    values=ports,
    state="readonly",
    width=10
)
if ports:
    com_text_box.set(ports[0])
com_text_box.pack(padx=10, pady=(0, 10))

refresh_button = tk.Button(
    control_frame,
    text="Refresh Ports",
    command=refresh_ports
)

refresh_button.pack(pady=5)
baud_label = tk.Label(control_frame, text="Baud Rate:")
baud_label.pack(pady=(10, 2))
baud_text_box = ttk.Combobox(
    control_frame,
    values=["9600", "19200", "38400", "57600", "115200"],
    state="readonly",
    width=10
)
baud_text_box.set("115200")
baud_text_box.pack(padx=10, pady=(0, 10))

StartButton = tk.Button(
    control_frame,
    text="Start",
    command=start_sensors
)
StartButton.pack(pady=10)

sensor1_checkbox_enable = tk.IntVar(value=1)
checkbox1 = tk.Checkbutton(
    control_frame,
    text="Plot Sensor 1 Data",
    variable=sensor1_checkbox_enable,
    command=toggle_sensor1
)
checkbox1.pack(pady=10)

sensor2_checkbox_enable = tk.IntVar(value=1)
checkbox2 = tk.Checkbutton(
    control_frame,
    text="Plot Sensor 2 Data",
    variable=sensor2_checkbox_enable,
    command=toggle_sensor2
)
checkbox2.pack(pady=10)

ExportButton = tk.Button(
    control_frame,
    text="Export data to Excel",
    command=export_data
)
ExportButton.pack(pady=10)

# 4. Draw the canvas and start the loop
canvas.draw()

# Keeps the window interactive and running indefinitely
window.mainloop()