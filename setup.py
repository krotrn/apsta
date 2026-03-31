from setuptools import find_packages, setup

setup(
    name="apsta",
    version="0.6.0",
    py_modules=["apsta", "apsta_gtk"],
    packages=find_packages(include=["apsta_cli*", "apsta_gui*"]),
    install_requires=["qrcode[pil]>=7.4"],
    entry_points={
        "console_scripts": [
            "apsta=apsta:main",
            "apsta-gtk=apsta_gtk:main",
        ],
    },
)