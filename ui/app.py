"""
Reflex UI — vCenter Power Guard
操作流程：選 Cluster → 預覽 VM 狀態 → 確認 Suspend → 確認維護模式 → 確認 Shutdown
"""

import sys
import asyncio
from pathlib import Path

import reflex as rx
import yaml

# 讓 ui/ 目錄下可以 import 專案根目錄的模組
sys.path.insert(0, str(Path(__file__).parent.parent))

from vcenter_mcp.tools import list_vms, suspend_vms, enter_maintenance, shutdown_host

CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"


class State(rx.State):
    clusters: list[str] = []
    selected_cluster: str = ""
    vms: list[dict] = []
    selected_vms: list[str] = []
    loading: bool = False
    error: str = ""
    step: str = "select_cluster"
    # select_cluster → review_vms → confirm_suspend
    # → confirm_maintenance → confirm_shutdown → done
    log_lines: list[str] = []

    def load_clusters(self):
        cfg = yaml.safe_load(CONFIG_PATH.read_text())
        self.clusters = [c["name"] for c in cfg.get("clusters", [])]

    def select_cluster(self, name: str):
        self.selected_cluster = name
        self.vms = []
        self.error = ""

    @rx.background
    async def fetch_vms(self):
        async with self as state:
            state.loading = True
            state.error = ""
        try:
            vms = await asyncio.to_thread(list_vms, self.selected_cluster)
            async with self as state:
                state.vms = vms
                state.selected_vms = [
                    v["name"] for v in vms if v["power_state"] == "poweredOn"
                ]
                state.loading = False
                state.step = "review_vms"
        except Exception as e:
            async with self as state:
                state.error = f"無法取得 VM 清單：{e}"
                state.loading = False

    def toggle_vm(self, name: str):
        if name in self.selected_vms:
            self.selected_vms.remove(name)
        else:
            self.selected_vms.append(name)

    def confirm_suspend(self):
        self.step = "confirm_suspend"

    def back_to_review(self):
        self.step = "review_vms"

    @rx.background
    async def do_suspend(self):
        async with self as state:
            state.loading = True
            state.log_lines.append(f"▶ Suspending {len(state.selected_vms)} VM(s)...")
        try:
            results = await asyncio.to_thread(
                suspend_vms, list(self.selected_vms), False
            )
            async with self as state:
                for r in results:
                    icon = "✓" if r["action"] == "suspended" else "⚠"
                    state.log_lines.append(f"  {icon} {r['name']}: {r['action']} ({r['reason']})")
                # 記下涉及的 host 清單供後續維護/關機使用
                state._affected_hosts = list({
                    v["host"] for v in state.vms if v["name"] in state.selected_vms
                })
                state.loading = False
                state.step = "confirm_maintenance"
        except Exception as e:
            async with self as state:
                state.log_lines.append(f"  ✗ Suspend 失敗：{e}")
                state.loading = False

    @rx.background
    async def do_maintenance(self):
        async with self as state:
            state.loading = True
            state.log_lines.append("▶ Entering maintenance mode...")
        try:
            hosts = self._affected_hosts or []
            results = await asyncio.gather(
                *[asyncio.to_thread(enter_maintenance, h, True, False) for h in hosts]
            )
            async with self as state:
                for r in results:
                    icon = "✓" if r["action"] in ("maintenance", "skipped") else "✗"
                    state.log_lines.append(f"  {icon} {r['host']}: {r['action']} ({r['reason']})")
                state.loading = False
                state.step = "confirm_shutdown"
        except Exception as e:
            async with self as state:
                state.log_lines.append(f"  ✗ 維護模式失敗：{e}")
                state.loading = False

    @rx.background
    async def do_shutdown(self):
        async with self as state:
            state.loading = True
            state.log_lines.append("▶ Shutting down hosts...")
        try:
            hosts = self._affected_hosts or []
            results = await asyncio.gather(
                *[asyncio.to_thread(shutdown_host, h, False) for h in hosts]
            )
            async with self as state:
                for r in results:
                    icon = "✓" if r["action"] == "shutdown_initiated" else "✗"
                    state.log_lines.append(f"  {icon} {r['host']}: {r['action']} ({r['reason']})")
                state.loading = False
                state.step = "done"
        except Exception as e:
            async with self as state:
                state.log_lines.append(f"  ✗ Shutdown 失敗：{e}")
                state.loading = False

    # 內部暫存，不需要 reactive
    _affected_hosts: list[str] = []

    def reset(self):
        self.step = "select_cluster"
        self.vms = []
        self.selected_vms = []
        self.log_lines = []
        self.error = ""
        self._affected_hosts = []


def vm_row(vm: dict) -> rx.Component:
    checked = State.selected_vms.contains(vm["name"])
    return rx.table.row(
        rx.table.cell(
            rx.checkbox(checked=checked, on_change=lambda _: State.toggle_vm(vm["name"]))
        ),
        rx.table.cell(vm["name"]),
        rx.table.cell(
            rx.badge(
                vm["power_state"],
                color_scheme=rx.cond(vm["power_state"] == "poweredOn", "green", "gray"),
            )
        ),
        rx.table.cell(vm["host"]),
        rx.table.cell(f"{vm['mem_usage_mb']} MB"),
    )


def index() -> rx.Component:
    return rx.container(
        rx.heading("vCenter Power Guard", size="6", margin_bottom="1em"),

        rx.cond(
            State.error != "",
            rx.callout(State.error, icon="x_circle", color_scheme="red", margin_bottom="1em"),
        ),

        rx.cond(
            State.step == "select_cluster",
            rx.vstack(
                rx.text("選擇 Cluster", weight="bold"),
                rx.select(
                    State.clusters,
                    on_change=State.select_cluster,
                    placeholder="-- 選擇 Cluster --",
                ),
                rx.button(
                    "載入 VM 清單",
                    on_click=State.fetch_vms,
                    loading=State.loading,
                    disabled=State.selected_cluster == "",
                ),
                on_mount=State.load_clusters,
            ),
        ),

        rx.cond(
            State.step == "review_vms",
            rx.vstack(
                rx.text(
                    f"Cluster: {State.selected_cluster} — 請勾選要 Suspend 的 VM",
                    weight="bold",
                ),
                rx.table.root(
                    rx.table.header(
                        rx.table.row(
                            rx.table.column_header_cell(""),
                            rx.table.column_header_cell("VM 名稱"),
                            rx.table.column_header_cell("狀態"),
                            rx.table.column_header_cell("Host"),
                            rx.table.column_header_cell("記憶體"),
                        )
                    ),
                    rx.table.body(rx.foreach(State.vms, vm_row)),
                ),
                rx.hstack(
                    rx.button(
                        "下一步：確認 Suspend",
                        on_click=State.confirm_suspend,
                        color_scheme="orange",
                        disabled=State.selected_vms.length() == 0,
                    ),
                    rx.button("取消", on_click=State.reset, variant="outline"),
                ),
            ),
        ),

        rx.cond(
            State.step == "confirm_suspend",
            rx.vstack(
                rx.callout(
                    f"確認 Suspend {State.selected_vms.length()} 台 VM？此操作會暫停 VM 執行。",
                    icon="triangle_alert",
                    color_scheme="orange",
                ),
                rx.hstack(
                    rx.button(
                        "確認執行",
                        on_click=State.do_suspend,
                        loading=State.loading,
                        color_scheme="red",
                    ),
                    rx.button("返回", on_click=State.back_to_review, variant="outline"),
                ),
            ),
        ),

        rx.cond(
            State.step == "confirm_maintenance",
            rx.vstack(
                rx.callout(
                    "VM 已全數 Suspend，是否進入 Host 維護模式？",
                    icon="triangle_alert",
                    color_scheme="orange",
                ),
                rx.hstack(
                    rx.button(
                        "進入維護模式",
                        on_click=State.do_maintenance,
                        loading=State.loading,
                        color_scheme="red",
                    ),
                    rx.button("暫停（稍後再做）", variant="outline"),
                ),
            ),
        ),

        rx.cond(
            State.step == "confirm_shutdown",
            rx.vstack(
                rx.callout(
                    "Host 已進入維護模式，是否執行 Shutdown？",
                    icon="triangle_alert",
                    color_scheme="red",
                ),
                rx.hstack(
                    rx.button(
                        "關機",
                        on_click=State.do_shutdown,
                        loading=State.loading,
                        color_scheme="red",
                    ),
                    rx.button("暫停（稍後再做）", variant="outline"),
                ),
            ),
        ),

        rx.cond(
            State.step == "done",
            rx.vstack(
                rx.callout("完成！所有 Host 已關機。", icon="check", color_scheme="green"),
                rx.button("重新開始", on_click=State.reset),
            ),
        ),

        rx.cond(
            State.log_lines.length() > 0,
            rx.vstack(
                rx.separator(),
                rx.text("操作記錄", weight="bold", margin_top="1em"),
                rx.foreach(
                    State.log_lines,
                    lambda l: rx.text(l, font_family="monospace", size="2"),
                ),
            ),
        ),

        max_width="900px",
        padding="2em",
    )


app = rx.App()
app.add_page(index, route="/", on_load=State.load_clusters)
