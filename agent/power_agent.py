"""
Power Agent — AI 驅動的停電保護流程
當 UPS 進入 CRITICAL/EMERGENCY 狀態時由 state_machine 呼叫

流程：
  1. list_vms → 取得當前開機 VM
  2. 依 priority_vms 排序（重要的最後關）
  3. suspend_vms(dry_run=False)
  4. enter_maintenance for each host
  5. shutdown_host for each host

CRITICAL 狀態：步驟 1-3 自動，步驟 4-5 等 USER 確認（透過 Reflex UI 或 Mattermost 回覆）
EMERGENCY 狀態：全部自動，不等確認
"""

import logging
import yaml
from pathlib import Path
from ups_monitor.state_machine import UPSState
from vcenter_mcp.tools import list_vms, suspend_vms, enter_maintenance, shutdown_host

log = logging.getLogger(__name__)
CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"


def get_shutdown_order(vms: list[dict], priority_vms: list[str]) -> list[str]:
    """
    priority_vms 裡的 VM 排最後關。
    priority_vms 的列表順序即關閉順序：index 越大 = 越晚關。
    最後一個 entry（通常是 vCenter Server）一定是最後執行，
    確保前面的 API 呼叫還有 vCenter 可用。
    """
    priority_index = {name: i for i, name in enumerate(priority_vms)}
    priority_set   = set(priority_vms)
    normal    = [v["name"] for v in vms if v["power_state"] == "poweredOn" and v["name"] not in priority_set]
    important = sorted(
        [v["name"] for v in vms if v["power_state"] == "poweredOn" and v["name"] in priority_set],
        key=lambda n: priority_index.get(n, 0)
    )
    return normal + important


def run_emergency_shutdown(cluster_name: str, dry_run: bool = True):
    """EMERGENCY 模式：全自動，不等確認"""
    cfg = yaml.safe_load(CONFIG_PATH.read_text())
    cluster_cfg = next((c for c in cfg["clusters"] if c["name"] == cluster_name), {})
    priority_vms = cluster_cfg.get("priority_vms", [])

    log.warning(f"[EMERGENCY] Starting shutdown for cluster: {cluster_name} dry_run={dry_run}")

    vms = list_vms(cluster_name)
    order = get_shutdown_order(vms, priority_vms)
    log.warning(f"Suspend order: {order}")

    results = suspend_vms(order, dry_run=dry_run)
    for r in results:
        log.warning(f"  {r['name']}: {r['action']} ({r['reason']})")

    hosts = list({v["host"] for v in vms})
    for host in hosts:
        r = enter_maintenance(host, dry_run=dry_run)
        log.warning(f"  {host} maintenance: {r['action']}")
        r = shutdown_host(host, dry_run=dry_run)
        log.warning(f"  {host} shutdown: {r['action']}")


def on_ups_state_change(state: UPSState, cluster_name: str):
    """UPS 狀態變化時的 handler，由 state_machine 呼叫"""
    if state == UPSState.CRITICAL:
        # 只 suspend，等 UI 確認後再 maintenance/shutdown
        log.warning(f"[CRITICAL] Auto-suspending VMs in {cluster_name}")
        cfg = yaml.safe_load(CONFIG_PATH.read_text())
        cluster_cfg = next((c for c in cfg["clusters"] if c["name"] == cluster_name), {})
        vms = list_vms(cluster_name)
        order = get_shutdown_order(vms, cluster_cfg.get("priority_vms", []))
        suspend_vms(order, dry_run=False)

    elif state == UPSState.EMERGENCY:
        run_emergency_shutdown(cluster_name, dry_run=False)
