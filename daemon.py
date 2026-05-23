"""
Power Guard 主 daemon — 串接所有模組

snmp_poller → state_machine → power_agent
                            → runtime_predictor.record_event（市電恢復時）
"""

import logging
import yaml
from datetime import datetime
from pathlib import Path

from ups_monitor.snmp_poller import run_poller, UPSStatus
from ups_monitor.state_machine import StateMachine, UPSState
from agent.power_agent import on_ups_state_change
from agent.runtime_predictor import predict_runtime, record_event

log = logging.getLogger(__name__)
CONFIG_PATH = Path(__file__).parent / "config.yaml"


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    cfg = yaml.safe_load(CONFIG_PATH.read_text())
    cluster_name = cfg.get("clusters", [{}])[0].get("name", "")
    ups_max_kw = cfg.get("ups", {}).get("max_output_kw", 2.7)

    sm = StateMachine()

    # 停電事件追蹤（市電恢復後用來 record_event）
    _ev: dict = {}

    def on_status(status: UPSStatus):
        # 預測修正 runtime（模型不存在時自動 fallback SNMP × 0.8）
        predicted = predict_runtime(
            load_kw=status.output_load_pct / 100 * ups_max_kw,
            temperature_c=status.temperature_c,
            vm_count=0,
            snmp_estimate_min=status.battery_remaining_min,
        )

        prev_state = sm.state
        new_state = sm.update(
            on_battery=status.on_battery,
            remaining_min=status.battery_remaining_min,
            predicted_min=predicted if predicted > 0 else None,
        )

        # 進入 ON_BATTERY：記錄起始資訊
        if prev_state == UPSState.NORMAL and new_state != UPSState.NORMAL:
            _ev.update({
                "start": datetime.now(),
                "snmp_min": status.battery_remaining_min,
                "load_kw": status.output_load_pct / 100 * ups_max_kw,
                "temp_c": status.temperature_c,
            })

        # 市電恢復：record_event 累積訓練資料
        if prev_state != UPSState.NORMAL and new_state == UPSState.NORMAL:
            if _ev.get("start"):
                actual_min = (datetime.now() - _ev["start"]).total_seconds() / 60
                try:
                    record_event(
                        load_kw=_ev["load_kw"],
                        temperature_c=_ev["temp_c"],
                        vm_count=0,
                        actual_runtime_min=actual_min,
                        snmp_estimate_min=_ev["snmp_min"],
                    )
                    log.info(
                        f"Battery event recorded: actual={actual_min:.1f}min "
                        f"snmp={_ev['snmp_min']:.1f}min"
                    )
                except Exception as e:
                    log.warning(f"record_event failed: {e}")
                _ev.clear()

        # CRITICAL / EMERGENCY → power_agent
        if new_state in (UPSState.CRITICAL, UPSState.EMERGENCY):
            if prev_state != new_state and cluster_name:
                on_ups_state_change(new_state, cluster_name)

    log.info(f"Power Guard daemon started — cluster={cluster_name}")
    run_poller(on_status)


if __name__ == "__main__":
    main()
