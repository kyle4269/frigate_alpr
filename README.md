# Frigate ALPR

**This project is no longer being developed due to LPR being included in Frigate 0.16.**

Identify license plates via [Plate Recognizer](https://guides.platerecognizer.com/) or [CodeProject.AI](https://www.codeproject.com/) and add them as sublabels to [blakeblackshear/frigate](https://github.com/blakeblackshear/frigate)

**The core foundation of this code is based on the exceptional work found in  [Frigate_Plate_recognizer](https://github.com/ljmerza/frigate_plate_recognizer). I have contributed by integrating my own modifications and enhancements to further refine its functionality.**


## Setup

Create a `config.yml` file in your docker volume with the following contents:

```yml
frigate:
  frigate_url: http://127.0.0.1:5000
  mqtt_server: 127.0.0.1
  mqtt_port: 1883 # Optional. Default shown.
  mqtt_username: username
  mqtt_password: password
  main_topic: frigate
  return_topic: frigate_alpr
  frigate_plus: false
  camera:
    - driveway_camera
  objects:
    - car
  min_score: .8
plate_recognizer:
  token: xxxxxxxxxx
  regions: 
    - us-ca
logger_level: INFO
```

Update your frigate url, mqtt server settings. If you are using mqtt authentication, update the username and password. Update the camera name(s) to match the camera name in your frigate config. Add your Plate Recognizer API key and region(s).

You'll need to make an account (free) [here](https://app.platerecognizer.com/accounts) and get an API key. You get up to 2,500 lookups per month for free. You will also need to enable car object detection for the cameras you want to use this with. See [here](https://guides.platerecognizer.com/docs/snapshot/getting-started/) on how to locally host Plate Recognizer.

You can specify a custom url for the plate_recognizer api by adding `api_url` to your config:

```yml
plate_recognizer:
  api_url: http://HOST-IP:8080/v1/plate-reader
  token: xxxxxxxxxx
  regions: 
    - us-ca
```

You can also filter by zones and/or cameras. If you want to filter by zones, add `zones` to your config:

```yml
frigate:
  # ...
  zones:
    - front_door
    - back_door
```

If no objects are speficied in the Frigate options, it will default to `[motorcycle, car, bus]`.

If you have a custom model with Frigate+ then it's able to detect license plates via an event's attributes, you can set `frigate_plus` to `true` in your config to activate this feature:

```yaml
frigate:
  # ... 
  frigate_plus: true
  license_plate_min_score: 0 # default is show all but can speficify a min score from 0 - 1 for example 0.8
  max_attempts: 20 # Optional: if set, will limit the number of snapshots sent for recognition for any particular event. 
```

If you're using CodeProject.AI, you'll need to comment out plate_recognizer in your config. Then add and update "api_url" with your CodeProject.AI Service API URL. Your config should look like:

```yml
#plate_recognizer:
#  token: xxxxxxxxxx
#  regions: 
#    - us-ca
code_project:
  api_url: http://127.0.0.1:32168/v1/image/alpr
```

## Debugging

set `logger_level` in your config to `DEBUG` to see more logging information:

```yml
logger_level: DEBUG
```

Logs will be in `/config/frigate_alpr.log`

## Save Snapshot Images to Path

If you want frigate-plate-recognizer to automatically save snapshots of recognized plates, add the following to your config.yml:

```yml
frigate:
  draw_box: True # Optional - Draws a box around the plate on the snapshot along with the license plate text (Required Frigate plus setting)
  always_save_snapshot: True # Optional - will save a snapshot of every event sent to frigate_alpr, even if no plate is detected
```

Snapshots will be saved into the '/plates' directory within your container - to access them directly, map an additional volume within your docker-compose, e.g.:

## Running
```bash
docker run -v /path/to/config:/config /path/to/plates:/plates:rw -e TZ=America/New_York -it --rm --name frigate_alpr kyle4269/frigate_alpr:latest
```
or using docker-compose:
```yml
services:
  frigate_alpr:
    image: kyle4269/frigate_alpr:latest
    container_name: frigate_alpr
    volumes:
      - /path/to/config:/config
      - /path/to/plates:/plates:rw
    restart: unless-stopped
    environment:
      - TZ=America/New_York
```

## Crop License Plate

If you want to crop the license plate and have it added the the saved snapshot, add the following to your config.yaml:
```yml
frigate:
  crop_plate: True
  crop_plate_location: bottom_right # Options are : top_left, top_right, bottom_left and bottom_right
  scale_top: 3.5 # If detected in the top third of the image, this will make the cropped license plate bigger or smaller on the saved snapshot.
  scale_middle: 3.0 # If detected in the middle of the image, this will make the cropped license plate bigger or smaller on the saved snapshot.
  scale_bottom: 2.0 # If detected in the bottom third of the image, this will make the cropped license plate bigger or smaller on the saved snapshot.
```

## Auto Delete Saved Snapshots
**DISCLAIMER:**

**PLEASE READ!!**

**THIS WILL DELETE SNAPSHOTS!!**

**INCLUDING ANY EXISTING SNAPSHOTS THAT ARE OLDER THAN THE SPECIFIED NUMBER OF DAYS (days_of_snapshots)!!**

**NOTE: This runs once a day and will add a file "last_run.txt" to your snapshot directory.**

If you want to only keep X days of snapshots, add the following to your config.yaml:
```yml
frigate:
  delete_old_snapshots: True
  days_of_snapshots: 30 # Default if you don't set your own.
```

## Telegram Support

If you want to send the saved snapshot to telegram, add the following to your config.yaml:

```yml
telegram:
  token: "XXXXXXX"
  chat_id: "XXXXXXXX"
  send_photo: True # Setting this to False will still send a message to Telegram with the Plate Number and Score.
```

## Monitor Watched Plates

If you want frigate-plate-recognizer to check recognized plates against a list of watched plates for close matches (including fuzzy recognition), add the following to your config.yml:

```yml
frigate:
  watched_plates: #list of plates to watch.
    -  ABC123
    -  DEF456
  fuzzy_match: 80 # default is test against plate-recognizer / CP.AI 'candidates' only, but can specify a min score for fuzzy matching if no candidates match watched plates from 0 - 100 for example 80
```

If a watched plate is found in the list of candidates plates returned by plate-recognizer / CP.AI, the response will be updated to use that plate and its score. The original plate will be added to the MQTT response as an additional `original_plate` field.

If no candidates match and fuzzy_match is enabled with a value, the recognized plate is compared against each of the watched_plates using fuzzy matching. If a plate is found with a score > fuzzy_match, the response will be updated with that plate. The original plate and the associated fuzzy_score will be added to the MQTT response as additional fields `original_plate` and `fuzzy_score`.

## Flask Web App

Additionally, I've developed a simple Web Application using Flask to complement Frigate_ALPR. For those interested in integrating this web interface, please explore [Frigate_ALPR_Web](https://github.com/kyle4269/frigate_alpr_web) for more details and setup instructions.

## Home Assistant Blueprint

This blueprint lets you to designate a specific license plate for monitoring, allowing you to receive alerts directly on your mobile device whenever it is detected.

[![Open your Home Assistant instance and show the blueprint import dialog with a specific blueprint pre-filled.](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fkyle4269%2Ffrigate_alpr%2Fblob%2Fmain%2Fblueprint%2Falert_plates.yaml)
