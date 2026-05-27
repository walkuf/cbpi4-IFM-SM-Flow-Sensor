# cbpi4-IFM-SM-Flow-Sensor

CraftBeerPi4 plugin for the **IFM SM6004** (and compatible SM series) magnetic-inductive flow sensors,
read via an **ADS1115 ADC** over bit-bang I2C. Includes a companion **volume-steps plugin** that turns
raw flow data into automated, volume-controlled brewing transfers.

---

## Hardware

- **Sensor**: IFM SM6004 (4–20 mA current loop output)
- **ADC**: ADS1115 at I2C address 0x48
- **Shunt**: 150 Ω per channel (4 mA → 0.6 V, 20 mA → 3.0 V)
- **Wiring**: Bit-bang I2C — avoids conflict with hardware I2C pins; set `SCL_PIN` / `SDA_PIN` in plugin config
- **Range**: Sensor hard-wired to 0–3.0 G/min at 0–20 mA. Set sensor `MaxVal = 3.0` in CBPI4 to match.

Multiple sensors share a single ADS1115 instance (channels 0–3). A shared bus singleton is
initialized on first use and reused by all sensor instances, so only one I2C init happens regardless
of how many sensors are configured.

---

## Installation

```bash
cd ~/cbpi4_env
source bin/activate
pip install -e /path/to/cbpi4-IFM-SM-Flow-Sensor
```

Or install directly into the CBPI4 plugin folder:

```bash
cp -r cbpi4_IFM_SM_Flow_Sensor ~/config/plugins/
```

---

## Sensor Configuration (CBPI4)

| Property | Value |
|---|---|
| Channel | ADS1115 channel (0–3) |
| MinVal | 0 |
| MaxVal | **3.0** (matches SM6004 hardware 20 mA calibration — do not change) |

> **Important**: MaxVal must match the physical sensor range. Changing it breaks all volume math downstream.

---

## Companion Plugin: `cbpi4_volume_steps`

The flow sensor alone gives you a G/min reading. The companion plugin turns that into **volume-controlled
automated transfers** with per-path calibration, virtual kettle gauges, pump priming, and fly-sparge support.

### Components

#### KettleVolumeSensor
Virtual sensor that tracks estimated volume (gallons) in a kettle. Updated in real-time by transfer steps.

- Persists volume to disk across restarts
- Dashboard actions: **Set Volume**, **Reset to Initial**, **Reset to Zero**
- Optional **WarningVolume** threshold fires a UI notification when kettle approaches full

#### VolumeTransferStep
Runs an actor group (pump + valves) until a target volume has passed through the flow sensor, then
advances the recipe automatically.

**Properties**:
| Property | Description |
|---|---|
| FlowSensor | IFM flow sensor sensor ID |
| ActorGroup | Pump + valve group to activate |
| TargetVolume | Gallons to transfer |
| LowFlowCutoff | G/min below which transfer is considered done (kettle ran dry) |
| MinRunTime | Seconds before low-flow cutoff becomes active |
| SourceVolumeSensor | KettleVolumeSensor that loses volume (optional) |
| DestVolumeSensor | KettleVolumeSensor that gains volume (optional) |
| PreOpenDelay | Seconds after actor ON before counting flow (lets slow valves open) |
| ResetSourceOnEmpty | Set source volume to 0 when low-flow cutoff fires |

#### FlySpargeTransferStep
Runs two pump groups simultaneously — HLT water into mash (P1) and wort out of mash into boil (P2) —
stopping each group independently when its volume target is reached.

**Properties**: Two sets of FlowSensor/ActorGroup/TargetVolume (group 1 and 2) plus three
KettleVolumeSensor references (HLT, Mash, Boil) and shared LowFlowCutoff/MinRunTime.

#### PrimeStep
Primes a pump before a transfer to eliminate air locks:
1. Opens all safe valves (no risk of routing to mash)
2. Briefly flashes a purge valve (e.g. V6) for a few seconds to clear that line, then closes it
3. Cycles the pump in short bursts through the safe path

**Properties**: PumpActor, ValveGroup (safe valves), BriefValve (purge valve), BriefSeconds,
BurstSeconds, BurstCount, PauseBetween.

#### TimedActorStep
Turns an actor group ON for N seconds, then advances the recipe. Useful for:
- Mash burp: open recirculation valves briefly before pump starts to purge trapped air
- Any timed valve or pump operation between recipe steps

#### ActorOffStep
Turns an actor OFF and immediately advances to the next step. Useful for stopping a manually-started
actor (e.g. mash recirculation) before a fly sparge.

#### ResetVolumesActor
Dashboard button actor. When activated, resets all KettleVolumeSensors to their configured
InitialVolume. Useful at brew-day start after filling kettles to known volumes.

---

## Per-Path Calibration

Each transfer path through the brewery has different geometry (tubing length, valve types, elevation
changes), so a single flow sensor correction factor is not enough. This plugin uses **per-path
calibration factors** stored in CBPI4's native Settings page.

### How it works

Six `CAL_*` config keys are registered at startup and editable at **Settings → CAL_BOIL_TO_MASH** etc.:

| Key | Path | Notes |
|---|---|---|
| CAL_BOIL_TO_MASH | Boil kettle → Mash tun | Mash-in step |
| CAL_BOIL_TO_HLT | Boil kettle → HLT | Prime/move steps |
| CAL_BOIL_TO_HLT_REMAINDER | Boil → HLT (run-to-empty) | Mapped when step name contains "remaining" |
| CAL_HLT_TO_MASH | HLT → Mash | Fly sparge water in |
| CAL_MASH_TO_BOIL | Mash → Boil | Fly sparge wort out |
| CAL_BOIL_TO_FERM | Boil → Fermenter | Post-boil transfer |

Step names are matched automatically to the correct factor. The factor is applied to the flow sensor
reading before accumulation:

```
accumulated += flow_rate × cal_factor × poll_interval
```

Factor > 1.0 means the sensor under-reads (actual > sensor). Factor < 1.0 means the sensor over-reads.

### Calibrating a path

1. Set all `CAL_*` factors to **1.0** in CBPI4 Settings to get raw sensor readings.
2. Run the transfer. Note the CBPI4-accumulated volume and the actual physical volume (dipstick/measuring cup).
3. Compute: `new_factor = actual_volume / cbpi_accumulated_volume`
4. Enter the new factor in CBPI4 Settings. No restart required.

### Example calibration results (Placitas Brewery, IFM SM6004 + pump inversion 2026-05-26)

| Path | Sensor (G) | Actual (G) | Factor |
|---|---|---|---|
| BOIL_TO_MASH | 8.716 | 8.7 | 0.998 |
| BOIL_TO_HLT_REMAINDER | 7.557 | 8.4 | 1.112 |
| MASH_TO_BOIL | 11.977 raw × 1.1541 display | 12.4 | 0.897 |

> **Note**: If your setup uses a hot-wort flow correction in `flow_cal.json` (for the fly sparge
> Mash→Boil path), the `CAL_MASH_TO_BOIL` factor must absorb both the sensor geometry error **and**
> any error in the hot-wort factor: `new_cal = actual / (sensor × flow_cal_factor)`.

---

## Step Name → Calibration Key Mapping

Steps are matched by their name (case-insensitive substring):

| Step name contains | Mapped to |
|---|---|
| "remaining" + "hlt" | CAL_BOIL_TO_HLT_REMAINDER |
| "hlt" + ("boil" or "move") | CAL_BOIL_TO_HLT |
| "mash in" or "strike" | CAL_BOIL_TO_MASH |
| "mash out" or "mash to boil" | CAL_MASH_TO_BOIL |
| "sparge" or "hlt to mash" | CAL_HLT_TO_MASH |
| "ferm" | CAL_BOIL_TO_FERM |

You can also pass the key directly (e.g. step name = `"BOIL_TO_MASH"`) for an exact lookup.

---

## Installing the Volume Steps Plugin

```bash
cp -r cbpi4_volume_steps ~/config/plugins/
```

The plugin self-registers all `CAL_*` config keys at CBPI4 startup. If a key already exists (user has
set a value), it is left unchanged — it will not be reset to 1.0 on restart.

---

## Repository Layout

```
cbpi4_IFM_SM_Flow_Sensor/   # Flow sensor plugin (install this for raw G/min readings)
cbpi4_volume_steps/          # Companion plugin (volume transfers, priming, calibration)
```

---

## License

GPL-3.0
