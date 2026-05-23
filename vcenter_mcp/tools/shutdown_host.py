"""
shutdown_host — 關閉 ESXi host（必須已在維護模式）
"""

import logging
from pyVmomi import vim
from ._session import get_si

log = logging.getLogger(__name__)


def shutdown_host(host_name: str, dry_run: bool = True) -> dict:
    host = _find_host(host_name)
    if host is None:
        return {"host": host_name, "action": "error", "reason": "host not found"}

    if not host.runtime.inMaintenanceMode:
        return {
            "host": host_name, "action": "error",
            "reason": "host must be in maintenance mode before shutdown"
        }

    if dry_run:
        return {"host": host_name, "action": "would_shutdown", "reason": "dry_run"}

    log.warning(f"Shutting down host: {host_name}")
    host.ShutdownHost_Task(force=False)
    return {"host": host_name, "action": "shutdown_initiated", "reason": "ok"}


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
