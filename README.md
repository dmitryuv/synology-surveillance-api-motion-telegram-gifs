# synology-surveillance-api-motion-telegram-gifs
A python script to create animated gifs from videos recorded by cameras attached to Synology Surveillance Station and send them via telegram bot API

Forked from [original project](https://github.com/dmitryuv/synology-surveillance-api-motion-telegram-gifs) with few key changes and small fixes:
1. Updated API version, tested only on DSM 7+ with Surveillance Station 9+.
2. Added support for per-camera telegram targets, including different bot tokens per my needs.
3. Added support for CMS configuration, where cameras are on different recording servers.
4. Improved video fetching logic - given 10sec polling interval, it ensures that video have at least skip+length seconds if still recording to avoid too short and truncated results.

Supports multiple cameras polling and ffmpeg parameters
Remembers already processed events across restarts.

## Config File

Needs a simple JSON based config file passed in on the command line.

For example:

```json
{
  "data_folder": "./data", // <-- Leave it empty if you are using docker image
  "ffmpeg_working_folder": "./gifs",
  "tele_bot_token": "XXX", // <-- default token for all cameras, can be empty if specified per camera
  "tele_chat_id": 123, // <-- default chat id for all cameras, can be empty if specified per camera
  "synology_base_api_url": "http://127.0.0.1",
  "synology_user": "admin",
  "synology_password": "password123",
  "synology_cameras": [
    {
      "id": 1, 
      "skip_first_n_secs": 5, //<-- Skip seconds recorded before motion event is triggered
      "max_length_secs": 5, //<-- Do not create gif for video full length but only with first n seconds
      "scale": 320, //<-- Determine quality and size of the output gif
      "tele_bot_token": "XXX", //<-- custom bot token for this camera
      "tele_chat_id": 123 //<-- custom chat id for this camera
    },
    {
      "id": 2,
      "skip_first_n_secs": 7,
      "max_length_secs": 10,
      "scale": 640,
      "topic_name": "camera_2"
    }
  ]
}

```
* `data_folder`: Path where to stored sqlite db for already processed events (preserve state across restarts). Leave empty if using docker image.
* `tele_bot_token`: Telegram Bot token, this is a default global setting, can be skipped when using per-camera setting
* `tele_chat_id`: Target chat id, this is a default global setting, can be skipped when using per-camera setting
* `ffmpeg_working_folder`: Working folder for downloaded mp4 videos and created GIFs
* `synology_base_api_url`: Base url of Synology Surveillance Station APIs
* `synology_user`: User to access Synology Surveillance Station APIs
* `synology_password`: User's password to access Synology Surveillance Station APIs
* `synology_cameras`: Array of cameras for events polling
    * `id`: Synology Surveillance Station camera id
    * `skip_first_n_secs`: Skip seconds recorded before motion event is triggered
    * `max_length_secs`: Do not create gif for video full length but only with first n seconds
    * `scale`: Determine quality and size of the output gif
    * `tele_bot_token`: Per-camera Telegram settings
    * `tele_chat_id`: Per-camera Telgram settings

If you don't know camera ids, leave cameras section empty and you'll get ids printed at first run
```
"synology_cameras": []
```
Example:
```
[INFO] (MainThread) Synology Info Camera Id 1 Name arzilla_veranda IP 192.168.1.87
[INFO] (MainThread) Synology Info Camera Id 2 Name arzilla_piazzale IP 192.168.1.88
[INFO] (MainThread) Synology Info Camera Id 3 Name arzilla_campo IP 192.168.1.148
[INFO] (MainThread) Synology Info Camera Id 4 Name arzilla_veranda_interno IP 192.168.1.126
```

## Installation

You need to build a docker image first if you want to run it in a container.

If you'd prefer to install dependencies yourself, you'll need:

* ffmpeg 4.0 (other versions probably work, but that's what I tested with)
* Python 3.9
* python libraries listed in `requirements.txt` (install via `pip install -r requirements.txt`)