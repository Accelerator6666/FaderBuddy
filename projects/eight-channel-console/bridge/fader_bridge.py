#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import paho.mqtt.client as mqtt
import requests
import yaml

LOG = logging.getLogger("fader-bridge")


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def pos_to_unit(pos: int) -> float:
    return clamp(int(pos), 0, 255) / 255.0


def unit_to_pos(v: float) -> int:
    return int(round(clamp(float(v), 0.0, 1.0) * 255.0))


class Adapter:
    def set_position(self, position: int) -> None:
        raise NotImplementedError

    def get_position(self) -> Optional[int]:
        return None

    def double_tap(self) -> None:
        pass

    def close(self) -> None:
        pass


class HomeAssistantLightAdapter(Adapter):
    def __init__(self, base_url: str, token: str, entity_id: str, timeout: float = 3.0):
        self.base_url = base_url.rstrip("/")
        self.entity_id = entity_id
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })

    def set_position(self, position: int) -> None:
        p = int(clamp(position, 0, 255))
        if p == 0:
            service = "turn_off"
            payload = {"entity_id": self.entity_id}
        else:
            service = "turn_on"
            payload = {"entity_id": self.entity_id, "brightness": p, "transition": 0}
        r = self.session.post(
            f"{self.base_url}/api/services/light/{service}",
            json=payload,
            timeout=self.timeout,
        )
        r.raise_for_status()

    def get_position(self) -> Optional[int]:
        r = self.session.get(
            f"{self.base_url}/api/states/{self.entity_id}",
            timeout=self.timeout,
        )
        r.raise_for_status()
        state = r.json()
        if state.get("state") == "off":
            return 0
        brightness = state.get("attributes", {}).get("brightness")
        return int(clamp(brightness if brightness is not None else 255, 0, 255))

    def double_tap(self) -> None:
        current = self.get_position()
        service = "turn_on" if current == 0 else "turn_off"
        r = self.session.post(
            f"{self.base_url}/api/services/light/{service}",
            json={"entity_id": self.entity_id},
            timeout=self.timeout,
        )
        r.raise_for_status()


class WindowsMasterAdapter(Adapter):
    def __init__(self):
        if os.name != "nt":
            raise RuntimeError("windows_master requires Windows")
        from ctypes import POINTER, cast
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        device = AudioUtilities.GetSpeakers()
        interface = device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        self.volume = cast(interface, POINTER(IAudioEndpointVolume))

    def set_position(self, position: int) -> None:
        self.volume.SetMasterVolumeLevelScalar(pos_to_unit(position), None)

    def get_position(self) -> Optional[int]:
        return unit_to_pos(self.volume.GetMasterVolumeLevelScalar())

    def double_tap(self) -> None:
        self.volume.SetMute(not bool(self.volume.GetMute()), None)


class WindowsSessionAdapter(Adapter):
    def __init__(self, process: str):
        if os.name != "nt":
            raise RuntimeError("windows_session requires Windows")
        self.process = process.lower()

    def _session_volume(self):
        from pycaw.pycaw import AudioUtilities
        for session in AudioUtilities.GetAllSessions():
            proc = session.Process
            if proc is not None and proc.name().lower() == self.process:
                return session.SimpleAudioVolume
        return None

    def set_position(self, position: int) -> None:
        volume = self._session_volume()
        if volume is None:
            raise RuntimeError(f"audio session not found: {self.process}")
        volume.SetMasterVolume(pos_to_unit(position), None)

    def get_position(self) -> Optional[int]:
        volume = self._session_volume()
        if volume is None:
            return None
        return unit_to_pos(volume.GetMasterVolume())

    def double_tap(self) -> None:
        volume = self._session_volume()
        if volume is None:
            raise RuntimeError(f"audio session not found: {self.process}")
        volume.SetMute(not bool(volume.GetMute()), None)


class OBSInputAdapter(Adapter):
    def __init__(self, client, input_name: str):
        self.client = client
        self.input_name = input_name

    def set_position(self, position: int) -> None:
        self.client.set_input_volume_mul(self.input_name, pos_to_unit(position))

    def get_position(self) -> Optional[int]:
        response = self.client.get_input_volume(self.input_name)
        value = getattr(response, "input_volume_mul", None)
        if value is None:
            value = getattr(response, "inputVolumeMul", None)
        if value is None:
            return None
        return unit_to_pos(value)

    def double_tap(self) -> None:
        response = self.client.get_input_mute(self.input_name)
        muted = getattr(response, "input_muted", None)
        if muted is None:
            muted = getattr(response, "inputMuted", False)
        self.client.set_input_mute(self.input_name, not bool(muted))


class MidiCCAdapter(Adapter):
    def __init__(self, port: str, channel: int, cc: int):
        import mido
        self.mido = mido
        self.output = mido.open_output(port)
        self.channel = int(clamp(channel, 0, 15))
        self.cc = int(clamp(cc, 0, 127))
        self.last_position = 0

    def set_position(self, position: int) -> None:
        self.last_position = int(clamp(position, 0, 255))
        value = int(round(self.last_position * 127 / 255))
        self.output.send(self.mido.Message(
            "control_change",
            channel=self.channel,
            control=self.cc,
            value=value,
        ))

    def get_position(self) -> Optional[int]:
        return self.last_position

    def close(self) -> None:
        self.output.close()


@dataclass(frozen=True)
class MappingKey:
    layer: int
    channel: int


class Bridge:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.topic_prefix = config.get("topic_prefix", "faderbuddy/console").rstrip("/")
        self.event_topic = f"{self.topic_prefix}/event"
        self.command_topic = f"{self.topic_prefix}/command"
        self.adapters: Dict[MappingKey, Adapter] = {}
        self.last_published: Dict[MappingKey, Tuple[int, float]] = {}
        self.stop_event = threading.Event()

        self._build_adapters()

        mqtt_cfg = config["mqtt"]
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=mqtt_cfg.get("client_id", "faderbuddy-pc-bridge"),
        )
        if mqtt_cfg.get("username"):
            self.client.username_pw_set(
                mqtt_cfg["username"],
                mqtt_cfg.get("password"),
            )
        if mqtt_cfg.get("tls", False):
            self.client.tls_set()
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.connect(
            mqtt_cfg["host"],
            int(mqtt_cfg.get("port", 1883)),
            keepalive=30,
        )

    def _build_adapters(self) -> None:
        ha_cfg = self.config.get("home_assistant", {})
        obs_cfg = self.config.get("obs", {})
        obs_client = None

        for layer_s, channels in self.config.get("layers", {}).items():
            layer = int(layer_s)
            for channel_s, mapping in channels.items():
                channel = int(channel_s)
                kind = mapping["type"]

                if kind == "homeassistant_light":
                    token = ha_cfg.get("token")
                    if not token:
                        raise RuntimeError("home_assistant.token is required")
                    adapter = HomeAssistantLightAdapter(
                        ha_cfg.get("url", "http://homeassistant.local:8123"),
                        token,
                        mapping["entity_id"],
                    )
                elif kind == "windows_master":
                    adapter = WindowsMasterAdapter()
                elif kind == "windows_session":
                    adapter = WindowsSessionAdapter(mapping["process"])
                elif kind == "obs_input":
                    if obs_client is None:
                        import obsws_python as obs
                        obs_client = obs.ReqClient(
                            host=obs_cfg.get("host", "127.0.0.1"),
                            port=int(obs_cfg.get("port", 4455)),
                            password=obs_cfg.get("password", ""),
                            timeout=float(obs_cfg.get("timeout", 3)),
                        )
                    adapter = OBSInputAdapter(obs_client, mapping["input"])
                elif kind == "midi_cc":
                    adapter = MidiCCAdapter(
                        mapping["port"],
                        int(mapping.get("midi_channel", 0)),
                        int(mapping["cc"]),
                    )
                else:
                    raise ValueError(
                        f"unknown mapping type {kind!r} for layer {layer} channel {channel}"
                    )

                self.adapters[MappingKey(layer, channel)] = adapter

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code != 0:
            LOG.error("MQTT connect failed: %s", reason_code)
            return
        LOG.info("MQTT connected; subscribing to %s", self.event_topic)
        client.subscribe(self.event_topic, qos=0)

    def _on_message(self, client, userdata, msg):
        try:
            event = json.loads(msg.payload.decode("utf-8"))
            key = MappingKey(int(event["layer"]), int(event["channel"]))
            adapter = self.adapters.get(key)
            if adapter is None:
                return

            kind = event.get("event")
            if kind == "move":
                adapter.set_position(int(event["position"]))
            elif kind == "double_tap":
                adapter.double_tap()
        except Exception:
            LOG.exception("failed to process event payload=%r", msg.payload)

    def publish_target(self, key: MappingKey, position: int, speed: int = 100) -> None:
        payload = {
            "op": "move",
            "channel": key.channel,
            "layer": key.layer,
            "position": int(clamp(position, 0, 255)),
            "speed": int(clamp(speed, 0, 255)),
        }
        self.client.publish(
            self.command_topic,
            json.dumps(payload, separators=(",", ":")),
            qos=0,
            retain=False,
        )

    def sync_loop(self) -> None:
        interval = float(self.config.get("sync_interval", 0.75))
        deadband = int(self.config.get("sync_deadband", 2))
        while not self.stop_event.wait(interval):
            for key, adapter in list(self.adapters.items()):
                try:
                    pos = adapter.get_position()
                    if pos is None:
                        continue
                    previous = self.last_published.get(key)
                    if previous is not None and abs(previous[0] - pos) <= deadband:
                        continue
                    self.publish_target(key, pos)
                    self.last_published[key] = (pos, time.monotonic())
                except Exception as exc:
                    LOG.debug(
                        "sync unavailable layer=%d channel=%d: %s",
                        key.layer,
                        key.channel,
                        exc,
                    )

    def run(self) -> None:
        threading.Thread(
            target=self.sync_loop,
            name="fader-sync",
            daemon=True,
        ).start()
        self.client.loop_forever()

    def close(self) -> None:
        self.stop_event.set()
        try:
            self.client.disconnect()
        except Exception:
            pass
        for adapter in self.adapters.values():
            try:
                adapter.close()
            except Exception:
                LOG.exception("adapter close failed")


def load_config(path: Path) -> Dict[str, Any]:
    raw = os.path.expandvars(path.read_text(encoding="utf-8"))
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise ValueError("configuration root must be a mapping")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description="FaderBuddy 8-channel PC bridge")
    parser.add_argument("-c", "--config", default="config.yaml")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    bridge = Bridge(load_config(Path(args.config)))
    try:
        bridge.run()
    except KeyboardInterrupt:
        LOG.info("stopping")
    finally:
        bridge.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
