"""Python client for Ziggo Next."""
from .ziggonext import ZiggoNext
from .models import ZiggoRecordingSingle, ZiggoRecordingShow
from .ziggonextbox import ZiggoNextBox
from .const import ONLINE_RUNNING, ONLINE_STANDBY
from .exceptions import ZiggoNextAuthenticationError, ZiggoNextConnectionError