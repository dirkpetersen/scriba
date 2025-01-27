# Scriba

An eager helper who transcribes your voice notes and prints the result into the currently active Windows application

In ancient Rome, the scriba (Latin; pl.: scribae[1]) was a public notary or clerk (see also scrivener). The word scriba might also refer to a man who was a private secretary, but should be distinguished from a copyist (who might be called a "scribe" in English) or bookseller (librarius). [see Wikipedia](https://en.wikipedia.org/wiki/Scriba_(ancient_Rome))

This tool uses the current default Microphone and prints out the text at the currently active Window. It is intended to (temporarily) replace Windows Voice Access, at last until that gets better (as of January 2025 it does not produce good results, especially if you speak English with an accent)

It is largely based on [amazon-transcribe-streaming-python-websockets](https://github.com/aws-samples/amazon-transcribe-streaming-python-websockets)

## Install 

```
python3 -m pip install pipx
python3 -m pipx install envoicer 