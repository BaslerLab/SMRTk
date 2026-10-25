"""
chimerax_mcp_server.py - Lightweight ChimeraX FastMCP Bridge for SMRTk
========================================================================
Runs a FastMCP server in WSL2 that connects to UCSF ChimeraX's REST API
running on the host (e.g. Windows 11) or locally.

HOW IT WORKS:
1. UCSF ChimeraX has a native REST remote control server built-in.
   In ChimeraX's GUI command bar, run:
       remotecontrol rest start port 21049
2. This bridge runs in WSL2 and exposes standardized MCP tools over SSE or stdio.
3. Commands sent by SMRTk are relayed to ChimeraX via:
       http://<HOST_IP>:21049/run?command=<cmd>
4. Because any ChimeraX command (open, surface, color, matchmaker, alphafold,
   save, python) can be passed to /run, this bridge gives 100% full coverage
   of all ChimeraX features without tool-schema bloat.

STARTING THIS SERVER:
    pixi run python chimerax_mcp_server.py
    # or with custom ports/hosts:
    pixi run python chimerax_mcp_server.py --port 21050 --cx-port 21049

CONNECTING IN SMRTK:
    /mcp on chimerax
"""

import os
import sys
import argparse
import html
import re
import base64
import time
from typing import Optional, List

try:
    import httpx
except ImportError:
    httpx = None

try:
    import urllib.request
    import urllib.parse
    import urllib.error
except ImportError:
    pass

from fastmcp import FastMCP

# Try importing host IP resolution from SMRTk harness
try:
    from mcp_config import resolve_host_ip, HOST_IP
except ImportError:
    def resolve_host_ip() -> str:
        return os.environ.get("HOST_IP", "127.0.0.1")
    HOST_IP = resolve_host_ip()

# Initialize FastMCP Server
mcp = FastMCP(
    name="ChimeraX",
)

# Global connection parameters (populated at runtime or via CLI)
CX_PORT: int = int(os.environ.get("CHIMERAX_PORT", 21049))
CX_HOST_OVERRIDE: Optional[str] = os.environ.get("CHIMERAX_HOST", None)
_cached_active_host: Optional[str] = None


def get_candidate_hosts() -> List[str]:
    """Returns candidate IPs to probe for ChimeraX REST server."""
    if CX_HOST_OVERRIDE:
        return [CX_HOST_OVERRIDE]
    candidates = []
    # 1. In WSL2 mirrored networking mode or native Linux, 127.0.0.1 connects directly
    candidates.append("127.0.0.1")
    # 2. In WSL2 NAT mode, Windows host IP from default gateway / DNS
    if HOST_IP and HOST_IP not in candidates:
        candidates.append(HOST_IP)
    for alias in ["host.wsl.internal", "host.containers.internal", "host.docker.internal"]:
        if alias not in candidates:
            candidates.append(alias)
    return candidates


def _clean_response(content: str) -> str:
    """Cleans up raw HTML tags from ChimeraX response if present."""
    if not content:
        return "Command executed successfully (no output)."
    if "__B64__" in content:
        return content
    # If HTML markup is detected, strip tags while preserving linebreaks
    if "<html" in content.lower() or "<body" in content.lower() or "<pre" in content.lower():
        text = re.sub(r'<br\s*/?>', '\n', content, flags=re.IGNORECASE)
        text = re.sub(r'<p.*?>', '\n', text, flags=re.IGNORECASE)
        text = re.sub(r'<[^>]+>', '', text)
        text = html.unescape(text).strip()
        return text if text else "Command executed successfully (no output)."
    return content.strip()


def send_chimerax_command(command: str, timeout: float = 60.0) -> str:
    """
    Sends an arbitrary command string to ChimeraX REST API.
    Probes candidate host addresses dynamically if not yet connected.
    """
    global _cached_active_host
    candidate_hosts = [_cached_active_host] if _cached_active_host else get_candidate_hosts()
    attempted_urls = []
    last_error = None

    for host in candidate_hosts:
        url = f"http://{host}:{CX_PORT}/run"
        attempted_urls.append(url)
        try:
            if httpx:
                with httpx.Client(timeout=timeout) as client:
                    resp = client.get(url, params={"command": command})
                    if resp.status_code == 200:
                        _cached_active_host = host
                        return _clean_response(resp.text)
                    else:
                        return f"[ChimeraX Error {resp.status_code}]: {_clean_response(resp.text)}"
            else:
                encoded_cmd = urllib.parse.urlencode({"command": command})
                req_url = f"{url}?{encoded_cmd}"
                req = urllib.request.Request(req_url)
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    raw = response.read().decode("utf-8", errors="replace")
                    _cached_active_host = host
                    return _clean_response(raw)

        except Exception as e:
            last_error = str(e)
            if _cached_active_host == host:
                _cached_active_host = None  # Reset cache and try other candidates

    # If cached host failed, try remaining candidate hosts once
    if len(candidate_hosts) == 1 and not CX_HOST_OVERRIDE:
        for host in get_candidate_hosts():
            if host in candidate_hosts:
                continue
            url = f"http://{host}:{CX_PORT}/run"
            attempted_urls.append(url)
            try:
                if httpx:
                    with httpx.Client(timeout=timeout) as client:
                        resp = client.get(url, params={"command": command})
                        if resp.status_code == 200:
                            _cached_active_host = host
                            return _clean_response(resp.text)
                        else:
                            return f"[ChimeraX Error {resp.status_code}]: {_clean_response(resp.text)}"
                else:
                    encoded_cmd = urllib.parse.urlencode({"command": command})
                    req_url = f"{url}?{encoded_cmd}"
                    req = urllib.request.Request(req_url)
                    with urllib.request.urlopen(req, timeout=timeout) as response:
                        raw = response.read().decode("utf-8", errors="replace")
                        _cached_active_host = host
                        return _clean_response(raw)
            except Exception as e:
                last_error = str(e)

    return (
        f"[ChimeraX Connection Error]: Unable to reach ChimeraX REST server on port {CX_PORT}.\n"
        f"Attempted endpoints: {', '.join(attempted_urls)}\n"
        f"Last system error: {last_error}\n\n"
        f"Troubleshooting Checklist:\n"
        f"1. Is UCSF ChimeraX running on the host machine?\n"
        f"2. Have you started the REST server in ChimeraX? In the command bar run:\n"
        f"       remotecontrol rest start port {CX_PORT}\n"
        f"3. In WSL2 NAT mode, Windows binds 127.0.0.1 by default. Either:\n"
        f"   - Enable mirrored networking in %USERPROFILE%/.wslconfig (networkingMode=mirrored), OR\n"
        f"   - Forward port 21049 in Windows PowerShell (Admin):\n"
        f"       netsh interface portproxy add v4tov4 listenport={CX_PORT} listenaddress=0.0.0.0 connectport={CX_PORT} connectaddress=127.0.0.1\n"
        f"   - Or specify the exact host IP via CHIMERAX_HOST=<ip>.\n"
    )


# ==============================================================================
# SECURITY FIREWALL & COMMAND WHITELISTS
# ==============================================================================
# Whitelist of approved, safe ChimeraX commands for molecular visualization & modeling
SAFE_CHIMERAX_VERBS = {
    "open",
    "color",
    "label",
    "surface",
    "hide",
    "show",
    "view",
    "matchmaker",
    "align",
    "save",
    "close",
    "set",
    "style",
    "coulombic",
    "alphafold",
    "size",
    "transparency",
    "lighting",
    "camera",
    "info",
    "select",
    "sym",
}

# Regex to detect dangerous code execution keywords or shell escapes
DANGEROUS_RE = re.compile(
    r"\b(python|runscript|system|powershell|cmd|bash|sh|exec|eval|import|subprocess|socket|shutil)\b"
    r"|(\bos\.)|(\bsys\.)|(__)",
    re.IGNORECASE,
)

# Forbidden executable/script file extensions
DANGEROUS_EXTS = [
    ".py", ".pyw", ".exe", ".bat", ".cmd", ".ps1", ".vbs", ".sh",
    ".bash", ".dll", ".so", ".bin", ".msi", ".com", ".js", ".scr"
]


def validate_command(cmd: str) -> tuple[bool, str]:
    """Strictly validates a ChimeraX command string before relaying to Windows."""
    cmd_clean = cmd.strip()
    if not cmd_clean:
        return False, "Empty command"

    # 1. Block command chaining and shell metacharacters
    if any(char in cmd_clean for char in [";", "\n", "\r", "&", "|", "`", "$"]):
        return False, (
            "Command chaining or special characters (';', '&', '|', '`', '$', newline) are forbidden. "
            "Please issue only a single, simple command at a time."
        )

    # 2. Block dangerous execution keywords
    if DANGEROUS_RE.search(cmd_clean):
        return False, "Dangerous keyword detected. Python, script, or system execution is strictly forbidden."

    # 3. Block executable or script file extensions
    cmd_lower = cmd_clean.lower()
    for ext in DANGEROUS_EXTS:
        if ext in cmd_lower:
            return False, f"Forbidden file extension detected: '{ext}'"

    # 4. Enforce structural biology verb whitelist
    tokens = cmd_clean.split()
    verb = tokens[0].lower()
    # Handle model/chain prefixes like '#1 style ...' or '/A:1-50 ...'
    if (verb.startswith("#") or verb.startswith("/")) and len(tokens) > 1:
        verb = tokens[1].lower()

    if verb not in SAFE_CHIMERAX_VERBS:
        return False, (
            f"Command verb '{verb}' is not in the safe ChimeraX whitelist. "
            f"Allowed verbs: {', '.join(sorted(SAFE_CHIMERAX_VERBS))}"
        )

    return True, "OK"


# ==============================================================================
# FAST-MCP TOOLS EXPOSED TO THE OVERSEER BRAIN
# ==============================================================================

@mcp.tool()
def run_command(command: str) -> str:
    """
    Executes a single, safe UCSF ChimeraX visualization/modeling command.

    SECURITY RESTRICTIONS:
    - Only single, simple commands are permitted. Command chaining (';', '&', '|', newlines) is BLOCKED.
    - Python execution ('python', 'runscript'), system commands, and shell escapes are STRICTLY FORBIDDEN.
    - Only approved structural biology verbs are allowed:
      open, color, label, surface, hide, show, view, matchmaker, align, save, close, set, style,
      coulombic, alphafold, size, transparency, lighting, camera, info, select, sym.

    CRITICAL CHIMERAX SYNTAX CHEATSHEET:
    - Selecting residues by name uses colon ':' (NOT '/resn'):
      * Acidic (negatively charged): ':ASP,GLU'
      * Basic (positively charged):  ':LYS,ARG,HIS'
      * Specific chain/residues:     '/A:1-50', '#1/B:ASP'
      * Inverted (all except):       ':^ASP,GLU'
    - Coloring residues by charge/type:
      * Step 1 (reset to base):      'color all lightgray'
      * Step 2 (negative/acidic):    'color :ASP,GLU red'
      * Step 3 (positive/basic):     'color :LYS,ARG,HIS blue'
      * By chain:                    'color bychain #1'
    - Labeling residues:
      * Show residue labels:         'label :ASP,GLU,LYS,ARG,HIS text "{name}{number}"'
      * Remove all labels:           'label clear'
    - Representations:
      * Cartoons:                    'show cartoons'
      * Hide all atoms:              'hide atoms'
      * Sidechains:                  'show :ASP,GLU,LYS,ARG,HIS sticks'
      * Molecular surface:           'surface #1'
      * Orient camera:               'view orient' or 'view'
    - DISCIPLINE: If a command returns a syntax error, DO NOT enter a retry loop with minor variations;
      report the output and stop.
    """
    is_valid, reason = validate_command(command)
    if not is_valid:
        return f"[Security Block]: Command rejected by ChimeraX safety firewall: {reason}"
    return send_chimerax_command(command)


@mcp.tool()
def open_structure(specifier: str) -> str:
    """
    Opens a molecular structure by PDB ID, local file path, or URL in the live ChimeraX GUI window on the host.

    CRITICAL USAGE DIRECTIVES:
    - This tool opens the structure in the live ChimeraX GUI on the user's desktop screen.
    - Once loaded, the user can see and interact with the structure directly in ChimeraX.
    - DO NOT follow this tool with unsolicited styling commands (cartoons, ribbons, colors, surfaces)
      or unprompted save_image calls unless the user explicitly requested them in their prompt.
    - If the user's prompt is to 'open' a structure (e.g. 'open VgrG in Chimera'), this tool ALONE
      100% completes the task. Immediately report the PDB ID loaded and conclude your turn.
    """
    spec_clean = specifier.strip()
    if any(char in spec_clean for char in [";", "\n", "\r", "&", "|", "`", "$"]):
        return "[Security Block]: Special characters and command chaining are forbidden in structure specifier."
    for ext in DANGEROUS_EXTS:
        if spec_clean.lower().endswith(ext):
            return f"[Security Block]: Forbidden file extension '{ext}' in open_structure."
    if DANGEROUS_RE.search(spec_clean):
        return "[Security Block]: Dangerous keyword detected in structure specifier."
    return send_chimerax_command(f"open {spec_clean}")


@mcp.tool()
def save_image(filepath: str = "chimerax_snapshot.png", width: int = 1920, height: int = 1080, transparent: bool = True) -> str:
    """
    Renders and saves the current ChimeraX 3D viewport to an image file.

    USAGE INSTRUCTIONS:
    - ONLY call this tool if the user EXPLICITLY requested an image, screenshot, or render in their prompt.
    - DO NOT call this tool unprompted after opening a structure.
    - The image is saved directly into the shared host input directory:
      Windows UNC path: \\\\wsl.localhost\\Ubuntu-26.04\\home\\agent\\ai_workspace\\my_host_input\\<filename>
    - Inside your Linux sandbox, the saved image is immediately available at:
      /app/host_input/<filename>
    - You can directly inspect or pass '/app/host_input/<filename>' to analyze_files.
    - DO NOT search /tmp or /app/workspace for the image; inspect /app/host_input/<filename>.
    """
    filename = os.path.basename(filepath.strip()) if filepath else "chimerax_snapshot.png"
    filename = re.sub(r'[^A-Za-z0-9._-]', '_', filename)
    if not filename or filename.startswith("."):
        filename = "chimerax_snapshot.png"
    if not os.path.splitext(filename)[1]:
        filename += ".png"
    for ext in DANGEROUS_EXTS:
        if filename.lower().endswith(ext):
            return f"[Security Block]: Forbidden file extension '{ext}' in save_image."

    host_input_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "my_host_input")
    os.makedirs(host_input_dir, exist_ok=True)
    local_path = os.path.join(host_input_dir, filename)
    sandbox_path = f"/app/host_input/{filename}"
    t_flag = "true" if transparent else "false"
    distro = os.environ.get("WSL_DISTRO_NAME", "Ubuntu-26.04")

    # Clean up prior file if present
    if os.path.exists(local_path):
        try:
            os.remove(local_path)
        except OSError:
            pass

    # Method 1: Try Direct Save via Windows UNC Path to WSL2
    win_rel_path = local_path.lstrip("/").replace("/", "\\")
    unc_path = f"\\\\wsl.localhost\\{distro}\\{win_rel_path}"
    unc_alt = f"\\\\wsl$\\{distro}\\{win_rel_path}"
    unc_fwd = f"//wsl.localhost/{distro}/{local_path.lstrip('/')}"

    for unc in [unc_path, unc_alt, unc_fwd]:
        res = send_chimerax_command(f'save "{unc}" width {width} height {height} transparent {t_flag}')
        if "[ChimeraX Connection Error]" in res:
            return res
        time.sleep(0.3)
        if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
            return (
                f"Snapshot successfully rendered by ChimeraX!\n"
                f"Saved to: {sandbox_path} ({os.path.getsize(local_path)} bytes)\n"
                f"You can now inspect or verify the image directly in the sandbox at: {sandbox_path}"
            )

    # Method 2: Automatic Network Bridge Fallback
    # If Windows UNC path is unreachable, save locally in ChimeraX and transfer bytes over REST
    temp_cx_name = f"cx_snap_{int(time.time())}.png"
    save_res = send_chimerax_command(f'save "{temp_cx_name}" width {width} height {height} transparent {t_flag}')
    if "[ChimeraX Connection Error]" in save_res:
        return save_res

    pull_script = (
        f"python import base64, os; "
        f"p = os.path.abspath('{temp_cx_name}'); "
        f"data = open(p, 'rb').read() if os.path.exists(p) else b''; "
        f"print('__B64__' + base64.b64encode(data).decode('ascii') + '__B64__') if data else print('__NO_DATA__')"
    )
    pull_res = send_chimerax_command(pull_script)

    if "__B64__" in pull_res:
        try:
            b64_data = pull_res.split("__B64__")[1].strip()
            raw_bytes = base64.b64decode(b64_data)
            with open(local_path, "wb") as f:
                f.write(raw_bytes)
            # Cleanup temp file in ChimeraX
            send_chimerax_command(f"python import os; os.remove(os.path.abspath('{temp_cx_name}')) if os.path.exists(os.path.abspath('{temp_cx_name}')) else None")
            return (
                f"Snapshot successfully rendered by ChimeraX!\n"
                f"Saved to: {sandbox_path} ({len(raw_bytes)} bytes)\n"
                f"You can now inspect or verify the image directly in the sandbox at: {sandbox_path}"
            )
        except Exception as e:
            return f"[Error transferring rendered image to host input]: {e}"

    return (
        f"[Warning]: ChimeraX executed save command, but image could not be written to {sandbox_path}.\n"
        f"ChimeraX output: {save_res}\n"
        f"Transfer output: {pull_res}"
    )


@mcp.tool()
def close_models(target: str = "all") -> str:
    """
    Closes open molecular models in the active ChimeraX session.
    
    Arguments:
    - target: Model specifier (e.g. '#1', '#1,2') or 'all' to clear the entire session.
    """
    cmd = "close" if target.strip().lower() == "all" else f"close {target.strip()}"
    is_valid, reason = validate_command(cmd)
    if not is_valid:
        return f"[Security Block]: Command rejected by ChimeraX safety firewall: {reason}"
    return send_chimerax_command(cmd)




@mcp.tool()
def get_status() -> str:
    """
    Checks connection health to UCSF ChimeraX and reports active status and responsiveness.
    """
    res = send_chimerax_command("view")
    if "[ChimeraX Connection Error]" in res:
        return res
    active_host = _cached_active_host or "host"
    return f"ChimeraX is online and responding at http://{active_host}:{CX_PORT}. (View refreshed successfully)"


# ==============================================================================
# MAIN ENTRYPOINT
# ==============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ChimeraX FastMCP Bridge for SMRTk")
    parser.add_argument("--transport", choices=["sse", "stdio"], default="sse", help="Transport mode (default: sse)")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address for FastMCP server (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=21050, help="Port for FastMCP SSE server (default: 21050)")
    parser.add_argument("--cx-host", default=None, help="Explicit host address for ChimeraX REST (default: auto-detect)")
    parser.add_argument("--cx-port", type=int, default=21049, help="ChimeraX REST port (default: 21049)")

    args = parser.parse_args()

    CX_PORT = args.cx_port
    if args.cx_host:
        CX_HOST_OVERRIDE = args.cx_host

    if args.transport == "sse":
        print(f"============================================================")
        print(f" ChimeraX FastMCP SSE Bridge Server")
        print(f"============================================================")
        print(f" FastMCP SSE URL : http://{args.host}:{args.port}/sse")
        print(f" ChimeraX Target : http://{CX_HOST_OVERRIDE or 'auto-detect'}:{CX_PORT}/run")
        print(f" Candidate Hosts : {', '.join(get_candidate_hosts())}")
        print(f" Connect in Chat : /mcp on chimerax http://127.0.0.1:{args.port}/sse")
        print(f"============================================================")
        mcp.run(transport="sse", host=args.host, port=args.port)
    else:
        mcp.run()
