from setuptools import setup

setup(
    name="apsta",
    version="0.5.6",
    py_modules=["apsta", "apsta_gtk"],
    install_requires=[],
    entry_points={
        "console_scripts": [
            "apsta=apsta:main",
            "apsta-gtk=apsta_gtk:main",
        ],
    },
)