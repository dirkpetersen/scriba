import threading, logging
from PIL import Image, ImageDraw
import pystray

class GUI:
    def __init__(self, on_click_callback=None, on_exit_callback=None, language="en-US"):
        self.icon = None
        self.on_click_callback = on_click_callback
        self.on_exit_callback = on_exit_callback
        self.current_language = language
        self._create_icon()
        
    def _create_base_image(self, color):
        """Create a colored circle icon"""
        width = 64
        height = 64
        image = Image.new('RGB', (width, height), color='black')
        dc = ImageDraw.Draw(image)
        dc.ellipse([4, 4, width-4, height-4], fill=color)
        return image
        
    def _create_menu(self, toggle_handler, exit_handler):
        """Create the system tray menu"""
        return pystray.Menu(
            pystray.MenuItem(
                text="Toggle Recording",
                action=toggle_handler,
                default=True,
                visible=True
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
        
        self.icon = pystray.Icon(
            name='scriba',
            icon=image,
            title="Scriba (Ready)",
            menu=self._create_menu(toggle_handler, exit_handler)
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
            'timeout': 'Scriba (Cloud Disconnected)'
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

    
