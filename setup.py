from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setup(
    name="scriba",
    version="0.1.0",
    author="Dirk Petersen",
    author_email="your.email@example.com",
    description="A private secretary who transcribes your voice notes using AWS transcribe",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/yourusername/scriba",
    packages=find_packages(),
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Environment :: Win32 (MS Windows)",
        "Intended Audience :: End Users/Desktop",
        "License :: OSI Approved :: MIT License",
        "Operating System :: Microsoft :: Windows",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",        
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",        

    ],
    python_requires=">=3.8",
    install_requires=[
        "configparser",
        "websockets",
        "pyaudio",
        "keyboard",
        "pywin32",
        "pystray",
        "pillow",
    ],
    entry_points={
        "console_scripts": [
            "scriba=scriba.scriba:main",
        ],
    },
    include_package_data=True,
)

[build-system]
requires = ["setuptools>=45", "wheel", "setuptools_scm>=6.2"]
build-backend = "setuptools.build_meta"
import pytest
from scriba.scriba import Scriba

def test_scriba_init():
    scriba = Scriba()
    assert scriba.RATE == 16000
    assert scriba.CHANNELS == 1
    assert scriba.FORMAT == 16 # pyaudio.paInt16
"""
Scriba - A private secretary who transcribes your voice notes
"""

__version__ = "0.1.0"
