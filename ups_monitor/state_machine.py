"""
UPS 狀態機：NORMAL → ON_BATTERY → WARNING → CRITICAL → EMERGENCY

狀態轉移：
  NORMAL      : 市電正常
  ON_BATTERY  : 偵測到吃電池，只告警
  WARNING     : 剩餘 < warning_minutes，升級告警通知 on-call
  CRITICAL    : 剩餘 < critical_minutes，通知 UI 等待 USER 確認開始 suspend
  EMERGENCY   : 剩餘 < emergency_minutes，跳過確認直接執行 shutdown
"""

from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime
import logging
import requests
import yaml
from pathlib import Path

log = logging.getLogger(__name__)
CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"


class UPSState(str, Enum):
    NORMAL = "NORMAL"
    ON_BATTERY = "ON_BATTERY"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    EMERGENCY = "EMERGENCY"


@dataclass
class StateMachine:
    state: UPSState = UPSState.NORMAL
    state_since: datetime = field(default_factory=datetime.now)
    _cfg: dict = field(default_factory=dict, repr=False)

    def __post_init__(self):
        self._cfg = yaml.safe_load(CONFIG_PATH.read_text())

    def _thresholds(self):
        return self._cfg["thresholds"]

    def transition(self, new_state: UPSState, reason: str):
        if new_state == self.state:
            return
        log.warning(f"UPS state: {self.state} → {new_state} ({reason})")
        self.state = new_state
        self.state_since = datetime.now()
        self._notify(new_state, reason)

    def _notify(self, state: UPSState, reason: str):
        icons = {
            UPSState.NORMAL:     "✅",
            UPSState.ON_BATTERY: "⚡",
            UPSState.WARNING:    "🟠",
            UPSState.CRITICAL:   "🔴",
            UPSState.EMERGENCY:  "🆘",
        }
        msg = f"{icons[state]} *[UPS {state}]* {reason}"
        # 從 config/.env 讀 Telegram token（格式：TG_TOKEN=xxx, TG_CHAT_ID=xxx）
        try:
            env_file = Path(__file__).parent.parent / "config" / ".env"
            token, chat_id = "", ""
            if env_file.exists():
                for line in env_file.read_text().splitlines():
                    if line.startswith("TG_TOKEN="):
                        token = line.split("=", 1)[1].strip()
                    elif line.startswith("TG_CHAT_ID="):
                        chat_id = line.split("=", 1)[1].strip()
            if token and chat_id:
                requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"},
                    timeout=10
                )
        except Exception as e:
            log.error(f"Telegram notify failed: {e}")

    def update(self, on_battery: bool, remaining_min: float,
               predicted_min: float | None = None) -> UPSState:
        """
        predicted_min: runtime_predictor.predict_runtime() 的修正值
                       若為 None 則沿用 config 靜態閾值
        """
        t = self._thresholds()
        effective_min = predicted_min if predicted_min is not None else remaining_min

        if not on_battery:
            if self.state != UPSState.NORMAL:
                # 市電恢復 → 若之前在吃電池，記錄本次實際 runtime 供模型訓練
                self._battery_on_start = None
                self.transition(UPSState.NORMAL, "市電恢復")
            return self.state

        # 第一次進入吃電池：記錄開始時間與 SNMP 估計值
        if self.state == UPSState.NORMAL:
            self._battery_on_start = datetime.now()
            self._snmp_estimate_at_start = remaining_min

        if effective_min < t["emergency_minutes"]:
            self.transition(UPSState.EMERGENCY,
                            f"剩餘 {effective_min:.1f} 分鐘，緊急關機")
        elif effective_min < self._dynamic_critical(t):
            self.transition(UPSState.CRITICAL,
                            f"剩餘 {effective_min:.1f} 分鐘，請確認是否開始關機")
        elif effective_min < t["warning_minutes"]:
            self.transition(UPSState.WARNING,
                            f"剩餘 {effective_min:.1f} 分鐘")
        else:
            self.transition(UPSState.ON_BATTERY,
                            f"切換至電池供電，預估 {effective_min:.1f} 分鐘")

        return self.state

    def _dynamic_critical(self, t: dict) -> int:
        """
        若 shutdown_estimator 可取得動態閾值則用之，否則退回 config 靜態值。
        動態閾值 = 預估關機時間 + buffer，確保 CRITICAL 觸發後來得及關完所有 VM。
        """
        try:
            import yaml
            from agent.shutdown_estimator import recommended_critical_threshold_min
            from vcenter_mcp.tools import list_vms
            cfg = yaml.safe_load(
                (__import__("pathlib").Path(__file__).parent.parent / "config.yaml").read_text()
            )
            cluster_name = cfg.get("clusters", [{}])[0].get("name", "")
            if cluster_name:
                vms = list_vms(cluster_name)
                return recommended_critical_threshold_min(vms)
        except Exception as e:
            log.debug(f"Dynamic critical threshold unavailable: {e}")
        return t["critical_minutes"]

    # 內部狀態（不需要 persist）
    _battery_on_start: datetime | None = None
    _snmp_estimate_at_start: float = 0.0
