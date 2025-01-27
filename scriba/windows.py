import logging
import time
import ctypes
from ctypes import wintypes
import win32con
import win32gui
import win32api
import win32process

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

# Constants
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002

# Initialize user32
user32 = ctypes.WinDLL('user32', use_last_error=True)
user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT

def send_keystrokes_win32(text):
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
