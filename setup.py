from setuptools import setup, find_packages

setup(
    name="cbpi4-IFM-SM-Flow-Sensor",
    version="0.0.1",
    description="CraftBeerPi4 sensor plugin for the IFM SM6004 magnetic-inductive flow sensor via ADS1115 ADC",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    author="walkuf",
    url="https://github.com/walkuf/cbpi4-IFM-SM-Flow-Sensor",
    license="GPL-3.0",
    packages=find_packages(),
    install_requires=[
        "adafruit-circuitpython-ads1x15",
        "adafruit-blinka",
        "adafruit-circuitpython-bitbangio",
    ],
    keywords=["cbpi4", "craftbeerpi", "flow sensor", "IFM", "SM6004", "ADS1115"],
    include_package_data=True,
)
