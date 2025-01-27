# Scriba

An eager helper who transcribes your voice notes and prints the result into the currently active Windows application

In ancient Rome, the scriba (Latin; pl.: scribae[1]) was a public notary or clerk (see also scrivener). The word scriba might also refer to a man who was a private secretary, but should be distinguished from a copyist (who might be called a "scribe" in English) or bookseller (librarius). [see Wikipedia](https://en.wikipedia.org/wiki/Scriba_(ancient_Rome))

This tool uses the current default Microphone and prints out the text at the currently active Window. It is intended to (temporarily) replace Windows Voice Access, at last until that gets better (as of January 2025 it does not produce good results, especially if you speak English with an accent)

It is largely based on [amazon-transcribe-streaming-python-websockets](https://github.com/aws-samples/amazon-transcribe-streaming-python-websockets)

## Download and setup Windows executable

Download [Scriba.exe](https://github.com/dirkpetersen/scriba/raw/refs/heads/download/scriba.exe), store it in reasonable location such as %USERPROFILE%\bin and create a link on your desktop or autostart. Scriba will use the default AWS profile credentials that you might have stored in %USERPROFILE%\.aws. If it does not find any AWS credentials it will prompt you.


## Run Scriba

Once you execute Scriba, it will show up in your Windows system tray as a yellow icon which means that it has started in standby mode. Now drag the yellow icon from the system tray to the Windows notification area, for example right next to the OneDrive icon.




## Install from PyPI

If you already have Python installed you can also install it as a Console application. 

```
python3 -m pip install pipx
python3 -m pipx install pyscriba
