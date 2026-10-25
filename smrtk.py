import os
import asyncio
import json
import time
import re
import copy
import subprocess
import traceback
import logging
import argparse
import sys
import atexit
import shutil
import hashlib
import types
from datetime import datetime
from openai import AsyncOpenAI
import tiktoken
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client
import mcp_config

# Ensure stdout uses line-buffering so redirected logs remain sequentially ordered
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

# --- MONKEY-PATCH MCP TO PREVENT CRASHES ON STDOUT POLLUTION ---
from mcp.types import JSONRPCMessage

_original_model_validate_json = JSONRPCMessage.model_validate_json

@classmethod
def robust_model_validate_json(cls, json_data: str | bytes, *args, **kwargs):
    if isinstance(json_data, bytes):
        line = json_data.decode('utf-8', errors='replace')
    else:
        line = json_data

    # Try validating directly first
    try:
        return _original_model_validate_json(json_data, *args, **kwargs)
    except Exception as original_exc:
        # Try to extract the JSON-RPC object if there is pollution around it
        try:
            start_idx = line.find('{')
            if start_idx != -1:
                end_idx = line.rfind('}')
                if end_idx != -1 and end_idx > start_idx:
                    cleaned_json = line[start_idx:end_idx + 1]
                    # Verify it's valid JSON
                    json.loads(cleaned_json)
                    return _original_model_validate_json(cleaned_json, *args, **kwargs)
        except Exception:
            pass
        
        # If it's completely non-JSON (like a print or system warning),
        # return a dummy notification to prevent the stdout_reader task from crashing.
        try:
            dummy_notification = '{"jsonrpc": "2.0", "method": "dummy/ignore"}'
            return _original_model_validate_json(dummy_notification, *args, **kwargs)
        except Exception:
            raise original_exc

JSONRPCMessage.model_validate_json = robust_model_validate_json
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.key_binding import KeyBindings
from contextlib import AsyncExitStack, nullcontext

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel

import config

# Can be run using arguments like this:
# pixi run python smrtk.py -p "tell me what tools do you have" -f markdown -v detailed -s "Test_2" --brain 2 --coder 3 --summarizer 3 --adviser 3 -x

# --- CLI ARGUMENT PARSER ---
parser = argparse.ArgumentParser(description="SMRTk (Sandboxed Multi-agent Research Toolkit)")
parser.add_argument("-p", "--prompt", type=str, help="Initial user prompt")
parser.add_argument("-f", "--format", choices=["markdown", "text"], help="Console format override")
parser.add_argument("-v", "--verbosity", choices=["silent", "minimal", "standard", "detailed"], help="Console verbosity override")
parser.add_argument("-s", "--session", type=str, help="Session ID to load or create")
parser.add_argument("-x", "--exit", action="store_true", help="Auto-exit after finishing the CLI prompt")
# Add the LLM profile overrides back:
parser.add_argument("--brain", type=int, help="Brain LLM profile index")
parser.add_argument("--coder", type=int, help="Coder LLM profile index")
parser.add_argument("--summarizer", type=int, help="Summarizer LLM profile index")
parser.add_argument("--adviser", type=int, help="Adviser LLM profile index")
parser.add_argument("--analyst", type=int, help="Analyst LLM profile index")
parser.add_argument("--architect", type=int, help="Architect LLM profile index")
parser.add_argument("--mcp", type=str, help="Comma-separated external MCP servers to enable (e.g. --mcp chimerax)")
parser.add_argument("--all-mcp", action="store_true", help="Enable all configured external MCP servers")
parser.add_argument("--skills", type=str, help="Comma-separated host skill packs or skills to load into session (e.g. --skills development,mcp-builder)")
parser.add_argument("--all-skills", action="store_true", help="Load all available skills from ./skills into the session")

cli_args = parser.parse_args()

# Apply Overrides BEFORE any LLM clients initialize
if cli_args.format: config.FORMAT_MODE = cli_args.format
if cli_args.verbosity: config.VERBOSITY_MODE = cli_args.verbosity
if cli_args.session:
    if not re.fullmatch(r"^[A-Za-z0-9._-]{1,64}$", cli_args.session):
        parser.error(f"Invalid session ID '{cli_args.session}'. Must be 1-64 characters matching [A-Za-z0-9._-].")
    config.SESSION_ID = cli_args.session
if cli_args.brain is not None: config.ACTIVE_BRAIN_PROFILE = cli_args.brain
if cli_args.coder is not None: config.ACTIVE_CODER_PROFILE = cli_args.coder
if cli_args.summarizer is not None: config.ACTIVE_SUMMARIZER_PROFILE = cli_args.summarizer
if cli_args.adviser is not None: config.ACTIVE_ADVISER_PROFILE = cli_args.adviser
if cli_args.analyst is not None: config.ACTIVE_ANALYST_PROFILE = cli_args.analyst
if cli_args.architect is not None: config.ACTIVE_ARCHITECT_PROFILE = cli_args.architect

# Capture Prompt (from -p flag OR piped STDIN)
cli_prompt = cli_args.prompt
if not cli_prompt and not sys.stdin.isatty():
    # This grabs piped text like: cat input.txt | python smrtk.py
    cli_prompt = sys.stdin.read().strip()
   
# Mute the MCP client's internal info logs
logging.getLogger("mcp").setLevel(logging.WARNING)
logging.getLogger("fastmcp").setLevel(logging.WARNING)

# Initialize the rich console
console = Console()

# --- CLI Colors ---
COLOR_RED = '\033[91m'
COLOR_BLUE = '\033[94m'
COLOR_YELLOW = '\033[93m'
COLOR_BRIGHT_GREEN = '\033[92m'
COLOR_DARK_GREEN = '\033[32m'
COLOR_ORANGE = '\033[38;5;208m' # ANSI 256-color orange
COLOR_DIM = '\033[2m'   # Dim text for "thinking"
COLOR_RESET = '\033[0m'
COLOR_CYAN = '\033[96m'

# --- LLM setups ---
tokenizer = tiktoken.get_encoding("cl100k_base")

brain_profile = config.LLM_PROFILES[config.ACTIVE_BRAIN_PROFILE]
if brain_profile.get("base_url"):
    brain_client = AsyncOpenAI(base_url=brain_profile["base_url"], api_key=brain_profile["api_key"], timeout=180.0)
else:
    brain_client = AsyncOpenAI(api_key=brain_profile["api_key"], timeout=180.0)

# --- SESSION & DIRECTORY SETUP ---
timestamp = datetime.now().strftime("%Y%m%d%H%M%S")

if config.SESSION_ID:
    active_session = config.SESSION_ID if str(config.SESSION_ID).startswith("Session_ID_") else f"Session_ID_{config.SESSION_ID}"
else:
    active_session = f"Session_ID_{timestamp}"

SESSION_DIR = os.path.realpath(os.path.abspath(f"./sessions/{active_session}"))
is_resuming = os.path.exists(SESSION_DIR)

# Build the isolated folder structure
for folder in [
    "logs", "tools", "histories", "histories/tool_outputs", "memories",
    "state", "state/rest_calls", "state/reports", "state/failures", "state/jobs",
    "sandbox", "outputs", "archive", "skills", "custom_packages"
]:
    os.makedirs(f"{SESSION_DIR}/{folder}", exist_ok=True)

# Build the isolated folder structure for input/output and host skills folders
os.makedirs(config.HOST_INPUT_DIR, exist_ok=True)
os.makedirs(config.HOST_SKILLS_DIR, exist_ok=True)

# --- Pixi environment already baked into the podman container! ---

# Point the log file to this specific session
LOG_FILE = f"{SESSION_DIR}/logs/chat_log_{timestamp}.txt"
CURRENT_HISTORY_FILE = f"{SESSION_DIR}/state/current_history.json"
ACTIVE_SKILLS_FILE = f"{SESSION_DIR}/state/active_skills.json"


def load_active_skills_state():
    if os.path.exists(ACTIVE_SKILLS_FILE):
        try:
            with open(ACTIVE_SKILLS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_active_skills_state(state_data):
    try:
        with open(ACTIVE_SKILLS_FILE, "w", encoding="utf-8") as f:
            json.dump(state_data, f, indent=2)
    except Exception:
        pass


def discover_host_skills():
    """Recursively scans config.HOST_SKILLS_DIR for folders containing SKILL.md."""
    host_root = os.path.realpath(config.HOST_SKILLS_DIR)
    discovered = []
    if not os.path.exists(host_root):
        return discovered

    for root, dirs, files in os.walk(host_root):
        dirs.sort()
        if "SKILL.md" in files:
            skill_md_path = os.path.join(root, "SKILL.md")
            rel_path = os.path.relpath(root, host_root).replace("\\", "/")
            skill_name = os.path.basename(root)
            parent_rel = os.path.dirname(rel_path).replace("\\", "/")
            pack_name = parent_rel if parent_rel and parent_rel != "." else "(root)"
            try:
                with open(skill_md_path, "r", encoding="utf-8") as f:
                    desc = config.extract_skill_description(f.read())
            except Exception:
                desc = "No description provided."
            discovered.append({
                "name": skill_name,
                "rel_path": rel_path,
                "pack": pack_name,
                "abs_path": root,
                "description": desc,
            })
            # Do not descend into subdirectories of a skill folder
            dirs[:] = []
    return discovered


def install_host_skills(targets_str: str, verbose: bool = True):
    """Copies matching skill packs or individual skills from host ./skills into SESSION_DIR/skills."""
    host_skills = discover_host_skills()
    raw_targets = [t.strip().strip("/") for t in targets_str.split(",") if t.strip().strip("/")]
    matched = []
    unmatched = []
    seen_names = set()

    for t in raw_targets:
        q = t.lower()
        found_for_t = False
        for s in host_skills:
            rel_l = s["rel_path"].lower()
            pack_l = s["pack"].lower()
            name_l = s["name"].lower()
            if (
                q == "all"
                or f"/{q}/" in f"/{rel_l}/"
                or (pack_l and f"/{q}/" in f"/{pack_l}/")
                or name_l == q
            ):
                found_for_t = True
                if s["name"] not in seen_names:
                    seen_names.add(s["name"])
                    matched.append(s)
        if not found_for_t:
            unmatched.append(t)

    if not matched:
        return [], unmatched

    skills_dir = os.path.join(SESSION_DIR, "skills")
    os.makedirs(skills_dir, exist_ok=True)
    state_data = load_active_skills_state()
    installed = []

    for s in matched:
        dst_path = os.path.join(skills_dir, s["name"])
        shutil.copytree(s["abs_path"], dst_path, dirs_exist_ok=True, symlinks=False)
        state_data[s["name"]] = {
            "pack": s["pack"],
            "rel_path": s["rel_path"],
            "loaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        installed.append(s)

    save_active_skills_state(state_data)
    if verbose and config.VERBOSITY_MODE != "silent" and installed:
        names_str = ", ".join(f"{s['name']} ({s['pack']})" for s in installed)
        print(f"\n{COLOR_BRIGHT_GREEN}[SKILLS SYSTEM: Copied {len(installed)} skill(s) into session workspace: {names_str}]{COLOR_RESET}")
    return installed, unmatched


def uninstall_session_skills(targets_str: str, verbose: bool = True):
    """Removes matching skills or skill packs from SESSION_DIR/skills (host ./skills stays untouched)."""
    skills_dir = os.path.join(SESSION_DIR, "skills")
    if not os.path.exists(skills_dir):
        return [], [targets_str]

    state_data = load_active_skills_state()
    installed_names = sorted(
        item for item in os.listdir(skills_dir)
        if os.path.exists(os.path.join(skills_dir, item, "SKILL.md"))
    )
    raw_targets = [t.strip().strip("/") for t in targets_str.split(",") if t.strip().strip("/")]
    to_remove = []
    unmatched = []

    for t in raw_targets:
        q = t.lower()
        found_for_t = False
        for s_name in installed_names:
            meta = state_data.get(s_name, {})
            rel_l = meta.get("rel_path", s_name).lower()
            pack_l = meta.get("pack", "").lower()
            name_l = s_name.lower()
            if (
                q == "all"
                or name_l == q
                or f"/{q}/" in f"/{rel_l}/"
                or (pack_l and f"/{q}/" in f"/{pack_l}/")
            ):
                found_for_t = True
                if s_name not in to_remove:
                    to_remove.append(s_name)
        if not found_for_t:
            unmatched.append(t)

    removed = []
    for s_name in to_remove:
        target_dir = os.path.realpath(os.path.join(skills_dir, s_name))
        if os.path.commonpath([target_dir, SESSION_DIR]) == SESSION_DIR and os.path.exists(target_dir):
            shutil.rmtree(target_dir)
            state_data.pop(s_name, None)
            removed.append(s_name)

    save_active_skills_state(state_data)
    if verbose and config.VERBOSITY_MODE != "silent" and removed:
        print(f"\n{COLOR_YELLOW}[SKILLS SYSTEM: Unloaded {len(removed)} skill(s) from session workspace: {', '.join(removed)}]{COLOR_RESET}")
    return removed, unmatched


def save_session_skill_to_host(skill_name: str, pack_subpath: str):
    """Copies a session skill from SESSION_DIR/skills/<skill_name> into host ./skills/<pack_subpath>/<skill_name>."""
    safe_skill = os.path.basename(skill_name.strip())
    src_dir = os.path.realpath(os.path.join(SESSION_DIR, "skills", safe_skill))
    if os.path.commonpath([src_dir, SESSION_DIR]) != SESSION_DIR or not os.path.exists(os.path.join(src_dir, "SKILL.md")):
        return False, f"Skill '{safe_skill}' not found in current session (/app/workspace/skills)."

    clean_pack = os.path.normpath(pack_subpath.strip().strip("/")).replace("\\", "/")
    if not clean_pack or clean_pack.startswith(".."):
        return False, f"Invalid pack path '{pack_subpath}'."

    host_root = os.path.realpath(config.HOST_SKILLS_DIR)
    dst_dir = os.path.realpath(os.path.join(host_root, clean_pack, safe_skill))
    if os.path.commonpath([dst_dir, host_root]) != host_root:
        return False, f"Invalid destination path '{pack_subpath}'."

    os.makedirs(os.path.dirname(dst_dir), exist_ok=True)
    shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True, symlinks=False)
    rel_path = f"{clean_pack}/{safe_skill}"
    state_data = load_active_skills_state()
    state_data[safe_skill] = {
        "pack": clean_pack,
        "rel_path": rel_path,
        "loaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_active_skills_state(state_data)
    return True, rel_path


def build_skills_menu():
    """Scans the sandbox workspace for mounted SKILL.md files and appends a compact index of unmounted host packs."""
    skills_dir = os.path.join(SESSION_DIR, "skills")
    mounted_names = set()
    menu_lines = ["AVAILABLE SKILLS MENU (Mounted in /app/workspace/skills — call `load_skill(skill_name)` to read full instructions):"]

    if os.path.exists(skills_dir):
        for item in sorted(os.listdir(skills_dir)):
            skill_path = os.path.join(skills_dir, item, "SKILL.md")
            # HOST PROTECTION: Reject symlinks pointing outside SESSION_DIR
            try:
                if os.path.commonpath([os.path.realpath(skill_path), SESSION_DIR]) != SESSION_DIR:
                    continue
            except Exception:
                continue
            if os.path.exists(skill_path):
                try:
                    with open(skill_path, "r", encoding="utf-8") as f:
                        content = f.read()
                    description = config.extract_skill_description(content)
                    menu_lines.append(f"- {item}: {description}")
                    mounted_names.add(item)
                except Exception:
                    pass

    if len(menu_lines) == 1:
        menu_lines.append("- None currently mounted in session. Use `manage_skills` to mount host packs, or `create_skill` to build new ones.")

    # Append compact summary of unmounted host skill packs
    host_skills = discover_host_skills()
    unmounted_by_pack = {}
    for s in host_skills:
        if s["name"] not in mounted_names:
            unmounted_by_pack.setdefault(s["pack"], []).append(s["name"])

    if unmounted_by_pack:
        menu_lines.append("\nUNMOUNTED HOST SKILL PACKS (Use `manage_skills(action='list'|'mount'|'unmount', target='...')` or call `load_skill(skill_name)` to auto-mount):")
        for pack_name in sorted(unmounted_by_pack.keys()):
            names_csv = ", ".join(unmounted_by_pack[pack_name])
            menu_lines.append(f"- {pack_name}: {names_csv}")

    return "\n".join(menu_lines)


# --- STATE MANAGEMENT HELPERS ---
def load_history():
    """Loads the true state of the brain from the hard drive."""
    skills_menu = build_skills_menu()
    system_prompt = f"{config.SYSTEM_PROMPTS['brain']}\n\n{skills_menu}"
    
    # HOST PROTECTION: Reject if CURRENT_HISTORY_FILE points outside SESSION_DIR
    try:
        if os.path.commonpath([os.path.realpath(CURRENT_HISTORY_FILE), SESSION_DIR]) != SESSION_DIR:
            init_state = [{"role": "system", "content": system_prompt}]
            save_history(init_state)
            return init_state
    except Exception:
        pass

    if not os.path.exists(CURRENT_HISTORY_FILE):
        init_state = [{"role": "system", "content": system_prompt}]
        save_history(init_state)
        return init_state
        
    with open(CURRENT_HISTORY_FILE, "r") as f: 
        messages = json.load(f)
        
    if messages and messages[0]["role"] == "system":
        messages[0]["content"] = system_prompt
        
    return messages

def save_history(messages):
    """Saves the active history. Strips thinking tokens using atomic writes."""
    clean_messages = []
    for msg in messages:
        clean_msg = copy.deepcopy(msg)
        clean_msg.pop("reasoning_content", None)
        clean_messages.append(clean_msg)
        
    # 1. Write to a temporary file
    temp_path = f"{CURRENT_HISTORY_FILE}.{os.getpid()}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f: 
        json.dump(clean_messages, f, indent=4)
        
    # 2. Atomically swap it
    os.replace(temp_path, CURRENT_HISTORY_FILE)


def _container_to_host_path(cpath: str) -> str:
    """Translates a container /app/workspace/... path to its host SESSION_DIR equivalent."""
    if not cpath:
        return ""
    if cpath.startswith("/app/workspace/"):
        rel = cpath[len("/app/workspace/"):]
        return os.path.join(SESSION_DIR, rel)
    return cpath


def harvest_background_jobs(messages: list) -> tuple[list, int, list]:
    """Checks state/jobs/bg_jobs.json for newly completed background jobs.
    Logs full sub-agent thoughts/outputs to LOG_FILE, injects completion notices into messages
    (only for jobs not already returned via manage_jobs(action='wait'|'status')),
    and returns (updated_messages, newly_notified_count, running_jobs_list).
    """
    state_dir = os.path.join(SESSION_DIR, "state")

    def _harvest_mutator(data):
        to_log = []
        to_notify = []
        running = []
        for jid, j in data.get("jobs", {}).items():
            if j.get("status") == "running":
                running.append(copy.deepcopy(j))
            else:
                if not j.get("logged_to_chat", False):
                    j["logged_to_chat"] = True
                    to_log.append(copy.deepcopy(j))
                if not j.get("notified", False):
                    j["notified"] = True
                    to_notify.append(copy.deepcopy(j))
        return to_log, to_notify, running

    to_log_jobs, to_notify_jobs, running_jobs = config.update_bg_jobs(state_dir, _harvest_mutator)
    if not to_log_jobs and not to_notify_jobs:
        return messages, 0, running_jobs

    notified_ids = {j.get("job_id") for j in to_notify_jobs}

    # 1. Log full sub-agent <thinking> & output to LOG_FILE (100% observability, never duplicated)
    for j in to_log_jobs:
        if j.get("type") == "subagent":
            jid = j.get("job_id", "job_?")
            ag = j.get("agent", "subagent")
            thinking = j.get("thinking", "")
            full_out = j.get("full_output", "")
            tok_in = j.get("tokens_in", 0)
            tok_out = j.get("tokens_out", 0)
            tok_think = j.get("thinking_tokens", 0)

            time_str = datetime.now().strftime("%H:%M:%S")
            display_label = f"BACKGROUND DELEGATED {ag.upper()} ({jid})"
            log_text = f"\n[{time_str}] === {display_label} (HIDDEN) ===\n"
            if thinking:
                log_text += f"<thinking>\n{thinking}\n</thinking>\n\n"
            if full_out:
                log_text += f"--- SUBAGENT OUTPUT ---\n{full_out}\n\n"
            if tok_in or tok_out:
                log_text += f"[Tokens: {tok_in} in | {tok_out} out (~{tok_think} thinking)]\n"
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(log_text.replace("\x00", ""))

            # Only print to console if not already shown by manage_jobs(wait/status)
            if config.VERBOSITY_MODE == "detailed" and jid in notified_ids:
                print(f"\n{COLOR_ORANGE}[{time_str}] === {display_label} (HIDDEN) ==={COLOR_RESET}")
                if thinking:
                    print(f"{COLOR_DIM}<thinking>\n{thinking}\n</thinking>{COLOR_RESET}")
                if full_out:
                    print(f"--- SUBAGENT OUTPUT ---\n{full_out}\n")

    # 2. Inject [SYSTEM NOTICE] into Brain's context ONLY for jobs not already returned via manage_jobs(wait/status)
    for j in to_notify_jobs:
        jid = j.get("job_id", "job_?")
        jtype = j.get("type", "bash")
        status_up = str(j.get("status", "completed")).upper()
        dur = j.get("duration_seconds", 0.0)

        if jtype == "subagent":
            ag = j.get("agent", "subagent")
            full_out = j.get("full_output", "")
            tok_in = j.get("tokens_in", 0)
            tok_out = j.get("tokens_out", 0)

            if j.get("status") == "completed":
                preview = j.get("result_preview") or full_out[:1500]
                out_file_note = f"\nSaved Deliverable: {j['output_filepath']}" if j.get("output_filepath") else ""
                notice_msg = (
                    f"[SYSTEM NOTICE: Background Sub-Agent Job '{jid}' ({ag.upper()}) {status_up} in {dur}s "
                    f"(Tokens: {tok_in} in, {tok_out} out).\n"
                    f"Full Trace File: {j.get('log_path')}{out_file_note}\n"
                    f"--- Result Preview ---\n{preview}\n----------------------]"
                )
            else:
                notice_msg = (
                    f"[SYSTEM NOTICE: Background Sub-Agent Job '{jid}' ({ag.upper()}) {status_up} after {dur}s.\n"
                    f"Error: {j.get('error', 'Unknown error')}\n"
                    f"Failure Trace: {j.get('failure_trace', 'N/A')}]"
                )
        else:
            host_log = _container_to_host_path(j.get("log_path", ""))
            tail_preview = "(no output)"
            if host_log and os.path.exists(host_log):
                try:
                    with open(host_log, "r", encoding="utf-8", errors="replace") as lf:
                        lines = lf.readlines()
                    if len(lines) >= 4 and lines[0].startswith("=== BACKGROUND BASH JOB:"):
                        lines = lines[4:]
                    tail_str = "".join(lines[-25:])
                    if len(tail_str) > 1500:
                        tail_str = "... [truncated] ...\n" + tail_str[-1500:]
                    tail_preview = tail_str.strip() or "(empty output)"
                except Exception:
                    pass
            notice_msg = (
                f"[SYSTEM NOTICE: Background Bash Job '{jid}' (PID {j.get('pid')}) {status_up} "
                f"(Exit Code: {j.get('exit_code')}, Duration: {dur}s).\n"
                f"Command: {(j.get('command') or '')[:160]}\n"
                f"Full Log File: {j.get('log_path')}\n"
                f"--- Log Tail Preview ---\n{tail_preview}\n------------------------]"
            )

        messages.append({"role": "user", "content": notice_msg})
        log_event("INJECTED CONTEXT TO BRAIN", notice_msg)

    return messages, len(to_notify_jobs), running_jobs

COMPACTION_MARKER = "HISTORICAL TOOL OUTPUT PRUNED"

def compact_historical_tool_outputs(messages, protect_recent: int = None, min_chars: int = None):
    """Prunes older tool outputs in history (> protect_recent tool calls ago) to prevent quadratic context blowup.
    Preserves recent active tool turns at 100% full fidelity.
    Only tool outputs exceeding min_chars are pruned; smaller outputs remain verbatim.
    Saves full output to disk and records metadata in manifest.json before compacting to guarantee zero data loss.
    Replaces pruned in-memory content with a lean pointer containing file path, character count, and preview."""
    if protect_recent is None:
        protect_recent = config.TOOL_PRUNE_PROTECT_RECENT_CALLS
    if min_chars is None:
        min_chars = config.TOOL_PRUNE_MIN_CHARS

    # 1. Identify all tool output messages by role, and those large enough to be prunable
    tool_indices = [idx for idx, msg in enumerate(messages) if msg.get("role") == "tool"]
    prunable_indices = [
        idx for idx in tool_indices
        if len(str(messages[idx].get("content", ""))) > min_chars or COMPACTION_MARKER in str(messages[idx].get("content", ""))
    ]
    
    if len(prunable_indices) <= protect_recent:
        return messages, 0
    
    # 2. Protect the most recent N prunable tool calls; all older large tool calls are pruning candidates
    protected_tool_indices = set(prunable_indices[-protect_recent:]) if protect_recent > 0 else set()
    
    manifest_path = os.path.join(SESSION_DIR, "histories/tool_outputs/manifest.json")
    manifest = {}
    manifest_updated = False
    pruned_count = 0
    
    new_messages = []
    for idx, msg in enumerate(messages):
        if idx in tool_indices and idx not in protected_tool_indices:
            content = str(msg.get("content", ""))
            
            # Skip if already pruned or under the character threshold
            if (COMPACTION_MARKER in content or 
                "characters of historical tool output compacted" in content or 
                len(content) <= min_chars):
                new_messages.append(msg)
                continue
                
            # Compute deterministic SHA1 digest for disk storage
            digest = hashlib.sha1(content.encode("utf-8", errors="replace")).hexdigest()[:16]
            rel_path = f"histories/tool_outputs/tool_output_{digest}.txt"
            host_path = os.path.join(SESSION_DIR, rel_path)
            
            # Save full verbatim output to disk safely
            try:
                if not os.path.exists(host_path):
                    os.makedirs(os.path.dirname(host_path), exist_ok=True)
                    with open(host_path, "w", encoding="utf-8") as f:
                        f.write(content)
            except Exception:
                new_messages.append(msg)
                continue
                
            # Maintain disk manifest
            try:
                if not manifest and os.path.exists(manifest_path):
                    with open(manifest_path, "r", encoding="utf-8") as mf:
                        manifest = json.load(mf)
                entry_key = f"tool_output_{digest}"
                if entry_key not in manifest:
                    manifest[entry_key] = {
                        "tool_name": msg.get("name", "tool"),
                        "tool_call_id": msg.get("tool_call_id", ""),
                        "char_count": len(content),
                        "sha1": digest,
                        "file_path": f"/app/workspace/{rel_path}",
                        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    }
                    manifest_updated = True
            except Exception:
                pass
                
            # Extract high-signal head + tail preview (preserving exit status & key start/end findings)
            raw_lines = [line.strip() for line in content.splitlines() if line.strip()]
            status_line = ""
            body_lines = []
            for ln in raw_lines:
                if not status_line and ln.lower().startswith("exit code:"):
                    status_line = ln[:100]
                elif ln.lower() == "output:":
                    continue
                else:
                    body_lines.append(ln)
            body_text = "\n".join(body_lines)
            head_snip = body_text[:300].replace("\n", " | ")
            tail_snip = body_text[-300:].replace("\n", " | ") if len(body_text) > 350 else ""
            preview_items = []
            if status_line:
                preview_items.append(f"Status: {status_line}")
            if head_snip:
                preview_items.append(f"Head Preview: {head_snip}")
            if tail_snip and tail_snip != head_snip:
                preview_items.append(f"Tail Preview: ... {tail_snip}")
            preview_text = ("\n".join(preview_items) + "\n") if preview_items else ""
            
            compacted_body = (
                f"[{COMPACTION_MARKER}: {len(content):,} characters pruned for context efficiency]\n"
                f"Tool: {msg.get('name', 'tool')} | Call ID: {msg.get('tool_call_id', 'unknown')}\n"
                f"Archived on disk: /app/workspace/{rel_path}\n"
                f"{preview_text}"
                f"[NOTE FOR BRAIN: Full verbatim output is preserved on disk. "
                f"To inspect complete output, use execute_bash ('cat /app/workspace/{rel_path}') or analyze_files.]"
            )
            msg_copy = copy.copy(msg)
            msg_copy["content"] = compacted_body
            new_messages.append(msg_copy)
            pruned_count += 1
            continue
            
        new_messages.append(msg)
        
    if manifest_updated:
        try:
            tmp_manifest = manifest_path + ".tmp"
            with open(tmp_manifest, "w", encoding="utf-8") as mf:
                json.dump(manifest, mf, indent=2)
            os.replace(tmp_manifest, manifest_path)
        except Exception:
            pass
            
    return new_messages, pruned_count

    
def estimate_tokens(messages):
    """Uses tiktoken for highly accurate estimation when the API receipt is voided."""
    total_text = ""
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    total_text += part.get("text", "")
        else:
            total_text += str(content or "")
        # Also count tool arguments if they exist
        if "tool_calls" in msg and msg["tool_calls"]:
            for tc in msg["tool_calls"]:
                total_text += str(tc.get("function", {}).get("arguments", ""))
                
    # Returns the actual token count with OpenAI structural message framing padding (4 tokens per turn)
    return len(tokenizer.encode(total_text)) + (len(messages) * 4)


def detect_text_loop(text: str, min_loop_length: int = 120, required_repeats: int = 3) -> tuple[bool, int]:
    """Normalized loop detector. Strips structural formatting noise to catch 
    loops that contain minor spacing, newline, or character variations.
    Returns (True, loop_size) if a loop repeating more than required_repeats is found.
    """
    # Remove all spaces, newlines, and punctuation, forcing pure lowercase alphanumeric text
    clean_text = re.sub(r'[^a-zA-Z0-9]', '', text.lower())
    L = len(clean_text)
    
    if L < min_loop_length * (required_repeats + 1):
        return False, 0
        
    # Widened evaluation window to catch large multi-paragraph loops
    max_check_window = min(L // (required_repeats + 1), 3000)
    for size in range(min_loop_length, max_check_window + 1):
        chunk = clean_text[-size:]
        is_loop = True
        for r in range(1, required_repeats + 1):
            start_idx = - (r + 1) * size
            end_idx = - r * size  # ◄--- FIX: Clean, fixed negative indexing slices perfectly
            prev_chunk = clean_text[start_idx:end_idx]
            if prev_chunk != chunk:
                is_loop = False
                break
        if is_loop:
            return True, size
            
    return False, 0

def clean_tail_by_alphanumeric_count(raw_text: str, alpha_count_to_remove: int) -> str:
    """Removes characters from the trailing end of raw_text until exactly 
    alpha_count_to_remove alphanumeric characters have been stripped out.
    Guarantees perfect word boundaries without trimming trailing words in half.
    """
    removed_alpha = 0
    idx = len(raw_text) - 1
    while idx >= 0 and removed_alpha < alpha_count_to_remove:
        if raw_text[idx].isalnum():
            removed_alpha += 1
        idx -= 1
    return raw_text[:idx + 1]


def log_event(role, content, usage=None, thinking=None, text_color=None, hide_console=False):
    time_str = datetime.now().strftime("%H:%M:%S")
    
    # 1. Plain Text Log File (Always writes full details)
    file_msg = f"\n[{time_str}] === {role.upper()} ===\n"
    if thinking:
        file_msg += f"<thinking>\n{thinking}\n</thinking>\n\n"
    file_msg += f"{content}\n"
    if usage and usage.prompt_tokens is not None:
        reasoning_str = ""
        r_tokens = 0
        if hasattr(usage, 'completion_tokens_details') and usage.completion_tokens_details:
            r_tokens = getattr(usage.completion_tokens_details, 'reasoning_tokens', 0)
        if r_tokens == 0 and thinking:
            r_tokens = len(thinking) // 4
        if r_tokens > 0:
            reasoning_str = f" (~{r_tokens} thinking)"
        file_msg += f"[Tokens: {usage.prompt_tokens} in | {usage.completion_tokens} out{reasoning_str}]\n"
        
    file_msg = file_msg.replace("\x00", "")
    with open(LOG_FILE, "a", encoding="utf-8") as f: 
        f.write(file_msg)

    # 2. Console logging (Respects Verbosity)
    if config.VERBOSITY_MODE == "silent" or hide_console:
        return

    if not role.upper().startswith("BRAIN"):
        if role.upper().startswith("TOOL"):
            console_header = f"\n{COLOR_BRIGHT_GREEN}[{time_str}] === {role.upper()} ==={COLOR_RESET}"
            actual_color = text_color if text_color else COLOR_DARK_GREEN
            console_content = f"{actual_color}{content}{COLOR_RESET}"
        elif role.upper() in ["INJECTED CONTEXT TO BRAIN", "BRAIN CONTEXT INJECTION"]:
            console_header = f"\n{COLOR_YELLOW}[{time_str}] === INJECTED CONTEXT TO BRAIN ==={COLOR_RESET}"
            actual_color = text_color if text_color else COLOR_YELLOW
            console_content = f"{actual_color}{content}{COLOR_RESET}"
        elif role.upper() == "MCP REGISTRY":
            console_header = f"\n{COLOR_CYAN}[{time_str}] === MCP REGISTRY ==={COLOR_RESET}"
            actual_color = text_color if text_color else COLOR_CYAN
            console_content = f"{actual_color}{content}{COLOR_RESET}"
        else:
            console_header = f"\n{COLOR_BLUE}[{time_str}] === {role.upper()} ==={COLOR_RESET}"
            console_content = f"{COLOR_RED}{content}{COLOR_RESET}" if role.upper() in ["USER", "YOU"] else content
            
        print(f"{console_header}\n{console_content}")
        if usage and usage.prompt_tokens is not None:
            print(f"{COLOR_YELLOW}[Tokens: {usage.prompt_tokens} in | {usage.completion_tokens} out]{COLOR_RESET}")
            

def format_tool_signature(tool_def):
    fn = tool_def.get("function", {})
    name = fn.get("name", "unknown")
    desc = fn.get("description", "")
    params = fn.get("parameters", {}) or {}
    props = params.get("properties", {}) or {}
    req = set(params.get("required", []) or [])
    param_strs = []
    for p_name, p_info in props.items():
        p_type = p_info.get("type", "any") if isinstance(p_info, dict) else "any"
        opt = "" if p_name in req else " = None"
        param_strs.append(f"{p_name}: {p_type}{opt}")
    sig = f"{name}({', '.join(param_strs)})"
    return sig, desc

active_container_name = None

def cleanup_container():
    """Guarantees the container is killed when the python script exits."""
    if active_container_name:
        if config.VERBOSITY_MODE != "silent":
            print(f"\n{COLOR_CYAN}[SYSTEM] Tearing down container {active_container_name}...{COLOR_RESET}")
        subprocess.run(
            ["podman", "rm", "-f", "--ignore", active_container_name],
            check=False,
            stderr=subprocess.DEVNULL, 
            stdout=subprocess.DEVNULL
        )

# Register the cleanup function to run when the script dies
atexit.register(cleanup_container)

async def run_chat():
    state_dir = os.path.join(SESSION_DIR, "state")
    totals = config.get_token_totals(state_dir)
    grand = totals.get("_grand_total", {"total": 0, "prompt": 0, "completion": 0, "thinking": 0})
    tool_totals = config.get_tool_totals(state_dir)
    total_tool_calls = tool_totals.get("total_calls", 0)
    by_tool = tool_totals.get("by_tool", {})
    tool_breakdown_str = ""
    if total_tool_calls > 0 and by_tool:
        top_tools = [f"{t}: {c}" for t, c in sorted(by_tool.items(), key=lambda x: -x[1])[:5]]
        tool_breakdown_str = f" ({', '.join(top_tools)})"
    banner = (
        f"Session: [{active_session}]\n"
        f"Brain:      {config.LLM_PROFILES[config.ACTIVE_BRAIN_PROFILE]['name']}\n"
        f"Coder:      {config.LLM_PROFILES[config.ACTIVE_CODER_PROFILE]['name']}\n"
        f"Summarizer: {config.LLM_PROFILES[config.ACTIVE_SUMMARIZER_PROFILE]['name']}\n"
        f"Adviser:    {config.LLM_PROFILES[config.ACTIVE_ADVISER_PROFILE]['name']}\n"
        f"Analyst:    {config.LLM_PROFILES[config.ACTIVE_ANALYST_PROFILE]['name']}\n"
        f"Architect:  {config.LLM_PROFILES[config.ACTIVE_ARCHITECT_PROFILE]['name']}\n"
        f"Decider:    {config.DECIDER_CONFIG['name']}\n"
        f"Log saved to: {LOG_FILE}\n"
        f"Accumulated Session Tokens: {grand['total']:,} ({grand['prompt']:,} in, {grand['completion']:,} out, {grand['thinking']:,} thinking) | Tool Calls: {total_tool_calls}{tool_breakdown_str}"
    )
    log_event("SYSTEM", banner)
    
    prompt_session = None
    quit_app = False
    last_actual_prompt_tokens = 0
    last_estimated_payload_tokens = 0
    token_calibration_ratio = 1.0 # Calibrated ratio: serving model tokenizer vs cl100k_base
    last_history_len = 0
    tool_schemas_overhead = 0 # Estimate of token cost for registered tool schemas
    current_token_estimate = 0
    
    help_text = "Commands: '/exit' or '/quit' to quit | UI: '/text', '/markdown' | Verbosity: '/silent', '/minimal', '/standard', '/detailed' | MCP: '/mcp' | Skills: '/skills' | Jobs: '/jobs'"
    
    if is_resuming:
        log_event("SYSTEM", f"Successfully restored '{active_session}'. Your custom tools are loaded.\n{help_text}")
    else:
        log_event("SYSTEM", f"Started new workspace: '{active_session}'.\n{help_text}")

    # Load initial host skills if requested via CLI (--skills or --all-skills)
    if getattr(cli_args, "all_skills", False):
        install_host_skills("all", verbose=True)
    elif getattr(cli_args, "skills", None):
        _, unmatched_cli_skills = install_host_skills(cli_args.skills, verbose=True)
        if unmatched_cli_skills and config.VERBOSITY_MODE != "silent":
            print(f"{COLOR_YELLOW}[SKILLS WARNING: Could not find host skill/pack for: {', '.join(unmatched_cli_skills)}]{COLOR_RESET}")
        
    skills_menu = build_skills_menu()
    system_prompt = f"{config.SYSTEM_PROMPTS['brain']}\n\n{skills_menu}"
    
    # THE SELF-HEALING CONNECTION LOOP
    while not quit_app:
        global active_container_name
        active_container_name = f"sandbox_{active_session}_{os.getpid()}"
        # Clear stale Podman WSL state before every single boot/reboot ---
        if config.VERBOSITY_MODE != "silent":
            print(f"{COLOR_CYAN}[SYSTEM] Sweeping stale Podman state...{COLOR_RESET}")

        subprocess.run(
            ["podman", "rm", "-f", "--ignore", active_container_name],
            check=False,
            stderr=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL
        )

        try:
            # --- 1. DYNAMICALLY BUILD THE TARGET COMMAND ---
            # Start with the base command
            sandbox_tools_cmd = "pixi run --locked --manifest-path /app/pixi.toml -q python /app/sandbox_tools.py"
            
            if cli_args.coder is not None: sandbox_tools_cmd += f" --coder {cli_args.coder}"
            if cli_args.summarizer is not None: sandbox_tools_cmd += f" --summarizer {cli_args.summarizer}"
            if cli_args.adviser is not None: sandbox_tools_cmd += f" --adviser {cli_args.adviser}"
            if cli_args.analyst is not None: sandbox_tools_cmd += f" --analyst {cli_args.analyst}"
            if cli_args.architect is not None: sandbox_tools_cmd += f" --architect {cli_args.architect}"
                    
            # Check if an init binary (catatonit / tini) is present on the host
            has_init = (
                shutil.which("catatonit") is not None
                or shutil.which("tini") is not None
                or any(os.path.exists(p) for p in [
                    "/usr/bin/catatonit", "/usr/libexec/podman/catatonit",
                    "/usr/lib/podman/catatonit", "/usr/local/bin/catatonit"
                ])
            )
            podman_args = [
                "--log-level=error",
                "run", "-i", "--rm",
            ]
            if has_init:
                podman_args.append("--init")
            elif config.VERBOSITY_MODE != "silent":
                sys.stderr.write("\033[93m[SYSTEM NOTICE: 'catatonit' init binary not found on host; running container without --init. Install with: sudo apt install -y catatonit]\033[0m\n")

            # Check if NVIDIA GPU CDI specification exists on host for GPU passthrough
            has_gpu_cdi = any(
                os.path.exists(p) for p in [
                    "/etc/cdi/nvidia.yaml",
                    "/var/run/cdi/nvidia.yaml",
                    os.path.expanduser("~/.config/cdi/nvidia.yaml")
                ]
            )
            if has_gpu_cdi:
                podman_args.append("--device=nvidia.com/gpu=all")
            elif config.VERBOSITY_MODE != "silent":
                sys.stderr.write("\033[94m[SYSTEM INFO: No NVIDIA CDI spec found; running Podman sandbox in CPU-only mode]\033[0m\n")

            podman_args.extend([
                f"--name={active_container_name}", # True unique identifier
                    "--hostname=sandbox",     # Mask host/session identity inside /etc/hosts and hostname
                    "--dns-search=.",         # Prevent host organization DNS search domains from leaking into /etc/resolv.conf
                    "--network=slirp4netns:allow_host_loopback=false", # prevents talking to the host machine through things like rest calls!
#                    "--add-host=host.containers.internal:10.0.2.2", # this version is needed for direct communication with Win11 host server
                    "--add-host=host.containers.internal:host-gateway",
                    # Core Security Protections
                    "--security-opt", "no-new-privileges=true",
                    # This safely hides vulnerable kernel symbols without crashing Podman!
                    "--security-opt", "mask=/proc/kallsyms",
                    "--security-opt", "mask=/proc/modules",
                    "--cap-drop=ALL",         # Drop all Linux capabilities
                    "--cpus=4.0",            # Limit to 4 CPU cores
                    "--memory=16g",           # Limit to 16 GB of RAM
                    "--shm-size=2g",          # Provides ample shared memory for Chromium & heavy compilers
                    "--pids-limit=1000",      # Neutralizes bash fork bombs
                    "--userns=keep-id:uid=1000,gid=1000",
                    "--tz=local",             # Synchronizes container logs and file timestamps with host clock
#                    "--storage-opt", "size=10G", # Limits the container's scratch space, does not work on WSL2
                    "--env", "PYTHONSAFEPATH=1",
                    "--env", "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/app/.pixi/envs/default/bin:/home/agent/.cargo/bin",
                    # MANDATORY HARDENING: Blinds the container to unused high-risk SUID and namespace-manipulation paths entirely
                    "-v", "/dev/null:/usr/bin/su:ro",
                    "-v", "/dev/null:/usr/bin/mount:ro",
                    "-v", "/dev/null:/usr/bin/umount:ro",
                    "-v", "/dev/null:/usr/bin/unshare:ro",
                    "-v", "/dev/null:/usr/bin/nsenter:ro",
                    "-v", "/dev/null:/usr/bin/passwd:ro",
                    "-v", "/dev/null:/usr/bin/gpasswd:ro",
                    
                    "-v", f"{SESSION_DIR}:/app/workspace:Z,nosuid,nodev",
                    "-v", f"{os.path.abspath('./config.py')}:/app/config.py:ro,Z",
                    "-v", f"{os.path.abspath('./llm_config.py')}:/app/llm_config.py:ro,Z",
                    "-v", f"{os.path.abspath('./mcp_config.py')}:/app/mcp_config.py:ro,Z",
                    "-v", f"{os.path.abspath('./sandbox_tools.py')}:/app/sandbox_tools.py:ro,Z",
                    "-v", f"{os.path.abspath('./smrtk.py')}:/app/smrtk.py:ro,Z", # it can read own code
                    "-v", f"{os.path.abspath('./chimerax_mcp_server.py')}:/app/chimerax_mcp_server.py:ro,Z",
                    "-v", f"{os.path.abspath('./mock_mcp_server.py')}:/app/mock_mcp_server.py:ro,Z",
                    "-v", f"{os.path.abspath('./readme.md')}:/app/readme.md:ro,Z",
                    "-v", f"{os.path.abspath('./Containerfile')}:/app/Containerfile:ro,Z",
                    "-v", f"{os.path.abspath('./setup_wsl2.sh')}:/app/setup_wsl2.sh:ro,Z",
                    "-v", f"{config.HOST_INPUT_DIR}:/app/host_input:ro,Z,nosuid,nodev", # same for all sessions, read only
                    
                    "sandbox-image",
                    "bash", "-c", f"""
                    # 1. Initialize safe agent Git credentials quietly
                    git config --global user.name "Sandbox-Agent"
                    git config --global user.email "agent@sandbox.local"

                    # FIX 1: Explicitly tell Git never to print diagnostic hints to stdout
                    git config --global advice.detachedHead false
                    git config --global advice.initBranch false

                    git config --global init.defaultBranch main

                    # 2. Ensure all isolated workspace subfolders exist
                    mkdir -p /app/workspace/logs /app/workspace/tools /app/workspace/histories /app/workspace/histories/tool_outputs /app/workspace/memories /app/workspace/state /app/workspace/state/rest_calls /app/workspace/state/reports /app/workspace/state/failures /app/workspace/state/jobs /app/workspace/sandbox /app/workspace/outputs /app/workspace/archive /app/workspace/skills /app/workspace/custom_packages

                    # 3. Setup or detect an active Git repository in the workspace
                    if [ ! -d "/app/workspace/.git" ]; then
                        cd /app/workspace
                        
                        # FIX 2: Mute git init output by redirecting it entirely to /dev/null
                        git init > /dev/null
                        
                        # Create a base ignore pattern so the agent doesn't track massive logs, databases, or compiled binaries
                        echo -e "logs/\nstate/\nhistories/\nsandbox/\ncustom_packages/\n__pycache__/\n*.pyc\n*.bin\n*.cpp_bin\n*.o\ntarget/\n*.db\n*.tmp\n*.bak" > .gitignore
                        
                        # FIX 3: Mute the initial commit tracking messages
                        git add .gitignore > /dev/null 2>&1
                        git commit -m "chore: initial sandbox tracking initialization" > /dev/null 2>&1
                    fi
                    
                    # 4. APPLICATION PROTECTION: Lock down standard library, user site-packages, and /app root
                    mkdir -p /home/agent/.local/lib/python3.14/site-packages
                    chmod -R a-w /home/agent/.local/ 2>/dev/null
                    chmod -R a-w /app/__pycache__ 2>/dev/null
                    chmod a-w /app 2>/dev/null
                    
                    # 5. HARDENING: Freeze the Pixi environment directories and binary path to block tampering and PATH hijacking
                    chmod -R a-w /app/.pixi/envs/default/bin/ 2>/dev/null
                    chmod a-w /app/.pixi /app/.pixi/envs /app/.pixi/envs/default /app/.pixi/envs/default/lib/python*/site-packages 2>/dev/null
                    
                    # 6. CORRECT ENVIRONMENT ALIGNMENT: Prepend framework paths while appending custom tool dirs to PATH
                    export PYTHONPATH=/app:/app/workspace/custom_packages:$PYTHONPATH
                    export PATH=$PATH:/app/workspace/tools:/app/workspace/tools/bin
                    
                    # 7. Start the MCP server safely
                    cd /app/workspace
                    {sandbox_tools_cmd}
                    """
            ])

            server_params = StdioServerParameters(
                command="podman",
                args=podman_args
            )
            
            async with stdio_client(server_params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    
                    mcp_tools = await session.list_tools()
                    sandbox_openai_tools = [
                        {
                            "type": "function", 
                            "function": {
                                "name": t.name, 
                                "description": t.description, 
                                "parameters": t.inputSchema or {"type": "object", "properties": {}}
                            }
                        } 
                        for t in mcp_tools.tools
                    ]

                    # --- EXTERNAL & HOST MCP REGISTRY STATE ---
                    active_ext_servers = {}
                    ext_tool_router = {}

                    async def mcp_worker(server_id, cfg, cmd_queue, ready_queue):
                        stack = AsyncExitStack()
                        try:
                            if cfg.get("transport") == "sse":
                                sse_ctx = sse_client(cfg["url"], timeout=5.0)
                                read, write = await stack.enter_async_context(sse_ctx)
                            elif cfg.get("transport") == "stdio":
                                params = StdioServerParameters(command=cfg["command"], args=cfg.get("args", []), env=cfg.get("env"))
                                read, write = await stack.enter_async_context(stdio_client(params))
                            else:
                                ready_queue.put_nowait({"error": "Unknown transport protocol"})
                                return

                            session = await stack.enter_async_context(ClientSession(read, write))
                            await session.initialize()
                            tools_res = await session.list_tools()
                            ready_queue.put_nowait({"tools": tools_res.tools})

                            while True:
                                cmd = await cmd_queue.get()
                                if cmd is None or cmd.get("action") == "close":
                                    break
                                if cmd.get("action") == "call_tool":
                                    try:
                                        res = await session.call_tool(cmd["name"], cmd["args"])
                                        if not cmd["future"].done():
                                            cmd["future"].set_result(res)
                                    except Exception as call_err:
                                        if not cmd["future"].done():
                                            cmd["future"].set_exception(call_err)
                        except Exception as e:
                            if ready_queue.empty():
                                ready_queue.put_nowait({"error": str(e)})
                        finally:
                            while not cmd_queue.empty():
                                try:
                                    pending_cmd = cmd_queue.get_nowait()
                                    if isinstance(pending_cmd, dict) and "future" in pending_cmd and not pending_cmd["future"].done():
                                        pending_cmd["future"].set_exception(RuntimeError(f"MCP server '{server_id}' disconnected unexpectedly."))
                                except Exception:
                                    break
                            try:
                                await stack.aclose()
                            except Exception:
                                pass

                    async def call_external_tool(target, orig_name, args):
                        srv_id = target.get("server_id", "")
                        srv_info = active_ext_servers.get(srv_id)
                        if srv_info and srv_info.get("task") and srv_info["task"].done():
                            raise RuntimeError(f"MCP server '{srv_id}' worker task has terminated unexpectedly.")
                        loop = asyncio.get_running_loop()
                        fut = loop.create_future()
                        await target["cmd_queue"].put({"action": "call_tool", "name": orig_name, "args": args, "future": fut})
                        return await asyncio.wait_for(fut, timeout=60.0)

                    async def connect_external_mcp(server_id, cfg, verbose=True):
                        cmd_queue = asyncio.Queue()
                        ready_queue = asyncio.Queue()
                        task = asyncio.create_task(mcp_worker(server_id, cfg, cmd_queue, ready_queue))

                        try:
                            ready_res = await asyncio.wait_for(ready_queue.get(), timeout=6.0)
                        except Exception as e:
                            ready_res = {"error": f"Connection timed out: {e}"}

                        if "error" in ready_res:
                            try:
                                await cmd_queue.put({"action": "close"})
                                await asyncio.wait_for(task, timeout=1.0)
                            except Exception:
                                pass
                            if cfg.get("optional", True):
                                if verbose and config.VERBOSITY_MODE != "silent":
                                    print(f"\n{COLOR_YELLOW}[MCP NOTICE: External server '{server_id}' ({cfg.get('url', cfg.get('command', ''))}) is offline/unreachable: {ready_res['error']}. Skipping.]{COLOR_RESET}")
                                log_event("MCP REGISTRY", f"External MCP '{server_id}' unavailable (skipped): {ready_res['error']}", hide_console=True)
                                return None
                            else:
                                raise RuntimeError(ready_res["error"])

                        prefix = cfg.get("tool_prefix", "")
                        registered = []
                        for t in ready_res["tools"]:
                            q_name = f"{prefix}{t.name}" if prefix else t.name
                            ext_tool_router[q_name] = {
                                "cmd_queue": cmd_queue,
                                "original_name": t.name,
                                "server_id": server_id,
                                "require_confirmation": cfg.get("require_confirmation", False),
                            }
                            registered.append({
                                "type": "function",
                                "function": {
                                    "name": q_name,
                                    "description": f"[{cfg.get('name', server_id)}] {t.description or ''}",
                                    "parameters": t.inputSchema or {"type": "object", "properties": {}},
                                }
                            })
                        active_ext_servers[server_id] = {
                            "task": task,
                            "cmd_queue": cmd_queue,
                            "cfg": cfg,
                            "tools": registered,
                        }

                        # Format registered tool schemas for transparency in log and console
                        schema_lines = []
                        for t in registered:
                            sig, desc = format_tool_signature(t)
                            schema_lines.append(f"  • {sig}" + (f"\n    {desc}" if desc else ""))
                        schema_text = "\n".join(schema_lines)

                        srv_display_name = cfg.get('name', server_id)
                        tool_names = ", ".join(t["function"]["name"] for t in registered)
                        log_msg = (
                            f"Connected external MCP '{server_id}' ({srv_display_name})\n"
                            f"Transport: {cfg.get('transport', 'sse')} | Target: {cfg.get('url', cfg.get('command', ''))}\n"
                            f"Registered Tools ({len(registered)}):\n{schema_text}"
                        )
                        log_event("MCP REGISTRY", log_msg, hide_console=True)

                        if verbose and config.VERBOSITY_MODE != "silent":
                            print(f"\n{COLOR_BRIGHT_GREEN}[MCP SYSTEM: Connected to '{srv_display_name}' ({len(registered)} tools: {tool_names})]{COLOR_RESET}")
                            if config.VERBOSITY_MODE in ["standard", "detailed"]:
                                print(f"{COLOR_CYAN}[Registered External Tool Schemas]:\n{schema_text}{COLOR_RESET}")
                        return registered

                    async def disconnect_external_mcp(server_id, verbose=True):
                        if server_id in active_ext_servers:
                            entry = active_ext_servers.pop(server_id)
                            removed_tools = []
                            for q_name in list(ext_tool_router.keys()):
                                if ext_tool_router[q_name]["server_id"] == server_id:
                                    removed_tools.append(q_name)
                                    del ext_tool_router[q_name]
                            try:
                                await entry["cmd_queue"].put({"action": "close"})
                                await asyncio.wait_for(entry["task"], timeout=3.0)
                            except Exception:
                                pass
                            srv_name = entry["cfg"].get("name", server_id)
                            tool_list_str = ", ".join(removed_tools) if removed_tools else "none"
                            log_msg = f"Disconnected external MCP '{server_id}' ({srv_name}). Removed {len(removed_tools)} tools: {tool_list_str}"
                            log_event("MCP REGISTRY", log_msg, hide_console=True)
                            if verbose and config.VERBOSITY_MODE != "silent":
                                print(f"\n{COLOR_YELLOW}[MCP SYSTEM: Disconnected external server '{srv_name}' ({server_id}). Removed tools: {tool_list_str}]{COLOR_RESET}")

                    MANAGE_SKILLS_TOOL = {
                        "type": "function",
                        "function": {
                            "name": "manage_skills",
                            "description": "Meta-tool to inspect, mount (copy into /app/workspace/skills), unmount, or save skill packs between the host skills library (./skills) and the current session.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "action": {
                                        "type": "string",
                                        "enum": ["list", "mount", "unmount", "save"],
                                        "description": "'list' to view host skill packs and descriptions; 'mount' to copy a pack/category/skill into the session; 'unmount' to remove a mounted pack/skill from the session; 'save' to export a session skill back to the host library."
                                    },
                                    "target": {
                                        "type": "string",
                                        "description": "Skill pack, category path, individual skill name, comma-separated list, or 'all' (e.g. 'Anthropic/development', 'development', 'mcp-builder', or 'all'). Optional for 'list'."
                                    },
                                    "pack": {
                                        "type": "string",
                                        "description": "Destination pack subpath under ./skills when action='save' (e.g. 'science/genomics' or 'custom')."
                                    }
                                },
                                "required": ["action"]
                            }
                        }
                    }

                    async def handle_manage_skills(m_args):
                        action = (m_args.get("action") or "").lower().strip()
                        target = (m_args.get("target") or "").strip()
                        pack_dest = (m_args.get("pack") or "").strip()

                        if action in ("list", "status"):
                            host_skills = discover_host_skills()
                            skills_dir = os.path.join(SESSION_DIR, "skills")
                            mounted_set = set()
                            if os.path.exists(skills_dir):
                                for item in os.listdir(skills_dir):
                                    if os.path.exists(os.path.join(skills_dir, item, "SKILL.md")):
                                        mounted_set.add(item)

                            q = target.strip("/").lower()
                            lines = ["=== HOST SKILLS LIBRARY ==="]
                            packs = {}
                            for s in host_skills:
                                if q and q != "all":
                                    rel_l = s["rel_path"].lower()
                                    pack_l = s["pack"].lower()
                                    if not (f"/{q}/" in f"/{rel_l}/" or (pack_l and f"/{q}/" in f"/{pack_l}/") or s["name"].lower() == q):
                                        continue
                                packs.setdefault(s["pack"], []).append(s)

                            if not packs:
                                lines.append(f"  (No matching host skills found{f' for {target!r}' if target else ''})")
                            else:
                                for pack_name in sorted(packs.keys()):
                                    lines.append(f"\nPack: {pack_name}")
                                    for s in packs[pack_name]:
                                        st = "[MOUNTED]" if s["name"] in mounted_set else "[UNMOUNTED]"
                                        lines.append(f"  - {s['name']} {st}: {s['description']}")

                            host_names = {s["name"] for s in host_skills}
                            session_only = sorted(mounted_set - host_names)
                            if session_only and not q:
                                lines.append("\nSession-Only Skills (in /app/workspace/skills):")
                                for s_name in session_only:
                                    lines.append(f"  - {s_name} [MOUNTED]")

                            lines.append("\nUsage: Call manage_skills(action='mount', target='<pack_or_skill>') to copy into /app/workspace/skills, then load_skill('<skill_name>') to read full instructions.")
                            return "\n".join(lines)

                        elif action in ("mount", "connect", "on", "enable", "load"):
                            if not target:
                                return "SYSTEM ERROR: Provide 'target' (pack name, category, skill name, or 'all') to mount."
                            installed, unmatched = install_host_skills(target, verbose=True)
                            if not installed:
                                return f"SYSTEM ERROR: No host skill or pack matched '{target}'. Call manage_skills(action='list') to view available packs."
                            desc_lines = [f"  - {s['name']} ({s['pack']}): {s['description']}" for s in installed]
                            unmatched_note = f"\n(Note: unmatched targets ignored: {', '.join(unmatched)})" if unmatched else ""
                            return (
                                f"SUCCESS: Mounted {len(installed)} skill(s) into /app/workspace/skills:\n"
                                + "\n".join(desc_lines)
                                + unmatched_note
                                + "\n\nCall load_skill(skill_name) to read full instructions and bundled resource paths."
                            )

                        elif action in ("unmount", "disconnect", "off", "disable", "unload"):
                            if not target:
                                return "SYSTEM ERROR: Provide 'target' (pack name, skill name, or 'all') to unmount."
                            removed, unmatched = uninstall_session_skills(target, verbose=True)
                            if not removed:
                                return f"NOTICE: No currently mounted session skill matched '{target}'."
                            return f"SUCCESS: Unmounted {len(removed)} skill(s) from /app/workspace/skills: {', '.join(removed)}."

                        elif action in ("save", "export"):
                            if not target or not pack_dest:
                                return "SYSTEM ERROR: Provide both 'target' (skill_name in /app/workspace/skills) and 'pack' (destination subpath under ./skills, e.g. 'science/genomics')."
                            ok, res_msg = save_session_skill_to_host(target, pack_dest)
                            if ok:
                                return f"SUCCESS: Saved session skill '{target}' permanently to host library at './skills/{res_msg}'."
                            return f"SYSTEM ERROR: {res_msg}"

                        else:
                            return f"SYSTEM ERROR: Unknown action '{action}'. Valid actions are 'list', 'mount', 'unmount', 'save'."

                    MANAGE_EXTERNAL_MCP_TOOL = {
                        "type": "function",
                        "function": {
                            "name": "manage_external_mcp",
                            "description": "Meta-tool to inspect, connect, or disconnect Model Context Protocol (MCP) tool suites (SSE or stdio). When connected, its tools are registered directly into your toolset until disconnected.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "action": {
                                        "type": "string",
                                        "enum": ["list", "connect", "disconnect"],
                                        "description": "'list' to view configured and active servers; 'connect' to attach a server; 'disconnect' to detach a server."
                                    },
                                    "server_name": {
                                        "type": "string",
                                        "description": "Identifier for the server. Can be a known name from mcp_config (e.g. 'chimerax') or a new custom name for a universal MCP."
                                    },
                                    "address": {
                                        "type": "string",
                                        "description": "For universal custom SSE MCPs: the URL (e.g. 'http://host:9000/sse'). 'host' or '{HOST_IP}' is automatically resolved to the host IP. Optional if server_name is already in mcp_config."
                                    },
                                    "command": {
                                        "type": "string",
                                        "description": "For universal custom stdio MCPs: command to execute (e.g. 'npx', 'python', 'uvx')."
                                    },
                                    "args": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "For universal custom stdio MCPs: list of command arguments (e.g. ['-y', '@modelcontextprotocol/server-memory'])."
                                    },
                                    "prefix": {
                                        "type": "string",
                                        "description": "Tool prefix to namespace the imported tools and avoid collisions (e.g. 'cx_', 'custom_'). Defaults to '{server_name}_'."
                                    },
                                    "transport": {
                                        "type": "string",
                                        "enum": ["sse", "stdio"],
                                        "description": "Transport protocol for universal custom MCPs. Defaults to 'sse' (or 'stdio' if command is provided)."
                                    }
                                },
                                "required": ["action"]
                            }
                        }
                    }

                    def save_active_mcp_state():
                        state_dir = os.path.join(SESSION_DIR, "state")
                        os.makedirs(state_dir, exist_ok=True)
                        state_file = os.path.join(state_dir, "active_mcp_servers.json")
                        saved_data = {}
                        for s_id, s_data in active_ext_servers.items():
                            saved_data[s_id] = s_data["cfg"]
                        try:
                            with open(state_file, "w", encoding="utf-8") as f:
                                json.dump(saved_data, f, indent=2)
                        except Exception:
                            pass

                    def load_saved_mcp_state():
                        state_file = os.path.join(SESSION_DIR, "state", "active_mcp_servers.json")
                        if os.path.exists(state_file):
                            try:
                                with open(state_file, "r", encoding="utf-8") as f:
                                    return json.load(f)
                            except Exception:
                                return {}
                        return {}

                    def rebuild_openai_tools():
                        combined = [MANAGE_EXTERNAL_MCP_TOOL, MANAGE_SKILLS_TOOL] + list(sandbox_openai_tools)
                        for s_data in active_ext_servers.values():
                            combined.extend(s_data["tools"])
                        return combined

                    async def handle_manage_external_mcp(m_args):
                        nonlocal openai_tools, tool_schemas_overhead
                        action = (m_args.get("action") or "").lower().strip()
                        server_name = (m_args.get("server_name") or "").lower().strip()

                        if action == "list":
                            lines = ["=== MCP SERVER REGISTRY ==="]
                            lines.append("Pre-Configured Servers (from mcp_config):")
                            for s_id, s_cfg in mcp_config.EXTERNAL_MCP_SERVERS.items():
                                status = "[CONNECTED]" if s_id in active_ext_servers else "[DISCONNECTED]"
                                lines.append(f"  - {s_id}: {s_cfg.get('name', s_id)} ({s_cfg.get('url', s_cfg.get('command', ''))}) {status}")
                            lines.append("\nActive Dynamic Servers:")
                            dynamic_found = False
                            for s_id, s_data in active_ext_servers.items():
                                if s_id not in mcp_config.EXTERNAL_MCP_SERVERS:
                                    dynamic_found = True
                                    lines.append(f"  - {s_id}: {s_data['cfg'].get('url', s_data['cfg'].get('command', ''))} [CONNECTED - {len(s_data['tools'])} tools]")
                            if not dynamic_found:
                                lines.append("  (none)")
                            lines.append("\nUsage: Call manage_external_mcp with action='connect' and server_name (and optional address/prefix for universal custom servers).")
                            return "\n".join(lines)

                        elif action == "connect":
                            if not server_name:
                                return "SYSTEM ERROR: You must provide 'server_name' to connect an MCP server."

                            if server_name in active_ext_servers:
                                return f"NOTICE: Server '{server_name}' is already connected."

                            if server_name in mcp_config.EXTERNAL_MCP_SERVERS:
                                cfg = copy.deepcopy(mcp_config.EXTERNAL_MCP_SERVERS[server_name])
                                if m_args.get("address"):
                                    raw_addr = m_args["address"]
                                    for h_alias in ["{HOST_IP}", "{host}", "{HOST}", "host.wsl.internal", "host.containers.internal", "host.docker.internal", "host"]:
                                        raw_addr = raw_addr.replace(h_alias, mcp_config.HOST_IP)
                                    cfg["url"] = raw_addr
                                if m_args.get("prefix"):
                                    cfg["tool_prefix"] = m_args["prefix"]
                            else:
                                # Universal Custom MCP Server
                                address = m_args.get("address")
                                command = m_args.get("command")
                                transport = (m_args.get("transport") or "").lower().strip()
                                if command and not transport:
                                    transport = "stdio"
                                elif not transport:
                                    transport = "sse"

                                prefix = m_args.get("prefix") or f"{server_name}_"

                                if transport == "stdio":
                                    cmd = command or address
                                    if not cmd:
                                        return f"SYSTEM ERROR: Unknown server '{server_name}'. For stdio MCPs, provide 'command' (e.g. 'npx') and optional 'args' (e.g. ['-y', '@modelcontextprotocol/server-memory'])."
                                    args_list = m_args.get("args") or []
                                    if isinstance(args_list, str):
                                        args_list = args_list.split()
                                    cfg = {
                                        "name": server_name,
                                        "description": f"Universal stdio MCP server ({server_name}): {cmd}",
                                        "transport": "stdio",
                                        "command": cmd,
                                        "args": args_list,
                                        "tool_prefix": prefix,
                                        "optional": True,
                                        "require_confirmation": True,
                                    }
                                else:
                                    if not address:
                                        return f"SYSTEM ERROR: Unknown server '{server_name}' in mcp_config. To connect a universal custom SSE MCP, provide 'address' (e.g. 'http://host:9000/sse')."
                                    raw_addr = address
                                    for h_alias in ["{HOST_IP}", "{host}", "{HOST}", "host.wsl.internal", "host.containers.internal", "host.docker.internal", "host"]:
                                        raw_addr = raw_addr.replace(h_alias, mcp_config.HOST_IP)
                                    cfg = {
                                        "name": server_name,
                                        "description": f"Universal MCP server ({server_name}) at {raw_addr}",
                                        "transport": "sse",
                                        "url": raw_addr,
                                        "tool_prefix": prefix,
                                        "optional": True,
                                        "require_confirmation": True,
                                    }

                            # Security confirmation check
                            if cfg.get("require_confirmation", False):
                                if cli_args.exit:
                                    deny_msg = f"SECURITY ALERT: Connecting to external MCP '{server_name}' ({cfg.get('url', cfg.get('command', ''))}) requires human confirmation, but running in unattended batch mode (-x). Connection DENIED."
                                    print(f"\n{COLOR_RED}[SECURITY GATE] {deny_msg}{COLOR_RESET}")
                                    log_event("SYSTEM", deny_msg)
                                    return f"SYSTEM ERROR: {deny_msg}"
                                else:
                                    target_loc = cfg.get('url', cfg.get('command', ''))
                                    print(f"\n{COLOR_RED}[SECURITY GATE] The Brain requests to connect to external MCP server '{server_name}' at '{target_loc}'.{COLOR_RESET}")
                                    try:
                                        confirm = await asyncio.to_thread(input, f"{COLOR_CYAN}Allow connection? [y/N]: {COLOR_RESET}")
                                    except (EOFError, KeyboardInterrupt):
                                        confirm = "n"
                                    if confirm.strip().lower() not in ["y", "yes"]:
                                        msg = f"SYSTEM NOTICE: Connection to external MCP '{server_name}' was DENIED by the user for security reasons."
                                        print(f"{COLOR_YELLOW}{msg}{COLOR_RESET}")
                                        log_event("SYSTEM", msg)
                                        return msg

                            tools_added = await connect_external_mcp(server_name, cfg, verbose=True)
                            if tools_added:
                                openai_tools = rebuild_openai_tools()
                                tool_schemas_overhead = len(tokenizer.encode(json.dumps(openai_tools)))
                                save_active_mcp_state()
                                tool_names = [t["function"]["name"] for t in tools_added]
                                return f"SUCCESS: Connected to '{cfg.get('name', server_name)}'. Registered {len(tool_names)} tools: {', '.join(tool_names)}. These tools are now permanently available in your toolset for all subsequent turns in this session until you disconnect them."
                            else:
                                err_hint = ""
                                target_loc = str(cfg.get('url', cfg.get('command', '')))
                                if "127.0.0.1" in target_loc or "localhost" in target_loc:
                                    err_hint = " Note: The Overseer runs on the host and looks for servers on the host network. If you are building new tools inside the container, do NOT use in-container MCP servers; use 'create_tool' to build and register tools natively instead."
                                elif cfg.get("transport") == "stdio":
                                    err_hint = " Note: stdio commands execute on the host machine. Ensure the command binary is installed and executable on the host."
                                return f"SYSTEM ERROR: Could not connect to '{server_name}' at {target_loc}. The server appears to be offline or unreachable.{err_hint}"

                        elif action == "disconnect":
                            if not server_name:
                                return "SYSTEM ERROR: You must provide 'server_name' to disconnect an MCP server."
                            if server_name not in active_ext_servers:
                                return f"NOTICE: Server '{server_name}' is not currently connected."
                            await disconnect_external_mcp(server_name, verbose=True)
                            openai_tools = rebuild_openai_tools()
                            tool_schemas_overhead = len(tokenizer.encode(json.dumps(openai_tools)))
                            save_active_mcp_state()
                            return f"SUCCESS: Disconnected from '{server_name}'. Its tools have been removed from your toolset."

                        else:
                            return f"SYSTEM ERROR: Unknown action '{action}'. Valid actions are 'list', 'connect', 'disconnect'."

                    # Reconnect saved active servers from session state
                    saved_servers = load_saved_mcp_state()
                    for s_id, s_cfg in saved_servers.items():
                        if s_id not in active_ext_servers:
                            await connect_external_mcp(s_id, s_cfg, verbose=True)

                    # Connect initial external MCP servers enabled via CLI or config
                    cli_mcp_targets = set()
                    if getattr(cli_args, "all_mcp", False):
                        cli_mcp_targets = set(mcp_config.EXTERNAL_MCP_SERVERS.keys())
                    elif getattr(cli_args, "mcp", None):
                        cli_mcp_targets = {s.strip().lower() for s in cli_args.mcp.split(",") if s.strip()}

                    for s_id, s_cfg in mcp_config.EXTERNAL_MCP_SERVERS.items():
                        if s_id not in active_ext_servers and (s_id in cli_mcp_targets or (not getattr(cli_args, "mcp", None) and s_cfg.get("enabled", False))):
                            await connect_external_mcp(s_id, s_cfg, verbose=True)

                    save_active_mcp_state()
                    openai_tools = rebuild_openai_tools()
                    
                    # Calculate token overhead of the registered tool schemas
                    tool_schemas_overhead = len(tokenizer.encode(json.dumps(openai_tools)))
                    
                    # Initialize tracker outside the loop
                    cli_prompt_consumed = False
                    tool_failure_streaks = {}
                    tool_last_failed_args = {}
                    
                    while True:
                        try:
                            # 1. Check if we have a CLI or Piped prompt to use first
                            if cli_prompt and not cli_prompt_consumed:
                                user_input = cli_prompt
                                cli_prompt_consumed = True
                            else:
                                # 2. Auto-exit for silent mode after CLI prompt is done
                                if cli_prompt and (config.VERBOSITY_MODE == "silent" or cli_args.exit):
                                    quit_app = True
                                    break
                                    
                                # 3. Normal interactive mode
                                # Only initialize the prompter if we actually need human input!
                                if prompt_session is None:
                                    prompt_kb = KeyBindings()

                                    @prompt_kb.add("escape", "enter")
                                    @prompt_kb.add("escape", "c-m")
                                    @prompt_kb.add("c-o")
                                    def _insert_newline(event):
                                        """Inserts a newline in interactive prompt on Alt+Enter, Esc+Enter, or Ctrl+O."""
                                        event.current_buffer.insert_text("\n")

                                    prompt_session = PromptSession(key_bindings=prompt_kb)

                                prompt_text = ANSI(f"\n{COLOR_RED}YOU: {COLOR_RESET}")
                                user_input = await prompt_session.prompt_async(prompt_text)
                                

                        # Catch Ctrl+C and Ctrl+D gracefully
                        except KeyboardInterrupt:
                            continue
                        except EOFError:
                            quit_app = True
                            break

                        user_input = user_input.strip()
                        
                        # --- 1. EXIT COMMANDS ---
                        if user_input.lower() in ['/quit', '/exit']: 
                            quit_app = True
                            break
                            
                        # --- 2. DYNAMIC VERBOSITY COMMANDS ---
                        if user_input.lower() in ['/silent', '/minimal', '/standard', '/detailed']:
                            new_mode = user_input.lower()[1:] # Slice off the '/'
                            config.VERBOSITY_MODE = new_mode
                            
                            # Give the user immediate visual feedback
                            print(f"\n{COLOR_BRIGHT_GREEN}[SYSTEM: Console verbosity instantly changed to '{new_mode}']{COLOR_RESET}")
                            continue # Critical: Skip the rest of the loop so the LLM doesn't see this command!

                        # --- 3. DYNAMIC FORMAT COMMANDS ---
                        if user_input.lower() in ['/text', '/markdown']:
                            new_format = user_input.lower()[1:] 
                            config.FORMAT_MODE = new_format
                            
                            print(f"\n{COLOR_BRIGHT_GREEN}[SYSTEM: Console format instantly changed to '{new_format}']{COLOR_RESET}")
                            continue

                        # --- 4. DYNAMIC MCP COMMANDS ---
                        if user_input.lower().startswith('/mcp'):
                            parts = user_input.strip().split()
                            if len(parts) == 1 or parts[1].lower() in ['list', 'status', 'help']:
                                print(f"\n{COLOR_CYAN}=== EXTERNAL MCP REGISTRY ==={COLOR_RESET}")
                                for s_id, s_cfg in mcp_config.EXTERNAL_MCP_SERVERS.items():
                                    is_active = s_id in active_ext_servers
                                    status_tag = f"{COLOR_BRIGHT_GREEN}[ACTIVE]{COLOR_RESET}" if is_active else f"{COLOR_DIM}[OFF]{COLOR_RESET}"
                                    confirm_tag = " (requires confirmation)" if s_cfg.get("require_confirmation") else ""
                                    tools_cnt = f" ({len(active_ext_servers[s_id]['tools'])} tools)" if is_active else ""
                                    print(f"  {status_tag} {s_id} - {s_cfg.get('name', s_id)} ({s_cfg.get('url', s_cfg.get('command', ''))}){tools_cnt}{confirm_tag}")
                                for s_id, s_data in active_ext_servers.items():
                                    if s_id not in mcp_config.EXTERNAL_MCP_SERVERS:
                                        tools_cnt = f" ({len(s_data['tools'])} tools)"
                                        url_or_cmd = s_data['cfg'].get('url', s_data['cfg'].get('command', ''))
                                        print(f"  {COLOR_BRIGHT_GREEN}[ACTIVE]{COLOR_RESET} {s_id} - {s_data['cfg'].get('name', s_id)} ({url_or_cmd}){tools_cnt} (dynamic)")
                                print(f"{COLOR_DIM}Commands: /mcp on <name> | /mcp off <name>{COLOR_RESET}\n")
                                continue
                            elif parts[1].lower() in ['on', 'enable']:
                                if len(parts) < 3:
                                    print(f"\n{COLOR_RED}[MCP ERROR: Missing server name. Usage: /mcp on <name> [<url>] (e.g. /mcp on mock)]{COLOR_RESET}")
                                    continue
                                target_name = parts[2].lower()
                                if target_name in active_ext_servers:
                                    print(f"\n{COLOR_YELLOW}[MCP NOTICE: '{target_name}' is already active.]{COLOR_RESET}")
                                    continue
                                if target_name in mcp_config.EXTERNAL_MCP_SERVERS:
                                    cfg = copy.deepcopy(mcp_config.EXTERNAL_MCP_SERVERS[target_name])
                                    if len(parts) > 3:
                                        raw_addr = parts[3]
                                        for h_alias in ["host.wsl.internal", "host.containers.internal", "host.docker.internal", "host"]:
                                            raw_addr = raw_addr.replace(h_alias, mcp_config.HOST_IP)
                                        cfg["url"] = raw_addr
                                elif len(parts) > 3:
                                    raw_addr = parts[3]
                                    for h_alias in ["host.wsl.internal", "host.containers.internal", "host.docker.internal", "host"]:
                                        raw_addr = raw_addr.replace(h_alias, mcp_config.HOST_IP)
                                    cfg = {
                                        "name": target_name,
                                        "description": f"Universal MCP server at {raw_addr}",
                                        "transport": "sse",
                                        "url": raw_addr,
                                        "tool_prefix": f"{target_name}_",
                                        "optional": True,
                                        "require_confirmation": True,
                                    }
                                else:
                                    print(f"\n{COLOR_RED}[MCP ERROR: Unknown server '{target_name}'. To connect a custom MCP, provide the URL: /mcp on {target_name} http://host:port/sse]{COLOR_RESET}")
                                    continue
                                tools_added = await connect_external_mcp(target_name, cfg, verbose=True)
                                if tools_added:
                                    openai_tools = rebuild_openai_tools()
                                    tool_schemas_overhead = len(tokenizer.encode(json.dumps(openai_tools)))
                                    save_active_mcp_state()
                                    inject_note = f"[SYSTEM NOTICE: External tool suite '{cfg.get('name', target_name)}' is now connected. Available tools: {', '.join(t['function']['name'] for t in tools_added)}]"
                                    messages = load_history()
                                    messages.append({"role": "user", "content": inject_note})
                                    save_history(messages)
                                    log_event("INJECTED CONTEXT TO BRAIN", inject_note)
                                continue
                            elif parts[1].lower() in ['off', 'disable']:
                                if len(parts) < 3:
                                    print(f"\n{COLOR_RED}[MCP ERROR: Missing server name. Usage: /mcp off <name> (e.g. /mcp off mock)]{COLOR_RESET}")
                                    continue
                                target_name = parts[2].lower()
                                if target_name not in active_ext_servers:
                                    print(f"\n{COLOR_YELLOW}[MCP NOTICE: '{target_name}' is not currently active.]{COLOR_RESET}")
                                    continue
                                cfg = active_ext_servers[target_name]["cfg"]
                                await disconnect_external_mcp(target_name, verbose=True)
                                openai_tools = rebuild_openai_tools()
                                tool_schemas_overhead = len(tokenizer.encode(json.dumps(openai_tools)))
                                save_active_mcp_state()
                                inject_note = f"[SYSTEM NOTICE: External tool suite '{cfg.get('name', target_name)}' has been disconnected. Its tools are no longer available.]"
                                messages = load_history()
                                messages.append({"role": "user", "content": inject_note})
                                save_history(messages)
                                log_event("INJECTED CONTEXT TO BRAIN", inject_note)
                                continue
                            else:
                                print(f"\n{COLOR_RED}[MCP ERROR: Unknown command '{user_input}'. Usage: /mcp list | /mcp on <name> [<url>] | /mcp off <name>]{COLOR_RESET}")
                                continue

                        # --- 4b. DYNAMIC SKILLS COMMANDS ---
                        if user_input.lower() == '/skill' or user_input.lower().startswith('/skill ') or user_input.lower() == '/skills' or user_input.lower().startswith('/skills '):
                            parts = user_input.strip().split()
                            if len(parts) == 1 or parts[1].lower() in ['list', 'status', 'help']:
                                host_skills = discover_host_skills()
                                skills_dir = os.path.join(SESSION_DIR, "skills")
                                installed_set = set()
                                if os.path.exists(skills_dir):
                                    for item in os.listdir(skills_dir):
                                        if os.path.exists(os.path.join(skills_dir, item, "SKILL.md")):
                                            installed_set.add(item)

                                print(f"\n{COLOR_CYAN}=== HOST SKILLS LIBRARY ({config.HOST_SKILLS_DIR}) ==={COLOR_RESET}")
                                if not host_skills:
                                    print(f"  {COLOR_DIM}(No skills found in {config.HOST_SKILLS_DIR}){COLOR_RESET}")
                                else:
                                    packs = {}
                                    for s in host_skills:
                                        packs.setdefault(s["pack"], []).append(s)
                                    for pack_name in sorted(packs.keys()):
                                        pack_items = packs[pack_name]
                                        active_cnt = sum(1 for s in pack_items if s["name"] in installed_set)
                                        total_cnt = len(pack_items)
                                        if active_cnt == total_cnt:
                                            p_tag = f"{COLOR_BRIGHT_GREEN}[ACTIVE {active_cnt}/{total_cnt}]{COLOR_RESET}"
                                        elif active_cnt > 0:
                                            p_tag = f"{COLOR_YELLOW}[PARTIAL {active_cnt}/{total_cnt}]{COLOR_RESET}"
                                        else:
                                            p_tag = f"{COLOR_DIM}[OFF 0/{total_cnt}]{COLOR_RESET}"
                                        skill_labels = [
                                            f"{COLOR_BRIGHT_GREEN}{s['name']}{COLOR_RESET}" if s["name"] in installed_set else s["name"]
                                            for s in pack_items
                                        ]
                                        print(f"  {p_tag} {COLOR_CYAN}{pack_name}{COLOR_RESET}: {', '.join(skill_labels)}")

                                host_skill_names = {s["name"] for s in host_skills}
                                custom_session_skills = sorted(installed_set - host_skill_names)
                                if custom_session_skills:
                                    print(f"\n{COLOR_CYAN}=== SESSION-ONLY SKILLS (/app/workspace/skills) ==={COLOR_RESET}")
                                    print(f"  {COLOR_BRIGHT_GREEN}[ACTIVE]{COLOR_RESET} {', '.join(custom_session_skills)}")

                                print(f"{COLOR_DIM}Commands: /skills on <pack|skill|all> | /skills off <pack|skill|all> | /skills save <skill> <pack>{COLOR_RESET}\n")
                                continue

                            elif parts[1].lower() in ['on', 'enable', 'load']:
                                if len(parts) < 3:
                                    print(f"\n{COLOR_RED}[SKILLS ERROR: Missing pack or skill name. Usage: /skills on <pack|skill|all> (e.g. /skills on development or /skills on mcp-builder)]{COLOR_RESET}")
                                    continue
                                target_arg = ",".join(parts[2:])
                                installed, unmatched = install_host_skills(target_arg, verbose=True)
                                if unmatched:
                                    print(f"{COLOR_YELLOW}[SKILLS WARNING: No matching host skill/pack found for: {', '.join(unmatched)}]{COLOR_RESET}")
                                if installed:
                                    loaded_names = [s["name"] for s in installed]
                                    inject_note = (
                                        f"[SYSTEM NOTICE: {len(installed)} skill(s) copied into /app/workspace/skills and added to your AVAILABLE SKILLS MENU: "
                                        f"{', '.join(loaded_names)}. Call load_skill(skill_name) when relevant.]"
                                    )
                                    messages = load_history()
                                    messages.append({"role": "user", "content": inject_note})
                                    save_history(messages)
                                    log_event("INJECTED CONTEXT TO BRAIN", inject_note)
                                continue

                            elif parts[1].lower() in ['off', 'disable', 'unload']:
                                if len(parts) < 3:
                                    print(f"\n{COLOR_RED}[SKILLS ERROR: Missing pack or skill name. Usage: /skills off <pack|skill|all> (e.g. /skills off development or /skills off all)]{COLOR_RESET}")
                                    continue
                                target_arg = ",".join(parts[2:])
                                removed, unmatched = uninstall_session_skills(target_arg, verbose=True)
                                if unmatched:
                                    print(f"{COLOR_YELLOW}[SKILLS WARNING: No active session skill/pack matched: {', '.join(unmatched)}]{COLOR_RESET}")
                                if removed:
                                    inject_note = (
                                        f"[SYSTEM NOTICE: {len(removed)} skill(s) unloaded from /app/workspace/skills: {', '.join(removed)}.]"
                                    )
                                    messages = load_history()
                                    messages.append({"role": "user", "content": inject_note})
                                    save_history(messages)
                                    log_event("INJECTED CONTEXT TO BRAIN", inject_note)
                                continue

                            elif parts[1].lower() in ['save', 'export']:
                                if len(parts) < 4:
                                    print(f"\n{COLOR_RED}[SKILLS ERROR: Usage: /skills save <skill_name> <pack_subpath> (e.g. /skills save my_skill science)]{COLOR_RESET}")
                                    continue
                                ok, res_msg = save_session_skill_to_host(parts[2], parts[3])
                                if ok:
                                    print(f"\n{COLOR_BRIGHT_GREEN}[SKILLS SYSTEM: Saved session skill '{parts[2]}' permanently to host library at './skills/{res_msg}']{COLOR_RESET}")
                                else:
                                    print(f"\n{COLOR_RED}[SKILLS ERROR: {res_msg}]{COLOR_RESET}")
                                continue

                            else:
                                print(f"\n{COLOR_RED}[SKILLS ERROR: Unknown command '{user_input}'. Usage: /skills list | /skills on <pack|skill|all> | /skills off <pack|skill|all> | /skills save <skill> <pack>]{COLOR_RESET}")
                                continue

                        # --- 4d. BACKGROUND JOBS COMMANDS (/jobs) ---
                        if user_input.lower().startswith('/jobs'):
                            parts = user_input.split()
                            sub_cmd = parts[1].lower() if len(parts) > 1 else "list"
                            target_arg = parts[2] if len(parts) > 2 else None
                            try:
                                if sub_cmd in ("list", "ls"):
                                    res = await session.call_tool("manage_jobs", {"action": "list"})
                                elif sub_cmd in ("status", "log", "tail"):
                                    res = await session.call_tool("manage_jobs", {"action": "status", "job_id": target_arg})
                                elif sub_cmd in ("kill", "stop"):
                                    if not target_arg:
                                        print(f"\n{COLOR_RED}[JOBS ERROR: Usage: /jobs kill <job_id|all>]{COLOR_RESET}")
                                        continue
                                    res = await session.call_tool("manage_jobs", {"action": "kill", "job_id": target_arg})
                                else:
                                    # Allow shorthand: /jobs job_1 -> status of job_1
                                    res = await session.call_tool("manage_jobs", {"action": "status", "job_id": parts[1]})
                                out_txt = res.content[0].text if (res and res.content and hasattr(res.content[0], "text")) else str(res)
                                print(f"\n{COLOR_CYAN}{out_txt}{COLOR_RESET}")
                                print(f"{COLOR_DIM}Commands: /jobs list | /jobs status <job_id> | /jobs kill <job_id|all>{COLOR_RESET}\n")
                            except Exception as je:
                                print(f"\n{COLOR_RED}[JOBS ERROR: {je}]{COLOR_RESET}")
                            continue

                        # --- 5. EMPTY INPUT CHECK ---
                        if not user_input: continue
                            
                        log_event("USER", user_input)

                        
                        # Load and save state
                        messages = load_history()
                        messages.append({"role": "user", "content": user_input})
                        save_history(messages)

                        # --- ESCALATING LOOP DETECTION (INIT) ---
                        consecutive_tool_chains = 0
                        consecutive_no_tool_turns = 0
                        last_tool_call_signature = None
                        consecutive_identical_tool_calls = 0
                        
                        turn_start_time = time.time()
                        turn_llm_time = 0.0
                        turn_tool_time = 0.0
                        
                        while True:
                            try:
                                messages = load_history()
                                
                                # --- 1. SWEEP TRANSIENT INJECTIONS ---
                                # Remove any previous system clocks, active bg job banners, or warnings to prevent bloat and maintain clean history
                                messages = [
                                    msg for msg in messages
                                    if not (
                                        msg.get("role") == "user"
                                        and (
                                            "[SYSTEM CLOCK:" in str(msg.get("content"))
                                            or "[ACTIVE BACKGROUND JOBS (" in str(msg.get("content"))
                                            or "[SYSTEM WARNING: Your context window is at" in str(msg.get("content"))
                                        )
                                    )
                                ]

                                # --- 1b. HARVEST COMPLETED BACKGROUND JOBS ---
                                messages, _, running_bg_jobs = harvest_background_jobs(messages)

                                # --- 2. HISTORICAL TOOL OUTPUT AGING (H3) ---
                                messages, pruned_count = compact_historical_tool_outputs(messages)
                                if pruned_count > 0:
                                    last_actual_prompt_tokens = 0

                                # --- 3. TOKEN WARNING INJECTION (H1 Calibrated) ---
                                payload_tokens = estimate_tokens(messages)
                                last_estimated_payload_tokens = payload_tokens
                                # Calibrated estimate accounts for real serving tokenizer overhead
                                calibrated_payload = int(payload_tokens * token_calibration_ratio)
                                current_token_estimate = calibrated_payload + tool_schemas_overhead
                                if last_actual_prompt_tokens > 0 and len(messages) >= last_history_len and (calibrated_payload + tool_schemas_overhead) >= (last_actual_prompt_tokens - 1000):
                                    current_token_estimate = max(current_token_estimate, last_actual_prompt_tokens)
                                last_history_len = len(messages)
                                pct = current_token_estimate / config.MAX_CONTEXT_TOKENS

                                if pct >= 0.80:
                                    warn_msg = f"[SYSTEM WARNING: Your context window is at ~{pct*100:.0f}%. "
                                    if pct >= 0.90: warn_msg += "CRITICAL LIMIT REACHED. You MUST use the compact_context tool immediately.]"
                                    else: warn_msg += "Consider finishing your current task and using the compact_context tool soon.]"
                                    
                                    # Avoid appending duplicate warnings on consecutive tool turns
                                    if not any(m.get("content", "").startswith("[SYSTEM WARNING: Your context window is at") for m in messages[-2:]):
                                        messages.append({"role": "user", "content": warn_msg})
                                        print(f"\n{COLOR_YELLOW}{warn_msg}{COLOR_RESET}", flush=True)
                                        log_event("SYSTEM", warn_msg)

                                # --- 4. TIME & ACTIVE BACKGROUND JOBS INJECTION (Appended at end to preserve prefix caching) ---
                                bg_status_str = ""
                                if running_bg_jobs:
                                    max_bg_bash = getattr(config, "MAX_BACKGROUND_JOBS", 9)
                                    max_bg_sub = getattr(config, "MAX_BACKGROUND_SUBTASKS", 9)
                                    run_bash_cnt = sum(1 for rj in running_bg_jobs if rj.get("type", "bash") == "bash")
                                    run_sub_cnt = sum(1 for rj in running_bg_jobs if rj.get("type") == "subagent")
                                    bg_parts = []
                                    for rj in running_bg_jobs:
                                        el = int(time.time() - float(rj.get("start_epoch", time.time())))
                                        j_tag = rj.get("type", "bash") if rj.get("type") == "bash" else f"subagent:{rj.get('agent', 'analyst')}"
                                        bg_parts.append(f"{rj.get('job_id')} ({j_tag}, running {el}s)")
                                    bg_status_str = f"[ACTIVE BACKGROUND JOBS ({run_bash_cnt}/{max_bg_bash} bash, {run_sub_cnt}/{max_bg_sub} subtasks): {', '.join(bg_parts)}]"

                                # Only inject ambient system clock if at the start of a user turn (not immediately following a tool output)
                                if not (messages and messages[-1].get("role") == "tool"):
                                    live_time = datetime.now().strftime("%A, %B %d, %Y %H:%M:%S")
                                    clock_text = f"[SYSTEM CLOCK: It is currently {live_time}. (Ambient system timestamp: do not acknowledge or comment on this clock notice unless the user's prompt explicitly asks for the time.)]"
                                    if bg_status_str:
                                        clock_text += f"\n{bg_status_str}"
                                    messages.append({
                                        "role": "user", 
                                        "content": clock_text
                                    })
                                elif bg_status_str and config.VERBOSITY_MODE != "silent":
                                    print(f"{COLOR_CYAN}{bg_status_str}{COLOR_RESET}", flush=True)

                                # --- 4. SAVE THE PURIST HISTORY BEFORE API CALL ---
                                save_history(messages)

                                # Setup API args using the true, saved messages
                                api_args = brain_profile["api_params"].copy()
                                api_args["model"] = brain_profile["model"]
                                api_args["messages"] = messages
                                api_args["tools"] = openai_tools
                                api_args["stream"] = True
                                api_args["stream_options"] = {"include_usage": True}
                                
                                if api_args.get("seed") is None:
                                    api_args.pop("seed", None)

                                state_dir = os.path.join(SESSION_DIR, "state")
                                brain_turn_num = config.get_agent_turn_count(state_dir, "brain") + 1

                                if config.VERBOSITY_MODE != "silent":
                                    calib_note = f" (calibrated x{token_calibration_ratio:.2f})" if abs(token_calibration_ratio - 1.0) > 0.04 else ""
                                    print(f"\n{COLOR_YELLOW}[System: Brain #{brain_turn_num} payload is ~{payload_tokens} estimated tokens{calib_note} (Estimated total context window: ~{current_token_estimate})]{COLOR_RESET}", flush=True)
                                    if config.VERBOSITY_MODE in ["standard", "detailed"] and active_ext_servers:
                                        ext_tool_names = list(ext_tool_router.keys())
                                        print(f"{COLOR_CYAN}[Active External MCP Tools in Brain Context: {', '.join(ext_tool_names)}]{COLOR_RESET}", flush=True)

                                llm_start_time = time.time()
                                response_stream = await brain_client.chat.completions.create(**api_args)
                                
                                if config.VERBOSITY_MODE != "silent":
                                    time_str = datetime.now().strftime("%H:%M:%S")
                                    print(f"\n{COLOR_BLUE}[{time_str}] === BRAIN #{brain_turn_num} ==={COLOR_RESET}")
                                
                                full_content = ""
                                full_thinking = ""
                                tool_calls_dict = {}
                                final_usage = None
                                loop_interrupted = False  # Track if a loop circuit breaker trips
                                stream_finish_reason = None
                                last_loop_check_think = 0
                                last_loop_check_content = 0
                                
                                # --- MULTI-MODE CONTEXT ROUTING ---
                                if config.FORMAT_MODE == "markdown" and config.VERBOSITY_MODE != "silent":
                                    display_ctx = Live(console=console, refresh_per_second=4, transient=False)
                                else:
                                    display_ctx = nullcontext()

                                with display_ctx as live:
                                    async for chunk in response_stream:
                                        if chunk.usage:
                                            final_usage = chunk.usage

                                        if chunk.choices and len(chunk.choices) > 0 and chunk.choices[0].finish_reason:
                                            stream_finish_reason = chunk.choices[0].finish_reason

                                        if not chunk.choices or not chunk.choices[0].delta:
                                            continue
                                    
                                        if len(chunk.choices) > 0:
                                            delta = chunk.choices[0].delta
                                            
                                            chunk_thinking = getattr(delta, 'reasoning_content', None)
                                            if not chunk_thinking and hasattr(delta, "model_extra") and delta.model_extra:
                                                chunk_thinking = delta.model_extra.get("reasoning_content") or delta.model_extra.get("reasoning")
                                            
                                            if chunk_thinking: 
                                                full_thinking += chunk_thinking
                                                # Throttle loop check: only run every 100 characters to prevent event loop stutter
                                                if len(full_thinking) > 500 and (len(full_thinking) - last_loop_check_think >= 100):
                                                    last_loop_check_think = len(full_thinking)
                                                    tail_buffer = full_thinking[-10000:]
                                                    is_loop, loop_size = detect_text_loop(tail_buffer, required_repeats=3)

                                                    if is_loop:
                                                        print(f"\n{COLOR_RED}[SYSTEM CIRCUIT BREAKER] Thinking repetition detected. Halting stream...{COLOR_RESET}")
                                                        full_thinking = clean_tail_by_alphanumeric_count(full_thinking, loop_size * 3) + "\n\n[SYSTEM NOTICE: Thinking loop halted due to repetition.]"
                                                        loop_interrupted = True
                                                        break

                                            if delta.content: 
                                                full_content += delta.content
                                                # Throttle loop check: only run every 100 characters to prevent event loop stutter
                                                if len(full_content) > 500 and (len(full_content) - last_loop_check_content >= 100):
                                                    last_loop_check_content = len(full_content)
                                                    tail_buffer = full_content[-10000:]
                                                    is_loop, loop_size = detect_text_loop(tail_buffer, required_repeats=3)

                                                    if is_loop:
                                                        print(f"\n{COLOR_RED}[SYSTEM CIRCUIT BREAKER] Text output repetition detected. Halting stream...{COLOR_RESET}")
                                                        total_chars_to_purge = loop_size * 3
                                                        full_content = clean_tail_by_alphanumeric_count(full_content, total_chars_to_purge)
                                                        full_content += "\n\n[SYSTEM NOTICE: Generation halted due to a repetition loop.]"
                                                        loop_interrupted = True
                                                        break

                                            if delta.tool_calls:
                                                for tc in delta.tool_calls:
                                                    if tc.index not in tool_calls_dict:
                                                        tool_calls_dict[tc.index] = {
                                                            "id": tc.id or "", 
                                                            "type": "function", 
                                                            "function": {"name": (tc.function.name if (tc.function and tc.function.name) else "") or "", "arguments": ""}
                                                        }
                                                    else:
                                                        if tc.id and not tool_calls_dict[tc.index]["id"]:
                                                            tool_calls_dict[tc.index]["id"] = tc.id
                                                        if tc.function and getattr(tc.function, "name", None):
                                                            tool_calls_dict[tc.index]["function"]["name"] += tc.function.name
                                                    if tc.function and getattr(tc.function, "arguments", None):
                                                        tool_calls_dict[tc.index]["function"]["arguments"] += tc.function.arguments

                                            # --- CONSOLE MODE DISPATCHER ---
                                            if config.VERBOSITY_MODE != "silent":
                                                if config.FORMAT_MODE == "text":
                                                    if chunk_thinking and config.VERBOSITY_MODE in ["standard", "detailed"]:
                                                        print(f"{COLOR_DIM}{chunk_thinking}{COLOR_RESET}", end="", flush=True)
                                                    if delta.content:
                                                        print(delta.content, end="", flush=True)
                                                        
                                                elif config.FORMAT_MODE == "markdown":
                                                    display_elements = []
                                                    if full_thinking and config.VERBOSITY_MODE in ["standard", "detailed"]:
                                                        
                                                        # --- Cap the display height to prevent terminal overflow ---
                                                        think_lines = full_thinking.splitlines()
                                                        max_lines = 15
                                                        
                                                        if len(think_lines) > max_lines:
                                                            display_thinking = "...\n" + "\n".join(think_lines[-max_lines:])
                                                        else:
                                                            display_thinking = full_thinking
                                                            
                                                        display_elements.append(Panel(
                                                            Markdown(display_thinking),
                                                            title=f"[bold yellow]BRAIN #{brain_turn_num} THOUGHTS[/bold yellow]", border_style="yellow", padding=(1, 2), subtitle="[dim white]Internal Logic Loop[/dim white]"
                                                        ))
                                                        
                                                    if full_content:
                                                        display_elements.append(Markdown(full_content))
                                                    
                                                    if not full_content and tool_calls_dict:
                                                        tool_names = [tc['function']['name'] for tc in tool_calls_dict.values()]
                                                        display_elements.append(Markdown(f"*Preparing tool calls: {', '.join(f'`{n}`' for n in tool_names)}...*"))
                                                        
                                                    if display_elements:
                                                        live.update(Group(*display_elements))

                                # If mode is "silent", we do nothing visually!
                                if config.VERBOSITY_MODE != "silent":
                                    print()  # Drop a clean newline after the stream is fully finished
                                    if stream_finish_reason == "length":
                                        print(f"{COLOR_YELLOW}[SYSTEM NOTICE: Response reached max_tokens limit while emitting reasoning/response. Output was stopped by serving model. All thinking and generated content have been safely preserved in session logs.]{COLOR_RESET}")

                                if not full_thinking and full_content:
                                    for tag in ["think", "thought"]:
                                        if f"<{tag}>" in full_content:
                                            think_match = re.search(rf"<{tag}>(.*?)</{tag}>", full_content, re.DOTALL)
                                            if think_match:
                                                full_thinking = think_match.group(1).strip()
                                                full_content = re.sub(rf"<{tag}>.*?</{tag}>", "", full_content, flags=re.DOTALL).strip()
                                                break
                                            elif full_content.strip().startswith(f"<{tag}>"):
                                                full_thinking = re.sub(rf"^<{tag}>\s*", "", full_content, flags=re.DOTALL).strip()
                                                full_content = ""
                                                break

                                assistant_message = {"role": "assistant", "content": full_content}
                                
                                if full_thinking:
                                    assistant_message["reasoning_content"] = full_thinking

                                # Strictly omit any parsed tools if the stream was forcefully aborted
                                if tool_calls_dict and not loop_interrupted:
                                    assistant_message["tool_calls"] = list(tool_calls_dict.values())
                                    
                                messages = load_history()
                                messages.append(assistant_message)
                                
                                # Process state saving depending on loop status
                                if loop_interrupted:
                                    messages.append({
                                        "role": "user", 
                                        "content": (
                                            "[CRITICAL INTERVENTION: Your stream was automatically halted because a highly repetitive text sequence or looping thought-pattern was detected."
                                            "1. IF YOU ARE TRAPPED IN AN UNINTENDED ERROR/CODE LOOP: Stop immediately. Do NOT apologize. Change your strategy, use 'manage_plan' to pivot, or use 'consult_adviser' to analyze your project workspace files. "
                                            "2. IF YOU ARE INTENTIONALLY GENERATING REPETITIVE DATA FOR A USER TEST: Acknowledge that the system's streaming guardrail was tripped by the repetition. Do NOT keep trying to stream the exact same multi-paragraph payload out to the console. Instead, use 'write_file' to dump the requested repetitive dataset cleanly into an output file for the user, summarize what you did in a brief sentence, and wait for new instructions.]"
                                            )
                                    })
                                    if not final_usage:
                                        fb_prompt = estimate_tokens(messages)
                                        fb_comp = len(tokenizer.encode(full_content)) if tokenizer else len(full_content) // 4
                                        fb_reasoning = len(full_thinking) // 4 if full_thinking else 0
                                        final_usage = types.SimpleNamespace(
                                            prompt_tokens=fb_prompt,
                                            completion_tokens=fb_comp,
                                            completion_tokens_details=types.SimpleNamespace(reasoning_tokens=fb_reasoning)
                                        )
                                    save_history(messages)
                                    log_event(f"BRAIN #{brain_turn_num}", full_content, final_usage, full_thinking)
                                    
                                    llm_duration = time.time() - llm_start_time
                                    turn_llm_time += llm_duration
                                    
                                    # ◄--- Keeps session statistics completely accurate! ---
                                    reasoning_tokens = getattr(final_usage.completion_tokens_details, 'reasoning_tokens', 0) if hasattr(final_usage, 'completion_tokens_details') and final_usage.completion_tokens_details else 0
                                    if reasoning_tokens == 0 and full_thinking: 
                                        reasoning_tokens = len(full_thinking) // 4
                                    config.log_token_usage(os.path.join(SESSION_DIR, "state"), "brain", final_usage.prompt_tokens, final_usage.completion_tokens, reasoning_tokens)

                                    consecutive_tool_chains = 0
                                    continue
                                else:
                                    is_actual_usage = final_usage is not None
                                    if not final_usage:
                                        fb_prompt = estimate_tokens(messages)
                                        fb_comp = len(tokenizer.encode(full_content)) if tokenizer else len(full_content) // 4
                                        fb_reasoning = len(full_thinking) // 4 if full_thinking else 0
                                        final_usage = types.SimpleNamespace(
                                            prompt_tokens=fb_prompt,
                                            completion_tokens=fb_comp,
                                            completion_tokens_details=types.SimpleNamespace(reasoning_tokens=fb_reasoning)
                                        )
                                    save_history(messages)
                                    log_event(f"BRAIN #{brain_turn_num}", full_content, final_usage, full_thinking)

                                # Update exact token count state and calibrate tokenizer for the next loop!
                                if is_actual_usage and final_usage.prompt_tokens and last_estimated_payload_tokens > 0:
                                    # Calibrate ratio between serving model's tokenizer and local cl100k estimate
                                    observed_msg_tokens = max(1, final_usage.prompt_tokens - tool_schemas_overhead)
                                    observed_ratio = observed_msg_tokens / last_estimated_payload_tokens
                                    if 0.5 <= observed_ratio <= 3.0:
                                        token_calibration_ratio = (0.7 * token_calibration_ratio) + (0.3 * observed_ratio)
                                last_actual_prompt_tokens = final_usage.prompt_tokens
                                reasoning_tokens = 0
                                if hasattr(final_usage, 'completion_tokens_details') and final_usage.completion_tokens_details:
                                    reasoning_tokens = getattr(final_usage.completion_tokens_details, 'reasoning_tokens', 0)
                                
                                if reasoning_tokens == 0 and full_thinking:
                                    reasoning_tokens = len(full_thinking) // 4
                                    
                                config.log_token_usage(
                                    os.path.join(SESSION_DIR, "state"),
                                    "brain",
                                    final_usage.prompt_tokens,
                                    final_usage.completion_tokens,
                                    reasoning_tokens
                                )
                                    
                                llm_duration = time.time() - llm_start_time
                                turn_llm_time += llm_duration

                                if config.VERBOSITY_MODE != "silent":   
                                    est_tag = "" if is_actual_usage else " (est)"
                                    max_ctx = getattr(config, "MAX_CONTEXT_TOKENS", 120000)
                                    ctx_pct = (final_usage.prompt_tokens / max_ctx * 100) if max_ctx else 0.0
                                    llm_time_str = f"Time: {llm_duration:.1f}s"
                                    ctx_str = f"Context: {ctx_pct:.1f}% ({final_usage.prompt_tokens:,}/{max_ctx:,})"
                                    if reasoning_tokens > 0:
                                        print(f"{COLOR_YELLOW}[Brain #{brain_turn_num} | Tokens{est_tag}: {final_usage.prompt_tokens:,} in | {final_usage.completion_tokens:,} out (~{reasoning_tokens:,} thinking) | {llm_time_str} | {ctx_str}]{COLOR_RESET}")
                                    else:
                                        print(f"{COLOR_YELLOW}[Brain #{brain_turn_num} | Tokens{est_tag}: {final_usage.prompt_tokens:,} in | {final_usage.completion_tokens:,} out | {llm_time_str} | {ctx_str}]{COLOR_RESET}")

                                if not tool_calls_dict:
                                    messages = load_history()
                                    messages, harvested_cnt, running_bg = harvest_background_jobs(messages)
                                    if harvested_cnt > 0:
                                        save_history(messages)
                                        continue
                                    if running_bg and consecutive_no_tool_turns == 0:
                                        consecutive_no_tool_turns += 1
                                        for _ in range(6):
                                            await asyncio.sleep(0.5)
                                            messages = load_history()
                                            messages, harvested_cnt, running_bg = harvest_background_jobs(messages)
                                            if harvested_cnt > 0:
                                                break
                                        if harvested_cnt > 0:
                                            save_history(messages)
                                            continue
                                        running_summary = ", ".join(f"{j.get('job_id')} ({j.get('type')})" for j in running_bg)
                                        bg_nudge = (
                                            f"[SYSTEM BACKGROUND JOBS NOTICE: You did not invoke any tools, but {len(running_bg)} background job(s) are still running: {running_summary}. "
                                            f"If you need their output before finishing, call manage_jobs(action='wait', job_id='{running_bg[0].get('job_id')}', wait_seconds=120) now. "
                                            f"If a background daemon/server is no longer needed, call manage_jobs(action='kill', job_id='...') before concluding. "
                                            f"If your task is 100% finished and the background process is intentionally left running, reply confirming completion without invoking tools.]"
                                        )
                                        messages.append({"role": "user", "content": bg_nudge})
                                        save_history(messages)
                                        if config.VERBOSITY_MODE != "silent":
                                            print(f"\n{COLOR_YELLOW}{bg_nudge}{COLOR_RESET}", flush=True)
                                        log_event("SYSTEM", bg_nudge)
                                        continue
                                    if cli_args.exit and consecutive_no_tool_turns == 0:
                                        consecutive_no_tool_turns += 1
                                        nudge_msg = config.PROMPTS["autonomous_audit_nudge"]
                                        messages = load_history()
                                        messages.append({"role": "user", "content": nudge_msg})
                                        save_history(messages)
                                        if config.VERBOSITY_MODE != "silent":
                                            print(f"\n{COLOR_YELLOW}{nudge_msg}{COLOR_RESET}", flush=True)
                                        log_event("SYSTEM", nudge_msg)
                                        continue
                                    break

                                consecutive_no_tool_turns = 0

                                state_dir = os.path.join(SESSION_DIR, "state")
                                next_call_num = config.get_tool_totals(state_dir).get("total_calls", 0) + 1

                                # Partition tool calls into concurrent execution waves
                                parsed_tool_items = []
                                for tc_data in assistant_message["tool_calls"]:
                                    t_name = tc_data["function"]["name"]
                                    t_args_str = tc_data["function"]["arguments"]
                                    t_id = tc_data["id"]
                                    c_num = next_call_num
                                    next_call_num += 1
                                    try:
                                        t_args = json.loads(t_args_str)
                                        if not isinstance(t_args, dict):
                                            t_args = {}
                                        parsed_tool_items.append({
                                            "name": t_name, "args": t_args, "args_str": t_args_str,
                                            "tc_id": t_id, "call_num": c_num, "malformed": False
                                        })
                                    except json.JSONDecodeError:
                                        parsed_tool_items.append({
                                            "name": t_name, "args": {}, "args_str": t_args_str,
                                            "tc_id": t_id, "call_num": c_num, "malformed": True
                                        })

                                file_writers = {"create_tool", "edit_code", "write_file", "create_skill"}
                                file_consumers = {"execute_bash", "analyze_files", "consult_adviser", "load_skill"}

                                def _is_file_writer(it):
                                    nm = it["name"]
                                    ag = it["args"] if isinstance(it["args"], dict) else {}
                                    if nm == "delegate_task" and ag.get("background", True):
                                        return False
                                    return (
                                        nm in file_writers
                                        or (nm in ("query_auxiliary_llm", "delegate_task") and bool(ag.get("output_filepath")))
                                        or (nm == "execute_rest_call" and bool(ag.get("response_filepath")))
                                    )

                                def _is_file_consumer(it):
                                    nm = it["name"]
                                    ag = it["args"] if isinstance(it["args"], dict) else {}
                                    if nm == "delegate_task" and ag.get("background", True):
                                        return False
                                    return nm in file_consumers or bool(ag.get("context_filepaths")) or bool(ag.get("media_filepaths"))

                                def _target_resource(it):
                                    nm = it["name"]
                                    ag = it["args"] if isinstance(it["args"], dict) else {}
                                    if nm in ("write_file", "edit_code") and ag.get("filepath"):
                                        return f"file:{os.path.normpath(str(ag['filepath']).strip())}"
                                    if nm in ("query_auxiliary_llm", "delegate_task") and ag.get("output_filepath"):
                                        return f"file:{os.path.normpath(str(ag['output_filepath']).strip())}"
                                    if nm == "execute_rest_call" and ag.get("response_filepath"):
                                        return f"file:{os.path.normpath(str(ag['response_filepath']).strip())}"
                                    if nm == "create_tool" and ag.get("tool_name"):
                                        return f"tool:{str(ag['tool_name']).strip().lower()}"
                                    if nm == "create_skill" and ag.get("skill_name"):
                                        return f"skill:{str(ag['skill_name']).strip().lower()}"
                                    if nm in ("query_sqlite_db", "batch_generate_embeddings") and ag.get("db_path"):
                                        return f"db:{os.path.normpath(str(ag['db_path']).strip())}"
                                    if nm == "manage_plan":
                                        return "state:active_plan"
                                    if nm in ("store_memory", "read_memory", "view_memory_registry"):
                                        return "state:memory_registry" if nm == "store_memory" else ""
                                    return ""

                                max_wave_tools = getattr(config, "MAX_CONCURRENT_TOOLS", 30)

                                def _can_join_wave(current_wave, candidate):
                                    if not current_wave:
                                        return True
                                    if len(current_wave) >= max_wave_tools:
                                        return False
                                    for item in (current_wave[-1], candidate):
                                        if item["malformed"]:
                                            return False
                                        iname = item["name"]
                                        if iname in ("compact_context", "manage_external_mcp", "manage_skills"):
                                            return False
                                        if iname in ext_tool_router and ext_tool_router[iname].get("require_confirmation", False):
                                            return False
                                    cname = candidate["name"]
                                    cargs = candidate["args"] if isinstance(candidate["args"], dict) else {}
                                    if cname == "execute_bash" and not cargs.get("background", False):
                                        if any(w["name"] == "execute_bash" and not (w["args"].get("background", False) if isinstance(w["args"], dict) else False) for w in current_wave):
                                            return False
                                    if (_is_file_consumer(candidate) and any(_is_file_writer(w) for w in current_wave)) or (_is_file_writer(candidate) and any(_is_file_consumer(w) for w in current_wave)):
                                        return False
                                    cand_res = _target_resource(candidate)
                                    if cand_res and any(_target_resource(w) == cand_res for w in current_wave):
                                        return False
                                    return True

                                tool_waves = []
                                for item in parsed_tool_items:
                                    if not tool_waves or not _can_join_wave(tool_waves[-1], item):
                                        tool_waves.append([item])
                                    else:
                                        tool_waves[-1].append(item)

                                compress_triggered = False
                                for wave in tool_waves:
                                    if len(wave) == 1 and wave[0]["malformed"]:
                                        m_item = wave[0]
                                        name = m_item["name"]
                                        call_num = m_item["call_num"]
                                        args_str = m_item["args_str"]
                                        tc_id = m_item["tc_id"]
                                        error_msg = "SYSTEM ERROR: You provided invalid JSON arguments for this tool call. Please check your syntax (watch out for unescaped quotes or missing brackets) and try again."
                                        print(f"{COLOR_RED}Error decoding JSON (Tool #{call_num}: {name}). Intercepting and asking Brain to retry...{COLOR_RESET}")
                                        config.log_tool_usage(state_dir, name, caller="brain", duration_seconds=0.0, is_error=True)
                                        log_event(f"TOOL CALL #{call_num} ({name})", f"Requesting: {name}\nArgs: [MALFORMED JSON]\n{args_str}")
                                        log_event(f"TOOL RESULT #{call_num} ({name}) (0.00s)", error_msg, text_color=COLOR_RED)
                                        messages = load_history()
                                        messages.append({
                                            "role": "tool",
                                            "tool_call_id": tc_id,
                                            "name": name,
                                            "content": error_msg
                                        })
                                        save_history(messages)
                                        continue

                                    if len(wave) > 1 and config.VERBOSITY_MODE != "silent":
                                        print(f"\n{COLOR_CYAN}[⚡ Parallel Wave: Executing {len(wave)} tools concurrently (Tools #{wave[0]['call_num']}–#{wave[-1]['call_num']})]{COLOR_RESET}")

                                    hide_args = config.VERBOSITY_MODE in ["minimal", "standard"]
                                    for w_item in wave:
                                        name = w_item["name"]
                                        args = w_item["args"]
                                        call_num = w_item["call_num"]
                                        log_event(f"TOOL CALL #{call_num} ({name})", f"Requesting: {name}\nArgs: {json.dumps(args, indent=2)}", hide_console=hide_args)

                                        if config.VERBOSITY_MODE != "silent":
                                            pfx = f"▶ Tool #{call_num}: "
                                            if name == "create_tool":
                                                print(f"\n{COLOR_ORANGE}{pfx}Routing to Coder for tool creation...{COLOR_RESET}")
                                            elif name == "compact_context":
                                                print(f"\n{COLOR_ORANGE}{pfx}Triggering Memory Manager Pipeline... Awaiting response...{COLOR_RESET}")
                                            elif name == "consult_adviser":
                                                print(f"\n{COLOR_ORANGE}{pfx}Consulting Senior Adviser... Awaiting strategic report...{COLOR_RESET}")
                                            elif name == "fast_decision":
                                                print(f"\n{COLOR_ORANGE}{pfx}Routing to The Decider for rapid ruling...{COLOR_RESET}")
                                            elif name == "query_auxiliary_llm":
                                                target_model = args.get("model", "")
                                                target_ep = args.get("endpoint", "")
                                                ep_str = f" ({target_model}" + (f" on {target_ep}" if target_ep else "") + ")" if target_model else ""
                                                print(f"\n{COLOR_ORANGE}{pfx}Spawning Auxiliary LLM{ep_str}... Awaiting response...{COLOR_RESET}")
                                            elif name == "execute_rest_call":
                                                target_url = args.get("url", "")
                                                print(f"\n{COLOR_ORANGE}{pfx}Dispatching REST call ({target_url})...{COLOR_RESET}")
                                            elif name == "analyze_files":
                                                print(f"\n{COLOR_ORANGE}{pfx}Passing files to The Analyst... Awaiting report...{COLOR_RESET}")
                                            elif name == "create_skill":
                                                print(f"\n{COLOR_ORANGE}{pfx}Waking up the Architect to draft skill...{COLOR_RESET}")
                                            elif name == "manage_external_mcp":
                                                action_tag = args.get("action", "")
                                                srv_tag = args.get("server_name", "")
                                                print(f"\n{COLOR_CYAN}{pfx}Managing external MCP tool suites ({action_tag} {srv_tag})...{COLOR_RESET}")
                                            elif name == "manage_skills":
                                                action_tag = args.get("action", "")
                                                tgt_tag = args.get("target", "")
                                                print(f"\n{COLOR_CYAN}{pfx}Managing host skill packs ({action_tag} {tgt_tag})...{COLOR_RESET}")
                                            elif name == "delegate_task":
                                                ag_tag = (args.get("agent") or "analyst").upper()
                                                bg_mode = "background" if args.get("background", True) else "foreground"
                                                print(f"\n{COLOR_ORANGE}{pfx}Delegating subtask to {ag_tag} ({bg_mode})...{COLOR_RESET}")
                                            elif name == "manage_jobs":
                                                act_tag = args.get("action", "list")
                                                jid_tag = args.get("job_id") or ""
                                                print(f"\n{COLOR_CYAN}{pfx}Managing background jobs ({act_tag} {jid_tag})...{COLOR_RESET}")
                                            elif name in ext_tool_router:
                                                srv_id = ext_tool_router[name]["server_id"]
                                                print(f"\n{COLOR_CYAN}{pfx}Routing to external host MCP '{srv_id}': {name}...{COLOR_RESET}")
                                            elif hide_args:
                                                print(f"{COLOR_ORANGE}{pfx}Running tool: {name}...{COLOR_RESET}")

                                    async def _execute_wave_item(w_item):
                                        name = w_item["name"]
                                        args = w_item["args"]
                                        t_start = time.time()
                                        try:
                                            if name == "manage_external_mcp":
                                                output = await handle_manage_external_mcp(args)
                                                result = None
                                            elif name == "manage_skills":
                                                output = await handle_manage_skills(args)
                                                result = None
                                            elif name in ext_tool_router:
                                                target = ext_tool_router[name]
                                                srv_id = target["server_id"]
                                                orig_name = target["original_name"]
                                                if target.get("require_confirmation", False):
                                                    if cli_args.exit:
                                                        output = f"SYSTEM ERROR: Tool '{name}' requires human confirmation, but running in unattended batch mode (-x). Execution stopped/denied."
                                                        print(f"\n{COLOR_RED}[SECURITY GATE] {output}{COLOR_RESET}")
                                                        log_event("SYSTEM", output)
                                                        result = None
                                                    else:
                                                        print(f"\n{COLOR_RED}[SECURITY GATE] The Brain requests to execute external tool '{name}' on '{srv_id}'.{COLOR_RESET}")
                                                        print(f"{COLOR_YELLOW}Arguments: {json.dumps(args, indent=2)}{COLOR_RESET}")
                                                        try:
                                                            confirm = await asyncio.to_thread(input, f"{COLOR_CYAN}Allow execution on host? [y/N]: {COLOR_RESET}")
                                                        except (EOFError, KeyboardInterrupt):
                                                            confirm = "n"
                                                        if confirm.strip().lower() not in ["y", "yes"]:
                                                            output = f"SYSTEM NOTICE: Execution of external tool '{name}' was DENIED by the user for security reasons."
                                                            print(f"{COLOR_YELLOW}{output}{COLOR_RESET}")
                                                            log_event("SYSTEM", output)
                                                            result = None
                                                        else:
                                                            result = await call_external_tool(target, orig_name, args)
                                                else:
                                                    result = await call_external_tool(target, orig_name, args)
                                            else:
                                                # Auto-mount host skill if load_skill is called for an unmounted host skill
                                                if name == "load_skill" and isinstance(args, dict):
                                                    req_skill = os.path.basename(str(args.get("skill_name") or "").strip())
                                                    if req_skill and not os.path.exists(os.path.join(SESSION_DIR, "skills", req_skill, "SKILL.md")):
                                                        install_host_skills(req_skill, verbose=True)
                                                result = await session.call_tool(name, args)

                                            if result is not None:
                                                if result.content and len(result.content) > 0 and hasattr(result.content[0], "text"):
                                                    output = result.content[0].text
                                                elif result.content and len(result.content) > 0:
                                                    output = str(result.content[0])
                                                else:
                                                    output = "SUCCESS: Tool executed with no output."
                                        except Exception as tool_err:
                                            output = f"SYSTEM ERROR: Tool execution failed: {str(tool_err)}"
                                        return output, (time.time() - t_start)

                                    wave_start_time = time.time()
                                    totals_before = config.get_token_totals(state_dir)
                                    wave_results = await asyncio.gather(*[_execute_wave_item(w_item) for w_item in wave])
                                    wave_wall_duration = time.time() - wave_start_time
                                    turn_tool_time += wave_wall_duration
                                    totals_after = config.get_token_totals(state_dir)
                                    wave_token_diff = config.get_totals_diff(totals_before, totals_after)

                                    subagent_call_cursors = {
                                        ag: totals_before.get(ag, {}).get("calls", 0)
                                        for ag in ("coder", "adviser", "analyst", "architect", "summarizer", "decider", "auxiliary")
                                    }

                                    for idx_in_wave, (w_item, (output, tool_duration)) in enumerate(zip(wave, wave_results)):
                                        name = w_item["name"]
                                        args = w_item["args"]
                                        tc_id = w_item["tc_id"]
                                        call_num = w_item["call_num"]

                                        # --- FAILURE STREAK TRACKER (REFINED) ---
                                        is_error = False
                                        output_lower = output.lower()
                                        output_stripped = output_lower.lstrip()
                                        if name == "execute_bash":
                                            if output_stripped.startswith(("system error:", "error executing")):
                                                is_error = True
                                            elif output_stripped.startswith("exit code:") and not output_stripped.startswith("exit code: 0"):
                                                cmd_str = str(args.get("command", "")).strip() if isinstance(args, dict) else ""
                                                first_word = cmd_str.split()[0] if cmd_str.split() else ""
                                                if first_word in ["grep", "egrep", "fgrep", "rg", "diff", "cmp", "test", "[", "pgrep"] and output_stripped.startswith("exit code: 1"):
                                                    is_error = False
                                                else:
                                                    is_error = True
                                        else:
                                            if output_stripped.startswith(("system error:", "[system error:", "[architect error]", "error:", "error executing", "error writing", "error reading", "failed", "fatal tool creation exception", "fatal forging exception", "llm query failed", "analyst failed", "unknown tool", "tool not found", "validation error", "syntax error")):
                                                is_error = True
                                            elif "traceback (most recent" in output_lower and not output_stripped.startswith(("---", "success", "[")):
                                                is_error = True

                                        config.log_tool_usage(
                                            state_dir,
                                            name,
                                            caller="brain",
                                            duration_seconds=tool_duration,
                                            is_error=is_error
                                        )

                                        if wave_token_diff and config.VERBOSITY_MODE != "silent" and (len(wave) == 1 or idx_in_wave == len(wave) - 1):
                                            diff_parts = []
                                            for agent, details in wave_token_diff.items():
                                                part = f"{agent}: +{details['total']:,} ({details['prompt']:,} in, {details['completion']:,} out"
                                                if details['thinking'] > 0:
                                                    part += f", {details['thinking']:,} thinking"
                                                part += ")"
                                                diff_parts.append(part)
                                            tool_label = f"Tool #{call_num} ({name})" if len(wave) == 1 else f"Parallel Wave #{wave[0]['call_num']}–#{wave[-1]['call_num']}"
                                            time_val = tool_duration if len(wave) == 1 else wave_wall_duration
                                            print(f"{COLOR_YELLOW}[Tokens used: {', '.join(diff_parts)} | Time: {time_val:.2f}s | {tool_label}]{COLOR_RESET}")

                                        # Update the streak, tracking command variance so pivoting does not trigger false loops
                                        cmd_fingerprint = json.dumps(args, sort_keys=True) if isinstance(args, dict) else str(args)
                                        if is_error:
                                            prev_cmd = tool_last_failed_args.get(name, "")
                                            if prev_cmd and prev_cmd == cmd_fingerprint:
                                                tool_failure_streaks[name] = tool_failure_streaks.get(name, 0) + 1
                                            else:
                                                tool_failure_streaks[name] = 1
                                                tool_last_failed_args[name] = cmd_fingerprint
                                        else:
                                            tool_failure_streaks[name] = 0
                                            tool_last_failed_args[name] = ""

                                        if tool_failure_streaks.get(name, 0) >= 3:
                                            intervention_msg = (
                                                f"[CRITICAL SYSTEM ALERT: You have failed to use the '{name}' tool {tool_failure_streaks[name]} times in a row. "
                                                f"YOU ARE STUCK IN A LOOP. You MUST STOP trying the exact same command. "
                                                f"Reflect on why this is failing. Consider using 'consult_adviser' for a new strategy, "
                                                f"or use 'fetch_webpage' to read the documentation for what you are trying to do.]"
                                            )
                                            output += f"\n\n{intervention_msg}"
                                            if config.VERBOSITY_MODE != "silent":
                                                print(f"\n{COLOR_RED}[SYSTEM: AI is stuck looping on {name}. Injecting forced intervention!]{COLOR_RESET}")
                                            log_event("SYSTEM", f"Forced intervention triggered for tool: {name}")

                                        # --- CONSECUTIVE IDENTICAL TOOL CALL TRACKER (SOFT ADVISORY) ---
                                        current_call_sig = (name, cmd_fingerprint)
                                        if current_call_sig == last_tool_call_signature:
                                            consecutive_identical_tool_calls += 1
                                        else:
                                            last_tool_call_signature = current_call_sig
                                            consecutive_identical_tool_calls = 1

                                        if consecutive_identical_tool_calls >= 5 and consecutive_identical_tool_calls % 5 == 0:
                                            soft_loop_msg = config.PROMPTS["tool_loop_soft_warning"].format(
                                                tool_name=name,
                                                count=consecutive_identical_tool_calls
                                            )
                                            output += f"\n\n{soft_loop_msg}"
                                            if config.VERBOSITY_MODE != "silent":
                                                print(f"\n{COLOR_YELLOW}[SYSTEM: Soft loop advisory for '{name}' ({consecutive_identical_tool_calls} identical calls in a row)]{COLOR_RESET}")
                                            log_event("SYSTEM", f"Soft loop advisory triggered for tool: {name} ({consecutive_identical_tool_calls} identical calls)")

                                        # --- SUBAGENT HIDDEN THOUGHTS & ARTIFACTS LOGGING ---
                                        subagent_specs = [
                                            ("CODER", "coder", r"<___CODER_THOUGHTS___>(.*?)</___CODER_THOUGHTS___>", r"<___CODER_CODE___>(.*?)</___CODER_CODE___>", "GENERATED CODE / EDIT"),
                                            ("ADVISER", "adviser", r"<___ADVISER_THOUGHTS___>(.*?)</___ADVISER_THOUGHTS___>", r"<___ADVISER_REPORT___>(.*?)</___ADVISER_REPORT___>", "ADVISER REPORT"),
                                            ("ANALYST", "analyst", r"<___ANALYST_THOUGHTS___>(.*?)</___ANALYST_THOUGHTS___>", r"<___ANALYST_REPORT___>(.*?)</___ANALYST_REPORT___>", "ANALYST REPORT"),
                                            ("ARCHITECT", "architect", r"<___ARCHITECT_THOUGHTS___>(.*?)</___ARCHITECT_THOUGHTS___>", r"<___ARCHITECT_SKILL___>(.*?)</___ARCHITECT_SKILL___>", "GENERATED SKILL"),
                                            ("SUMMARIZER", "summarizer", r"<___SUMMARIZER_THOUGHTS___>(.*?)</___SUMMARIZER_THOUGHTS___>", r"<___SUMMARIZER_OUTPUT___>(.*?)</___SUMMARIZER_OUTPUT___>", "SUMMARIZER OUTPUT"),
                                            ("DECIDER", "decider", None, r"<___DECIDER_RULING___>(.*?)</___DECIDER_RULING___>", "RULING"),
                                            ("AUXILIARY LLM", "auxiliary", r"<___AUXILIARY_THOUGHTS___>(.*?)</___AUXILIARY_THOUGHTS___>", r"<___AUXILIARY_OUTPUT___(?:\s+model=\"([^\"]*)\")?>(.*?)</___AUXILIARY_OUTPUT___>", "RAW OUTPUT"),
                                        ]

                                        for agent_label, agent_key, thought_pattern, content_pattern, content_label in subagent_specs:
                                            agent_thoughts = ""
                                            agent_content = ""
                                            model_tag = ""

                                            if thought_pattern:
                                                match_t = re.search(thought_pattern, output, re.DOTALL)
                                                if match_t:
                                                    agent_thoughts = match_t.group(1).strip()
                                                    output = re.sub(thought_pattern, "", output, flags=re.DOTALL).strip()

                                            if content_pattern:
                                                match_c = re.search(content_pattern, output, re.DOTALL)
                                                if match_c:
                                                    if len(match_c.groups()) == 2:
                                                        model_tag = f" ({match_c.group(1)})" if match_c.group(1) else ""
                                                        agent_content = match_c.group(2).strip()
                                                    else:
                                                        agent_content = match_c.group(1).strip()
                                                    output = re.sub(content_pattern, "", output, flags=re.DOTALL).strip()

                                            if agent_thoughts or agent_content:
                                                time_str = datetime.now().strftime("%H:%M:%S")
                                                if len(wave) == 1:
                                                    sub_call_num = totals_after.get(agent_key, {}).get("calls", 0)
                                                else:
                                                    subagent_call_cursors[agent_key] = subagent_call_cursors.get(agent_key, 0) + 1
                                                    sub_call_num = subagent_call_cursors[agent_key]
                                                sub_num_tag = f" #{sub_call_num}" if sub_call_num > 0 else ""
                                                display_label = f"{agent_label}{sub_num_tag}{model_tag}"
                                                log_text = f"\n[{time_str}] === {display_label} (HIDDEN) ===\n"

                                                hide_subagent = config.VERBOSITY_MODE in ["silent", "minimal", "standard"]

                                                if not hide_subagent:
                                                    print(f"\n{COLOR_ORANGE}[{time_str}] === {display_label} (HIDDEN) ==={COLOR_RESET}")

                                                if agent_thoughts:
                                                    log_text += f"<thinking>\n{agent_thoughts}\n</thinking>\n\n"
                                                    if not hide_subagent:
                                                        print(f"{COLOR_DIM}<thinking>\n{agent_thoughts}\n</thinking>{COLOR_RESET}")

                                                if agent_content:
                                                    log_text += f"--- {content_label} ---\n{agent_content}\n\n"
                                                    if not hide_subagent:
                                                        print(f"--- {content_label} ---\n{agent_content}\n")

                                                with open(LOG_FILE, "a", encoding="utf-8") as f:
                                                    f.write(log_text.replace("\x00", ""))

                                        out_color = COLOR_ORANGE if name in ["create_tool", "compact_context", "create_skill", "consult_adviser", "fast_decision", "query_auxiliary_llm", "analyze_files", "execute_rest_call", "delegate_task"] else COLOR_DARK_GREEN
                                        
                                        hide_output = config.VERBOSITY_MODE in ["minimal", "standard"]
                                        log_event(f"TOOL RESULT #{call_num} ({name}) ({tool_duration:.2f}s)", output, text_color=out_color, hide_console=hide_output)
                                        
                                        if hide_output and config.VERBOSITY_MODE != "silent":
                                            if name in ["create_skill", "consult_adviser", "fast_decision", "query_auxiliary_llm", "analyze_files", "create_tool", "execute_rest_call", "delegate_task", "manage_jobs"]:
                                                print(f"{out_color}{output}{COLOR_RESET}")
                                            else:
                                                print(f"{COLOR_DARK_GREEN}✓ Tool #{call_num} '{name}' completed ({tool_duration:.2f}s).{COLOR_RESET}")

                                        if name == "compact_context" and output.startswith("SUCCESS:"):
                                            print(f"\n{COLOR_ORANGE}[SYSTEM] Memory compression cycle complete. Waking up with pristine context...{COLOR_RESET}")
                                            messages = load_history()
                                            if len(messages) <= 1:
                                                messages.append({
                                                    "role": "user",
                                                    "content": output
                                                })
                                                save_history(messages)
                                            last_actual_prompt_tokens = 0
                                            last_history_len = 0
                                            consecutive_tool_chains = 0
                                            compress_triggered = True
                                            break
                                        else:
                                            messages = load_history()
                                            messages.append({
                                                "role": "tool",
                                                "tool_call_id": tc_id,
                                                "name": name,
                                                "content": output
                                            })
                                            save_history(messages)

                                    if compress_triggered:
                                        break

                                # --- ESCALATING LOOP DETECTION (CHECK) ---
                                consecutive_tool_chains += 1

                                if consecutive_tool_chains >= 300:
                                    # Hard Stop: Protect the API limits
                                    halt_msg = f"[SYSTEM METRIC: You have executed {consecutive_tool_chains} consecutive tool chains. For safety and observability, you MUST STOP using tools now. Summarize your progress and ask the user for permission to continue.]"
                                    messages = load_history()
                                    messages = [msg for msg in messages if not (msg.get("role") == "user" and "[SYSTEM METRIC: You have executed" in str(msg.get("content")))]
                                    messages.append({"role": "user", "content": halt_msg})
                                    save_history(messages)
                                    print(f"\n{COLOR_YELLOW}[SYSTEM: Hard pause triggered ({consecutive_tool_chains} chains). Forcing Brain to wait for user.]{COLOR_RESET}", flush=True)
                                    log_event("SYSTEM", halt_msg)
                                    consecutive_tool_chains = 0
                                
                                elif consecutive_tool_chains > 0 and consecutive_tool_chains % 100 == 0:
                                    messages = load_history()
                                    # Sweep old soft-pauses before saving so they never accumulate
                                    messages = [msg for msg in messages if not (msg.get("role") == "user" and "[SYSTEM METRIC: You have executed" in str(msg.get("content")))]
                                    # Soft Reflection: Ask the AI to evaluate itself
                                    eval_msg = f"[SYSTEM METRIC: You have executed {consecutive_tool_chains} consecutive tool chains. Please review your recent actions. Are you making steady progress, or are you stuck in an error loop? If you are stuck or repeatedly failing, STOP using tools and ask the user for input. If you are making legitimate progress, continue.]"
                                    messages.append({"role": "user", "content": eval_msg})
                                    save_history(messages)
                                    print(f"\n{COLOR_YELLOW}[SYSTEM: Soft pause triggered ({consecutive_tool_chains} chains). Prompting Brain to self-evaluate.]{COLOR_RESET}", flush=True)
                                    log_event("SYSTEM", eval_msg)
                              
                                    
                            except (KeyboardInterrupt, asyncio.CancelledError):
                                print(f"\n\n{COLOR_RED}[SYSTEM] 🛑 Process manually interrupted! Returning to prompt...{COLOR_RESET}")
                                log_event("SYSTEM", "Process manually interrupted by user.")
                                
                                messages = load_history()
                                # Reconcile pending tool_calls to maintain valid assistant->tool message structure
                                if messages:
                                    for msg in reversed(messages):
                                        if msg.get("role") == "assistant":
                                            if msg.get("tool_calls"):
                                                existing_tc_ids = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
                                                for tc in msg["tool_calls"]:
                                                    tc_id = tc.get("id")
                                                    if tc_id and tc_id not in existing_tc_ids:
                                                        messages.append({
                                                            "role": "tool",
                                                            "tool_call_id": tc_id,
                                                            "name": tc.get("function", {}).get("name", "unknown"),
                                                            "content": "[INTERRUPTED: Tool execution cancelled by user (Ctrl+C)]"
                                                        })
                                            break
                                        elif msg.get("role") == "user":
                                            break

                                messages.append({
                                     "role": "user", 
                                     "content": "[SYSTEM ALERT: The user pressed Ctrl+C to instantly abort the previous text generation or tool execution. Stop what you were doing, acknowledge the interruption, and await new instructions.]"
                                 })
                                save_history(messages)
                                break
                        if config.VERBOSITY_MODE != "silent":
                            state_dir = os.path.join(SESSION_DIR, "state")
                            totals = config.get_token_totals(state_dir)
                            if totals:
                                totals_parts = []
                                for agent, details in totals.items():
                                    if agent == "_grand_total":
                                        continue
                                    calls_cnt = details.get("calls", 0)
                                    calls_tag = f" (#{calls_cnt})" if calls_cnt > 0 else ""
                                    part = f"{agent}{calls_tag}: {details['total']:,}"
                                    if details['thinking'] > 0:
                                        part += f" ({details['thinking']:,} thinking)"
                                    totals_parts.append(part)
                                grand = totals.get("_grand_total", {"total": 0, "thinking": 0})
                                grand_str = f"grand: {grand['total']:,}"
                                if grand['thinking'] > 0:
                                    grand_str += f" ({grand['thinking']:,} thinking)"
                                totals_parts.append(grand_str)
                                print(f"\n{COLOR_YELLOW}[Session Tokens: {' | '.join(totals_parts)}]{COLOR_RESET}")

                            tool_totals = config.get_tool_totals(state_dir)
                            total_calls = tool_totals.get("total_calls", 0)
                            by_tool = tool_totals.get("by_tool", {})
                            if total_calls > 0 and by_tool:
                                breakdown_parts = [f"{t}: {c}" for t, c in sorted(by_tool.items(), key=lambda x: -x[1])]
                                breakdown_str = f" ({', '.join(breakdown_parts)})"
                            else:
                                breakdown_str = ""

                            brain_turns_total = totals.get("brain", {}).get("calls", 0) if totals else 0
                            turn_total_time = time.time() - turn_start_time
                            max_ctx = getattr(config, "MAX_CONTEXT_TOKENS", 120000)
                            tokens_for_ctx = last_actual_prompt_tokens if last_actual_prompt_tokens > 0 else current_token_estimate
                            ctx_pct = (tokens_for_ctx / max_ctx * 100) if max_ctx else 0.0

                            tools_summary_line = (
                                f"[Brain Turns: {brain_turns_total} | Session Tools: {total_calls} calls{breakdown_str} | "
                                f"Turn Time: {turn_total_time:.1f}s (LLM: {turn_llm_time:.1f}s, Tools: {turn_tool_time:.1f}s) | "
                                f"Context: {ctx_pct:.1f}% ({tokens_for_ctx:,}/{max_ctx:,})]"
                            )
                            if not totals:
                                print(f"\n{COLOR_YELLOW}{tools_summary_line}{COLOR_RESET}")
                            else:
                                print(f"{COLOR_YELLOW}{tools_summary_line}{COLOR_RESET}")
                                
                    # Clean up any active external MCP sessions
                    for s_data in list(active_ext_servers.values()):
                        try:
                            await s_data["cmd_queue"].put({"action": "close"})
                            await asyncio.wait_for(s_data["task"], timeout=2.0)
                        except Exception:
                            pass
                    active_ext_servers.clear()
                    ext_tool_router.clear()
                                
        # 3. CATCH DEAD CONTAINERS AND RESTART
        except (KeyboardInterrupt, asyncio.CancelledError):
            print(f"\n{COLOR_YELLOW}[SYSTEM] Process interrupted by user. Shutting down sandbox cleanly...{COLOR_RESET}")
            quit_app = True
            break
        except Exception as e:
            # Check if this exception was caused by a user interrupt or broken pipe during cancellation
            is_interrupt = False
            if type(e).__name__ in ["BaseExceptionGroup", "ExceptionGroup"] and hasattr(e, "exceptions"):
                is_interrupt = any(
                    isinstance(sub, (KeyboardInterrupt, asyncio.CancelledError)) or
                    type(sub).__name__ in ["KeyboardInterrupt", "CancelledError", "BrokenResourceError", "ClosedResourceError"]
                    for sub in e.exceptions
                )
            elif type(e).__name__ in ["BrokenResourceError", "ClosedResourceError"]:
                is_interrupt = True

            if is_interrupt:
                print(f"\n{COLOR_YELLOW}[SYSTEM] Process interrupted by user. Shutting down sandbox cleanly...{COLOR_RESET}")
                quit_app = True
                break

            # Unwraps ExceptionGroups to print the real underlying API/network failures
            if type(e).__name__ in ["BaseExceptionGroup", "ExceptionGroup"] and hasattr(e, "exceptions"):
                print(f"\n{COLOR_RED}[CRASH DETECTED] Wrapped Exception Group Context Triggered:{COLOR_RESET}")
                for sub_exception in e.exceptions:
                    print(f"{COLOR_YELLOW}- Internal Error Type: {type(sub_exception).__name__}: {str(sub_exception)}{COLOR_RESET}")
                    print(f"{COLOR_DIM}--- SUB-TRACEBACK ---{COLOR_RESET}")
                    traceback.print_exception(type(sub_exception), sub_exception, sub_exception.__traceback__)
                    print(f"{COLOR_DIM}---------------------{COLOR_RESET}")
            else:
                print(f"\n{COLOR_RED}[CRASH DETECTED] {type(e).__name__}: {str(e)}{COLOR_RESET}")
                print(f"{COLOR_YELLOW}--- TRACEBACK ---{COLOR_RESET}")
                traceback.print_exc()
                print(f"{COLOR_YELLOW}-----------------{COLOR_RESET}")
                print(f"\n{COLOR_YELLOW}[SYSTEM] Sandbox connection dropped or API failed. Restarting loop...{COLOR_RESET}")
            await asyncio.sleep(1)

if __name__ == "__main__":
    try:
        asyncio.run(run_chat())
    except KeyboardInterrupt:
        print(f"\n{COLOR_RED}[SYSTEM] Forced shutdown. Goodbye!{COLOR_RESET}")