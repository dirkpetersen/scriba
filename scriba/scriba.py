# Note: This App runs on Windows OS

import os, sys, logging, string, random, configparser, asyncio, pathlib, re, argparse
from logging.handlers import RotatingFileHandler
import websockets
import pyaudio
import keyboard
import win32api, win32con, win32gui, win32process, win32event, winerror
import ctypes
from ctypes import wintypes
import time
from presigned_url import AWSTranscribePresignedURL
from eventstream import create_audio_event, decode_event
from gui import GUI
import win32api, win32con, win32gui, win32process, win32event, winerror

LOGLEVEL=logging.INFO  # logging.INFO or logging.DEBUG

class Scriba:

    def __init__(self):
        # Global transcript state
        self._current_transcript = ""
        self._last_printed_text = ""
        
        # Connection state management
        self._connection_attempts = 0
        self._last_timeout = 0
        self._consecutive_timeouts = 0
        self._backoff_time = 1
        self._max_retries = 10
        self._retry_delay = 2
        
        # Language settings
        self._current_language = "en-US"  # Default to English
        self.language_switched = False 
        
        # AWS Transcribe billing optimization
        self._minute_start_time = 0
        self._in_billable_minute = False
        
        # Configure logging based on execution context
        log_format = '%(asctime)s.%(msecs)03d %(levelname)s: %(message)s'
        date_format = '%H:%M:%S'
        
        # Check if running as PyInstaller executable
        if getattr(sys, 'frozen', False):
            try:
                import threading
                import queue
                
                # Create a queue for log messages
                self.log_queue = queue.Queue()
                
                # Create a QueueHandler to send logs to our queue
                queue_handler = logging.handlers.QueueHandler(self.log_queue)
                
                # Configure root logger with queue handler
                root_logger = logging.getLogger()
                root_logger.setLevel(LOGLEVEL)
                root_logger.addHandler(queue_handler)
                
                # Set up file handler in a separate thread
                def logger_thread():
                    log_file = os.path.join(pathlib.Path.home(), 'scriba-log.txt')
                    try:
                        # Create rotating file handler
                        file_handler = RotatingFileHandler(
                            log_file,
                            maxBytes=1024*1024,  # 1MB max file size
                            backupCount=1,
                            delay=True  # Don't open file until first emit
                        )
                        file_handler.setFormatter(logging.Formatter(log_format, date_format))
                        
                        # Create console handler for stdout/stderr
                        console_handler = logging.StreamHandler(sys.stderr)
                        console_handler.setFormatter(logging.Formatter(log_format, date_format))
                        
                        # Process log records from the queue
                        while True:
                            try:
                                record = self.log_queue.get()
                                if record is None:  # Shutdown signal
                                    break
                                    
                                # Write to both file and console
                                try:
                                    file_handler.emit(record)
                                except Exception as e:
                                    print(f"Error writing to log file: {e}")
                                    
                                try:
                                    console_handler.emit(record)
                                except Exception as e:
                                    print(f"Error writing to console: {e}")
                                    
                            except Exception as e:
                                print(f"Error in logger thread: {e}")
                                
                    except Exception as e:
                        print(f"Failed to initialize file logging: {e}")
                        # Continue with just console output
                        while True:
                            record = self.log_queue.get()
                            if record is None:
                                break
                            try:
                                console_handler.emit(record)
                            except Exception:
                                pass
                
                # Start logger thread
                self.logger_thread = threading.Thread(target=logger_thread, daemon=True)
                self.logger_thread.start()
                
            except Exception as e:
                print(f"Warning: Could not set up logging system: {e}")
                # Fall back to basic console logging
                logging.basicConfig(
                    stream=sys.stderr,
                    level=LOGLEVEL,
                    format=log_format,
                    datefmt=date_format
                )
        else:
            # Running as normal Python script
            logging.basicConfig(
                stream=sys.stderr,
                level=LOGLEVEL,
                format=log_format,
                datefmt=date_format
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
        
        # GUI will be initialized in main()
        self.gui = None
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
            
            if not self.access_key or not self.secret_key:
                from aws_credentials_form import prompt_aws_credentials
                self.access_key, self.secret_key, self.region = prompt_aws_credentials(self.region)
                if not self.access_key or not self.secret_key:
                    logging.error("AWS credentials not provided - exiting")
                    sys.exit(1)

        # print(self.access_key, self.secret_key, self.session_token, self.region)
        # Initialize PyAudio
        self.audio = pyaudio.PyAudio()
        # Toggle if a full stop was set 
        self.full_stop = False


    def convert_umlauts(self, text):
        return text.replace('ä','ae').replace('Ä','Ae').replace('ö','oe').replace('Ö','Oe').replace('ü','ue').replace('Ü','Ue').replace('ß','ss')

    def process_transcript(self, text: str, is_partial: bool) -> None:
        """Process / correct / modify transcript text"""
        try:
            if is_partial: 
                logging.debug(f"Partial: {text}")
            else:
                # Handle capitalization and periods based on "stop" command
                sendtext  = text.rstrip('.')
                if sendtext.lower() == "period":
                    sendtext = "."  # Just send a period for "stop" command
                    self.full_stop = True
                elif sendtext.lower().endswith('period'):
                    sendtext = sendtext[:-6]
                    if sendtext.lower().endswith(', '):
                        sendtext = sendtext[:-2]                        
                    sendtext = sendtext.strip()+'.' 
                    sendtext = " " + sendtext 
                    self.full_stop = True
                # Capitalize only if previous was "stop"       
                else: 
                    if not self.full_stop:
                        sendtext = " " + sendtext[0].lower() + sendtext[1:]
                    else:
                        sendtext = " " + sendtext 
                    self.full_stop = False
                sendtext = "," if sendtext.strip().lower() == "comma" else sendtext
                # Remove filler words and their variations, including at start of sentences
                sendtext = re.sub(r'\b(hm+|mm+|oh|uh+|um+|ah+|er+|well+)\s*(?:[,.])?\s*', '', sendtext, flags=re.IGNORECASE)
                # Insert a space after punctuation if not already present
                #sendtext = re.sub(r'([.?!])(?![\s"])', r'\1 ', sendtext)
                sendtext = self.convert_umlauts(sendtext)
                send_keystrokes_win32(sendtext)
                logging.info(f"Transcript: {sendtext}")
                        
        except Exception as e:
            logging.error(f"Error processing transcript:  {e}")



        
        
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

    def _init_audio_stream(self):
        """Initialize and configure the audio input stream"""
        try:
            if hasattr(self, '_stream'):
                try:
                    self._stream.stop_stream()
                    self._stream.close()
                except:
                    pass
            
            self.get_default_input_device_info()
            self._stream = self.audio.open(
                format=self.FORMAT,
                channels=self.CHANNELS,
                rate=self.RATE,
                input=True,
                frames_per_buffer=self.CHUNK
            )
            logging.info(f"Started recording with: {self.RATE}Hz, {self.CHANNELS} channels, chunk size: {self.CHUNK}")
            return self._stream
        except Exception as e:
            logging.error(f"Error initializing audio stream: {e}")
            raise

    def _process_voice_activity(self, is_active, voice_active, silence_frames):
        """Handle voice activity state changes and update GUI"""
        if is_active:
            silence_frames = 0
            self._last_activity_time = time.time()
            if not voice_active:
                logging.info("Voice activity detected")
                voice_active = True
                self.gui.set_state('active')
        elif voice_active:
            silence_frames += 1
            if silence_frames > 10:
                logging.info("Voice activity stopped")
                voice_active = False
                silence_frames = 0
                self.gui.set_state('ready')
        return voice_active, silence_frames

    def _handle_billable_minute(self, is_active, current_time):
        """Manage billable minute state"""
        if is_active and not self._in_billable_minute and self.recording_enabled:
            self._minute_start_time = current_time
            self._in_billable_minute = True
            logging.debug("Starting new billable minute")
        elif self._in_billable_minute and current_time - self._minute_start_time >= 60:
            self._in_billable_minute = False
            if not is_active:
                logging.debug("Billable minute complete")

    async def _process_audio_chunk(self, data, websocket, voice_active, silence_frames):
        """Process a single chunk of audio data"""
        is_active = self.is_audio_active(data)
        current_time = time.time()
        
        # Handle recording state
        if not self.recording_enabled:
            is_active = False
            data = b'\x00' * (self.CHUNK * self.BYTES_PER_SAMPLE)
        elif not is_active and self._in_billable_minute:
            data = b'\x00' * (self.CHUNK * self.BYTES_PER_SAMPLE)
            
        # Update voice activity state
        voice_active, silence_frames = self._process_voice_activity(
            is_active, voice_active, silence_frames)
            
        # Handle billable minute
        self._handle_billable_minute(is_active, current_time)
        
        # Send audio data if in billable minute
        if self._in_billable_minute:
            audio_event = create_audio_event(data)
            await websocket.send(audio_event)
            
        return is_active, voice_active, silence_frames

    async def record_and_stream(self, websocket):
        """Main audio recording and streaming loop"""
        try:
            stream = self._init_audio_stream()
            if not stream or not stream.is_active():
                raise RuntimeError("Failed to initialize audio stream")
                
            consecutive_errors = 0
            voice_active = False
            silence_frames = 0
            
            while self.running and stream.is_active():
                try:
                    data = stream.read(self.CHUNK, exception_on_overflow=False)
                    if len(data) > 0:
                        _, voice_active, silence_frames = await self._process_audio_chunk(
                            data, websocket, voice_active, silence_frames)
                        consecutive_errors = 0
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


    async def receive_transcription(self, websocket):
        """Receive and process transcription results from websocket"""
        while self.running:
            try:
                response = await websocket.recv()
                # Only process messages if recording is enabled
                if not self.recording_enabled:
                    continue
                    
                header, payload = decode_event(response)
                logging.debug(f"Received message type: {header[':message-type']}")
                if header[":message-type"] == 'exception':
                    error_msg = payload['Message']
                    logging.error(f"AWS Exception: {error_msg}")
                    if "The security token included in the request is invalid" in error_msg:
                        logging.error("Invalid AWS credentials - exiting")
                        self.gui.show_notification(
                            "Scriba Error",
                            "Invalid AWS credentials - please check your configuration",
                            duration=5
                        )
                        self.running = False
                        break
                    elif "Your request timed out because no new audio was received" in error_msg:
                        logging.info("AWS Transcribe timeout detected")
                        self.gui.set_state('timeout')
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
                logging.info("Streaming completed successfully - waiting ...")
                logging.debug("Connection close code: normal closure")
                if self.running:
                    logging.info("Will attempt to reconnect...")
                break
                
            except websockets.exceptions.ConnectionClosedError as e:
                logging.error(f"WebSocket connection closed unexpectedly: {e}")
                logging.debug(f"Connection close code: {e.code}, reason: {e.reason}")
                break
                
            except Exception as e:
                logging.exception(f"Error in receive_transcription: {e}")
                break

    def _reset_connection_state(self):
        """Reset connection state after successful connection"""
        self._connection_attempts = 0
        self._consecutive_timeouts = 0
        self._backoff_time = 1
        self._in_billable_minute = False
        self._minute_start_time = 0

    def _handle_timeout(self):
        """Handle AWS Transcribe timeout with exponential backoff"""
        self._consecutive_timeouts += 1
        self._backoff_time = min(300, 2 ** self._consecutive_timeouts)  # Cap at 5 minutes
        logging.warning(f"Timeout detected (consecutive: {self._consecutive_timeouts}, "
                       f"next backoff: {self._backoff_time}s)")
        self.gui.set_state('timeout')

    async def _reinitialize_stream(self):
        """Reinitialize audio stream and connection state"""
        logging.info("Reinitializing audio stream and connection...")
        if hasattr(self, 'audio'):
            self.audio.terminate()
        self.audio = pyaudio.PyAudio()
        self._reset_connection_state()
        self._connection_attempts = 0  # Reset attempt counter
        await asyncio.sleep(self._backoff_time)

    def _generate_websocket_headers(self):
        """Generate WebSocket headers for connection"""
        websocket_key = ''.join(random.choices(string.ascii_uppercase + string.ascii_lowercase + string.digits, k=20))
        return {
            "Origin": "https://localhost",
            "Sec-Websocket-Key": websocket_key,
            "Sec-Websocket-Version": "13",
            "Connection": "keep-alive"
        }

    def _create_transcribe_url(self):
        """Create AWS Transcribe WebSocket URL"""
        transcribe_url_generator = AWSTranscribePresignedURL(
            access_key=self.access_key,
            secret_key=self.secret_key,
            session_token=self.session_token,
            region=self.region
        )
        
        return transcribe_url_generator.get_request_url(
            sample_rate=self.RATE,
            language_code=self._current_language,  # en-US or de-DE
            identify_language=False,
            identify_multiple_languages= False,
            language_options="",  # en-US,de-DE
            preferred_language="", # en-US
            media_encoding="pcm",
            number_of_channels=self.CHANNELS,
            enable_channel_identification=False,
            enable_partial_results_stabilization=True,
            partial_results_stability="medium"
        )

    async def _handle_websocket_tasks(self, websocket):
        """Handle WebSocket tasks for recording and transcription"""
        tasks = [
            asyncio.create_task(self.record_and_stream(websocket), name="record_stream"),
            asyncio.create_task(self.receive_transcription(websocket), name="transcription")
        ]
        logging.debug("Created tasks: %s", [t.get_name() for t in tasks])
        
        try:
            logging.debug("Starting task execution")
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            
            for task in tasks:
                if task.done() and not task.cancelled():
                    try:
                        exc = task.exception()
                        if exc:
                            if isinstance(exc, websockets.exceptions.ConnectionClosedOK):
                                logging.info("Normal connection closure - will reconnect")
                                for t in tasks:
                                    if not t.done():
                                        t.cancel()
                                await asyncio.sleep(1)
                                if self.running:
                                    self._in_billable_minute = False
                                    self._minute_start_time = 0
                                    return True
                            else:
                                logging.error(f"Task {task.get_name()} failed: {exc}")
                                raise exc
                    except asyncio.CancelledError:
                        pass
            return self.running
        finally:
            for task in tasks:
                if not task.done():
                    logging.debug(f"Cancelling task: {task.get_name()}")
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        logging.debug(f"Task {task.get_name()} cancelled")
                    except Exception as e:
                        logging.error(f"Error cancelling {task.get_name()}: {e}")

    async def connect_to_websocket(self):
        """Main connection loop with improved error handling"""
        attempt = 0
        while self.running:
            try:
                attempt += 1
                logging.info(f"Connecting to AWS Transcribe ... ")
                
                headers = self._generate_websocket_headers()
                request_url = self._create_transcribe_url()
                
                # Configure timeouts and retry logic
                connect_timeout = min(30, 5 * (attempt + 1))  # Increase timeout with each attempt
                async with websockets.connect(
                    request_url,
                    additional_headers=headers,
                    ping_timeout=None,
                    open_timeout=connect_timeout,
                    close_timeout=5,
                    max_size=2**20,  # 1MB max message size
                    compression=None  # Disable compression to reduce overhead
                ) as websocket:
                    logging.info("Connected to AWS Transcribe")
                    if self.language_switched:
                        self.gui.show_notification(
                            "Scriba",
                            "Language switched to " + self._current_language,
                            duration=1
                        )
                        self.language_switched = False
                    logging.debug(f"Starting transcription session with URL: {request_url}")
                    
                    try:
                        should_continue = await self._handle_websocket_tasks(websocket)
                        if should_continue:
                            continue
                    except websockets.exceptions.ConnectionClosedOK:
                        if not await self._handle_normal_closure(websocket):
                            break
                        continue
                    except websockets.exceptions.ConnectionClosedError as e:
                        if await self._handle_connection_error(e, attempt):
                            continue
                    
            except (TimeoutError, ConnectionRefusedError, OSError) as e:
                logging.error(f"Connection error: {e}")
                # Use exponential backoff with jitter for retries
                base_delay = min(300, 2 ** attempt)  # Cap at 5 minutes
                jitter = random.uniform(0, min(30, base_delay * 0.1))  # Add up to 10% jitter
                retry_delay = base_delay + jitter
                
                logging.info(f"Retrying in {retry_delay:.1f} seconds... (attempt {attempt})")
                # self.gui.show_notification(
                #     "Scriba",
                #     f"Connection failed. Retrying in {int(retry_delay)} seconds...",
                #     duration=2
                # )
                await asyncio.sleep(retry_delay)
                continue
                
            except Exception as e:
                if await self._handle_unexpected_error(e, attempt):
                    continue
                break

    async def _handle_normal_closure(self, websocket):
        """Handle normal WebSocket closure"""
        logging.info("Connection closed normally")
        
        if "timeout" in str(websocket.close_reason).lower():
            self._handle_timeout()
            await self._reinitialize_stream()
        else:
            self._reset_connection_state()
            await asyncio.sleep(1)
            
        if not self.running:
            logging.info("Application stopping - not reconnecting")
            return False
            
        logging.debug("Checking active tasks before reconnect")
        current_tasks = [t for t in asyncio.all_tasks() 
                        if t is not asyncio.current_task()]
        for task in current_tasks:
            logging.debug(f"Task {task.get_name()}: done={task.done()}, "
                         f"cancelled={task.cancelled()}, "
                         f"exception={task.exception() if task.done() else 'N/A'}")
        
        await asyncio.sleep(1)
        if self.running:
            logging.info("Attempting to reconnect after normal closure...")
            self._in_billable_minute = False
            self._minute_start_time = 0
            return True
        return False

    async def _handle_connection_error(self, error, attempt):
        """Handle WebSocket connection errors"""
        if not self.running:
            return False
            
        attempt += 1
        if attempt >= self._max_retries:
            logging.error(f"Failed to maintain connection after {self._max_retries} attempts - resetting counter")
            attempt = 0
            await asyncio.sleep(self._retry_delay * 2)
        else:
            logging.warning(f"Connection closed unexpectedly, retrying in {self._retry_delay} seconds... "
                          f"(attempt {attempt}/{self._max_retries}) ({error})")
            await asyncio.sleep(self._retry_delay)
        return True

    async def _handle_unexpected_error(self, error, attempt):
        """Handle unexpected errors during connection"""
        logging.exception(f"Unexpected error in connection: {error}")
        if attempt < self._max_retries:
            await asyncio.sleep(self._retry_delay * attempt)
            return True
        return False

    async def cleanup(self):
        """Clean up resources"""
        if not self.running:
            return
            
        self.running = False
        logging.info("Cleaning up resources...")
        
        # Shutdown logging queue if it exists
        if hasattr(self, 'log_queue'):
            try:
                self.log_queue.put(None)  # Signal logger thread to stop
                self.logger_thread.join(timeout=5.0)  # Wait for logger thread
            except Exception as e:
                print(f"Error shutting down logger: {e}")
        
        try:
            # Cancel all tasks except the current one
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            if tasks:
                logging.debug(f"Cancelling {len(tasks)} tasks")
                for task in tasks:
                    if not task.done():
                        task.cancel()
                        
                # Wait with timeout for tasks to cancel
                try:
                    await asyncio.wait(tasks, timeout=5.0)
                except asyncio.TimeoutError:
                    logging.warning("Timeout waiting for tasks to cancel")
                    
                # Force cancel any remaining tasks
                for task in tasks:
                    if not task.done():
                        logging.warning(f"Force cancelling task: {task.get_name()}")
                        task.cancel()
                        
        except Exception as e:
            logging.error(f"Error during task cleanup: {e}")
            
        try:
            if hasattr(self, 'audio'):
                self.audio.terminate()
                logging.debug("Audio resources terminated")
        except Exception as e:
            logging.error(f"Error terminating audio: {e}")
            
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

            self.gui.show_notification(
                "Scriba",
                f"Ready for {self._current_language} transcription in current window",
                duration=3
            )
            
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
                logging.debug(f"Cleanup: Found {len(pending)} pending tasks")
                if pending:
                    logging.debug(f"Cleanup: Task states before cancellation:")
                    for task in pending:
                        logging.debug(f"Task {task.get_name()}: done={task.done()}, cancelled={task.cancelled()}")
                        
                    # Only cancel tasks that aren't already done
                    active_tasks = [t for t in pending if not t.done()]
                    if active_tasks:
                        logging.debug(f"Cleanup: Cancelling {len(active_tasks)} active tasks")
                        for task in active_tasks:
                            task.cancel()
                            
                        # Wait for cancellation with timeout
                        try:
                            loop.run_until_complete(
                                asyncio.wait(active_tasks, timeout=5.0)
                            )
                            logging.debug("Cleanup: All tasks cancelled successfully")
                        except asyncio.TimeoutError:
                            logging.warning("Cleanup: Timeout waiting for tasks to cancel")
                        except Exception as e:
                            logging.error(f"Cleanup: Error during task cleanup: {e}")
            except Exception as e:
                logging.error(f"Error during shutdown: {e}")
            finally:
                logging.info("Closing event loop")
                loop.close()
                print("Scriba shutdown complete.")
    
    def toggle_recording(self, icon=None, item=None):
        """Toggle recording state when icon is clicked"""
        if item and item.text.startswith("Switch to"):
            new_lang = "de-DE" if self._current_language == "en-US" else "en-US"
            self._current_language = new_lang
            self.language_switched = True
            self.gui.current_language = new_lang
            self.gui.show_notification(
                "Scriba",
                f"Switching to {self._current_language} in the next cloud connection",
                duration=3
            )
            # Update menu text
            icon.menu = self.gui._create_menu(self.toggle_recording, self.stop)
        else:
            self.recording_enabled = not self.recording_enabled
            state = 'ready' if self.recording_enabled else 'disabled'
            self.gui.set_state(state)
            if not self.recording_enabled:
                self.gui.show_notification(
                    "Scriba",
                    f"Recording {state}",
                    duration=1
                )
            logging.info(f"Recording {state}")
        
    
    def stop(self):
        """Stop the voice transcription service"""
        try:
            # Create a new event loop for this thread
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            # Run cleanup synchronously since we're shutting down
            loop.run_until_complete(self.cleanup())
        finally:
            loop.close()
            self.gui.stop()
            print("\nScriba stopped.")

def main():
    
    # Parse command line arguments
    parser = argparse.ArgumentParser(description='Scriba - Real-time speech transcription')
    parser.add_argument('--language', default='en-US', help='Transcription language (default: en-US)')
    args = parser.parse_args()
    
    mutex_name = "Global\\ScribaSingleInstance"
    mutex = win32event.CreateMutex(None, False, mutex_name)
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        print("Another instance of Scriba is already running.")
        sys.exit(1)
        
    def signal_handler(signum, frame):
        print("\nPlease use Ctrl+Shift+X to stop Scriba properly.")
        print("Continuing...")
        
    import signal
    signal.signal(signal.SIGINT, signal_handler)
    
    try:
        scriba = Scriba()
        scriba._current_language = args.language  # Set initial language from command line
        scriba.gui = GUI(
            on_click_callback=scriba.toggle_recording,
            on_exit_callback=scriba.stop,
            language=args.language
        )
        scriba.gui.start()  # Start GUI after initialization
        scriba.start()
    finally:
        # Release the mutex when the program exitsMa. 
        if mutex:
            win32api.CloseHandle(mutex)

if __name__ == "__main__":
    main()
