"""MQTT 클라이언트 생성.

web 패키지를 import하지 않는다.
"""

from __future__ import annotations

import paho.mqtt.client as mqtt


def mqtt_client(client_id):
    """paho 버전에 맞는 MQTT 클라이언트를 만든다."""
    try:
        return mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    except (TypeError, AttributeError):
        return mqtt.Client(client_id=client_id)
