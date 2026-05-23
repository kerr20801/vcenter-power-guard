"""
Shutdown Estimator — 預測「現在這批 VM 全部 suspend 需要幾分鐘」
讓狀態機動態決定 CRITICAL 閾值，而非寫死 15 分鐘

訓練資料：每次 suspend 完成後自動記錄到 data/suspend_history.json
特徵：ram_gb, storage_type, cpu_load_pct, guest_os_family
目標：actual_suspend_sec

suspend 期間電力注意事項：
  - suspend = 把 RAM 寫入 .vmem 磁碟檔
  - 寫入期間 I/O + CPU 滿載 → 電力短暫上升（大 VM 可能 +10~20%）
  - 故 suspend 必須 sequential，不可同時對多台大 VM 操作
  - 本模組同時估算「suspend 期間電力衝擊」供 UPS runtime 預測參考
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

DATA_DIR     = Path(__file__).parent.parent / "data"
HISTORY_DB   = DATA_DIR / "suspend_history.json"
MODEL_PKL    = DATA_DIR / "suspend_model.pkl"
DATA_DIR.mkdir(exist_ok=True)

# RAM 寫入速度估算（GB/s），依儲存類型
_WRITE_SPEED = {
    "ssd":  1.0,   # NVMe SSD
    "sata": 0.4,   # SATA SSD
    "hdd":  0.15,  # HDD（最慢）
    "san":  0.6,   # SAN / iSCSI
}
BUFFER_SEC = 60  # 每台 VM 額外緩衝時間（OS shutdown hook 等）


# ── 快速估算（不需要模型，rule-based） ────────────────────────────────────

def estimate_suspend_time_sec(ram_gb: float, storage_type: str = "ssd") -> float:
    """
    rule-based 估算單台 VM 的 suspend 時間（秒）
    實際值會因 storage 壓力而不同，累積資料後用 ML 修正
    """
    write_speed = _WRITE_SPEED.get(storage_type.lower(), 0.5)
    write_time = ram_gb / write_speed
    return write_time + BUFFER_SEC


def estimate_total_shutdown_min(vms: list[dict], storage_type: str = "ssd") -> float:
    """
    估算清單中所有 poweredOn VM 全部 sequential suspend 的總時間（分鐘）
    包含電力衝擊緩衝（大 VM 之間稍微間隔）

    vms: list_vms() 的回傳格式，需要 mem_usage_mb 欄位
    """
    powered_on = [v for v in vms if v.get("power_state") == "poweredOn"]
    if not powered_on:
        return 0.0

    total_sec = 0.0
    for vm in powered_on:
        ram_gb = vm.get("mem_usage_mb", 4096) / 1024
        sec = estimate_suspend_time_sec(ram_gb, storage_type)
        total_sec += sec
        log.debug(f"  {vm['name']}: {ram_gb:.1f}GB → {sec:.0f}s")

    # 大 VM（>16GB）之間加 30s 讓 I/O 壓力消散
    large_vms = sum(1 for v in powered_on if v.get("mem_usage_mb", 0) > 16384)
    total_sec += large_vms * 30

    total_min = total_sec / 60
    log.info(f"Estimated shutdown time: {len(powered_on)} VMs → {total_min:.1f} min")
    return round(total_min, 1)


def recommended_critical_threshold_min(vms: list[dict], storage_type: str = "ssd",
                                       buffer_min: int = 5) -> int:
    """
    動態計算 CRITICAL 閾值（分鐘）
    = 預測關機時間 + buffer
    永遠不低於 10 分鐘
    """
    estimated = estimate_total_shutdown_min(vms, storage_type)
    threshold = max(int(estimated) + buffer_min, 10)
    log.info(f"Recommended CRITICAL threshold: {threshold} min (estimated={estimated:.1f} + buffer={buffer_min})")
    return threshold


# ── 記錄實際值（每次 suspend 完成後呼叫） ────────────────────────────────

def record_suspend(
    vm_name: str,
    ram_gb: float,
    storage_type: str,
    cpu_load_pct: float,
    guest_os: str,
    actual_sec: float,
    power_delta_pct: float = 0.0,   # suspend 期間電力上升百分比（若有量測）
) -> None:
    history = _load_history()
    history.append({
        "vm_name":        vm_name,
        "ram_gb":         round(ram_gb, 1),
        "storage_type":   storage_type,
        "cpu_load_pct":   round(cpu_load_pct, 1),
        "guest_os_family": _os_family(guest_os),
        "actual_sec":     round(actual_sec, 1),
        "power_delta_pct": round(power_delta_pct, 1),
    })
    HISTORY_DB.write_text(json.dumps(history, ensure_ascii=False, indent=2))


# ── ML 訓練（累積足夠資料後） ─────────────────────────────────────────────

def train() -> dict:
    history = _load_history()
    if len(history) < 10:
        return {"status": "insufficient_data", "n_records": len(history)}

    try:
        import numpy as np
        import joblib
        from catboost import CatBoostRegressor
        from sklearn.model_selection import cross_val_score
        from sklearn.preprocessing import LabelEncoder

        le = LabelEncoder()
        os_enc = le.fit_transform([h["guest_os_family"] for h in history])
        stor_enc = LabelEncoder().fit_transform([h["storage_type"] for h in history])

        X = np.array([
            [h["ram_gb"], h["cpu_load_pct"], o, s]
            for h, o, s in zip(history, os_enc, stor_enc)
        ])
        y = np.array([h["actual_sec"] for h in history])

        model = CatBoostRegressor(iterations=200, depth=4, verbose=False, random_seed=42)
        model.fit(X, y)
        cv_rmse = (-cross_val_score(model, X, y, cv=min(5, len(history)),
                                    scoring="neg_root_mean_squared_error")).mean()
        joblib.dump({"model": model, "le_os": le}, MODEL_PKL)
        return {"status": "ok", "n_records": len(history), "cv_rmse_sec": round(cv_rmse, 1)}
    except ImportError:
        return {"status": "dependency_missing"}


# ── helpers ──────────────────────────────────────────────────────────────

def _load_history() -> list[dict]:
    if not HISTORY_DB.exists():
        return []
    try:
        return json.loads(HISTORY_DB.read_text())
    except Exception:
        return []


def _os_family(guest_os: str) -> str:
    g = (guest_os or "").lower()
    if "windows" in g:
        return "windows"
    if "ubuntu" in g or "debian" in g or "rhel" in g or "centos" in g or "linux" in g:
        return "linux"
    return "other"
