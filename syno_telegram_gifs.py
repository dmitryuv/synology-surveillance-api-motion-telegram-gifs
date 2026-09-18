#!/usr/bin/env python3
import datetime
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
import telebot
from sqlite3 import Error
from telebot import apihelper
from telebot.apihelper import ApiTelegramException

import requests
# disable SSL exceptions
from requests.packages.urllib3.exceptions import InsecureRequestWarning
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)


logging.basicConfig(level=logging.INFO,
                    format='[%(asctime)s] [%(levelname)s] (%(threadName)-10s) %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S')

# SYNO.API urls
logged_in = False
synoApiLoginUrl = "{}/webapi/auth.cgi?api=SYNO.API.Auth&method=login&version=6" \
                  "&session=SurveillanceStation&format=cookie&account={}&passwd={}"
synoApiCamerasInfoUrl = "{}/webapi/entry.cgi?api=SYNO.SurveillanceStation.Camera&method=List&version=1&_sid={}"
synoApiEventQueryUrl = "{}/webapi/entry.cgi?api=SYNO.SurveillanceStation.Recording&method=List" \
                       "&version=5&locked=0&dualRecModeList=0&limit=1&cameraIds={}&evtSrcType={}&evtSrcId={}&_sid={}"
synoApiEventDownloadUrl = "{}/webapi/entry.cgi?api=SYNO.SurveillanceStation.Recording" \
                          "&method=Download&version=6&id={}&_sid={}"
synoApiCMSEventDownloadUrl = "{}/webapi/entry.cgi?api=SYNO.SurveillanceStation.CMS&method=Redirect&version=1&dsId={}&webAPI={}&isDownloadFile=true&_sid={}"
synoApiCMSRedirectArgs = "{{\"api\":\"SYNO.SurveillanceStation.Recording\",\"version\":6,\"method\":\"Download\",\"id\":{}}}"
synoApiEventDownloadUrlPartial = "{}/webapi/entry.cgi?api=SYNO.SurveillanceStation.Recording" \
                                 "&method=Download&version=6&id={}&offsetTimeMs={}&playTimeMs={}&_sid={}"

# extra playback seconds appended after skip_first_n_secs + max_length_secs when downloading a slice
DOWNLOAD_MARGIN_SECS = 4

# sync API authentication error codes returned for an expired/invalid session
SYNO_AUTH_ERROR_CODES = {105, 106, 107, 119}

DOWNLOAD_CHUNK_SIZE = 4096
DOWNLOAD_PROGRESS_SCALE = 50

# output video height in lines (e.g. 480 = 480p); width is derived from aspect ratio
DEFAULT_VIDEO_MAX_HEIGHT = 480
# x264 encoding preset for mp4 delivery
DEFAULT_VIDEO_PRESET = "veryfast"
X264_PRESETS = {"ultrafast", "superfast", "veryfast", "faster", "fast",
                "medium", "slow", "slower", "veryslow", "placebo"}


def normalize_video_height(video_max_height):
    try:
        height = int(video_max_height)
    except (TypeError, ValueError):
        logging.warning('Invalid video_max_height %r, using default %i',
                        video_max_height, DEFAULT_VIDEO_MAX_HEIGHT)
        height = DEFAULT_VIDEO_MAX_HEIGHT
    if height <= 0:
        logging.warning('Invalid video_max_height %r, using default %i',
                        video_max_height, DEFAULT_VIDEO_MAX_HEIGHT)
        height = DEFAULT_VIDEO_MAX_HEIGHT
    return max(2, height // 2 * 2)  # yuv420p needs even dimensions

sql_create_processed_events_table = """ CREATE TABLE IF NOT EXISTS processed_events (
                                        id integer PRIMARY KEY,
                                        camera_id text NOT NULL,
                                        last_event_id int NOT NULL,
                                        processed_date timestamp NOT NULL
                                    ); """

sql_create_processed_events_table_unique = """ CREATE UNIQUE INDEX IF NOT EXISTS idx_processed_events_camera ON processed_events (camera_id); """


def parse_config(config_path):
    with open(config_path, 'r') as config_file:
        config_data = json.load(config_file)
    return config_data


def create_connection(data_folder):
    try:
        conn = sqlite3.connect(data_folder + '/processed_events.db')
        logging.info('SQLite version %s', sqlite3.version)
        return conn
    except Error as e:
        logging.error("CANNOT CREATE DB", e)

    return None


def create_processed_events_table(conn):
    try:
        c = conn.cursor()
        c.execute(sql_create_processed_events_table)
        c.execute(sql_create_processed_events_table_unique)
    except Error as e:
        logging.error("CANNOT CREATE TABLE", e)


def is_event_processed(conn, camera_id, event_id):
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM processed_events WHERE camera_id=? AND last_event_id>=? LIMIT 1",
                (camera_id, event_id))

    return cur.fetchone() is not None


def replace_processed_events(conn, processed_event):
    sql = ''' INSERT INTO processed_events(camera_id, last_event_id, processed_date)
              VALUES(?,?,?)
              ON CONFLICT(camera_id) DO UPDATE SET
                last_event_id=excluded.last_event_id,
                processed_date=excluded.processed_date '''
    cur = conn.cursor()
    cur.execute(sql, processed_event)

    conn.commit()


def syno_login(base_url, user, password):
    login_response = requests.get(synoApiLoginUrl.format(base_url, user, password), verify=False)
    logging.info('login_response status_code %s', login_response.status_code)

    if login_response.ok:
        login_data = json.loads(login_response.content.decode('utf-8'))
        if login_data["success"]:
            return login_data["data"]["sid"]
        else:
            return ""

    else:
        login_response.raise_for_status()


def syno_info(base_url, sid):
    info_response = requests.get(synoApiCamerasInfoUrl.format(base_url, sid), verify=False)

    if info_response.ok:
        info_data = json.loads(info_response.content.decode('utf-8'))
        return info_data

    else:
        info_response.raise_for_status()


def syno_last_event(base_url, camera_id, camera_time, srcType, srcId, sid):
    global logged_in

    event_response = requests.get(synoApiEventQueryUrl.format(base_url, camera_id, srcType, srcId, sid),
                                  verify=False)

    if event_response.ok:
        event_data = json.loads(event_response.content.decode('utf-8'))

        if not event_data["success"]:
            err_code = event_data["error"]["code"]
            # handle auth failure and exit to re-authenticate
            if err_code in SYNO_AUTH_ERROR_CODES:
                logged_in = False
            return -1, 0

        if len(event_data["data"]["events"]) == 0:
            return -1, 0

        event_rec = event_data["data"]["events"][0]
        rec_time = event_rec["stopTime"] - event_rec["startTime"]

        if event_rec["cameraId"] == camera_id and (event_rec["recording"] == False or (event_rec["recording"] == True and rec_time >= camera_time)):
            return event_rec["id"], event_rec["dsId"]
    else:
        event_response.raise_for_status()

    return -1, 0


def syno_download_video(download_dir, base_url, event_id, event_ds_id, sid):
    outfile_mp4 = '{}/{}.mp4'.format(download_dir, event_id)

    with open(outfile_mp4, "wb") as f:
        logging.info('Downloading video for event id %i to %s .....', event_id, outfile_mp4)

        if event_ds_id > 0:
            redirect_args = synoApiCMSRedirectArgs.format(event_id)
            uri = synoApiCMSEventDownloadUrl.format(base_url, event_ds_id, redirect_args, sid)
        else:
            uri = synoApiEventDownloadUrl.format(base_url, event_id, sid)

        download_response = requests.get(uri, verify=False, stream=True)

        if download_response.ok:
            total_length = download_response.headers.get('content-length')

            if total_length is None:  # no content length header
                f.write(download_response.content)
            else:
                dl = 0
                total_length = int(total_length)
                for data in download_response.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                    dl += len(data)
                    f.write(data)
                    done = int(DOWNLOAD_PROGRESS_SCALE * dl / total_length)
                    sys.stdout.flush()
            logging.info('Downloading video for event id %i to %s .....DONE', event_id, outfile_mp4)
            return outfile_mp4

        else:
            download_response.raise_for_status()


def syno_download_video_partial(download_dir, base_url, event_id, event_ds_id, sid, play_time_ms, offset_time_ms=0):
    outfile_mp4 = '{}/{}.mp4'.format(download_dir, event_id)

    with open(outfile_mp4, "wb") as f:
        logging.info('Downloading partial video for event id %i (offset %ims, play %ims) to %s .....',
                     event_id, offset_time_ms, play_time_ms, outfile_mp4)

        if event_ds_id > 0:
            redirect_args = json.dumps({
                "api": "SYNO.SurveillanceStation.Recording",
                "version": 6,
                "method": "Download",
                "id": event_id,
                "offsetTimeMs": offset_time_ms,
                "playTimeMs": play_time_ms
            }, separators=(',', ':'))
            uri = synoApiCMSEventDownloadUrl.format(base_url, event_ds_id, redirect_args, sid)
        else:
            uri = synoApiEventDownloadUrlPartial.format(base_url, event_id, offset_time_ms, play_time_ms, sid)

        download_response = requests.get(uri, verify=False, stream=True)

        if download_response.ok:
            total_length = download_response.headers.get('content-length')

            if total_length is None:  # no content length header
                f.write(download_response.content)
            else:
                dl = 0
                total_length = int(total_length)
                for data in download_response.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                    dl += len(data)
                    f.write(data)
                    done = int(DOWNLOAD_PROGRESS_SCALE * dl / total_length)
                    sys.stdout.flush()
            logging.info('Downloading partial video for event id %i to %s .....DONE', event_id, outfile_mp4)
            return outfile_mp4

        else:
            download_response.raise_for_status()


def convert_video_gif(video_max_height, skip_first_n_secs, max_length_secs, input_video, output_gif, fps=15):
    height = normalize_video_height(video_max_height)
    logging.info('convert_video_gif video_max_height %i skip_first_n_secs %i max_length_secs %i fps %i input_video %s output_gif %s',
                 height, skip_first_n_secs, max_length_secs, fps, input_video, output_gif)

    seek_to = datetime.timedelta(seconds=skip_first_n_secs)

    retcode = subprocess.call([
        "ffmpeg", "-loglevel", "warning", "-y",
        # input option -ss: keyframe fast seek (frame-accurate, may clip one frame at the start)
        "-ss", str(seek_to),
        "-i", input_video,
        "-vf", "fps={},scale=-2:min(ih\\,{}):flags=bilinear".format(fps, height),
        "-vsync", "vfr",
        "-t", "{}".format(max_length_secs),
        str(output_gif)
    ])
    return retcode


def convert_video_to_mp4(video_max_height, skip_first_n_secs, max_length_secs, input_video, output_video, fps=15, preset=DEFAULT_VIDEO_PRESET):
    height = normalize_video_height(video_max_height)
    if preset not in X264_PRESETS:
        logging.warning('Invalid video_preset %r, using default %s', preset, DEFAULT_VIDEO_PRESET)
        preset = DEFAULT_VIDEO_PRESET
    logging.info('convert_video_to_mp4 video_max_height %i skip_first_n_secs %i max_length_secs %i fps %i preset %s input_video %s output_video %s',
                 height, skip_first_n_secs, max_length_secs, fps, preset, input_video, output_video)

    seek_to = datetime.timedelta(seconds=skip_first_n_secs)

    retcode = subprocess.call([
        "ffmpeg", "-loglevel", "warning", "-y",
        "-ss", str(seek_to),
        "-i", input_video,
        "-vf", "fps={},scale=-2:min(ih\\,{}):flags=bilinear".format(fps, height),
        "-c:v", "libx264", "-preset", preset, "-crf", "30",
        "-pix_fmt", "yuv420p", "-an",
        "-movflags", "+faststart",
        "-t", "{}".format(max_length_secs),
        str(output_video)
    ])
    return retcode


class CameraMotionEventHandler:
    def __init__(self, processed_events_conn, base_url, camera, config, sid):
        self.base_url = base_url
        self.camera = camera
        self.config = config
        self.sid = sid
        self.processed_events_conn = processed_events_conn

    def publish_telegram_message(self, gif):
        max_attempts = 3  # maximum number of retries
        for attempt in range(1, max_attempts + 1):
            try:
                if not "bot" in self.camera:
                    logging.error("Camera %s does not have bot configured", self.camera["id"])
                    return True

                tb = self.camera["bot"]
                chat_id = self.camera["tele_chat_id"]
                tb.send_chat_action(chat_id, 'upload_video')

                with open(gif, "rb") as fb:
                    tb.send_animation(chat_id, fb, disable_notification=True, caption=self.camera["name"])

                # remove file on success
                os.remove(gif)
                return True

            except ApiTelegramException as e:
                if e.error_code == 429:  # "Too Many Requests"
                    retry_after = e.result_json['parameters']['retry_after']
                    logging.warning("Telegram rate limit hit (attempt %d/%d). Retrying in %s seconds...", 
                                   attempt, max_attempts, retry_after)
                    time.sleep(retry_after)
                else:
                    logging.error("Telegram API exception: %s", e)
                    return False

            except Exception as e:
                logging.error("General exception: %s", e)
                return False

        logging.error("Failed to send after %d attempts", max_attempts)
        return False


    def publish_video_telegram_message(self, video, caption):
        max_attempts = 3  # maximum number of retries
        for attempt in range(1, max_attempts + 1):
            try:
                if not "bot" in self.camera:
                    logging.error("Camera %s does not have bot configured", self.camera["id"])
                    return True

                tb = self.camera["bot"]
                chat_id = self.camera["tele_chat_id"]
                tb.send_chat_action(chat_id, 'upload_video')

                with open(video, "rb") as fb:
                    tb.send_video(chat_id, fb, disable_notification=True, caption=caption)

                # remove file on success
                os.remove(video)
                return True

            except ApiTelegramException as e:
                if e.error_code == 429:  # "Too Many Requests"
                    retry_after = e.result_json['parameters']['retry_after']
                    logging.warning("Telegram rate limit hit (attempt %d/%d). Retrying in %s seconds...",
                                   attempt, max_attempts, retry_after)
                    time.sleep(retry_after)
                else:
                    logging.error("Telegram API exception: %s", e)
                    return False

            except Exception as e:
                logging.error("General exception: %s", e)
                return False

        logging.error("Failed to send after %d attempts", max_attempts)
        return False


    def poll_event(self):
        camera_time = self.camera["skip_first_n_secs"] + self.camera["max_length_secs"]
        camera_id = self.camera["id"]
        event_id, event_ds_id = syno_last_event(self.base_url, camera_id, camera_time, self.camera["srcType"], self.camera["dsId"], self.sid)
        if event_id > -1:
            if is_event_processed(self.processed_events_conn, camera_id, event_id):
                return None, None

            logging.info('Start downloading event video for event_id %i, camera_id %i', event_id, camera_id)
            play_time_ms = (camera_time + DOWNLOAD_MARGIN_SECS) * 1000
            mp4_file = syno_download_video_partial(self.config["ffmpeg_working_folder"], self.base_url, event_id, event_ds_id, self.sid, play_time_ms)

            outdir = self.config["ffmpeg_working_folder"]
            delivery = self.config.get("format", "mp4").lower()
            fps = self.camera.get("fps", 15)
            video_max_height = self.camera.get("video_max_height", DEFAULT_VIDEO_MAX_HEIGHT)
            preset = self.config.get("video_preset", DEFAULT_VIDEO_PRESET)

            if delivery == "gif":
                outfile = '{}/{}.gif'.format(outdir, event_id)
                convert_retcode = convert_video_gif(video_max_height,
                                                    self.camera["skip_first_n_secs"],
                                                    self.camera["max_length_secs"],
                                                    mp4_file, outfile, fps=fps)
            else:
                outfile = '{}/{}_video.mp4'.format(outdir, event_id)
                convert_retcode = convert_video_to_mp4(video_max_height,
                                                       self.camera["skip_first_n_secs"],
                                                       self.camera["max_length_secs"],
                                                       mp4_file, outfile, fps=fps, preset=preset)
            if os.path.exists(mp4_file):
                os.remove(mp4_file)
            if convert_retcode == 0:
                if delivery == "gif":
                    tele_retcode = self.publish_telegram_message(outfile)
                else:
                    tele_retcode = self.publish_video_telegram_message(outfile, self.camera["name"])
                if tele_retcode:
                    processed_event = (camera_id, event_id, datetime.datetime.now());
                    replace_processed_events(self.processed_events_conn, processed_event)
                    logging.info('Done processing event_id  %i, camera_id %i', event_id, camera_id)
                else:
                    logging.error('Invalid return code from telegram send for event_id %i, camera_id %i', event_id, camera_id)
            else:
                logging.error('Invalid return code from ffmpeg subprocess call for event id %i', event_id)


def _configure_camera(config, camera_info):
    for camera in config["synology_cameras"]:
        if camera["id"] == camera_info["id"]:
            camera["dsId"] = camera_info["ownerDsId"]
            camera["name"] = camera_info["name"]
            # delete active handler during re-auth
            camera.pop("handler", None)

            if int(camera["dsId"]) > 0:
                camera["srcType"] = 2  # type 2 - recording server
            else:
                camera["srcType"] = 0  # type 0 - host server

            if "tele_bot_token" in camera:
                camera["bot"] = telebot.TeleBot(camera["tele_bot_token"])
            elif "tele_bot_token" in config:
                if not "bot" in config:
                    config["bot"] = telebot.TeleBot(config["tele_bot_token"])
                camera["bot"] = config["bot"]

            if not "tele_chat_id" in camera and "tele_chat_id" in config:
                camera["tele_chat_id"] = config["tele_chat_id"]
            break


def main():
    global logged_in

    _, config_filename = sys.argv
    logging.info('Starting')
    logging.info('Parsing %s', config_filename)
    config = parse_config(config_filename)

    if "proxy" in config and config["proxy"]:
        proxy_string = config["proxy"]
        apihelper.proxy = {'https': proxy_string}
        logging.info("Proxy set: %s", proxy_string)

    if "tele_base_url" in config and config["tele_base_url"]:
        apihelper.API_URL = config["tele_base_url"] + "/bot{0}/{1}"
        logging.info("Telegram base URL set: %s", config["tele_base_url"])

    config_data_folder = ''
    if 'data_folder' in config:
        config_data_folder = config["data_folder"]
    if config_data_folder == '':
        config_data_folder = "/data"

    logging.info('Creating/Opening processed_events database on file %s', config_data_folder)
    processed_events_conn = create_connection(config_data_folder)
    if processed_events_conn is not None:
        # create processed_events table
        create_processed_events_table(processed_events_conn)
    else:
        logging.error('Error! cannot create the database connection.')
        return

    try:
        while True:
            time.sleep(10)
            if not logged_in:
                sid = syno_login(config["synology_base_api_url"], config["synology_user"], config["synology_password"])
                if sid == "":
                    logging.error('Synology credentials not valid')
                    continue
                else:
                    logged_in = True
                    logging.info('Synology Auth ok')
                    info_data = syno_info(config["synology_base_api_url"], sid)
                    for camera_info in info_data["data"]["cameras"]:
                        logging.warning('Synology Info Camera Id %i Name %s IP %s DsID %s',
                                        camera_info["id"], camera_info["name"],
                                        camera_info["host"], camera_info["ownerDsId"])
                        _configure_camera(config, camera_info)

            for camera in config["synology_cameras"]:
                if not "handler" in camera:
                    logging.info('CameraMotionEventHandler created poll_event %s', camera["id"])
                    camera["handler"] = CameraMotionEventHandler(processed_events_conn, config["synology_base_api_url"],
                                                                 camera,
                                                                 config, sid)
                camera["handler"].poll_event()

    except KeyboardInterrupt:
        logging.info('KeyboardInterrupt')

    logging.info('Ending')


if __name__ == "__main__":
    main()