import pytest
from scriba.scriba import Scriba

def test_scriba_init():
    scriba = Scriba()
    assert scriba.RATE == 16000
    assert scriba.CHANNELS == 1
    assert scriba.FORMAT == pyaudio.paInt16
