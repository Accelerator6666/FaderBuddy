#!/usr/bin/env python3
from __future__ import annotations

import argparse, json, logging, os, threading
from pathlib import Path
from typing import Any, Optional

import paho.mqtt.client as mqtt
import requests
import yaml

LOG = logging.getLogger("fader-bridge")


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def pos_to_unit(pos: int) -> float:
    return clamp(int(pos), 0, 255) / 255.0


def unit_to_pos(v: float) -> int:
    return int(round(clamp(float(v), 0.0, 1.0) * 255))


class Adapter:
    def set_position(self, position: int) -> None:
        raise NotImplementedError
    def get_position(self) -> Optional[int]:
        return None
    def double_tap(self) -> None:
        pass
    def close(self) -> None:
        pass


class HomeAssistantLight(Adapter):
    def __init__(self, base_url: str, token: str, entity_id: str):
        self.base = base_url.rstrip("/")
        self.entity = entity_id
        self.http = requests.Session()
        self.http.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })

    def set_position(self, position: int) -> None:
        position = int(clamp(position, 0, 255))
        service = "turn_off" if position == 0 else "turn_on"
        data = {"entity_id": self.entity}
        if position:
            data.update(brightness=position, transition=0)
        r = self.http.post(f"{self.base}/api/services/light/{service}", json=data, timeout=3)
        r.raise_for_status()

    def get_position(self) -> Optional[int]:
        r = self.http.get(f"{self.base}/api/states/{self.entity}", timeout=3)
        r.raise_for_status()
        state = r.json()
        if state.get("state") == "off":
            return 0
        return int(clamp(state.get("attributes", {}).get("brightness", 255), 0, 255))

    def double_tap(self) -> None:
        service = "turn_on" if self.get_position() == 0 else "turn_off"
        r = self.http.post(
            f"{self.base}/api/services/light/{service}",
            json={"entity_id": self.entity},
            timeout=3,
        )
        r.raise_for_status()


class WindowsMaster(Adapter):
    def __init__(self):
        if os.name != "nt":
            raise RuntimeError("windows_master requires Windows")
        from pycaw.pycaw import AudioUtilities
        device = AudioUtilities.GetSpeakers()
        if device is None:
            raise RuntimeError("no default Windows speaker device found")
        self.volume = device.EndpointVolume

    def set_position(self, position: int) -> None:
        self.volume.SetMasterVolumeLevelScalar(pos_to_unit(position), None)

    def get_position(self) -> Optional[int]:
        return unit_to_pos(self.volume.GetMasterVolumeLevelScalar())

    def double_tap(self) -> None:
        self.volume.SetMute(not bool(self.volume.GetMute()), None)


class WindowsSession(Adapter):
    def __init__(self, process: str):
        if os.name != "nt":
            raise RuntimeError("windows_session requires Windows")
        self.process = process.lower()

    def _volume(self):
        from pycaw.pycaw import AudioUtilities
        for session in AudioUtilities.GetAllSessions():
            if session.Process and session.Process.name().lower() == self.process:
                return session.SimpleAudioVolume
        return None

    def set_position(self, position: int) -> None:
        volume = self._volume()
        if volume is None:
            raise RuntimeError(f"audio session not found: {self.process}")
        volume.SetMasterVolume(pos_to_unit(position), None)

    def get_position(self) -> Optional[int]:
        volume = self._volume()
        return None if volume is None else unit_to_pos(volume.GetMasterVolume())

    def double_tap(self) -> None:
        volume = self._volume()
        if volume is None:
            raise RuntimeError(f"audio session not found: {self.process}")
        volume.SetMute(not bool(volume.GetMute()), None)


class OBSInput(Adapter):
    def __init__(self, client, name: str):
        self.client, self.name = client, name

    def set_position(self, position: int) -> None:
        self.client.set_input_volume(self.name, vol_mul=pos_to_unit(position))

    def get_position(self) -> Optional[int]:
        return unit_to_pos(self.client.get_input_volume(self.name).input_volume_mul)

    def double_tap(self) -> None:
        muted = self.client.get_input_mute(self.name).input_muted
        self.client.set_input_mute(self.name, not muted)


class MidiCC(Adapter):
    def __init__(self, port: str, channel: int, cc: int):
        import mido
        self.mido = mido
        self.out = mido.open_output(port)
        self.channel = int(clamp(channel, 0, 15))
        self.cc = int(clamp(cc, 0, 127))

    def set_position(self, position: int) -> None:
        value = int(round(clamp(position, 0, 255) * 127 / 255))
        self.out.send(self.mido.Message(
            "control_change", channel=self.channel, control=self.cc, value=value
        ))

    def close(self) -> None:
        self.out.close()


class Bridge:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        prefix = cfg.get("topic_prefix", "faderbuddy/console").rstrip("/")
        self.event_topic = f"{prefix}/event"
        self.command_topic = f"{prefix}/command"
        self.adapters: dict[tuple[int, int], Adapter] = {}
        self.last: dict[tuple[int, int], int] = {}
        self.stop = threading.Event()
        self._build_adapters()

        mc = cfg["mqtt"]
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=mc.get("client_id", "faderbuddy-pc-bridge"),
        )
        if mc.get("username"):
            self.client.username_pw_set(mc["username"], mc.get("password"))
        if mc.get("tls", False):
            self.client.tls_set()
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.connect(mc["host"], int(mc.get("port", 1883)), keepalive=30)

    def _build_adapters(self):
        ha = self.cfg.get("home_assistant", {})
        obs_cfg = self.cfg.get("obs", {})
        obs_client = None
        for layer_s, channels in self.cfg.get("layers", {}).items():
            layer = int(layer_s)
            for channel_s, item in channels.items():
                channel, kind = int(channel_s), item["type"]
                if kind == "homeassistant_light":
                    if not ha.get("token"):
                        raise RuntimeError("home_assistant.token is required")
                    adapter = HomeAssistantLight(
                        ha.get("url", "http://homeassistant.local:8123"),
                        ha["token"],
                        item["entity_id"],
                    )
                elif kind == "windows_master":
                    adapter = WindowsMaster()
                elif kind == "windows_session":
                    adapter = WindowsSession(item["process"])
                elif kind == "obs_input":
                    if obs_client is None:
                        import obsws_python as obs
                        obs_client = obs.ReqClient(
                            host=obs_cfg.get("host", "127.0.0.1"),
                            port=int(obs_cfg.get("port", 4455)),
                            password=obs_cfg.get("password", ""),
                            timeout=float(obs_cfg.get("timeout", 3)),
                        )
                    adapter = OBSInput(obs_client, item["input"])
                elif kind == "midi_cc":
                    adapter = MidiCC(
                        item["port"],
                        int(item.get("midi_channel", 0)),
                        int(item["cc"]),
                    )
                else:
                    raise ValueError(f"unknown mapping type: {kind}")
                self.adapters[(layer, channel)] = adapter

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            LOG.info("MQTT connected; listening on %s", self.event_topic)
            client.subscribe(self.event_topic, qos=0)
        else:
            LOG.error("MQTT connect failed: %s", reason_code)

    def _on_message(self, client, userdata, msg):
        try:
            event = json.loads(msg.payload.decode())
            kind = event.get("event")
            if kind == "layer":
                return
            if "layer" not in event or "channel" not in event:
                return
            adapter = self.adapters.get((int(event["layer"]), int(event["channel"])))
            if adapter is None:
                return
            if kind == "move":
                adapter.set_position(int(event["position"]))
            elif kind == "double_tap":
                adapter.double_tap()
        except Exception:
            LOG.exception("failed to process event payload=%r", msg.payload)

    def publish_target(self, layer: int, channel: int, position: int):
        self.client.publish(
            self.command_topic,
            json.dumps({
                "op": "move", "layer": layer, "channel": channel,
                "position": int(clamp(position, 0, 255)), "speed": 100,
            }, separators=(",", ":")),
            qos=0,
            retain=False,
        )

    def _sync_loop(self):
        interval = float(self.cfg.get("sync_interval", 0.75))
        deadband = int(self.cfg.get("sync_deadband", 2))
        while not self.stop.wait(interval):
            for (layer, channel), adapter in list(self.adapters.items()):
                try:
                    pos = adapter.get_position()
                    if pos is None:
                        continue
                    old = self.last.get((layer, channel))
                    if old is not None and abs(old - pos) <= deadband:
                        continue
                    self.publish_target(layer, channel, pos)
                    self.last[(layer, channel)] = pos
                except Exception as exc:
                    LOG.debug("sync unavailable L%d CH%d: %s", layer, channel, exc)

    def run(self):
        threading.Thread(target=self._sync_loop, daemon=True).start()
        self.client.loop_forever()

    def close(self):
        self.stop.set()
        for adapter in self.adapters.values():
            try:
                adapter.close()
            except Exception:
                LOG.exception("adapter close failed")
        try:
            self.client.disconnect()
        except Exception:
            pass


def load_config(path: Path) -> dict[str, Any]:
    cfg = yaml.safe_load(os.path.expandvars(path.read_text(encoding="utf-8")))
    if not isinstance(cfg, dict):
        raise ValueError("configuration root must be a mapping")
    return cfg


def main() -> int:
    p = argparse.ArgumentParser(description="FaderBuddy 8-channel PC bridge")
    p.add_argument("-c", "--config", default="config.yaml")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()
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
