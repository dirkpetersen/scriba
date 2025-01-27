import pytest
from scriba.scriba import Scriba
import os

def test_scriba_audio_settings():
    # Set dummy AWS credentials for testing
    os.environ["AWS_ACCESS_KEY_ID"] = "dummy_key"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "dummy_secret"
    
    scriba = Scriba()
    assert scriba.RATE == 16000
    assert scriba.CHANNELS == 1
    assert scriba.CHUNK == 1600
