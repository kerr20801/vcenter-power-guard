"""
vCenter MCP Server
提供給 AI Agent（Claude Code）呼叫的 MCP tools

啟動：python -m vcenter_mcp.server
"""

import json
import logging
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent
from .tools import list_vms, suspend_vms, enter_maintenance, exit_maintenance, shutdown_host

log = logging.getLogger(__name__)
app = Server("vcenter-power-guard")


@app.list_tools()
async def _list_tools() -> list[Tool]:
    return [
        Tool(
            name="list_vms",
            description="列出指定 cluster 內所有 VM 及電源狀態",
            inputSchema={
                "type": "object",
                "properties": {"cluster_name": {"type": "string"}},
                "required": ["cluster_name"],
            },
        ),
        Tool(
            name="suspend_vms",
            description="批次 suspend VM（dry_run=true 只預覽，不實際執行）",
            inputSchema={
                "type": "object",
                "properties": {
                    "vm_names": {"type": "array", "items": {"type": "string"}},
                    "dry_run": {"type": "boolean", "default": True},
                },
                "required": ["vm_names"],
            },
        ),
        Tool(
            name="enter_maintenance",
            description="讓指定 ESXi host 進入維護模式（dry_run=true 只預覽）",
            inputSchema={
                "type": "object",
                "properties": {
                    "host_name": {"type": "string"},
                    "dry_run": {"type": "boolean", "default": True},
                },
                "required": ["host_name"],
            },
        ),
        Tool(
            name="exit_maintenance",
            description="讓指定 ESXi host 離開維護模式",
            inputSchema={
                "type": "object",
                "properties": {
                    "host_name": {"type": "string"},
                    "dry_run": {"type": "boolean", "default": True},
                },
                "required": ["host_name"],
            },
        ),
        Tool(
            name="shutdown_host",
            description="關閉 ESXi host（host 必須已在維護模式，dry_run=true 只預覽）",
            inputSchema={
                "type": "object",
                "properties": {
                    "host_name": {"type": "string"},
                    "dry_run": {"type": "boolean", "default": True},
                },
                "required": ["host_name"],
            },
        ),
    ]


@app.call_tool()
async def _call_tool(name: str, arguments: dict):
    try:
        if name == "list_vms":
            result = list_vms(arguments["cluster_name"])
        elif name == "suspend_vms":
            result = suspend_vms(arguments["vm_names"], arguments.get("dry_run", True))
        elif name == "enter_maintenance":
            result = enter_maintenance(arguments["host_name"], dry_run=arguments.get("dry_run", True))
        elif name == "exit_maintenance":
            result = exit_maintenance(arguments["host_name"], dry_run=arguments.get("dry_run", True))
        elif name == "shutdown_host":
            result = shutdown_host(arguments["host_name"], dry_run=arguments.get("dry_run", True))
        else:
            result = {"error": f"unknown tool: {name}"}
    except Exception as e:
        result = {"error": str(e)}

    return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
