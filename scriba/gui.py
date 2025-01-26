import threading, time, logging
from PIL import Image, ImageDraw
import pystray
import win32gui, win32con

class GUI:
    def __init__(self, on_click_callback=None, on_exit_callback=None, on_language_callback=None):
        self.icon = None
        self.on_click_callback = on_click_callback
        self.on_exit_callback = on_exit_callback
        self.on_language_callback = on_language_callback
        self.current_language = "en-US"
        self._create_icon()
        
    def _create_base_image(self, color):
        """Create a colored circle icon"""
        width = 64
        height = 64
        image = Image.new('RGB', (width, height), color='black')
        dc = ImageDraw.Draw(image)
        dc.ellipse([4, 4, width-4, height-4], fill=color)
        return image
        
    def _create_menu(self, toggle_handler, language_handler, exit_handler):
        """Create the system tray menu"""
        return pystray.Menu(
            pystray.MenuItem(
                text="Toggle Recording",
                action=toggle_handler,
                default=True,
                visible=True
            ),
            pystray.MenuItem(
                text="Switch to English" if self.current_language == "de-DE" else "Switch to German",
                action=language_handler
            ),
            pystray.MenuItem(
                text="Exit",
                action=exit_handler
            )
        )

    def _create_icon(self):
        """Initialize the system tray icon"""
        image = self._create_base_image('yellow')
        
        def toggle_handler(icon, item):
            """Handle toggle recording clicks"""
            if self.on_click_callback:
                self.on_click_callback()
                
        def exit_handler(icon, item):
            """Handle exit menu clicks"""
            if self.on_exit_callback:
                self.on_exit_callback()
            icon.stop()
        
        def language_handler(icon, item):
            """Handle language selection"""
            if self.on_language_callback:
                new_lang = "de-DE" if self.current_language == "en-US" else "en-US"
                self.current_language = new_lang
                self.on_language_callback(new_lang)
                # Update menu with new text
                icon.menu = self._create_menu(toggle_handler, language_handler, exit_handler)
                
        self.icon = pystray.Icon(
            name='scriba',
            icon=image,
            title="Scriba (Ready)",
            menu=self._create_menu(toggle_handler, language_handler, exit_handler)
        )
        
    def start(self):
        """Start the icon in the system tray"""
        threading.Thread(target=self.icon.run, daemon=True).start()
        
    def stop(self):
        """Remove the icon from the system tray"""
        if self.icon:
            self.icon.stop()
            
    def set_state(self, state):
        """Update icon color based on state"""
        colors = {
            'ready': 'yellow',
            'active': 'green',
            'disabled': 'red',
            'timeout': 'orange'
        }
        tooltips = {
            'ready': 'Scriba (Ready)',
            'active': 'Scriba (Active)', 
            'disabled': 'Scriba (Disabled)',
            'timeout': 'Scriba (Connection Timeout)'
        }
        
        if state in colors:
            try:
                self.icon.icon = self._create_base_image(colors[state])
                self.icon.title = tooltips[state]
            except Exception as e:
                logging.error(f"Failed to update icon state: {e}")

    def show_notification(self, title, message, duration=3):
        """Show a Windows notification"""
        try:
            if self.icon:
                self.icon.notify(message, title)
        except Exception as e:
            logging.error(f"Failed to show notification: {e}")

    def show_notification_new(self, title, message, duration=3):
        """
        Shows a notification in the Windows system tray.
        
        Args:
            title (str): The title of the notification
            message (str): The message content
            timeout (int): How long the notification should remain visible in seconds
        """
        # Window class name
        wc = win32gui.WNDCLASS()
        wc.lpszClassName = 'PythonTaskbar'
        wc.lpfnWndProc = {
            win32con.WM_DESTROY: lambda hwnd, msg, wparam, lparam: win32gui.PostQuitMessage(0)
        }
        
        # Register the window class
        try:
            win32gui.RegisterClass(wc)
            # Create the window
            hwnd = win32gui.CreateWindow(
                wc.lpszClassName,
                'Notification',
                win32con.WS_OVERLAPPED | win32con.WS_SYSMENU,
                0, 0, win32con.CW_USEDEFAULT, win32con.CW_USEDEFAULT,
                0, 0, 0, None
            )
            
            # Create and show the notification
            icon_flags = win32con.LR_LOADFROMFILE | win32con.LR_DEFAULTSIZE
            hicon = win32gui.LoadIcon(0, win32con.IDI_APPLICATION)
            
            nid = (
                hwnd,
                0,
                win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP | win32gui.NIF_INFO,
                win32con.WM_USER + 20,
                hicon,
                'Tooltip',
                message,
                duration * 1000,
                title
            )
            
            # Add the notification to the system tray
            win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, nid)
            
            # Wait for the timeout
            time.sleep(duration)
            
            # Remove the notification
            win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, nid)
            
            # Destroy the window
            win32gui.DestroyWindow(hwnd)
            
        finally:
            # Unregister the window class
            win32gui.UnregisterClass(wc.lpszClassName, None)
