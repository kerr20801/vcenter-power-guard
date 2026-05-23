"""
NUT UPS poller (nutdrv_qx via USB)
取代原本的 Eaton SNMP poller，改用 upsc 指令讀取 NUT 資料
"""

import subprocess
import time
import logging
import yaml
from pathlib import Path

log = logging.getLogger(__name__)
CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"


def load_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text())


class UPSStatus:
    def __init__(self):
        self.on_battery: bool = False
        self.battery_remaining_sec: int = 0
        self.battery_charge_pct: int = 0
        self.output_load_pct: int = 0
        self.temperature_c: float = 0.0

    @property
    def battery_remaining_min(self) -> float:
        return self.battery_remaining_sec / 60

    def __repr__(self):
        return (
            f"UPSStatus(on_battery={self.on_battery}, "
            f"remaining={self.battery_remaining_min:.1f}min, "
            f"charge={self.battery_charge_pct}%, "
            f"load={self.output_load_pct}%)"
        )


def poll_once(cfg: dict) -> UPSStatus:
    ups_name = cfg["ups"]["nut_ups_name"]
    host     = cfg["ups"].get("nut_host", "localhost")

    result = subprocess.run(
        ["upsc", f"{ups_name}@{host}"],
        capture_output=True, text=True, timeout=10
    )

    status = UPSStatus()
    for line in result.stdout.splitlines():
        if ": " not in line:
            continue
        key, _, val = line.partition(": ")
        key = key.strip()
        val = val.strip()
        try:
            if key == "ups.status":
                status.on_battery = "OB" in val   # OB = On Battery
            elif key == "battery.runtime":
                status.battery_remaining_sec = int(float(val))
            elif key == "battery.charge":
                status.battery_charge_pct = int(float(val))
            elif key == "ups.load":
                status.output_load_pct = int(float(val))
            elif key == "ups.temperature":
                status.temperature_c = float(val)
        except (ValueError, TypeError):
            pass

    if result.returncode != 0:
        log.warning(f"upsc error: {result.stderr.strip()}")

    # DS3000F 不回傳 battery.runtime（永遠 0），改用 charge_pct 估算
    # 6×12V7Ah = 504Wh 理論，DoD 80% × 效率 85% = ~343Wh 可用
    if status.battery_remaining_sec == 0 and status.battery_charge_pct > 0:
        _BATT_WH  = 343.0
        _UPS_WATT = 2700.0
        load_w = _UPS_WATT * max(status.output_load_pct, 5) / 100
        max_runtime_min = _BATT_WH / load_w * 60
        status.battery_remaining_sec = int(
            status.battery_charge_pct / 100.0 * max_runtime_min * 60
        )
        log.debug(
            f"Estimated runtime from charge: {status.battery_charge_pct}% "
            f"@ {load_w:.0f}W → {status.battery_remaining_min:.1f}min"
        )

    return status


def run_poller(callback):
    """持續 poll，每次有新狀態時呼叫 callback(UPSStatus)"""
    cfg = load_config()
    interval = cfg["thresholds"]["poll_interval_sec"]
    ups_name = cfg["ups"]["nut_ups_name"]
    log.info(f"UPS poller started — NUT ups={ups_name} interval={interval}s")
    while True:
        try:
            status = poll_once(cfg)
            log.debug(status)
            callback(status)
        except Exception as e:
            log.error(f"Poll failed: {e}")
        time.sleep(interval)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_poller(print)
