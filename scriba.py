# Note: This App runs on Windows OS

import os, sys, logging, string, random, configparser, asyncio, pathlib, re, threading
import websockets
import pyaudio
import keyboard
import win32con
import win32gui
import win32api
import win32process
import ctypes
from ctypes import wintypes
import time
from presigned_url import AWSTranscribePresignedURL
from eventstream import create_audio_event, decode_event
from gui import GUI

user32 = ctypes.WinDLL('user32', use_last_error=True)

LOGLEVEL=logging.INFO # logging.INFO or logging.DEBUG
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002

# C struct definitions
if not hasattr(wintypes, 'ULONG_PTR'):
    wintypes.ULONG_PTR = wintypes.WPARAM if hasattr(wintypes, 'WPARAM') else ctypes.c_size_t

class MOUSEINPUT(ctypes.Structure):
    _fields_ = (("dx",          wintypes.LONG),
                ("dy",          wintypes.LONG),
                ("mouseData",    wintypes.DWORD),
                ("dwFlags",     wintypes.DWORD),
                ("time",        wintypes.DWORD),
                ("dwExtraInfo", wintypes.ULONG_PTR))

class KEYBDINPUT(ctypes.Structure):
    _fields_ = (("wVk",         wintypes.WORD),
                ("wScan",       wintypes.WORD),
                ("dwFlags",     wintypes.DWORD),
                ("time",        wintypes.DWORD),
                ("dwExtraInfo", wintypes.ULONG_PTR))

class HARDWAREINPUT(ctypes.Structure):
    _fields_ = (("uMsg",    wintypes.DWORD),
                ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD))

class INPUT(ctypes.Structure):
    class _INPUT(ctypes.Union):
        _fields_ = (("ki", KEYBDINPUT),
                   ("mi", MOUSEINPUT),
                   ("hi", HARDWAREINPUT))
    _anonymous_ = ("_input",)
    _fields_ = (("type",   wintypes.DWORD),
                ("_input", _INPUT))

# Declare SendInput function parameters and return type
user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT

class Scriba:

    def __init__(self):
        # Global transcript state
        self._current_transcript = ""
        self._last_printed_text = ""
        
        # AWS Transcribe billing optimization
        self._minute_start_time = 0
        self._in_billable_minute = False
        
        # Configure logging
        # Configure logging
        logging.basicConfig(
            stream=sys.stderr,
            level=LOGLEVEL,
            format='%(asctime)s.%(msecs)03d %(levelname)s: %(message)s',
            datefmt='%H:%M:%S'
        )
        # Suppress websockets debug logging
        logging.getLogger('websockets').setLevel(logging.INFO)
        
        # Audio settings following AWS best practices
        self.CHANNELS = 1
        self.RATE = 16000  # Recommended 16kHz sampling rate
        self.CHUNK = 1600  # 100ms chunks: (0.100 * 16000 * 2) / 2 = 1600 samples
        self.FORMAT = pyaudio.paInt16  # 16-bit PCM
        
        # Calculate bytes per sample and verify chunk alignment
        self.BYTES_PER_SAMPLE = 2  # 16-bit = 2 bytes
        assert self.CHUNK * self.BYTES_PER_SAMPLE % (self.CHANNELS * self.BYTES_PER_SAMPLE) == 0, \
            "Chunk size must be aligned with frame size"
        self.running = False
        self.recording_enabled = True
        self.sent_sentences = set()  # Track sent sentences
        
        # Initialize GUI        
        self.gui = GUI(self.toggle_recording)
        self.gui.start()
        self.gui.show_notification(
            "Scriba Started",
            "Ready to transcribe audio"
        )
        self.silence_threshold = 300  # Lower threshold for audio activity
        self.debug_audio = False  # Disable audio level debugging
        
        # AWS Configuration
        self.access_key = os.getenv("AWS_ACCESS_KEY_ID", "")
        self.secret_key = os.getenv("AWS_SECRET_ACCESS_KEY", "")
        self.session_token = os.getenv("AWS_SESSION_TOKEN", "")
        self.region = os.getenv("AWS_DEFAULT_REGION", "us-west-2")
        self.region = os.getenv("AWS_REGION", self.region)

        if not self.access_key or not self.secret_key:
            config = configparser.ConfigParser()
            credentials_path = os.path.join(pathlib.Path.home(),'.aws','credentials')
            if os.path.exists(credentials_path):
                config.read(credentials_path)
                if 'default' in config:
                    self.access_key = config['default'].get('aws_access_key_id', '')
                    self.secret_key = config['default'].get('aws_secret_access_key', '')      
                    self.session_token = config['default'].get('aws_session_token', '')

        # print(self.access_key, self.secret_key, self.session_token, self.region)
            
        # Initialize PyAudio
        self.audio = pyaudio.PyAudio()
        
    def send_keystrokes_win32(self, text):
        """Send keystrokes using Win32 API SendInput to active window"""
        # Ensure window is active and ready
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            logging.warning("No active window found - text not sent")
            return False
            
        try:
            logging.debug(f"Active window handle: 0x{hwnd:08x}")
            window_title = win32gui.GetWindowText(hwnd)
            window_class = win32gui.GetClassName(hwnd)
            window_thread = win32process.GetWindowThreadProcessId(hwnd)[0]
            logging.debug(f"Window title: '{window_title}', class: '{window_class}', thread: {window_thread}")

            # Send each character
            for char in text:
                vk = win32api.VkKeyScan(char)
                if vk == -1:
                    logging.debug(f"No virtual key code found for character: {char!r}")
                    continue
                    
                vk_code = vk & 0xFF
                shift_state = (vk >> 8) & 0xFF
                logging.debug(f"Character {char!r}: vk_code=0x{vk_code:02x}, shift_state=0x{shift_state:02x}")
                
                inputs = []
                
                # Add shift key if needed
                if shift_state & 1:
                    shift_down = INPUT(type=INPUT_KEYBOARD, 
                                     ki=KEYBDINPUT(wVk=win32con.VK_SHIFT))
                    inputs.append(shift_down)
                    
                # Key down
                key_down = INPUT(type=INPUT_KEYBOARD,
                                ki=KEYBDINPUT(wVk=vk_code))
                inputs.append(key_down)
                
                # Key up
                key_up = INPUT(type=INPUT_KEYBOARD,
                              ki=KEYBDINPUT(wVk=vk_code, 
                                          dwFlags=KEYEVENTF_KEYUP))
                inputs.append(key_up)
                
                # Release shift if needed
                if shift_state & 1:
                    shift_up = INPUT(type=INPUT_KEYBOARD,
                                   ki=KEYBDINPUT(wVk=win32con.VK_SHIFT,
                                               dwFlags=KEYEVENTF_KEYUP))
                    inputs.append(shift_up)
                    
                # Send inputs
                num_inputs = len(inputs)
                input_array = (INPUT * num_inputs)(*inputs)
                logging.debug(f"Sending {num_inputs} inputs")
                for i, inp in enumerate(inputs):
                    logging.debug(f"Input {i}: type={inp.type}, vk=0x{inp.ki.wVk:02x}, "
                                f"scan=0x{inp.ki.wScan:02x}, flags=0x{inp.ki.dwFlags:08x}, "
                                f"time={inp.ki.time}, extra=0x{inp.ki.dwExtraInfo:016x}")
                
                # Ensure all parameters are properly typed
                c_num_inputs = ctypes.c_uint(num_inputs)
                c_size = ctypes.c_int(ctypes.sizeof(INPUT))
                
                result = user32.SendInput(c_num_inputs, input_array, c_size)
                if result != num_inputs:
                    error = ctypes.get_last_error()
                    error_msg = (f"SendInput failed: only {result}/{num_inputs} inputs were sent "
                               f"(error: {error}, {ctypes.FormatError(error)})")
                    logging.error(error_msg)
                    raise ctypes.WinError(error)
                logging.debug(f"SendInput succeeded: sent {result}/{num_inputs} inputs")
                time.sleep(0.005)  # Smaller delay since SendInput is more reliable
                
            window_title = win32gui.GetWindowText(hwnd)
            logging.info(f"Sent to '{window_title}': {text}")
            return True
            
        except Exception as e:
            logging.error(f"Failed to send text: {e}")
            return False

        
        
    def get_default_input_device_info(self):
        """Get and log information about the default input device"""
        try:
            default_input = self.audio.get_default_input_device_info()
            logging.info(f"Using input device: {default_input['name']}")
            logging.info(f"Device info: {default_input}")
            return default_input
        except Exception as e:
            logging.error(f"Error getting input device info: {e}")
            return None

    def is_audio_active(self, audio_data):
        """Check if there's significant audio activity"""
        audio_level = max(abs(int.from_bytes(audio_data[i:i+2], 'little', signed=True)) 
                         for i in range(0, len(audio_data), 2))
        return audio_level > self.silence_threshold

    async def record_and_stream(self, websocket):
        self.get_default_input_device_info()
        stream = self.audio.open(
            format=self.FORMAT,
            channels=self.CHANNELS,
            rate=self.RATE,
            input=True,
            frames_per_buffer=self.CHUNK
        )
        logging.info(f"Started recording with: {self.RATE}Hz, {self.CHANNELS} channels, chunk size: {self.CHUNK}")
        
        try:
            consecutive_errors = 0
            voice_active = False
            silence_frames = 0
            current_time = time.time()
            
            while self.running:
                try:
                    data = stream.read(self.CHUNK, exception_on_overflow=False)
                    if len(data) > 0:
                        is_active = self.is_audio_active(data)
                        current_time = time.time()
                        
                        # If not active, send silence (zeros) to maintain stream
                        if not is_active and self._in_billable_minute:
                            data = b'\x00' * (self.CHUNK * self.BYTES_PER_SAMPLE)
                        
                        # Start a new billable minute when voice activity is detected
                        if is_active and not self._in_billable_minute:
                            self._minute_start_time = current_time
                            self._in_billable_minute = True
                            logging.debug("Starting new billable minute")
                        
                        # Handle voice activity state changes
                        if is_active:
                            silence_frames = 0
                            if not voice_active:
                                logging.info("Voice activity detected")
                                voice_active = True
                                self.gui.set_state('active')
                                self.gui.show_notification(
                                    "Voice Detected",
                                    "Started transcribing audio",
                                    duration=2
                                )
                        elif voice_active:
                            silence_frames += 1
                            if silence_frames > 10:
                                logging.info("Voice activity stopped")
                                voice_active = False
                                silence_frames = 0
                                self.gui.set_state('ready')
                        
                        # Send audio if we're in a billable minute
                        if self._in_billable_minute:
                            audio_event = create_audio_event(data)
                            await websocket.send(audio_event)
                            consecutive_errors = 0
                            
                            # Check if current minute is complete
                            if current_time - self._minute_start_time >= 60:
                                self._in_billable_minute = False
                                if not is_active:
                                    logging.debug("Billable minute complete")
                        await asyncio.sleep(0.001)  # Small sleep to prevent CPU overload
                except websockets.exceptions.ConnectionClosedError:
                    consecutive_errors += 1
                    if consecutive_errors > 5:
                        logging.error("Too many consecutive connection errors")
                        break
                    continue
        finally:
            stream.stop_stream()
            stream.close()

    def process_transcript(self, text: str, is_partial: bool) -> None:
        """Process / correct / modify transcript text"""
        try:
            # Insert a space after punctuation if not already present
            sendtext = re.sub(r'([.?!])(?![\s"])', r'\1 ', text)
            if is_partial:
                logging.debug(f"Partial: {text}")
            else:
                self.send_keystrokes_win32(sendtext)               
                logging.info(f"Transcript: {sendtext}")
                        
        except Exception as e:
            logging.error(f"Error processing transcript: {e}")

    async def receive_transcription(self, websocket):
        """Receive and process transcription results from websocket"""
        while self.running:
            try:
                response = await websocket.recv()
                header, payload = decode_event(response)

                if header[":message-type"] == 'exception':
                    logging.error(payload['Message'])
                    continue
                
                if header[':message-type'] != 'event':
                    continue
                    
                # Process transcript if available
                if 'Transcript' in payload and payload['Transcript']['Results']:
                    transcript = payload['Transcript']['Results'][0]                            
                    if 'Alternatives' in transcript and transcript['Alternatives']:
                        text = transcript['Alternatives'][0]['Transcript']                              
                        is_partial = transcript.get('IsPartial', True)                                
                        self.process_transcript(text, is_partial)                                    
                
                await asyncio.sleep(0)
                
            except websockets.exceptions.ConnectionClosedOK:
                logging.info("Streaming completed successfully - reconnecting...")
                break
                
            except websockets.exceptions.ConnectionClosedError:
                logging.error("WebSocket connection closed unexpectedly")
                break
                
            except Exception as e:
                logging.exception(f"Error in receive_transcription: {e}")
                break

    async def connect_to_websocket(self):
        max_retries = 3
        retry_delay = 2
        attempt = 0
        
        while self.running and attempt < max_retries:
            try:
                attempt += 1
                logging.info(f"Connecting to AWS Transcribe (attempt {attempt}/{max_retries})")
                
                # Initialize URL generator with explicit region
                transcribe_url_generator = AWSTranscribePresignedURL(
                    access_key=self.access_key,
                    secret_key=self.secret_key,
                    session_token=self.session_token,
                    region=self.region
                )
                
                # Generate random websocket key and headers like in example.py
                websocket_key = ''.join(random.choices(string.ascii_uppercase + string.ascii_lowercase + string.digits, k=20))
                headers = {
                    "Origin": "https://localhost",
                    "Sec-Websocket-Key": websocket_key,
                    "Sec-Websocket-Version": "13",
                    "Connection": "keep-alive"
                }
                
                # Generate presigned URL with all required parameters
                request_url = transcribe_url_generator.get_request_url(
                    sample_rate=self.RATE,
                    language_code="en-US",
                    media_encoding="pcm",
                    number_of_channels=self.CHANNELS,
                    enable_channel_identification=False,
                    enable_partial_results_stabilization=True,
                    partial_results_stability="medium"
                )
                
                async with websockets.connect(
                    request_url,
                    additional_headers=headers,
                    ping_timeout=None
                ) as websocket:
                    logging.info("Connected to AWS Transcribe")
                    try:
                        await asyncio.gather(
                            self.record_and_stream(websocket),
                            self.receive_transcription(websocket)
                        )
                    except websockets.exceptions.ConnectionClosedOK:
                        logging.info("Connection closed normally, reconnecting...")
                        await asyncio.sleep(1)  # Brief pause before reconnecting
                        continue
                    except websockets.exceptions.ConnectionClosedError as e:
                        if attempt < max_retries:
                            logging.warning(f"Connection closed unexpectedly, retrying in {retry_delay} seconds... ({e})")
                            await asyncio.sleep(retry_delay)
                            continue
                        else:
                            logging.error(f"Failed to maintain connection after {max_retries} attempts")
                            break
                    
            except Exception as e:
                logging.exception(f"Unexpected error in connection: {e}")
                if attempt < max_retries:
                    await asyncio.sleep(retry_delay * attempt)  # Exponential backoff
                    continue
                break

    async def cleanup(self):
        """Clean up resources"""
        if not self.running:
            return
            
        self.running = False
        logging.info("Cleaning up resources...")
        
        # Cancel all tasks except the current one
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
            
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as e:
            logging.error(f"Error during cleanup: {e}")
        finally:
            self.audio.terminate()
            logging.info("Cleanup complete")

    def start(self):
        """Start the voice transcription service"""
        self.running = True
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        def stop_app():
            if self.running:
                print("\nStopping Scriba...")
                logging.info("Initiating shutdown sequence")
                self.running = False
                # Schedule cleanup in the event loop
                asyncio.run_coroutine_threadsafe(self.cleanup(), loop)
                
        try:
            # Register hotkey to stop the service
            keyboard.add_hotkey('ctrl+shift+x', stop_app)
            
            print("\nScriba started. Press Ctrl+Shift+X to stop.")
            print("Listening for audio input...")
            
            try:
                loop.run_until_complete(self.connect_to_websocket())
            except RuntimeError as e:
                if str(e) == 'Event loop stopped before Future completed.':
                    # This is expected during normal shutdown
                    pass
                else:
                    raise
            
        finally:
            try:
                # Ensure all tasks are cleaned up
                pending = asyncio.all_tasks(loop)
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception as e:
                logging.error(f"Error during shutdown: {e}")
            finally:
                logging.info("Closing event loop")
                loop.close()
                print("Scriba shutdown complete.")
    
    def toggle_recording(self):
        """Toggle recording state when icon is clicked"""
        self.recording_enabled = not self.recording_enabled
        state = 'ready' if self.recording_enabled else 'disabled'
        self.gui.set_state(state)
        self.gui.show_notification(
            "Recording Status",
            f"Recording {state}",
            duration=2
        )
        logging.info(f"Recording {state}")
        
    def stop(self):
        """Stop the voice transcription service"""
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(self.cleanup())
        self.gui.stop()
        print("\nScriba stopped.")

def main():
    def signal_handler(signum, frame):
        print("\nPlease use Ctrl+Shift+X to stop Scriba properly.")
        print("Continuing...")
        
    import signal
    signal.signal(signal.SIGINT, signal_handler)
    
    scriba = Scriba()
    scriba.start()

if __name__ == "__main__":
    main()
