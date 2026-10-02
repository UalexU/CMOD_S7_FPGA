# CMOD S7-25 — TMAG5170 Hall + MAX31865 RTD

| Folder | What it is | Open with |
|---|---|---|
| `hw/` | Vivado project (MicroBlaze block design, constraints) | Vivado → open `hw/CMOD_S7 FPGA.xpr` |
| `xsa/` | Exported hardware (`design_1_wrapper.xsa`) — the hand-off from Vivado to Vitis | — |
| `fw/` | Vitis workspace: `CMOD` platform + apps `SPI_BOTH_HALL_TEMP`, `SPI_Hall_Temp`, `SPI_Hull_Sensor` | Vitis → Open Workspace → `fw/` |
| `host/` | PC-side Python: `tmag_gui.py`, `tmag_scope.py`, `sensor.py`, `run_board.py`; `scope_v2/` is the PySide6 variant | Python |

## Workflow
1. Change hardware in Vivado → File → Export → Export Hardware (include bitstream) → save to `xsa/design_1_wrapper.xsa`.
2. In Vitis, update the `CMOD` platform from that .xsa, rebuild the platform, then rebuild the app.
3. `python host/run_board.py` programs the board with the newest app build in `fw/`.

## Python setup (once)
```
cd host
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python tmag_gui.py
```
