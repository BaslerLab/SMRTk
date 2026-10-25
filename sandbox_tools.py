import asyncio
import signal
import os
import json
import subprocess
import traceback
import logging
import re
import sys
import shutil
import shlex
import argparse
import sqlite3
import sqlite_vec
import array
import base64
import urllib.parse
import urllib.request
import urllib.error
import mimetypes
import socket
import ipaddress
import time
import httpx
from typing import Any, Optional, Union, Dict, List
from datetime import datetime
from openai import AsyncOpenAI
from playwright.async_api import async_playwright
from playwright_stealth import Stealth
from bs4 import BeautifulSoup

import tiktoken
tokenizer = tiktoken.get_encoding("cl100k_base")

import config

# --- HIDE FASTMCP INTERNAL LOGS BEFORE IMPORT ---
# We MUST set this before importing FastMCP, otherwise it reads the default settings!
os.environ["FASTMCP_LOG_ENABLED"] = "false"
from fastmcp import FastMCP


# --- CATCH CLI ARGUMENTS FROM PODMAN ---
parser = argparse.ArgumentParser(description="Sandbox Tools (Containerized)")
parser.add_argument("--coder", type=int, help="Coder LLM profile index")
parser.add_argument("--summarizer", type=int, help="Summarizer LLM profile index")
parser.add_argument("--adviser", type=int, help="Adviser LLM profile index")
parser.add_argument("--analyst", type=int, help="Analyst LLM profile index")
parser.add_argument("--architect", type=int, help="Architect LLM profile index")
args, unknown = parser.parse_known_args() # Ignore other arguments Podman might pass
# --- HIDE ARGUMENTS FROM FASTMCP ---
sys.argv = [sys.argv[0]] + unknown

# --- OVERRIDE CONFIG IN MEMORY ---
if args.coder is not None: config.ACTIVE_CODER_PROFILE = args.coder
if args.summarizer is not None: config.ACTIVE_SUMMARIZER_PROFILE = args.summarizer
if args.adviser is not None: config.ACTIVE_ADVISER_PROFILE = args.adviser
if args.analyst is not None: config.ACTIVE_ANALYST_PROFILE = args.analyst
if args.architect is not None: config.ACTIVE_ARCHITECT_PROFILE = args.architect

## Start the MCP server
mcp = FastMCP("sandbox_mcp")


# --- MCP STREAM PROTECTION & LOGGING ---
# Suppress stdout logging to protect FastMCP, but route logs to a file for debugging
log_file_path = "/app/workspace/logs/container_debug.log"
os.makedirs(os.path.dirname(log_file_path), exist_ok=True)

logging.basicConfig(
    filename=log_file_path,
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
# Forcefully clear any existing console handlers that might corrupt the JSON-RPC stream
logging.getLogger().handlers = [h for h in logging.getLogger().handlers if isinstance(h, logging.FileHandler)]
for noisy_lib in ("httpx", "httpx2", "httpcore", "fastmcp", "openai"):
    logging.getLogger(noisy_lib).setLevel(logging.WARNING)
logger = logging.getLogger("SandboxMCP")


WORKSPACE_DIR = "/app/workspace"
STATE_DIR = os.path.join(WORKSPACE_DIR, "state")
REPORTS_DIR = os.path.join(STATE_DIR, "reports")
FAILURES_DIR = os.path.join(STATE_DIR, "failures")
JOBS_DIR = os.path.join(STATE_DIR, "jobs")
BG_JOBS_FILE = os.path.join(JOBS_DIR, "bg_jobs.json")
_ACTIVE_BG_TASKS: Dict[str, Dict[str, Any]] = {}
SANDBOX_DIR = os.path.join(WORKSPACE_DIR, "sandbox")
OUTPUTS_DIR = os.path.join(WORKSPACE_DIR, "outputs")
TOOLS_DIR = os.path.join(WORKSPACE_DIR, "tools")
_LOADED_SKILLS = set()
MEMORIES_DIR = os.path.join(WORKSPACE_DIR, "memories")
HISTORIES_DIR = os.path.join(WORKSPACE_DIR, "histories")
TOOL_REGISTRY_FILE = os.path.join(STATE_DIR, "tool_registry.json")
MEMORY_REGISTRY_FILE = os.path.join(STATE_DIR, "memory_registry.json")
CURRENT_HISTORY_FILE = os.path.join(STATE_DIR, "current_history.json")

coder_profile = config.LLM_PROFILES[config.ACTIVE_CODER_PROFILE]
summarizer_profile = config.LLM_PROFILES[config.ACTIVE_SUMMARIZER_PROFILE]
adviser_profile = config.LLM_PROFILES[config.ACTIVE_ADVISER_PROFILE]

if coder_profile.get("base_url"):
    coder_client = AsyncOpenAI(base_url=coder_profile["base_url"], api_key=coder_profile["api_key"], timeout=120.0)
else:
    coder_client = AsyncOpenAI(api_key=coder_profile["api_key"], timeout=120.0)
    
if summarizer_profile.get("base_url"):
    summarizer_client = AsyncOpenAI(base_url=summarizer_profile["base_url"], api_key=summarizer_profile["api_key"], timeout=180.0)
else:
    summarizer_client = AsyncOpenAI(api_key=summarizer_profile["api_key"], timeout=180.0)

if adviser_profile.get("base_url"):
    adviser_client = AsyncOpenAI(base_url=adviser_profile["base_url"], api_key=adviser_profile["api_key"], timeout=300.0)
else:
    adviser_client = AsyncOpenAI(api_key=adviser_profile["api_key"], timeout=300.0)

analyst_profile = config.LLM_PROFILES[config.ACTIVE_ANALYST_PROFILE]

if analyst_profile.get("base_url"):
    analyst_client = AsyncOpenAI(base_url=analyst_profile["base_url"], api_key=analyst_profile["api_key"], timeout=300.0)
else:
    analyst_client = AsyncOpenAI(api_key=analyst_profile["api_key"], timeout=300.0)

architect_profile = config.LLM_PROFILES[config.ACTIVE_ARCHITECT_PROFILE]

if architect_profile.get("base_url"):
    architect_client = AsyncOpenAI(base_url=architect_profile["base_url"], api_key=architect_profile["api_key"], timeout=180.0)
else:
    architect_client = AsyncOpenAI(api_key=architect_profile["api_key"], timeout=180.0)
    

# --- Initialize Embedding Client ---
emb_config = config.EMBEDDING_CONFIG
embedding_client = AsyncOpenAI(
    base_url=emb_config["base_url"], 
    api_key=emb_config["api_key"], 
    timeout=emb_config["timeout"]
)

# --- Initialize Decider Config ---
decider_config = config.DECIDER_CONFIG

# --- HELPER FUNCTIONS ---
def extract_thinking_and_content(message) -> tuple[str, str]:
    """Extracts thinking/reasoning content and main text content from an OpenAI message object or dict."""
    thinking = None
    if isinstance(message, dict):
        thinking = message.get('reasoning_content') or message.get('reasoning')
        content = message.get('content') or ""
    else:
        thinking = getattr(message, 'reasoning_content', None)
        if not thinking and hasattr(message, 'model_extra') and message.model_extra:
            thinking = message.model_extra.get('reasoning_content') or message.model_extra.get('reasoning')
        content = getattr(message, 'content', None) or ""
    
    if not thinking:
        for tag in ["think", "thought"]:
            if f"<{tag}>" in content:
                think_match = re.search(rf"<{tag}>(.*?)</{tag}>", content, re.DOTALL)
                if think_match:
                    thinking = think_match.group(1).strip()
                    content = re.sub(rf"<{tag}>.*?</{tag}>", "", content, flags=re.DOTALL).strip()
                    break
                elif content.strip().startswith(f"<{tag}>"):
                    thinking = re.sub(rf"^<{tag}>\s*", "", content, flags=re.DOTALL).strip()
                    content = ""
                    break
            
    return (thinking or "").strip(), (content or "").strip()

def extract_json_payload(raw_text: str):
    """
    Robustly extracts and parses a JSON payload from an LLM response string.
    Handles:
    1. Pure valid JSON strings.
    2. JSON wrapped in markdown fences (```json ... ``` or ``` ... ```).
    3. JSON containing inner markdown fences inside string values without corrupting the outer structure.
    4. Leading or trailing conversational text wrapping a JSON object or array.
    """
    if not raw_text or not isinstance(raw_text, str):
        raise json.JSONDecodeError("Empty or non-string input", str(raw_text), 0)
    
    clean_text = raw_text.strip()
    
    # 1. Direct parse attempt (fastest, cleanest, handles inner markdown untouched)
    try:
        return json.loads(clean_text)
    except Exception:
        pass
        
    # 2. Outer markdown code fence stripping
    if clean_text.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*\r?\n?", "", clean_text, flags=re.IGNORECASE)
        stripped = re.sub(r"\r?\n?```\s*$", "", stripped).strip()
        try:
            return json.loads(stripped)
        except Exception:
            pass

    # 3. Outer code block regex (matching outermost ``` fence)
    match = re.search(r"```(?:json)?\s*(\{.*\}|\[.*\])\s*```", clean_text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except Exception:
            pass

    # 4. Outermost brace/bracket extraction (for conversational wrapper text)
    first_brace = clean_text.find("{")
    last_brace = clean_text.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        try:
            return json.loads(clean_text[first_brace:last_brace+1])
        except Exception:
            pass

    first_bracket = clean_text.find("[")
    last_bracket = clean_text.rfind("]")
    if first_bracket != -1 and last_bracket != -1 and last_bracket > first_bracket:
        try:
            return json.loads(clean_text[first_bracket:last_bracket+1])
        except Exception:
            pass

    # 5. Fallback: try raw json.loads to raise standard JSONDecodeError
    return json.loads(clean_text)

def load_json(filepath):
    if not os.path.exists(filepath): return {}
    with open(filepath, "r") as f: return json.load(f)

def save_json(filepath, data):
    """Saves JSON using an atomic write to prevent corruption on crash."""
    temp_path = f"{filepath}.tmp"
    
    # 1. Write to a temporary file first
    with open(temp_path, "w", encoding="utf-8") as f: 
        json.dump(data, f, indent=4)
        
    # 2. Atomically swap it with the real file
    # If the system crashes during step 1, the original file is untouched!
    os.replace(temp_path, filepath)

def save_failure_trace(subagent_name: str, payload: dict) -> str:
    """Saves a failure trace / post-mortem diagnostic artifact to state/failures/ for full observability."""
    try:
        os.makedirs(FAILURES_DIR, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = f"failure_{subagent_name}_{timestamp}.json"
        filepath = os.path.join(FAILURES_DIR, filename)
        save_json(filepath, payload)
        return filepath
    except Exception as e:
        logger.error(f"[save_failure_trace] Failed to save trace for {subagent_name}: {e}")
        return ""

def sqlite_authorizer(action, arg1, arg2, dbname, source):
    # 9 = SQLITE_DELETE, 11 = SQLITE_DROP_TABLE, 24 = SQLITE_ATTACH
    denied = (sqlite3.SQLITE_DELETE, sqlite3.SQLITE_DROP_TABLE)
    if hasattr(sqlite3, "SQLITE_ATTACH"):
        denied += (sqlite3.SQLITE_ATTACH,)
    if action in denied:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK

def get_payload_tokens(messages):
    """Accurately counts text tokens and adds safe mathematical buffers for images, audio, video, and PDFs."""
    total_text = ""
    image_count = 0
    audio_count = 0
    video_count = 0
    
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):  # Handle multimodal/multi-part arrays
            for part in content:
                p_type = part.get("type")
                if p_type == "text":
                    total_text += part.get("text", "")
                elif p_type == "image_url":
                    image_count += 1
                elif p_type in ["input_audio", "audio"]:
                    audio_count += 1
                elif p_type in ["video", "input_video"]:
                    video_count += 1
        else:  # Handle standard string content
            total_text += str(content)
            
        # Count tool arguments if they exist
        if "tool_calls" in msg and msg["tool_calls"]:
            for tc in msg["tool_calls"]:
                total_text += str(tc.get("function", {}).get("arguments", ""))
            
    # Measure exact text tokens
    text_tokens = len(tokenizer.encode(total_text))
    
    # Add safe buffers for multimodal parts (~1000 tokens per image/pdf, ~2000 per audio, ~3000 per video)
    image_tokens = image_count * 1000
    audio_tokens = audio_count * 2000
    video_tokens = video_count * 3000
    
    return text_tokens + image_tokens + audio_tokens + video_tokens + (len(messages) * 4)


def build_multimodal_content(text_content: str, media_filepaths: list[str] = None) -> tuple[Union[list[dict], str], list[str]]:
    """Encodes attached images, audio, video, and PDF files into standard OpenAI/LiteLLM multipart content blocks.
    Returns: (content_blocks_or_text, error_warnings)
    """
    if not media_filepaths:
        return text_content, []

    content_blocks = [{"type": "text", "text": text_content}]
    warnings = []

    for path in media_filepaths:
        if not path or not isinstance(path, str):
            continue
        clean_path = path.strip()
        if not clean_path:
            continue

        # Handle remote HTTP(S) URLs directly
        if clean_path.startswith("http://") or clean_path.startswith("https://"):
            mime_type, _ = mimetypes.guess_type(clean_path)
            content_blocks.append({
                "type": "image_url",
                "image_url": {"url": clean_path}
            })
            continue

        # Local file resolution inside safe boundaries
        resolved_path = os.path.realpath(clean_path)
        if not os.path.exists(resolved_path):
            alt_path = os.path.realpath(os.path.join(WORKSPACE_DIR, clean_path.lstrip("/")))
            if os.path.exists(alt_path):
                resolved_path = alt_path
            else:
                warnings.append(f"[MEDIA ERROR: Attached file '{clean_path}' not found on disk.]")
                continue

        try:
            allowed_safe = [WORKSPACE_DIR, "/app/workspace", "/app/host_input", "/app"]
            valid_safe = [d for d in allowed_safe if os.path.exists(d)]
            if valid_safe and not any(os.path.commonpath([resolved_path, d]) == d for d in valid_safe):
                warnings.append(f"[MEDIA ACCESS DENIED: Path '{os.path.basename(resolved_path)}' falls outside safe workspace boundaries.]")
                continue
        except Exception:
            pass

        file_size_bytes = os.path.getsize(resolved_path)
        mime_type, _ = mimetypes.guess_type(resolved_path)
        filename = os.path.basename(resolved_path)

        # File size safety limits (10MB for images, 25MB for audio/video/PDF)
        mime = (mime_type or "").lower()
        max_size = 25 * 1024 * 1024 if (mime.startswith("audio/") or mime.startswith("video/") or mime == "application/pdf") else 10 * 1024 * 1024
        if file_size_bytes > max_size:
            warnings.append(f"[MEDIA ERROR: File '{filename}' is too massive ({file_size_bytes / (1024*1024):.1f} MB, max {max_size // (1024*1024)} MB).]")
            continue

        try:
            with open(resolved_path, "rb") as mf:
                raw_bytes = mf.read()
            b64_data = base64.b64encode(raw_bytes).decode("utf-8")
        except Exception as read_err:
            warnings.append(f"[MEDIA READ ERROR: Could not read '{filename}': {str(read_err)}]")
            continue

        effective_mime = mime or "application/octet-stream"
        if effective_mime.startswith("image/"):
            content_blocks.append({
                "type": "image_url",
                "image_url": {"url": f"data:{effective_mime};base64,{b64_data}"}
            })
        elif effective_mime.startswith("audio/"):
            fmt = "wav" if "wav" in effective_mime else ("mp3" if "mp3" in effective_mime or "mpeg" in effective_mime else "wav")
            content_blocks.append({
                "type": "input_audio",
                "input_audio": {"data": b64_data, "format": fmt}
            })
        elif effective_mime.startswith("video/") or effective_mime == "application/pdf":
            # LiteLLM/Gemini data URI standard
            content_blocks.append({
                "type": "image_url",
                "image_url": {"url": f"data:{effective_mime};base64,{b64_data}"}
            })
        else:
            content_blocks.append({
                "type": "image_url",
                "image_url": {"url": f"data:{effective_mime};base64,{b64_data}"}
            })

    if warnings:
        warn_text = "\n".join(warnings) + "\n\n"
        if content_blocks and content_blocks[0].get("type") == "text":
            content_blocks[0]["text"] = warn_text + content_blocks[0]["text"]

    return content_blocks, warnings


def read_file_slice(
    filepath: str,
    start_line: int = 1,
    max_lines: int = None,
    tail_mode: bool = False,
    max_chars: int = 50000
) -> tuple[str, int, int, int, bool]:
    """Reads a bounded slice of a text file, reporting exact line numbers and coverage.
    Returns: (content, line_start_idx, line_end_idx, total_lines, is_partial)
    """
    file_size = os.path.getsize(filepath)
    if file_size == 0:
        return "", 0, 0, 0, False

    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()

    total_lines = len(all_lines)
    if total_lines == 0:
        return "", 0, 0, 0, False

    if tail_mode:
        candidate_lines = all_lines[-max_lines:] if (max_lines and max_lines > 0) else all_lines
        selected = []
        chars = 0
        for line in reversed(candidate_lines):
            if chars + len(line) > max_chars and selected:
                break
            selected.append(line)
            chars += len(line)
        selected.reverse()
        start_idx = total_lines - len(selected) + 1
        end_idx = total_lines
        content = "".join(selected)
    elif start_line > 1 or (max_lines is not None and max_lines > 0):
        s_idx = max(0, start_line - 1)
        e_idx = s_idx + max_lines if (max_lines and max_lines > 0) else total_lines
        candidate_lines = all_lines[s_idx:e_idx]
        selected = []
        chars = 0
        for line in candidate_lines:
            if chars + len(line) > max_chars and selected:
                break
            selected.append(line)
            chars += len(line)
        start_idx = s_idx + 1
        end_idx = s_idx + len(selected)
        content = "".join(selected)
    else:
        selected = []
        chars = 0
        for line in all_lines:
            if chars + len(line) > max_chars and selected:
                break
            selected.append(line)
            chars += len(line)
        start_idx = 1
        end_idx = len(selected)
        content = "".join(selected)

    is_partial = (start_idx > 1 or end_idx < total_lines)
    return content, start_idx, end_idx, total_lines, is_partial


def filter_file_lines(
    filepath: str,
    pattern: str,
    context_lines: int = 2,
    max_chars: int = 250000
) -> tuple[str, int, int, int, bool]:
    """Scans a file using regex/keyword matching and extracts matching lines 
    along with surrounding context lines. Merges contiguous/overlapping windows.
    Returns: (formatted_content, total_matches, included_matches, total_lines, is_partial)
    """
    file_size = os.path.getsize(filepath)
    if file_size == 0:
        return "", 0, 0, 0, False

    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error:
        regex = re.compile(re.escape(pattern), re.IGNORECASE)

    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()

    total_lines = len(all_lines)
    if total_lines == 0:
        return "", 0, 0, 0, False

    match_indices = [idx + 1 for idx, l in enumerate(all_lines) if regex.search(l)]
    total_matches = len(match_indices)
    if total_matches == 0:
        return f"[NO MATCHES: Filter pattern '{pattern}' found 0 occurrences across {total_lines:,} total lines.]", 0, 0, total_lines, False

    intervals = []
    for m_idx in match_indices:
        start = max(1, m_idx - context_lines)
        end = min(total_lines, m_idx + context_lines)
        if intervals and start <= intervals[-1][1] + 1:
            intervals[-1] = (intervals[-1][0], max(intervals[-1][1], end))
        else:
            intervals.append((start, end))

    blocks = []
    current_chars = 0
    included_matches = 0
    is_partial = False

    for (start, end) in intervals:
        interval_matches = [m for m in match_indices if start <= m <= end]
        lines_text = []
        for l_no in range(start, end + 1):
            prefix = ">>>" if l_no in match_indices else "   "
            lines_text.append(f"{prefix} {l_no:6d} | {all_lines[l_no - 1]}")
        block_str = f"--- [Lines {start}–{end}] ---\n" + "".join(lines_text)
        if current_chars + len(block_str) > max_chars and blocks:
            is_partial = True
            break
        blocks.append(block_str)
        current_chars += len(block_str)
        included_matches += len(interval_matches)

    if len(intervals) > len(blocks):
        is_partial = True

    content = "\n\n".join(blocks)
    return content, total_matches, included_matches, total_lines, is_partial


def gather_agent_context(filepaths: list[str] = None, max_chars_per_file: int = 40000) -> str:
    """Natively extracts and strings together absolute file contents inside the sandbox environment, 
    allowing sub-agents to read project code states without blowing out the main Overseer memory bank.
    """
    if not filepaths:
        return ""
        
    context_str = "\n\n=== ATTACHED AGENT CONTEXT BACKGROUND ENVIRONMENT ==="
    for path in filepaths:
        resolved_path = os.path.realpath(path)
        if os.path.exists(resolved_path):
            try:
                allowed_roots = [d for d in [WORKSPACE_DIR, "/app/workspace", "/app/host_input", "/app"] if os.path.exists(d)]
                if allowed_roots and not any(os.path.commonpath([resolved_path, d]) == d for d in allowed_roots):
                    context_str += f"\n\n[ACCESS DENIED: Path '{os.path.basename(resolved_path)}' falls outside safe workspace boundaries.]"
                    continue
                    
                # Check raw file size before reading to protect system memory
                file_size_bytes = os.path.getsize(resolved_path)
                if file_size_bytes > 5_000_000: # 5MB limit safety cutoff
                    context_str += f"\n\n--- REFERENCE FILE STATE: {os.path.basename(resolved_path)} ---\n[SYSTEM NOTICE: This file is too massive ({file_size_bytes / 1024 / 1024:.2f} MB) to read directly. Use dedicated grep or chunk analysis tools.]"
                    continue

                mime_t, _ = mimetypes.guess_type(resolved_path)
                mime_str = (mime_t or "").lower()
                if mime_str and (mime_str.startswith("image/") or mime_str.startswith("audio/") or mime_str.startswith("video/") or mime_str == "application/pdf"):
                    context_str += f"\n\n--- REFERENCE FILE: {os.path.basename(resolved_path)} ---\n[MULTIMODAL MEDIA FILE ({mime_str}): Pass via 'media_filepaths' in query_auxiliary_llm or 'filepaths' in analyze_files to inspect visually/auditorily.]"
                    continue

                content, start_idx, end_idx, total_lines, is_partial = read_file_slice(
                    resolved_path, max_chars=max_chars_per_file
                )
                est_tokens = len(tokenizer.encode(content))
                
                if is_partial:
                    pct = max(1, int((len(content) / file_size_bytes) * 100)) if file_size_bytes > 0 else 100
                    context_str += (
                        f"\n\n--- REFERENCE FILE STATE: {os.path.basename(resolved_path)} (~{est_tokens} tokens) "
                        f"[PARTIALLY INGESTED: lines {start_idx}–{end_idx} of {total_lines:,} ({pct}% coverage)] ---\n"
                        f"[COVERAGE BOUNDARY: Read first {len(content):,} of {file_size_bytes:,} chars (lines {start_idx} to {end_idx}). "
                        f"The remaining {total_lines - end_idx:,} lines were omitted to protect sub-agent context budget. "
                        f"ANTI-HALLUCINATION DIRECTIVE: Do NOT assume or guess function signatures, logic, or variables in unread lines {end_idx + 1}–{total_lines:,}.]\n"
                        f"{content}\n"
                        f"... [COVERAGE WARNING: TRUNCATED NATIVELY — Read lines {start_idx}–{end_idx} of {total_lines:,} ({pct}%). "
                        f"Lines {end_idx + 1} to {total_lines:,} omitted. To inspect downstream lines, slice with bash 'sed -n \\'{end_idx + 1},{end_idx + 500}p\\' <path>' or 'grep'.] ..."
                    )
                else:
                    context_str += f"\n\n--- REFERENCE FILE STATE: {os.path.basename(resolved_path)} (~{est_tokens} tokens, {total_lines:,} lines) [FULL CONTENT] ---\n{content}"
            except Exception as e:
                context_str += f"\n\n[READ FAULT: {os.path.basename(resolved_path)} - Error: {str(e)}]"
        else:
            context_str += f"\n\n[FILE TARGET NOT FOUND: {path}]"
            
    return context_str + "\n=======================================================\n\n"


# --- MCP TOOLS ---
@mcp.tool()
def view_tool_registry(category: str = None, detailed: bool = False) -> str:
    """Views the custom tools registry. 
    - Pass NO arguments to see top-level categories. 
    - Pass a category string to see tools in that category (returns path, language, description, and exists status).
    - Set detailed=True to include the full original authoring objective.
    """
    registry = load_json(TOOL_REGISTRY_FILE)
    if not registry: return "Tool Registry is empty."
    
    if not category:
        summary = {cat: data.get("category_description", "") for cat, data in registry.items()}
        return json.dumps({"categories": summary}, indent=2) + "\n\nCall this tool again with category='<name>' to view tools inside a category."
    else:
        if category in registry:
            tools_data = registry[category].get("tools", {})
            if not detailed:
                compact_tools = {}
                for t_name, t_info in tools_data.items():
                    compact_tools[t_name] = {
                        "path": t_info.get("path"),
                        "description": t_info.get("description"),
                        "language": t_info.get("language"),
                        "exists": os.path.exists(t_info.get("path", "")) if t_info.get("path") else False
                    }
                return json.dumps({category: compact_tools}, indent=2)
            else:
                return json.dumps({category: tools_data}, indent=2)
        else:
            return f"Category '{category}' not found. Available categories are: {list(registry.keys())}"


@mcp.tool()
def manage_plan(action: str, content: str = None) -> str:
    """Reads or completely overwrites the Master Project Plan.
    'action' must be 'read' or 'write'.
    If action is 'write', you MUST provide the full, updated markdown text in 'content'.
    """
    plan_path = os.path.join(STATE_DIR, "active_plan.md")
    normalized_action = action.strip().rstrip(">").rstrip(":").strip().lower() if action else ""
    
    if normalized_action == "read":
        if os.path.exists(plan_path):
            with open(plan_path, "r", encoding="utf-8") as f:
                return f"--- CURRENT MASTER PLAN ---\n{f.read()}"
        else:
            return "No active plan exists yet. Please initialize one using the 'write' action."
            
    elif normalized_action == "write":
        if not content:
            return "Error: You must provide the full markdown string in the 'content' argument to write."
        if os.path.exists(plan_path):
            try:
                with open(plan_path, "r", encoding="utf-8") as f_cur:
                    current_plan = f_cur.read()
                if current_plan.strip() == content.strip():
                    return "SUCCESS: Master Plan is already up to date (no changes detected)."
            except Exception:
                pass
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(plan_path, "w", encoding="utf-8") as f:
            f.write(content)
        return "SUCCESS: Master Plan has been updated and saved to disk."
        
    else:
        return f"Error: Invalid action '{action}'. Valid actions are 'read' and 'write'."

@mcp.tool()
async def consult_adviser(current_plan: str, encountered_problems: str, context_filepaths: list[str] = None) -> str:
    """Consults the Senior Adviser AI for strategic guidance.
    Pass your current plan and a detailed description of the problems or bottlenecks you are facing.
    The Adviser will review your available tools and memories and return a strategic document.
    'context_filepaths' provides explicit absolute paths to failing scripts, profiles, or log segments to audit.
    """
    tool_registry = load_json(TOOL_REGISTRY_FILE)
    memory_registry = load_json(MEMORY_REGISTRY_FILE)
    
    # ◄--- Gather background file targets ---
    file_environmental_context = gather_agent_context(context_filepaths)
    
    user_prompt = (
        f"--- CURRENT TOOL REGISTRY ---\n{json.dumps(tool_registry, indent=2)}\n\n"
        f"--- CURRENT MEMORY REGISTRY ---\n{json.dumps(memory_registry, indent=2)}\n\n"
        f"--- ATTACHED ENVIRONMENT CODE ANALYSIS EVIDENCE ---\n{file_environmental_context}\n"
        f"--- CURRENT PLAN ---\n{current_plan}\n\n"
        f"--- ENCOUNTERED PROBLEMS & REQUEST FOR ADVICE ---\n{encountered_problems}"
    )

    api_args = adviser_profile["api_params"].copy()
    api_args["model"] = adviser_profile["model"]
    api_args["messages"] = [
        {"role": "system", "content": config.SYSTEM_PROMPTS["adviser"]},
        {"role": "user", "content": user_prompt}
    ]
    
    # --- DYNAMIC PAYLOAD CHECKER ---
    max_context = adviser_profile.get("context_window", config.MAX_CONTEXT_TOKENS)
    safe_budget = int(max_context * 0.90)
    
    payload_tokens = get_payload_tokens(api_args["messages"])
    
    if payload_tokens > safe_budget:
        return (f"SYSTEM ERROR: The data you sent to the Adviser is too massive! "
                f"Payload is {payload_tokens} tokens, but the safety limit is {safe_budget}. "
                f"Please drastically shorten your 'current_plan' and 'encountered_problems' before consulting the Adviser.")
    
    if config.VERBOSITY_MODE != "silent":
        sys.stderr.write(f"\n\033[93m[System: Sending prompt to Adviser (~{payload_tokens} estimated tokens)...\033[0m\n")
        
    try:
        # 4. Await the LLM response using the dedicated ADVISER client
        response = await adviser_client.chat.completions.create(**api_args)
        adviser_thinking, advice_text = extract_thinking_and_content(response.choices[0].message)
        
        # Log token usage with fallback
        tokens_in = response.usage.prompt_tokens if (response.usage and response.usage.prompt_tokens) else get_payload_tokens(api_args["messages"])
        tokens_out = response.usage.completion_tokens if (response.usage and response.usage.completion_tokens) else (len(tokenizer.encode(advice_text)) if tokenizer else len(advice_text) // 4)
        thinking_tokens = getattr(response.usage.completion_tokens_details, 'reasoning_tokens', 0) if response.usage and hasattr(response.usage, 'completion_tokens_details') and response.usage.completion_tokens_details else 0
        if thinking_tokens == 0 and adviser_thinking:
            thinking_tokens = len(adviser_thinking) // 4
        config.log_token_usage(STATE_DIR, "adviser", tokens_in, tokens_out, thinking_tokens)
        
        # 5. Format the filename to start with the date (e.g., 20260503_204530_advice.md)
        os.makedirs(REPORTS_DIR, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = f"{timestamp}_advice.md"
        filepath = os.path.join(REPORTS_DIR, filename)
        
        # 6. Save the physical document to the state/reports folder (with collapsible thinking for auditability)
        file_body = advice_text
        if adviser_thinking:
            file_body = f"<details>\n<summary>Adviser Reasoning & Strategy</summary>\n\n{adviser_thinking}\n\n</details>\n\n{advice_text}"
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(file_body)
            
        # Context guardrail: truncate preview for context window if advice exceeds 8,000 characters
        if len(advice_text) > 8000:
            preview = advice_text[:3000]
            display_feedback = (
                f"{preview}\n\n... [SYSTEM CONTEXT GUARDRAIL: The full Adviser report ({len(advice_text):,} chars) was saved to '{filepath}'. "
                f"A preview of the first 3,000 characters is shown above to protect your context window.\n"
                f"HOW TO GET AROUND THIS TRUNCATION: Inspect specific sections of the saved report using analyze_files(['{filepath}'], ...) "
                f"or bash tools: execute_bash('sed -n \\'100,200p\\' {filepath}') or 'grep'.]"
            )
        else:
            display_feedback = advice_text

        # 7. Return the text back to the Brain's context window with hidden sentinel tags
        result_msg = f"Adviser report successfully saved to disk at '{filepath}'.\n\n--- ADVISER FEEDBACK ---\n{display_feedback}"
        if adviser_thinking:
            result_msg += f"\n<___ADVISER_THOUGHTS___>\n{adviser_thinking}\n</___ADVISER_THOUGHTS___>"
        result_msg += f"\n<___ADVISER_REPORT___>\n{advice_text}\n</___ADVISER_REPORT___>"
        return result_msg
        
    except Exception as e:
        trace_file = save_failure_trace("adviser", {
            "subagent": "adviser",
            "error": str(e),
            "traceback": traceback.format_exc(),
            "messages": api_args.get("messages", []) if 'api_args' in locals() else []
        })
        trace_note = f"\n[SYSTEM: Failure trace saved to '{trace_file}']" if trace_file else ""
        return f"Failed to consult the adviser. Error: {str(e)}{trace_note}"


@mcp.tool()
async def fast_decision(
    instructions: str,
    decision_type: str = "choice",
    options: list[str] | dict[str, str] | None = None,
    state_text: str = "",
    context_filepaths: list[str] | None = None,
    image_filepaths: list[str] | None = None,
    custom_questions: dict[str, Any] | None = None
) -> str:
    """Queries The Decider (clef-flash decision model via Ollama /v1/systemone) for rapid non-autoregressive rulings.
    Use this tool when you need an immediate single-step judgment, branch routing, hypothesis check, or safety gate without multi-turn generation lag.

    Parameters:
    - instructions: The question or directive to evaluate (e.g. 'Which library is best suited for columnar data?', 'Is this bash execution safe?').
    - decision_type: 'choice' (categorical selection), 'boolean' (yes/no true/false check), or 'score' (ordinal rating/severity scale).
    - options:
        * For 'choice': list of option names ['A', 'B', 'C'] or dict of option names to descriptions {'opt_a': 'desc a', 'opt_b': 'desc b'}.
        * For 'boolean': optional dict mapping {'true': 'description', 'false': 'description'} or 2-item list.
        * For 'score': list of level descriptions from lowest to highest, e.g. ['Low', 'Medium', 'High', 'Critical'].
    - state_text: Immediate text, code, traceback, or situation to judge.
    - context_filepaths: Optional list of file paths to load into state via pointers without bloating chat history.
    - image_filepaths: Optional list of image paths to base64-encode and evaluate jointly with text.
    - custom_questions: Optional raw questions schema matching Ollama /v1/systemone for multi-question queries.
    """
    # 1. Assemble state context
    gathered_context = ""
    if context_filepaths:
        gathered_context = gather_agent_context(context_filepaths)

    state_parts = []
    if state_text and str(state_text).strip():
        state_parts.append(str(state_text).strip())
    if gathered_context and gathered_context.strip():
        state_parts.append(f"=== ATTACHED ENVIRONMENT / FILE CONTEXT ===\n{gathered_context.strip()}")

    final_state = "\n\n".join(state_parts)
    if not final_state.strip():
        final_state = instructions

    # 2. Process images if any
    images_payload = []
    if image_filepaths:
        allowed_roots = [d for d in [WORKSPACE_DIR, "/app/workspace", "/app/host_input", "/app"] if os.path.exists(d)]
        for img_path in image_filepaths:
            clean_path = os.path.realpath(str(img_path).strip())
            if allowed_roots and not any(os.path.commonpath([clean_path, d]) == d for d in allowed_roots):
                logger.warning(f"Blocked image read outside safe boundaries in fast_decision: {clean_path}")
                continue
            if os.path.exists(clean_path):
                try:
                    with open(clean_path, "rb") as img_f:
                        images_payload.append(base64.b64encode(img_f.read()).decode("utf-8"))
                except Exception as img_err:
                    logger.warning(f"Failed to read image {clean_path}: {img_err}")

    # 3. Formulate questions payload
    if custom_questions and isinstance(custom_questions, dict):
        questions_payload = custom_questions
    else:
        q_type = str(decision_type).lower().strip()
        if q_type in ["boolean", "bool", "noul"]:
            q_obj: dict[str, Any] = {"type": "noul", "instructions": instructions}
            if isinstance(options, dict) and ("true" in options or "false" in options):
                q_obj["criteria"] = {str(k): str(v) for k, v in options.items()}
            elif isinstance(options, list) and len(options) == 2:
                q_obj["criteria"] = {"true": str(options[0]), "false": str(options[1])}
            questions_payload = {"decision": q_obj}
        elif q_type in ["score", "rating"]:
            if isinstance(options, list) and options:
                criteria = [str(x) for x in options]
            elif isinstance(options, dict) and options:
                criteria = [str(v) for _, v in sorted(options.items())]
            else:
                criteria = ["Very Low", "Low", "Medium", "High", "Critical"]
            questions_payload = {
                "decision": {
                    "type": "score",
                    "instructions": instructions,
                    "criteria": criteria
                }
            }
        else:  # 'choice'
            if isinstance(options, list) and options:
                criteria = {str(opt): None for opt in options}
            elif isinstance(options, dict) and options:
                criteria = {str(k): (str(v) if v is not None else None) for k, v in options.items()}
            else:
                criteria = {"yes": None, "no": None}
            questions_payload = {
                "decision": {
                    "type": "choice",
                    "instructions": instructions,
                    "criteria": criteria
                }
            }

    # 4. Determine endpoint and model from config
    base_url = decider_config["base_url"].rstrip("/")
    endpoint = f"{base_url}/systemone" if base_url.endswith("/v1") else f"{base_url}/v1/systemone"
    model_name = decider_config["model"]
    timeout = decider_config["timeout"]

    req_body: dict[str, Any] = {
        "model": model_name,
        "state": final_state,
        "questions": questions_payload,
    }
    if images_payload:
        req_body["images"] = images_payload

    # 5. Dispatch HTTP request
    def _post_systemone(url: str, body: dict, to_sec: float) -> dict:
        req_data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=req_data,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=to_sec) as resp:
            resp_bytes = resp.read()
            return json.loads(resp_bytes.decode("utf-8"))

    try:
        try:
            resp_json = await asyncio.to_thread(_post_systemone, endpoint, req_body, timeout)
        except urllib.error.HTTPError as he:
            err_detail = he.read().decode("utf-8", errors="replace") if hasattr(he, "read") else str(he)
            raise RuntimeError(f"HTTP {he.code}: {err_detail}") from he
    except Exception as exc:
        trace_file = save_failure_trace("decider", {
            "subagent": "decider",
            "endpoint": endpoint,
            "error": str(exc),
            "traceback": traceback.format_exc(),
            "payload_summary": {
                "model": req_body.get("model"),
                "state_length": len(final_state),
                "questions": questions_payload
            }
        })
        trace_note = f" (failure trace saved to '{trace_file}')" if trace_file else ""
        return f"SYSTEM ERROR: The Decider failed to evaluate decision: {str(exc)}{trace_note}\nCheck that Ollama is running at '{base_url}' with '{model_name}'."

    # 6. Parse and log usage
    usage = resp_json.get("usage", {})
    in_tokens = usage.get("input_tokens", 0)
    out_tokens = usage.get("output_tokens", 0)
    if in_tokens > 0 or out_tokens > 0:
        config.log_token_usage(STATE_DIR, "decider", in_tokens, out_tokens, 0)
    logger.info(f"[fast_decision] Evaluated {len(questions_payload)} question(s) via '{model_name}' ({in_tokens} in, {out_tokens} out)")

    # 7. Format clean report for the Brain and subagent logger
    active_model = resp_json.get("model", model_name)
    answers = resp_json.get("answers", {})
    output_lines = []

    for q_name, q_res in answers.items():
        ans_type = q_res.get("type", "")
        if ans_type == "choice":
            choice_val = q_res.get("choice", "N/A")
            conf = q_res.get("confidence", 0.0)
            probs = q_res.get("probabilities", {})
            output_lines.append(f"- **Question [{q_name}] Verdict:** `{choice_val}` (Confidence: {conf:.1%})")
            if probs:
                prob_parts = [f"`{k}`: {v:.1%}" for k, v in probs.items()]
                output_lines.append(f"  *Probabilities:* {', '.join(prob_parts)}")
        elif ans_type == "noul":
            prob_true = q_res.get("noul", 0.0)
            verdict = "TRUE" if prob_true >= 0.5 else "FALSE"
            output_lines.append(f"- **Question [{q_name}] Verdict:** `{verdict}` (Probability True: {prob_true:.1%})")
        elif ans_type == "score":
            score_val = q_res.get("score", 0.0)
            conf = q_res.get("confidence", 0.0)
            legend = q_res.get("legend", {})
            probs = q_res.get("probabilities", {})
            argmax_level = None
            if probs:
                try:
                    argmax_level = max(probs.items(), key=lambda x: x[1])[0]
                except Exception:
                    pass
            label_suffix = ""
            if argmax_level is not None:
                level_name = legend.get(argmax_level, f"Level {argmax_level}") if legend else f"Level {argmax_level}"
                label_suffix = f" → **[{level_name}]**"
            output_lines.append(f"- **Question [{q_name}] Rating:** `{score_val:.2f}`{label_suffix} (Confidence: {conf:.1%})")
            if legend and probs:
                prob_parts = [f"Level {lvl} ({legend.get(lvl, lvl)}): {prob:.1%}" for lvl, prob in probs.items()]
                output_lines.append(f"  *Levels:* {', '.join(prob_parts)}")
        else:
            output_lines.append(f"- **Question [{q_name}]:** {json.dumps(q_res)}")

    if in_tokens or out_tokens:
        output_lines.append(f"\n*(Tokens: {in_tokens} input, {out_tokens} output)*")

    report_content = "\n".join(output_lines)
    result_msg = f"The Decider completed ruling:\n{report_content}"
    result_msg += f"\n<___DECIDER_RULING___>\n{report_content}\n</___DECIDER_RULING___>"
    return result_msg


def resolve_auxiliary_endpoint(endpoint_name_or_url: str = None, api_key: str = None, timeout_seconds: float = 300.0) -> tuple[str, str, float]:
    """Resolves an endpoint name or URL to a concrete base_url, API key, and clamped timeout."""
    presets = getattr(config, "AUXILIARY_ENDPOINTS", {})
    default_name = getattr(config, "DEFAULT_AUXILIARY_ENDPOINT", "ollama")
    
    target_url = None
    target_key = api_key
    
    if not endpoint_name_or_url:
        preset = presets.get(default_name) or presets.get("ollama", {})
        target_url = preset.get("url", "http://host.containers.internal:64165/v1")
        if not target_key:
            target_key = preset.get("default_api_key", "Ollama")
    elif endpoint_name_or_url in presets:
        preset = presets[endpoint_name_or_url]
        target_url = preset.get("url")
        if not target_key:
            target_key = preset.get("default_api_key", "sk-sandbox-fake-key")
    else:
        # Treat as custom URL
        raw_url = endpoint_name_or_url.strip()
        try:
            parsed = urllib.parse.urlparse(raw_url)
            netloc = parsed.netloc
            if netloc.startswith("localhost:") or netloc == "localhost":
                netloc = netloc.replace("localhost", "host.containers.internal", 1)
            elif netloc.startswith("127.0.0.1:") or netloc == "127.0.0.1":
                netloc = netloc.replace("127.0.0.1", "host.containers.internal", 1)
            target_url = urllib.parse.urlunparse(parsed._replace(netloc=netloc))
        except Exception:
            target_url = raw_url
        if not target_key:
            target_key = presets.get(default_name, {}).get("default_api_key", "sk-sandbox-fake-key")
            
    # Clamp timeout between 1.0 second and 3600.0 seconds (up to 1 hour)
    try:
        t_val = float(timeout_seconds) if timeout_seconds is not None else 300.0
    except (ValueError, TypeError):
        t_val = 300.0
    timeout_val = min(max(1.0, t_val), 3600.0)
    
    return target_url, target_key or "sk-sandbox-fake-key", timeout_val


@mcp.tool()
async def query_auxiliary_llm(
    action: str,
    endpoint: str = None,
    model: str = None,
    system_prompt: str = "You are a helpful AI assistant.",
    user_prompt: str = "",
    api_key: str = None,
    temperature: float = 0.7,
    top_p: float = 1.0,
    max_tokens: int = 32768,
    timeout_seconds: float = 300.0,
    reasoning_effort: str = None,
    response_format: str = None,
    presence_penalty: float = 0.0,
    frequency_penalty: float = 0.0,
    stop: list[str] = None,
    seed: int = None,
    extra_body: dict = None,
    extra_headers: dict = None,
    context_filepaths: list[str] = None,
    media_filepaths: list[str] = None,
    output_filepath: str = None
) -> str:
    """Auxiliary backup tool to query an external LLM endpoint for experiments, second opinions, or cloud models via LiteLLM.
    Use ONLY when primary specialized sub-agents (Coder, Analyst, Adviser, Decider, Architect) cannot perform the task.

    Actions:
    - 'list_endpoints': Lists pre-configured endpoint presets from llm_config.
    - 'list_models': Lists available models on 'endpoint'.
    - 'chat': Sends prompt to 'model' on 'endpoint' with optional parameters (temperature, max_tokens, reasoning_effort, context_filepaths, media_filepaths, output_filepath, etc.).
    """
    if action == "list_endpoints":
        presets = getattr(config, "AUXILIARY_ENDPOINTS", {})
        default_name = getattr(config, "DEFAULT_AUXILIARY_ENDPOINT", "ollama")
        lines = ["--- PRE-CONFIGURED AUXILIARY LLM ENDPOINTS ---"]
        for ep_name, ep_data in presets.items():
            mark = " (DEFAULT)" if ep_name == default_name else ""
            lines.append(f"• '{ep_name}'{mark}: {ep_data.get('url')} - {ep_data.get('description', '')}")
        lines.append("\nTip: Pass any preset name (or custom URL) to 'endpoint' in query_auxiliary_llm.")
        return "\n".join(lines)

    elif action == "list_models":
        target_url, target_key, timeout_val = resolve_auxiliary_endpoint(endpoint, api_key, timeout_seconds)
        try:
            client = AsyncOpenAI(base_url=target_url, api_key=target_key, timeout=timeout_val)
            models_response = await client.models.list()
            model_names = [m.id for m in models_response.data]
            return f"--- AVAILABLE MODELS AT '{target_url}' ---\n" + "\n".join(model_names)
        except Exception as e:
            presets = getattr(config, "AUXILIARY_ENDPOINTS", {})
            available_presets = ", ".join(presets.keys())
            return (
                f"Failed to fetch model list from '{target_url}'. Error: {str(e)}\n"
                f"Available endpoint presets: {available_presets}. Use action='list_endpoints' for details."
            )

    elif action == "chat":
        if not model or not user_prompt:
            return "Error: You must provide a 'model' name and a 'user_prompt' to use the chat action."

        resolved_out = None
        if output_filepath:
            candidate_out = output_filepath if os.path.isabs(output_filepath) else os.path.join(WORKSPACE_DIR, output_filepath.lstrip("/"))
            resolved_out = os.path.realpath(candidate_out)
            allowed_out_dirs = [os.path.realpath(OUTPUTS_DIR), os.path.realpath(SANDBOX_DIR)]
            if not any(os.path.commonpath([resolved_out, d]) == d for d in allowed_out_dirs):
                return f"SYSTEM ERROR: output_filepath '{output_filepath}' must be inside {OUTPUTS_DIR} or {SANDBOX_DIR}."
            os.makedirs(os.path.dirname(resolved_out), exist_ok=True)

        target_url, target_key, timeout_val = resolve_auxiliary_endpoint(endpoint, api_key, timeout_seconds)

        effective_model = model.strip() if model else ""

        # Extract background context files quietly
        file_environmental_context = gather_agent_context(context_filepaths)
        final_user_payload = f"{file_environmental_context}{user_prompt}"

        # Build multimodal content if media files attached
        user_message_content, _ = build_multimodal_content(final_user_payload, media_filepaths)

        api_args = {
            "model": effective_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message_content}
            ],
            "temperature": float(temperature),
            "top_p": float(top_p),
            "max_tokens": int(max_tokens)
        }

        if presence_penalty != 0.0:
            api_args["presence_penalty"] = float(presence_penalty)
        if frequency_penalty != 0.0:
            api_args["frequency_penalty"] = float(frequency_penalty)
        if stop:
            api_args["stop"] = stop
        if seed is not None:
            api_args["seed"] = int(seed)

        if response_format:
            if isinstance(response_format, str) and response_format.lower() in ["json", "json_object"]:
                api_args["response_format"] = {"type": "json_object"}
            elif isinstance(response_format, dict):
                api_args["response_format"] = response_format

        body_kwargs = {}
        if reasoning_effort:
            body_kwargs["reasoning_effort"] = reasoning_effort
        if extra_body and isinstance(extra_body, dict):
            body_kwargs.update(extra_body)
        if body_kwargs:
            api_args["extra_body"] = body_kwargs

        if extra_headers and isinstance(extra_headers, dict):
            api_args["extra_headers"] = extra_headers

        safe_budget = int(config.MAX_CONTEXT_TOKENS * 0.90) 
        payload_tokens = get_payload_tokens(api_args["messages"])

        if payload_tokens > safe_budget:
            return (f"SYSTEM ERROR: The prompt and attached context files sent to the Auxiliary LLM are too massive! "
                    f"Your payload is {payload_tokens} tokens, but the safety limit is {safe_budget}. "
                    f"Please refine your 'context_filepaths' selection.")

        if config.VERBOSITY_MODE != "silent":
            sys.stderr.write(f"\n\033[93m[System: Sending prompt to Auxiliary LLM ({effective_model} at {target_url}) (~{payload_tokens} estimated tokens, timeout: {timeout_val:.0f}s)...\033[0m\n")

        try:
            client = AsyncOpenAI(base_url=target_url, api_key=target_key, timeout=timeout_val)
            response = await client.chat.completions.create(**api_args)
            thinking, content = extract_thinking_and_content(response.choices[0].message)

            # Log token usage with fallback
            tokens_in = response.usage.prompt_tokens if (response.usage and response.usage.prompt_tokens) else get_payload_tokens(api_args["messages"])
            tokens_out = response.usage.completion_tokens if (response.usage and response.usage.completion_tokens) else (len(tokenizer.encode(content)) if tokenizer else len(content) // 4)
            thinking_tokens = getattr(response.usage.completion_tokens_details, 'reasoning_tokens', 0) if response.usage and hasattr(response.usage, 'completion_tokens_details') and response.usage.completion_tokens_details else 0
            if thinking_tokens == 0 and thinking:
                thinking_tokens = len(thinking) // 4
            config.log_token_usage(STATE_DIR, "auxiliary", tokens_in, tokens_out, thinking_tokens)

            finish_reason = response.choices[0].finish_reason or "unknown"

            saved_note = ""
            display_body = content
            if resolved_out:
                saved_text = content if content.strip() else thinking
                with open(resolved_out, "w", encoding="utf-8") as f:
                    f.write(saved_text)
                saved_note = f"[Saved full response to: {resolved_out} ({len(saved_text):,} chars)]\n"
                if len(content) > 2000:
                    display_body = content[:1500] + f"\n\n... [Truncated inline preview; full {len(content):,}-char response saved to {resolved_out}]"

            result_str = f"--- RESPONSE FROM {effective_model} ({target_url}) ---\n{saved_note}"
            if thinking and not resolved_out:
                result_str += f"<thinking>\n{thinking}\n</thinking>\n\n"
            result_str += display_body

            if not content.strip() and not thinking:
                result_str += f"\n[SYSTEM WARNING: Empty response. Finish Reason: '{finish_reason}']"
            elif finish_reason == "length":
                result_str += f"\n\n[SYSTEM NOTICE: Model reached max_tokens limit ({max_tokens}) during reasoning/generation. Output was truncated by the model, but all thinking and partial output are fully preserved above.]"

            if thinking:
                result_str += f"\n<___AUXILIARY_THOUGHTS___>\n{thinking}\n</___AUXILIARY_THOUGHTS___>"
            if content:
                result_str += f"\n<___AUXILIARY_OUTPUT___ model=\"{effective_model}\">\n{content}\n</___AUXILIARY_OUTPUT___>"

            return result_str

        except Exception as e:
            trace_file = save_failure_trace("auxiliary", {
                "subagent": "auxiliary",
                "endpoint": target_url,
                "model": effective_model,
                "error": str(e),
                "traceback": traceback.format_exc(),
                "messages": api_args.get("messages", []) if 'api_args' in locals() else []
            })
            trace_note = f"\n[SYSTEM: Failure trace saved to '{trace_file}']" if trace_file else ""
            return f"LLM Query Failed on '{target_url}'. Error: {str(e)}{trace_note}"

    else:
        return "Error: Invalid action. Must be 'list_endpoints', 'list_models', or 'chat'."


@mcp.tool()
async def execute_rest_call(
    url: str,
    method: str = "POST",
    request_filepath: str = None,
    payload_json: dict = None,
    response_filepath: str = None,
    headers: dict = None,
    extract_field: str = None,
    timeout_seconds: int = 180,
    files: dict[str, str] = None,
    media_filepaths: list[str] = None
) -> str:
    """Auxiliary escape hatch: Dispatches an arbitrary HTTP REST call to an external API, microservice, or local model.
    Use ONLY for ad-hoc HTTP queries or custom endpoints, NOT for routine agent orchestration.
    Loopback addresses ('localhost', '127.0.0.1') are automatically mapped to 'host.containers.internal'.
    Supports JSON payloads ('payload_json', 'request_filepath'), multipart file uploads ('files'), and media ('media_filepaths').
    """
    if not url or not isinstance(url, str):
        return "SYSTEM ERROR: The 'url' parameter must be a non-empty string."

    method = method.strip().upper() if method and isinstance(method, str) else "POST"
    
    # Auto-resolve localhost/127.0.0.1 to host.containers.internal for Podman container networking
    try:
        parsed_url = urllib.parse.urlparse(url.strip())
        if parsed_url.port == 21049 and (parsed_url.hostname or "").lower() in ("localhost", "127.0.0.1", "host.containers.internal", "host.wsl.internal", "host.docker.internal"):
            return "SYSTEM ERROR: Direct REST calls to the unauthenticated ChimeraX port (21049) are blocked for security. Use the 'cx_*' MCP tools via the ChimeraX FastMCP bridge instead."
        netloc = parsed_url.netloc
        if netloc.startswith("localhost:") or netloc == "localhost":
            netloc = netloc.replace("localhost", "host.containers.internal", 1)
        elif netloc.startswith("127.0.0.1:") or netloc == "127.0.0.1":
            netloc = netloc.replace("127.0.0.1", "host.containers.internal", 1)
        clean_url = urllib.parse.urlunparse(parsed_url._replace(netloc=netloc))
    except Exception:
        clean_url = url.strip()

    rest_calls_dir = os.path.join(STATE_DIR, "rest_calls")
    os.makedirs(rest_calls_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    req_archive_path = os.path.join(rest_calls_dir, f"req_{timestamp}.json")

    allowed_roots = [d for d in [WORKSPACE_DIR, "/app/workspace", "/app/host_input", "/app"] if os.path.exists(d)]
    opened_files = {}
    if files and isinstance(files, dict):
        for field_name, fpath in files.items():
            if not fpath or not isinstance(fpath, str):
                continue
            resolved_f = os.path.realpath(fpath.strip())
            if not os.path.exists(resolved_f):
                alt_f = os.path.realpath(os.path.join(WORKSPACE_DIR, fpath.strip().lstrip("/")))
                if os.path.exists(alt_f):
                    resolved_f = alt_f
                else:
                    return f"SYSTEM ERROR: Upload file '{fpath}' for field '{field_name}' not found on disk."
            if allowed_roots and not any(os.path.commonpath([resolved_f, d]) == d for d in allowed_roots):
                return f"SYSTEM ERROR: Upload file '{fpath}' falls outside safe workspace boundaries."
            mime_t, _ = mimetypes.guess_type(resolved_f)
            fname = os.path.basename(resolved_f)
            try:
                fp = open(resolved_f, "rb")
                opened_files[field_name] = (fname, fp, mime_t or "application/octet-stream")
            except Exception as fe:
                return f"SYSTEM ERROR: Could not open upload file '{fpath}': {str(fe)}"

    data_payload = None
    if request_filepath:
        resolved_req = os.path.realpath(request_filepath)
        if not os.path.exists(resolved_req):
            alt_path = os.path.realpath(os.path.join(WORKSPACE_DIR, request_filepath.lstrip("/")))
            if os.path.exists(alt_path):
                resolved_req = alt_path
            else:
                for file_entry in opened_files.values():
                    try: file_entry[1].close()
                    except Exception: pass
                return f"SYSTEM ERROR: Request file '{request_filepath}' not found on disk."
        if allowed_roots and not any(os.path.commonpath([resolved_req, d]) == d for d in allowed_roots):
            for file_entry in opened_files.values():
                try: file_entry[1].close()
                except Exception: pass
            return f"SYSTEM ERROR: Request file '{request_filepath}' falls outside safe workspace boundaries."
        try:
            with open(resolved_req, "r", encoding="utf-8") as f:
                file_content = f.read()
            try:
                data_payload = json.loads(file_content)
            except Exception:
                data_payload = file_content
            with open(req_archive_path, "w", encoding="utf-8") as f:
                f.write(file_content if isinstance(data_payload, str) else json.dumps(data_payload, indent=2))
        except Exception as e:
            for file_entry in opened_files.values():
                try: file_entry[1].close()
                except Exception: pass
            return f"SYSTEM ERROR: Failed reading request file '{request_filepath}': {str(e)}"
    elif payload_json is not None:
        data_payload = payload_json
        try:
            with open(req_archive_path, "w", encoding="utf-8") as f:
                json.dump(data_payload, f, indent=2)
        except Exception:
            pass
    elif method in ["POST", "PUT", "PATCH"]:
        if not opened_files:
            return f"SYSTEM ERROR: HTTP {method} requires either 'request_filepath', 'payload_json', or 'files'."

    # If media_filepaths is specified for chat completions, inject multimodal blocks
    if media_filepaths and isinstance(data_payload, dict) and "messages" in data_payload and isinstance(data_payload["messages"], list):
        for msg in reversed(data_payload["messages"]):
            if isinstance(msg, dict) and msg.get("role") == "user":
                orig_content = msg.get("content", "")
                if isinstance(orig_content, str):
                    multimodal_content, _ = build_multimodal_content(orig_content, media_filepaths)
                    msg["content"] = multimodal_content
                elif isinstance(orig_content, list):
                    blocks, _ = build_multimodal_content("", media_filepaths)
                    if isinstance(blocks, list):
                        msg["content"] = orig_content + [b for b in blocks if b.get("type") != "text" or b.get("text")]
                break

    req_headers = {"User-Agent": "Sandbox-Overseer/1.0"}
    if data_payload is not None and not opened_files:
        req_headers["Content-Type"] = "application/json"
    if headers and isinstance(headers, dict):
        req_headers.update(headers)

    if response_filepath:
        resp_dest_path = os.path.abspath(response_filepath)
        if not resp_dest_path.startswith(WORKSPACE_DIR):
            resp_dest_path = os.path.abspath(os.path.join(WORKSPACE_DIR, response_filepath.lstrip("/")))
        os.makedirs(os.path.dirname(resp_dest_path), exist_ok=True)
    else:
        resp_dest_path = os.path.join(rest_calls_dir, f"resp_{timestamp}.json")
    latest_resp_path = os.path.join(rest_calls_dir, "latest_response.json")

    timeout_val = float(timeout_seconds) if timeout_seconds else 180.0
    start_time = time.perf_counter()

    try:
        async with httpx.AsyncClient(timeout=timeout_val, follow_redirects=True) as client:
            req_kwargs = {"headers": req_headers}
            if opened_files:
                req_kwargs["files"] = opened_files
                if data_payload is not None and isinstance(data_payload, dict):
                    req_kwargs["data"] = data_payload
            elif data_payload is not None:
                if isinstance(data_payload, (dict, list)):
                    req_kwargs["json"] = data_payload
                else:
                    req_kwargs["content"] = str(data_payload).encode("utf-8")

            response = await client.request(method, clean_url, **req_kwargs)
            elapsed_ms = int((time.perf_counter() - start_time) * 1000)
    except httpx.TimeoutException:
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        return f"SYSTEM ERROR: REST request to '{clean_url}' timed out after {elapsed_ms}ms (limit: {timeout_val}s)."
    except httpx.ConnectError as ce:
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        return (
            f"SYSTEM ERROR: Connection to '{clean_url}' failed after {elapsed_ms}ms ({str(ce)}).\n"
            f"Tip: If connecting to a service on the host machine, verify the port is open and reachable via host.containers.internal."
        )
    except Exception as req_err:
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        return f"SYSTEM ERROR: REST request to '{clean_url}' failed after {elapsed_ms}ms: {str(req_err)}"
    finally:
        for file_entry in opened_files.values():
            try:
                file_entry[1].close()
            except Exception:
                pass

    status_code = response.status_code
    reason_phrase = response.reason_phrase or "OK"
    resp_text = response.text

    is_json = False
    resp_json = None
    try:
        resp_json = response.json()
        is_json = True
    except Exception:
        pass

    try:
        with open(resp_dest_path, "w", encoding="utf-8") as f:
            if is_json:
                json.dump(resp_json, f, indent=2)
            else:
                f.write(resp_text)
        with open(latest_resp_path, "w", encoding="utf-8") as f:
            if is_json:
                json.dump(resp_json, f, indent=2)
            else:
                f.write(resp_text)
    except Exception as save_err:
        logger.warning(f"[execute_rest_call] Failed saving response file: {save_err}")

    extracted_content = ""
    extracted_thinking = ""
    token_info = ""

    if is_json and isinstance(resp_json, dict):
        if extract_field:
            cur = resp_json
            for key in extract_field.strip().split("."):
                if isinstance(cur, list) and key.isdigit():
                    idx = int(key)
                    cur = cur[idx] if idx < len(cur) else None
                elif isinstance(cur, dict):
                    cur = cur.get(key)
                else:
                    cur = None
                    break
            if cur is not None:
                extracted_content = str(cur) if not isinstance(cur, str) else cur

        if not extracted_content:
            if "choices" in resp_json and isinstance(resp_json["choices"], list) and len(resp_json["choices"]) > 0:
                msg = resp_json["choices"][0].get("message", {})
                extracted_content = msg.get("content", "")
                extracted_thinking = msg.get("reasoning_content") or ""
            elif "response" in resp_json:
                extracted_content = str(resp_json["response"])
            elif "message" in resp_json and isinstance(resp_json["message"], dict):
                extracted_content = resp_json["message"].get("content", "")
            elif "content" in resp_json and isinstance(resp_json["content"], list) and len(resp_json["content"]) > 0:
                extracted_content = resp_json["content"][0].get("text", "")
            elif "candidates" in resp_json and isinstance(resp_json["candidates"], list) and len(resp_json["candidates"]) > 0:
                cand = resp_json["candidates"][0]
                parts = cand.get("content", {}).get("parts", [])
                if parts and isinstance(parts, list):
                    extracted_content = parts[0].get("text", "")

        usage = resp_json.get("usage") or {}
        t_in = usage.get("prompt_tokens") or resp_json.get("prompt_eval_count") or 0
        t_out = usage.get("completion_tokens") or resp_json.get("eval_count") or 0
        t_think = getattr(usage, "completion_tokens_details", {}).get("reasoning_tokens", 0) if isinstance(usage, dict) else 0
        if t_in > 0 or t_out > 0:
            config.log_token_usage(STATE_DIR, "rest_call", t_in, t_out, t_think)
            token_info = f"\nTokens: {t_in} prompt, {t_out} completion"
            if t_think:
                token_info += f", {t_think} reasoning"

    res_str = (
        f"=== REST CALL: {status_code} {reason_phrase} ({elapsed_ms}ms) ===\n"
        f"URL: {clean_url}\n"
        f"Request File: {req_archive_path}\n"
        f"Response File: {resp_dest_path} ({len(resp_text)} bytes){token_info}\n"
    )
    if extracted_thinking:
        res_str += f"\n<thinking>\n{extracted_thinking}\n</thinking>\n"
    if extracted_content:
        res_str += f"\n--- EXTRACTED CONTENT ---\n{extracted_content}\n"
    else:
        preview = resp_text[:1500] + ("\n... [truncated, full response saved to file]" if len(resp_text) > 1500 else "")
        res_str += f"\n--- RESPONSE BODY PREVIEW ---\n{preview}\n"
    return res_str


def _reconcile_bg_jobs() -> dict:
    """Reconciles state/jobs/bg_jobs.json against live in-container processes and asyncio tasks."""
    def _mutator(data):
        now_ts = time.time()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for jid, jinfo in data.get("jobs", {}).items():
            if jinfo.get("status") != "running":
                continue
            if jid in _ACTIVE_BG_TASKS:
                active_entry = _ACTIVE_BG_TASKS[jid]
                proc = active_entry.get("process")
                if proc is not None and proc.returncode is not None:
                    jinfo["status"] = "completed" if proc.returncode == 0 else "failed"
                    jinfo["exit_code"] = proc.returncode
                    jinfo["end_time"] = now_str
                    jinfo["duration_seconds"] = round(now_ts - float(jinfo.get("start_epoch", now_ts)), 2)
                continue
            # Not in _ACTIVE_BG_TASKS (e.g. checked across restart)
            if jinfo.get("type") == "bash":
                pid = jinfo.get("pid")
                alive = False
                if pid:
                    try:
                        os.kill(int(pid), 0)
                        alive = True
                    except Exception:
                        alive = False
                if not alive:
                    jinfo["status"] = "completed" if jinfo.get("exit_code", 0) == 0 else "failed"
                    jinfo.setdefault("exit_code", 0)
                    jinfo["end_time"] = now_str
                    jinfo["duration_seconds"] = round(now_ts - float(jinfo.get("start_epoch", now_ts)), 2)
            else:
                jinfo["status"] = "failed"
                jinfo["error"] = "Background sub-agent interrupted due to container restart."
                jinfo["end_time"] = now_str
                jinfo["duration_seconds"] = round(now_ts - float(jinfo.get("start_epoch", now_ts)), 2)
        return data

    return config.update_bg_jobs(STATE_DIR, _mutator)


def _read_log_tail(filepath: str, max_lines: int = 40, max_chars: int = 4000) -> str:
    """Reads the last max_lines / max_chars from a background job log or trace file."""
    if not filepath or not os.path.exists(filepath):
        return "(no log output yet)"
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        if not lines:
            return "(empty log)"
        tail = "".join(lines[-max_lines:])
        if len(tail) > max_chars:
            tail = "... [truncated] ...\n" + tail[-max_chars:]
        return tail.strip() or "(empty log)"
    except Exception as e:
        return f"(failed reading log: {e})"


async def _monitor_bash_job(job_id: str, process, log_fp, log_path: str, start_epoch: float, timeout_val: float):
    """Monitors a background bash subprocess until completion, timeout, or kill."""
    final_status = "completed"
    exit_code = 0
    footer_note = ""
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout_val)
        exit_code = process.returncode if process.returncode is not None else 0
        final_status = "completed" if exit_code == 0 else "failed"
        footer_note = f"\n[SYSTEM: Background job {job_id} finished with Exit Code {exit_code}]\n"
    except asyncio.TimeoutError:
        final_status = "timed_out"
        exit_code = -9
        footer_note = f"\n[SYSTEM: Background job {job_id} timed out after {timeout_val:.0f}s and was terminated]\n"
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
        try:
            await process.wait()
        except Exception:
            pass
    except asyncio.CancelledError:
        final_status = "killed"
        exit_code = -15
        footer_note = f"\n[SYSTEM: Background job {job_id} was killed]\n"
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
        try:
            await process.wait()
        except Exception:
            pass
    except Exception as e:
        final_status = "failed"
        exit_code = -1
        footer_note = f"\n[SYSTEM: Background job {job_id} monitor error: {e}]\n"
    finally:
        dur = round(time.time() - start_epoch, 2)
        end_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            log_fp.write(footer_note)
            log_fp.flush()
            log_fp.close()
        except Exception:
            pass
        _ACTIVE_BG_TASKS.pop(job_id, None)

        def _finish_mutator(data):
            j = data.get("jobs", {}).get(job_id)
            if j:
                # Preserve 'killed' if explicitly killed by manage_jobs
                if j.get("status") != "killed":
                    j["status"] = final_status
                j["exit_code"] = exit_code
                j["duration_seconds"] = dur
                j["end_time"] = end_str
            return data

        config.update_bg_jobs(STATE_DIR, _finish_mutator)
        logger.info(f"[bg_bash] {job_id} finished: status={final_status}, rc={exit_code}, duration={dur}s")


@mcp.tool()
async def execute_bash(command: str, timeout_seconds: int = 180, background: bool = False) -> str:
    """Executes a bash command inside the isolated container (working directory defaults to '/app/workspace/sandbox'). 
    - 'timeout_seconds': Defaults to 180 in foreground (up to 600), or defaults to 3600 (60 min, up to 36000 / 10h) when background=True.
    - 'background': Defaults to False (synchronous). Set to True ONLY for long-running processes (>30-60s) or background servers/daemons while you perform parallel work (max 9 concurrent background bash jobs).
    Do NOT use destructive commands! Move files to the archive folder instead.
    DO NOT use this to author scripts or execute multi-line inline programs (use create_tool instead)."""
    
    # 1. Catch empty inputs or incorrect types immediately
    if not command or not isinstance(command, str):
        return 'SYSTEM ERROR: The "command" parameter must be a non-empty string. Example: command="ls -la"'

    # 2. Block Destructive Commands (rm, unlink, -delete, git rm without --cached, variable indirection)
    # Strip inline comments to avoid false-triggering on comment annotations
    cmd_no_comments = re.sub(r'(?m)(^|\s+)#[^"\'\n]*$', '', command)

    # Strip quoted string literals to inspect active unquoted shell syntax
    unquoted = re.sub(r'"[^"\\]*(?:\\.[^"\\]*)*"', '""', cmd_no_comments)
    unquoted = re.sub(r"'[^'\\]*(?:\\.[^'\\]*)*'", "''", unquoted)

    # Check for find -delete or find -exec rm / xargs rm
    if re.search(r'\bfind\b[^;&|\n]*\s+-delete\b', unquoted, re.IGNORECASE) or \
       re.search(r'\bfind\b[^;&|\n]*\s+-exec(?:dir)?\s+(?:/bin/|/usr/bin/)?(?:rm|unlink|shred|truncate)\b', unquoted, re.IGNORECASE) or \
       re.search(r'\bxargs\b[^;&|\n]*\s+(?:/bin/|/usr/bin/)?(?:rm|unlink|shred|truncate)\b', unquoted, re.IGNORECASE):
        logger.warning(f"[execute_bash] Security block: destructive find/xargs deletion rejected: {command}")
        return "SYSTEM ERROR: Destructive commands (rm, unlink, shred, truncate, -delete, git rm without --cached) are blocked by Anti-Deletion Protocol. Move files to /app/workspace/archive instead."

    # Check unquoted variable indirection assigning to rm / unlink / shred / truncate (e.g., R=rm, CMD=/bin/unlink)
    if re.search(r'\b[a-zA-Z_][a-zA-Z0-9_]*\s*=\s*(?:/bin/|/usr/bin/)?(?:rm|unlink|shred|truncate)\b', unquoted, re.IGNORECASE):
        logger.warning(f"[execute_bash] Security block: unquoted variable indirection to rm/unlink rejected: {command}")
        return "SYSTEM ERROR: Destructive commands (rm, unlink, shred, truncate, -delete, git rm without --cached) are blocked by Anti-Deletion Protocol. Move files to /app/workspace/archive instead."

    # Tokenize statements by unquoted shell operators (; && || | \n &)
    statements = re.findall(r'(?:[^;&|\n"\']+|"[^"\\]*(?:\\.[^"\\]*)*"|\'[^\'\\]*(?:\\.[^\'\\]*)*\')+', command)
    for raw_stmt in statements:
        raw_stmt = raw_stmt.strip()
        if not raw_stmt or raw_stmt.startswith('#'):
            continue
        try:
            tokens = shlex.split(raw_stmt, comments=True)
        except Exception:
            tokens = raw_stmt.split()
        if not tokens:
            continue

        idx = 0
        while idx < len(tokens):
            tok = tokens[idx]
            # Check for variable assignment indirection (e.g., R="rm", CMD='unlink')
            if re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*=(?:/bin/|/usr/bin/)?(?:rm|unlink|shred|truncate)$', tok, re.IGNORECASE):
                logger.warning(f"[execute_bash] Security block: variable indirection assignment rejected: {command}")
                return "SYSTEM ERROR: Destructive commands (rm, unlink, shred, truncate, -delete, git rm without --cached) are blocked by Anti-Deletion Protocol. Move files to /app/workspace/archive instead."

            # Skip benign variable assignments (e.g., VAR=hello cmd)
            if re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*=', tok):
                idx += 1
                continue

            # Skip command prefixes
            if tok in ('export', 'env', 'command', 'builtin', 'sudo'):
                idx += 1
                continue

            base_token = tok.split('/')[-1].lower()

            # git rm check: only permit non-destructive index untracking with --cached
            if base_token == 'git' and idx + 1 < len(tokens):
                if tokens[idx + 1].lower() == 'rm':
                    if '--cached' not in [t.lower() for t in tokens]:
                        logger.warning(f"[execute_bash] Security block: git rm without --cached rejected: {command}")
                        return "SYSTEM ERROR: Destructive commands (rm, unlink, shred, truncate, -delete, git rm without --cached) are blocked by Anti-Deletion Protocol. Move files to /app/workspace/archive instead."
                    break

            # Reject rm, unlink, shred, truncate as executable command binaries
            if base_token in ('rm', 'unlink', 'shred', 'truncate'):
                logger.warning(f"[execute_bash] Security block: destructive binary '{base_token}' rejected: {command}")
                return "SYSTEM ERROR: Destructive commands (rm, unlink, shred, truncate, -delete, git rm without --cached) are blocked by Anti-Deletion Protocol. Move files to /app/workspace/archive instead."

            break

    try:
        if not command.strip():
            logger.warning("[execute_bash] Empty command payload rejected")
            return "SYSTEM ERROR: Empty command payload."

        current_env = os.environ.copy()
        current_env["PYTHONPATH"] = f"/app/workspace/custom_packages:{current_env.get('PYTHONPATH', '')}"

        # Prepend runtime bash interceptor functions for rm, unlink, and truncate to guarantee defense-in-depth against variable indirection
        safe_bash_payload = (
            "rm() { echo 'SYSTEM ERROR: Destructive commands (rm, unlink, shred, truncate, -delete, git rm without --cached) are blocked by Anti-Deletion Protocol. Move files to /app/workspace/archive instead.' >&2; return 1; }; "
            "export -f rm 2>/dev/null || true; "
            "unlink() { echo 'SYSTEM ERROR: Destructive commands (rm, unlink, shred, truncate, -delete, git rm without --cached) are blocked by Anti-Deletion Protocol. Move files to /app/workspace/archive instead.' >&2; return 1; }; "
            "export -f unlink 2>/dev/null || true; "
            "truncate() { echo 'SYSTEM ERROR: Destructive commands (rm, unlink, shred, truncate, -delete, git rm without --cached) are blocked by Anti-Deletion Protocol. Move files to /app/workspace/archive instead.' >&2; return 1; }; "
            "export -f truncate 2>/dev/null || true; "
            + command
        )

        # --- BACKGROUND EXECUTION BRANCH ---
        if background:
            _reconcile_bg_jobs()
            max_jobs = getattr(config, "MAX_BACKGROUND_JOBS", 9)
            default_bg_timeout = float(getattr(config, "BG_JOB_DEFAULT_TIMEOUT", 3600))
            max_bg_timeout = float(getattr(config, "BG_JOB_MAX_TIMEOUT", 36000))
            bg_state = config.read_bg_jobs(STATE_DIR)
            running_bash_jobs = [
                jid for jid, j in bg_state.get("jobs", {}).items()
                if j.get("status") == "running" and j.get("type", "bash") == "bash"
            ]
            if len(running_bash_jobs) >= max_jobs:
                return (
                    f"SYSTEM ERROR: Maximum concurrent background bash jobs limit ({max_jobs}) reached "
                    f"(Active: {', '.join(running_bash_jobs)}). "
                    f"Use manage_jobs(action='list'|'wait'|'kill') before starting another background job."
                )

            bg_timeout = float(timeout_seconds) if (timeout_seconds and timeout_seconds != 180) else default_bg_timeout
            bg_timeout = min(max(1.0, bg_timeout), max_bg_timeout)
            os.makedirs(JOBS_DIR, exist_ok=True)

            def _alloc_bash_job(data):
                jid_num = data.get("next_id", 1)
                data["next_id"] = jid_num + 1
                return f"job_{jid_num}"

            job_id = config.update_bg_jobs(STATE_DIR, _alloc_bash_job)
            log_path = os.path.join(JOBS_DIR, f"{job_id}.log")
            start_epoch = time.time()
            start_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            log_fp = open(log_path, "w", encoding="utf-8", buffering=1)
            log_fp.write(
                f"=== BACKGROUND BASH JOB: {job_id} ===\n"
                f"Command: {command}\n"
                f"Started: {start_str} | Timeout: {bg_timeout:.0f}s\n"
                f"=====================================\n\n"
            )
            log_fp.flush()

            process = await asyncio.create_subprocess_shell(
                safe_bash_payload,
                cwd=SANDBOX_DIR,
                env=current_env,
                executable="/bin/bash",
                stdout=log_fp,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                pgid = os.getpgid(process.pid)
            except Exception:
                pgid = process.pid

            def _register_bash_job(data):
                data.setdefault("jobs", {})[job_id] = {
                    "job_id": job_id,
                    "type": "bash",
                    "command": command,
                    "pid": process.pid,
                    "pgid": pgid,
                    "status": "running",
                    "start_time": start_str,
                    "start_epoch": start_epoch,
                    "timeout_seconds": bg_timeout,
                    "log_path": log_path,
                    "notified": False,
                    "logged_to_chat": False,
                }
                return data

            config.update_bg_jobs(STATE_DIR, _register_bash_job)
            mon_task = asyncio.create_task(_monitor_bash_job(job_id, process, log_fp, log_path, start_epoch, bg_timeout))
            _ACTIVE_BG_TASKS[job_id] = {
                "type": "bash",
                "process": process,
                "task": mon_task,
                "pgid": pgid,
            }
            logger.info(f"[execute_bash] Started background job {job_id} (PID {process.pid}): {command[:200]}")

            # Brief grace window to catch instant syntax errors or immediate exits
            await asyncio.sleep(0.15)
            cur_job = config.read_bg_jobs(STATE_DIR).get("jobs", {}).get(job_id, {})
            if cur_job.get("status") in ("completed", "failed"):
                def _mark_imm_notified(data):
                    if job_id in data.get("jobs", {}):
                        data["jobs"][job_id]["notified"] = True
                    return data
                config.update_bg_jobs(STATE_DIR, _mark_imm_notified)
                tail_out = _read_log_tail(log_path, max_lines=30)
                return (
                    f"Background Job '{job_id}' (PID {process.pid}) finished immediately with status '{cur_job.get('status')}' "
                    f"(Exit Code: {cur_job.get('exit_code')}).\n"
                    f"Log File: {log_path}\n"
                    f"Output:\n{tail_out}"
                )

            return (
                f"SUCCESS: Background bash job '{job_id}' started in the background.\n"
                f"- Job ID: {job_id} | PID: {process.pid} | Timeout: {bg_timeout:.0f}s\n"
                f"- Live Output Log: {log_path}\n"
                f"- Active Background Bash Slots: {len(running_bash_jobs) + 1}/{max_jobs}\n"
                f"You will automatically receive a [SYSTEM NOTICE] when '{job_id}' finishes. "
                f"Use manage_jobs(action='status'|'wait'|'kill', job_id='{job_id}' or 'all') to inspect, wait for, or stop it."
            )

        # Utilizes an asynchronous shell context to permit multi-language execution pipelines (&&, |, >) safely within container borders
        process = await asyncio.create_subprocess_shell(
            safe_bash_payload,
            cwd=SANDBOX_DIR,
            env=current_env,
            executable="/bin/bash",
            stdout=asyncio.subprocess.PIPE,     
            stderr=asyncio.subprocess.STDOUT,   
            stdin=asyncio.subprocess.DEVNULL,   
            start_new_session=True              
        )

        try:
            # Await the completion with an async timeout
            timeout_val = float(timeout_seconds) if timeout_seconds else 180.0
            stdout_data, _ = await asyncio.wait_for(process.communicate(), timeout=timeout_val)
            
        except (asyncio.TimeoutError, asyncio.CancelledError):
            # If it times out or is cancelled, aggressively kill the entire process group
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
            try:
                await process.wait()
            except Exception:
                pass
                
            logger.error(f"[execute_bash] Command timed out after {timeout_seconds}s: {command[:200]}")
            return f"SYSTEM ERROR: Command timed out after {timeout_seconds} seconds and was forcefully terminated."

        # Decode the byte stream safely
        output = stdout_data.decode('utf-8', errors='replace') if stdout_data else ""

        if process.returncode != 0:
            logger.warning(f"[execute_bash] Non-zero exit ({process.returncode}) for: {command[:200]} | output: {output[:300]}")
            if "no such module: vec0" in output.lower() or "no such function: vec_" in output.lower():
                output += "\n[SYSTEM HINT: The sqlite3 CLI binary does not load the sqlite-vec extension. Use the native 'query_sqlite_db' tool instead, which pre-loads sqlite-vec.]"
        else:
            logger.info(f"[execute_bash] Success (exit 0): {command[:200]}")

        # Check for inline coding patterns and append soft delegation advisory
        inline_advisory = ""
        is_inline_code = (
            bool(re.search(r'>\s*[^;&|\n]*\.(?:py|js|ts|rs|cpp|c|hpp|h|mjs|cjs)\b', command, re.IGNORECASE)) or
            bool(re.search(r'\bpython[3]?\s+-\s*<<', command, re.IGNORECASE)) or
            any(
                '\n' in code.strip() or len(code.strip()) > 100 or bool(re.search(r'\b(def |class |while |for |with |open\(|lambda )\b', code))
                for _, code in re.findall(r'\bpython[3]?\s+-c\s+([\'"])(.*?)\1', command, re.DOTALL)
            ) or
            any(
                '\n' in code.strip() or len(code.strip()) > 60
                for _, code in re.findall(r'\bnode\s+-e\s+([\'"])(.*?)\1', command, re.DOTALL)
            )
        )
        if is_inline_code:
            inline_advisory = (
                "\n\n[SYSTEM DELEGATION ADVISORY: You authored or executed an inline script directly in bash. "
                "For reliability, automated syntax checking, and persistence, delegate script creation and data tasks to the Coder via 'create_tool'.]"
            )

        # --- THE POINTER APPROACH (Context Protection) ---
        if len(output) > 10000:
            # Generate a clean timestamped filename
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            temp_file_name = f"cmd_output_{timestamp}.txt"
            os.makedirs(SANDBOX_DIR, exist_ok=True)
            temp_file_path = os.path.join(SANDBOX_DIR, temp_file_name)
            
            # Save the full massive output safely to the sandbox
            with open(temp_file_path, "w", encoding="utf-8") as f:
                f.write(output)
                
            total_lines = output.count('\n') + 1
            logger.info(f"[execute_bash] Truncated output ({len(output):,} chars, {total_lines:,} lines) -> saved to '/app/workspace/sandbox/{temp_file_name}'")

            head_preview = output[:1500]
            tail_preview = output[-1500:]
            omitted_chars = len(output) - len(head_preview) - len(tail_preview)
            head_lines = head_preview.count('\n') + 1
            tail_lines = tail_preview.count('\n') + 1
            
            return (f"Exit Code: {process.returncode}\n"
                    f"full_output_filepath: /app/workspace/sandbox/{temp_file_name}\n"
                    f"Output Preview (Head: first 1,500 chars / {head_lines} lines | Tail: last 1,500 chars / {tail_lines} lines of {len(output):,} chars total):\n"
                    f"--- [HEAD PREVIEW (First 1,500 chars)] ---\n{head_preview}\n\n"
                    f"... [SNIP: {omitted_chars:,} characters omitted — complete {len(output):,} chars ({total_lines:,} lines) saved to '/app/workspace/sandbox/{temp_file_name}'] ...\n\n"
                    f"--- [TAIL PREVIEW (Last 1,500 chars)] ---\n{tail_preview}\n\n"
                    f"[SYSTEM CONTEXT GUARDRAIL: Output exceeded 10,000 characters. Both head (headers/start) and tail (final results/errors) are shown above.\n"
                    f"HOW TO GET AROUND THIS TRUNCATION:\n"
                    f"- To inspect additional tail lines: execute_bash('tail -n 100 /app/workspace/sandbox/{temp_file_name}')\n"
                    f"- To inspect a specific line range: execute_bash('sed -n \\'100,200p\\' /app/workspace/sandbox/{temp_file_name}')\n"
                    f"- To search for specific keywords: execute_bash('grep -n -C 3 \"ERROR\\|Exception\" /app/workspace/sandbox/{temp_file_name}')\n"
                    f"- To delegate analysis of the entire file: analyze_files(['/app/workspace/sandbox/{temp_file_name}'], 'Find errors ...')]{inline_advisory}")

        return f"Exit Code: {process.returncode}\nOutput:\n{output}{inline_advisory}"
        
    except Exception as e:
        logger.error(f"[execute_bash] Exception on '{command[:200]}': {str(e)}")
        return f"Error executing command: {str(e)}"


async def _run_delegated_subagent(
    job_id: str,
    agent_key: str,
    objective: str,
    context_filepaths: list[str] = None,
    resolved_out: str = None,
    trace_path: str = "",
    start_epoch: float = 0.0,
    timeout_seconds: float = 3600.0,
) -> tuple[bool, str, str, str]:
    """Executes a delegated sub-agent task (analyst, adviser, coder, or auxiliary), saves full trace/output, and updates bg_jobs.json."""
    try:
        file_env_ctx = gather_agent_context(context_filepaths) if context_filepaths else ""
        if agent_key == "coder":
            profile = coder_profile
            client = coder_client
            sys_prompt = config.SYSTEM_PROMPTS["coder"]
            user_prompt = f"{file_env_ctx}\n\n" + config.PROMPTS["coder_user"].format(objective=objective)
        elif agent_key == "adviser":
            profile = adviser_profile
            client = adviser_client
            sys_prompt = config.SYSTEM_PROMPTS["adviser"]
            user_prompt = f"--- ATTACHED ENVIRONMENT CONTEXT ---\n{file_env_ctx}\n\n--- DELEGATED OBJECTIVE ---\n{objective}"
        elif agent_key == "auxiliary":
            profile = analyst_profile
            target_url, target_key, aux_timeout = resolve_auxiliary_endpoint(None, None, timeout_seconds)
            client = AsyncOpenAI(base_url=target_url, api_key=target_key, timeout=aux_timeout)
            sys_prompt = "You are a helpful, precise technical sub-agent."
            user_prompt = f"{file_env_ctx}\n\n{objective}"
        else:  # analyst
            profile = analyst_profile
            client = analyst_client
            sys_prompt = config.SYSTEM_PROMPTS["analyst"]
            user_prompt = f"--- ATTACHED ENVIRONMENT CONTEXT ---\n{file_env_ctx}\n\n--- ANALYSIS OBJECTIVE ---\n{objective}"

        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt},
        ]
        max_ctx = profile.get("context_window", config.MAX_CONTEXT_TOKENS)
        safe_budget = int(max_ctx * 0.90)
        payload_tokens = get_payload_tokens(messages)
        if payload_tokens > safe_budget:
            err_msg = f"Payload ({payload_tokens} tokens) exceeds sub-agent context budget ({safe_budget} tokens)."
            raise ValueError(err_msg)

        api_args = profile.get("api_params", {}).copy()
        api_args["model"] = profile["model"]
        api_args["messages"] = messages
        api_args["timeout"] = timeout_seconds

        response = await client.chat.completions.create(**api_args)
        thinking, content = extract_thinking_and_content(response.choices[0].message)

        tokens_in = response.usage.prompt_tokens if (response.usage and response.usage.prompt_tokens) else payload_tokens
        tokens_out = (
            response.usage.completion_tokens
            if (response.usage and response.usage.completion_tokens)
            else (len(tokenizer.encode(content)) if tokenizer else len(content) // 4)
        )
        thinking_tokens = (
            getattr(response.usage.completion_tokens_details, "reasoning_tokens", 0)
            if response.usage and hasattr(response.usage, "completion_tokens_details") and response.usage.completion_tokens_details
            else 0
        )
        if thinking_tokens == 0 and thinking:
            thinking_tokens = len(thinking) // 4
        config.log_token_usage(STATE_DIR, agent_key, tokens_in, tokens_out, thinking_tokens)

        clean_output = content.strip()
        if (agent_key == "coder" or (resolved_out and resolved_out.lower().endswith(".json"))) and clean_output.startswith("```"):
            clean_output = re.sub(r"^```[a-zA-Z0-9_+-]*[ \t]*\r?\n?", "", clean_output)
            clean_output = re.sub(r"\r?\n?```\s*$", "", clean_output).strip()

        # Save full observable trace to state/jobs/<job_id>_<agent>.md
        os.makedirs(JOBS_DIR, exist_ok=True)
        dur = round(time.time() - start_epoch, 2)
        end_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        trace_doc = (
            f"# Delegated Sub-Agent Trace: {job_id} ({agent_key.upper()})\n"
            f"- **Model**: `{profile.get('model')}`\n"
            f"- **Completed**: {end_str} ({dur}s)\n"
            f"- **Tokens**: {tokens_in} in | {tokens_out} out | {thinking_tokens} thinking\n"
            f"- **Context Files**: {context_filepaths or 'None'}\n"
            f"- **Output File**: {resolved_out or 'None'}\n\n"
            f"## Objective\n{objective}\n\n"
        )
        if thinking:
            trace_doc += f"## Sub-Agent Reasoning (`<thinking>`)\n```text\n{thinking}\n```\n\n"
        trace_doc += f"## Sub-Agent Output\n{clean_output}\n"
        with open(trace_path, "w", encoding="utf-8") as tf:
            tf.write(trace_doc)

        # Save deliverable to resolved_out if requested (never write <thinking> into output_filepath)
        if resolved_out and clean_output:
            os.makedirs(os.path.dirname(resolved_out), exist_ok=True)
            if os.path.exists(resolved_out):
                os.makedirs("/app/workspace/archive", exist_ok=True)
                bak_name = f"{os.path.basename(resolved_out)}.{datetime.now().strftime('%Y%m%d%H%M%S_%f')}.bak"
                shutil.copy2(resolved_out, os.path.join("/app/workspace/archive", bak_name))
            with open(resolved_out, "w", encoding="utf-8") as of:
                of.write(clean_output)

        def _mark_done(data):
            j = data.get("jobs", {}).get(job_id)
            if j:
                if j.get("status") != "killed":
                    j["status"] = "completed"
                j["exit_code"] = 0
                j["duration_seconds"] = dur
                j["end_time"] = end_str
                j["tokens_in"] = tokens_in
                j["tokens_out"] = tokens_out
                j["thinking_tokens"] = thinking_tokens
                j["thinking"] = thinking
                j["full_output"] = clean_output
                j["result_preview"] = clean_output[:1500]
            return data

        config.update_bg_jobs(STATE_DIR, _mark_done)
        return True, thinking, clean_output, ""

    except asyncio.CancelledError:
        dur = round(time.time() - start_epoch, 2)
        end_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        def _mark_killed(data):
            j = data.get("jobs", {}).get(job_id)
            if j:
                j["status"] = "killed"
                j["exit_code"] = -15
                j["duration_seconds"] = dur
                j["end_time"] = end_str
            return data
        config.update_bg_jobs(STATE_DIR, _mark_killed)
        return False, "", "", "Task was killed."

    except Exception as e:
        dur = round(time.time() - start_epoch, 2)
        end_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        trace_f = save_failure_trace(f"delegate_{agent_key}", {
            "job_id": job_id,
            "subagent": agent_key,
            "objective": objective,
            "context_filepaths": context_filepaths,
            "error": str(e),
            "traceback": traceback.format_exc(),
        })
        def _mark_failed(data):
            j = data.get("jobs", {}).get(job_id)
            if j:
                j["status"] = "failed"
                j["exit_code"] = 1
                j["duration_seconds"] = dur
                j["end_time"] = end_str
                j["error"] = str(e)
                j["failure_trace"] = trace_f
            return data
        config.update_bg_jobs(STATE_DIR, _mark_failed)
        return False, "", "", str(e)
    finally:
        _ACTIVE_BG_TASKS.pop(job_id, None)


@mcp.tool()
async def delegate_task(
    objective: str,
    agent: str = "analyst",
    context_filepaths: list[str] = None,
    output_filepath: str = None,
    timeout_seconds: int = 3600,
    background: bool = True,
) -> str:
    """Delegates a self-contained subtask (analysis, strategic audit, or drafting) to a specialized sub-agent.
    - 'objective': Detailed, self-contained instructions for the sub-agent.
    - 'agent': Target specialist ('analyst', 'adviser', 'coder', or 'auxiliary'). Defaults to 'analyst'.
    - 'context_filepaths': Optional list of file paths to attach to the sub-agent's prompt.
    - 'output_filepath': Optional path in '/app/workspace/outputs' or '/app/workspace/sandbox' to save the sub-agent's output.
    - 'timeout_seconds': Defaults to 3600 (60 min, up to 36000 / 10h).
    - 'background': Defaults to True (runs asynchronously in the background while you continue other work; max 9 concurrent subtasks). Set to False to wait synchronously.
    """
    if not objective or not str(objective).strip():
        return "SYSTEM ERROR: 'objective' must be a non-empty string."

    agent_key = (agent or "analyst").strip().lower()
    if agent_key not in ("analyst", "adviser", "coder", "auxiliary"):
        return f"SYSTEM ERROR: Invalid agent '{agent}'. Must be one of: 'analyst', 'adviser', 'coder', 'auxiliary'."

    resolved_out = None
    if output_filepath:
        candidate = output_filepath.strip()
        if not os.path.isabs(candidate):
            candidate = os.path.join(SANDBOX_DIR, candidate)
        resolved_out = os.path.realpath(candidate)
        allowed_dirs = [os.path.realpath(OUTPUTS_DIR), os.path.realpath(SANDBOX_DIR)]
        if not any(os.path.commonpath([resolved_out, d]) == d for d in allowed_dirs):
            return f"SYSTEM ERROR: 'output_filepath' must be inside {OUTPUTS_DIR} or {SANDBOX_DIR}."

    if isinstance(context_filepaths, str):
        context_filepaths = [context_filepaths]
    effective_context_paths = [str(p).strip() for p in (context_filepaths or []) if str(p).strip()]
    if not effective_context_paths and objective:
        # Auto-extract existing workspace/host_input file paths mentioned in objective text
        raw_candidates = re.findall(r"(?:/app/(?:workspace|host_input)/[^\s'\"`()\[\]{}<>,;:|]+)", objective)
        seen_paths = set()
        for cand in raw_candidates:
            cand_clean = cand.rstrip(".,;:!?)")
            real_cand = os.path.realpath(cand_clean)
            if resolved_out and real_cand == resolved_out:
                continue
            if real_cand not in seen_paths and os.path.isfile(real_cand):
                seen_paths.add(real_cand)
                effective_context_paths.append(cand_clean)
    context_filepaths = effective_context_paths

    _reconcile_bg_jobs()
    max_subtasks = getattr(config, "MAX_BACKGROUND_SUBTASKS", 9)
    default_bg_timeout = float(getattr(config, "BG_JOB_DEFAULT_TIMEOUT", 3600))
    max_bg_timeout = float(getattr(config, "BG_JOB_MAX_TIMEOUT", 36000))
    sub_timeout = min(max(1.0, float(timeout_seconds or default_bg_timeout)), max_bg_timeout)

    bg_state = config.read_bg_jobs(STATE_DIR)
    running_subtasks = [
        jid for jid, j in bg_state.get("jobs", {}).items()
        if j.get("status") == "running" and j.get("type") == "subagent"
    ]
    if background and len(running_subtasks) >= max_subtasks:
        return (
            f"SYSTEM ERROR: Maximum concurrent background delegated subtasks limit ({max_subtasks}) reached "
            f"(Active: {', '.join(running_subtasks)}). "
            f"Use manage_jobs(action='list'|'wait'|'kill') before starting another background subtask."
        )

    os.makedirs(JOBS_DIR, exist_ok=True)
    def _alloc_sub_job(data):
        jid_num = data.get("next_id", 1)
        data["next_id"] = jid_num + 1
        return f"job_{jid_num}"

    job_id = config.update_bg_jobs(STATE_DIR, _alloc_sub_job)
    trace_path = os.path.join(JOBS_DIR, f"{job_id}_{agent_key}.md")
    start_epoch = time.time()
    start_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _register_sub_job(data):
        data.setdefault("jobs", {})[job_id] = {
            "job_id": job_id,
            "type": "subagent",
            "agent": agent_key,
            "objective": objective,
            "context_filepaths": context_filepaths or [],
            "output_filepath": resolved_out,
            "log_path": trace_path,
            "status": "running",
            "start_time": start_str,
            "start_epoch": start_epoch,
            "timeout_seconds": sub_timeout,
            "notified": not background,
            "logged_to_chat": not background,
        }
        return data

    config.update_bg_jobs(STATE_DIR, _register_sub_job)

    if background:
        task = asyncio.create_task(
            _run_delegated_subagent(job_id, agent_key, objective, context_filepaths, resolved_out, trace_path, start_epoch, sub_timeout)
        )
        _ACTIVE_BG_TASKS[job_id] = {"type": "subagent", "agent": agent_key, "task": task}
        out_note = f"\n- Target Deliverable Path: {resolved_out}" if resolved_out else ""
        return (
            f"SUCCESS: Delegated subtask '{job_id}' dispatched to '{agent_key.upper()}' in the background.\n"
            f"- Job ID: {job_id} | Agent: {agent_key} | Timeout: {sub_timeout:.0f}s\n"
            f"- Full Trace File: {trace_path}{out_note}\n"
            f"- Active Background Subtask Slots: {len(running_subtasks) + 1}/{max_subtasks}\n"
            f"You will automatically receive a [SYSTEM NOTICE] when '{job_id}' completes. "
            f"Use manage_jobs(action='status'|'wait', job_id='{job_id}' or 'all') to check or wait for completion."
        )
    else:
        ok, thinking, clean_output, err_str = await _run_delegated_subagent(
            job_id, agent_key, objective, context_filepaths, resolved_out, trace_path, start_epoch, sub_timeout
        )
        if not ok:
            return f"SYSTEM ERROR: Delegated task '{job_id}' ({agent_key}) failed: {err_str}"
        if not clean_output:
            warn_body = (
                f"[SYSTEM WARNING: Delegated '{agent_key}' returned empty output (only reasoning). "
                f"{'Output file was not written. ' if resolved_out else ''}"
                f"Ensure all input files are passed in 'context_filepaths' (sub-agents cannot execute file-reading tools on their own).]"
            )
            res_msg = f"=== DELEGATED {agent_key.upper()} COMPLETED ({job_id}) ===\n[Trace saved to: {trace_path}]\n\n{warn_body}"
        else:
            out_note = f"\n[Saved output to: {resolved_out} ({len(clean_output):,} chars)]" if resolved_out else ""
            preview = clean_output if len(clean_output) <= 6000 else (clean_output[:3000] + f"\n\n... [Truncated inline; full output saved to {resolved_out or trace_path}]")
            res_msg = f"=== DELEGATED {agent_key.upper()} COMPLETED ({job_id}) ==={out_note}\n[Trace saved to: {trace_path}]\n\n{preview}"
        tag_map = {
            "analyst": ("ANALYST_THOUGHTS", "ANALYST_REPORT"),
            "adviser": ("ADVISER_THOUGHTS", "ADVISER_REPORT"),
            "coder": ("CODER_THOUGHTS", "CODER_CODE"),
            "auxiliary": ("AUXILIARY_THOUGHTS", "AUXILIARY_OUTPUT"),
        }
        t_tag, c_tag = tag_map.get(agent_key, ("ANALYST_THOUGHTS", "ANALYST_REPORT"))
        if thinking:
            res_msg += f"\n<___{t_tag}___>\n{thinking}\n</___{t_tag}___>"
        res_msg += f"\n<___{c_tag}___>\n{clean_output}\n</___{c_tag}___>"
        return res_msg


@mcp.tool()
async def manage_jobs(
    action: str = "list",
    job_id: str = None,
    wait_seconds: int = 60,
) -> str:
    """Inspects, waits on, or terminates background jobs (both bash commands and delegated sub-agent tasks).
    - 'action':
      * 'list': Lists all background jobs in this session with status, duration, and log/output paths.
      * 'status': Shows detailed metadata and latest output for 'job_id' (or all running jobs if 'job_id' is omitted).
      * 'wait': Blocks up to 'wait_seconds' (1-36000s) until 'job_id' (or ALL running jobs if 'job_id' is 'all' or omitted) finishes, then returns the result(s).
      * 'kill': Terminates 'job_id' (or all running jobs if job_id='all').
    """
    act = (action or "list").strip().lower()
    _reconcile_bg_jobs()
    max_jobs = getattr(config, "MAX_BACKGROUND_JOBS", 9)
    max_subtasks = getattr(config, "MAX_BACKGROUND_SUBTASKS", 9)
    max_bg_timeout = float(getattr(config, "BG_JOB_MAX_TIMEOUT", 36000))

    def _resolve_jid(raw_id: str, jobs_dict: dict) -> str:
        if not raw_id:
            return ""
        cand = raw_id.strip()
        if cand in jobs_dict:
            return cand
        if cand.isdigit() and f"job_{cand}" in jobs_dict:
            return f"job_{cand}"
        return cand

    def _mark_jobs_notified(jids_to_mark: list[str]):
        if not jids_to_mark:
            return
        def _mut(data):
            for tid in jids_to_mark:
                j = data.get("jobs", {}).get(tid)
                if j and j.get("status") != "running":
                    j["notified"] = True
            return data
        config.update_bg_jobs(STATE_DIR, _mut)

    def _format_job_summary(jid: str, j: dict, include_tail: bool = False) -> str:
        jtype = j.get("type", "bash")
        status = j.get("status", "unknown").upper()
        if j.get("status") == "running":
            elapsed = round(time.time() - float(j.get("start_epoch", time.time())), 1)
            time_info = f"running {elapsed}s"
        else:
            time_info = f"{j.get('duration_seconds', 0.0)}s"

        if jtype == "bash":
            cmd_snip = (j.get("command") or "").replace("\n", " ")[:100]
            rc_str = f" | Exit Code: {j['exit_code']}" if "exit_code" in j and j.get("status") != "running" else ""
            header = f"• {jid} [BASH | PID {j.get('pid')}] — {status} ({time_info}{rc_str})\n  Command: {cmd_snip}\n  Log: {j.get('log_path')}"
        else:
            ag = j.get("agent", "subagent")
            obj_snip = (j.get("objective") or "").replace("\n", " ")[:100]
            out_str = f"\n  Output File: {j.get('output_filepath')}" if j.get("output_filepath") else ""
            tok_str = f" | Tokens: {j.get('tokens_in', 0)} in, {j.get('tokens_out', 0)} out" if j.get("tokens_in") else ""
            header = f"• {jid} [SUBAGENT:{ag.upper()}] — {status} ({time_info}{tok_str})\n  Objective: {obj_snip}\n  Trace: {j.get('log_path')}{out_str}"

        if include_tail:
            if jtype == "subagent":
                if j.get("status") == "running":
                    header += f"\n  --- Sub-Agent Status ({jid}) ---\n  (Sub-agent '{j.get('agent', 'subagent')}' is still generating its response...)"
                elif j.get("status") == "failed":
                    header += f"\n  --- Sub-Agent Error ({jid}) ---\n  {j.get('error', 'Unknown error')}"
                else:
                    clean_body = (j.get("full_output") or j.get("result_preview") or "").strip()
                    if len(clean_body) > 3000:
                        clean_body = clean_body[:3000] + f"\n\n... [Truncated inline; full output saved to {j.get('output_filepath') or j.get('log_path')}]"
                    header += f"\n  --- Sub-Agent Output ({jid}) ---\n{clean_body}\n  ----------------------------------------"
            else:
                raw_tail = _read_log_tail(j.get("log_path"), max_lines=45)
                tail_lines = raw_tail.splitlines()
                if len(tail_lines) >= 4 and tail_lines[0].startswith("=== BACKGROUND BASH JOB:"):
                    tail_lines = tail_lines[4:]
                    while tail_lines and not tail_lines[0].strip():
                        tail_lines.pop(0)
                clean_tail = "\n".join(tail_lines).strip() or "(no output yet)"
                header += f"\n  --- Latest Log Output ({jid}) ---\n{clean_tail}\n  ----------------------------------------"
        return header

    if act == "list":
        bg_state = config.read_bg_jobs(STATE_DIR)
        jobs = bg_state.get("jobs", {})
        if not jobs:
            return f"=== BACKGROUND JOBS (0/{max_jobs} bash, 0/{max_subtasks} subtasks active) ===\nNo background jobs have been started in this session."
        run_bash = sum(1 for j in jobs.values() if j.get("status") == "running" and j.get("type", "bash") == "bash")
        run_sub = sum(1 for j in jobs.values() if j.get("status") == "running" and j.get("type") == "subagent")
        lines = [f"=== BACKGROUND JOBS ({run_bash}/{max_jobs} bash slots, {run_sub}/{max_subtasks} subtask slots active) ==="]
        for jid, j in jobs.items():
            lines.append(_format_job_summary(jid, j, include_tail=False))
        return "\n".join(lines)

    elif act == "status":
        bg_state = config.read_bg_jobs(STATE_DIR)
        jobs = bg_state.get("jobs", {})
        if job_id and job_id.strip().lower() != "all":
            target_id = _resolve_jid(job_id, jobs)
            if target_id not in jobs:
                return f"SYSTEM ERROR: Job '{job_id}' not found. Available jobs: {', '.join(jobs.keys()) or 'none'}."
            if jobs[target_id].get("status") != "running":
                _mark_jobs_notified([target_id])
            return _format_job_summary(target_id, jobs[target_id], include_tail=True)
        else:
            targets = [jid for jid, j in jobs.items() if j.get("status") == "running"] or list(jobs.keys())[-9:]
            if not targets:
                return "No background jobs found."
            done_targets = [jid for jid in targets if jobs[jid].get("status") != "running"]
            _mark_jobs_notified(done_targets)
            return "\n\n".join(_format_job_summary(jid, jobs[jid], include_tail=True) for jid in targets)

    elif act == "wait":
        wait_limit = min(max(1.0, float(wait_seconds or 60)), max_bg_timeout)
        deadline = time.time() + wait_limit
        initial_jobs = config.read_bg_jobs(STATE_DIR).get("jobs", {})
        target_id = _resolve_jid(job_id, initial_jobs) if (job_id and job_id.strip().lower() != "all") else None
        tracked_all_ids = [
            jid for jid, j in initial_jobs.items()
            if j.get("status") == "running" or not j.get("notified", False)
        ]

        while time.time() < deadline:
            _reconcile_bg_jobs()
            jobs = config.read_bg_jobs(STATE_DIR).get("jobs", {})
            if target_id:
                if target_id not in jobs:
                    return f"SYSTEM ERROR: Job '{job_id}' not found."
                if jobs[target_id].get("status") != "running":
                    _mark_jobs_notified([target_id])
                    return f"Job '{target_id}' finished:\n" + _format_job_summary(target_id, jobs[target_id], include_tail=True)
            else:
                running = [jid for jid, j in jobs.items() if j.get("status") == "running"]
                if not running:
                    report_ids = [jid for jid in tracked_all_ids if jid in jobs] or list(jobs.keys())[-9:]
                    _mark_jobs_notified(report_ids)
                    return "All background jobs have finished:\n\n" + "\n\n".join(
                        _format_job_summary(jid, jobs[jid], include_tail=True) for jid in report_ids
                    )
            await asyncio.sleep(0.5)

        _reconcile_bg_jobs()
        jobs = config.read_bg_jobs(STATE_DIR).get("jobs", {})
        if target_id and target_id in jobs:
            return (
                f"WAIT TIMEOUT: Job '{target_id}' is still running after waiting {wait_limit:.0f}s.\n"
                + _format_job_summary(target_id, jobs[target_id], include_tail=True)
            )
        running = [jid for jid, j in jobs.items() if j.get("status") == "running"]
        return (
            f"WAIT TIMEOUT: {len(running)} job(s) still running after {wait_limit:.0f}s ({', '.join(running)}).\n\n"
            + "\n\n".join(_format_job_summary(jid, jobs[jid], include_tail=True) for jid in running)
        )

    elif act == "kill":
        if not job_id:
            return "SYSTEM ERROR: Provide 'job_id' (e.g. 'job_1' or 'all') to kill."
        bg_state = config.read_bg_jobs(STATE_DIR)
        jobs = bg_state.get("jobs", {})
        target_arg = job_id.strip()
        targets = (
            [jid for jid, j in jobs.items() if j.get("status") == "running"]
            if target_arg.lower() == "all"
            else [_resolve_jid(target_arg, jobs)]
        )
        if not targets:
            return "No running background jobs to kill."

        killed_list = []
        for tid in targets:
            if tid not in jobs:
                continue
            j = jobs[tid]
            active_entry = _ACTIVE_BG_TASKS.get(tid)
            if j.get("type") == "bash":
                pgid = j.get("pgid") or j.get("pid")
                if pgid:
                    try:
                        os.killpg(int(pgid), signal.SIGKILL)
                    except Exception:
                        try:
                            os.kill(int(j.get("pid")), signal.SIGKILL)
                        except Exception:
                            pass
            if active_entry and active_entry.get("task"):
                try:
                    active_entry["task"].cancel()
                except Exception:
                    pass
            killed_list.append(tid)

        await asyncio.sleep(0.1)
        now_ts = time.time()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        def _kill_mutator(data):
            for tid in killed_list:
                if tid in data.get("jobs", {}):
                    j = data["jobs"][tid]
                    j["status"] = "killed"
                    j["exit_code"] = -15
                    j["end_time"] = now_str
                    j["duration_seconds"] = round(now_ts - float(j.get("start_epoch", now_ts)), 2)
            return data
        config.update_bg_jobs(STATE_DIR, _kill_mutator)
        return f"SUCCESS: Terminated background job(s): {', '.join(killed_list)}."

    else:
        return f"SYSTEM ERROR: Unknown action '{action}'. Valid actions are 'list', 'status', 'wait', 'kill'."

        

@mcp.tool()
def write_file(filepath: str, content: str, mode: str = "w", force: bool = False) -> str:
    """Writes text content to a file in 'outputs' or 'sandbox' directory.
    - mode: 'w' (default) to overwrite, or 'a' to append content to an existing file.
    - force: Set to True to intentionally overwrite when the existing file is >1,000 bytes and the new content is <40% of the existing file size (a .bak backup is still saved).
    When mode='w' and the file already exists, an archive backup is automatically created.
    Use this instead of bash 'echo' or 'cat' to write markdown, or text files safely.
    You can only write to 'outputs' or 'sandbox' directories.
    IMPORTANT: For any source code files (Python, JS, TS, Rust, C++), you MUST use create_tool!
    """
    
    filepath = filepath.strip() if filepath else ""
    # Enforces code containment by routing all source code modifications through the syntax checking pipeline
    forbidden_extensions = (".py", ".js", ".ts", ".rs", ".cpp", ".c", ".hpp", ".h", ".cc", ".cxx", ".mjs", ".cjs")
    if filepath.lower().endswith(forbidden_extensions):
        logger.warning(f"[write_file] Blocked direct write to forbidden source extension: '{filepath}'")
        return f"SYSTEM ERROR: You are strictly FORBIDDEN from using write_file to create source code scripts directly. You MUST use 'create_tool' with the appropriate 'language' parameter so the Coder agent can generate and validate it safely."

    # 1. Resolve the absolute path
    resolved_path = os.path.realpath(filepath)
    
    # 2. Define safe zones
    allowed_dirs = [
        os.path.realpath("/app/workspace/outputs"),
        os.path.realpath("/app/workspace/sandbox")
    ]
    
    # 3. Check if the resolved path starts with any allowed directory
    is_safe = any(os.path.commonpath([resolved_path, safe_dir]) == safe_dir for safe_dir in allowed_dirs)
    
    if not is_safe:
        logger.warning(f"[write_file] Blocked path traversal attempt to '{filepath}' (resolved: '{resolved_path}')")
        return f"SYSTEM ERROR: Path Traversal Blocked. You are only allowed to write files to {allowed_dirs}."

    try:
        # Ensure parent directory exists for nested paths
        os.makedirs(os.path.dirname(resolved_path), exist_ok=True)

        # Check existing file status
        file_exists = os.path.exists(resolved_path)
        write_mode = "a" if mode.lower().strip() == "a" else "w"

        if file_exists and write_mode == "w":
            existing_size = os.path.getsize(resolved_path)
            # Size-shrink guard: prevent accidental clobbering if new content is drastically smaller (< 40%)
            if not force and existing_size > 1000 and len(content) < int(existing_size * 0.4):
                return (f"SYSTEM WARNING: Overwrite blocked to protect data integrity. "
                        f"Existing file is {existing_size} bytes, but incoming content is only {len(content)} bytes ({int(len(content)/existing_size*100)}%). "
                        f"If you intended to append, call write_file with mode='a'. If you intentionally wish to overwrite with shorter content, pass force=True (the previous version will still be backed up to archive/).")

            timestamp = datetime.now().strftime("%Y%m%d%H%M%S_%f")
            filename = os.path.basename(resolved_path)
            backup_dir = "/app/workspace/archive"
            os.makedirs(backup_dir, exist_ok=True)
            backup_path = os.path.join(backup_dir, f"{filename}.{timestamp}.bak")
            shutil.copy2(resolved_path, backup_path)
            backup_msg = f"(Old version backed up to archive/{os.path.basename(backup_path)})"
        elif file_exists and write_mode == "a":
            backup_msg = f"(Content appended to existing file)"
        else:
            backup_msg = "(New file created)"
            
        with open(resolved_path, write_mode, encoding="utf-8") as f:
            f.write(content)
            
        action_verb = "appended to" if write_mode == "a" else "saved to"
        return f"SUCCESS: File {action_verb} {resolved_path} {backup_msg}"
    except Exception as e:
        return f"Error writing file: {str(e)}"


# --- MEMORY TOOLS ---
@mcp.tool()
def view_memory_registry(category: str = None) -> str:
    """Views the long-term Memory Registry. Pass NO arguments to see memory categories. Pass a category to see specific memory titles, short descriptions, and timestamps."""
    memories = load_json(MEMORY_REGISTRY_FILE)
    if not memories: return "Memory Registry is empty."
    
    if not category:
        summary = {cat: data.get("category_description", "No description provided.") for cat, data in memories.items()}
        return json.dumps({"memory_categories": summary}, indent=2) + "\n\nCall this tool again with a category name to see available memories."
    
    if category in memories:
        cat_mems = memories[category].get("memories", {})
        summary = {}
        for title, data in cat_mems.items():
            summary[title] = {"description": data["description"], "timestamp": data["timestamp"]}
        return f"Memories in '{category}':\n{json.dumps(summary, indent=2)}\n\nUse read_memory with the exact title to read the full text."
    return f"Category not found. Available: {list(memories.keys())}"


@mcp.tool()
def read_memory(category: str, memory_title: str) -> str:
    """Reads the full markdown text of a specific memory from the registry."""
    memories = load_json(MEMORY_REGISTRY_FILE)
    try:
        mem_data = memories[category]["memories"][memory_title]
        filepath = os.path.join(WORKSPACE_DIR, mem_data["file"])
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        return f"--- MEMORY: {memory_title} ({mem_data['timestamp']}) ---\n{content}"
    except KeyError:
        return "Error: Memory or Category not found. Use view_memory_registry to see available options."
    except Exception as e:
        return f"Error reading memory file: {str(e)}"


@mcp.tool()
def store_memory(category: str, category_description: str, title: str, short_description: str, detailed_markdown: str) -> str:
    """Proactively stores an important fact, completed objective, or context into the long-term Memory Registry."""
    memories = load_json(MEMORY_REGISTRY_FILE)
    
    if category not in memories:
        memories[category] = {"category_description": category_description, "memories": {}}
    else:
        memories[category]["category_description"] = category_description
        
    safe_title = re.sub(r'[^a-zA-Z0-9_]', '', title) or "memory"
    timestamp_prefix = datetime.now().strftime("%Y%m%d%H%M%S_%f")
    filename = f"{timestamp_prefix}_{safe_title}.md"
    filepath = os.path.join(MEMORIES_DIR, filename)
    
    if isinstance(detailed_markdown, str) and "\\n" in detailed_markdown and "\n" not in detailed_markdown:
        detailed_markdown = detailed_markdown.replace("\\n", "\n").replace("\\t", "\t")
        
    os.makedirs(MEMORIES_DIR, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(detailed_markdown)
        
    memories[category]["memories"][title] = {
        "description": short_description,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "file": f"memories/{filename}"
    }
    
    save_json(MEMORY_REGISTRY_FILE, memories)
    return f"SUCCESS: Memory '{title}' saved to category '{category}'."



@mcp.tool()
async def compact_context() -> str:
    """Triggers the background Memory Manager to sequence a memory extraction followed by a targeted handoff history compression."""
    current_history = load_json(CURRENT_HISTORY_FILE)
    current_memories = load_json(MEMORY_REGISTRY_FILE)
    
    # 1. Extract the initial user objective from the first user turn
    initial_user_prompt = ""
    for msg in current_history:
        if msg.get("role") == "user":
            content = str(msg.get("content", ""))
            if not content.startswith("[SYSTEM CLOCK") and not content.startswith("[SYSTEM WARNING"):
                initial_user_prompt = content
                break
    if not initial_user_prompt:
        initial_user_prompt = "No initial user prompt identified."

    # 2. Read the Master Plan (active_plan.md)
    plan_path = os.path.join(STATE_DIR, "active_plan.md")
    active_plan_text = "No active plan found on disk."
    if os.path.exists(plan_path):
        try:
            with open(plan_path, "r", encoding="utf-8") as f:
                c = f.read().strip()
                if c:
                    active_plan_text = c
        except Exception:
            pass

    # 3. Inventory verified deliverables in /app/workspace/outputs/
    deliverables = []
    outputs_dir = os.path.join(WORKSPACE_DIR, "outputs")
    if os.path.exists(outputs_dir):
        try:
            for root, _, files in os.walk(outputs_dir):
                for file in files:
                    rel = os.path.relpath(os.path.join(root, file), WORKSPACE_DIR)
                    deliverables.append(f"/app/workspace/{rel}")
        except Exception:
            pass
    deliverables_summary = "\n".join(f"- {d}" for d in deliverables) if deliverables else "None identified yet in /app/workspace/outputs/."

    # 4. Extract recent execution window dynamically up to a safe token budget (excluding system prompt)
    non_sys_history = [m for m in current_history if m.get("role") != "system"]
    sum_ctx_limit = summarizer_profile.get("context_window", config.MAX_CONTEXT_TOKENS)
    turn_token_budget = max(20000, int(sum_ctx_limit * 0.45))
    recent_turns = []
    accum_tokens = 0
    for msg in reversed(non_sys_history):
        msg_str = json.dumps(msg)
        msg_tok = len(tokenizer.encode(msg_str)) if tokenizer else len(msg_str) // 4
        if recent_turns and len(recent_turns) >= 20 and (accum_tokens + msg_tok > turn_token_budget):
            break
        recent_turns.append(msg)
        accum_tokens += msg_tok
    recent_turns.reverse()
    recent_context_text = json.dumps(recent_turns, indent=2)

    # Accumulators for subagent telemetry
    summarizer_thoughts = []

    # ==========================================
    # STEP 1: EXTRACT MEMORIES (Strict Schema)
    # ==========================================
    memory_schema = {
        "type": "json_schema",
        "json_schema": {
            "name": "memory_extraction",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "extracted_memories": {
                        "type": "array",
                        "description": "A list of important facts, tool creations, or context to remember.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "category": {"type": "string", "description": "Broad category, e.g., 'Tool Concepts', 'User Preferences'"},
                                "category_description": {"type": "string", "description": "1-2 sentences explaining what types of memories belong in this category."},
                                "title": {"type": "string", "description": "Short, unique title"},
                                "short_description": {"type": "string", "description": "1-2 sentences summarizing the memory for the registry overview."},
                                "detailed_markdown": {"type": "string", "description": "The full, exhaustive details, code snippets, and explanations."}
                            },
                            "required": ["category", "category_description", "title", "short_description", "detailed_markdown"],
                            "additionalProperties": False
                        }
                    }
                },
                "required": ["extracted_memories"],
                "additionalProperties": False
            }
        }
    }

    mem_sys_prompt = (
        "You are a data extractor. Analyze the mission objective, active plan, verified deliverables, and recent turns. "
        "Extract NEW crucial long-term facts, completed objectives, database paths, or created tools into the memory schema. "
        "Write highly detailed markdown files for the 'detailed_markdown' field. "
        "CRITICAL: Cross-reference the provided CURRENT MEMORY REGISTRY. Do NOT extract or duplicate facts already saved in the registry!"
    )
    
    mem_user_prompt = (
        f"CURRENT MEMORY REGISTRY (DO NOT DUPLICATE THESE):\n{json.dumps(current_memories, indent=2)}\n\n"
        f"ORIGINAL GOAL:\n{initial_user_prompt}\n\n"
        f"ACTIVE MASTER PLAN:\n{active_plan_text}\n\n"
        f"VERIFIED DELIVERABLES ON DISK:\n{deliverables_summary}\n\n"
        f"RECENT EXECUTION TURNS:\n{recent_context_text}"
    )

    mem_api_args = summarizer_profile["api_params"].copy()
    mem_api_args["model"] = summarizer_profile["model"]
    mem_api_args["messages"] = [
        {"role": "system", "content": mem_sys_prompt},
        {"role": "user", "content": mem_user_prompt}
    ]
    mem_api_args["response_format"] = memory_schema

    if config.VERBOSITY_MODE != "silent":
        payload_tokens = get_payload_tokens(mem_api_args["messages"])
        sys.stderr.write(f"\n\033[93m[System: Summarizer memory extraction payload is ~{payload_tokens} estimated tokens]\033[0m\n")

    added_titles = []
    try:
        mem_response = await summarizer_client.chat.completions.create(**mem_api_args)
        mem_thinking, mem_raw_content = extract_thinking_and_content(mem_response.choices[0].message)
        if mem_thinking:
            summarizer_thoughts.append(f"--- MEMORY EXTRACTION THINKING ---\n{mem_thinking}")

        # Log token usage with fallback
        tokens_in = mem_response.usage.prompt_tokens if (mem_response.usage and mem_response.usage.prompt_tokens) else get_payload_tokens(mem_api_args["messages"])
        tokens_out = mem_response.usage.completion_tokens if (mem_response.usage and mem_response.usage.completion_tokens) else (len(tokenizer.encode(mem_raw_content)) if tokenizer else len(mem_raw_content) // 4)
        thinking_tokens = getattr(mem_response.usage.completion_tokens_details, 'reasoning_tokens', 0) if mem_response.usage and hasattr(mem_response.usage, 'completion_tokens_details') and mem_response.usage.completion_tokens_details else 0
        if thinking_tokens == 0 and mem_thinking:
            thinking_tokens = len(mem_thinking) // 4
        config.log_token_usage(STATE_DIR, "summarizer", tokens_in, tokens_out, thinking_tokens)

        mem_data = extract_json_payload(mem_raw_content)
        for memory in mem_data.get("extracted_memories", []):
            cat = memory["category"]
            cat_desc = memory["category_description"]
            title = memory["title"]
            short_desc = memory["short_description"]
            full_text = memory["detailed_markdown"]

            if cat not in current_memories:
                current_memories[cat] = {"category_description": cat_desc, "memories": {}}
            else:
                current_memories[cat]["category_description"] = cat_desc

            safe_title = re.sub(r'[^a-zA-Z0-9_]', '', title)
            timestamp_prefix = datetime.now().strftime("%Y%m%d%H%M%S")
            filename = f"{timestamp_prefix}_{safe_title}.md"
            filepath = os.path.join(MEMORIES_DIR, filename)

            with open(filepath, "w", encoding="utf-8") as f:
                f.write(full_text)

            current_memories[cat]["memories"][title] = {
                "description": short_desc,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "file": f"memories/{filename}"
            }
            added_titles.append(f"{{'category': '{cat}', 'title': '{title}'}}")

        save_json(MEMORY_REGISTRY_FILE, current_memories)

    except Exception as e:
        trace_file = save_failure_trace("summarizer_memory", {
            "subagent": "summarizer",
            "phase": "memory_extraction",
            "error": str(e),
            "traceback": traceback.format_exc(),
            "messages": mem_api_args.get("messages", [])
        })
        trace_note = f"\n[SYSTEM: Failure trace saved to '{trace_file}']" if trace_file else ""
        return f"FAILED during Memory Extraction Phase. Error: {str(e)}{trace_note}"

    # ==========================================
    # STEP 2: TARGETED HANDOFF SYNTHESIS
    # ==========================================
    handoff_schema = {
        "type": "json_schema",
        "json_schema": {
            "name": "targeted_handoff",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "handoff_markdown": {
                        "type": "string",
                        "description": "The concise 3-section transition handoff report in markdown."
                    }
                },
                "required": ["handoff_markdown"],
                "additionalProperties": False
            }
        }
    }

    handoff_sys_prompt = (
        "You are an operational handoff synthesizer. The agent's working context is being compacted. "
        "Synthesize a concise, high-signal Transition Handoff Report for the incoming Brain turn.\n\n"
        "CRITICAL REQUIREMENTS:\n"
        "1. Anchor strictly on the Original User Goal, the Master Plan, verified workspace deliverables, and recent actions.\n"
        "2. Format your response into exactly three structured sections:\n"
        "   ### 1. Accomplished Deliverables & Workspace State\n"
        "   - List exact verified filepaths created on disk, tools created, and biological/scientific findings established.\n"
        "   ### 2. Current Plan Status\n"
        "   - Specific checklist items completed vs. still pending in active_plan.md.\n"
        "   ### 3. Immediate Next Steps\n"
        "   - The precise next action or tool call the Brain should execute to continue progress toward the original goal.\n"
        "3. Be technical, precise, and completely free of conversational filler."
    )

    handoff_user_prompt = (
        f"=== ORIGINAL USER GOAL ===\n{initial_user_prompt}\n\n"
        f"=== ACTIVE MASTER PLAN (active_plan.md) ===\n{active_plan_text}\n\n"
        f"=== VERIFIED WORKSPACE DELIVERABLES ===\n{deliverables_summary}\n\n"
        f"=== RECENT EXECUTION TURNS ===\n{recent_context_text}"
    )

    handoff_api_args = summarizer_profile["api_params"].copy()
    handoff_api_args["model"] = summarizer_profile["model"]
    handoff_api_args["messages"] = [
        {"role": "system", "content": handoff_sys_prompt},
        {"role": "user", "content": handoff_user_prompt}
    ]
    handoff_api_args["response_format"] = handoff_schema

    if config.VERBOSITY_MODE != "silent":
        payload_tokens = get_payload_tokens(handoff_api_args["messages"])
        sys.stderr.write(f"\n\033[93m[System: Summarizer targeted handoff payload is ~{payload_tokens} estimated tokens]\033[0m\n")

    try:
        handoff_resp = await summarizer_client.chat.completions.create(**handoff_api_args)
        handoff_thinking, handoff_raw_content = extract_thinking_and_content(handoff_resp.choices[0].message)
        if handoff_thinking:
            summarizer_thoughts.append(f"--- TARGETED HANDOFF THINKING ---\n{handoff_thinking}")

        # Log token usage with fallback
        tokens_in = handoff_resp.usage.prompt_tokens if (handoff_resp.usage and handoff_resp.usage.prompt_tokens) else get_payload_tokens(handoff_api_args["messages"])
        tokens_out = handoff_resp.usage.completion_tokens if (handoff_resp.usage and handoff_resp.usage.completion_tokens) else (len(tokenizer.encode(handoff_raw_content)) if tokenizer else len(handoff_raw_content) // 4)
        thinking_tokens = getattr(handoff_resp.usage.completion_tokens_details, 'reasoning_tokens', 0) if handoff_resp.usage and hasattr(handoff_resp.usage, 'completion_tokens_details') and handoff_resp.usage.completion_tokens_details else 0
        if thinking_tokens == 0 and handoff_thinking:
            thinking_tokens = len(handoff_thinking) // 4
        config.log_token_usage(STATE_DIR, "summarizer", tokens_in, tokens_out, thinking_tokens)

        handoff_data = extract_json_payload(handoff_raw_content)
        handoff_markdown = handoff_data.get("handoff_markdown", "")

        # Backup the old bloated history before overwriting
        backup_file = os.path.join(HISTORIES_DIR, f"backup_history_{datetime.now().strftime('%Y%m%d%H%M%S')}.json")
        save_json(backup_file, current_history)

        handoff_directive = config.PROMPTS["handoff_wake_up_directive"]

        handoff_full_text = (
            f"=== SESSION TRANSITION HANDOFF ===\n\n"
            f"**Original Goal:**\n{initial_user_prompt}\n\n"
            f"{handoff_markdown}\n\n"
            f"===================================\n\n"
            f"{handoff_directive}"
        )

        # Overwrite the active working memory with pristine context
        new_history = [
            {"role": "system", "content": config.SYSTEM_PROMPTS["brain"]},
            {"role": "user", "content": handoff_full_text}
        ]
        save_json(CURRENT_HISTORY_FILE, new_history)
        _LOADED_SKILLS.clear()

        summarizer_output = f"Extracted Memories: {added_titles}\nCompressed History: 2 messages (Pristine Handoff)"
        result_msg = (
            f"SUCCESS: Context compressed and old history moved to {os.path.basename(backup_file)}.\n"
            f"New detailed memories extracted to disk: {added_titles}.\n"
            f"{handoff_directive}"
        )

        if summarizer_thoughts:
            result_msg += f"\n<___SUMMARIZER_THOUGHTS___>\n" + "\n\n".join(summarizer_thoughts) + "\n</___SUMMARIZER_THOUGHTS___>"
        result_msg += f"\n<___SUMMARIZER_OUTPUT___>\n{summarizer_output}\n</___SUMMARIZER_OUTPUT___>"

        return result_msg

    except Exception as e:
        trace_file = save_failure_trace("summarizer_handoff", {
            "subagent": "summarizer",
            "phase": "targeted_handoff_synthesis",
            "error": str(e),
            "traceback": traceback.format_exc(),
            "messages": handoff_api_args.get("messages", []) if 'handoff_api_args' in locals() else []
        })
        trace_note = f"\n[SYSTEM: Failure trace saved to '{trace_file}']" if trace_file else ""
        return f"FAILED during Targeted Handoff Synthesis Phase. Error: {str(e)}{trace_note}"


_WORKSPACE_STATE_LOCK = asyncio.Lock()


@mcp.tool()
async def create_tool(
    category: str, 
    category_description: str, 
    tool_name: str, 
    tool_description: str, 
    objective: str, 
    language: str = "python",
    context_filepaths: list[str] = None  # ◄--- Universal Context Entrypoint
) -> str:
    """Delegates code writing to the Coder LLM, validates compilation, and registers the tool.
    CRITICAL: Set 'language' explicitly to match your objective!
    - 'language': Target programming language: 'python' (default), 'rust', 'cpp', 'javascript', or 'typescript'.
      If your objective asks for Rust (cargo/rustc), you MUST pass language='rust'.
      If your objective asks for C++, you MUST pass language='cpp'.
      If your objective asks for TypeScript, you MUST pass language='typescript'.
    - 'tool_name': Base filename for the tool (without file extension, e.g. 'kmer_counter').
    - 'category': Grouping tag (e.g. 'bioinformatics', 'database', 'parsing').
    - 'category_description': Brief description of the category.
    - 'tool_description': Summary of what this tool does.
    - 'objective': Detailed functional requirements, CLI arguments, I/O formats, and logic for the Coder.
    - 'context_filepaths': Optional list of absolute file paths for the Coder to read to adapt to existing schemas/code.
    """
    lang_map = {
        "python": {"ext": ".py", "block": "python"},
        "javascript": {"ext": ".js", "block": "javascript"},
        "typescript": {"ext": ".ts", "block": "typescript"},
        "rust": {"ext": ".rs", "block": "rust"},
        "cpp": {"ext": ".cpp", "block": "cpp"}
    }
    target_lang = language.lower() if language.lower() in lang_map else "python"
    ext = lang_map[target_lang]["ext"]
    md_block = lang_map[target_lang]["block"]

    safe_name = os.path.basename(tool_name)
    for k_ext in [".py", ".js", ".ts", ".rs", ".cpp"]:
        if safe_name.endswith(k_ext): safe_name = safe_name[:-len(k_ext)]
    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '', safe_name)
    if not safe_name: safe_name = "default_tool_name"

    filename = f"{safe_name}{ext}"
    os.makedirs(TOOLS_DIR, exist_ok=True)
    file_path = os.path.join(TOOLS_DIR, filename)
    
    coder_sys = config.SYSTEM_PROMPTS["coder"]

    # ◄--- Gather background context files seamlessly ---
    file_environmental_context = gather_agent_context(context_filepaths)

    messages = [
        {"role": "system", "content": coder_sys},
        {"role": "user", "content": f"Language Selection: {target_lang}\n{file_environmental_context}\n\n" + config.PROMPTS["coder_user"].format(objective=objective)}
    ]

    max_context = coder_profile.get("context_window", config.MAX_CONTEXT_TOKENS)
    safe_budget = int(max_context * 0.90)
    payload_tokens = get_payload_tokens(messages)
    if payload_tokens > safe_budget:
        return (f"SYSTEM ERROR: The prompt and attached context files sent to the Coder are too massive! "
                f"Payload is {payload_tokens} tokens, but the safety limit is {safe_budget}. "
                f"Please refine your 'context_filepaths' selection or shorten 'objective'.")
    
    last_validation_error = "Unknown error (validation failed without output)"
    for attempt in range(config.MAX_TOOL_RETRIES):
        try:
            api_args = coder_profile["api_params"].copy()
            api_args["model"] = coder_profile["model"]
            api_args["messages"] = messages
            if "seed" in api_args:
                api_args["seed"] = (api_args.get("seed") or 1000) + attempt
            
            if config.VERBOSITY_MODE != "silent":
                sys.stderr.write(f"\n\033[93m[System: Coder ({target_lang}) payload is ~{get_payload_tokens(messages)} estimated tokens]\033[0m\n")
            
            response = await coder_client.chat.completions.create(**api_args)
            coder_thinking, raw_content = extract_thinking_and_content(response.choices[0].message)
            
            stripped_raw = raw_content.strip()
            fence_match = re.search(rf"```{md_block}[ \t]*\r?\n(.*)\r?\n```", stripped_raw, re.DOTALL)
            if not fence_match:
                fence_match = re.search(r"```[a-zA-Z0-9_+-]*[ \t]*\r?\n(.*)\r?\n```", stripped_raw, re.DOTALL)
            if fence_match:
                code = fence_match.group(1).strip()
            else:
                code = re.sub(r"^```[a-zA-Z0-9_+-]*[ \t]*\r?\n?", "", stripped_raw)
                code = re.sub(r"\r?\n?```\s*$", "", code).strip()
    
            # Log token usage with fallback
            tokens_in = response.usage.prompt_tokens if (response.usage and response.usage.prompt_tokens) else get_payload_tokens(api_args["messages"])
            tokens_out = response.usage.completion_tokens if (response.usage and response.usage.completion_tokens) else (len(tokenizer.encode(raw_content)) if tokenizer else len(raw_content) // 4)
            thinking_tokens = getattr(response.usage.completion_tokens_details, 'reasoning_tokens', 0) if response.usage and hasattr(response.usage, 'completion_tokens_details') and response.usage.completion_tokens_details else 0
            if thinking_tokens == 0 and coder_thinking:
                thinking_tokens = len(coder_thinking) // 4
            config.log_token_usage(STATE_DIR, "coder", tokens_in, tokens_out, thinking_tokens)
            
            if os.path.exists(file_path):
                shutil.copy2(file_path, os.path.join(WORKSPACE_DIR, "archive", f"{safe_name}_{datetime.now().strftime('%Y%m%d%H%M%S')}.bak{ext}"))
            
            with open(file_path, "w", encoding="utf-8") as f: 
                f.write(code)

            deps_report = ""
            if target_lang == "python":
                requires_matches = re.findall(r"# REQUIRES:\s*(.*)", code, re.IGNORECASE)
                if requires_matches:
                    raw_reqs = " ".join(requires_matches).replace("pip install", "").replace("pixi add", "").replace(",", " ").strip()
                    cleaned_reqs = re.sub(r'\(.*?\)', '', raw_reqs).strip()
                    words = [w.strip() for w in cleaned_reqs.split() if w.strip()]
                    ignored_words = {"none", "null", "nil", "standard", "stdlib", "library", "only", "built-in", "builtin", "no", "external", "dependencies", "deps", "-"}
                    safe_deps_list = list(dict.fromkeys(
                        w for w in words
                        if re.match(r'^[A-Za-z0-9_]+[A-Za-z0-9_.-]*(?:\[.*\])?(?:[<>=!~].*)?$', w) and w.lower() not in ignored_words
                    ))
                    if safe_deps_list:
                        os.makedirs("/app/workspace/custom_packages", exist_ok=True)
                        proc_install = await asyncio.create_subprocess_exec(
                            sys.executable, "-m", "pip", "install", "--target", "/app/workspace/custom_packages", *safe_deps_list,
                            cwd=WORKSPACE_DIR, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                        )
                        _, stderr_install = await proc_install.communicate()
                        deps_report = f"\n[SYSTEM: Installed custom dependencies: {', '.join(safe_deps_list)}]" if proc_install.returncode == 0 else f"\n[SYSTEM WARNING: Dependency fault: {stderr_install.decode('utf-8', errors='replace')}]"

            # Dynamic platform syntax compile evaluations across all supported execution architectures
            is_valid = True
            err_msg = ""
            if target_lang == "python":
                check = await asyncio.to_thread(subprocess.run, [sys.executable, "-m", "py_compile", filename], cwd=TOOLS_DIR, capture_output=True, text=True)
                if check.returncode != 0: is_valid = False; err_msg = check.stderr
            elif target_lang == "javascript":
                check = await asyncio.to_thread(subprocess.run, ["node", "--check", filename], cwd=TOOLS_DIR, capture_output=True, text=True)
                if check.returncode != 0: is_valid = False; err_msg = check.stderr
            elif target_lang == "typescript":
                # Validates TypeScript syntax via esbuild transpilation over stdin + node --check
                # This guarantees syntax verification WITHOUT executing top-level code.
                esbuild_bin = shutil.which("esbuild")
                if not esbuild_bin:
                    candidate = "/app/.pixi/envs/default/lib/node_modules/tsx/node_modules/esbuild/bin/esbuild"
                    if os.path.exists(candidate):
                        esbuild_bin = candidate
                if esbuild_bin:
                    try:
                        with open(os.path.join(TOOLS_DIR, filename), "rb") as ts_file:
                            ts_bytes = ts_file.read()
                        eb_check = await asyncio.to_thread(
                            subprocess.run,
                            [esbuild_bin, "--loader=ts", "--format=esm"],
                            input=ts_bytes,
                            capture_output=True,
                            timeout=10
                        )
                        if eb_check.returncode != 0:
                            is_valid = False
                            err_msg = eb_check.stderr.decode("utf-8", errors="replace")
                        else:
                            node_check = await asyncio.to_thread(
                                subprocess.run,
                                ["node", "--check", "--input-type=module"],
                                input=eb_check.stdout,
                                capture_output=True,
                                timeout=10
                            )
                            if node_check.returncode != 0:
                                is_valid = False
                                err_msg = node_check.stderr.decode("utf-8", errors="replace")
                    except Exception as ts_err:
                        is_valid = False
                        err_msg = f"TypeScript syntax validation exception: {str(ts_err)}"
                else:
                    check = await asyncio.to_thread(subprocess.run, ["node", "--check", filename], cwd=TOOLS_DIR, capture_output=True, text=True)
                    if check.returncode != 0: is_valid = False; err_msg = check.stderr
            elif target_lang == "rust":
                # Flawless validation: Provisions an isolated cargo scratchpad project to resolve external crates automatically
                scratch_dir = f"/app/workspace/sandbox/.check_rust_{safe_name}"
                os.makedirs(os.path.join(scratch_dir, "src"), exist_ok=True)
                
                # Parse optional crate dependencies from comments (e.g., // REQUIRES: serde_json = "1.0")
                extra_deps = []
                for line in code.splitlines()[:15]:
                    stripped = line.strip()
                    if stripped.startswith(("// REQUIRES:", "// CRATES:", "// DEPENDENCIES:")):
                        raw_crates = stripped.split(":", 1)[1].strip()
                        for c in raw_crates.split(","):
                            c = c.strip()
                            if c:
                                extra_deps.append(c if "=" in c else f'{c} = "*"')
                                
                deps_lines = extra_deps
                deps_block = "\n".join(deps_lines) + ("\n" if deps_lines else "")
                
                cargo_toml_content = f"""[package]
name = "check_rust"
version = "0.1.0"
edition = "2021"

[dependencies]
{deps_block}"""
                with open(os.path.join(scratch_dir, "Cargo.toml"), "w", encoding="utf-8") as f:
                    f.write(cargo_toml_content)
                    
                # Copies the created target source file into the scratchpad context entrypoint
                shutil.copy2(file_path, os.path.join(scratch_dir, "src", "main.rs"))
                
                # Executes cargo check - verifies language tokens and syncs library assets simultaneously
                check = await asyncio.to_thread(subprocess.run, ["cargo", "check", "-q"], cwd=scratch_dir, capture_output=True, text=True)
                if check.returncode != 0: 
                    is_valid = False
                    err_msg = check.stderr

            elif target_lang == "cpp":
                # GNU compiler syntax check mode - parses headers and code templates without linking output files
                check = await asyncio.to_thread(subprocess.run, ["g++", "-fsyntax-only", "-std=c++17", filename], cwd=TOOLS_DIR, capture_output=True, text=True)
                if check.returncode != 0: is_valid = False; err_msg = check.stderr

            # --- Behavioral Pre-Registration Smoke Test ---
            if is_valid and target_lang in ["python", "javascript", "typescript"]:
                smoke_env = os.environ.copy()
                smoke_env["PYTHONPATH"] = f"/app/workspace/custom_packages:{TOOLS_DIR}:{smoke_env.get('PYTHONPATH', '')}"
                smoke_cmd = None
                if target_lang == "python":
                    smoke_cmd = [sys.executable, filename, "--help"]
                elif target_lang == "javascript":
                    smoke_cmd = ["node", filename, "--help"]
                elif target_lang == "typescript":
                    smoke_cmd = ["tsx", filename, "--help"]

                if smoke_cmd:
                    stray_help = os.path.join(TOOLS_DIR, "--help")
                    had_help = os.path.exists(stray_help)
                    try:
                        smoke_res = await asyncio.to_thread(
                            subprocess.run, smoke_cmd, cwd=TOOLS_DIR, env=smoke_env,
                            capture_output=True, text=True, timeout=4
                        )
                        # Check if running generated an unhandled traceback or runtime crash
                        combined_err = (smoke_res.stderr + smoke_res.stdout).lower()
                        if smoke_res.returncode != 0 and ("traceback (most recent call last)" in combined_err or "referenceerror:" in combined_err or "typeerror:" in combined_err or "syntaxerror:" in combined_err):
                            is_valid = False
                            err_msg = f"Behavioral smoke test crashed during startup:\n{smoke_res.stderr or smoke_res.stdout}"
                            logger.warning(f"[create_tool] Smoke test failed for '{tool_name}': {err_msg[:200]}")
                    except subprocess.TimeoutExpired:
                        pass
                    except Exception:
                        pass
                    finally:
                        if not had_help and os.path.exists(stray_help):
                            try: os.remove(stray_help)
                            except Exception: pass

            if is_valid:
                bin_dir = os.path.join(TOOLS_DIR, "bin")
                os.makedirs(bin_dir, exist_ok=True)
                bin_path = os.path.join(bin_dir, safe_name)

                if target_lang == "python":
                    run_hint = f"execute_bash('python {file_path}')"
                elif target_lang == "javascript":
                    run_hint = f"execute_bash('node {file_path}')"
                elif target_lang == "typescript":
                    run_hint = f"execute_bash('tsx {file_path}')"
                elif target_lang == "cpp":
                    try:
                        await asyncio.to_thread(subprocess.run, ["g++", "-std=c++17", "-O2", file_path, "-o", bin_path], cwd=TOOLS_DIR, capture_output=True, text=True, timeout=30)
                        shutil.copy2(bin_path, f"{file_path}_bin")
                    except Exception:
                        pass
                    run_hint = f"execute_bash('{file_path}_bin')"
                elif target_lang == "rust":
                    project_dir = f"/app/workspace/sandbox/{safe_name}_project"
                    try:
                        os.makedirs(os.path.join(project_dir, "src"), exist_ok=True)
                        with open(os.path.join(project_dir, "Cargo.toml"), "w", encoding="utf-8") as cf:
                            cf.write(f'[package]\nname = "{safe_name}"\nversion = "0.1.0"\nedition = "2021"\n\n[dependencies]\n{deps_block}')
                        shutil.copy2(file_path, os.path.join(project_dir, "src", "main.rs"))
                    except Exception:
                        pass
                    run_hint = f"execute_bash('cd /app/workspace/sandbox/{safe_name}_project && cargo run -q --')"

                async with _WORKSPACE_STATE_LOCK:
                    registry = load_json(TOOL_REGISTRY_FILE)
                    if category not in registry: registry[category] = {"category_description": category_description, "tools": {}}
                    registry[category]["tools"][tool_name] = {"path": file_path, "description": tool_description, "language": target_lang, "usage_objective": objective}
                    save_json(TOOL_REGISTRY_FILE, registry)

                    if os.path.exists("/app/workspace/.git"):
                        try:
                            proc_add = await asyncio.create_subprocess_exec("git", "add", file_path, cwd=WORKSPACE_DIR, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                            await proc_add.communicate()
                            proc_diff = await asyncio.create_subprocess_exec("git", "diff", "--cached", "--quiet", cwd=WORKSPACE_DIR)
                            if (await proc_diff.wait()) != 0:
                                clean_desc = re.sub(r'[\r\n\x00-\x1f]+', ' ', tool_description).strip()[:40]
                                proc_commit = await asyncio.create_subprocess_exec("git", "commit", "-m", f"feat(coder): create {target_lang} tool {safe_name} - {clean_desc}", cwd=WORKSPACE_DIR, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                                await proc_commit.communicate()
                        except Exception:
                            pass

                code_lines = len(code.splitlines())
                report = f"SUCCESS (Attempt {attempt+1}): {language.upper()} Asset '{tool_name}' saved to registry.\n"
                report += f"[File: {file_path} | Lines: {code_lines} | Size: {len(code):,} bytes]\n"
                report += f"[Tokens: {tokens_in} in | {tokens_out} out]\n{deps_report}Execution Blueprint: {run_hint}"
                if coder_thinking: report += f"\n<___CODER_THOUGHTS___>\n{coder_thinking}\n</___CODER_THOUGHTS___>"
                report += f"\n<___CODER_CODE___>\n{code}\n</___CODER_CODE___>"
                return report
            else:
                last_validation_error = err_msg.strip() if err_msg else "Syntax validation failed"
                logger.warning(f"[create_tool] Attempt {attempt+1} validation failed for '{tool_name}': {last_validation_error[:200]}")
                if os.path.exists(file_path): os.remove(file_path)
                save_failure_trace("coder_create_tool", {
                    "subagent": "coder",
                    "action": "create_tool",
                    "tool_name": tool_name,
                    "target_language": target_lang,
                    "attempt": attempt + 1,
                    "validation_error": last_validation_error,
                    "generated_code": code,
                    "coder_thinking": coder_thinking,
                    "prompt_messages": messages
                })
                messages.append({"role": "assistant", "content": code})
                messages.append({"role": "user", "content": f"Code validation failed. Error:\n{err_msg}\nPlease patch the syntax rules and return the raw block."})
                
        except Exception as e:
            logger.error(f"[create_tool] Exception on attempt {attempt+1} for '{tool_name}': {str(e)}")
            trace_file = save_failure_trace("coder_create_tool_exception", {
                "subagent": "coder",
                "action": "create_tool",
                "tool_name": tool_name,
                "attempt": attempt + 1,
                "error": str(e),
                "traceback": traceback.format_exc(),
                "messages": messages
            })
            trace_note = f"\n[SYSTEM: Failure trace saved to '{trace_file}']" if trace_file else ""
            return f"Fatal Tool Creation Exception on attempt {attempt+1}: {str(e)}{trace_note}"
            
    logger.error(f"[create_tool] Tool creation failed after {config.MAX_TOOL_RETRIES} attempts for '{tool_name}': {last_validation_error[:200]}")
    trace_file = save_failure_trace("coder_create_tool_final", {
        "subagent": "coder",
        "action": "create_tool",
        "tool_name": tool_name,
        "target_language": target_lang,
        "attempts": config.MAX_TOOL_RETRIES,
        "last_validation_error": last_validation_error
    })
    fail_msg = f"FAILED: Coder could not validate artifact constraints after {config.MAX_TOOL_RETRIES} runs.\nLast Validation Error:\n{last_validation_error}"
    if trace_file:
        fail_msg += f"\n[SYSTEM: Failure trace saved to '{trace_file}']"
    if 'coder_thinking' in locals() and coder_thinking:
        fail_msg += f"\n<___CODER_THOUGHTS___>\n{coder_thinking}\n</___CODER_THOUGHTS___>"
    if 'code' in locals() and code:
        fail_msg += f"\n<___CODER_CODE___>\n{code}\n</___CODER_CODE___>"
    return fail_msg


@mcp.tool()
async def edit_code(filepath: str, edit_objective: str) -> str:
    """Delegates a targeted code modification task directly to the Coder agent.
    The Coder will natively read the existing file from disk, isolate the target lines, 
    and output ONLY the precise text replacement blocks required to satisfy the objective.
    Use this for modifying scripts or source files without rewriting the whole file.
    
    Parameters:
    - filepath: Absolute path to the file to modify.
    - edit_objective: Clear instruction on what needs to be changed, added, or fixed.
    """
    filepath = filepath.strip() if filepath else ""
    real_target = os.path.realpath(filepath)
    if not os.path.exists(real_target):
        return f"SYSTEM ERROR: Target file '{filepath}' does not exist."
    if (os.path.commonpath([real_target, "/app/workspace"]) != "/app/workspace" or
        os.path.commonpath([real_target, "/app/workspace/state"]) == "/app/workspace/state" or
        os.path.commonpath([real_target, "/app/workspace/.git"]) == "/app/workspace/.git"):
        return f"SYSTEM ERROR: Target file '{filepath}' is protected or falls outside workspace boundaries."

    # 1. Natively read the file content from disk so the Coder can see it
    with open(real_target, "r", encoding="utf-8") as f:
        current_code = f.read()

    # 2. Spawn a specialized system prompt forcing the Coder to emit structured search/replace format
    coder_sys = (
        "You are an expert full-stack developer operating as a surgical script-patching agent.\n"
        "Your objective is to modify an existing script without changing unneeded blocks.\n"
        "Analyze the provided source code, identify the precise snippet(s) needing correction, "
        "and return a clean JSON object structure containing an 'edits' array of search/replace objects.\n"
        "Each item in 'edits' must have 'search_string' and 'replace_string'.\n"
        "CRITICAL RULES:\n"
        "1. UNIQUE & EXACT MATCH: Each 'search_string' MUST exist inside the source code word-for-word, down to the exact spacing and newlines. Include enough surrounding lines so each 'search_string' matches uniquely.\n"
        "2. TARGET INTEGRITY: If the edit objective instructs you to modify, replace, or delete specific lines, functions, or symbols that DO NOT exist in the provided source code, you MUST NOT hallucinate, invent, or append them. Do NOT substitute an unrelated search block. Set 'status' to 'target_not_found', explain what was missing in 'explanation', and return an empty 'edits' array (`\"edits\": []`).\n"
        "3. MINIMAL SCOPE: Change ONLY the lines necessary to satisfy the objective. Do not alter surrounding formatting or unrelated logic.\n"
        "4. ORDER OF EDITS: If multiple separate locations need changes (e.g. imports at top, flags in main), provide multiple discrete edit items in 'edits' in the order they appear in the file. Do NOT lump unrelated sections into one giant search block.\n"
        "5. SHEBANG PRESERVATION: If the file starts with a '#!' shebang on line 1, always keep the shebang on line 1 and place any new top-of-file comments or imports after it.\n"
        "Output ONLY a valid, single JSON block wrapped inside a standard markdown json code block token. No conversational filler text."
    )

    coder_user = (
        f"--- TARGET FILE LOCATION ---\n{filepath}\n\n"
        f"--- EDIT OBJECTIVE ---\n{edit_objective}\n\n"
        f"--- CURRENT ON-DISK SOURCE CODE ---\n{current_code}\n\n"
        "Perform your analysis and return the json code block immediately. "
        "If any target code specified to be modified in the objective does not exist in the source code above, "
        "set status to 'target_not_found' and edits to []."
    )

    messages = [
        {"role": "system", "content": coder_sys},
        {"role": "user", "content": coder_user}
    ]

    max_context = coder_profile.get("context_window", config.MAX_CONTEXT_TOKENS)
    safe_budget = int(max_context * 0.90)
    payload_tokens = get_payload_tokens(messages)
    if payload_tokens > safe_budget:
        return (f"SYSTEM ERROR: The file '{filepath}' sent to the Coder for edit is too massive! "
                f"Payload is {payload_tokens} tokens, but the safety limit is {safe_budget}. "
                f"Consider splitting the file or editing smaller modules.")

    try:
        api_args = coder_profile["api_params"].copy()
        api_args["model"] = coder_profile["model"]
        api_args["messages"] = messages
        api_args["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "code_edit_schema",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "string",
                            "enum": ["success", "target_not_found", "error"],
                            "description": "'success' if all targets exist and can be safely patched, or 'target_not_found'/'error' if target code does not exist in source."
                        },
                        "explanation": {
                            "type": "string",
                            "description": "Clear explanation of changes made or why the target code was not found."
                        },
                        "edits": {
                            "type": "array",
                            "description": "Sequential search/replace blocks applied in file order. Empty if target_not_found.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "search_string": {"type": "string", "description": "The exact block of code to search for."},
                                    "replace_string": {"type": "string", "description": "The new block of code to swap in."}
                                },
                                "required": ["search_string", "replace_string"],
                                "additionalProperties": False
                            }
                        }
                    },
                    "required": ["status", "explanation", "edits"],
                    "additionalProperties": False
                }
            }
        }

        response = await coder_client.chat.completions.create(**api_args)
        coder_thinking, raw_json_str = extract_thinking_and_content(response.choices[0].message)
        
        # Log Coder token usage with fallback
        tokens_in = response.usage.prompt_tokens if (response.usage and response.usage.prompt_tokens) else get_payload_tokens(api_args["messages"])
        tokens_out = response.usage.completion_tokens if (response.usage and response.usage.completion_tokens) else (len(tokenizer.encode(raw_json_str)) if tokenizer else len(raw_json_str) // 4)
        thinking_tokens = getattr(response.usage.completion_tokens_details, 'reasoning_tokens', 0) if response.usage and hasattr(response.usage, 'completion_tokens_details') and response.usage.completion_tokens_details else 0
        if thinking_tokens == 0 and coder_thinking:
            thinking_tokens = len(coder_thinking) // 4
        config.log_token_usage(STATE_DIR, "coder", tokens_in, tokens_out, thinking_tokens)

        # Parse structural change instructions safely
        try:
            edit_data = extract_json_payload(raw_json_str)
        except json.JSONDecodeError as e:
            trace_file = save_failure_trace("coder_edit_json", {
                "subagent": "coder",
                "action": "edit_code",
                "filepath": filepath,
                "objective": edit_objective,
                "error": f"JSON decode error: {str(e)}",
                "raw_response": raw_json_str,
                "coder_thinking": coder_thinking
            })
            fail_msg = f"SYSTEM ERROR: Coder returned invalid JSON for code edit: {str(e)}"
            if trace_file: fail_msg += f"\n[SYSTEM: Failure trace saved to '{trace_file}']"
            if coder_thinking: fail_msg += f"\n<___CODER_THOUGHTS___>\n{coder_thinking}\n</___CODER_THOUGHTS___>"
            fail_msg += f"\n<___CODER_CODE___>\n{raw_json_str}\n</___CODER_CODE___>"
            return fail_msg

        status = str(edit_data.get("status", "success")).lower()
        explanation = edit_data.get("explanation", "")
        edits_list = edit_data.get("edits", [])
        if not edits_list and "search_string" in edit_data:
            edits_list = [{"search_string": edit_data["search_string"], "replace_string": edit_data.get("replace_string", "")}]

        if status in ("target_not_found", "error") or not edits_list:
            reason = explanation or ("Target code specified in objective not found in file." if status == "target_not_found" else "The Coder returned an empty edits list. No modifications applied.")
            trace_file = save_failure_trace("coder_edit_target_missing", {
                "subagent": "coder",
                "action": "edit_code",
                "filepath": filepath,
                "objective": edit_objective,
                "status": status,
                "explanation": explanation,
                "raw_response": raw_json_str,
                "coder_thinking": coder_thinking
            })
            fail_msg = f"SYSTEM ERROR: The Coder could not apply code edit ({status}): {reason}"
            if trace_file: fail_msg += f"\n[SYSTEM: Failure trace saved to '{trace_file}']"
            if coder_thinking: fail_msg += f"\n<___CODER_THOUGHTS___>\n{coder_thinking}\n</___CODER_THOUGHTS___>"
            fail_msg += f"\n<___CODER_CODE___>\n{raw_json_str}\n</___CODER_CODE___>"
            return fail_msg

        # Validate that all search blocks exist before applying any modification
        working_code = current_code
        applied_blocks = []
        for idx, edit_item in enumerate(edits_list, 1):
            s_block = edit_item.get("search_string", "")
            r_block = edit_item.get("replace_string", "")
            if not s_block:
                logger.warning(f"[edit_code] Edit #{idx} in '{filepath}' provided empty search_string")
                return f"SYSTEM ERROR: Edit #{idx} provided an empty search_string. Aborting modifications for safety."
            match_count = working_code.count(s_block)
            if match_count > 1:
                logger.warning(f"[edit_code] Non-unique search string in '{filepath}' on edit #{idx} ({match_count} occurrences)")
                trace_file = save_failure_trace("coder_edit_duplicate", {
                    "subagent": "coder",
                    "action": "edit_code",
                    "filepath": filepath,
                    "objective": edit_objective,
                    "edit_index": idx,
                    "search_string": s_block,
                    "match_count": match_count,
                    "coder_thinking": coder_thinking,
                })
                fail_msg = (
                    f"SYSTEM ERROR: Edit #{idx} 'search_string' matches {match_count} times in '{filepath}'. "
                    f"It must match uniquely (exactly 1 time). Include more surrounding context lines in 'search_string' to make it unique. "
                    f"Aborting all modifications for safety."
                )
                if trace_file: fail_msg += f"\n[SYSTEM: Failure trace saved to '{trace_file}']"
                if coder_thinking: fail_msg += f"\n<___CODER_THOUGHTS___>\n{coder_thinking}\n</___CODER_THOUGHTS___>"
                fail_msg += f"\n<___CODER_CODE___>\n{raw_json_str}\n</___CODER_CODE___>"
                return fail_msg
            if s_block not in working_code:
                logger.warning(f"[edit_code] Search string mismatch in '{filepath}' on edit #{idx}")
                # Provide diagnostic near-match context so the Coder can self-correct indentation/spacing immediately
                file_lines = working_code.splitlines()
                search_lines = s_block.splitlines()
                first_search_line = search_lines[0].strip() if search_lines else ""
                near_lines = []
                for l_idx, line in enumerate(file_lines):
                    if first_search_line and first_search_line in line:
                        start_ctx = max(0, l_idx - 1)
                        end_ctx = min(len(file_lines), l_idx + len(search_lines) + 2)
                        near_lines = file_lines[start_ctx:end_ctx]
                        break
                diff_hint = ""
                if near_lines:
                    diff_hint = f"\nClosest matching lines in file:\n```\n" + "\n".join(near_lines) + "\n```\n"
                trace_file = save_failure_trace("coder_edit_mismatch", {
                    "subagent": "coder",
                    "action": "edit_code",
                    "filepath": filepath,
                    "objective": edit_objective,
                    "edit_index": idx,
                    "search_string": s_block,
                    "replace_string": r_block,
                    "coder_thinking": coder_thinking,
                    "closest_lines": near_lines
                })
                fail_msg = (
                    f"SYSTEM ERROR: The Coder generated a 'search_string' in edit #{idx} that does not match the actual file lines exactly. "
                    f"Aborting all modifications for safety.{diff_hint}"
                    f"Ensure exact match of leading whitespace, indentation, and newlines."
                )
                if trace_file: fail_msg += f"\n[SYSTEM: Failure trace saved to '{trace_file}']"
                if coder_thinking: fail_msg += f"\n<___CODER_THOUGHTS___>\n{coder_thinking}\n</___CODER_THOUGHTS___>"
                fail_msg += f"\n<___CODER_CODE___>\n{raw_json_str}\n</___CODER_CODE___>"
                return fail_msg
            working_code = working_code.replace(s_block, r_block, 1)
            applied_blocks.append(f"--- EDIT #{idx} ---\nSearch Block:\n{s_block}\n\nReplace Block:\n{r_block}")

        updated_code = working_code

        # Pre-flight syntax validation before modifying disk
        ext = os.path.splitext(filepath)[1].lower()
        if ext == ".py":
            try:
                compile(updated_code, filepath, 'exec')
            except SyntaxError as e:
                bad_line = f" ({e.text.strip()})" if e.text else ""
                logger.error(f"[edit_code] Python syntax error in '{filepath}' on line {e.lineno}: {e.msg}{bad_line}")
                trace_file = save_failure_trace("coder_edit_syntax", {
                    "subagent": "coder",
                    "action": "edit_code",
                    "filepath": filepath,
                    "objective": edit_objective,
                    "error": f"SyntaxError line {e.lineno}: {e.msg}{bad_line}",
                    "coder_thinking": coder_thinking,
                    "raw_json_str": raw_json_str
                })
                fail_msg = f"SYSTEM ERROR: Code edit aborted because it introduces a Python syntax error on line {e.lineno}{bad_line}: {e.msg}. File on disk was NOT modified."
                if trace_file: fail_msg += f"\n[SYSTEM: Failure trace saved to '{trace_file}']"
                if coder_thinking: fail_msg += f"\n<___CODER_THOUGHTS___>\n{coder_thinking}\n</___CODER_THOUGHTS___>"
                fail_msg += f"\n<___CODER_CODE___>\n{raw_json_str}\n</___CODER_CODE___>"
                return fail_msg
        elif ext in (".js", ".cpp", ".cc", ".cxx"):
            os.makedirs(SANDBOX_DIR, exist_ok=True)
            tmp_chk_path = os.path.join(SANDBOX_DIR, f".edit_check_{os.getpid()}{ext}")
            try:
                with open(tmp_chk_path, "w", encoding="utf-8") as tf:
                    tf.write(updated_code)
                chk_cmd = ["node", "--check", tmp_chk_path] if ext == ".js" else ["g++", "-fsyntax-only", "-std=c++17", tmp_chk_path]
                chk_res = await asyncio.to_thread(subprocess.run, chk_cmd, capture_output=True, text=True, timeout=15)
                if chk_res.returncode != 0:
                    err_detail = (chk_res.stderr or chk_res.stdout or "Syntax validation failed").strip()
                    fail_msg = f"SYSTEM ERROR: Code edit aborted because it introduces a syntax error:\n{err_detail}\nFile on disk was NOT modified."
                    if coder_thinking: fail_msg += f"\n<___CODER_THOUGHTS___>\n{coder_thinking}\n</___CODER_THOUGHTS___>"
                    fail_msg += f"\n<___CODER_CODE___>\n{raw_json_str}\n</___CODER_CODE___>"
                    return fail_msg
            finally:
                if os.path.exists(tmp_chk_path):
                    try: os.remove(tmp_chk_path)
                    except Exception: pass

        # Archive backup snapshot
        filename = os.path.basename(real_target)
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S_%f")
        backup_path = f"/app/workspace/archive/{filename}.{timestamp}.edit.bak"
        with open(backup_path, "w", encoding="utf-8") as f:
            f.write(current_code)

        # Write applied changes back onto disk safely
        with open(real_target, "w", encoding="utf-8") as f:
            f.write(updated_code)

        logger.info(f"[edit_code] Successfully applied {len(edits_list)} edit(s) to '{real_target}'")

        # Dynamic Git Commit Tracking Checkpoint
        if os.path.exists("/app/workspace/.git"):
            async with _WORKSPACE_STATE_LOCK:
                try:
                    proc_add = await asyncio.create_subprocess_exec("git", "add", real_target, cwd=WORKSPACE_DIR, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    await proc_add.communicate()
                    proc_diff = await asyncio.create_subprocess_exec("git", "diff", "--cached", "--quiet", cwd=WORKSPACE_DIR)
                    diff_rc = await proc_diff.wait()
                    if diff_rc != 0:
                        clean_obj = re.sub(r'[\r\n\x00-\x1f]+', ' ', edit_objective).strip()[:40]
                        proc_commit = await asyncio.create_subprocess_exec("git", "commit", "-m", f"feat(coder): code patch applied ({len(edits_list)} edits) for {clean_obj}", cwd=WORKSPACE_DIR, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                        await proc_commit.communicate()
                except Exception:
                    pass

        registry = load_json(TOOL_REGISTRY_FILE)
        blueprint_hint = ""
        for category_data in registry.values():
            for name, t_data in category_data.get("tools", {}).items():
                if t_data.get("path") == filepath:
                    lang = t_data.get("language", "python")
                    if lang == "rust":
                        blueprint_hint = "\nREMINDER: This is a Rust asset. You MUST copy this updated file from tools into your sandbox Cargo project structure (src/main.rs) and re-run compilation."
                    elif lang == "cpp":
                        blueprint_hint = "\nREMINDER: This is a C++ asset. Ensure you re-compile the source file using g++ before executing the binary path."

        edit_summary_block = "\n\n".join(applied_blocks)
        explanation_note = f" Note: {explanation}" if explanation else ""
        res = f"SUCCESS: Coder applied {len(edits_list)} edit(s) to '{filepath}' to achieve the objective.{explanation_note} Backup generated: {backup_path}.{blueprint_hint}"
        if coder_thinking:
            res += f"\n<___CODER_THOUGHTS___>\n{coder_thinking}\n</___CODER_THOUGHTS___>"
        res += f"\n<___CODER_CODE___>\n{edit_summary_block}\n</___CODER_CODE___>"
        return res

    except Exception as e:
        logger.error(f"[edit_code] Exception on '{filepath}': {str(e)}")
        trace_file = save_failure_trace("coder_edit_exception", {
            "subagent": "coder",
            "action": "edit_code",
            "filepath": filepath,
            "objective": edit_objective,
            "error": str(e),
            "traceback": traceback.format_exc()
        })
        trace_note = f"\n[SYSTEM: Failure trace saved to '{trace_file}']" if trace_file else ""
        return f"SYSTEM ERROR: Code edit sequence aborted. Error: {str(e)}{trace_note}"

db_tool_desc = f"""Executes a SQL query against a specified SQLite database.
'db_path' MUST be an absolute path (e.g., '/app/workspace/state/my_db.db').

CRITICAL RULES:
- CONTEXT: Always use LIMIT in your SELECT queries (e.g., LIMIT 10) to protect your context window!
- MULTIPLE STATEMENTS: To execute multiple schema creation commands at once, you may chain them with semicolons.
- BULK INSERTS: To insert multiple rows efficiently, write a single parameterized INSERT statement and pass a LIST OF LISTS to 'parameters'. The system will automatically use batch execution (executemany).

SEMANTIC SEARCH: If you provide a string in 'search_text_to_embed', the system will automatically generate its vector and append it to the end of your 'parameters' list. 
Search Syntax Example:
  query="SELECT m.*, v.distance FROM (SELECT rowid, distance FROM docs_vec WHERE embedding MATCH ? ORDER BY distance LIMIT 5) v JOIN docs m ON v.rowid = m.id"
  parameters=[]
  search_text_to_embed="My search query"
"""

@mcp.tool(description=db_tool_desc)
async def query_sqlite_db(db_path: str, query: str, parameters: list = None, search_text_to_embed: str = None) -> str:

    # Validate database path is inside workspace
    resolved_db = os.path.abspath(db_path)
    if not (resolved_db == "/app/workspace" or resolved_db.startswith("/app/workspace/")):
        return "SYSTEM ERROR: Database path must be inside /app/workspace"

    conn = None

    try:
        # --- Handle Search Embedding Injection ---
        if search_text_to_embed:
            response = await embedding_client.embeddings.create(
                model=config.EMBEDDING_CONFIG["model"],
                input=search_text_to_embed
            )
            
            # Log embedding token usage with fallback
            tokens_in = response.usage.prompt_tokens if (response.usage and response.usage.prompt_tokens) else len(tokenizer.encode(search_text_to_embed))
            config.log_token_usage(STATE_DIR, "embedding", tokens_in, 0, 0)
            
            embedding_vector = response.data[0].embedding
            vector_blob = array.array('f', embedding_vector).tobytes()
            
            if parameters is None:
                parameters = []
            if len(parameters) > 0 and isinstance(parameters[0], list):
                return "SYSTEM ERROR: You cannot use 'search_text_to_embed' simultaneously with bulk (list of lists) parameters."
            
            placeholder_count = query.count('?')
            if placeholder_count > 0 and len(parameters) == placeholder_count:
                # The caller provided an explicit binding slot for the embedding; replace the final placeholder
                parameters[-1] = vector_blob
            else:
                parameters.append(vector_blob)
        
        # --- Database Execution (offloaded to thread to avoid blocking the async event loop) ---
        def _exec_sqlite():
            c = None
            try:
                db_dir = os.path.dirname(db_path)
                if db_dir:
                    os.makedirs(db_dir, exist_ok=True)
                    
                c = sqlite3.connect(db_path)
                c.set_authorizer(sqlite_authorizer)
                c.execute("PRAGMA journal_mode=WAL")
                c.enable_load_extension(True)
                sqlite_vec.load(c)
                c.row_factory = sqlite3.Row
                cursor = c.cursor()
                
                is_bulk_operation = False
                is_multi_statement = False
                multi_stmt_rowcount = 0
                
                try:
                    if parameters:
                        if len(parameters) > 0 and isinstance(parameters[0], list):
                            cursor.executemany(query, parameters)
                            is_bulk_operation = True
                        else:
                            cursor.execute(query, parameters)
                    else:
                        try:
                            cursor.execute(query)
                        except Exception as e:
                            if "one statement at a time" in str(e).lower() or "can only execute one statement" in str(e).lower():
                                forbidden_patterns = [r'\bdrop\s+table\b', r'\bdelete\s+from\b', r'\btruncate\b']
                                if any(re.search(pat, query.lower()) for pat in forbidden_patterns):
                                    return "SYSTEM ERROR: Destructive database modifications (DROP/DELETE) are blocked in multi-statement queries."
                                
                                statements = []
                                buf = ""
                                for part in re.split(r'(;)', query):
                                    buf += part
                                    if part == ";" and sqlite3.complete_statement(buf):
                                        if re.sub(r'--[^\n]*|/\*[\s\S]*?\*/', '', buf).rstrip(';').strip():
                                            statements.append(buf.strip())
                                        buf = ""
                                if re.sub(r'--[^\n]*|/\*[\s\S]*?\*/', '', buf).rstrip(';').strip():
                                    statements.append(buf.strip())

                                is_multi_statement = True
                                for stmt in statements:
                                    cursor.execute(stmt)
                                    if cursor.description is None and cursor.rowcount and cursor.rowcount > 0:
                                        multi_stmt_rowcount += cursor.rowcount
                            else:
                                raise e

                except Exception as db_exec_error:
                    if c:
                        c.rollback()
                    raise db_exec_error
                                
                if is_bulk_operation or cursor.description is None:
                    rowcount = multi_stmt_rowcount if is_multi_statement else cursor.rowcount
                    res_str = f"Operation executed successfully. Rows affected/processed: {rowcount}"
                else:
                    rows = cursor.fetchall()
                    if not rows:
                        res_str = "Query executed successfully. 0 rows returned."
                    else:
                        MAX_ROWS = 50
                        warning_msg = ""
                        if len(rows) > MAX_ROWS:
                            warning_msg = f"\n\n... [SYSTEM WARNING: The query returned {len(rows)} rows, but only the first {MAX_ROWS} are shown to protect context. Use 'LIMIT' to paginate.]"
                            rows = rows[:MAX_ROWS]
                        
                        data = []
                        for row in rows:
                            row_dict = {}
                            for k in row.keys():
                                v = row[k]
                                if isinstance(v, (bytes, bytearray)) and len(v) > 64:
                                    row_dict[k] = f"<BLOB {len(v)} bytes>"
                                else:
                                    row_dict[k] = v
                            data.append(row_dict)

                        res_str = json.dumps(data, indent=2, default=str)
                        if len(res_str) > 20000 and len(data) > 1:
                            while len(res_str) > 20000 and len(data) > 1:
                                data = data[:max(1, len(data) // 2)]
                                res_str = json.dumps(data, indent=2, default=str)
                            res_str += f"\n\n... [SYSTEM WARNING: Output was truncated to {len(data)} rows to stay within 20,000 characters. Refine your query or use LIMIT.]"
                        else:
                            res_str += warning_msg
                            
                c.commit()
                logger.info(f"[query_sqlite_db] Success on '{os.path.basename(db_path)}' ({query[:80].strip()})")
                return res_str
            finally:
                if c:
                    c.close()

        return await asyncio.to_thread(_exec_sqlite)

    except Exception as e:
        clean_q = query[:120].replace('\n', ' ').strip()
        logger.error(f"[query_sqlite_db] Error on '{os.path.basename(db_path)}' query [{clean_q}]: {str(e)}")
        return f"SYSTEM ERROR: Database Exception on query [{clean_q}]: {str(e)}"


@mcp.tool()
async def batch_generate_embeddings(
    db_path: str,
    vec_table: Optional[str] = None,
    source_query: Optional[str] = None,
    target_table: Optional[str] = None,
    text_template: Optional[str] = None
) -> str:
    """
    Generates vector embeddings in bulk natively from the database and inserts them into a vec0 virtual table.
    This completely bypasses the context window!
    'source_query' MUST be a SELECT statement returning EXACTLY two columns:
    1. The integer ID (which maps to the vec_table's rowid).
    2. The text string to be embedded.
    Example: "SELECT id, description FROM tools WHERE id NOT IN (SELECT rowid FROM tools_vec)"
    """
    effective_vec_table = (vec_table or target_table or "").strip()
    if not effective_vec_table:
        return "SYSTEM ERROR: 'vec_table' (or 'target_table') must be specified."

    if not source_query or not source_query.strip():
        return "SYSTEM ERROR: 'source_query' must be provided."

    # HARDENING: Validate table names to prevent SQL injection compilation tricks
    if not re.match(r'^[a-zA-Z0-9_]+$', effective_vec_table):
        return "SYSTEM ERROR: Invalid characters detected in table name parameters."
    
    # Validate database path is inside workspace
    resolved_db = os.path.abspath(db_path)
    if not (resolved_db == "/app/workspace" or resolved_db.startswith("/app/workspace/")):
        return "SYSTEM ERROR: Database path must be inside /app/workspace"

    if not source_query.strip().upper().startswith("SELECT"):
        return "SYSTEM ERROR: 'source_query' must be a SELECT statement."
        
    conn = None # Initialize empty so the finally block doesn't crash

    try:
        # 1. Connect and LOAD THE VECTOR EXTENSION IMMEDIATELY
        conn = sqlite3.connect(db_path)
        conn.set_authorizer(sqlite_authorizer)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        cursor = conn.cursor()
        
        # Now it is safe to execute queries that reference virtual vec0 tables!
        cursor.execute(source_query)
        rows = cursor.fetchall()
        
        if not rows:
            return "SUCCESS: source_query returned 0 rows. Nothing to embed."
            
        rowids = []
        texts_to_embed = []
        col_names = [d[0] for d in cursor.description] if cursor.description else []
        for row in rows:
            if text_template:
                if len(row) < 1:
                    return "SYSTEM ERROR: source_query must return at least 1 column (id) when using text_template."
                rowids.append(row[0])
                row_map = {col_names[i]: row[i] for i in range(min(len(col_names), len(row)))}
                try:
                    texts_to_embed.append(text_template.format(*row, **row_map))
                except Exception:
                    texts_to_embed.append(" ".join(str(x) for x in row[1:]))
            else:
                if len(row) != 2:
                    return f"SYSTEM ERROR: source_query must return exactly 2 columns (id, text). Yours returned {len(row)}."
                rowids.append(row[0])
                texts_to_embed.append(str(row[1]))
            
        # 2. Generate Embeddings via API in safe chunks of 128
        BATCH_SIZE = 128
        inserted_count = 0
        total_tokens_in = 0

        for chunk_start in range(0, len(texts_to_embed), BATCH_SIZE):
            chunk_texts = texts_to_embed[chunk_start:chunk_start + BATCH_SIZE]
            chunk_rowids = rowids[chunk_start:chunk_start + BATCH_SIZE]

            response = await embedding_client.embeddings.create(
                model=config.EMBEDDING_CONFIG["model"],
                input=chunk_texts
            )

            if response.usage and response.usage.prompt_tokens:
                total_tokens_in += response.usage.prompt_tokens
            else:
                total_tokens_in += sum(len(tokenizer.encode(t)) for t in chunk_texts)

            # Insert batch into vec table
            for i, rowid in enumerate(chunk_rowids):
                embedding_vector = response.data[i].embedding
                vector_blob = array.array('f', embedding_vector).tobytes()
                try:
                    cursor.execute(f"INSERT INTO {effective_vec_table}(rowid, embedding) VALUES (?, ?)", (rowid, vector_blob))
                except (sqlite3.IntegrityError, sqlite3.OperationalError) as insert_err:
                    if "UNIQUE constraint failed" in str(insert_err):
                        cursor.execute(f"UPDATE {effective_vec_table} SET embedding = ? WHERE rowid = ?", (vector_blob, rowid))
                    else:
                        raise insert_err
                inserted_count += 1

        config.log_token_usage(STATE_DIR, "embedding", total_tokens_in, 0, 0)
        conn.commit()
        return f"SUCCESS: Natively extracted and embedded {inserted_count} rows into '{effective_vec_table}'."
        
    except Exception as e:
        return f"SYSTEM ERROR: Batch Embedding Failed. {str(e)}"
    finally:
        if conn:
            conn.close() # GUARANTEES the lock is released!

@mcp.tool()
async def search_web(query: str, max_results: int = 5) -> str:
    """Searches the web using a stealth browser to find relevant URLs and snippets.
    ALWAYS use this tool first to find factual URLs before using fetch_webpage.
    Do NOT guess or fabricate URLs.
    """
    # We use DuckDuckGo's legacy HTML page because it lacks advanced JS bot-detection
    url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
    
    try:
        # Spin up the exact same stealth browser you use for fetch_webpage
        async with Stealth().use_async(async_playwright()) as p:
            browser = await p.chromium.launch(
                headless=True, 
                args=[
                    '--no-sandbox', 
                    '--disable-setuid-sandbox', 
                    '--disable-dev-shm-usage',
                    '--disable-blink-features=AutomationControlled' 
                ]
            )
            try:
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    viewport={"width": 1920, "height": 1080},
                    locale="en-US"
                )
                
                page = await context.new_page()
                
                # Navigate to the search page
                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                html_content = await page.content()
            finally:
                await browser.close()
            
        # Parse the raw HTML using BeautifulSoup
        soup = BeautifulSoup(html_content, 'html.parser')
        results = soup.find_all('div', class_='result')
        
        if not results:
            return f"SYSTEM ERROR: No results found for '{query}'. The stealth browser may have been served a Captcha."
            
        formatted_results = f"--- SEARCH RESULTS FOR '{query}' ---\n\n"
        count = 0
        limit = int(max_results) if max_results is not None else 5
        
        for row in results:
            if count >= limit:
                break
                
            title_tag = row.find('a', class_='result__a')
            snippet_tag = row.find('a', class_='result__snippet')
            url_tag = row.find('a', class_='result__url')
            
            if title_tag and snippet_tag and url_tag:
                title = title_tag.text.strip()
                snippet = snippet_tag.text.strip()
                
                # Extract the real destination URL from the redirect link if available
                actual_url = ""
                raw_href = title_tag.get('href', '')
                if "uddg=" in raw_href:
                    try:
                        qs = urllib.parse.parse_qs(urllib.parse.urlparse(raw_href).query)
                        if "uddg" in qs and qs["uddg"]:
                            actual_url = qs["uddg"][0]
                    except Exception:
                        pass
                        
                if not actual_url:
                    actual_url = url_tag.text.strip()
                    # Clean up breadcrumbs or extra spaces if present
                    actual_url = actual_url.split()[0]
                    if not actual_url.startswith("http"):
                        actual_url = "https://" + actual_url
                    
                formatted_results += f"{count+1}. {title}\nURL: {actual_url}\nSnippet: {snippet}\n\n"
                count += 1
                
        if count == 0:
            return f"SYSTEM ERROR: Could not extract valid links from the search page."
            
        return formatted_results

    except Exception as e:
        return f"SYSTEM ERROR: Stealth search failed. {str(e)}"


def is_safe_web_url(url: str) -> tuple[bool, str]:
    """Validates that a URL is safe to fetch and does not probe local/host networks."""
    try:
        parsed = urllib.parse.urlparse(url.strip())
        if parsed.scheme.lower() not in ("http", "https"):
            return False, f"Scheme '{parsed.scheme}' is blocked. Only http and https are permitted."
        
        hostname = parsed.hostname
        if not hostname:
            return False, "Missing hostname in URL."
            
        lower_host = hostname.lower()
        if lower_host in ("localhost", "host.containers.internal", "host.docker.internal"):
            return False, f"Host alias '{hostname}' is restricted."

        try:
            addr_info = socket.getaddrinfo(hostname, None)
        except socket.gaierror as e:
            return False, f"DNS resolution failed for '{hostname}': {str(e)}"

        for _, _, _, _, sockaddr in addr_info:
            ip = ipaddress.ip_address(sockaddr[0])
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_multicast
            ):
                return False, f"Target resolves to restricted private/internal IP ({sockaddr[0]})."

        return True, ""
    except Exception as e:
        return False, f"Invalid URL: {str(e)}"


@mcp.tool()
async def fetch_webpage(url: str) -> str:
    """Fetches a webpage using a headless Chromium browser to render JavaScript, 
    and returns ONLY the clean, readable text. 
    Use this to read documentation, articles, or search results without writing a custom scraper.
    """
    safe, reason = await asyncio.to_thread(is_safe_web_url, url)
    if not safe:
        return f"SYSTEM ERROR: URL blocked by security policy. {reason}"

    try:
        async with Stealth().use_async(async_playwright()) as p:
            # Launch chromium natively in headless mode with container-safe flags
            browser = await p.chromium.launch(
                headless=True, 
                args=[
                    '--no-sandbox', 
                    '--disable-setuid-sandbox', 
                    '--disable-dev-shm-usage',
                    '--disable-blink-features=AutomationControlled' 
                ]
            )
            try:
                # Spoof a realistic Windows Chrome browser
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    viewport={"width": 1920, "height": 1080},
                    locale="en-US",
                    timezone_id="America/New_York"
                )
                
                page = await context.new_page()
                
                # Protect against redirect SSRF to internal/host IPs
                async def filter_navigation(route):
                    if route.request.is_navigation_request():
                        req_safe, _ = await asyncio.to_thread(is_safe_web_url, route.request.url)
                        if not req_safe:
                            await route.abort()
                            return
                    await route.continue_()

                await page.route("**/*", filter_navigation)
                            
                # Navigate and wait for the page to finish loading its network requests (JS rendering)
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                            
                # Extract the raw HTML after JS has executed
                html_content = await page.content()
            finally:
                await browser.close()
            
            # Use BeautifulSoup to aggressively strip out layout garbage
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # Extract scholarly and document metadata before decomposing tags
            scholarly_meta = []
            for meta_tag in soup.find_all('meta'):
                name = meta_tag.get('name', '').lower()
                prop = meta_tag.get('property', '').lower()
                content = meta_tag.get('content', '').strip()
                if content and (name.startswith('citation_') or name.startswith('dc.') or prop.startswith('og:') or name == 'description'):
                    meta_key = name if name else prop
                    scholarly_meta.append(f"{meta_key}: {content}")

            for tag in soup(['script', 'style', 'nav', 'footer', 'aside', 'header', 'meta', 'noscript', 'svg']):
                tag.decompose()
                
            # Extract just the readable text
            text = soup.get_text(separator='\n\n')
            
            # Prepend extracted metadata if present
            if scholarly_meta:
                meta_block = "--- SCHOLARLY / PAGE METADATA ---\n" + "\n".join(scholarly_meta[:25]) + "\n\n"
                text = meta_block + text

            # Clean up excessive newlines to protect the context window
            clean_text = re.sub(r'\n\s*\n', '\n\n', text).strip()
            
            # --- THE POINTER APPROACH (Context Protection) ---
            if len(clean_text) > 20000:
                # Create a safe filename based on the domain name
                parsed_url = urllib.parse.urlparse(url)
                safe_domain = re.sub(r'[^a-zA-Z0-9]', '_', parsed_url.hostname or "webpage")
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                temp_file_name = f"web_{safe_domain}_{timestamp}.txt"
                os.makedirs(SANDBOX_DIR, exist_ok=True)
                temp_file_path = os.path.join(SANDBOX_DIR, temp_file_name)
                
                # Save the full scraped text
                with open(temp_file_path, "w", encoding="utf-8") as f:
                    f.write(f"--- FULL CONTENT FROM {url} ---\n\n{clean_text}")
                    
                preview = clean_text[:6000]
                
                return (f"--- PREVIEW FROM {url} (First 6,000 of {len(clean_text):,} chars) ---\n\n{preview}\n\n"
                        f"... [SYSTEM CONTEXT GUARDRAIL: The webpage was {len(clean_text):,} characters long. To protect your context window, "
                        f"the full text was saved to '/app/workspace/sandbox/{temp_file_name}'.\n"
                        f"HOW TO GET AROUND THIS TRUNCATION: Inspect the full text using analyze_files(['/app/workspace/sandbox/{temp_file_name}'], ...) "
                        f"or bash tools: execute_bash('grep -n -C 3 <pattern> /app/workspace/sandbox/{temp_file_name}') or 'sed'.]")
                
            return f"--- CONTENT FROM {url} ---\n\n{clean_text}"

    except Exception as e:
        return f"Failed to fetch {url}. Error: {str(e)}"


@mcp.tool()
async def analyze_files(
    filepaths: list[str], 
    instruction: str,
    start_line: int = 1,
    max_lines: int = None,
    tail_mode: bool = False,
    max_chars: int = None,
    filter_pattern: str = None,
    context_lines: int = 2,
    output_filepath: str = None
) -> str:
    """Delegates the analysis of multiple massive text files, logs, images, audio, video, or PDFs to the Analyst LLM.
    Use this to prevent large files from blowing out your context window, or to compare multiple files.
    
    Parameters:
    - filepaths: List of absolute paths to the files (text, images, audio, video, or PDFs).
    - instruction: Specific question or analysis directive (e.g., "Find all error root causes and timeout occurrences").
    - filter_pattern: Optional regex or keyword pattern to filter massive files (e.g., "ERROR|Traceback|TimeoutException"). When specified, extracts all matching lines and surrounding context lines with line numbers instead of reading sequentially.
    - context_lines: Number of surrounding lines of context to include around each match when filter_pattern is set (defaults to 2).
    - start_line: 1-indexed line number to start reading from when reading sequentially (defaults to 1).
    - max_lines: Maximum number of lines to read sequentially.
    - tail_mode: If True, reads from the end (tail) of the file up to the character budget. Perfect for inspecting recent errors in large log files!
    - max_chars: Optional manual character limit per file. If omitted, the budget dynamically scales up to 80% of the Analyst's context window (~280,000 characters / ~70,000 tokens for a single file).
    - output_filepath: Optional path in /app/workspace/outputs/ or /app/workspace/sandbox/ to save the Analyst's output directly to disk (ideal when requesting structured JSON, CSV, or standalone Markdown artifacts).
    """
    start_line = int(start_line) if start_line is not None else 1
    max_lines = int(max_lines) if max_lines is not None else None
    max_chars = int(max_chars) if max_chars is not None else None
    context_lines = int(context_lines) if context_lines is not None else 2

    resolved_out = None
    if output_filepath and str(output_filepath).strip():
        resolved_out = os.path.realpath(str(output_filepath).strip())
        allowed_out = [os.path.realpath(OUTPUTS_DIR), os.path.realpath(SANDBOX_DIR)]
        if not any(os.path.commonpath([resolved_out, r]) == r for r in allowed_out):
            return "SYSTEM ERROR: 'output_filepath' in analyze_files must be inside /app/workspace/outputs or /app/workspace/sandbox."
        os.makedirs(os.path.dirname(resolved_out), exist_ok=True)

    if isinstance(filepaths, str):
        filepaths = [filepaths]

    api_args = analyst_profile["api_params"].copy()
    api_args["model"] = analyst_profile["model"]
    
    # We build a multi-part message array
    user_content = [{"type": "text", "text": f"Instruction: {instruction}\n\n"}]
    truncated_files_info = []
    
    try:
        # Pre-flight check: if all requested existing files are empty (0 bytes), return immediately
        existing_files = [f for f in filepaths if os.path.exists(f) and not os.path.isdir(f)]
        if existing_files and all(os.path.getsize(f) == 0 for f in existing_files):
            file_names = ", ".join([os.path.basename(f) for f in existing_files])
            return f"SYSTEM NOTICE: Target file(s) [{file_names}] are currently 0 bytes (empty). No content or tracebacks exist to analyze."

        # Dynamically scale the per-file ingestion budget based on Analyst profile capacity (80% safe ceiling)
        max_context = analyst_profile.get("context_window", config.MAX_CONTEXT_TOKENS)
        safe_budget = int(max_context * 0.80)  # Conservative 80% ceiling to account for tokenizer variance & report response room
        
        available_text_tokens = max(10000, safe_budget - 5000)
        total_char_budget = int(available_text_tokens * 3.2)  # Conservative 3.2 chars/token
        num_text_files = max(1, len([f for f in filepaths if os.path.exists(f) and not os.path.isdir(f) and not (mimetypes.guess_type(f)[0] and (mimetypes.guess_type(f)[0].startswith('image/') or mimetypes.guess_type(f)[0].startswith('audio/') or mimetypes.guess_type(f)[0].startswith('video/')))]))
        allocated_chars = max(40000, min(280000, total_char_budget // num_text_files))
        effective_max_chars = max_chars if (max_chars and max_chars > 0) else allocated_chars

        allowed_roots = [d for d in [WORKSPACE_DIR, "/app/workspace", "/app/host_input", "/app"] if os.path.exists(d)]
        for raw_path in filepaths:
            filepath = raw_path.strip() if isinstance(raw_path, str) else str(raw_path)
            resolved_fp = os.path.realpath(filepath)
            if allowed_roots and not any(os.path.commonpath([resolved_fp, d]) == d for d in allowed_roots):
                user_content.append({"type": "text", "text": f"\n[ACCESS DENIED: Path '{os.path.basename(resolved_fp)}' falls outside safe workspace boundaries.]"})
                continue
            filepath = resolved_fp
            if not os.path.exists(filepath):
                user_content.append({"type": "text", "text": f"\n[ERROR: File '{filepath}' does not exist.]"})
                continue

            if os.path.isdir(filepath):
                user_content.append({"type": "text", "text": f"\n[ERROR: Path '{filepath}' is a directory, not a file. Use bash tools (ls, tree) to inspect directories.]"})
                continue

            mime_type, _ = mimetypes.guess_type(filepath)
            mime_str = (mime_type or "").lower()
            is_image = mime_str.startswith('image/') or filepath.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'))
            is_audio = mime_str.startswith('audio/') or filepath.lower().endswith(('.wav', '.mp3', '.m4a', '.ogg', '.flac', '.aac'))
            is_video = mime_str.startswith('video/') or filepath.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm'))
            is_pdf = mime_str == 'application/pdf' or filepath.lower().endswith('.pdf')
            
            filename = os.path.basename(filepath)
            file_size = os.path.getsize(filepath)

            if is_image:
                # --- IMAGE PIPELINE ---
                max_media_size = 10 * 1024 * 1024
                if file_size > max_media_size:
                    user_content.append({"type": "text", "text": f"\n[ERROR: Image '{filename}' is too massive ({file_size / (1024*1024):.1f} MB). Max size is 10 MB.]"})
                    continue
                with open(filepath, "rb") as mf:
                    b64_data = base64.b64encode(mf.read()).decode('utf-8')
                actual_mime = mime_str if mime_str.startswith('image/') else "image/png"
                user_content.append({"type": "text", "text": f"\n--- IMAGE: {filename} ---"})
                user_content.append({"type": "image_url", "image_url": {"url": f"data:{actual_mime};base64,{b64_data}"}})

            elif is_video:
                # --- VIDEO PIPELINE (Sample keyframe via ffmpeg) ---
                thumb_path = os.path.join(SANDBOX_DIR, f".thumb_{filename}.jpg")
                video_meta = ""
                try:
                    probe = subprocess.run(
                        ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_name,width,height", "-of", "default=noprint_wrappers=1", filepath],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10
                    )
                    if probe.returncode == 0 and probe.stdout.strip():
                        video_meta = f"Metadata: {probe.stdout.strip().replace(chr(10), ', ')}"
                except Exception:
                    pass

                try:
                    os.makedirs(SANDBOX_DIR, exist_ok=True)
                    subprocess.run(
                        ["ffmpeg", "-y", "-ss", "00:00:01", "-i", filepath, "-vframes", "1", "-q:v", "2", thumb_path],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20
                    )
                except Exception:
                    pass

                if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
                    with open(thumb_path, "rb") as tf:
                        b64_thumb = base64.b64encode(tf.read()).decode('utf-8')
                    header = f"\n--- VIDEO: {filename} (Keyframe sampled at 1s via ffmpeg) ---"
                    if video_meta:
                        header += f"\n[{video_meta}]"
                    user_content.append({"type": "text", "text": header})
                    user_content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_thumb}"}})
                else:
                    user_content.append({
                        "type": "text",
                        "text": f"\n--- VIDEO: {filename} ({file_size:,} bytes) ---\n[NOTICE: Video file detected. {video_meta} No image keyframe could be extracted.]"
                    })

            elif is_audio:
                # --- AUDIO PIPELINE (Metadata + Whisper transcription routing notice) ---
                audio_meta = ""
                try:
                    probe = subprocess.run(
                        ["ffprobe", "-v", "error", "-show_entries", "format=duration,bit_rate:stream=codec_name,channels,sample_rate", "-of", "default=noprint_wrappers=1", filepath],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10
                    )
                    if probe.returncode == 0 and probe.stdout.strip():
                        audio_meta = f"Metadata: {probe.stdout.strip().replace(chr(10), ', ')}"
                except Exception:
                    pass
                user_content.append({
                    "type": "text",
                    "text": (
                        f"\n--- AUDIO FILE: {filename} ({file_size:,} bytes) ---\n"
                        f"[NOTICE: Raw audio waveforms cannot be directly ingested by LLM vision endpoints. {audio_meta}\n"
                        f"To transcribe audio content, use `execute_rest_call` against Whisper at `/v1/audio/transcriptions` (e.g. via `files={{'file': '{filepath}'}}`) or run an in-container transcription script.]\n"
                    )
                })

            elif is_pdf:
                # --- PDF PIPELINE (Auto-extract text via pdftotext / pypdf) ---
                extracted_txt_path = os.path.join(SANDBOX_DIR, f".extracted_{filename}.txt")
                extracted_text = ""
                try:
                    res = subprocess.run(["pdftotext", filepath, "-"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
                    if res.returncode == 0 and res.stdout.strip():
                        extracted_text = res.stdout
                except Exception:
                    pass

                if not extracted_text:
                    try:
                        import pypdf
                        reader = pypdf.PdfReader(filepath)
                        pages_text = [page.extract_text() or "" for page in reader.pages]
                        extracted_text = "\n\n".join(pages_text).strip()
                    except Exception:
                        pass

                if not extracted_text:
                    try:
                        import PyPDF2
                        reader = PyPDF2.PdfReader(filepath)
                        pages_text = [page.extract_text() or "" for page in reader.pages]
                        extracted_text = "\n\n".join(pages_text).strip()
                    except Exception:
                        pass

                if not extracted_text:
                    user_content.append({
                        "type": "text",
                        "text": f"\n--- PDF DOCUMENT: {filename} ({file_size:,} bytes) ---\n[SYSTEM NOTICE: No extractable text found in '{filename}'. It may be a scanned image or empty. To inspect scanned documents, extract pages as images or run OCR.]\n"
                    })
                    continue

                os.makedirs(SANDBOX_DIR, exist_ok=True)
                with open(extracted_txt_path, "w", encoding="utf-8") as pf:
                    pf.write(extracted_text)

                target_text_file = extracted_txt_path
                eff_file_size = len(extracted_text)
                
                if filter_pattern:
                    content, total_matches, included_matches, total_lines, is_partial = filter_file_lines(
                        target_text_file, filter_pattern, context_lines=context_lines, max_chars=effective_max_chars
                    )
                    if is_partial:
                        pct = max(1, int((included_matches / total_matches) * 100)) if total_matches > 0 else 100
                        truncated_files_info.append(
                            f"'{filename}' (PDF) [FILTER: '{filter_pattern}'] ({included_matches} of {total_matches} matches, {len(content):,} chars, {pct}%)"
                        )
                        coverage_header = (
                            f"[FILTERED EXTRACTION FROM PDF: Pattern '{filter_pattern}' matched {total_matches} occurrences across {total_lines:,} total lines.\n"
                            f"INCLUDED IN THIS WINDOW: First {included_matches} matches ({len(content):,} chars, {pct}% coverage). Remaining {total_matches - included_matches} matches were omitted to protect context window limits.\n"
                            f"MANDATORY INSTRUCTION FOR ANALYST:\n"
                            f"- State this filtered coverage explicitly in your executive summary.\n"
                            f"- CRITICAL EPISTEMIC GUARD: This is a FILTERED SUBSET for '{filter_pattern}'. Unmatched lines are omitted. Do NOT assume that functions, variables, or content absent here do not exist in the full document. State: 'Not found within filtered matches; verify without filters.'\n"
                            f"- Do NOT assume what occurred in the omitted matches.]\n"
                        )
                    else:
                        coverage_header = (
                            f"[FILTERED EXTRACTION FROM PDF: Pattern '{filter_pattern}' matched {total_matches} occurrences across {total_lines:,} total lines (100% of matches included, {len(content):,} chars).\n"
                            f"CRITICAL EPISTEMIC GUARD: Only lines matching '{filter_pattern}' and context (±{context_lines} lines) are displayed. Do NOT conclude that symbols absent from this subset do not exist in the full document. Matching lines are prefixed with '>>>'.]\n"
                        )
                    content = coverage_header + content
                    user_content.append({"type": "text", "text": f"\n--- PDF DOCUMENT (FILTERED TEXT): {filename} ---\n{content}\n"})
                else:
                    content, start_idx, end_idx, total_lines, is_partial = read_file_slice(
                        target_text_file, start_line=start_line, max_lines=max_lines, tail_mode=tail_mode, max_chars=effective_max_chars
                    )
                    if is_partial:
                        pct = max(1, int((len(content) / eff_file_size) * 100)) if eff_file_size > 0 else 100
                        truncated_files_info.append(f"'{filename}' (PDF) (read lines {start_idx}–{end_idx} of {total_lines:,}, {len(content):,}/{eff_file_size:,} chars, {pct}%)")
                        unread_desc = f"lines 1–{max(1, start_idx - 1):,}" if tail_mode else f"lines {end_idx + 1}–{total_lines:,}"
                        coverage_header = (
                            f"[COVERAGE BOUNDARY: Read lines {start_idx} to {end_idx} of {total_lines:,} total lines "
                            f"({len(content):,} of {eff_file_size:,} chars, {pct}% coverage). "
                            f"Lines outside this window were omitted to protect context window limits.\n"
                            f"MANDATORY INSTRUCTION FOR ANALYST:\n"
                            f"- State this crop/coverage explicitly in your executive summary.\n"
                            f"- Do NOT assume, extrapolate, or hallucinate events or errors in unread lines ({unread_desc}).]\n"
                        )
                        content = coverage_header + content + f"\n... [END OF INGESTED SLICE (lines {start_idx}–{end_idx} of {total_lines:,})] ..."

                    user_content.append({"type": "text", "text": f"\n--- PDF DOCUMENT (EXTRACTED TEXT): {filename} ---\n{content}\n"})

            else:
                # --- TEXT PIPELINE ---
                if file_size == 0:
                    user_content.append({"type": "text", "text": f"\n--- TEXT FILE: {filename} (EMPTY - 0 bytes) ---\n[This file is currently 0 bytes / empty. No data has been written to it.]\n"})
                    continue

                if filter_pattern:
                    content, total_matches, included_matches, total_lines, is_partial = filter_file_lines(
                        filepath, filter_pattern, context_lines=context_lines, max_chars=effective_max_chars
                    )
                    if is_partial:
                        pct = max(1, int((included_matches / total_matches) * 100)) if total_matches > 0 else 100
                        truncated_files_info.append(
                            f"'{filename}' [FILTER: '{filter_pattern}'] ({included_matches} of {total_matches} matches, {len(content):,} chars, {pct}%)"
                        )
                        coverage_header = (
                            f"[FILTERED EXTRACTION: Pattern '{filter_pattern}' matched {total_matches} occurrences across {total_lines:,} total lines.\n"
                            f"INCLUDED IN THIS WINDOW: First {included_matches} matches ({len(content):,} chars, {pct}% coverage). Remaining {total_matches - included_matches} matches were omitted to protect context window limits.\n"
                            f"MANDATORY INSTRUCTION FOR ANALYST:\n"
                            f"- State this filtered coverage explicitly in your executive summary.\n"
                            f"- CRITICAL EPISTEMIC GUARD: This is a FILTERED WINDOW showing ONLY matches for '{filter_pattern}'. Do NOT assume or report that functions, variables, or errors absent from these matches do not exist in the full file! State: 'Not found within filtered matches; verify by inspecting without filters.'\n"
                            f"- Do NOT assume what occurred in the omitted {total_matches - included_matches} matches.\n"
                            f"- If your analysis requires inspecting the remaining matches or other sections, explicitly advise the Brain in your next steps to narrow the filter pattern or inspect specific line ranges with `start_line`.]\n"
                        )
                    else:
                        coverage_header = (
                            f"[FILTERED EXTRACTION: Pattern '{filter_pattern}' matched {total_matches} occurrences across {total_lines:,} total lines (100% of matches included, {len(content):,} chars).\n"
                            f"CRITICAL EPISTEMIC GUARD: Only lines matching '{filter_pattern}' and context (±{context_lines} lines) are displayed. Do NOT conclude that symbols absent from this subset do not exist in the file. Non-matching sections were omitted.\n"
                            f"Matching lines are prefixed with '>>>'.]\n"
                        )
                    content = coverage_header + content
                    user_content.append({"type": "text", "text": f"\n--- TEXT FILE (FILTERED): {filename} ---\n{content}\n"})
                else:
                    content, start_idx, end_idx, total_lines, is_partial = read_file_slice(
                        filepath, start_line=start_line, max_lines=max_lines, tail_mode=tail_mode, max_chars=effective_max_chars
                    )

                    if is_partial:
                        pct = max(1, int((len(content) / file_size) * 100)) if file_size > 0 else 100
                        truncated_files_info.append(f"'{filename}' (read lines {start_idx}–{end_idx} of {total_lines:,}, {len(content):,}/{file_size:,} chars, {pct}%)")
                        unread_desc = f"lines 1–{max(1, start_idx - 1):,}" if tail_mode else f"lines {end_idx + 1}–{total_lines:,}"
                        
                        coverage_header = (
                            f"[COVERAGE BOUNDARY: Read lines {start_idx} to {end_idx} of {total_lines:,} total lines "
                            f"({len(content):,} of {file_size:,} chars, {pct}% coverage). "
                            f"Lines outside this window were omitted to protect context window limits.\n"
                            f"MANDATORY INSTRUCTION FOR ANALYST:\n"
                            f"- State this crop/coverage explicitly in your executive summary.\n"
                            f"- Do NOT assume, extrapolate, or hallucinate events or errors in unread lines ({unread_desc}).\n"
                            f"- If your analysis requires inspecting unread parts of this file, explicitly advise the Brain in your next steps to retrieve subsequent chunks (e.g. `analyze_files(..., start_line={end_idx + 1})`), inspect the tail (`tail_mode=True`), or apply a targeted filter (`filter_pattern='...'`).]\n"
                        )
                        content = coverage_header + content + f"\n... [END OF INGESTED SLICE (lines {start_idx}–{end_idx} of {total_lines:,})] ..."

                    user_content.append({"type": "text", "text": f"\n--- TEXT FILE: {filename} ---\n{content}\n"})

        api_args["messages"] = [
            {"role": "system", "content": config.PROMPTS["analyst_system"]},
            {"role": "user", "content": user_content}
        ]
        
        # --- DYNAMIC PAYLOAD CHECKER ---
        # 1. Accurately measure what we are about to send
        payload_tokens = get_payload_tokens(api_args["messages"])
        
        # 2. Bounce the request back to the Brain if it's too massive
        if payload_tokens > safe_budget:
            return (f"SYSTEM ERROR: The data sent to the Analyst is too massive! "
                    f"Your payload is {payload_tokens} tokens, but the safe 80% limit is {safe_budget} tokens. "
                    f"Please run 'analyze_files' with 'filter_pattern' (e.g. filter_pattern='ERROR|Traceback'), "
                    f"or use 'start_line'/'max_lines' to analyze in smaller slices.")
                    
        # --- 1. FORCE THE JSON SCHEMA (unless direct file output is requested via output_filepath) ---
        if not resolved_out:
            api_args["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "analyst_report_schema",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "executive_summary": {
                                "type": "string", 
                                "description": "A 1-3 sentence definitive answer or core conclusion."
                            },
                            "full_report": {
                                "type": "string", 
                                "description": "The exhaustive, detailed analysis and breakdown in full. NEVER use placeholders such as '[JSON array follows]' or '[content omitted]'—always emit the complete requested content inside this field."
                            }
                        },
                        "required": ["executive_summary", "full_report"],
                        "additionalProperties": False
                    }
                }
            }

        if config.VERBOSITY_MODE != "silent":
            sys.stderr.write(f"\n\033[93m[System: Analyst payload is ~{payload_tokens} estimated tokens]\033[0m\n")

        # Call the Analyst Model
        logger.info(f"[analyze_files] Delegating analysis of {[os.path.basename(f) for f in filepaths]}")
        response = await analyst_client.chat.completions.create(**api_args)
        analyst_thinking, raw_analyst_content = extract_thinking_and_content(response.choices[0].message)
        finish_reason = getattr(response.choices[0], 'finish_reason', None) if response.choices else None
        if finish_reason == "length":
            logger.warning(f"[analyze_files] Analyst generation was truncated by max_tokens limit")
        
        # Log token usage with fallback
        tokens_in = response.usage.prompt_tokens if (response.usage and response.usage.prompt_tokens) else get_payload_tokens(api_args["messages"])
        tokens_out = response.usage.completion_tokens if (response.usage and response.usage.completion_tokens) else (len(tokenizer.encode(raw_analyst_content)) if tokenizer else len(raw_analyst_content) // 4)
        thinking_tokens = getattr(response.usage.completion_tokens_details, 'reasoning_tokens', 0) if response.usage and hasattr(response.usage, 'completion_tokens_details') and response.usage.completion_tokens_details else 0
        if thinking_tokens == 0 and analyst_thinking:
            thinking_tokens = len(analyst_thinking) // 4
        config.log_token_usage(STATE_DIR, "analyst", tokens_in, tokens_out, thinking_tokens)
        
        file_list = ", ".join([os.path.basename(f) for f in filepaths])
        saved_out_note = ""

        # --- 2. PARSE THE OUTPUT ---
        if resolved_out:
            clean_out = raw_analyst_content.strip()
            if resolved_out.lower().endswith(".json") and clean_out.startswith("```"):
                clean_out = re.sub(r"^```[a-zA-Z0-9_+-]*[ \t]*\r?\n?", "", clean_out)
                clean_out = re.sub(r"\r?\n?```\s*$", "", clean_out).strip()
            with open(resolved_out, "w", encoding="utf-8") as out_f:
                out_f.write(clean_out)
            saved_out_note = f"[Saved Analyst output directly to: '{resolved_out}' ({len(clean_out):,} chars)]\n"
            ex_summ = f"Analysis saved directly to '{resolved_out}' ({len(clean_out):,} chars)."
            full_rep = clean_out
            combined_text = f"{saved_out_note}\n{clean_out}"
        else:
            try:
                report_data = extract_json_payload(raw_analyst_content)
                ex_summ = report_data.get("executive_summary", "")
                full_rep = report_data.get("full_report", "")
            except json.JSONDecodeError:
                # Fallback just in case the JSON breaks
                ex_summ = "Failed to parse JSON."
                full_rep = raw_analyst_content
            combined_text = f"--- EXECUTIVE SUMMARY ---\n{ex_summ}\n\n--- DETAILED REPORT ---\n{full_rep}"

        if finish_reason == "length":
            combined_text += "\n\n[SYSTEM NOTICE: Analyst generation was cut short because it reached the max_tokens limit. Consider requesting a more concise format or auditing specific sections.]"
        
        # --- 3. AUTO-SAVE THE FULL COMBINED REPORT (pure report output) ---
        os.makedirs(REPORTS_DIR, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = f"{timestamp}_analyst_report.md"
        filepath = os.path.join(REPORTS_DIR, filename)
        
        report_body = combined_text
        if analyst_thinking:
            report_body = f"<details>\n<summary>Analyst Reasoning & Strategy</summary>\n\n{analyst_thinking}\n\n</details>\n\n{combined_text}"
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(report_body)
            
        # --- 4. YOUR DYNAMIC ROUTING LOGIC ---
        coverage_banner = ""
        if truncated_files_info:
            coverage_banner = (
                f"[COVERAGE ALERT: Partial read or filtered extraction applied to: {', '.join(truncated_files_info)}.\n"
                f"The Analyst ONLY analyzed the ingested window. Do NOT assume facts about unread lines.\n"
                f"HOW TO GET SUBSEQUENT CHUNKS OR TARGETED DATA:\n"
                f"- To inspect a subsequent slice: analyze_files(filepaths=[...], instruction='...', start_line=<N>)\n"
                f"- To inspect the tail of a log file: analyze_files(filepaths=[...], instruction='...', tail_mode=True)\n"
                f"- To extract specific keywords/errors: analyze_files(filepaths=[...], instruction='...', filter_pattern='<regex>')\n"
                f"- Or inspect via bash: execute_bash('sed -n \\'<start>,<end>p\\' <path>')]\n\n"
            )

        if len(combined_text) > 20000:
            # Return ONLY the summary / preview
            preview_body = ex_summ if not resolved_out else ( f"{saved_out_note}{full_rep[:2000]}\n\n... [Preview truncated]" )
            result_msg = (f"{coverage_banner}--- ANALYST EXECUTIVE SUMMARY FOR [{file_list}] ---\n{preview_body}\n\n"
                    f"... [SYSTEM ALERT: The detailed report was {len(combined_text):,} chars long. To protect your context window, "
                    f"the full analysis was saved to file '{filepath}'"
                    f"{f' (and written to {resolved_out})' if resolved_out else ''}.\n"
                    f"HOW TO GET AROUND THIS TRUNCATION: Inspect specific sections using 'analyze_files' or bash 'sed/grep' on '{resolved_out or filepath}'.]")
        else:
            # Return BOTH
            result_msg = f"{coverage_banner}--- ANALYST REPORT FOR [{file_list}] ---\n{combined_text}\n\n[SYSTEM: A backup of this report was saved to file '{filepath}' (do not use read_memory for state/reports files; inspect via bash or analyze_files if needed)]"

        if analyst_thinking:
            result_msg += f"\n<___ANALYST_THOUGHTS___>\n{analyst_thinking}\n</___ANALYST_THOUGHTS___>"
        result_msg += f"\n<___ANALYST_REPORT___>\n{combined_text}\n</___ANALYST_REPORT___>"
        return result_msg

    except Exception as e:
        logger.error(f"[analyze_files] Failed to process files: {str(e)}")
        trace_file = save_failure_trace("analyst", {
            "subagent": "analyst",
            "filepaths": filepaths,
            "instruction": instruction,
            "error": str(e),
            "traceback": traceback.format_exc(),
            "messages": api_args.get("messages", []) if 'api_args' in locals() else []
        })
        trace_note = f"\n[SYSTEM: Failure trace saved to '{trace_file}']" if trace_file else ""
        return f"Analyst failed to process files. Error: {str(e)}{trace_note}"
        
@mcp.tool()
def load_skill(skill_name: str = "") -> str:
    """Loads the full instructional blueprint for a given skill.
    If called without a skill_name (or empty string), it lists all available skills and their descriptions.
    Use this immediately when your current task matches a skill in your system prompt."""
    global _LOADED_SKILLS
    skills_root = os.path.realpath("/app/workspace/skills")
    
    # Helper to gather all available skills
    available_skills = {}
    if os.path.exists(skills_root):
        for item in sorted(os.listdir(skills_root)):
            s_path = os.path.join(skills_root, item, "SKILL.md")
            if os.path.exists(s_path):
                try:
                    with open(s_path, "r", encoding="utf-8") as f:
                        c = f.read()
                    available_skills[item] = config.extract_skill_description(c)
                except Exception:
                    available_skills[item] = "Available (failed to parse description)."

    # If no skill name provided, return the catalog
    if not skill_name or not skill_name.strip():
        if not available_skills:
            return "AVAILABLE SKILLS: None currently installed in /app/workspace/skills. Use create_skill to build some."
        summary_lines = ["AVAILABLE SKILLS REGISTRY (Call load_skill(skill_name) to activate):"]
        for s_name, s_desc in available_skills.items():
            summary_lines.append(f"- {s_name}: {s_desc}")
        return "\n".join(summary_lines)

    safe_name = os.path.basename(skill_name.strip())
    skill_path = os.path.realpath(os.path.join(skills_root, safe_name, "SKILL.md"))
    try:
        if not os.path.commonpath([skill_path, skills_root]) == skills_root:
            return f"[SYSTEM ERROR: Invalid skill name '{skill_name}'.]"
    except Exception:
        return f"[SYSTEM ERROR: Invalid skill name '{skill_name}'.]"

    if not os.path.exists(skill_path):
        avail_list = ", ".join(available_skills.keys()) if available_skills else "None"
        return f"[SYSTEM ERROR: Skill '{safe_name}' not found. Available skills: {avail_list}]"
        
    with open(skill_path, "r", encoding="utf-8") as f:
        content = f.read()
        
    skill_dir = os.path.dirname(skill_path)
    extra_items = [
        e + ("/" if os.path.isdir(os.path.join(skill_dir, e)) else "")
        for e in sorted(os.listdir(skill_dir))
        if e not in ("SKILL.md", "LICENSE.txt") and not e.startswith(".")
    ]
    resource_note = (
        f"\n[SKILL DIRECTORY: /app/workspace/skills/{safe_name} | Bundled resources: {', '.join(extra_items)}]"
        if extra_items else f"\n[SKILL DIRECTORY: /app/workspace/skills/{safe_name}]"
    )
    is_reload = safe_name in _LOADED_SKILLS
    _LOADED_SKILLS.add(safe_name)
    status_label = "RE-ACTIVATED" if is_reload else "ACTIVATED"
    return f"--- SKILL {status_label}: {safe_name} ---{resource_note}\n{content}\n\n[SYSTEM: You must now strictly follow these instructions.]"


@mcp.tool()
async def create_skill(skill_name: str, objective: str, brain_notes: str, context_filepaths: list[str] = None) -> str:
    """Use this immediately after solving a complex problem to permanently document it as a system skill.
    Passes raw notes to the Architect agent, who formats and saves it as a new Skill.
    'context_filepaths' can take target code implementations or terminal output histories to extract instructions from.
    """
    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '', os.path.basename(skill_name.strip()))
    if not safe_name:
        return "[ARCHITECT ERROR] Invalid skill name provided."

    # ◄--- Gather background reference metrics ---
    file_environmental_context = gather_agent_context(context_filepaths)

    architect_user = f"{file_environmental_context}Skill Name: {safe_name}\nObjective: {objective}\nBrain's Notes:\n{brain_notes}"
    
    try:
        messages = [
            {"role": "system", "content": config.SYSTEM_PROMPTS["architect"]},
            {"role": "user", "content": architect_user}
        ]

        max_context = architect_profile.get("context_window", config.MAX_CONTEXT_TOKENS)
        safe_budget = int(max_context * 0.90)
        payload_tokens = get_payload_tokens(messages)
        if payload_tokens > safe_budget:
            return (f"[ARCHITECT ERROR] The notes and attached context files sent to the Architect are too massive! "
                    f"Payload is {payload_tokens} tokens, but the safety limit is {safe_budget}. "
                    f"Please refine 'context_filepaths' or shorten 'brain_notes'.")
        
        if config.VERBOSITY_MODE != "silent":
            sys.stderr.write(f"\n\033[93m[System: Architect payload is ~{payload_tokens} estimated tokens]\033[0m\n")

        api_args = architect_profile.get("api_params", {}).copy()
        api_args["model"] = architect_profile["model"]
        api_args["messages"] = messages

        response = await architect_client.chat.completions.create(**api_args)
        architect_thinking, formatted_skill_md = extract_thinking_and_content(response.choices[0].message)
        
        # Log token usage with fallback
        tokens_in = response.usage.prompt_tokens if (response.usage and response.usage.prompt_tokens) else get_payload_tokens(api_args["messages"])
        tokens_out = response.usage.completion_tokens if (response.usage and response.usage.completion_tokens) else (len(tokenizer.encode(formatted_skill_md)) if tokenizer else len(formatted_skill_md) // 4)
        thinking_tokens = getattr(response.usage.completion_tokens_details, 'reasoning_tokens', 0) if response.usage and hasattr(response.usage, 'completion_tokens_details') and response.usage.completion_tokens_details else 0
        if thinking_tokens == 0 and architect_thinking:
            thinking_tokens = len(architect_thinking) // 4
        config.log_token_usage(STATE_DIR, "architect", tokens_in, tokens_out, thinking_tokens)
        
        clean_skill_md = formatted_skill_md.strip()
        # Only unwrap if the entire output was enclosed in top-level markdown fences
        if clean_skill_md.startswith("```"):
            clean_skill_md = re.sub(r"^```(?:markdown|md)?\s*\r?\n", "", clean_skill_md)
            clean_skill_md = re.sub(r"\r?\n```\s*$", "", clean_skill_md).strip()

        skill_dir = f"/app/workspace/skills/{safe_name}"
        os.makedirs(skill_dir, exist_ok=True)
        
        skill_path = os.path.join(skill_dir, "SKILL.md")
        with open(skill_path, "w", encoding="utf-8") as f:
            f.write(clean_skill_md)
            
        result_msg = f"[SUCCESS] The Architect has drafted and saved '{safe_name}' to {skill_path}. It is now immediately available in your Available Skills Menu and can be loaded via load_skill('{safe_name}')."
        if architect_thinking:
            result_msg += f"\n<___ARCHITECT_THOUGHTS___>\n{architect_thinking}\n</___ARCHITECT_THOUGHTS___>"
        result_msg += f"\n<___ARCHITECT_SKILL___>\n{clean_skill_md}\n</___ARCHITECT_SKILL___>"
        return result_msg
    except Exception as e:
        trace_file = save_failure_trace("architect", {
            "subagent": "architect",
            "skill_name": skill_name,
            "objective": objective,
            "brain_notes": brain_notes,
            "error": str(e),
            "traceback": traceback.format_exc(),
            "messages": api_args.get("messages", []) if 'api_args' in locals() else []
        })
        trace_note = f"\n[SYSTEM: Failure trace saved to '{trace_file}']" if trace_file else ""
        return f"[ARCHITECT ERROR] Failed to generate skill: {str(e)}{trace_note}"


if __name__ == "__main__":
    mcp.run()