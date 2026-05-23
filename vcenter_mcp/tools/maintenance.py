"""
maintenance — ESXi host 進入 / 離開維護模式
"""

import logging
from pyVmomi import vim
from ._session import get_si

log = logging.getLogger(__name__)


def enter_maintenance(host_name: str, evacuate_powered_off: bool = True,
                      dry_run: bool = True) -> dict:
    host = _find_host(host_name)
    if host is None:
        return {"host": host_name, "action": "error", "reason": "host not found"}

    if host.runtime.inMaintenanceMode:
        return {"host": host_name, "action": "skipped", "reason": "already in maintenance"}

    if dry_run:
        return {"host": host_name, "action": "would_enter_maintenance", "reason": "dry_run"}

    log.info(f"Entering maintenance mode: {host_name}")
    task = host.EnterMaintenanceMode_Task(
        timeout=600,
        evacuatePoweredOffVms=evacuate_powered_off,
    )
    _wait_task(task)
    return {"host": host_name, "action": "maintenance", "reason": "ok"}


def exit_maintenance(host_name: str, dry_run: bool = True) -> dict:
    host = _find_host(host_name)
    if host is None:
        return {"host": host_name, "action": "error", "reason": "host not found"}

    if not host.runtime.inMaintenanceMode:
        return {"host": host_name, "action": "skipped", "reason": "not in maintenance"}

    if dry_run:
        return {"host": host_name, "action": "would_exit_maintenance", "reason": "dry_run"}

    log.info(f"Exiting maintenance mode: {host_name}")
    task = host.ExitMaintenanceMode_Task(timeout=300)
    _wait_task(task)
    return {"host": host_name, "action": "active", "reason": "ok"}


def _find_host(name: str):
    si = get_si()
    content = si.RetrieveContent()
    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.HostSystem], True
    )
    for h in container.view:
        if h.name == name:
            return h
    return None


def _wait_task(task, timeout_sec: int = 600):
    import time
    start = time.time()
    while task.info.state in ("queued", "running"):
        if time.time() - start > timeout_sec:
            raise TimeoutError("Task timeout")
        time.sleep(3)
    if task.info.state == "error":
        raise RuntimeError(task.info.error.msg)
