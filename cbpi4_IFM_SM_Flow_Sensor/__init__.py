import logging
import asyncio
from cbpi.api import *
import board
import adafruit_bitbangio as bitbangio
import adafruit_ads1x15.ads1115 as ADS
from adafruit_ads1x15.analog_in import AnalogIn

logger = logging.getLogger(__name__)

@parameters([
    Property.Select(label="Address", options=["0x48", "0x49", "0x4A", "0x4B"],
                    description="I2C address of the ADS1115 (default 0x48)"),
    Property.Select(label="Channel", options=["0", "1", "2", "3"],
                    description="ADS1115 channel: 0=AIN0, 1=AIN1, 2=AIN2, 3=AIN3"),
    Property.Number(label="Resistor", configurable=True, default_value=150,
                    description="Shunt resistor value in ohms (default 150)"),
    Property.Number(label="mA_Low", configurable=True, default_value=4,
                    description="Low end of current range in mA (typically 4)"),
    Property.Number(label="mA_High", configurable=True, default_value=20,
                    description="High end of current range in mA (typically 20)"),
    Property.Number(label="MinVal", configurable=True, default_value=0,
                    description="Engineering value at mA_Low (e.g. 0 for 0 G/min or 25 for 25 F)"),
    Property.Number(label="MaxVal", configurable=True, default_value=3,
                    description="Engineering value at mA_High (e.g. 3 for 3 G/min or 175 for 175 F)"),
    Property.Text(label="Unit", configurable=True, default_value="G/min",
                  description="Unit label shown in CBPI4 (e.g. G/min or F)"),
])
class IFM_SM6004_ADS1115_Sensor(CBPiSensor):

    async def run(self):
        try:
            i2c = bitbangio.I2C(board.D26, board.D21, frequency=50000)
            i2c_addr = int(self.props.get("Address", "0x48"), 16)
            ads = ADS.ADS1115(i2c, address=i2c_addr)
            selected_pin = int(self.props.get("Channel", 0))
            chan = AnalogIn(ads, selected_pin)
            resistor = float(self.props.get("Resistor", 150))
            ma_low   = float(self.props.get("mA_Low",  4))
            ma_high  = float(self.props.get("mA_High", 20))
            min_val  = float(self.props.get("MinVal",  0))
            max_val  = float(self.props.get("MaxVal",  3))
            v_low  = (ma_low  / 1000.0) * resistor
            v_high = (ma_high / 1000.0) * resistor
            logger.info(f"IFM SM6004 started: addr={hex(i2c_addr)} ch={selected_pin}")
        except Exception as e:
            logger.error(f"IFM SM6004 init failed: {e}")
            return

        while self.running:
            try:
                voltage = chan.voltage
                voltage = max(v_low, min(v_high, voltage))
                ratio = (voltage - v_low) / (v_high - v_low)
                val = round(min_val + ratio * (max_val - min_val), 3)
                self.push_update(val)
            except Exception as e:
                logger.error(f"IFM SM6004 read error: {e}")
            await asyncio.sleep(1)


def setup(cbpi):
    cbpi.plugin.register("IFM_SM6004_Sensor", IFM_SM6004_ADS1115_Sensor)
