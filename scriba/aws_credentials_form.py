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
         sg.Input(key='access_key', size=(45, 1))],
        [sg.Text('AWS Secret Key:', size=(15, 1)), 
         sg.Input(key='secret_key', size=(45, 1), password_char='*')],
        [sg.Text('AWS Region:', size=(15, 1)), 
         sg.Input(default_text=default_region, key='region', size=(45, 1))],
        [sg.Button('Save'), sg.Button('Cancel')]
    ]
    
    window = sg.Window('AWS Credentials', layout, finalize=True)
    window.bring_to_front()
    
    try:
        event, values = window.read()
        
        if event == 'Save' and values['access_key'] and values['secret_key']:
            # Create config directories if they don't exist
            aws_dir = os.path.join(pathlib.Path.home(), '.aws')
            os.makedirs(aws_dir, exist_ok=True)
            
            # Save credentials
            config = configparser.ConfigParser()
            config['default'] = {
                'aws_access_key_id': values['access_key'],
                'aws_secret_access_key': values['secret_key']
            }
            with open(os.path.join(aws_dir, 'credentials'), 'w') as f:
                config.write(f)
                
            # Save region config
            config = configparser.ConfigParser()
            config['default'] = {
                'region': values['region']
            }
            with open(os.path.join(aws_dir, 'config'), 'w') as f:
                config.write(f)
                
            return values['access_key'], values['secret_key'], values['region']
            
    finally:
        window.close()
        
    return None, None, None
