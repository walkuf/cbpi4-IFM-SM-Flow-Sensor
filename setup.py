from setuptools import setup, find_packages

setup(
    name='cbpi4-IFM-SM-Flow-Sensor',
    version='0.0.1',
    description='CBPI4 custom plugin for IFM SM004 Flow Sensor',
    author='Your Name',
    author_email='your.email@example.com',
    url='https://github.com/walkuf/cbpi4-IFM-SM-Flow-Sensor',
    license='MIT',
    packages=find_packages(),
    include_package_data=True,
    install_requires=[
        'cbpi>=4.0.0',
        'pymodbus>=2.5.0',
    ],
    entry_points={
        'cbpi_plugins': [
            'cbpi4_IFM_SM004 = cbpi4_IFM_SM004:setup',
        ]
    }
)
