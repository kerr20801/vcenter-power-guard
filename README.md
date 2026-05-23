# vCenter Power Guard

UPS 斷電自動保護：NUT 監控 → 5 狀態機 → vCenter 有序 Suspend/Shutdown。

適合家用 lab 或小型機房，UPS 透過 USB 連接並由 NUT 管理。

---

## 架構

```
NUT (upsc)
  └── ups_monitor/snmp_poller.py    每 30 秒 poll UPS 狀態
        └── ups_monitor/state_machine.py    NORMAL → ON_BATTERY → WARNING → CRITICAL → EMERGENCY
              └── agent/power_agent.py      自動 Suspend / Shutdown vCenter cluster

vcenter_mcp/tools/                  pyVmomi 封裝（list_vms / suspend / maintenance / shutdown）
agent/runtime_predictor.py          CatBoost：預測 UPS 實際剩餘時間（自動累積訓練資料）
agent/shutdown_estimator.py         預估「全部 VM suspend 要幾分鐘」→ 動態計算 CRITICAL 閾值
```

---

## 狀態機行為

| 狀態 | 觸發條件 | 自動動作 |
|------|----------|----------|
| ON_BATTERY | 市電中斷 | Telegram 告警 |
| WARNING | 剩餘 < `warning_minutes` | 升級告警 |
| CRITICAL | 剩餘 < 動態閾值 | 自動 Suspend 所有 VM，等人確認 maintenance/shutdown |
| EMERGENCY | 剩餘 < `emergency_minutes` | 全自動 shutdown，不等確認 |
| NORMAL | 市電恢復 | 告警解除，記錄本次實際 runtime 供模型訓練 |

**動態 CRITICAL 閾值**：系統自動計算「現在這批 VM 全部 suspend 需要幾分鐘」+ 5 分鐘 buffer，不用手動調閾值。

**Sequential Suspend**：suspend = 把 RAM 寫入磁碟，大 VM suspend 期間電力會短暫上升 10~20%。本系統一次只 suspend 一台，避免同時操作觸發 UPS 過載。

---

## 前置需求

- NUT 已安裝並管理 UPS（`upsc <ups-name>@localhost` 能回傳資料）
- vCenter 可連線，有 suspend / maintenance / shutdown 權限
- Python 3.11+

```bash
pip install -r requirements.txt
```

---

## 設定

```bash
cp config.example.yaml config.yaml
# 填寫 vcenter.host / vcenter.username / clusters[0].name / priority_vms
```

密碼用環境變數，不要寫進 config：

```bash
export VCENTER_PASSWORD="your-vcenter-password"
```

Telegram 通知（可選）：

```
# config/.env
TG_TOKEN=your-bot-token
TG_CHAT_ID=your-chat-id
```

---

## 啟動

```bash
# 直接執行（推薦先測試）
python3 daemon.py

# 或 Docker
cp config.example.yaml config.yaml
VCENTER_PASSWORD=xxx docker compose up -d
```

---

## 計畫性停電演練

換電池或機房維護前，用 `saturday_drill.py` 確認關機順序：

```bash
# 先 dry run 看會做什麼
export GOVC_URL="https://admin%40vsphere.local:Password@vcenter.example.com"
python3 saturday_drill.py dryrun

# 確認順序正確後正式執行
python3 saturday_drill.py execute

# 換完電池後做健康測試（量放電曲線，存 SQLite）
python3 saturday_drill.py battery
```

---

## DS3000F / battery.runtime = 0 問題

部分 UPS 的 NUT driver 不回傳 `battery.runtime`（永遠為 0）。  
系統會自動從 `battery.charge` 百分比 × 在當前負載下的理論 runtime 估算，並乘以 0.8 保守係數。

累積 5 次以上真實停電資料後，CatBoost 模型會接管預測，比 SNMP 估算更準確。

---

## MCP 工具（供 Claude Code / AI Agent 呼叫）

```
python -m vcenter_mcp.server   # 啟動 MCP server（port 9100）
```

| Tool | 說明 |
|------|------|
| `list_vms` | 列出 cluster 所有 VM 及電源狀態 |
| `suspend_vms` | 批次 suspend，預設 `dry_run=true` |
| `enter_maintenance` | Host 進入維護模式 |
| `exit_maintenance` | Host 離開維護模式 |
| `shutdown_host` | 關閉 ESXi Host |

所有工具預設 `dry_run=true`，需明確傳入 `dry_run=false` 才會真正執行。

---

## 測試

見 [TESTING.md](TESTING.md)。
