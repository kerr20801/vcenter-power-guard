# TESTING.md — vCenter Power Guard

測試流程分四層：元件測試 → 整合測試 → 演練測試 → 上線後驗收。

---

## 前置條件

```bash
# 確認 NUT daemon 在跑
systemctl status nut-server nut-client

# 確認 upsc 可讀到 UPS
upsc optiups@localhost | grep -E "ups.status|battery.charge|battery.runtime|ups.load"

# 確認 vCenter 可連
python3 -c "from vcenter_mcp.tools._session import get_si; si = get_si(); print('vCenter OK')"

# 確認 config.yaml 填好
grep -E "nut_ups_name|vcenter_host|cluster" config.yaml
```

---

## 1. 元件測試（不需 UPS 斷電）

### 1-A NUT Poller

```bash
cd vcenter-power-guard

# 單次 poll，看輸出是否合理
python3 -c "
import yaml
from ups_monitor.snmp_poller import poll_once
cfg = yaml.safe_load(open('config.yaml').read())
s = poll_once(cfg)
print(s)
print('  remaining_min:', s.battery_remaining_min)
print('  on_battery   :', s.on_battery)
"
```

預期輸出：`UPSStatus(on_battery=False, remaining=xx.xmin, charge=100%, load=xx%)`

DS3000F 的 `battery.runtime` 永遠為 0，poller 會自動從 `battery.charge` 估算，屬正常。

---

### 1-B 狀態機乾跑

模擬各狀態觸發，不需真正斷電：

```bash
python3 -c "
from ups_monitor.state_machine import StateMachine, UPSState

sm = StateMachine()

# 正常市電
s = sm.update(on_battery=False, remaining_min=60)
assert s == UPSState.NORMAL, s

# 切換到電池（足夠電量）
s = sm.update(on_battery=True, remaining_min=50)
assert s == UPSState.ON_BATTERY, s

# 觸發 WARNING（config.yaml 的 warning_minutes，通常 30）
s = sm.update(on_battery=True, remaining_min=25)
assert s == UPSState.WARNING, s

# 觸發 CRITICAL（動態閾值，通常 15-25 min，依 VM 數量而定）
s = sm.update(on_battery=True, remaining_min=8)
assert s in (UPSState.CRITICAL, UPSState.EMERGENCY), s

# 市電恢復
s = sm.update(on_battery=False, remaining_min=0)
assert s == UPSState.NORMAL, s

print('狀態機測試通過')
"
```

注意：CRITICAL 閾值是動態的（`_dynamic_critical()` 呼叫 `shutdown_estimator`），
若 vCenter 連線失敗會退回 `config.yaml` 的 `critical_minutes` 靜態值。

---

### 1-C VM suspend dry_run

```bash
python3 -c "
from vcenter_mcp.tools.list_vms import list_vms
from vcenter_mcp.tools.suspend_vm import suspend_vms

cluster = 'YOUR_CLUSTER_NAME'   # 換成 config.yaml 的 clusters[0].name

vms = list_vms(cluster)
powered_on = [v['name'] for v in vms if v['power_state'] == 'poweredOn']
print(f'poweredOn: {len(powered_on)} VMs: {powered_on}')

# dry_run=True，不實際執行
results = suspend_vms(powered_on, dry_run=True)
for r in results:
    print(f'  {r[\"name\"]}: {r[\"action\"]}')
"
```

全部應回傳 `action: would_suspend`。

---

### 1-D 動態 CRITICAL 閾值確認

```bash
python3 -c "
from vcenter_mcp.tools.list_vms import list_vms
from agent.shutdown_estimator import (
    estimate_total_shutdown_min,
    recommended_critical_threshold_min,
)
import yaml

cfg = yaml.safe_load(open('config.yaml').read())
cluster = cfg['clusters'][0]['name']
vms = list_vms(cluster)

est = estimate_total_shutdown_min(vms)
threshold = recommended_critical_threshold_min(vms)
print(f'poweredOn VM 數   : {sum(1 for v in vms if v[\"power_state\"]==\"poweredOn\")}')
print(f'預估關機時間       : {est} 分鐘')
print(f'建議 CRITICAL 閾值 : {threshold} 分鐘（含 5min buffer）')
"
```

確認 threshold 比你的 UPS 在該負載下的實際 runtime 小，否則需調整 VM 優先序或降低負載。

---

## 2. 整合測試（daemon 全流程 dry_run）

不實際斷電，注入模擬停電事件：

```bash
python3 -c "
import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s — %(message)s')

from ups_monitor.state_machine import StateMachine, UPSState
from agent.power_agent import on_ups_state_change
from agent.runtime_predictor import predict_runtime
import yaml

cfg = yaml.safe_load(open('config.yaml').read())
cluster = cfg['clusters'][0]['name']
ups_max_kw = cfg.get('ups', {}).get('max_output_kw', 2.7)
sm = StateMachine()

def simulate(on_battery, load_pct, charge_pct, temp_c=25.0):
    # 模擬 DS3000F 估算（同 snmp_poller）
    _BATT_WH = 343.0
    _UPS_WATT = 2700.0
    load_w = _UPS_WATT * max(load_pct, 5) / 100
    max_runtime_min = _BATT_WH / load_w * 60
    remaining_min = charge_pct / 100.0 * max_runtime_min if on_battery else 60.0

    predicted = predict_runtime(
        load_kw=load_pct / 100 * ups_max_kw,
        temperature_c=temp_c,
        vm_count=0,
        snmp_estimate_min=remaining_min,
    )
    prev = sm.state
    new  = sm.update(on_battery=on_battery, remaining_min=remaining_min,
                     predicted_min=predicted if predicted > 0 else None)
    print(f'  [simulate] on_battery={on_battery} charge={charge_pct}% remaining={remaining_min:.1f}min '
          f'predicted={predicted:.1f}min → {prev}→{new}')
    if new in (UPSState.CRITICAL, UPSState.EMERGENCY) and prev != new:
        print(f'  >>> 觸發 on_ups_state_change({new}, dry_run 由 power_agent 內部控制)')
        # 注意：測試時 power_agent.on_ups_state_change 會呼叫 suspend_vms(dry_run=False)
        # 若不想對 vCenter 操作，改呼叫 run_emergency_shutdown(..., dry_run=True)

simulate(on_battery=False, load_pct=30, charge_pct=100)  # NORMAL
simulate(on_battery=True,  load_pct=30, charge_pct=90)   # ON_BATTERY
simulate(on_battery=True,  load_pct=30, charge_pct=60)   # WARNING
simulate(on_battery=True,  load_pct=30, charge_pct=20)   # → CRITICAL or EMERGENCY
simulate(on_battery=False, load_pct=30, charge_pct=100)  # 復電 → NORMAL
"
```

**重要**：CRITICAL 觸發後 `on_ups_state_change` 會呼叫真實的 `suspend_vms(dry_run=False)`。
若只想驗證流程不動 VM，在 `agent/power_agent.py` 暫時把 `dry_run=False` 改成 `dry_run=True`。

---

## 3. saturday_drill.py 演練

這是完整的預演，用於每次實際斷電前的人工操作演練。

### Step 1：Dry Run（必做）

```bash
cd vcenter-power-guard
python3 saturday_drill.py dryrun
```

確認輸出的關機順序符合預期：

```
Wave 1: K8S nodes (node1, node2)
Wave 2: K8S master
Wave 3: Logstash
Wave 4: Kibana / Graylog / LibreNMS / Es03 / Es02
Wave 5: Nginx / Gitlab / Ansible ...
Wave 6: DNS
Wave 7: vCenter Server
```

vCenter Server 一定最後，這樣前面的 API 呼叫才有 vCenter 可用。

### Step 2：Execute（計畫性停電時才跑）

```bash
python3 saturday_drill.py execute
# 會先確認清單：UPS OL、crontab 暫停、Email AI 通知
# 輸入 yes → 可選先 dry run 再正式執行
```

Wave 之間有 30 秒等待讓 OS 完整關閉。

### Step 3：換電池後健康測試

```bash
python3 saturday_drill.py battery
# 確認充電量 >= 95%
# 手動拔電 → 監控放電曲線 → 復電
# 結果自動存入 data/battery_health.db
```

健康基準：新電池放電速率應比舊電池慢 2x 以上。

---

## 4. 上線後驗收（第一次真實停電後）

### 4-A 確認 record_event 有寫入

```bash
python3 -c "
from agent.runtime_predictor import _load_events
events = _load_events()
print(f'事件筆數: {len(events)}')
if events:
    print('最後一筆:', events[-1])
"
```

若市電已恢復但筆數仍為 0，檢查 `daemon.py` 中 `_ev.get('start')` 是否有被設定
（第一次進入 ON_BATTERY 時 `_ev.update(...)` 必須被呼叫）。

### 4-B 確認 record_suspend 有寫入

```bash
python3 -c "
from agent.shutdown_estimator import _load_history
h = _load_history()
print(f'Suspend 記錄筆數: {len(h)}')
if h:
    print('最後一筆:', h[-1])
"
```

每台成功 suspend 的 VM 應對應一筆記錄（ram_gb、actual_sec、guest_os_family）。

### 4-C 電池老化報告

```bash
python3 -c "
from agent.runtime_predictor import battery_health_report
import json
print(json.dumps(battery_health_report(), indent=2, ensure_ascii=False))
"
```

累積 6 次以上停電事件後，`degradation.assessment` 若出現「注意」或「建議換電池」即應計畫換電池。

### 4-D ML 訓練（資料夠時）

```bash
# UPS runtime 模型（累積 5 次停電後）
python3 -c "from agent.runtime_predictor import train; print(train())"

# Suspend 時間模型（累積 10 次 suspend 後）
python3 -c "from agent.shutdown_estimator import train; print(train())"
```

---

## EMERGENCY 模式安全確認

上線前必須人工確認：

- [ ] `config.yaml` 的 `emergency_minutes` 是否合理（建議 5 min）
- [ ] `priority_vms` 清單最後一個是否為 `vCenter Server`（否則 API 呼叫會失敗）
- [ ] `suspend_vms` 是 sequential 的，不會同時對多台大 VM 操作（已實作，確認 code 未被修改）
- [ ] EMERGENCY 狀態下 `run_emergency_shutdown(dry_run=False)` 會真實執行，無 UI 確認步驟——確保閾值不會被正常波動誤觸

預估 suspend 時間可用以下指令確認是否在 EMERGENCY 觸發前能完成：

```bash
python3 -c "
from vcenter_mcp.tools.list_vms import list_vms
from agent.shutdown_estimator import estimate_total_shutdown_min
import yaml
cfg = yaml.safe_load(open('config.yaml').read())
cluster = cfg['clusters'][0]['name']
emergency_min = cfg['thresholds']['emergency_minutes']
est = estimate_total_shutdown_min(list_vms(cluster))
margin = emergency_min - est
print(f'預估關機: {est:.1f} min | emergency 閾值: {emergency_min} min | 餘裕: {margin:.1f} min')
if margin < 2:
    print('⚠ 餘裕不足 2 分鐘，建議降低 emergency_minutes 或縮短 VM 清單')
else:
    print('✅ 餘裕充足')
"
```
