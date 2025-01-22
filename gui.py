from PIL import Image, ImageDraw
import pystray
import threading
import logging

class GUI:
    def __init__(self, on_click_callback=None):
        self.icon = None
        self.on_click_callback = on_click_callback
        self._create_icon()
        
    def _create_base_image(self, color):
        """Create a colored circle icon"""
        width = 64
        height = 64
        image = Image.new('RGB', (width, height), color='black')
        dc = ImageDraw.Draw(image)
        dc.ellipse([4, 4, width-4, height-4], fill=color)
        return image
        
    def _create_icon(self):
        """Initialize the system tray icon"""
        image = self._create_base_image('yellow')
        
        def handler(icon, item):
            """Handle menu item clicks"""
            if self.on_click_callback:
                self.on_click_callback()
        
        self.icon = pystray.Icon(
            name='envoicer',
            icon=image,
            title="Envoicer (Ready)",
            menu=pystray.Menu(
                pystray.MenuItem(
                    text="Toggle Recording",
                    action=handler,
                    default=True,
                    visible=True
                )
            )
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
            'disabled': 'red'
        }
        tooltips = {
            'ready': 'Envoicer (Ready)',
            'active': 'Envoicer (Active)',
            'disabled': 'Envoicer (Disabled)'
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
