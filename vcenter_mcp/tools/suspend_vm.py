"""
suspend_vm — 批次 suspend 指定 VM 清單（poweredOn 才操作）
"""

import logging
import time
from pyVmomi import vim
from ._session import get_si

try:
    from agent.shutdown_estimator import record_suspend as _record_suspend
except ImportError:
    _record_suspend = None

log = logging.getLogger(__name__)


def suspend_vms(vm_names: list[str], dry_run: bool = True) -> list[dict]:
    """
    dry_run=True  → 只回傳「會做什麼」，不實際執行
    dry_run=False → 執行 SuspendVM_Task，等待完成

    回傳：[{"name": ..., "action": "suspended"|"skipped"|"error", "reason": ...}]
    """
    si = get_si()
    content = si.RetrieveContent()
    vm_map = _build_vm_map(content)

    results = []
    tasks = []

    for name in vm_names:
        vm = vm_map.get(name)
        if vm is None:
            results.append({"name": name, "action": "error", "reason": "VM not found"})
            continue

        state = vm.summary.runtime.powerState
        if state != "poweredOn":
            results.append({"name": name, "action": "skipped", "reason": f"power_state={state}"})
            continue

        if dry_run:
            results.append({"name": name, "action": "would_suspend", "reason": "dry_run"})
        else:
            try:
                task = vm.SuspendVM_Task()
                tasks.append((name, task, time.time(), vm))
                log.info(f"Suspending {name}...")
            except Exception as e:
                results.append({"name": name, "action": "error", "reason": str(e)})

    if not dry_run:
        for name, task, start_ts, vm in tasks:
            try:
                _wait_task(task)
                elapsed = time.time() - start_ts
                results.append({"name": name, "action": "suspended", "reason": "ok"})
                _record_suspend_safe(vm, name, elapsed)
            except Exception as e:
                results.append({"name": name, "action": "error", "reason": str(e)})

    return results


def _record_suspend_safe(vm, name: str, elapsed_sec: float):
    if _record_suspend is None:
        return
    try:
        cfg = vm.summary.config
        ram_gb = cfg.memorySizeMB / 1024 if cfg else 0.0
        guest_os = getattr(cfg, "guestFullName", "") if cfg else ""
        _record_suspend(
            vm_name=name,
            ram_gb=ram_gb,
            storage_type="hdd",   # conservative default，無法從 vCenter 直接判斷
            cpu_load_pct=0.0,
            guest_os=guest_os,
            actual_sec=elapsed_sec,
        )
        log.debug(f"record_suspend: {name} ram={ram_gb:.1f}GB elapsed={elapsed_sec:.0f}s")
    except Exception as e:
        log.debug(f"record_suspend skipped ({name}): {e}")


def _build_vm_map(content) -> dict:
    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.VirtualMachine], True
    )
    return {vm.name: vm for vm in container.view}


def _wait_task(task, timeout_sec: int = 120):
    import time
    start = time.time()
    while task.info.state in ("queued", "running"):
        if time.time() - start > timeout_sec:
            raise TimeoutError("Task timeout")
        time.sleep(2)
    if task.info.state == "error":
        raise RuntimeError(task.info.error.msg)
