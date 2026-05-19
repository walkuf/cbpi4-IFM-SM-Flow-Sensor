import logging
import asyncio
from cbpi.api import *
import board
import bitbangio
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
    """
    CraftBeerPi4 sensor plugin for the IFM SM6004 magnetic-inductive flow sensor
    and its temperature output, read via an ADS1115 ADC over software I2C.

    Supports all four ADS1115 channels on a single device, allowing two SM6004
    sensors (flow + temperature each) to be read from one ADS1115.

    Wiring:
        ADS1115 SCL -> GPIO20
        ADS1115 SDA -> GPIO21
        Each sensor output -> 150 ohm shunt resistor -> ADS1115 AINx -> GND

    Sensor instance configuration guide:
        Flow Sensor 1:  Channel=0, MinVal=0,  MaxVal=3,   Unit=G/min
        Temp Sensor 1:  Channel=1, MinVal=25, MaxVal=175, Unit=F
        Flow Sensor 2:  Channel=2, MinVal=0,  MaxVal=3,   Unit=G/min
        Temp Sensor 2:  Channel=3, MinVal=25, MaxVal=175, Unit=F
    """

    def init(self):
        try:
            self.i2c = bitbangio.I2C(board.D20, board.D21)
            i2c_addr = int(self.props.get("Address", "0x48"), 16)
            self.ads = ADS.ADS1115(self.i2c, address=i2c_addr)
            selected_pin = int(self.props.get("Channel", 0))
            self.chan = AnalogIn(self.ads, selected_pin)

            self.resistor = float(self.props.get("Resistor", 150))
            self.ma_low   = float(self.props.get("mA_Low",  4))
            self.ma_high  = float(self.props.get("mA_High", 20))
            self.min_val  = float(self.props.get("MinVal",  0))
            self.max_val  = float(self.props.get("MaxVal",  3))

            logger.info(
                f"IFM SM6004 initialized: addr={self.props.get('Address')}, "
                f"ch={self.props.get('Channel')}, R={self.resistor}Ω, "
                f"mA range={self.ma_low}–{self.ma_high}mA, "
                f"value range={self.min_val}–{self.max_val} {self.props.get('Unit', '')}"
            )
        except Exception as e:
            logger.error(f"Failed to initialize ADS1115: {e}")
            self.chan = None

    def voltage_to_engineering(self, voltage):
        """
        Convert ADS1115 voltage reading to engineering units
        using the configurable mA range and shunt resistor value.
        """
        v_low  = (self.ma_low  / 1000.0) * self.resistor
        v_high = (self.ma_high / 1000.0) * self.resistor

        # Clamp to valid window to prevent out-of-range values from noise
        voltage = max(v_low, min(v_high, voltage))

        ratio = (voltage - v_low) / (v_high - v_low)
        return round(self.min_val + ratio * (self.max_val - self.min_val), 3)

    def get_value(self):
        if self.chan is None:
            return 0.0
        try:
            voltage = self.chan.voltage
            return self.voltage_to_engineering(voltage)
        except Exception as e:
            logger.error(f"Error reading channel {self.props.get('Channel')}: {e}")
            return 0.0

    async def run(self):
        while self.running:
            val = self.get_value()
            self.push_update(val)
            await asyncio.sleep(1)


def setup(cbpi):
    cbpi.plugin.register("IFM_SM6004_Sensor", IFM_SM6004_ADS1115_Sensor)
