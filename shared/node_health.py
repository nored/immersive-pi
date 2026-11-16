"""Read-only Pi health evidence; unavailable sensors are reported as unknown."""
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

THROTTLE_FLAGS = {0: "undervoltage", 1: "frequency_capped", 2: "throttled", 3: "soft_temperature_limit"}


def read(path, default=None):
    try:
        return Path(path).read_text().strip()
    except (OSError, ValueError):
        return default


def number(path, scale=1):
    try:
        return int(read(path)) / scale
    except (TypeError, ValueError):
        return None


def collect(root="/", include_kernel=False):
    root = Path(root)
    def path(value):
        return root / value.lstrip("/")
    network = {}
    for name in ("operstate", "carrier", "speed", "duplex", "address"):
        network[name] = read(path(f"/sys/class/net/eth0/{name}"))
    for name in ("rx_errors", "tx_errors", "rx_dropped", "tx_dropped", "rx_bytes", "tx_bytes"):
        network[name] = number(path(f"/sys/class/net/eth0/statistics/{name}"))
    memory = {}
    for line in (read(path("/proc/meminfo"), "")).splitlines():
        key, _, value = line.partition(":")
        if key in ("MemTotal", "MemAvailable"):
            memory[key] = int(value.split()[0]) * 1024
    data = {
        "boot_id": read(path("/proc/sys/kernel/random/boot_id")),
        "model": read(path("/proc/device-tree/model"), "unknown").rstrip("\x00"),
        "temperature_c": number(path("/sys/class/thermal/thermal_zone0/temp"), 1000),
        "cpu_mhz": number(path("/sys/devices/system/cpu/cpufreq/policy0/scaling_cur_freq"), 1000),
        "governor": read(path("/sys/devices/system/cpu/cpufreq/policy0/scaling_governor")),
        "network": network, "memory": memory,
        "uptime_s": (read(path("/proc/uptime"), "0").split() or ["0"])[0],
        "hdmi": {p.parent.name: {"status": read(p), "modes": read(p.parent / "modes", "").splitlines()}
                 for p in path("/sys/class/drm").glob("card*-*/status")},
        "power": {"raw": None, "current": None, "since_boot": None},
        "faults": [],
        "fans": [],
    }
    for sensor in path("/sys/class/hwmon").glob("hwmon*"):
        name = read(sensor / "name", "")
        if name == "rpi_volt":
            alarm = number(sensor / "in0_lcrit_alarm")
            if alarm is not None:
                data["power"]["undervoltage_now"] = bool(alarm)
                data["power"]["source"] = "rpi_volt hwmon"
        if "fan" in name:
            data["fans"].append({"controller": name, "pwm": number(sensor / "pwm1"),
                                 "rpm": number(sensor / "fan1_input")})
    try:
        usage = os.statvfs(path("/"))
        data["storage"] = {"free_bytes": usage.f_bavail * usage.f_frsize,
                           "total_bytes": usage.f_blocks * usage.f_frsize,
                           "read_only": bool(usage.f_flag & os.ST_RDONLY)}
    except OSError:
        data["storage"] = None
    # Do not invent a healthy value when the firmware utility is unavailable.
    if root == Path("/") and shutil.which("vcgencmd"):
        try:
            result = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=2)
            if result.returncode == 0:
                flags = int(result.stdout.strip().split("=", 1)[1], 16)
                data["power"] = {"raw": flags,
                    "current": [name for bit, name in THROTTLE_FLAGS.items() if flags & (1 << bit)],
                    "since_boot": [name for bit, name in THROTTLE_FLAGS.items() if flags & (1 << (bit+16))]}
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
            pass
    faults = data["faults"]
    if network["carrier"] == "0":
        faults.append({"kind": "network", "severity": "error", "evidence": "Ethernet carrier is down"})
    if data["power"]["current"]:
        faults.append({"kind": "power_or_thermal", "severity": "error", "evidence": data["power"]["current"]})
    if data["power"].get("undervoltage_now"):
        faults.append({"kind": "power", "severity": "error", "evidence": "Firmware reports current undervoltage"})
    if data["power"]["since_boot"]:
        faults.append({"kind": "power_or_thermal_history", "severity": "warning", "evidence": data["power"]["since_boot"]})
    storage = data["storage"]
    if storage and storage["read_only"]:
        faults.append({"kind": "storage", "severity": "error", "evidence": "Root filesystem is read-only"})
    if storage and storage["free_bytes"] < 256 * 1024 * 1024:
        faults.append({"kind": "storage", "severity": "warning", "evidence": "Less than 256 MiB free"})
    if include_kernel and root == Path("/"):
        try:
            result = subprocess.run(["journalctl", "-k", "-b", "-p", "warning", "-n", "80", "--no-pager", "-o", "cat"],
                                     capture_output=True, text=True, timeout=3)
            lines = result.stdout.splitlines()
            data["kernel_faults"] = [line for line in lines if re.search(
                r"under.?voltage|over.?current|mmc[0-9]+: .*(error|timeout|crc)|I/O error|EXT4-fs error|"
                r"v3d.*(hang|reset|fault)|GPU.*(hang|reset)|NETDEV WATCHDOG|Out of memory|Killed process", line, re.I)]
        except (OSError, subprocess.TimeoutExpired):
            data["kernel_faults"] = None
    return data


if __name__ == "__main__":
    print(json.dumps(collect(include_kernel=True)))
