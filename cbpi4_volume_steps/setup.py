from setuptools import setup, find_packages

setup(
    name="cbpi4-volume-steps",
    version="0.0.1",
    description="CraftBeerPi4 volume-based mash step plugins for transfer and fly sparge",
    author="walkuf",
    url="https://github.com/walkuf/cbpi4-volume-steps",
    license="GPL-3.0",
    packages=find_packages(),
    keywords=["cbpi4", "craftbeerpi", "mash", "sparge", "volume", "flow"],
    include_package_data=True,
)
