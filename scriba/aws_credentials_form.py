import PySimpleGUI as sg
import os
import configparser
import pathlib

def prompt_aws_credentials(default_region='us-west-2'):
    """Show a form to collect AWS credentials and region"""
    
    sg.theme('DefaultNoMoreNagging')
    
    layout = [
        [sg.Text('AWS Credentials Required', font=('Helvetica', 12, 'bold'))],
        [sg.Text('Please enter your AWS credentials:')],
        [sg.Text('AWS Access Key ID:', size=(15, 1)), 
         sg.Input(key='access_key', size=(45, 1), right_click_menu=['&Right', ['Paste']])],
        [sg.Text('AWS Secret Key:', size=(15, 1)), 
         sg.Input(key='secret_key', size=(45, 1), password_char='*', right_click_menu=['&Right', ['Paste']])],
        [sg.Text('AWS Region:', size=(15, 1)), 
         sg.Input(default_text=default_region, key='region', size=(45, 1), right_click_menu=['&Right', ['Paste']])],
        [sg.Button('Save'), sg.Button('Cancel')]
    ]
    
    window = sg.Window('AWS Credentials', layout, finalize=True)
    window.bring_to_front()
    
    try:
        while True:
            event, values = window.read()
            
            if event == sg.WIN_CLOSED or event == 'Cancel':
                break
                
            if event == 'Paste':
                element = window.find_element_with_focus()
                if element:
                    element.Widget.event_generate('<<Paste>>')
                    
            if event == 'Save' and values['access_key'] and values['secret_key']:
                # Create config directories if they don't exist
                aws_dir = os.path.join(pathlib.Path.home(), '.aws')
                os.makedirs(aws_dir, exist_ok=True)
                
                # Save credentials
            config = configparser.ConfigParser()
            credentials_file = os.path.join(aws_dir, 'credentials')
            if os.path.exists(credentials_file):
                config.read(credentials_file)
            if 'default' not in config:
                config['default'] = {}
            config['default']['aws_access_key_id'] = values['access_key']
            config['default']['aws_secret_access_key'] = values['secret_key']
            with open(credentials_file, 'w') as f:
                config.write(f)
                
            # Save region config
            config = configparser.ConfigParser()
            config_file = os.path.join(aws_dir, 'config')
            if os.path.exists(config_file):
                config.read(config_file)
            if 'default' not in config:
                config['default'] = {}
            config['default']['region'] = values['region']
            with open(config_file, 'w') as f:
                config.write(f)
                
            return values['access_key'], values['secret_key'], values['region']
            
    finally:
        window.close()
        
    return None, None, None
