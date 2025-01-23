from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setup(
    name="pyscriba",
    version="0.1.0",
    author="Dirk Petersen",
    author_email="your.email@example.com",
    description="An eager helper who transcribes your voice notes using AWS transcribe",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/dirkpetersen/scriba",
    packages=find_packages(),
    classifiers=[
        "Development Status :: 4 - Beta",
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
    python_requires=">=3.9",
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

"""
Scriba - An eager helper who transcribes your voice notes
"""

__version__ = "0.1.0"
