import logging
import asyncio
import json
import os
from datetime import datetime
from cbpi.api import *
from cbpi.api.dataclasses import NotificationType

logger = logging.getLogger(__name__)

CAL_FILE = "/home/pi/config/flow_cal.json"

def load_cal_factor(key):
    try:
        with open(CAL_FILE) as f:
            cal = json.load(f)
        return cal.get(key, {}).get("factor", 1.0)
    except:
        return 1.0


def _read_sensor(cbpi, sensor_id):
    try:
        if sensor_id is None:
            return None
        return cbpi.sensor.get_sensor_value(sensor_id)
    except Exception as e:
        logger.error(f"Error reading sensor {sensor_id}: {e}")
        return None


CORRECTIONS_PATH = "/home/pi/config/calibration/step_corrections.json"

# CBPI4 global config keys for calibration factors — editable in Settings UI
CAL_CONFIG_KEYS = [
    "CAL_BOIL_TO_MASH",
    "CAL_BOIL_TO_HLT",
    "CAL_BOIL_TO_HLT_REMAINDER",
    "CAL_HLT_TO_MASH",
    "CAL_MASH_TO_BOIL",
    "CAL_BOIL_TO_FERM",
]

def _get_cal(cbpi, key):
    try:
        val = cbpi.config.get(key, default=1.0)
        return float(val) if val not in (None, "") else 1.0
    except Exception:
        return 1.0

def _get_step_correction(cbpi, step_name):
    try:
        n = (step_name or "").lower()
        # Direct key lookup: e.g. "HLT_TO_MASH" or "CAL_HLT_TO_MASH"
        upper = step_name.upper() if step_name else ""
        if upper in ("HLT_TO_MASH", "MASH_TO_BOIL", "BOIL_TO_MASH",
                     "BOIL_TO_HLT", "BOIL_TO_HLT_REMAINDER", "BOIL_TO_FERM"):
            return _get_cal(cbpi, f"CAL_{upper}")
        if upper in CAL_CONFIG_KEYS:
            return _get_cal(cbpi, upper)
        # Step name matching
        if "remaining" in n and "hlt" in n:
            return _get_cal(cbpi, "CAL_BOIL_TO_HLT_REMAINDER")
        if "hlt" in n and ("boil" in n or "move" in n):
            return _get_cal(cbpi, "CAL_BOIL_TO_HLT")
        if "mash in" in n or "strike" in n:
            return _get_cal(cbpi, "CAL_BOIL_TO_MASH")
        if "mash out" in n or "mash to boil" in n:
            return _get_cal(cbpi, "CAL_MASH_TO_BOIL")
        if "sparge" in n or "hlt to mash" in n:
            return _get_cal(cbpi, "CAL_HLT_TO_MASH")
        if "ferm" in n:
            return _get_cal(cbpi, "CAL_BOIL_TO_FERM")
        return 1.0
    except Exception as e:
        logger.warning(f"step correction read error: {e}")
        return 1.0


# ---------------------------------------------------------------------------
# Shared kettle volume state — updated by steps, read by KettleVolumeSensor
# ---------------------------------------------------------------------------
VOLUME_STATE_FILE = "/home/pi/config/calibration/volume_state.json"

def _load_volume_state():
    try:
        with open(VOLUME_STATE_FILE) as f:
            return {k: float(v) for k, v in json.load(f).items()}
    except Exception:
        return {}

def _save_volume_state():
    try:
        tmp = VOLUME_STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({k: round(v, 3) for k, v in _kettle_volume_state.items()}, f, indent=2)
        os.replace(tmp, VOLUME_STATE_FILE)
    except Exception as e:
        logger.error(f"volume state save failed: {e}")

_kettle_volume_state = {}    # sensor_id -> float (gallons), live values
_kettle_volume_override = {} # sensor_id -> float; set by manual action; steps rebase when seen
_kettle_initial_volume = {}  # sensor_id -> float; InitialVolume from props, populated at sensor start

def _set_kettle_volume(sensor_id, gallons):
    if sensor_id:
        v = max(0.0, float(gallons))
        _kettle_volume_state[sensor_id] = v
        _kettle_volume_override[sensor_id] = v  # signal running steps to rebase
        _save_volume_state()

def _add_kettle_volume(sensor_id, delta_gallons):
    if sensor_id:
        current = _kettle_volume_state.get(sensor_id, 0.0)
        _kettle_volume_state[sensor_id] = max(0.0, current + float(delta_gallons))


# ---------------------------------------------------------------------------
# KettleVolumeSensor — virtual sensor tracking estimated volume (gallons)
# ---------------------------------------------------------------------------
@parameters([
    Property.Number(label="InitialVolume", configurable=True, default_value=0,
                    description="Starting volume in gallons (can be overridden with Set Volume action)"),
    Property.Number(label="MaxVolume", configurable=True, default_value=15,
                    description="Maximum kettle capacity in gallons (reference only)"),
    Property.Number(label="WarningVolume", configurable=True, default_value=0,
                    description="Fire a UI warning when volume reaches or exceeds this value (0 = disabled)"),
])
class KettleVolumeSensor(CBPiSensor):
    """Virtual sensor tracking estimated liquid volume (gallons).
    Updated in real-time by VolumeTransferStep and FlySpargeTransferStep.
    Use 'Set Volume' action to initialize at brew-day start."""

    async def run(self):
        sensor_id = self.id
        initial = float(self.props.get("InitialVolume", 0))
        _kettle_initial_volume[sensor_id] = initial
        if sensor_id not in _kettle_volume_state:
            persisted = _load_volume_state()
            if sensor_id in persisted:
                _kettle_volume_state[sensor_id] = persisted[sensor_id]
                logger.info(f"KettleVolumeSensor {sensor_id}: restored {persisted[sensor_id]:.2f} gal from disk")
            else:
                _kettle_volume_state[sensor_id] = initial
        warning_vol = float(self.props.get("WarningVolume") or 0)
        # Start warned=True if already at/above threshold so no spurious startup alert
        warned = warning_vol > 0 and _kettle_volume_state.get(sensor_id, 0.0) >= warning_vol
        tick = 0
        while True:
            try:
                self.value = round(_kettle_volume_state.get(sensor_id, 0.0), 2)
                self.push_update(self.value)
                tick += 1
                if tick % 60 == 0:
                    _save_volume_state()
                if warning_vol > 0:
                    if self.value >= warning_vol and not warned:
                        try:
                            self.cbpi.notify(
                                "Kettle Near Full",
                                f"{sensor_id}: {self.value:.1f} gal",
                                NotificationType.WARNING
                            )
                        except Exception:
                            pass
                        warned = True
                    elif self.value < warning_vol:
                        warned = False
            except Exception as loop_err:
                logger.error(f"KettleVolumeSensor {sensor_id} loop error: {loop_err}")
            await asyncio.sleep(2)

    @action("Set Volume", parameters=[
        Property.Number(label="Gallons", configurable=True, default_value=0,
                        description="Set current volume to this value (gallons)")
    ])
    async def set_volume_action(self, Gallons=0, **kwargs):
        _set_kettle_volume(self.id, float(Gallons))
        logger.info(f"KettleVolumeSensor {self.id}: manually set to {float(Gallons):.2f} gal")

    @action("Reset to Initial Volume", parameters=[])
    async def reset_to_initial_action(self, **kwargs):
        initial = float(self.props.get("InitialVolume", 0))
        _set_kettle_volume(self.id, initial)
        logger.info(f"KettleVolumeSensor {self.id}: reset to initial {initial:.2f} gal")

    @action("Reset to Zero", parameters=[])
    async def reset_volume_action(self, **kwargs):
        _set_kettle_volume(self.id, 0.0)
        logger.info(f"KettleVolumeSensor {self.id}: reset to 0")

    def get_state(self):
        return dict(value=getattr(self, "value", 0))


# ---------------------------------------------------------------------------
# Step 1 – Volume Transfer
# ---------------------------------------------------------------------------
@parameters([
    Property.Sensor(label="FlowSensor",
                    description="Flow sensor to monitor (G/min)"),
    Property.Actor(label="ActorGroup",
                   description="Actor group to run during transfer"),
    Property.Number(label="TargetVolume", configurable=True, default_value=5,
                    description="Volume setpoint in gallons to stop transfer"),
    Property.Select(label="LowFlowCutoff", options=["0.05", "0.10", "0.15", "0.20"],
                    description="Flow rate (G/min) below which transfer is considered done"),
    Property.Number(label="MinRunTime", configurable=True, default_value=60,
                    description="Seconds before low-flow cutoff becomes active"),
    Property.Sensor(label="SourceVolumeSensor",
                    description="KettleVolumeSensor that loses volume (optional)"),
    Property.Sensor(label="DestVolumeSensor",
                    description="KettleVolumeSensor that gains volume (optional)"),
    Property.Number(label="PreOpenDelay", configurable=True, default_value=0,
                    description="Seconds to wait after actor ON before counting flow (lets stuck valves open fully)"),
    Property.Select(label="ResetSourceOnEmpty", options=["No", "Yes"],
                    description="Set source vessel volume to 0 when low-flow cutoff triggers (kettle ran dry)"),
])
class VolumeTransferStep(CBPiStep):

    async def on_timer_done(self, timer):
        pass

    async def run(self):
        target_volume   = float(self.props.get("TargetVolume", 5))
        low_flow_cutoff = float(self.props.get("LowFlowCutoff") or 0)
        min_run_time    = float(self.props.get("MinRunTime") or 60)
        flow_sensor_id  = self.props.get("FlowSensor")
        actor_id        = self.props.get("ActorGroup")
        src_vol_id      = self.props.get("SourceVolumeSensor")
        dst_vol_id      = self.props.get("DestVolumeSensor")
        step_correction = _get_step_correction(self.cbpi, self.name)
        if step_correction != 1.0:
            logger.info(f"VolumeTransfer: applying step correction {step_correction} for '{self.name}'")

        initial_src = _kettle_volume_state.get(src_vol_id, 0.0) if src_vol_id else 0.0
        initial_dst = _kettle_volume_state.get(dst_vol_id, 0.0) if dst_vol_id else 0.0

        # Clear any pending overrides so we start from current state
        _kettle_volume_override.pop(src_vol_id, None)
        _kettle_volume_override.pop(dst_vol_id, None)

        pre_open_delay  = float(self.props.get("PreOpenDelay") or 0)
        accumulated_volume = 0.0
        elapsed = 0.0
        stop_reason = None

        if actor_id:
            await self.actor_on(actor_id)
            logger.info(f"VolumeTransfer: actor {actor_id} ON")

        if pre_open_delay > 0:
            logger.info(f"VolumeTransfer: waiting {pre_open_delay:.0f}s for valve to open")
            await asyncio.sleep(pre_open_delay)

        await self.push_update()

        POLL_INTERVAL = 2.0  # seconds between flow samples
        while self.running:
            await asyncio.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL

            flow_rate = _read_sensor(self.cbpi, flow_sensor_id)
            if flow_rate is None:
                flow_rate = 0.0

            accumulated_volume += (flow_rate * step_correction) * (POLL_INTERVAL / 60.0)

            # Rebase if a manual Set Volume action fired during the transfer
            if src_vol_id and src_vol_id in _kettle_volume_override:
                override_val = _kettle_volume_override.pop(src_vol_id)
                initial_src = override_val + accumulated_volume
                logger.info(f"VolumeTransfer: src rebased to {initial_src:.2f} (override={override_val:.2f})")
            if dst_vol_id and dst_vol_id in _kettle_volume_override:
                override_val = _kettle_volume_override.pop(dst_vol_id)
                initial_dst = override_val - accumulated_volume
                logger.info(f"VolumeTransfer: dst rebased to {initial_dst:.2f} (override={override_val:.2f})")

            if src_vol_id:
                _kettle_volume_state[src_vol_id] = max(0.0, initial_src - accumulated_volume)
            if dst_vol_id:
                _kettle_volume_state[dst_vol_id] = initial_dst + accumulated_volume

            logger.info(f"VolumeTransfer '{self.name}': elapsed={elapsed:.0f}s "
                        f"flow={flow_rate:.3f} G/min "
                        f"accumulated={accumulated_volume:.3f}/{target_volume:.2f} gal")

            if accumulated_volume >= target_volume:
                stop_reason = f"Target volume reached: {accumulated_volume:.2f} gal"
                break

            if low_flow_cutoff > 0 and elapsed >= min_run_time and flow_rate <= low_flow_cutoff:
                stop_reason = (f"Low flow detected: {flow_rate:.3f} G/min "
                               f"after {elapsed:.0f}s")
                if self.props.get("ResetSourceOnEmpty") == "Yes" and src_vol_id:
                    _kettle_volume_state[src_vol_id] = 0.0
                    logger.info(f"VolumeTransfer: source {src_vol_id} reset to 0 (ran empty)")
                break

        if actor_id:
            await self.actor_off(actor_id)
            logger.info(f"VolumeTransfer: actor {actor_id} OFF")
            await asyncio.sleep(3)

        if getattr(self, '_finished_once', False):
            logger.warning("VolumeTransfer: finish called twice, ignoring")
            return
        self._finished_once = True

        if stop_reason:
            logger.info(f"VolumeTransfer complete: {stop_reason}")
            await self.push_update()
            self.summary = (f"Transfer complete — {stop_reason}\n"
                            f"Total volume: {accumulated_volume:.2f} gal")

        try:
            await self.next()
        except Exception as e:
            logger.error(f"VolumeTransfer: next() failed: {e}")

    async def reset(self):
        pass


# ---------------------------------------------------------------------------
# Step 2 – Fly Sparge Transfer
# ---------------------------------------------------------------------------
@parameters([
    Property.Sensor(label="FlowSensor1",
                    description="Flow sensor for HLT group (G/min)"),
    Property.Actor(label="ActorGroup1",
                   description="HLT actor group (sparge water in)"),
    Property.Number(label="TargetVolume1", configurable=True, default_value=7,
                    description="Volume setpoint in gallons to stop HLT group"),
    Property.Sensor(label="FlowSensor2",
                    description="Flow sensor for Boil/lauter group (G/min)"),
    Property.Actor(label="ActorGroup2",
                   description="Boil/lauter actor group (wort out)"),
    Property.Number(label="TargetVolume2", configurable=True, default_value=7,
                    description="Volume setpoint in gallons to stop Boil/lauter group"),
    Property.Sensor(label="HLTVolumeSensor",
                    description="HLT KettleVolumeSensor (loses volume to Mash, optional)"),
    Property.Sensor(label="MashVolumeSensor",
                    description="Mash KettleVolumeSensor (gains from HLT, loses to Boil, optional)"),
    Property.Sensor(label="BoilVolumeSensor",
                    description="Boil KettleVolumeSensor (gains from Mash, optional)"),
    Property.Number(label="PreOpenDelay", configurable=True, default_value=0,
                    description="Seconds to wait after actors ON before counting flow (lets stuck valves open fully)"),
    Property.Select(label="LowFlowCutoff", options=["0", "0.05", "0.10", "0.15", "0.20"],
                    description="Flow rate (G/min) below which a group is considered done (0 = disabled, volume target only)"),
    Property.Number(label="MinRunTime", configurable=True, default_value=60,
                    description="Seconds before low-flow cutoff becomes active for each group"),
])
class FlySpargeTransferStep(CBPiStep):

    async def on_timer_done(self, timer):
        pass

    async def run(self):
        target1      = float(self.props.get("TargetVolume1", 7))
        target2_raw  = float(self.props.get("TargetVolume2", 7))
        cal_factor2  = load_cal_factor("MASH_TO_BOIL")
        target2      = target2_raw / cal_factor2
        logger.warning(f"FlySpargeTransfer: MASH_TO_BOIL cal factor={cal_factor2:.4f} "
                       f"target={target2_raw:.2f} gal adjusted to {target2:.2f} gal sensor units")
        sensor1_id  = self.props.get("FlowSensor1")
        sensor2_id  = self.props.get("FlowSensor2")
        actor1_id   = self.props.get("ActorGroup1")
        actor2_id   = self.props.get("ActorGroup2")

        hlt_vol_id  = self.props.get("HLTVolumeSensor")
        mash_vol_id = self.props.get("MashVolumeSensor")
        boil_vol_id = self.props.get("BoilVolumeSensor")
        initial_hlt  = _kettle_volume_state.get(hlt_vol_id,  0.0) if hlt_vol_id  else 0.0
        initial_mash = _kettle_volume_state.get(mash_vol_id, 0.0) if mash_vol_id else 0.0
        initial_boil = _kettle_volume_state.get(boil_vol_id, 0.0) if boil_vol_id else 0.0

        # Clear any pending overrides so we start from current state
        for _vid in [hlt_vol_id, mash_vol_id, boil_vol_id]:
            _kettle_volume_override.pop(_vid, None)

        low_flow_cutoff = float(self.props.get("LowFlowCutoff") or 0)
        min_run_time    = float(self.props.get("MinRunTime") or 60)
        volume1 = 0.0
        volume2 = 0.0
        elapsed = 0.0
        reason1 = "stopped manually"
        reason2 = "stopped manually"
        step_correction_in  = _get_step_correction(self.cbpi, "HLT_TO_MASH")
        step_correction_out = _get_step_correction(self.cbpi, "MASH_TO_BOIL")
        logger.info(f"FlySparge corrections: in={step_correction_in} out={step_correction_out}")
        group1_done = False
        group2_done = False

        pre_open_delay = float(self.props.get("PreOpenDelay") or 0)

        if actor1_id:
            await self.actor_on(actor1_id)
            logger.info(f"FlySpargeTransfer: actor1 {actor1_id} ON")
        if actor2_id:
            await self.actor_on(actor2_id)
            logger.info(f"FlySpargeTransfer: actor2 {actor2_id} ON")

        if pre_open_delay > 0:
            logger.info(f"FlySpargeTransfer: waiting {pre_open_delay:.0f}s for valves to open")
            await asyncio.sleep(pre_open_delay)

        await self.push_update()

        POLL_INTERVAL = 2.0  # seconds between flow samples
        while self.running:
            await asyncio.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL

            if not group1_done:
                flow1 = _read_sensor(self.cbpi, sensor1_id) or 0.0
                volume1 += (flow1 * step_correction_in) * (POLL_INTERVAL / 60.0)
                logger.debug(f"FlySpargeTransfer: HLT flow={flow1:.3f} "
                             f"vol={volume1:.3f} gal")
                low_flow1 = (low_flow_cutoff > 0 and elapsed >= min_run_time
                             and flow1 <= low_flow_cutoff)
                if volume1 >= target1 or low_flow1:
                    if actor1_id:
                        await self.actor_off(actor1_id)
                    reason1 = (f"low flow {flow1:.3f} G/min after {elapsed:.0f}s"
                               if low_flow1 else f"target reached {volume1:.2f} gal")
                    logger.info(f"FlySpargeTransfer: actor1 {actor1_id} OFF — {reason1}")
                    group1_done = True
                    logger.info("notify: Fly Sparge — HLT Complete | " +
                                f"HLT group stopped at {volume1:.2f} gal ({reason1}).")

            if not group2_done:
                flow2 = _read_sensor(self.cbpi, sensor2_id) or 0.0
                volume2 += (flow2 * step_correction_out) * (POLL_INTERVAL / 60.0)
                logger.debug(f"FlySpargeTransfer: Lauter flow={flow2:.3f} "
                             f"vol={volume2:.3f} gal")
                volume2_display = round(volume2 * cal_factor2, 3)
                low_flow2 = (low_flow_cutoff > 0 and elapsed >= min_run_time
                             and flow2 <= low_flow_cutoff)
                if volume2 >= target2 or low_flow2:
                    if actor2_id:
                        await self.actor_off(actor2_id)
                    reason2 = (f"low flow {flow2:.3f} G/min after {elapsed:.0f}s"
                               if low_flow2 else f"target reached {volume2:.2f} gal")
                    logger.info(f"FlySpargeTransfer: actor2 {actor2_id} OFF — {reason2}")
                    group2_done = True
                    logger.info("notify: Fly Sparge — Lauter Complete | " +
                                f"Lauter group stopped at {volume2:.2f} gal ({reason2}).")

            # Rebase if a manual Set Volume action fired during the transfer
            if hlt_vol_id and hlt_vol_id in _kettle_volume_override:
                override_val = _kettle_volume_override.pop(hlt_vol_id)
                initial_hlt = override_val + volume1
                logger.info(f"FlySpargeTransfer: HLT rebased to {initial_hlt:.2f} (override={override_val:.2f})")
            if mash_vol_id and mash_vol_id in _kettle_volume_override:
                override_val = _kettle_volume_override.pop(mash_vol_id)
                initial_mash = override_val - volume1 + volume2
                logger.info(f"FlySpargeTransfer: Mash rebased to {initial_mash:.2f} (override={override_val:.2f})")
            if boil_vol_id and boil_vol_id in _kettle_volume_override:
                override_val = _kettle_volume_override.pop(boil_vol_id)
                initial_boil = override_val - (volume2 * cal_factor2)
                logger.info(f"FlySpargeTransfer: Boil rebased to {initial_boil:.2f} (override={override_val:.2f})")

            # Update kettle volumes in real-time
            if hlt_vol_id:
                _kettle_volume_state[hlt_vol_id] = max(0.0, initial_hlt - volume1)
            if mash_vol_id:
                # Mash gains from HLT (volume1) and loses to Boil (volume2 in sensor units)
                _kettle_volume_state[mash_vol_id] = max(0.0, initial_mash + volume1 - volume2)
            if boil_vol_id:
                # Use cal-adjusted volume for Boil display
                _kettle_volume_state[boil_vol_id] = initial_boil + (volume2 * cal_factor2)

            if group1_done and group2_done:
                break

        if actor1_id and not group1_done:
            await self.actor_off(actor1_id)
        if actor2_id and not group2_done:
            await self.actor_off(actor2_id)

        self.summary = (f"Fly Sparge complete!\n"
                        f"HLT: {volume1:.2f} gal ({reason1})\n"
                        f"Lauter: {volume2:.2f} gal ({reason2})")
        logger.info("notify: Fly Sparge Complete | " +
                    f"HLT: {volume1:.2f} gal ({reason1}). "
                    f"Lauter: {volume2:.2f} gal ({reason2}).")

        try:
            await self.next()
        except Exception as e:
            logger.error(f"VolumeTransfer: next() failed: {e}")

    async def reset(self):
        pass



def _safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _write_calibration_file(path, payload):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=2)


@parameters([
    Property.Sensor(label="FlowSensor",
                    description="Flow sensor to monitor (G/min)"),
    Property.Actor(label="ActorGroup",
                   description="Actor group to run during calibration"),
    Property.Number(label="FinalTargetVolume", configurable=True, default_value=19,
                    description="Final target volume in gallons"),
    Property.Number(label="Checkpoint1", configurable=True, default_value=5,
                    description="Checkpoint 1 in gallons"),
    Property.Number(label="Checkpoint2", configurable=True, default_value=10,
                    description="Checkpoint 2 in gallons"),
    Property.Number(label="Checkpoint3", configurable=True, default_value=15,
                    description="Checkpoint 3 in gallons"),
    Property.Select(label="LowFlowCutoff", options=["0.05", "0.10", "0.15", "0.20"],
                    description="Flow rate (G/min) below which transfer is considered done"),
    Property.Number(label="MinRunTime", configurable=True, default_value=60,
                    description="Seconds before low-flow cutoff becomes active"),
    Property.Select(label="CheckpointPauseSeconds", options=["0", "10", "15", "20", "30"],
                    description="Optional pause time at each checkpoint"),
    Property.Text(label="CalibrationFile", configurable=True,
                  default_value="/home/pi/config/calibration/placitas_pils_evolved_latest.json",
                  description="Where checkpoint calibration data will be written"),
    Property.Sensor(label="SourceVolumeSensor",
                    description="KettleVolumeSensor that loses volume (optional)"),
    Property.Sensor(label="DestVolumeSensor",
                    description="KettleVolumeSensor that gains volume (optional)"),
])
class CalibrationTransferStep(CBPiStep):

    async def on_timer_done(self, timer):
        pass

    async def run(self):
        flow_sensor_id   = self.props.get("FlowSensor")
        actor_id         = self.props.get("ActorGroup")
        final_target     = _safe_float(self.props.get("FinalTargetVolume", 19), 19.0)
        cp1              = _safe_float(self.props.get("Checkpoint1", 5), 5.0)
        cp2              = _safe_float(self.props.get("Checkpoint2", 10), 10.0)
        cp3              = _safe_float(self.props.get("Checkpoint3", 15), 15.0)
        low_flow_cutoff  = _safe_float(self.props.get("LowFlowCutoff", "0.10"), 0.10)
        min_run_time     = _safe_float(self.props.get("MinRunTime", 60), 60.0)
        pause_seconds    = int(_safe_float(self.props.get("CheckpointPauseSeconds", "0"), 0))
        calibration_file = self.props.get(
            "CalibrationFile",
            "/home/pi/config/calibration/placitas_pils_evolved_latest.json"
        )
        src_vol_id = self.props.get("SourceVolumeSensor")
        dst_vol_id = self.props.get("DestVolumeSensor")
        initial_src = _kettle_volume_state.get(src_vol_id, 0.0) if src_vol_id else 0.0
        initial_dst = _kettle_volume_state.get(dst_vol_id, 0.0) if dst_vol_id else 0.0

        checkpoints = sorted({x for x in [cp1, cp2, cp3] if x > 0 and x < final_target})

        accumulated_volume = 0.0
        elapsed = 0.0
        checkpoint_index = 0
        stop_reason = None

        data = {
            "started_utc": datetime.utcnow().isoformat() + "Z",
            "final_target_gal": final_target,
            "checkpoints": [],
            "final": None,
            "stop_reason": None
        }

        _write_calibration_file(calibration_file, data)

        if actor_id:
            await self.actor_on(actor_id)
            logger.info(f"CalibrationTransfer: actor {actor_id} ON")

        await self.push_update()

        POLL_INTERVAL = 2.0
        while self.running:
            await asyncio.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL

            flow_rate = _safe_float(_read_sensor(self.cbpi, flow_sensor_id), 0.0)
            accumulated_volume += flow_rate * (POLL_INTERVAL / 60.0)

            if src_vol_id:
                _kettle_volume_state[src_vol_id] = max(0.0, initial_src - accumulated_volume)
            if dst_vol_id:
                _kettle_volume_state[dst_vol_id] = initial_dst + accumulated_volume

            logger.debug(
                f"CalibrationTransfer: elapsed={elapsed:.0f}s "
                f"flow={flow_rate:.3f} G/min volume={accumulated_volume:.3f} gal"
            )

            while checkpoint_index < len(checkpoints) and accumulated_volume >= checkpoints[checkpoint_index]:
                target = checkpoints[checkpoint_index]
                event = {
                    "checkpoint_no": checkpoint_index + 1,
                    "target_gal": round(target, 3),
                    "cbpi_cumulative_gal": round(accumulated_volume, 3),
                    "flow_gpm": round(flow_rate, 3),
                    "elapsed_s": int(elapsed),
                    "utc": datetime.utcnow().isoformat() + "Z"
                }
                data["checkpoints"].append(event)
                _write_calibration_file(calibration_file, data)

                logger.info(
                    f"CalibrationTransfer checkpoint {checkpoint_index + 1}: "
                    f"target={target:.2f} cbpi={accumulated_volume:.2f}"
                )

                if actor_id and pause_seconds > 0:
                    await self.actor_off(actor_id)
                    logger.info(f"CalibrationTransfer: actor {actor_id} OFF for checkpoint pause")

                logger.info("notify: Calibration Checkpoint | " +
                            str(f"Target {target:.2f} gal reached. "
                                f"CBPi cumulative = {accumulated_volume:.2f} gal. "
                                f"Confirm actual vessel volume now."))

                if actor_id and pause_seconds > 0:
                    await asyncio.sleep(pause_seconds)
                    if self.running:
                        await self.actor_on(actor_id)
                        logger.info(f"CalibrationTransfer: actor {actor_id} ON after checkpoint pause")

                checkpoint_index += 1

            if accumulated_volume >= final_target:
                stop_reason = f"Final target reached: {accumulated_volume:.2f} gal"
                break

            if low_flow_cutoff > 0 and elapsed >= min_run_time and flow_rate <= low_flow_cutoff:
                stop_reason = f"Low flow detected: {flow_rate:.3f} G/min after {elapsed:.0f}s"
                break

        if actor_id:
            await self.actor_off(actor_id)
            logger.info(f"CalibrationTransfer: actor {actor_id} OFF")

        data["final"] = {
            "cbpi_cumulative_gal": round(accumulated_volume, 3),
            "elapsed_s": int(elapsed),
            "utc": datetime.utcnow().isoformat() + "Z"
        }
        data["stop_reason"] = stop_reason or "Stopped manually"
        data["ended_utc"] = datetime.utcnow().isoformat() + "Z"
        _write_calibration_file(calibration_file, data)

        logger.info(f"CalibrationTransfer complete: {data['stop_reason']}")

        self.summary = (
            f"Calibration complete\n"
            f"CBPi total: {accumulated_volume:.2f} gal\n"
            f"File: {calibration_file}\n"
            f"Run placitas_calibrate.py to compute new MaxVal."
        )

        logger.info("notify: Calibration Transfer Complete | " +
                    str(f"CBPi total = {accumulated_volume:.2f} gal. "
                        f"Next: run placitas_calibrate.py against the saved calibration file."))

        try:
            await self.next()
        except Exception as e:
            logger.error(f"VolumeTransfer: next() failed: {e}")

    async def reset(self):
        pass


# ---------------------------------------------------------------------------
# ResetVolumesActor — dashboard button that resets all KettleVolumeSensors
# ---------------------------------------------------------------------------
@parameters([])
class ResetVolumesActor(CBPiActor):
    """Virtual actor: when switched ON, resets all KettleVolumeSensors to their
    InitialVolume, then turns itself off after 1 second."""

    async def on(self, power=0, **kwargs):
        count = 0
        for sid, initial in _kettle_initial_volume.items():
            _set_kettle_volume(sid, initial)
            count += 1
            logger.info(f"ResetVolumesActor: reset {sid} → {initial:.2f} gal")
        logger.info(f"ResetVolumesActor: reset {count} volume sensor(s)")
        asyncio.ensure_future(self._auto_off())

    async def _auto_off(self):
        await asyncio.sleep(1)
        try:
            await self.cbpi.actor.off(self.id)
        except Exception as e:
            logger.debug(f"ResetVolumesActor auto-off: {e}")

    async def off(self, **kwargs):
        pass

    def get_state(self):
        return dict(state=False)


# ---------------------------------------------------------------------------
# ActorOffStep — turns an actor OFF and immediately advances to next step
# ---------------------------------------------------------------------------
@parameters([
    Property.Actor(label="Actor", description="Actor to turn OFF"),
])
class ActorOffStep(CBPiStep):

    async def on_timer_done(self, timer):
        pass

    async def run(self):
        actor_id = self.props.get("Actor")
        if actor_id:
            await self.actor_off(actor_id)
            logger.warning(f"ActorOffStep: turned OFF {actor_id}")
        await self.finish()

    async def reset(self):
        pass

# ---------------------------------------------------------------------------
# CalibrationExtension — kept for backward compatibility but no longer used.
# CAL_* entries are now registered directly in setup() below.
# ---------------------------------------------------------------------------
class CalibrationExtension(CBPiExtension):
    def __init__(self, cbpi):
        self.cbpi = cbpi


# ---------------------------------------------------------------------------
# CalibrationSensor — shows a single CAL_* config value on the dashboard
# ---------------------------------------------------------------------------
@parameters([
    Property.Select(label="CalKey", options=CAL_CONFIG_KEYS,
                    description="Which calibration factor to display")
])
class CalibrationSensor(CBPiSensor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.value = 1.0

    async def run(self):
        await asyncio.sleep(5)
        while self.running:
            try:
                key = self.props.get("CalKey", "CAL_BOIL_TO_MASH")
                self.value = round(_get_cal(self.cbpi, key), 4)
            except Exception as e:
                logger.warning(f"CalibrationSensor read error: {e}")
            try:
                self.push_update(self.value)
            except Exception:
                pass
            await asyncio.sleep(15)

    def get_state(self):
        return dict(value=self.value)


# ---------------------------------------------------------------------------
# TimedActorStep — turns an actor ON for N seconds then advances
# ---------------------------------------------------------------------------
@parameters([
    Property.Actor(label="Actor", description="Actor or group to turn on"),
    Property.Number(label="Duration", description="Seconds to hold actor on", configurable=True),
])
class TimedActorStep(CBPiStep):
    async def on_timer_done(self, timer):
        pass

    async def run(self):
        actor_id = self.props.get("Actor")
        duration = float(self.props.get("Duration", 10))
        if actor_id:
            await self.actor_on(actor_id)
            logger.info(f"TimedActorStep: {actor_id} ON for {duration}s")
        await asyncio.sleep(duration)
        if actor_id:
            await self.actor_off(actor_id)
            logger.info(f"TimedActorStep: {actor_id} OFF")
        await self.next()

    async def reset(self):
        pass


# ---------------------------------------------------------------------------
# PrimeStep — primes Pump 2 before a transfer
#   1. Opens safe valve group (all except V6)
#   2. Flashes V6 briefly to purge that line, then closes it
#   3. Cycles Pump 2 in short bursts through the safe path (toward HLT)
# ---------------------------------------------------------------------------
@parameters([
    Property.Actor(label="PumpActor",   description="Pump 2"),
    Property.Actor(label="ValveGroup",  description="Safe valves to hold open (all except V6)"),
    Property.Actor(label="BriefValve",  description="V6 — opened briefly to purge line, then closed"),
    Property.Number(label="BriefSeconds",  configurable=True, default_value=3,
                    description="Seconds V6 stays open"),
    Property.Number(label="BurstSeconds",  configurable=True, default_value=3,
                    description="Seconds each pump burst runs"),
    Property.Number(label="BurstCount",    configurable=True, default_value=3,
                    description="Number of pump bursts"),
    Property.Number(label="PauseBetween",  configurable=True, default_value=2,
                    description="Seconds between bursts"),
])
class PrimeStep(CBPiStep):
    async def on_timer_done(self, timer):
        pass

    async def run(self):
        pump_id     = self.props.get("PumpActor")
        valve_id    = self.props.get("ValveGroup")
        brief_id    = self.props.get("BriefValve")
        brief_secs  = float(self.props.get("BriefSeconds", 3))
        burst_secs  = float(self.props.get("BurstSeconds", 3))
        burst_count = int(float(self.props.get("BurstCount", 3)))
        pause_secs  = float(self.props.get("PauseBetween", 2))

        # Open safe valves and flash V6 simultaneously
        if valve_id:
            await self.actor_on(valve_id)
        if brief_id:
            await self.actor_on(brief_id)
            await asyncio.sleep(brief_secs)
            await self.actor_off(brief_id)
            logger.info(f"PrimeStep: V6 closed after {brief_secs}s")

        # Cycle pump in short bursts through safe valve path
        for i in range(burst_count):
            if pump_id:
                await self.actor_on(pump_id)
                logger.info(f"PrimeStep: pump burst {i+1}/{burst_count} ON")
            await asyncio.sleep(burst_secs)
            if pump_id:
                await self.actor_off(pump_id)
                logger.info(f"PrimeStep: pump burst {i+1}/{burst_count} OFF")
            if i < burst_count - 1:
                await asyncio.sleep(pause_secs)

        if valve_id:
            await self.actor_off(valve_id)
        logger.info("PrimeStep: complete")
        await self.next()

    async def reset(self):
        pass


def _register_cal_config(cbpi):
    """Add CAL_* entries to config.json if missing. Runs synchronously at startup."""
    config_path = "/home/pi/config/config.json"
    cal_defs = [
        ("CAL_BOIL_TO_MASH",          "Cal factor: Boil to Mash (Mash In step)"),
        ("CAL_BOIL_TO_HLT",           "Cal factor: Boil to HLT (move/prime steps)"),
        ("CAL_BOIL_TO_HLT_REMAINDER", "Cal factor: Boil to HLT remainder"),
        ("CAL_HLT_TO_MASH",           "Cal factor: HLT to Mash (fly sparge in)"),
        ("CAL_MASH_TO_BOIL",          "Cal factor: Mash to Boil (fly sparge out)"),
        ("CAL_BOIL_TO_FERM",          "Cal factor: Boil to Fermenter"),
    ]
    try:
        with open(config_path) as f:
            config = json.load(f)
        changed = False
        for key, desc in cal_defs:
            if key not in config:
                config[key] = {"description": desc, "name": key, "options": None,
                               "source": "cbpi4_volume_steps", "type": "number", "value": 1.0}
                # Mirror into the live cache so the running instance sees it without restart
                try:
                    cbpi.config.cache[key] = cbpi.config.cache.get(
                        "AUTHOR").__class__(key, 1.0, desc, type("n", (), {"value": "number"})(), "cbpi4_volume_steps", None)
                except Exception:
                    pass
                changed = True
                logger.info(f"setup: registered missing config key {key}")
        if changed:
            with open(config_path, "w") as f:
                json.dump(config, f, indent=4, sort_keys=True)
    except Exception as e:
        logger.warning(f"setup: could not register CAL_* config keys: {e}")


def setup(cbpi):
    _register_cal_config(cbpi)
    cbpi.plugin.register("KettleVolumeSensor", KettleVolumeSensor)
    cbpi.plugin.register("VolumeTransferStep", VolumeTransferStep)
    cbpi.plugin.register("FlySpargeTransferStep", FlySpargeTransferStep)
    cbpi.plugin.register("ActorOffStep", ActorOffStep)
    cbpi.plugin.register("TimedActorStep", TimedActorStep)
    cbpi.plugin.register("PrimeStep", PrimeStep)
    cbpi.plugin.register("CalibrationTransferStep", CalibrationTransferStep)
    cbpi.plugin.register("ResetVolumesActor", ResetVolumesActor)
    cbpi.plugin.register("CalibrationSensor", CalibrationSensor)
    cbpi.plugin.register("CalibrationExtension", CalibrationExtension)
