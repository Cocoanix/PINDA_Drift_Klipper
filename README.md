# PINDA Drift for Klipper

**Keep your first layer consistent as your PINDA probe warms up.**

`Cocoanix/PINDA_Drift_Klipper` is a Klipper host extension for a switching inductive probe with a readable thermistor. It interpolates a measured drift curve and corrects the reported bed height at each probe sample. On supported Klipper hosts, the correction feeds into Z homing through `probe:z_virtual_endstop`, Z tilt and bed meshing. The optional warmup step waits for the probe to settle before final homing.

[Compatibility](#compatibility) · [Install](#install) · [Calibrate](#calibrate) · [Start print](#add-warmup-to-start_print) · [Commands](#commands)

## Compatibility

- Use a working Klipper `[probe]` and a readable `[temperature_sensor]` for the probe thermistor. Prusa's PINDA V2 has a built-in thermistor. Check that the sensor is exposed to Klipper before installing. [Prusa's PINDA guide](https://help.prusa3d.com/article/p-i-n-d-a-superpinda-sensor-testing_2091)
- Tested on Klipper host `v0.13.0-740-g60fc7aa67`. This extension requires the probe result event and probe-based Z homing interfaces present in that revision. Older host revisions that lack those interfaces are rejected at startup. [Klipper probe source](https://github.com/Klipper3d/klipper/blob/60fc7aa67a8da9abb43a2bad825d4992294ebf3f/klippy/extras/probe.py) · [Klipper homing source](https://github.com/Klipper3d/klipper/blob/60fc7aa67a8da9abb43a2bad825d4992294ebf3f/klippy/extras/homing.py)

## Install

From the downloaded or cloned project folder **on the Klipper host**, run the following. Adjust the destination paths if your installation uses different directories.

```sh
cp pinda_temperature.py ~/klipper/klippy/extras/
cp examples/pinda_temperature.cfg ~/printer_data/config/pinda_temperature.cfg
```

Set `sensor` in the copied file to your full temperature sensor section name, such as `temperature_sensor probe_temp`. Add the include to `printer.cfg`, above the `SAVE_CONFIG` block.

```ini
[include pinda_temperature.cfg]
```

Restart the Klipper host service while the printer is idle. On a systemd installation, use `sudo systemctl restart klipper`. Run `PINDA_TEMP_STATUS` in the console and check that the reading is plausible. Both example profiles start disabled so you can collect your own measurements first.

## Example values

The measured example came from **Cocoanix Printer One**, with the nozzle held at **140°C** during probing. The controller used `PA0` for an `EPCOS 100K B57560G104F` thermistor input. The final base `[probe] z_offset` was **2.300 mm** after a **+0.05 mm** first-layer lift on a PLA test at **220°C / 60°C**.

<details>
<summary>Sensor and probe settings from the example printer</summary>

These pin assignments and offsets describe the example hardware. Use the settings for your own controller and probe mount when installing.

```ini
[temperature_sensor probe_temp]
sensor_type: EPCOS 100K B57560G104F
sensor_pin: PA0

[probe]
pin: ^PA1
x_offset: -22
y_offset: -22
z_offset: 2.300
speed: 5.0
```

</details>

| Bed target | Probe at trigger | Trigger-to-paper distance | Drift from reference |
| ---: | ---: | ---: | ---: |
| 60°C | 34.825876°C | 2.350 mm | 0.000 mm |
| 75°C | 37.643262°C | 2.600 mm | +0.250 mm |
| 90°C | 42.095369°C | 3.100 mm | +0.750 mm |

The [measured config](examples/pinda_temperature.cfg) uses linear interpolation. The [Prusa factory example](examples/prusa_factory_preset.cfg) preserves the separate 35–60°C table that the module uses by default, with cubic interpolation. Include one profile and treat its values as examples for your own calibration. [Prusa firmware source](https://github.com/prusa3d/Prusa-Firmware/blob/MK3/Firmware/Marlin_main.cpp#L6864-L6875)

On Cocoanix Printer One, warming and cooling measurements around 28.7°C differed by **0.150 mm**. After warming up, ten consecutive probes spanned **0.015 mm**. The measured example uses the settled points so the first layer depends less on how recently the printer was heated.

## Calibrate

1. Leave compensation disabled. Fit the sheet you print on, clean the nozzle and home using your normal routine. Clear the active mesh if your printer uses one.
2. For each condition you want to cover, heat the bed and hold the nozzle at the same preheat temperature you intend to use during print startup. Let the probe settle at a repeatable position near the bed. Lift Z before moving to the calibration spot. Use the same area of the sheet for every measurement.
3. Run `PINDA_TEMP_CALIBRATE`. It takes an uncompensated probe sample, lifts the toolhead and moves the nozzle over the probe spot. Use small `TESTZ` steps to reach the same paper drag at each temperature, then run `ACCEPT` to record the point or `ABORT` to discard it. [Klipper's paper test workflow](https://www.klipper3d.org/Probe_Calibrate.html#calibrating-probe-z-offset)
4. Measure at least two distinct probe temperatures spanning your normal range. Review `PINDA_TEMP_POINTS`, which prints a candidate table and its matching base `z_offset`. Copy the table and reference temperature into your profile and replace your existing `[probe] z_offset` with the matching value. Record the results before restarting, since the collected points are held in memory. Each row is **probe temperature in °C, then trigger-distance drift in mm** relative to the reference.
5. Set `enabled: True` and run `RESTART`. Home at a calibrated temperature, regenerate the mesh if used and verify a first layer. Apply any final first-layer trim to the base probe offset through Klipper's normal Z offset workflow. [Klipper probe commands](https://www.klipper3d.org/G-Codes.html#probe)

## Add warmup to `START_PRINT`

Insert the conditional block below **after your bed and nozzle preheat waits** and just before your existing final `G28`. The example parks over the calibration area on Cocoanix Printer One. Choose reachable XY coordinates and a safe park height for **your nozzle and probe**, including after an uncompensated home. Keep your existing Z tilt and meshing steps after final homing.

```ini
    {% if printer["pinda_temperature"].enabled %}
        PINDA_TEMP_BOOTSTRAP_HOME
        G90
        G1 Z10 F300
        G1 X110 Y117 F6000
        G1 Z2.5 F300
        PINDA_TEMP_STABILIZE
    {% endif %}
    G28
```

The preliminary home establishes enough positioning to park near the bed. The final `G28` establishes compensated Z. On the measured example the wait requires at least **180 seconds**, both heaters near their targets and a probe temperature inside the allowed range with at most **0.3°C** of variation over **60 seconds**. The wait stops after **900 seconds** if those conditions cannot be met.

Remove any older PINDA drift macro or offset calculation from the same print-start path to avoid applying the correction twice.

## Commands

| Command | What it does |
| --- | --- |
| `PINDA_TEMP_STATUS` | Shows the current probe temperature, predicted correction and last result. Outside the curve it shows a preview using the nearest endpoint. |
| `PINDA_TEMP_CALIBRATE` | Starts an uncompensated sample and manual paper check. Finish with `ACCEPT` or `ABORT`. |
| `PINDA_TEMP_POINTS` | Lists session measurements and suggests a reference, drift table and base probe offset. |
| `PINDA_TEMP_BOOTSTRAP_HOME` | Runs a preliminary `G28` with drift correction suspended for that home. |
| `PINDA_TEMP_STABILIZE` | Waits at the current parked position for temperatures to settle. |
| `PINDA_TEMP_SET ENABLE=0` or `ENABLE=1` | Changes compensation until restart. Change it while idle with Z unhomed, then rehome and rebuild the mesh. |

Normal probing uses `out_of_range: error` by default. `range_margin` permits a small extension at either end using the nearest endpoint, and `max_adjustment` rejects an excessive correction. If warmup times out, use `PINDA_TEMP_STATUS` to check which probe temperature your printer reaches and calibrate that condition if it falls outside the measured range.

To uninstall, remove the warmup block and config include, restore your previous base probe offset, remove the Python module from `klippy/extras` and restart the host service.

## Tests and license

Point the tests at a Klipper checkout of the revision above. Run them with a Python environment that has Klipper's dependencies installed to include the real heater wrapper test.

```sh
KLIPPER_DIR="$HOME/klipper" python -m unittest discover -s tests -v
```

[GNU GPL v3](LICENSE).
