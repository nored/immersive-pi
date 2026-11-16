import json
import socket

from shared.node_health import collect
from shared.service_health import EventLog, notify


def test_missing_sensors_are_unknown_and_link_failure_has_evidence(tmp_path):
    path = tmp_path / "sys/class/net/eth0"
    path.mkdir(parents=True)
    (path / "carrier").write_text("0\n")
    report = collect(tmp_path)
    assert report["power"]["current"] is None
    assert report["temperature_c"] is None
    assert {"kind": "network", "severity": "error", "evidence": "Ethernet carrier is down"} in report["faults"]


def test_voltage_alarm_and_poe_fan_controller_are_reported_without_vcgencmd(tmp_path):
    voltage = tmp_path / "sys/class/hwmon/hwmon1"
    voltage.mkdir(parents=True)
    (voltage / "name").write_text("rpi_volt")
    (voltage / "in0_lcrit_alarm").write_text("1")
    fan = tmp_path / "sys/class/hwmon/hwmon2"
    fan.mkdir()
    (fan / "name").write_text("pwmfan")
    (fan / "pwm1").write_text("100")
    report = collect(tmp_path)
    assert report["power"]["undervoltage_now"] is True
    assert report["power"]["since_boot"] is None
    assert report["fans"] == [{"controller": "pwmfan", "pwm": 100, "rpm": None}]
    assert any(fault["kind"] == "power" for fault in report["faults"])


def test_event_history_survives_restart_and_is_bounded(tmp_path):
    events = EventLog(tmp_path, capacity=2)
    events.record("connected", "node-1")
    events.record("session_failed", error="decoder unavailable")
    events.record("disconnected", "node-1")
    restored = EventLog(tmp_path, capacity=2)
    assert [e["kind"] for e in restored.events] == ["session_failed", "disconnected"]
    assert len((tmp_path / "events.jsonl").read_text().splitlines()) == 3


def test_watchdog_notification_uses_systemd_socket(tmp_path, monkeypatch):
    address = str(tmp_path / "notify.sock")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
        receiver.bind(address)
        receiver.settimeout(1)
        monkeypatch.setenv("NOTIFY_SOCKET", address)
        notify("WATCHDOG=1")
        assert receiver.recv(100) == b"WATCHDOG=1"
