# Scriba

An eager helper who transcribes your voice notes and prints the result into the currently active Windows application

In ancient Rome, the scriba (Latin; pl.: scribae[1]) was a public notary or clerk (see also scrivener). The word scriba might also refer to a man who was a private secretary, but should be distinguished from a copyist (who might be called a "scribe" in English) or bookseller (librarius). [see Wikipedia](https://en.wikipedia.org/wiki/Scriba_(ancient_Rome))

This tool uses the current default Microphone and prints out the text at the currently active Window. It is intended to (temporarily) replace Windows Voice Access, at last until that gets better (as of January 2025 it does not produce good results, especially if you speak English with an accent)

It is largely based on [amazon-transcribe-streaming-python-websockets](https://github.com/aws-samples/amazon-transcribe-streaming-python-websockets)

## Download and setup Windows executable

Download [Scriba.exe](https://github.com/dirkpetersen/scriba/raw/refs/heads/download/scriba.exe), store it in reasonable location such as %USERPROFILE%\bin, and create a link on your desktop or autostart. 


## Run Scriba

Once you execute Scriba, Windows will complain that the application is not signed. Click `more info` and then `Run Anyway`.

![image](https://github.com/user-attachments/assets/dbb764e1-a278-49a0-bf7a-ead4952808fe)

Scriba requires AWS credentials that have at least AmazonTranscribeFullAccess (and in the future AmazonBedrockFullAccess). It will use the default AWS profile credentials (it needs at least AmazonTranscribeFullAccess that you might have stored in environment variables or in %USERPROFILE%\\.aws, but if it does not find any AWS credentials, it will prompt you:

![image](https://github.com/user-attachments/assets/c7efab4a-4cd9-48b2-80c3-43fe37cc4e4a)

The app will show up in your Windows system tray as a yellow icon, which means that it has started in standby mode. Now drag the yellow icon from the system tray to the Windows notification area, for example right next to the OneDrive icon.

![image](https://github.com/user-attachments/assets/fae28182-36d3-4bba-a5b4-81f2b3fdd129)

As you start speaking, the icon will turn green and transcribe and print the text in the currently active window.  if you (left)click on the icon, it will turn red and stop transcribing 

![image](https://github.com/user-attachments/assets/0d85f860-4641-4f8a-b38e-97e7f4a0ade0)

If you right-click on the icon, you can switch to German language which will be activated after a short delay

![image](https://github.com/user-attachments/assets/d6dc1906-2c45-4abc-8d73-5ac61cdad20a)

Other languages are only supported via command line, for example `scriba.exe --language=it-IT` and you can simply configure the default language in your icon properties:

![image](https://github.com/user-attachments/assets/b863d844-d962-444a-9e49-2679712db11b)

### special commands 

if you say "Period" or "and Period" it will print a . to end the sentence. The german equivalents to this are "Punkt" or "und Punkt". If you say "Comma" or "and Comma" it will print a , (German: "Komma" "und Komma") . 


## Install from PyPI

If you already have Python installed you can also install it as a console application. 

```
python3 -m pip install pipx
python3 -m pipx install pyscriba
