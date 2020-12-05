"""Python client for Ziggo Next."""
import json
from logging import Logger
from paho.mqtt.client import Client
import paho.mqtt.client as mqtt
import random
import time
import sys, traceback

import requests
from .models import ZiggoNextSession, ZiggoChannel, ZiggoRecordingSingle, ZiggoRecordingShow
from .ziggonextbox import ZiggoNextBox
from .exceptions import ZiggoNextConnectionError, ZiggoNextAuthenticationError

from .const import (
    BOX_PLAY_STATE_BUFFER,
    BOX_PLAY_STATE_CHANNEL,
    BOX_PLAY_STATE_DVR,
    BOX_PLAY_STATE_REPLAY,
    ONLINE_RUNNING,
    ONLINE_STANDBY,
    UNKNOWN,
    MEDIA_KEY_PLAY_PAUSE,
    MEDIA_KEY_CHANNEL_DOWN,
    MEDIA_KEY_CHANNEL_UP,
    MEDIA_KEY_POWER,
    COUNTRY_URLS_HTTP,
    COUNTRY_URLS_MQTT,
    COUNTRY_URLS_PERSONALIZATION_FORMAT
)

DEFAULT_PORT = 443

def _makeId(stringLength=10):
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(random.choice(letters) for i in range(stringLength))


class ZiggoNext:
    """Main class for handling connections with Ziggo Next Settop boxes."""
    logger: Logger
    session: ZiggoNextSession
    def __init__(self, username: str, password: str, country_code: str = "nl") -> None:
        """Initialize connection with Ziggo Next"""
        self.username = username
        self.password = password
        self.token = None
        self.session = None
        self.logger = None
        self.settop_boxes = {}
        self.channels = {}
        self._country_code = country_code
        self.channels = {}
        baseUrl = COUNTRY_URLS_HTTP[self._country_code]
        self._api_url_session =  baseUrl + "/session"
        self._api_url_token =  baseUrl + "/tokens/jwt"
        self._api_url_channels =  baseUrl + "/channels"
        self._api_url_recordings = baseUrl + "/networkdvrrecordings"

    def authenticate(self):
        payload = {"username": self.username, "password": self.password}
        try:
            response = requests.post(self._api_url_session, json=payload)
        except (Exception):
            raise ZiggoNextConnectionError("Unknown connection failure")
        if not response.ok:
            status = response.json()
            if status[0]['code'] == 'invalidCredentials':
                raise ZiggoNextAuthenticationError("Invalid credentials")
            raise ZiggoNextConnectionError("Authentication error: " + status)


    def get_session(self):
        """Get Ziggo Next Session information"""
        payload = {"username": self.username, "password": self.password}
        try:
            response = requests.post(self._api_url_session, json=payload)
        except (Exception):
            raise ZiggoNextConnectionError("Unknown connection failure")

        if not response.ok:
            status = response.json()
            self.logger.debug(status)
            if status[0]['code'] == 'invalidCredentials':
                raise ZiggoNextAuthenticationError("Invalid credentials")
            raise ZiggoNextConnectionError("Connection failed: " + status)
        else:
            session = response.json()
            self.logger.debug(session)
            self.session = ZiggoNextSession(
                session["customer"]["householdId"], session["oespToken"]
            )

    def get_session_and_token(self):
        """Get session and token from Ziggo Next"""
        self.get_session()
        self._get_token()

    def _register_settop_boxes(self):
        """Get settopxes"""
        jsonResult = self._do_api_call(self._api_url_settop_boxes)
        for box in jsonResult:
            if not box["platformType"] == "EOS":
                continue
            box_id = box["deviceId"]
            self.settop_boxes[box_id] = ZiggoNextBox(box_id, box["settings"]["deviceFriendlyName"], self.session.householdId, self.token, self._country_code, self.logger, self.mqttClient, self.mqttClientId)

    def _on_mqtt_client_connect(self, client, userdata, flags, resultCode):
        """Handling mqtt connect result"""
        if resultCode == 0:
            client.on_message = self._on_mqtt_client_message
            self.logger.debug("Connected to mqtt client.")
            self.mqttClientConnected = True
            for box_key in self.settop_boxes.keys():
                self.settop_boxes[box_key].register()

        elif resultCode == 5:
            self.logger.debug("Not authorized mqtt client. Retry to connect")
            client.username_pw_set(self.session.householdId, self.token)
            client.connect(self._mqtt_broker, DEFAULT_PORT)
            client.loop_start()
        else:
            raise Exception("Could not connect to Mqtt server")

    def _on_mqtt_client_disconnect(self, client, userdata, resultCode):
        """Set state to diconnect"""
        self.logger.debug(f"Disconnected from mqtt client: {resultCode}")
        self.mqttClientConnected = False

    def _on_mqtt_client_message(self, client, userdata, message):
        """Handles messages received by mqtt client"""
        jsonPayload = json.loads(message.payload)
        deviceId = jsonPayload["source"]
        self.logger.debug(jsonPayload)
        if "deviceType" in jsonPayload and jsonPayload["deviceType"] == "STB":
           self.settop_boxes[deviceId]._update_settopbox_state(jsonPayload)
        if "status" in jsonPayload:
            self.settop_boxes[deviceId].update_settop_box(jsonPayload)

    def _do_api_call(self, url, tries = 0):
        """Executes api call and returns json object"""
        if tries > 9:
            raise ZiggoNextConnectionError("API call failed. See previous errors.")
        headers = {
            "X-OESP-Token": self.session.oespToken,
            "X-OESP-Username": self.username,
        }
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            return response.json()
        elif response.status_code == 403:
            self.logger.warning(f"Api call resultcode was 403. Refreshing token en trying again...")
            self.get_session()
            tries+=1
            return self._do_api_call(url, tries) 
        else:
            raise ZiggoNextConnectionError("API call failed: " + str(response.status_code))
    
    def _get_token(self):
        """Get token from Ziggo Next"""
        jsonResult = self._do_api_call(self._api_url_token)
        self.token = jsonResult["token"]
        self.logger.debug("Fetched a token: %s", jsonResult)
        
    def connect(self, logger, enableMqttLogging: bool = False):
        """Get token and start mqtt client for receiving data from Ziggo Next"""
        self._mqtt_broker = COUNTRY_URLS_MQTT[self._country_code]
        self.logger = logger
        self.get_session_and_token()
        self._api_url_settop_boxes =  COUNTRY_URLS_PERSONALIZATION_FORMAT[self._country_code].format(household_id=self.session.householdId)
        self.mqttClientId = _makeId(30)
        self.mqttClient = mqtt.Client(self.mqttClientId, transport="websockets")
        if enableMqttLogging:
            self.mqttClient.enable_logger(logger)
        self.mqttClient.username_pw_set(self.session.householdId, self.token)
        self.mqttClient.tls_set()
        self.mqttClient.on_connect = self._on_mqtt_client_connect
        self.mqttClient.on_disconnect = self._on_mqtt_client_disconnect
        self.mqttClient.connect(self._mqtt_broker, DEFAULT_PORT)
        self._register_settop_boxes()
        self.load_channels()
        self.mqttClient.loop_start()

    def _send_key_to_box(self, box_id: str, key: str):
        self.settop_boxes[box_id].send_key_to_box(key)

    def select_source(self, source, box_id):
        """Changes te channel from the settopbox"""
        channel = [src for src in self.channels.values() if src.title == source][0]
        self.settop_boxes[box_id].set_channel(channel.serviceId)

    def pause(self, box_id):
        """Pauses the given settopbox"""
        box = self.settop_boxes[box_id]
        if box.state == ONLINE_RUNNING and not box.info.paused:
            self._send_key_to_box(box_id, MEDIA_KEY_PLAY_PAUSE)

    def play(self, box_id):
        """Resumes the settopbox"""
        box = self.settop_boxes[box_id]
        if box.state == ONLINE_RUNNING and box.info.paused:
            self._send_key_to_box(box_id, MEDIA_KEY_PLAY_PAUSE)

    def next_channel(self, box_id):
        """Select the next channel for given settop box."""
        box = self.settop_boxes[box_id]
        if box.state == ONLINE_RUNNING:
            self._send_key_to_box(box_id, MEDIA_KEY_CHANNEL_UP)

    def previous_channel(self, box_id):
        """Select the previous channel for given settop box."""
        box = self.settop_boxes[box_id]
        if box.state == ONLINE_RUNNING:
            self._send_key_to_box(box_id, MEDIA_KEY_CHANNEL_DOWN)

    def turn_on(self, box_id):
        """Turn the settop box on."""
        box = self.settop_boxes[box_id]
        if box.state == ONLINE_STANDBY:
            self._send_key_to_box(box_id, MEDIA_KEY_POWER)

    def turn_off(self, box_id):
        """Turn the settop box off."""
        box = self.settop_boxes[box_id]
        if box.state == ONLINE_RUNNING:
           self._send_key_to_box(box_id, MEDIA_KEY_POWER)
           box.turn_off()

    def is_available(self, box_id):
        box = self.settop_boxes[box_id]
        state = box.state
        return (state == ONLINE_RUNNING or state == ONLINE_STANDBY)

    def load_channels(self):
        """Refresh channels list for now-playing data."""
        response = requests.get(self._api_url_channels)
        if response.status_code == 200:
            content = response.json()

            for channel in content["channels"]:
                station = channel["stationSchedules"][0]["station"]
                serviceId = station["serviceId"]
                streamImage = None
                channelImage = None
                for image in station["images"]:
                    if image["assetType"] == "imageStream":
                        streamImage = image["url"]
                    if image["assetType"] == "station-logo-small":
                        channelImage =  image["url"]

                self.channels[serviceId] = ZiggoChannel(
                    serviceId,
                    channel["title"],
                    streamImage,
                    channelImage,
                    channel["channelNumber"],
                )
            self.channels["NL_000073_019506"] = ZiggoChannel(
                "NL_000073_019506",
                "Netflix",
                None,
                None,
                "150"
            )

            self.channels["NL_000074_019507"] = ZiggoChannel(
                "NL_000074_019507",
                "Videoland",
                None,
                None,
                "151"
            )
            self.logger.debug("Updated channels.")
            for box in self.settop_boxes.values():
                box.channels = self.channels
        else:
            self.logger.error("Can't retrieve channels...")

    def get_recordings(self):
        results = []
        json_result = self._do_api_call(self._api_url_recordings)
        recordings = json_result["recordings"]
        for recording in recordings:
            if recording["type"] == "single":
                results.append(self._get_single_recording(recording))
            elif recording["type"] == "season":
                results.append(self._get_show_recording_summary(recording, "parentMediaGroupId"))
            elif recording["type"] == "show":
                results.append(self._get_show_recording_summary(recording, "mediaGroupId"))

        return results
    
    def _get_single_recording(self, payload):
        recording = ZiggoRecordingSingle(payload["recordingId"], payload["title"], payload["images"][0]["url"])
        if "seasonNumber" in payload:
            recording.set_season(payload["seasonNumber"])
        else:
            recording.set_season(None)
        if "episodeNumber" in payload:
            recording.set_episode(payload["episodeNumber"])
        else:
            recording.set_episode(None)
        return {
            "type": "recording",
            "recording": recording
        }
    
    def get_show_recording(self, media_group_id):
        show_url = self._api_url_recordings + f"?byMediaGroupIdForShow={media_group_id}&sort=startTime%7CASC"
        show_payload = self._do_api_call(show_url)
        
        recordings = show_payload["recordings"]
        example_recording = recordings[0]
        if "numberOfEpisodes" not in example_recording:
            example_recording["numberOfEpisodes"] = 0
        show_recording = ZiggoRecordingShow(media_group_id, example_recording["showTitle"], example_recording["numberOfEpisodes"], example_recording["images"][0]["url"])
        for recording in recordings:
            show_recording.append_child(self._get_single_recording(recording))
        return {
            "type": "show",
            "show": show_recording
        }

    def _get_show_recording_summary(self, recording_payload, group_id):
        show_recording = ZiggoRecordingShow(recording_payload[group_id], recording_payload["title"],recording_payload["numberOfEpisodes"],  recording_payload["images"][0]["url"])
        return {
            "type": "show",
            "show": show_recording
        }
    
    def play_recording(self, box_id, recording_id):
        self.settop_boxes[box_id].play_recording(recording_id)

    def disconnect(self):
        if not self.mqttClientConnected:
            return
        self.mqttClient.disconnect()
