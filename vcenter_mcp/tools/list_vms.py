"""
list_vms — 列出指定 cluster 內所有 VM 及其電源狀態
"""

from pyVmomi import vim
from ._session import get_si


def list_vms(cluster_name: str) -> list[dict]:
    """
    回傳格式：
    [
      {
        "name": "vm-name",
        "power_state": "poweredOn" | "poweredOff" | "suspended",
        "cpu_usage_mhz": 1200,
        "mem_usage_mb": 4096,
        "host": "esxi-host-fqdn",
        "guest_os": "ubuntu64Guest",
      },
      ...
    ]
    """
    si = get_si()
    content = si.RetrieveContent()

    cluster = _find_cluster(content, cluster_name)
    if not cluster:
        raise ValueError(f"Cluster '{cluster_name}' not found")

    vms = []
    for host in cluster.host:
        for vm in host.vm:
            summary = vm.summary
            vms.append({
                "name": vm.name,
                "power_state": summary.runtime.powerState,
                "cpu_usage_mhz": summary.quickStats.overallCpuUsage or 0,
                "mem_usage_mb": summary.quickStats.guestMemoryUsage or 0,
                "host": host.name,
                "guest_os": summary.config.guestFullName if summary.config else "",
            })
    return vms


def _find_cluster(content, name: str):
    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.ClusterComputeResource], True
    )
    for cluster in container.view:
        if cluster.name == name:
            return cluster
    return None
