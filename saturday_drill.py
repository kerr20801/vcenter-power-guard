#!/usr/bin/env python3
"""
週六電池更換演練腳本 — 2026-05-10
流程：
  1. dry run 確認 VM 順序
  2. 確認後執行有序關機
  3. 換電池
  4. 開機後跑 healthy 電池測試

用法：
  python3 saturday_drill.py dryrun     ← 先看會做什麼，不實際執行
  python3 saturday_drill.py execute    ← 正式執行（需確認）
  python3 saturday_drill.py battery    ← 換完電池後做健康測試
"""

import sys, time, subprocess, sqlite3, os
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

STEP = sys.argv[1] if len(sys.argv) > 1 else "dryrun"

# ── Step 1: Dry Run ────────────────────────────────────────────────────────────

def do_dryrun():
    print("\n" + "="*60)
    print("  週六演練 — Dry Run（govc 查詢電源狀態）")
    print("="*60)

    # 用 govc 查目前狀態
    result = subprocess.run(
        ["govc", "ls", "-t", "VirtualMachine", "/DC/vm/..."],
        capture_output=True, text=True, env={**os.environ, **GOVC_ENV}
    )
    powered_on = set()
    for path in result.stdout.splitlines():
        if "vCLS" in path or "Discovered" in path:
            continue
        info = subprocess.run(
            ["govc", "vm.info", path],
            capture_output=True, text=True, env={**os.environ, **GOVC_ENV}
        )
        for line in info.stdout.splitlines():
            if "Power state" in line and "poweredOn" in line:
                powered_on.add(path.split("/")[-1])

    print(f"\n  目前 poweredOn：{len(powered_on)} 台")
    print("\n  ── 計畫關機順序 ──")
    seq = 1
    for wi, wave in enumerate(SHUTDOWN_WAVES, 1):
        active = [vm for vm in wave if vm in powered_on]
        if not active:
            continue
        print(f"\n  Wave {wi}：")
        for vm in active:
            print(f"    {seq:2d}. {vm}")
            seq += 1

    print(f"\n  ── 已關閉（不需處理）──")
    all_wave_vms = {vm for wave in SHUTDOWN_WAVES for vm in wave}
    already_off = [v for v in ["30_Ubuntu24.04_Jump","81_Ubuntu24.04_Server","22_Ubuntu22.04_Harbor"] if v not in powered_on]
    for vm in already_off:
        print(f"    - {vm}（已關閉）")

    print(f"\n  確認順序正確後執行：python3 saturday_drill.py execute")

# ── Step 2: Execute ────────────────────────────────────────────────────────────

GOVC_ENV = {
    # 從環境變數讀取，範例：
    # export GOVC_URL="https://admin%40vsphere.local:YourPassword@vcenter.example.com"
    "GOVC_URL":      os.environ.get("GOVC_URL", ""),
    "GOVC_INSECURE": os.environ.get("GOVC_INSECURE", "1"),
    **{k: v for k, v in os.environ.items()},
}

# 關機順序：先關依賴多的，DNS 和 vCenter 最後
SHUTDOWN_WAVES = [
    # Wave 1：K8S nodes 先，master 後
    ["72_Ubuntu24.04_k8s_node1_uat", "73_Ubuntu24.04_k8s_node2_uat"],
    ["71_Ubuntu24.04_k8s_master_uat"],
    # Wave 2：ELK 依序
    ["35_Ubuntu22.04_Logstash"],
    ["31_Ubuntu22.04_Kibana&grafana", "29_Ubuntu24.04_Graylog", "36_Ubuntu22.04_Librenms"],
    ["34_Ubuntu22.04_Es03", "33_Ubuntu22.04_Es02"],
    # Wave 3：其他服務
    ["44_Ubuntu24.04_Proxy_Nginx2", "43_Ubuntu24.04_Proxy_Nginx1",
     "27_Ubuntu24.04_OpenCode", "42_Ubuntu24.04_fileserver",
     "21_Ubuntu22.04_Gitlab", "23_Ubuntu22.04_Ansible&Docker"],
    # Wave 4：最後兩台
    ["80_Centos8_DNS"],
    ["vCenter Server"],
]

def govc_shutdown(vm_name: str, dry_run: bool = True) -> str:
    """發出 guest shutdown（等同按關機鍵，讓 OS 優雅關閉）"""
    path = f"/DC/vm/{vm_name}"
    # 嘗試找在 folder 裡的 VM
    result = subprocess.run(
        ["govc", "ls", "-t", "VirtualMachine", "/DC/vm/..."],
        capture_output=True, text=True, env=GOVC_ENV
    )
    for line in result.stdout.splitlines():
        if line.endswith(f"/{vm_name}"):
            path = line
            break

    if dry_run:
        return f"would shutdown: {path}"
    r = subprocess.run(
        ["govc", "vm.power", "-s", path],
        capture_output=True, text=True, env=GOVC_ENV, timeout=60
    )
    return "ok" if r.returncode == 0 else f"error: {r.stderr.strip()}"


def do_execute():
    print("\n" + "="*60)
    print("  週六演練 — 正式執行（有序 Guest Shutdown）")
    print("="*60)
    print("""
  執行前確認清單：
  □ UPS 目前 OL（市電供電，battery.charge=100）
  □ 台股 AI crontab 已暫停（!crontab -r 或確認今天不跑）
  □ Email AI 通知 Jessie 暫停使用
  □ 沒有正在進行的 K8S deployment
""")
    ans = input("  確認以上都 OK？輸入 'yes' 繼續：").strip().lower()
    if ans != "yes":
        print("  取消")
        return

    dry = input("  要先 dry run 看順序嗎？(y/n)：").strip().lower() == "y"

    t0 = time.time()
    total_ok, total_err = 0, 0

    for wi, wave in enumerate(SHUTDOWN_WAVES, 1):
        print(f"\n  [Wave {wi}] 關閉：{', '.join(wave)}")
        results = []
        for vm in wave:
            r = govc_shutdown(vm, dry_run=dry)
            results.append((vm, r))
            print(f"    {vm}: {r}")

        if not dry and wi < len(SHUTDOWN_WAVES):
            print(f"    等待 30 秒讓 OS 正常關閉...")
            time.sleep(30)

    elapsed = int(time.time() - t0)
    print(f"\n  {'Dry run' if dry else '實際執行'} 完成，耗時 {elapsed} 秒")

    if dry:
        print("\n  確認順序正確後執行：python3 saturday_drill.py execute（再輸入 'no' 跳過 dry run）")
        return

    print("\n  [下一步] 手動關閉 ESXi Hosts")
    print("  請到每台 ESXi UI 依序：")
    print("  1. 進入 Maintenance Mode")
    print("  2. Shut Down")
    print("  順序：.72 → .73 → .71（K8S nodes）→ .35 → .33/.34（ELK）→ .21 其他 → 最後 .99（vCenter host）")
    input("\n  ESXi 全部關閉後按 Enter...")

    print("\n  [最後] 關閉本機（AI 主機 .116）")
    print("  關機後流程：")
    print("    1. 拔 AI 主機電源線")
    print("    2. 拔 UPS 市電插頭")
    print("    3. 換電池（YUASA NP7.2-12 × 6）")
    print("    4. 插回 UPS，開 UPS")
    print("    5. 開 AI 主機，等服務起來")
    print("    6. 執行：python3 saturday_drill.py battery")

    ans2 = input("\n  現在關閉本機？輸入 'shutdown' 確認：").strip().lower()
    if ans2 == "shutdown":
        print("  10 秒後關機...")
        time.sleep(10)
        os.system("sudo shutdown -h now")
    else:
        print("  跳過，請手動關機")

# ── Step 3: Battery Health Test ────────────────────────────────────────────────

def do_battery():
    print("\n" + "="*60)
    print("  新電池健康測試")
    print("="*60)
    print("""
  步驟：
  1. 確認 UPS 已充電完畢（battery.charge = 100）
  2. 執行此步驟
  3. 當提示時拔除電源
  4. 看到電壓穩定後插回（約 2 分鐘）
""")

    # 確認充電
    try:
        out = subprocess.check_output(["upsc", "optiups@localhost"],
                                      stderr=subprocess.DEVNULL).decode()
        d = dict(line.split(": ", 1) for line in out.splitlines() if ": " in line)
        charge = d.get("battery.charge", "?")
        status = d.get("ups.status", "?")
        voltage = d.get("battery.voltage", "?")
        print(f"  目前狀態：{status}  電量：{charge}%  電壓：{voltage}V")
        if float(charge or 0) < 95:
            print("  ⚠ 電量尚未充滿，建議等充到 100% 再測")
    except Exception as e:
        print(f"  ⚠ 無法讀 UPS：{e}")

    input("\n  準備好後按 Enter，然後去拔電源...")

    # 監控放電
    samples = []
    start_v = None
    print("\n  等待斷電...")

    for _ in range(200):   # 最多 200 次 × 5s = 1000s
        try:
            out = subprocess.check_output(["upsc", "optiups@localhost"],
                                          stderr=subprocess.DEVNULL).decode()
            d = dict(line.split(": ", 1) for line in out.splitlines() if ": " in line)
            v = float(d.get("battery.voltage", 0) or 0)
            l = float(d.get("ups.load", 0) or 0)
            c = float(d.get("battery.charge", 0) or 0)
            s = d.get("ups.status", "")
        except:
            time.sleep(5)
            continue

        elapsed = len(samples) * 5

        if start_v is None and "OB" in s:
            start_v = v
            print(f"\n  ✅ 偵測到斷電！開始記錄")

        if start_v is not None:
            samples.append((elapsed, v, l, c, s))
            drop = start_v - v
            print(f"  [{elapsed:4d}s] V={v:.3f}  load={l:.0f}%  charge={c:.0f}%  drop={drop:.3f}V")

            if "OL" in s and elapsed > 10:
                print(f"\n  ✅ 復電！測試完成")
                break
        else:
            print(f"\r  等待拔電... ({s} V={v})", end="", flush=True)

        time.sleep(5)

    if not samples:
        print("  沒有捕捉到放電數據，請重試")
        return

    # 計算並比較
    end_v = samples[-1][1]
    dur = len(samples) * 5
    drop = (start_v or 0) - end_v
    rate = drop / (dur / 60) if dur > 0 else 0

    # 讀取劣化基準
    DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "battery_health.db")
    baseline_rate = None
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT drop_rate_per_min, duration_sec FROM discharge_sessions "
            "WHERE label='degraded' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            baseline_rate = row[0]
        conn.close()
    except:
        pass

    # 存新電池數據
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""INSERT INTO discharge_sessions
            (label, tested_at, duration_sec, start_voltage, end_voltage,
             min_voltage, voltage_drop, avg_load, drop_rate_per_min,
             charge_start, charge_end, notes)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("healthy", datetime.now().isoformat(), dur,
             start_v, end_v, end_v, drop,
             sum(s[2] for s in samples)/len(samples), rate,
             samples[0][3], samples[-1][3],
             f"新電池 YUASA NP7.2-12×6，換電池後基準，{datetime.now().strftime('%Y-%m-%d')}"))
        conn.executemany("""INSERT INTO discharge_samples
            (session_id, elapsed_sec, voltage, load_pct, charge_pct, status)
            VALUES (last_insert_rowid(),?,?,?,?,?)""",
            [(s[0], s[1], s[2], s[3], s[4]) for s in samples])
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"  ⚠ 存檔失敗：{e}")

    # 結果報告
    print(f"\n{'='*60}")
    print(f"  新電池測試結果")
    print(f"{'='*60}")
    print(f"  持續時間   : {dur} 秒")
    print(f"  電壓降幅   : {drop:.3f}V（{start_v:.2f}V → {end_v:.2f}V）")
    print(f"  放電速率   : {rate:.4f} V/min")

    if baseline_rate:
        improvement = baseline_rate / rate if rate > 0 else 0
        print(f"\n  vs 舊電池：")
        print(f"  舊電池速率 : {baseline_rate:.4f} V/min")
        print(f"  新電池速率 : {rate:.4f} V/min")
        print(f"  改善倍數   : {improvement:.1f}x 更慢放電")
        if improvement > 2:
            print(f"  ✅ 新電池明顯比舊電池好")
        else:
            print(f"  ⚠ 改善幅度不如預期，請確認電池是否安裝正確")
    print(f"{'='*60}\n")


# ── Main ───────────────────────────────────────────────────────────────────────

if STEP == "dryrun":
    do_dryrun()
elif STEP == "execute":
    do_execute()
elif STEP == "battery":
    do_battery()
else:
    print(f"用法：python3 saturday_drill.py [dryrun|execute|battery]")
