"""vCenter session 管理（singleton，自動 reconnect）"""

import ssl
import os
import yaml
from pathlib import Path
from pyVim.connect import SmartConnect, Disconnect
import atexit

_si = None
CONFIG_PATH = Path(__file__).parent.parent.parent / "config.yaml"


def get_si():
    global _si
    if _si is None:
        cfg = yaml.safe_load(CONFIG_PATH.read_text())["vcenter"]
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        if not cfg.get("ssl_verify", True):
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        _si = SmartConnect(
            host=cfg["host"],
            port=cfg.get("port", 443),
            user=cfg["username"],
            pwd=os.environ.get("VCENTER_PASSWORD", cfg.get("password", "")),
            sslContext=context,
        )
        atexit.register(Disconnect, _si)
    return _si
