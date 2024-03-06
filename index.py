#!/bin/python3

from datetime import datetime

import os
import sqlite3
import time
import logging

import paho.mqtt.client as mqtt
import yaml
import sys
import json
import requests
import io

from datetime import datetime, timedelta
from PIL import Image, ImageDraw, UnidentifiedImageError, ImageFont
from thefuzz import fuzz
from thefuzz import process

mqtt_client = None
config = None
first_message = True
_LOGGER = None

VERSION = '1.3.4'

CONFIG_PATH = '/config/config.yml'
DB_PATH = '/config/frigate_plate_recogizer.db'
LOG_FILE = '/config/frigate_plate_recogizer.log'
SNAPSHOT_PATH = '/plates'

DATETIME_FORMAT = "%Y-%m-%d_%H-%M"

PLATE_RECOGIZER_BASE_URL = 'https://api.platerecognizer.com/v1/plate-reader'
DEFAULT_OBJECTS = ['car', 'motorcycle', 'bus']
CURRENT_EVENTS = {}

def run_daily():

    directory = SNAPSHOT_PATH

    # Today's date
    today = datetime.now().date()

    # File to store the last run date
    last_run_file = os.path.join(directory, "last_run.txt")

    # Read the last run date
    if os.path.exists(last_run_file):
        with open(last_run_file, "r") as file:
            last_run_data = file.read().strip()  # Strip whitespace from the read data
            try:
                last_run_date = datetime.strptime(last_run_data, '%Y-%m-%d').date()
            except ValueError:
                _LOGGER.error("Error parsing date from last_run.txt. Contents: '%s'", last_run_data)
                last_run_date = today - timedelta(days=1)
    else:
        last_run_date = today - timedelta(days=1)

    # Check if the function ran today
    if last_run_date < today:
        _LOGGER.debug("delete_old_images has not run today. Running now.")
        # Run the function
        delete_old_images()

        # Update the last run date
        with open(last_run_file, "w") as file:
            file.write(str(today))
    else:
            _LOGGER.debug("delete_old_images has already run today. Skipping.")

def delete_old_images():
    directory = SNAPSHOT_PATH
    try:
        days_to_keep = int(config['frigate'].get('days_of_snapshots', 30))
    except ValueError:
        _LOGGER.error("Invalid 'days_of_snapshots' value in config.yaml, must be an integer.")
        return

    if days_to_keep <= 0:
        _LOGGER.error("days_of_snapshots not in config.yaml or invalid, skipping delete_old_images!")
        return

    extension = 'png'
    current_time = time.time()
    threshold_time = current_time - days_to_keep * 86400  # seconds in a day
    num_files_to_delete = 0

    for filename in os.listdir(directory):
        if filename.endswith(extension):
            file_path = os.path.join(directory, filename)
            file_time = os.path.getmtime(file_path)

            if file_time < threshold_time:
                try:
                    os.remove(file_path)
                    num_files_to_delete += 1
                except Exception as e:
                    _LOGGER.error(f"Error deleting file {file_path}: {e}")

    if num_files_to_delete == 0:
        _LOGGER.info("File Cleanup: No files older than %d days found, deletion not required.", days_to_keep)
    else:
        _LOGGER.info(f"File Cleanup: Successfully deleted %d files older than %d days.", num_files_to_delete, days_to_keep)

def send_telegram_notification(image_name, image_path, plate_number, plate_score, original_plate_number, watched_plate):

    chat_id = config.get('telegram', {}).get('chat_id')
    token = config.get('telegram', {}).get('token')

    if plate_number:

        if watched_plate:
            plate_number = watched_plate
        else:
            plate_number = plate_number

        # Format plate numbers and score
        plate_number = plate_number.upper()
        original_plate_number = original_plate_number.upper()


        percent_score = f"{plate_score:.1%}" if plate_score is not None else "N/A"

        normalized_plate_number = plate_number.replace(" ", "").upper()
        normalized_original_plate_number = original_plate_number.replace(" ", "").upper()

        if normalized_plate_number == normalized_original_plate_number:
            message = f"Plate: {plate_number} Confidence: {percent_score}"
        else:
            message = f"Watched Plate: {plate_number} Confidence: {percent_score} Detected Plate: {original_plate_number}"

        # Decide whether to send a photo or a text message
        if config.get('telegram', {}).get('send_photo', False) and image_path:
            address = f'https://api.telegram.org/bot{token}/sendPhoto'
            with open(image_path, "rb") as image_file:
                files = {"photo": image_file}
                data = {"chat_id": chat_id, "caption": message}
                response = requests.post(address, files=files, data=data).json()
        else:
            url_req = f"https://api.telegram.org/bot{token}/sendMessage?chat_id={chat_id}&text={message}"
            response = requests.get(url_req).json()

        # Log the outcome
        if response.get("ok"):
            _LOGGER.debug("Sent to Telegram OK")
        else:
            _LOGGER.debug(f"Telegram error: {response}")

def on_connect(mqtt_client, userdata, flags, rc):
    _LOGGER.info("MQTT Connected")
    mqtt_client.subscribe(config['frigate']['main_topic'] + "/events")

def on_disconnect(mqtt_client, userdata, rc):
    if rc != 0:
        _LOGGER.warning("Unexpected disconnection, trying to reconnect")
        while True:
            try:
                mqtt_client.reconnect()
                break
            except Exception as e:
                _LOGGER.warning(f"Reconnection failed due to {e}, retrying in 60 seconds")
                time.sleep(60)
    else:
        _LOGGER.error("Expected disconnection")

def set_sublabel(frigate_url, frigate_event_id, sublabel, score):
    post_url = f"{frigate_url}/api/events/{frigate_event_id}/sub_label"
    _LOGGER.debug(f'sublabel: {sublabel}')
    _LOGGER.debug(f'sublabel url: {post_url}')

    # frigate limits sublabels to 20 characters currently
    if len(sublabel) > 20:
        sublabel = sublabel[:20]

    # Plates are always upper cased
    sublabel = str(sublabel).upper()

    # Submit the POST request with the JSON payload
    payload = { "subLabel": sublabel }
    headers = { "Content-Type": "application/json" }
    response = requests.post(post_url, data=json.dumps(payload), headers=headers)

    percent_score = "{:.1%}".format(score)

    # Check for a successful response
    if response.status_code == 200:
        _LOGGER.info(f"Sublabel set successfully to: {sublabel} with {percent_score} confidence")
    else:
        _LOGGER.error(f"Failed to set sublabel. Status code: {response.status_code}")

def code_project(image):
    api_url = config['code_project'].get('api_url')

    try:
        response = requests.post(api_url,files=dict(upload=image))
        response.raise_for_status()  # Raises an HTTPError for bad responses
        response = response.json()
        _LOGGER.debug(f"response: {response}")

        if response.get('predictions') is None:
            _LOGGER.error(f"Failed to get plate number. Response: {response}")
            return None, None, None, None

        if len(response['predictions']) == 0:
            _LOGGER.debug(f"No plates found")
            return None, None, None, None

        plate_number = response['predictions'][0].get('plate')
        score = response['predictions'][0].get('confidence')

        #get license plate coordinates
        plate_x_min = response['predictions'][0].get('x_min')
        plate_y_min = response['predictions'][0].get('y_min')
        plate_x_max = response['predictions'][0].get('x_max')
        plate_y_max = response['predictions'][0].get('y_max')

        watched_plate, watched_score, fuzzy_score = check_watched_plates(plate_number, response['predictions'])

        if fuzzy_score:
            return plate_number, score, watched_plate, fuzzy_score
        elif watched_plate:
            return plate_number, watched_score, watched_plate, None
        else:
            return plate_number, score, None, None

    except requests.exceptions.ConnectionError as e:
        _LOGGER.error("Connection failed! {}".format(e))
        return None, None, None, None
    except requests.exceptions.Timeout as e:
        # Handle Timeout errors
        _LOGGER.error("Request timed out: {}".format(e))
        return None, None, None, None
    except requests.exceptions.RequestException as e:
        # Handle ambiguous exception that occurred while handling your request
        _LOGGER.error("A requests exception occurred: {}".format(e))
        return None, None, None, None
    except Exception as e:
        _LOGGER.error("An error occurred: {}".format(e))
        return None, None, None, None

def plate_recognizer(image):
    api_url = config['plate_recognizer'].get('api_url') or PLATE_RECOGIZER_BASE_URL
    token = config['plate_recognizer']['token']

    try:
        response = requests.post(api_url,data=dict(regions=config['plate_recognizer']['regions']),files=dict(upload=image),headers={'Authorization': f'Token {token}'})
        response.raise_for_status()  # Raises an HTTPError for bad responses
        response = response.json()
        _LOGGER.debug(f"response: {response}")

        if response.get('results') is None:
            _LOGGER.error(f"Failed to get plate number. Response: {response}")
            return None, None, None, None

        if len(response['results']) == 0:
            _LOGGER.debug(f"No plates found")
            return None, None, None

        plate_number = response['results'][0].get('plate')
        score = response['results'][0].get('score')

        watched_plate, watched_score, fuzzy_score = check_watched_plates(plate_number, response['results'][0].get('candidates'))
        if fuzzy_score:
            return plate_number, score, watched_plate, fuzzy_score
        elif watched_plate:
            return plate_number, watched_score, watched_plate, None
        else:
            return plate_number, score, None, None

    except requests.exceptions.ConnectionError as e:
        _LOGGER.error("Connection failed! {}".format(e))
        return None, None, None, None
    except requests.exceptions.Timeout as e:
        # Handle Timeout errors
        _LOGGER.error("Request timed out: {}".format(e))
        return None, None, None, None
    except requests.exceptions.RequestException as e:
        # Handle ambiguous exception that occurred while handling your request
        _LOGGER.error("A requests exception occurred: {}".format(e))
        return None, None, None, None
    except Exception as e:
        _LOGGER.error("An error occurred: {}".format(e))
        return None, None, None, None

def check_watched_plates(plate_number, response):
    config_watched_plates = config['frigate'].get('watched_plates', [])
    if not config_watched_plates:
        _LOGGER.debug("Skipping checking Watched Plates because watched_plates is not set")
        return None, None, None

    config_watched_plates = [str(x).lower() for x in config_watched_plates] #make sure watched_plates are all lower case

    #Step 1 - test if top plate is a watched plate
    matching_plate = str(plate_number).lower() in config_watched_plates
    if matching_plate:
        _LOGGER.info(f"Recognised plate is a Watched Plate: {plate_number}")
        return None, None, None

    #Step 2 - test against AI candidates:
    for i, plate in enumerate(response):
        matching_plate = plate.get('plate') in config_watched_plates
        if matching_plate:
            if config.get('plate_recognizer'):
                score = plate.get('score')
            else:
                if i == 0: continue  #skip first response for CodeProjet.AI as index 0 = original plate.
                score = plate.get('confidence')
            _LOGGER.info(f"Watched plate found from AI candidates: {plate.get('plate')} with score {score}")
            return plate.get('plate'), score, None

    _LOGGER.debug("No Watched Plates found from AI candidates")

    #Step 3 - test against fuzzy match:
    fuzzy_match = config['frigate'].get('fuzzy_match', 0)

    if fuzzy_match == 0:
        _LOGGER.debug(f"Skipping fuzzy matching because fuzzy_match value not set in config")
        return None, None, None

    max_score = 0
    best_match = None
    for candidate in config_watched_plates:
        current_score = fuzz.ratio(str(plate_number).lower(), str(candidate).lower())
        if current_score > max_score:
            max_score = current_score
            best_match = candidate

    _LOGGER.debug(f"Best fuzzy_match: {best_match} ({max_score})")

    if max_score >= fuzzy_match:
        _LOGGER.info(f"Watched plate found from fuzzy matching: {best_match} with score {max_score}")
        return best_match, None, max_score

    _LOGGER.debug("No matching Watched Plates found.")
    #No watched_plate matches found
    return None, None, None

def send_mqtt_message(plate_number, plate_score, frigate_event_id, after_data, formatted_start_time, watched_plate, fuzzy_score):
    if not config['frigate'].get('return_topic'):
        return

    if watched_plate:
        message = {
            'plate_number': str(watched_plate).upper(),
            'score': plate_score,
            'frigate_event_id': frigate_event_id,
            'camera_name': after_data['camera'],
            'start_time': formatted_start_time,
            'fuzzy_score': fuzzy_score,
            'original_plate': str(plate_number).upper()
        }
    else:
        message = {
            'plate_number': str(plate_number).upper(),
            'score': plate_score,
            'frigate_event_id': frigate_event_id,
            'camera_name': after_data['camera'],
            'start_time': formatted_start_time
        }

    _LOGGER.debug(f"Sending MQTT message: {message}")

    main_topic = config['frigate']['main_topic']
    return_topic = config['frigate']['return_topic']
    topic = f'{main_topic}/{return_topic}'

    mqtt_client.publish(topic, json.dumps(message))

def has_common_value(array1, array2):
    return any(value in array2 for value in array1)

def save_image(config, after_data, frigate_url, frigate_event_id, watched_plate, plate_number, plate_score):

    if config['frigate'].get('always_save_snapshot', False):

        original_plate_number = plate_number

        snapshot = get_snapshot(frigate_event_id, frigate_url, False)
        if not snapshot:
            return

        image = Image.open(io.BytesIO(bytearray(snapshot)))

        # Save image
        timestamp = datetime.now().strftime(DATETIME_FORMAT)
        image_name = f"{after_data['camera']}_{timestamp}.png"
        image_path = f"{SNAPSHOT_PATH}/{image_name}"

        # Avoid a crash if the shared folder is no longer available
        try:
            image.save(image_path)
        except OSError as e:
            if "Key has been revoked" in str(e):
                _LOGGER.error("Failed to save image, likely due to the shared folder no longer being available: %s", image_path)
        else:
                _LOGGER.info(f"Saving image with path: {image_path}")

        # Only send a telegram message IF draw_box and crop_plate is false
        if config['frigate'].get('draw_box', False) is False and config['frigate'].get('crop_plate', False) is False:
            send_telegram_notification(image_name, image_path, plate_number, plate_score, original_plate_number, watched_plate)

    if config['frigate'].get('draw_box', False):

        if plate_number:

            original_plate_number = plate_number

            if watched_plate:
                plate_number = watched_plate
            else:
                plate_number = plate_number

            # get latest Event Data from Frigate API
            event_url = f"{frigate_url}/api/events/{frigate_event_id}"

            final_attribute = get_final_data(event_url)

            # get latest snapshot
            snapshot = get_snapshot(frigate_event_id, frigate_url, False)
            if not snapshot:
                return

            image = Image.open(io.BytesIO(bytearray(snapshot)))
            draw = ImageDraw.Draw(image)
            font = ImageFont.truetype("./Arial.ttf", size=14)

            if final_attribute:
                image_width, image_height = image.size

                # Coordinates from Frigate
                lic_plate = (
                    final_attribute[0]['box'][0]*image_width,
                    final_attribute[0]['box'][1]*image_height,
                    (final_attribute[0]['box'][0]+final_attribute[0]['box'][2])*image_width,
                    (final_attribute[0]['box'][1]+final_attribute[0]['box'][3])*image_height
                )

                draw.rectangle(lic_plate, outline="red", width=2)
                _LOGGER.debug(f"Drawing Plate Box: {lic_plate}")

                if plate_number:
                    draw.text(((final_attribute[0]['box'][0]*image_width)+5,((final_attribute[0]['box'][1]+final_attribute[0]['box'][3])*image_height)+5), str(plate_number).upper(), font=font)

            # Save image
            timestamp = datetime.now().strftime(DATETIME_FORMAT)
            image_name = f"{after_data['camera']}_{timestamp}.png"
            if plate_number:
                image_name = f"{str(plate_number).upper()}_{image_name}"
            image_path = f"{SNAPSHOT_PATH}/{image_name}"

            # Avoid a crash if the shared folder is no longer available
            try:
                image.save(image_path)
            except OSError as e:
                if "Key has been revoked" in str(e):
                    _LOGGER.error("Failed to save image, likely due to the shared folder no longer being available: %s", image_path)
                    return
            else:
                    _LOGGER.info(f"Saving image with path: {image_path}")

            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            # Now, update the has_image column for the row with last_row_id
            cursor.execute("""UPDATE plates SET has_image = ? WHERE id = ?""", (image_name, last_row_id))

            conn.commit()
            conn.close()

            send_telegram_notification(image_name, image_path, plate_number, plate_score, original_plate_number, watched_plate)

    # Crop license plate and insert it onto the saved snapshot
    if config['frigate'].get('crop_plate', False):

        if plate_number:

            original_plate_number = plate_number

            if watched_plate:
                plate_number = watched_plate
            else:
                plate_number = plate_number

            # get latest Event Data from Frigate API
            event_url = f"{frigate_url}/api/events/{frigate_event_id}"

            final_attribute = get_final_data(event_url)

            # get latest snapshot
            snapshot = get_snapshot(frigate_event_id, frigate_url, False)
            if not snapshot:
                return

            image = Image.open(io.BytesIO(bytearray(snapshot)))
            draw = ImageDraw.Draw(image)
            font = ImageFont.truetype("./Arial.ttf", size=14)

            margin = 10
            scale_top = config['frigate'].get('scale_top', 1.0)
            scale_middle = config['frigate'].get('scale_middle', 1.0)
            scale_bottom = config['frigate'].get('scale_bottom', 1.0)

            if final_attribute:
                image_width, image_height = image.size

                # Coordinates from Frigate
                lic_plate = (
                    final_attribute[0]['box'][0]*image_width,
                    final_attribute[0]['box'][1]*image_height,
                    (final_attribute[0]['box'][0]+final_attribute[0]['box'][2])*image_width,
                    (final_attribute[0]['box'][1]+final_attribute[0]['box'][3])*image_height
                )

                plate_coords = lic_plate
                plate = image.crop(plate_coords)

                plate_width, plate_height = plate.size
                image_height = image.size[1]

                third_of_image = image_height // 3

                if plate_coords[1] + plate_height < third_of_image:
                    # Plate is in the top third
                    scale_factor = scale_top
                    _LOGGER.debug(f"Top Selection Scale: {scale_factor}")
                elif plate_coords[1] > 2 * third_of_image:
                    # Plate is in the bottom Third
                    scale_factor = scale_bottom
                    _LOGGER.debug(f"Bottom Selection Scale: {scale_factor}")
                else:
                    scale_factor = scale_middle
                    _LOGGER.debug(f"Middle Selection Scale: {scale_factor}")

                new_plate_width = int(plate_width * scale_factor)
                new_plate_height = int(plate_height * scale_factor)
                new_plate_size = (new_plate_width, new_plate_height)
                resized_plate = plate.resize(new_plate_size, Image.Resampling.LANCZOS)

                original_width, original_height = image.size
                plate_width, plate_height = plate.size


                top_left = (margin, margin)
                top_right = (original_width - new_plate_width - margin, margin)
                bottom_left = (margin, original_height - new_plate_height - margin)
                bottom_right = (original_width - new_plate_width - margin, original_height - new_plate_height - margin)

                image = image.copy()

                crop_location = config['frigate'].get('crop_plate_location', 'bottom_right')

                location_mappings = {
                    'top_left': top_left,
                    'top_right': top_right,
                    'bottom_left': bottom_left,
                    'bottom_right': bottom_right
                }

                # Use the mapping to get the actual coordinates
                paste_location = location_mappings.get(crop_location)
                image.paste(resized_plate, paste_location)

            # Save image
            timestamp = datetime.now().strftime(DATETIME_FORMAT)
            image_name = f"{after_data['camera']}_{timestamp}.png"
            if plate_number:
                image_name = f"{str(plate_number).upper()}_{image_name}"
            image_path = f"{SNAPSHOT_PATH}/{image_name}"


            # Avoid a crash if the shared folder is no longer available
            try:
                image.save(image_path)
            except OSError as e:
                if "Key has been revoked" in str(e):
                    _LOGGER.error("Failed to save image, likely due to the shared folder no longer being available: %s", image_path)
                    return
            else:
                    _LOGGER.info(f"Saving image with path: {image_path}")

            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            # Now, update the has_image column for the row with last_row_id
            cursor.execute("""UPDATE plates SET has_image = ? WHERE id = ?""", (image_name, last_row_id))

            conn.commit()
            conn.close()

            send_telegram_notification(image_name, image_path, plate_number, plate_score, original_plate_number, watched_plate)

def check_first_message():
    global first_message
    if first_message:
        first_message = False
        _LOGGER.debug("Skipping first message")
        return True
    return False

def check_invalid_event(before_data, after_data):
    # check if it is from the correct camera or zone
    config_zones = config['frigate'].get('zones', [])
    config_cameras = config['frigate'].get('camera', [])

    matching_zone = any(value in after_data['current_zones'] for value in config_zones) if config_zones else True
    matching_camera = after_data['camera'] in config_cameras if config_cameras else True

    # Check if either both match (when both are defined) or at least one matches (when only one is defined)
    if not (matching_zone and matching_camera):
        _LOGGER.debug(f"Skipping event: {after_data['id']} because it does not match the configured zones/cameras")
        return True

    # check if it is a valid object
    valid_objects = config['frigate'].get('objects', DEFAULT_OBJECTS)
    if(after_data['label'] not in valid_objects):
        _LOGGER.debug(f"is not a correct label: {after_data['label']}")
        return True

    # limit api calls to plate checker api by only checking the best score for an event
    if(before_data['top_score'] == after_data['top_score'] and after_data['id'] in CURRENT_EVENTS) and not config['frigate'].get('frigate_plus', False):
        _LOGGER.debug(f"duplicated snapshot from Frigate as top_score from before and after are the same: {after_data['top_score']} {after_data['id']}")
        return True
    return False

def get_snapshot(frigate_event_id, frigate_url, cropped):
    _LOGGER.debug(f"Getting snapshot for event: {frigate_event_id}, Crop: {cropped}")
    snapshot_url = f"{frigate_url}/api/events/{frigate_event_id}/snapshot.jpg"
    _LOGGER.debug(f"event URL: {snapshot_url}")

    # get snapshot
    response = requests.get(snapshot_url, params={ "crop": cropped, "quality": 100 })

    # Check if the request was successful (HTTP status code 200)
    if response.status_code != 200:
        _LOGGER.error(f"Error getting snapshot: {response.status_code}")
        return

    return response.content

def get_cropped_snapshot(frigate_event_id, frigate_url):
    _LOGGER.debug(f"Getting snapshot for event: {frigate_event_id}")
    snapshot_url = f"{frigate_url}/api/events/{frigate_event_id}/snapshot.jpg"
    _LOGGER.debug(f"event URL: {snapshot_url}")

    # get snapshot
    response = requests.get(snapshot_url)
    image = Image.open(io.BytesIO(response.content))

    # get latest Event Data from Frigate API
    event_url = f"{frigate_url}/api/events/{frigate_event_id}"

    final_attribute = get_final_data(event_url)

    if final_attribute:
        image_width, image_height = image.size

        # Coordinates from Frigate
        lic_plate = (
            final_attribute[0]['box'][0]*image_width,
            final_attribute[0]['box'][1]*image_height,
            (final_attribute[0]['box'][0]+final_attribute[0]['box'][2])*image_width,
            (final_attribute[0]['box'][1]+final_attribute[0]['box'][3])*image_height
        )

        lic_plate_coords = lic_plate

    # Check if the request was successful (HTTP status code 200)
    if response.status_code != 200:
        _LOGGER.error(f"Error getting snapshot: {response.status_code}")
        return

    desired_width = 1000
    desired_height = 1000

    # Calculate current width and height
    current_width = lic_plate[2] - lic_plate[0]
    current_height = lic_plate[3] - lic_plate[1]

    # Calculate how much to expand (or contract) on each side
    expand_width = (desired_width - current_width) / 2
    expand_height = (desired_height - current_height) / 2

    # Adjust the bounding box, ensuring it does not go beyond image boundaries
    expanded_lic_plate = (
        max(lic_plate[0] - expand_width, 0),
        max(lic_plate[1] - expand_height, 0),
        min(lic_plate[2] + expand_width, image_width),
        min(lic_plate[3] + expand_height, image_height)
    )

    cropped_image = image.crop(expanded_lic_plate)

    # Convert the cropped image back to bytes
    cropped_image_bytes = io.BytesIO()
    cropped_image.save(cropped_image_bytes, format="PNG")
    cropped_image_bytes.seek(0)

    return cropped_image_bytes.getvalue()

def get_license_plate_attribute(after_data):
    if config['frigate'].get('frigate_plus', False):
        attributes = after_data.get('current_attributes', [])
        license_plate_attribute = [attribute for attribute in attributes if attribute['label'] == 'license_plate']
        return license_plate_attribute
    else:
        return None

def get_final_data(event_url):
    if config['frigate'].get('frigate_plus', False):
        response = requests.get(event_url)
        if response.status_code != 200:
            _LOGGER.error(f"Error getting final data: {response.status_code}")
            return
        event_json = response.json()
        event_data = event_json.get('data', {})

        if event_data:
            attributes = event_data.get('attributes', [])
            final_attribute = [attribute for attribute in attributes if attribute['label'] == 'license_plate']
            return final_attribute
        else:
            return None

    else:
        return None

def get_vehicle_data(event_url):
    response = requests.get(event_url)
    if response.status_code != 200:
        _LOGGER.error(f"Error getting final data: {response.status_code}")
        return
    event_json = response.json()
    event_data = event_json['data']['box']
    final_attribute = event_data

    return final_attribute

def is_valid_license_plate(after_data):
    # if user has frigate plus then check license plate attribute
    after_license_plate_attribute = get_license_plate_attribute(after_data)
    if not any(after_license_plate_attribute):
        _LOGGER.debug(f"no license_plate attribute found in event attributes")
        return False

    # check min score of license plate attribute
    license_plate_min_score = config['frigate'].get('license_plate_min_score', 0)
    if after_license_plate_attribute[0]['score'] < license_plate_min_score:
        _LOGGER.debug(f"license_plate attribute score is below minimum: {after_license_plate_attribute[0]['score']}")
        return False

    return True

def is_duplicate_event(frigate_event_id):
     # see if we have already processed this event
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""SELECT * FROM plates WHERE frigate_event = ?""", (frigate_event_id,))
    row = cursor.fetchone()
    conn.close()

    if row is not None:
        _LOGGER.debug(f"Skipping event: {frigate_event_id} because it has already been processed")
        return True

    return False

def get_plate(snapshot):
    # try to get plate number
    plate_number = None
    plate_score = None

    if config.get('plate_recognizer'):
        plate_number, plate_score , watched_plate, fuzzy_score = plate_recognizer(snapshot)
    elif config.get('code_project'):
        plate_number, plate_score, watched_plate, fuzzy_score = code_project(snapshot)
    else:
        _LOGGER.error("Plate Recognizer is not configured")
        return None, None, None, None

    # check Plate Recognizer score
    min_score = config['frigate'].get('min_score')
    score_too_low = min_score and plate_score and plate_score < min_score

    if not fuzzy_score and score_too_low:
        _LOGGER.info(f"Score is below minimum: {plate_score} ({plate_number})")
        return None, None, None, None

    return plate_number, plate_score, watched_plate, fuzzy_score

def store_plate_in_db(plate_number, plate_score, frigate_event_id, after_data, formatted_start_time):
    global last_row_id

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    _LOGGER.info(f"Storing plate number in database: {plate_number} with score: {plate_score}")

    cursor.execute("""INSERT INTO plates (detection_time, score, plate_number, frigate_event, camera_name) VALUES (?, ?, ?, ?, ?)""",
        (formatted_start_time, plate_score, plate_number, frigate_event_id, after_data['camera'])
    )
    # Retrieve last row ID
    last_row_id = cursor.lastrowid

    conn.commit()
    conn.close()

def on_message(client, userdata, message):
    if check_first_message():
        return

    # get frigate event payload
    payload_dict = json.loads(message.payload)
    _LOGGER.debug(f'mqtt message: {payload_dict}')

    before_data = payload_dict.get('before', {})
    after_data = payload_dict.get('after', {})
    type = payload_dict.get('type','')

    frigate_url = config['frigate']['frigate_url']
    frigate_event_id = after_data['id']

    if type == 'end' and after_data['id'] in CURRENT_EVENTS:
        _LOGGER.debug(f"CLEARING EVENT: {frigate_event_id} after {CURRENT_EVENTS[frigate_event_id]} calls to AI engine")
        del CURRENT_EVENTS[frigate_event_id]

    if check_invalid_event(before_data, after_data):
        return

    if is_duplicate_event(frigate_event_id):
        return

    frigate_plus = config['frigate'].get('frigate_plus', False)
    if frigate_plus and not is_valid_license_plate(after_data):
        return

    if not type == 'end' and not after_data['id'] in CURRENT_EVENTS:
        CURRENT_EVENTS[frigate_event_id] =  0

#    if config['frigate'].get('detector_crop', False):
#        snapshot = get_cropped_snapshot(frigate_event_id, frigate_url)
#    else:

    snapshot = get_snapshot(frigate_event_id, frigate_url, True)
    if not snapshot:
        # Check if the key exists in the dictionary before attempting to delete
        if frigate_event_id in CURRENT_EVENTS:
            del CURRENT_EVENTS[frigate_event_id]
        else:
            _LOGGER.debug(f"Key {frigate_event_id} not found in CURRENT_EVENTS.") 
        return

    _LOGGER.debug(f"Getting plate for event: {frigate_event_id}")
    if frigate_event_id in CURRENT_EVENTS:
        if config['frigate'].get('max_attempts', 0) > 0 and CURRENT_EVENTS[frigate_event_id] > config['frigate'].get('max_attempts', 0):
            _LOGGER.debug(f"Maximum number of AI attempts reached for event {frigate_event_id}: {CURRENT_EVENTS[frigate_event_id]}")
            return
        CURRENT_EVENTS[frigate_event_id] += 1

    plate_number, plate_score, watched_plate, fuzzy_score = get_plate(snapshot)
    if plate_number:
        start_time = datetime.fromtimestamp(after_data['start_time'])
        formatted_start_time = start_time.strftime("%Y-%m-%d %H:%M:%S")

        if watched_plate:
            store_plate_in_db(watched_plate, plate_score, frigate_event_id, after_data, formatted_start_time)
        else:
            store_plate_in_db(plate_number, plate_score, frigate_event_id, after_data, formatted_start_time)
        set_sublabel(frigate_url, frigate_event_id, watched_plate if watched_plate else plate_number, plate_score)

        send_mqtt_message(plate_number, plate_score, frigate_event_id, after_data, formatted_start_time, watched_plate, fuzzy_score)

    save_image(
        config=config,
        after_data=after_data,
        frigate_url=frigate_url,
        frigate_event_id=frigate_event_id,
        plate_score=plate_score,
        plate_number=plate_number,
        watched_plate=watched_plate
    )

    if config['frigate'].get('delete_old_images', False):
        run_daily()

def setup_db():
    conn = sqlite3.connect(DB_PATH)

    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS plates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            detection_time TIMESTAMP NOT NULL,
            score TEXT NOT NULL,
            plate_number TEXT NOT NULL,
            frigate_event TEXT NOT NULL UNIQUE,
            camera_name TEXT NOT NULL,
            has_image TEXT
        )
    """)
    conn.commit()
    conn.close()

def load_config():
    global config
    with open(CONFIG_PATH, 'r') as config_file:
        config = yaml.safe_load(config_file)

    if SNAPSHOT_PATH:
        if not os.path.isdir(SNAPSHOT_PATH):
            os.makedirs(SNAPSHOT_PATH)

def run_mqtt_client():
    global mqtt_client
    _LOGGER.info(f"Starting MQTT client. Connecting to: {config['frigate']['mqtt_server']}")
    now = datetime.now()
    current_time = now.strftime("%Y%m%d%H%M%S")

    # setup mqtt client
    mqtt_client = mqtt.Client("FrigatePlateRecognizer" + current_time)
    mqtt_client.on_message = on_message
    mqtt_client.on_disconnect = on_disconnect
    mqtt_client.on_connect = on_connect

    # check if we are using authentication and set username/password if so
    if config['frigate'].get('mqtt_username', False):
        username = config['frigate']['mqtt_username']
        password = config['frigate'].get('mqtt_password', '')
        mqtt_client.username_pw_set(username, password)

    mqtt_client.connect(config['frigate']['mqtt_server'])
    mqtt_client.loop_forever()

def load_logger():
    global _LOGGER
    _LOGGER = logging.getLogger(__name__)
    _LOGGER.setLevel(config.get('logger_level', 'INFO'))

    # Create a formatter to customize the log message format
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    # Create a console handler and set the level to display all messages
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    console_handler.setFormatter(formatter)

    # Create a file handler to log messages to a file
    file_handler = logging.FileHandler(LOG_FILE)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    # Add the handlers to the logger
    _LOGGER.addHandler(console_handler)
    _LOGGER.addHandler(file_handler)

def main():
    load_config()
    setup_db()
    load_logger()

    current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
    _LOGGER.info(f"Time: {current_time}")
    _LOGGER.info(f"Python Version: {sys.version}")
    _LOGGER.info(f"Frigate ALPR Version: {VERSION}")
    _LOGGER.info(f"Telegram Support")
    _LOGGER.debug(f"config: {config}")

    if config.get('plate_recognizer'):
        _LOGGER.info(f"Using Plate Recognizer API")
    else:
        _LOGGER.info(f"Using CodeProject.AI API")


    run_mqtt_client()

if __name__ == '__main__':
    main()
