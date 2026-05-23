"""
UPS Runtime Predictor — CatBoost
預測「現在這個負載下 UPS 實際還能撐幾分鐘」
並追蹤電池老化趨勢（同負載，年份越久 runtime 越短）

訓練資料：每次停電事件自動記錄到 data/ups_events.json
特徵：load_kw, battery_age_days, temperature_c, vm_count
目標：actual_runtime_min
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, date
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

DATA_DIR  = Path(__file__).parent.parent / "data"
EVENT_DB  = DATA_DIR / "ups_events.json"
MODEL_PKL = DATA_DIR / "runtime_model.pkl"
DATA_DIR.mkdir(exist_ok=True)

# 電池安裝日期（YUASA 12V7Ah，2026-05-12 換入）
BATTERY_INSTALL_DATE = date(2026, 5, 12)


# ── 事件記錄（每次停電自動呼叫） ──────────────────────────────────────────

def record_event(
    load_kw: float,
    temperature_c: float,
    vm_count: int,
    actual_runtime_min: float,
    snmp_estimate_min: float,
) -> None:
    """停電事件結束後記錄一筆訓練資料"""
    events = _load_events()
    events.append({
        "timestamp":         datetime.now().isoformat(),
        "battery_age_days":  (date.today() - BATTERY_INSTALL_DATE).days,
        "load_kw":           round(load_kw, 2),
        "temperature_c":     round(temperature_c, 1),
        "vm_count":          vm_count,
        "actual_runtime_min":   round(actual_runtime_min, 1),
        "snmp_estimate_min":    round(snmp_estimate_min, 1),
    })
    EVENT_DB.write_text(json.dumps(events, ensure_ascii=False, indent=2))
    log.info(f"Recorded UPS event: actual={actual_runtime_min:.1f}min snmp={snmp_estimate_min:.1f}min")


# ── 訓練 ─────────────────────────────────────────────────────────────────

def train() -> dict:
    """用歷史事件訓練 CatBoost 模型，回傳訓練結果摘要"""
    events = _load_events()
    if len(events) < 5:
        log.warning(f"Only {len(events)} events — need at least 5 to train. Using SNMP estimate.")
        return {"status": "insufficient_data", "n_events": len(events)}

    try:
        import numpy as np
        import joblib
        from catboost import CatBoostRegressor
        from sklearn.model_selection import cross_val_score

        X = np.array([[
            e["load_kw"], e["battery_age_days"],
            e["temperature_c"], e["vm_count"],
        ] for e in events])
        y = np.array([e["actual_runtime_min"] for e in events])

        model = CatBoostRegressor(
            iterations=200, depth=4, learning_rate=0.1,
            loss_function="RMSE", verbose=False, random_seed=42,
        )
        model.fit(X, y)

        cv_rmse = (-cross_val_score(model, X, y, cv=min(5, len(events)),
                                    scoring="neg_root_mean_squared_error")).mean()
        joblib.dump(model, MODEL_PKL)
        log.info(f"Model trained: n={len(events)} cv_rmse={cv_rmse:.1f}min")

        # 電池老化趨勢：比較近 5 筆 vs 舊 5 筆的平均 runtime（same load 範圍）
        degradation = _compute_degradation(events)

        return {
            "status": "ok", "n_events": len(events),
            "cv_rmse_min": round(cv_rmse, 1),
            "degradation": degradation,
        }
    except ImportError:
        log.warning("catboost/joblib not installed — skipping train")
        return {"status": "dependency_missing"}


# ── 預測 ─────────────────────────────────────────────────────────────────

def predict_runtime(
    load_kw: float,
    temperature_c: float,
    vm_count: int,
    snmp_estimate_min: float,
) -> float:
    """
    回傳預測剩餘分鐘數。
    若模型不存在（訓練資料不足），退回 SNMP 估計值 × 校正係數。
    """
    if MODEL_PKL.exists():
        try:
            import numpy as np
            import joblib
            model = joblib.load(MODEL_PKL)
            age_days = (date.today() - BATTERY_INSTALL_DATE).days
            pred = float(model.predict([[load_kw, age_days, temperature_c, vm_count]])[0])
            log.debug(f"predict_runtime: model={pred:.1f}min snmp={snmp_estimate_min:.1f}min")
            return max(pred, 0)
        except Exception as e:
            log.warning(f"Prediction failed: {e}, falling back to SNMP")

    # Fallback: SNMP 通常高估，先打 0.8 折
    return snmp_estimate_min * 0.8


# ── 電池老化報告 ─────────────────────────────────────────────────────────

def battery_health_report() -> dict:
    events = _load_events()
    if len(events) < 2:
        return {"status": "insufficient_data"}
    return {
        "status": "ok",
        "n_events": len(events),
        "battery_age_days": (date.today() - BATTERY_INSTALL_DATE).days,
        "degradation": _compute_degradation(events),
        "events_summary": [
            {
                "date": e["timestamp"][:10],
                "load_kw": e["load_kw"],
                "actual_min": e["actual_runtime_min"],
                "snmp_min": e["snmp_estimate_min"],
                "snmp_error_pct": round(
                    (e["snmp_estimate_min"] - e["actual_runtime_min"])
                    / max(e["actual_runtime_min"], 1) * 100, 1
                ),
            }
            for e in events[-10:]
        ],
    }


# ── helpers ──────────────────────────────────────────────────────────────

def _load_events() -> list[dict]:
    if not EVENT_DB.exists():
        return []
    try:
        return json.loads(EVENT_DB.read_text())
    except Exception:
        return []


def _compute_degradation(events: list[dict]) -> Optional[dict]:
    if len(events) < 6:
        return None
    old = events[: len(events) // 2]
    new = events[len(events) // 2 :]
    old_avg = sum(e["actual_runtime_min"] for e in old) / len(old)
    new_avg = sum(e["actual_runtime_min"] for e in new) / len(new)
    pct = round((new_avg - old_avg) / old_avg * 100, 1)
    return {
        "old_avg_min": round(old_avg, 1),
        "new_avg_min": round(new_avg, 1),
        "change_pct": pct,
        "assessment": "正常" if pct > -15 else ("注意" if pct > -30 else "建議換電池"),
    }
