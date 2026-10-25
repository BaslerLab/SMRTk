"""
mcp_config.py - External & Host MCP Server Configurations
==========================================================
Defines external Model Context Protocol (MCP) servers accessible by the
Overseer Brain outside the default Podman sandbox (e.g. running on the host,
in WSL2, on a remote server, or another container).

All external servers are strictly OPTIONAL by default:
- If a server is enabled but offline / unreachable, the harness will log a
  warning and continue seamlessly without crashing.
- Servers can be toggled via CLI flags (--mcp <name>) or interactively in-chat
  via slash commands (/mcp on <name>, /mcp off <name>).
- Dangerous or host-modifying tools can require interactive user confirmation
  before execution.
"""

import os
import socket
import subprocess
from typing import Dict, Any, Optional


def resolve_host_ip() -> str:
    """
    Dynamically resolves the IP or hostname of the host machine from the
    current execution environment (WSL2, container, or native Linux).
    """
    # 1. Explicit override via environment variable
    if os.environ.get("HOST_IP"):
        return os.environ["HOST_IP"]

    # 2. Try standard container/WSL DNS names
    for host_name in ["host.wsl.internal", "host.containers.internal", "host.docker.internal"]:
        try:
            socket.gethostbyname(host_name)
            return host_name
        except (socket.gaierror, OSError):
            pass

    # 3. Detect default gateway from ip route (standard for WSL2 NAT and container bridges)
    try:
        route_out = subprocess.check_output(
            ["ip", "route", "show", "default"],
            stderr=subprocess.DEVNULL,
            timeout=1
        ).decode("utf-8")
        parts = route_out.split()
        if "via" in parts:
            gw_ip = parts[parts.index("via") + 1]
            return gw_ip
    except Exception:
        pass

    # 4. Fallback reading nameserver from /etc/resolv.conf
    try:
        if os.path.exists("/etc/resolv.conf"):
            with open("/etc/resolv.conf", "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("nameserver"):
                        ns_ip = line.split()[1]
                        if not ns_ip.startswith("127."):
                            return ns_ip
    except Exception:
        pass

    # 5. Default fallback (e.g. for mirrored networking mode or localhost)
    return "127.0.0.1"


HOST_IP = resolve_host_ip()

# ==============================================================================
# EXTERNAL MCP SERVER REGISTRY
# ==============================================================================
# Configuration keys per server:
# - name: Friendly display name.
# - description: Purpose of this MCP server.
# - transport: 'sse' (HTTP Server-Sent Events) or 'stdio' (subprocess command).
# - url: Required for 'sse' transport.
# - command / args: Required for 'stdio' transport.
# - enabled: Default boot status (False = off by default for zero-trust security).
# - optional: If True, connection failures are caught gracefully and will NEVER
#             crash the harness.
# - tool_prefix: Optional prefix attached to tool names to prevent collisions
#                with native sandbox tools (e.g. 'cx_run_command').
# - require_confirmation: If True, the Overseer pauses and interactively prompts
#                         the user (y/n) before executing any tool from this MCP.
# ==============================================================================

EXTERNAL_MCP_SERVERS: Dict[str, Dict[str, Any]] = {
    # --------------------------------------------------------------------------
    # Example 1: UCSF ChimeraX Molecular Visualization & Structural Biology
    # --------------------------------------------------------------------------
    # HOW CHIMERAX MCP WORKS IN THIS WORKSPACE:
    # 1. On Windows 11 host: In ChimeraX GUI console, start the REST server:
    #      remotecontrol rest start port 21049
    # 2. In WSL2: Start the included FastMCP bridge in a separate terminal:
    #      pixi run python chimerax_mcp_server.py
    #    (Listens on port 21050 and proxies commands to ChimeraX on Windows).
    # 3. In the chat overseer, connect via:
    #      /mcp on chimerax
    #
    # SECURITY & HARDENING ARCHITECTURE:
    # - ChimeraX's native REST API accepts raw commands with full user privileges.
    #   To protect the host, the included chimerax_mcp_server.py acts as a strict
    #   firewall between the AI sandbox container and Windows 11:
    #     * Raw Python execution ('run_python') is NEVER exposed to the AI.
    #     * Strict verb whitelist: Only safe structural commands are permitted
    #       (open, color, label, surface, hide, show, view, matchmaker, save, etc.).
    #     * Command chaining (';', '&', '|', newlines) and scripts are BLOCKED.
    # - When finished modeling, stop the REST listener in ChimeraX:
    #     remotecontrol rest stop
    # - For maximum zero-trust oversight, set 'require_confirmation: True' below
    #   to prompt [y/N] before executing each ChimeraX action.
    # --------------------------------------------------------------------------
    "chimerax": {
        "name": "UCSF ChimeraX",
        "description": "Molecular visualization, PDB rendering, and structural biology analysis (chimerax_mcp_server.py)",
        "transport": "sse",
        "url": "http://127.0.0.1:21050/sse",
        "enabled": False,              # Off by default - toggle with --mcp chimerax or /mcp on chimerax
        "optional": True,              # Never crashes if ChimeraX bridge isn't currently running
        "tool_prefix": "cx_",          # Namespaces tools: cx_run_command, cx_open_structure, cx_save_image
        "require_confirmation": False, # Set True for interactive [y/N] approval before each host command
    },

    # --------------------------------------------------------------------------
    # Example 2: Generic Host Tools / Script Runner (Example with Security Gate)
    # --------------------------------------------------------------------------
    "host_tools": {
        "name": "Host System Tools",
        "description": "Host-level automation and notifications outside container",
        "transport": "sse",
        "url": f"http://{HOST_IP}:8080/sse",
        "enabled": False,
        "optional": True,
        "tool_prefix": "host_",
        "require_confirmation": True,  # Extra security gate: always asks user before executing!
    },

    # --------------------------------------------------------------------------
    # Example 3: Mock Test Server (Included Harness Verification Asset)
    # --------------------------------------------------------------------------
    # HOW TO START: In a separate terminal run:
    #   pixi run python mock_mcp_server.py
    # Exposes: mock_get_host_time, mock_multiply_numbers on port 8888.
    # --------------------------------------------------------------------------
    "mock": {
        "name": "Mock Test Server",
        "description": "Local test MCP server with time and math tools (mock_mcp_server.py)",
        "transport": "sse",
        "url": "http://127.0.0.1:8888/sse",
        "enabled": False,              # Off by default - toggle with --mcp mock or /mcp on mock
        "optional": True,              # Never crashes if mock server isn't running
        "tool_prefix": "mock_",        # Namespaces tools: mock_get_host_time, mock_multiply_numbers
        "require_confirmation": False, # Direct execution without user confirmation gate
    },
}


def get_server_config(server_id: str) -> Optional[Dict[str, Any]]:
    """Returns the configuration dict for a specific server_id."""
    return EXTERNAL_MCP_SERVERS.get(server_id)


def list_configured_servers() -> Dict[str, Dict[str, Any]]:
    """Returns the full external server registry."""
    return EXTERNAL_MCP_SERVERS
