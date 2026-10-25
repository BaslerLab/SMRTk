# SMRTk — Complete Development History & Architectural Evolution

> **From a Minimal Sandboxed Chat Harness (*AI-Forge-Sandbox*) to a Full-Scale Autonomous Multi-Agent Research Toolkit (Pre-Research Edition $\rightarrow$ v42.3)**

---

## Overview

**SMRTk** (*Sandboxed Multi-Agent Research Toolkit*) evolved across two major eras:
1. **Pre-Research Edition (*AI-Forge-Sandbox*, v1.0 – v2.1)**: Where the foundational zero-trust rootless Podman architecture, LiteLLM proxy airlock, hierarchical tool/memory registries, and the initial 3-agent split (*Brain, Coder, Summarizer*) were forged.
2. **Research Edition $\rightarrow$ SMRTk (Step 1 / v1 $\rightarrow$ Step 14 / v42.3)**: Where the sandbox evolved through **42 major and minor architectural iterations** (spanning over 100 incremental refinements) into a fault-tolerant, multi-modal, polyglot research platform capable of autonomous tool creation, zero-context vector database indexing, surgical Git-backed code editing, external MCP orchestration, headless stealth web research, structural biology visualization, and asynchronous multi-agent background execution.

This document chronicles **every major architectural milestone as well as the smaller engineering tweaks, bug fixes, security hardenings, and behavioral prompt refinements** in sequential order—highlighting **what was added** and **what new capabilities each step unlocked**.

---

## At-a-Glance Evolution Summary

| Metric / Capability | Pre-Research Edition (`AI-Forge-Sandbox`) | Initial Research Edition (Step 1 / v1) | Current SMRTk Architecture (Step 14 / v42.3) |
| :--- | :--- | :--- | :--- |
| **Core Files** | `chat_overseer.py`, `god_tools.py`, `config.py`, `Containerfile` | `chat_overseer.py`, `god_tools.py`, `config.py`, scientific `Containerfile` | `smrtk.py`, `sandbox_tools.py`, `config.py`, `llm_config.py`, `mcp_config.py`, `chimerax_mcp_server.py`, `Containerfile`, `setup_wsl2.sh`, `run_tests.sh` |
| **Specialized Agent Roles** | **3** (*Brain, Coder, Summarizer*) | **3** (*Brain, Coder, Summarizer*) | **8** (*Brain, Coder, Summarizer, Adviser, Analyst, Architect, Decider, Auxiliary/Universal LLM*) |
| **Built-in Sandbox Tools** | **6** (`view_tool_registry`, `execute_bash`, `view_memory_registry`, `read_memory`, `compress_and_store_context`, `forge_and_register_tool`) | **6** (same 6 tools + Bioconda & scientific CLI binaries) | **22** core tools + dynamic Host MCP tools + dynamic on-demand Host Skills |
| **Supported Tool Languages** | Python only (`py_compile`) | Python only (`py_compile`) | Polyglot: **Python, JavaScript, TypeScript, Rust, C++** with language-specific compilation & `--help` behavioral smoke tests |
| **Execution Concurrency** | Strictly synchronous, 1 tool at a time | Strictly synchronous, 1 tool at a time | **Parallel multi-tool execution waves** (up to 30/wave) + **18 concurrent background jobs** (9 Bash + 9 Sub-Agent tasks) |
| **Context Window Management** | 32k $\rightarrow$ 60k limit; 2-step strict JSON schema compaction | 60k limit; 2-part *Current State + Active Plan* compaction | **120k token budget** with live fuel gauge, dynamic tokenizer calibration, SHA-1 historical tool pruning (>10 turns), pointer attachments, and 2-step targeted handoff compaction |
| **Data & Multimodal Support** | Plain text stdout only (5,000-char truncation) | Plain text + pre-installed `pdftotext`, `tesseract`, `ffmpeg`, `pandoc` | **Images (Vision), PDFs, Video keyframes, Audio metadata**, SQLite + `sqlite-vec` vector search, headless DOM + scholarly metadata |
| **Security & Reliability** | Air-gapped WSL2 + Rootless Podman + LiteLLM API key proxy | Added NVIDIA GPU passthrough + 50% VRAM cap + system prompt re-injection | **Zero-Trust Sandbox**: C-level SQLite authorizer, SSRF DNS/IP firewall + Playwright route interception, bash `rm`/`unlink` overrides, 40% file-shrink guard, read-only container layers, and ChimeraX command firewall |

---

## Table of Contents

* [Pre-Research Edition (`AI-Forge-Sandbox`, v1.0 – v2.1): The Original Zero-Trust Sandbox, 3-Agent Split & Hierarchical Registries](#pre-research-edition-ai-forge-sandbox-v10--v21-the-original-zero-trust-sandbox-3-agent-split--hierarchical-registries)
1. [Step 1 (v1 – v2): Birth of the Research Edition — GPU Passthrough, Scientific Base Image, Bioconda & Autonomous Wake-Up](#step-1-v1--v2-birth-of-the-research-edition--gpu-passthrough-scientific-base-image-bioconda--autonomous-wake-up)
2. [Step 2 (v3): Master Planning, Strategic Adviser, Universal LLM & Async Sub-Agents](#step-2-v3-master-planning-strategic-adviser-universal-llm--async-sub-agents)
3. [Step 3 (v4 – v5): Layered Base+Delta Environment, Proactive Memory, Live Clock & Macro Loop Breaker](#step-3-v4--v5-layered-basedelta-environment-proactive-memory-live-clock--macro-loop-breaker)
4. [Step 4 (v6 – v7.1): Rich Terminal UI, Headless CLI Flags, Piped STDIN & Multi-Agent Swarm Mode](#step-4-v6--v71-rich-terminal-ui-headless-cli-flags-piped-stdin--multi-agent-swarm-mode)
5. [Step 5 (v8 – v9): SQLite + `sqlite-vec` Vector Search, Headless Web Fetching, `write_file` & Soft-Delete Archive](#step-5-v8--v9-sqlite--sqlite-vec-vector-search-headless-web-fetching-write_file--soft-delete-archive)
6. [Step 6 (v10 – v11.1): The Analyst (Vision & Log Miner), Pointer Architecture, Micro-Breaker & Self-Inspection](#step-6-v10--v111-the-analyst-vision--log-miner-pointer-architecture-micro-breaker--self-inspection)
7. [Step 7 (v12 – v15.2): Decoupled UI Modes, Payload Airlocks, Rolling Summarization, Stealth Web Search & Zero-Context Bulk Embeddings](#step-7-v12--v152-decoupled-ui-modes-payload-airlocks-rolling-summarization-stealth-web-search--zero-context-bulk-embeddings)
8. [Step 8 (v17 – v21): The Architect & Skills (`SKILL.md`), Atomic Token Telemetry, SQLite Authorizer, MCP Stdout Shield & Polyglot Tooling](#step-8-v17--v21-the-architect--skills-skillmd-atomic-token-telemetry-sqlite-authorizer-mcp-stdout-shield--polyglot-tooling)
9. [Step 9 (v22 – v25.6): In-Workspace Git Versioning, Surgical Code Editing, Pointer Context Attachments, Stream Loop Breaker, Full Sub-Agent Observability & SSRF Hardening](#step-9-v22--v256-in-workspace-git-versioning-surgical-code-editing-pointer-context-attachments-stream-loop-breaker-full-sub-agent-observability--ssrf-hardening)
10. [Step 10 (v26 – v28.5): Automated WSL2 Installer (`setup_wsl2.sh`), Long-Task Refinements, Behavioral Smoke Tests & `llm_config.py` Split](#step-10-v26--v285-automated-wsl2-installer-setup_wsl2sh-long-task-refinements-behavioral-smoke-tests--llm_configpy-split)
11. [Step 11 (v29 – v32.4): KV-Cache Prefix Preservation, Failure Forensics, Dynamic File Slicing/Regex Filtering, Continuous SHA-1 Tool Pruning & 2-Step Targeted Handoff Compaction](#step-11-v29--v324-kv-cache-prefix-preservation-failure-forensics-dynamic-file-slicingregex-filtering-continuous-sha-1-tool-pruning--2-step-targeted-handoff-compaction)
12. [Step 12 (v33 – v34.9): The Decider (`fast_decision`), External Host MCP Ecosystem (`mcp_config.py`, `manage_external_mcp`) & Hardened UCSF ChimeraX Bridge](#step-12-v33--v349-the-decider-fast_decision-external-host-mcp-ecosystem-mcp_configpy-manage_external_mcp--hardened-ucsf-chimerax-bridge)
13. [Step 13 (v35 – v38.1): Generalized REST Escape Hatch (`execute_rest_call`), Full Multimodal Pipelines, Telemetry & Fuel Gauge, and Concurrent Multi-Tool Execution Waves](#step-13-v35--v381-generalized-rest-escape-hatch-execute_rest_call-full-multimodal-pipelines-telemetry--fuel-gauge-and-concurrent-multi-tool-execution-waves)
14. [Step 14 (v39 – v42.3): SMRTk Architecture Refactor, Automated Benchmark Suite (`run_tests.sh`), On-Demand Host Skills Library & Asynchronous Background Execution](#step-14-v39--v423-smrtk-architecture-refactor-automated-benchmark-suite-run_testssh-on-demand-host-skills-library--asynchronous-background-execution)
15. [Complete Reference: Agent Roles, Tools & Safety Layers](#complete-reference-agent-roles-tools--safety-layers)

---

## Pre-Research Edition (`AI-Forge-Sandbox`, v1.0 – v2.1): The Original Zero-Trust Sandbox, 3-Agent Split & Hierarchical Registries

Before the specialized *Research Edition* was created, the project began as **`AI-Forge-Sandbox`**—an experimental zero-trust multi-agent framework designed to let an LLM safely write, debug, and execute its own Python tools inside an air-gapped Linux container.

### Main Milestones
* **Hardened WSL2 Bubble & Rootless Podman Sandbox (`chat_overseer.py` + `god_tools.py`)**:
  * Designed a two-tier isolation model: first running inside a dedicated, non-sudo WSL2 Ubuntu instance with Windows drive automounting and interop disabled (`/etc/wsl.conf`: `automount=false`, `interop=false`), and second spawning a detached **Rootless Podman** container (`ai-forge`) over the Model Context Protocol (`FastMCP` stdio).
  * Enforced strict container resource and privilege boundaries: `--network=slirp4netns`, `--security-opt=no-new-privileges:true`, `--cap-drop=ALL`, `--cpus=4.0`, `--memory=16g`, `--pids-limit=1000` (neutralizing fork bombs), and `--userns=keep-id`.
  * Created a surgical **I/O Airlock**: mounted `./my_host_input` strictly read-only at `/app/host_input:ro,Z` (so the AI could never delete or corrupt source data) and mounted an isolated per-session workspace (`sessions/Session_ID_<timestamp>/`) at `/app/workspace:Z` with dedicated `forged_tools/`, `memories/`, `histories/`, `sandbox/`, `outputs/`, `logs/`, and `state/` subdirectories.
  * **What this allowed**: Arbitrary bash and Python execution in a persistent session workspace with zero risk to the Windows host or user source files.
* **Zero-Trust Host Proxy Routing (LiteLLM)**:
  * Eliminated all API keys and `.env` secrets from the container. Configured `config.py` to use a dummy key (`sk-sandbox-fake-key`) pointed at `http://host.containers.internal:4000/v1`, where a host-side LiteLLM proxy attached real credentials and routed requests to local vLLM/Ollama servers or SSH-tunneled GPUs.
  * **What this allowed**: Mathematical certainty that sandboxed code could never exfiltrate API keys.
* **The Foundational 3-Agent Split & 6 Core MCP Tools**:
  * **The Brain (`chat_overseer.py`)**: An `AsyncOpenAI` streaming orchestrator using `prompt_toolkit` for interactive ANSI-colored chat, monitoring context usage (initially `32,768` tokens, warning at 50% and 90%), and dispatching 6 native MCP tools (`view_tool_registry`, `execute_bash`, `view_memory_registry`, `read_memory`, `compress_and_store_context`, `forge_and_register_tool`).
  * **The Coder (`forge_and_register_tool`)**: An invisible background coding agent that generated standalone Python scripts in `/app/workspace/forged_tools/`, sanitized filenames, and ran an automatic **3-attempt `py_compile` syntax verification loop** (incrementing the random `seed` on each retry) before registering the tool.
  * **The Summarizer (`compress_and_store_context`)**: A background memory manager using OpenAI Structured Outputs (`response_format` with `"strict": True` JSON schemas) in a 2-step pipeline:
    1. *Step 1 (Memory Extraction)*: Cross-referenced `memory_registry.json` to avoid duplicates and extracted new long-term facts into timestamped `.md` files in `/app/workspace/memories/`.
    2. *Step 2 (History Compression)*: Backed up the bloated conversation to `/app/workspace/histories/backup_history_<timestamp>.json` and replaced `current_history.json` with a compact summary.
  * **What this allowed**: Long-running sessions that never crashed from JSON schema syntax errors or compiler tracebacks polluting the Brain's context window.
* **2-Level Hierarchical Tool & Memory Registries**:
  * Organized `tool_registry.json` and `memory_registry.json` into top-level `category` + `category_description` maps. Calling `view_tool_registry()` or `view_memory_registry()` with no arguments returned only lightweight category summaries; passing a category name revealed the specific tools or memories inside.
  * **What this allowed**: Prevented registry bloat from overwhelming the Brain as it accumulated dozens of forged tools and memories.
* **Per-Session Pixi Micro-Environments**:
  * Automatically initialized `pixi init && pixi add python` inside every new `Session_ID` directory so third-party packages installed via `pixi add <package>` stayed strictly isolated to that session.

### Tweaks & Refinements (v1.0 $\rightarrow$ v2.1)
* **Hidden `<___CODER_THOUGHTS___>` & `<___CODER_CODE___>` Stream Interception**: Embedded sentinel tags in `forge_and_register_tool` returns so `chat_overseer.py` could display and log the Coder's full reasoning and source code on the host while stripping them out before the tool result entered the Brain's context window.
* **Self-Healing Malformed JSON Tool Calls**: Caught `json.JSONDecodeError` on tool arguments in `chat_overseer.py` and returned a structured `SYSTEM ERROR` tool response prompting the Brain to fix its JSON syntax automatically.
* **5,000-Character Bash Output Truncation & Redirection Prompt**: Intercepted `execute_bash` outputs exceeding 5,000 characters, returning a 1,000-char preview and instructing the Brain to re-run with `> filename.txt` and write a Python tool to parse the file.
* **Self-Healing Container Reconnection Loop**: Wrapped the host `stdio_client` session in a `while not quit_app:` recovery loop catching `BaseExceptionGroup` and dead container disconnects, sweeping stale `~/.podman-run` state, and seamlessly restarting the container without losing `current_history.json`.
* **FastMCP JSON-RPC Stream Protection & Subprocess Isolation (v2.1 — `37cedbb`)**:
  * Added `Containerfile` (`ENV FASTMCP_SHOW_SERVER_BANNER=0`) to the repository.
  * Suppressed third-party Python loggers (`logging.getLogger().setLevel(logging.CRITICAL)`, `httpx`, `openai`) in `god_tools.py` so rogue library logs printed to `stdout` would not corrupt the FastMCP JSON-RPC stream.
  * Hardened `execute_bash`'s `subprocess.run` with `stdin=subprocess.DEVNULL` (preventing child processes from stealing the MCP stdio input stream) and `start_new_session=True` (trapping background grandchild daemons like Playwright in an isolated process group so they couldn't hang the MCP server).
  * Raised `MAX_CONTEXT_TOKENS` from `32,768` to `60,000` tokens.

---

## Step 1 (v1 – v2): Birth of the Research Edition — GPU Passthrough, Scientific Base Image, Bioconda & Autonomous Wake-Up

### Main Milestones
* **Transition from `AI-Forge-Sandbox` to `AI-Forge: Research Edition`**:
  * Evolved the general-purpose `AI-Forge-Sandbox` into a dedicated **scientific and computational research harness** while preserving its rootless Podman + FastMCP + 3-Agent foundation.
* **NVIDIA GPU Hardware Passthrough & VRAM Guardrails**:
  * Added `--device=nvidia.com/gpu=all` to `podman run` in `chat_overseer.py` to expose the host's NVIDIA GPU directly to the container.
  * Added strict system prompt guardrails requiring generated PyTorch scripts to cap VRAM allocation at 50% (`torch.cuda.set_per_process_memory_fraction(0.5, 0)`) and inference engines to pass `--gpu-memory-utilization 0.5`.
  * **What this allowed**: Hardware-accelerated deep learning, structural biology, and GPU compute inside the sandbox without starving the host's own LLM server of VRAM.
* **Pre-Installed Scientific, Media & Document Linux Base Image (`Containerfile`)**:
  * Upgraded `Containerfile` to pre-install a rich suite of native Linux scientific and media binaries during image build: `chromium`, `xvfb` (virtual framebuffer for headless browser scraping), `git`, `git-lfs`, `aria2` (`aria2c` for high-speed dataset downloads), `jq`, `pigz` & `zstd` (multi-threaded decompression), `poppler-utils` (`pdftotext`), `tesseract-ocr`, `ffmpeg`, `graphviz`, `pandoc` (for Markdown-to-HTML/PDF/Word report generation), and native compilers (`build-essential`, `cmake`, `gfortran`, `libxml2-dev`, `libxslt-dev`).
* **Bioconda Channel Integration & Scientific API Directives**:
  * Upgraded per-session Pixi initialization in `chat_overseer.py` to automatically inject the **`bioconda`** channel (`pixi project channel add bioconda`) so bioinformatics packages (`samtools`, `blast`, etc.) could be installed out of the box.
  * Added the **"Python First" Directive** (forbidding brittle, massive `awk`/`sed`/`grep` bash one-liners in favor of modular, reusable Python tools) and instructed the Brain to query official literature APIs (Crossref, PubMed/NCBI E-utilities, Semantic Scholar).
* **Hardened 2-Part Context Compaction & Autonomous Wake-Up**:
  * Fixed a critical failure mode in `compress_and_store_context` where asking the Summarizer LLM to output the `"system"` prompt caused it to summarize or corrupt the Brain's core operating rules.
  * Restricted the Summarizer's JSON schema strictly to `["user", "assistant"]` messages, required the compressed user state to contain two explicit sections (**`1. Current State`** and **`2. Active Plan & Next Steps`**), and programmatically injected the exact, unedited `config.PROMPTS["overseer_system"]` at index `0` (`new_history.insert(0, ...)`).
  * Added the **`WAKING UP`** protocol instructing the Brain to immediately read its *Active Plan & Next Steps* after compaction and continue executing autonomously without waiting for user permission.

### Tweaks & Refinements
* **Context Warning Threshold Adjustment**: Raised the context warning trigger in `chat_overseer.py` from `50%` to `75%` of `MAX_CONTEXT_TOKENS` (60,000 tokens) so the Brain could complete longer multi-step research chains before compacting.
* **GPU-to-CPU Fallback Rule (v2 — `7fd031a`)**: Added an explicit system prompt rule instructing the Brain and Coder to fall back gracefully to CPU execution if the host machine lacked an NVIDIA GPU or if `--device=nvidia.com/gpu=all` was unavailable.

---

## Step 2 (v3): Master Planning, Strategic Adviser, Universal LLM & Async Sub-Agents

### Main Milestones
* **End-to-End Asynchronous Sub-Agent I/O (`AsyncOpenAI`)**:
  * Converted all inner sub-agent OpenAI client calls inside `god_tools.py` from synchronous `OpenAI` to `AsyncOpenAI` (`async def` / `await`).
  * **What this allowed**: Prevented blocking the FastMCP event loop during long sub-agent reasoning calls and laid the foundation for parallel sub-agent execution in later versions.
* **The Adviser Sub-Agent (`consult_adviser`)**:
  * Introduced a 4th specialized role—**The Adviser**—a deep reasoning model (`ADVISER_CONFIG`) that the Brain could consult for complex scientific, mathematical, or architectural strategy.
  * Every consultation automatically saved a timestamped Markdown report (`state/<timestamp>_advice.md`) and returned the strategic blueprint to the Brain.
  * **What this allowed**: Separated high-level scientific/algorithmic brainstorming from the Brain's step-by-step operational loop.
* **Persistent Master Plan (`manage_plan` & `active_plan.md`)**:
  * Added the `manage_plan(action, plan_content)` tool (`read` / `write`) backed by `/app/workspace/state/active_plan.md`.
  * **What this allowed**: Gave the Brain a persistent markdown checklist on disk that survived context compactions and kept multi-stage workflows on track.
* **Universal LLM Gateway (`query_universal_llm`)**:
  * Added a flexible sub-agent tool capable of querying any model available on the host's Ollama or LiteLLM backend (`action="list_models"` to discover models, `action="chat"` to prompt a specific `model_name`).
  * **What this allowed**: Enabled the Brain to dynamically recruit domain-specialized models, test alternative prompts, or run second-opinion checks.

### Tweaks & Refinements
* **3-Section Context Compaction**: Upgraded `compress_and_store_context` from 2 sections to 3 (`completed_work_and_facts`, `current_active_task_and_variables`, and `pending_user_input`) so unanswered user questions were never lost during memory resets.
* **Robust Markdown Code Extraction**: Replaced brittle `.strip("```python")` string slicing in the Coder with a regex extractor (`re.search(r"```(?:python)?\s*\n(.*?)```", ..., re.DOTALL)`) so conversational preamble from reasoning models never broke tool compilation.
* **Reasoning Tag Extraction (`<think>`)**: Added automatic parsing for `<think>...</think>` blocks and `reasoning_content` fields across Ollama and DeepSeek models inside `query_universal_llm`.

---

## Step 3 (v4 – v5): Layered Base+Delta Environment, Proactive Memory, Live Clock & Macro Loop Breaker

### Main Milestones
* **Layered Micro-Environment (Immutable Base + Session Delta)**:
  * Removed slow per-session `pixi init` calls and instead baked a comprehensive scientific Python & system stack directly into `Containerfile` (`pandas`, `numpy`, `scipy`, `matplotlib`, `requests`, `beautifulsoup4`, `lxml`, `playwright` + Chromium, `pypdf2`, `python-docx`, `biopython`, `rdkit`, `pyarrow`, `pillow`, `tiktoken`, `sqlalchemy`, `networkx`, `sqlite3`, `imagemagick`).
  * Created a per-session delta overlay at `/app/workspace/custom_packages` prepended to `PYTHONPATH` in `ENV_VARS`. Whenever a forged tool declared `# REQUIRES: pkg`, the harness installed only the missing package via `pip install --target /app/workspace/custom_packages --no-cache-dir`.
  * **What this allowed**: Reduced new session startup time from minutes to milliseconds while eliminating 2GB+ environment duplication across sessions.
* **Proactive Long-Term Fact Storage (`store_memory`)**:
  * Added `store_memory(summary_title, facts_and_findings)` so the Brain could proactively commit key discoveries, file schemas, or experimental results to `/app/workspace/memories/` at any time without wiping its active conversation history.
* **Sweep-and-Replace Live System Clock**:
  * Injected a live `[SYSTEM CLOCK: YYYY-MM-DD HH:MM:SS]` tag into the active conversation turn while sweeping and stripping outdated clock tags from earlier turns.
  * **What this allowed**: Gave the Brain real-time temporal awareness (for logs, timestamps, and scheduling) without polluting historical messages.
* **Autonomous Macro-Breaker (Runaway Turn Protection)**:
  * Added an `autonomous_turn_count` tracker in `chat_overseer.py`:
    * **Soft Breaker (100 consecutive tool turns)**: Injects a mandatory pause forcing the Brain to run `compress_and_store_context` and summarize progress to the user.
    * **Hard Breaker (300 consecutive tool turns)**: Immediately halts the execution loop to prevent infinite API billing or runaway loops.

### Tweaks & Refinements
* **Mid-Turn Tool Output RAM Cache (`executed_tool_outputs`)**: Cached raw tool outputs in host memory during multi-step turns so that if `compress_and_store_context` triggered mid-turn, the Summarizer still had access to the full outputs of tools executed earlier in that turn.
* **Forgiving `execute_bash` Signatures (v5)**: Updated `execute_bash` to accept either `cmd` or `command` (catching common LLM argument naming slips) and merged `stderr` directly into `stdout` so compiler warnings and CLI error messages were always visible to the Brain in chronological order.

---

## Step 4 (v6 – v7.1): Rich Terminal UI, Headless CLI Flags, Piped STDIN & Multi-Agent Swarm Mode

### Main Milestones
* **Real-Time Rich Terminal Streaming UI (`rich`)**:
  * Integrated `rich.live.Live`, `Panel`, `Markdown`, and `Group` into `chat_overseer.py` to render live-updating panels distinguishing `[Thinking Process]` (dimmed italics) from `[Final Response]` (rendered Markdown) as tokens streamed in.
* **Headless Automation & CLI Argument Parser (`argparse`)**:
  * Added command-line flags for batch and scripted workflows:
    * `-p` / `--prompt`: Supply an initial prompt directly from the CLI.
    * `-f` / `--format`: Switch between `markdown` (Rich TUI) and `text` (plain stdout for log piping).
    * `-s` / `--session`: Resume an existing session ID or create a named session.
    * `-x` / `--exit`: Headless one-shot execution mode (runs the prompt and exits automatically upon completion).
    * `--brain`, `--coder`, `--summarizer`, `--adviser`: Per-role LLM profile overrides passed dynamically into the Podman container via `podman exec -e`.
    * **Piped `stdin` Ingestion**: Automatically detected piped input (`not sys.stdin.isatty()`) and appended it to the initial prompt while rebinding interactive input to `/dev/tty`.
  * **What this allowed**: Transformed the project from an interactive-only chatbot into a scriptable UNIX command-line utility (`cat data.csv | python chat_overseer.py -p "Analyze this" -f text -x`).

### Tweaks & Refinements
* **PID-Scoped Container Naming for Swarm Mode (v7.1)**: Changed the Podman container name from a static `forge_sandbox_{active_session}` to `forge_sandbox_{active_session}_{os.getpid()}`.
  * **What this allowed**: Multiple instances of the harness could now run simultaneously in separate `tmux` panes (or even share a session workspace in **Swarm Mode**) without Podman container name collisions.

---

## Step 5 (v8 – v9): SQLite + `sqlite-vec` Vector Search, Headless Web Fetching, `write_file` & Soft-Delete Archive

### Main Milestones
* **Native Relational + Vector Database (`query_sqlite_db` & `sqlite-vec`)**:
  * Installed `sqlite-vec` inside the container and built `query_sqlite_db(db_name, query, params, text_to_embed)`.
  * Introduced a standardized **Two-Table Vector Architecture**: standard relational tables (`docs`) paired with `vec0` virtual tables (`docs_vec` using `FLOAT[4096]`) linked by `rowid`.
  * When `text_to_embed` was provided, the tool automatically called the configured embedding model (`EMBEDDING_CONFIG`, e.g., `qwen3-embedding:8b-q8_0`) and substituted the 4096-dimensional `float32` byte vector into the `{{EMBEDDING}}` SQL placeholder for `MATCH` k-NN queries or inserts.
  * **What this allowed**: Gave the agent a zero-configuration local RAG and structured data warehouse inside every session.
* **Headless Browser Web Scraper (`fetch_webpage`)**:
  * Added `fetch_webpage(url, wait_seconds)` powered by headless **Playwright (Chromium)** and **BeautifulSoup4**, rendering JavaScript-heavy pages, stripping noisy DOM elements (`<script>`, `<style>`, `<nav>`, `<footer>`, `<svg>`), and returning clean readable text.
* **Direct Workspace File Writer (`write_file`)**:
  * Added `write_file(filepath, content)` so the Brain could directly author Markdown reports, JSON configs, CSVs, and SQL scripts without spawning a bash subshell.
  * Explicitly blocked `.py` file creation in `write_file` to enforce the architectural rule that all executable Python tools must go through the Coder (`forge_and_register_tool`).
* **Non-Destructive `/archive` Policy**:
  * Provisioned `/app/workspace/archive/` and instructed the agent never to permanently delete files (`rm`), moving superseded files to `/archive` instead.

### Tweaks & Refinements
* **Zombie Process Reaping (`--init` & `atexit`) (v8)**: Added `--init` to `podman run` so an init process reaped orphaned Chromium/subshell zombie processes inside the container, and registered a Python `atexit` handler on the host to guarantee `podman rm -f` cleanup even on unexpected exits.
* **SQLite Output Caps**: Enforced a hard 50-row fetch cap and 5,000-character truncation with a warning banner in `query_sqlite_db` so accidental `SELECT *` queries on massive tables could not overflow the Brain's context window.

---

## Step 6 (v10 – v11.1): The Analyst (Vision & Log Miner), Pointer Architecture, Micro-Breaker & Self-Inspection

### Main Milestones
* **The Analyst Sub-Agent (`analyze_file` $\rightarrow$ `analyze_files`)**:
  * Added a 5th specialized agent role—**The Analyst (`ANALYST_CONFIG`)**—equipped with a massive context window and native **Multimodal Vision**.
  * Upgraded in v10.1 from single-file `analyze_file` to multi-file `analyze_files(filepaths, instruction)` capable of inspecting multiple images (`.png`, `.jpg`, `.webp`, `.gif` encoded as Base64 data URLs) and massive text/CSV/log files (up to 100,000 characters per file) simultaneously.
  * Enforced a strict JSON output schema (`executive_summary` + `full_report`), saving the exhaustive report to `/app/workspace/state/<timestamp>_analyst_report.md` and returning only the executive summary + file pointer if the full report exceeded 20,000 characters.
  * **What this allowed**: The Brain could now "see" plots it generated with `matplotlib`, inspect microscopy/structural images, and mine 100k+ line log files without bloating its own working memory.
* **The Pointer Architecture for Massive Outputs (`execute_bash` & `fetch_webpage`)**:
  * Replaced destructive string truncation in `execute_bash` (>4,000 chars) and `fetch_webpage` (>10,000 chars): instead of discarding overflow text, the harness now writes the complete unedited output to `/app/workspace/sandbox/bash_output_<timestamp>.log` (or `webpage_<timestamp>.txt`), returns a preview snippet, and hands the Brain the file pointer to pass to `analyze_files`.
  * **What this allowed**: Zero data loss on large CLI outputs or long web articles while keeping the Brain's active context lean.
* **Autonomous Micro-Breaker (`tool_failure_streaks`)**:
  * Added per-tool consecutive failure tracking in `chat_overseer.py`. If any tool failed 5 times in a row (later tightened to 3), the harness injected a `[SYSTEM INTERVENTION - MICRO-BREAKER]` message forbidding the Brain from repeating the failing approach and forcing a strategy pivot or `consult_adviser` call.

### Tweaks & Refinements
* **Read-Only Self-Inspection Mounts (v10)**: Mounted `chat_overseer.py` (`:ro`) into `/app/chat_overseer.py` inside the container so the agent could inspect its own host/container source code for self-debugging and architectural analysis.
* **Dedicated Container Debug Log (v10.1)**: Configured Python `logging` inside `god_tools.py` to write exclusively to `/app/workspace/logs/container_debug.log` and stripped all console StreamHandlers so stray library logs could never corrupt the FastMCP `stdio` JSON-RPC pipe.
* **Security Hardenings (v11)**:
  * Added `os.path.realpath` and `os.path.commonpath` validation in `write_file` to block `../` path traversal outside `/app/workspace`.
  * Hardened the `# REQUIRES:` package installer in `forge_and_register_tool` to use `shlex.split()` + `subprocess.run(..., shell=False)` to prevent shell injection via package names.
  * Added automatic backup of existing plugins to `/app/workspace/archive/` before overwriting a tool in `forge_and_register_tool`.
  * Added regex blocks in `execute_bash` and `query_sqlite_db` against `rm`, `DROP TABLE`, and `DELETE FROM`.

---

## Step 7 (v12 – v15.2): Decoupled UI Modes, Payload Airlocks, Rolling Summarization, Stealth Web Search & Zero-Context Bulk Embeddings

### Main Milestones
* **Decoupled Output Formatting & 4-Tier Verbosity Controls (v12)**:
  * Separated terminal presentation into two orthogonal settings:
    * **Format (`-f`)**: `markdown` vs. `text`
    * **Verbosity (`-v`)**: `silent` (final answer only), `minimal` (final answer + tool call names), `standard` (Brain thoughts + tool args/results + final answer), and `detailed` (streams internal thoughts and raw outputs of all sub-agents in real time).
  * Added live interactive slash commands (`/silent`, `/minimal`, `/standard`, `/detailed`, `/text`, `/markdown`) to switch modes mid-conversation.
* **Pre-Flight Dynamic Payload Airlocks (`get_payload_tokens`) (v13)**:
  * Added `tiktoken` (`cl100k_base`) token counting before every sub-agent API call (`consult_adviser`, `analyze_files`, `query_universal_llm`). If a prompt + file payload exceeded 85% of the target model's context window, the tool cleanly rejected the call with an actionable token-count error instead of triggering an HTTP 400 crash or silent truncation.
* **Token-Targeted Rolling Chunk Summarization (v13)**:
  * Upgraded `compress_and_store_context` to slice massive conversation histories into 20,000-token chunks, summarize each chunk sequentially while carrying forward a rolling state summary, and splice the final compacted JSON back into `messages` while preserving the last 4 active turns intact.
* **Playwright-Stealth Live Web Search (`search_web`) (v14)**:
  * Integrated `playwright-stealth` (`Stealth().use_async`) and built `search_web(query, max_results)` to scrape DuckDuckGo's HTML portal using a realistic browser context (`1920x1080`, `en-US` locale, custom User-Agent) to bypass bot challenges and CAPTCHAs.
  * Added anti-hallucination prompt rules forbidding the Brain from guessing URLs and requiring `search_web` discovery before `fetch_webpage`.
* **Zero-Context SQL-Driven Bulk Embeddings (`batch_generate_embeddings`) (v15 – v15.1)**:
  * Introduced `batch_generate_embeddings` in v15 (initially accepting Python lists of `rowids` and `texts_to_embed`), then completely redesigned it in **v15.1** to accept a SQL `source_query` (e.g., `SELECT rowid, abstract FROM docs WHERE rowid NOT IN (SELECT rowid FROM docs_vec)`).
  * **What this allowed**: The container's Python backend fetched thousands of rows directly from SQLite, batched them through the `/v1/embeddings` endpoint in C-speed memory, and inserted the 4096-dim vectors into `docs_vec`—**embedding tens of thousands of documents with zero tokens passing through the Brain's context window**.

### Tweaks & Refinements
* **Atomic JSON State Persistence (v13)**: Updated `save_history` and `save_json` (`registry.json`, `memory_index.json`) to write to a `.tmp` file first and atomically swap via `os.replace()`, preventing corrupted JSON files if a process was killed mid-write.
* **Bulk SQLite Operations (`executemany` & `executescript`) (v15.1)**: Upgraded `query_sqlite_db` so passing a list-of-lists in `params` automatically triggered `cursor.executemany()` for fast bulk inserts, and multiple semicolon-separated DDL statements automatically used `cursor.executescript()`.
* **Decoupled Search vs. Insert Embedding Parameter (v15.2)**: Split `query_sqlite_db`'s embedding parameter into `text_to_embed` (for single-row `INSERT`) and `search_text_to_embed` (for `MATCH` k-NN search queries) to eliminate parameter ambiguity.

---

## Step 8 (v17 – v21): The Architect & Skills (`SKILL.md`), Atomic Token Telemetry, SQLite Authorizer, MCP Stdout Shield & Polyglot Tooling

### Main Milestones
* **The Architect Sub-Agent & Dynamic Skills System (`commission_architect` & `load_skill`) (v17)**:
  * Added a 6th specialized role—**The Architect (`ARCHITECT_CONFIG`)**—tasked with distilling complex multi-step workflows, domain SOPs, and tool pipelines into reusable **Agent Skills** (`/app/workspace/skills/<skill-name>/SKILL.md`) with YAML frontmatter (`name`, `description`).
  * Built `build_skills_menu()` in `chat_overseer.py` to scan `/skills/` on every turn and dynamically inject an `<available_skills>` catalog into the Brain's system prompt, paired with `load_skill(skill_name)` to load full procedural instructions on demand.
  * **What this allowed**: Progressive disclosure of procedural knowledge—the Brain only paid the token cost for a complex domain SOP when it actually needed to execute that workflow.
* **Atomic Per-Role Token Telemetry (`token_usage.json`) (v17)**:
  * Added `log_token_usage(role, prompt_tokens, completion_tokens)` guarded by `fcntl.flock` exclusive file locks, recording cumulative token consumption for `brain`, `coder`, `adviser`, `analyst`, `architect`, `summarizer`, `universal`, and `embedding` inside `/app/workspace/state/token_usage.json`.
  * Displayed per-turn delta (`+X in, +Y out`) and session totals in the terminal UI after every sub-agent and Brain turn, and included MCP tool JSON schema overhead in the Brain's context window token calculation.
* **SQLite C-Level Authorizer Firewall (`sqlite_authorizer`) (v17)**:
  * Replaced brittle regex checks on SQL strings with SQLite's native C-level `conn.set_authorizer(sqlite_authorizer)`, deterministically blocking `SQLITE_DELETE` and `SQLITE_DROP_TABLE` operations even if obfuscated inside CTEs or triggers.
* **Host MCP Client Stdout Immunization Monkey-Patch (v19)**:
  * Monkey-patched `mcp.types.JSONRPCMessage.model_validate_json` in `chat_overseer.py` so that if a C-library or subprocess inside the container leaked raw text onto `stdout` (violating the JSON-RPC stdio protocol), the host intercepted the `ValidationError`, logged the stray line cleanly as `[Container Stdout]`, and returned a harmless dummy ping response instead of crashing the entire session.
* **Polyglot Tool Creation Engine (Python, JS, TS, Rust, C++) (v21)**:
  * Installed **Rust (`rustup`, `cargo`, `rustc`)** and **Node.js (`node`, `npm`, `tsx`)** alongside `g++` in `Containerfile`.
  * Upgraded `forge_and_register_plugin` (renamed from `forge_and_register_tool`) with a `language` parameter (`python`, `javascript`, `typescript`, `rust`, `cpp`) and automated language-specific verification:
    * **Python**: `py_compile` + `# REQUIRES:` pip auto-installer.
    * **JavaScript**: `node --check` + `# REQUIRES:` npm auto-installer.
    * **TypeScript**: `tsx` wrapper execution.
    * **Rust**: Temporary Cargo project generation + `cargo check -q` + `rustc -O` native binary compilation.
    * **C++**: `g++ -fsyntax-only` check + `g++ -O3` native binary compilation.
  * **What this allowed**: When Python was too slow for heavy sequence alignment, matrix math, or graph traversal, the agent could autonomously forge and compile an `-O3` C++ or Rust binary tool.

### Tweaks & Refinements
* **Container Filesystem & Kernel Hardening (v19 – v20)**:
  * Locked down container binary and library directories (`chmod -R a-w /app/.pixi/envs/default/lib/python*/site-packages`, `/app/.pixi/envs/default/bin`, `/home/agent/.local`) so sandboxed code could not hijack Python packages (`sitecustomize.py`) or binaries.
  * Masked kernel symbol tables (`--volume /dev/null:/proc/kallsyms:ro`, `/dev/null:/proc/modules:ro`) and neutralized setuid/privilege binaries (`su`, `mount`, `passwd`, `gpasswd`) with `/dev/null` bind mounts.
* **Doubled Active Context Budget (v21)**: Increased `MAX_CONTEXT_TOKENS` from `60,000` to `120,000` tokens to support deeper multi-step scientific reasoning before compaction.

---

## Step 9 (v22 – v25.6): In-Workspace Git Versioning, Surgical Code Editing, Pointer Context Attachments, Stream Loop Breaker, Full Sub-Agent Observability & SSRF Hardening

### Main Milestones
* **Automated In-Workspace Git Version Control & `surgical_code_edit` (v22)**:
  * Auto-initialized a local Git repository inside every session workspace (`/app/workspace/.git`) with a curated `.gitignore` (tracking `plugins/`, `skills/`, `memories/`, and scripts while ignoring heavy `logs/`, `state/`, `sandbox/`, `custom_packages/`, and `*.db`).
  * Added `surgical_code_edit(filepath, modification_request)` so the Brain could delegate targeted search-and-replace patches on existing files to the Coder instead of regenerating entire files from scratch. Every tool creation, skill creation, or surgical edit automatically created a `.surgical.bak` backup and committed a timestamped snapshot to the workspace Git log.
  * **What this allowed**: Fast, low-token edits on large codebases with instant `git diff` / `git checkout` rollback safety.
* **Pointer-Based Context File Attachments (`context_filepaths`) (v23)**:
  * Added `context_filepaths: list[str]` (powered by `gather_agent_context`) to `forge_and_register_plugin`, `surgical_code_edit`, `consult_adviser`, `commission_architect`, and `query_universal_llm`.
  * **What this allowed**: Instead of the Brain reading a file into its own context just to copy-paste it into a sub-agent prompt, the Brain simply passed the file paths (`["/app/workspace/sandbox/schema.sql"]`) and the container backend injected the file contents directly into the sub-agent's prompt.
* **Real-Time Stream Repetition Circuit Breaker (`detect_text_loop`) (v23.1)**:
  * Built a sliding-window alphanumeric repetition detector monitoring the last 10,000 characters of both `thinking_buffer` and `content_buffer` during streaming. If a model fell into a degenerate token repetition loop (e.g., repeating a 50-char phrase 5+ times or a 200-char block 3+ times), the harness immediately aborted the stream and triggered an autonomous self-healing retry at `temperature=0.4`.
* **Unified Sub-Agent Thought & Artifact Observability (v24 – v24.1)**:
  * Standardized `extract_thinking_and_content()` and hidden sentinel tags (`<___ADVISER_THOUGHTS___>`, `<___ANALYST_THOUGHTS___>`, `<___ARCHITECT_THOUGHTS___>`, `<___SUMMARIZER_THOUGHTS___>`, `<___UNIVERSAL_THOUGHTS___>`) across every sub-agent in `god_tools.py`.
  * **What this allowed**: 100% of sub-agent internal chain-of-thought reasoning and generated reports were recorded in `chat_log_*.txt` and streamed live in `detailed` verbosity mode, while keeping saved disk files (`*_advice.md`, `*_analyst_report.md`, `SKILL.md`) completely free of `<think>` tags (v24.1).
* **Comprehensive Network SSRF Firewall & Symlink Escape Protection (v25)**:
  * Added `is_safe_web_url(url)` pre-flight DNS resolution and IP validation to `fetch_webpage` and `search_web`, blocking loopback (`127.0.0.0/8`), private RFC1918 networks, link-local/cloud metadata IPs (`169.254.0.0/16`), and `host.containers.internal`.
  * Hardened Podman network flags with `--network=slirp4netns:allow_host_loopback=false` (while passing `--add-host=host.containers.internal:host-gateway` strictly for port 4000 LiteLLM proxy traffic), added `nosuid,nodev` to workspace volume mounts, and added `os.path.realpath` symlink escape checks across `load_history`, `build_skills_menu`, and `load_skill`.

### Tweaks & Refinements
* **Expanded Scientific & CLI Base Image (v25.1)**: Pre-installed `scikit-learn`, `seaborn`, `statsmodels`, `openpyxl`, `duckdb`, `sympy`, `pyyaml`, `h5py`, and Linux CLI utilities (`procps`, `ripgrep`, `tree`, `bzip2`) in `Containerfile`, and set `--shm-size=2g` so multi-process PyTorch/Playwright workloads never crashed on `/dev/shm` exhaustion.
* **Chunked Embedding Sub-Batches (v25.1)**: Updated `batch_generate_embeddings` to process SQL rows in chunks of 128 so 10,000-row embedding jobs never hit HTTP payload limits or GPU OOMs.
* **Multi-Block Surgical Edits & Pre-Flight `compile()` Validation (v25.2 & v25.4)**: Upgraded `surgical_code_edit` to apply an array of multiple `{"search": ..., "replace": ...}` blocks in a single call (v25.2) and added an in-memory Python `compile(modified_content, filepath, 'exec')` check *before* writing to disk (v25.4) so syntax-breaking edits were auto-retried by the Coder without ever corrupting the file on disk.
* **Rust Crate Auto-Detection, Scholarly Metadata & DuckDuckGo Unwrapping (v25.4)**:
  * Added `// REQUIRES: serde = "1.0"` parsing to automatically populate `Cargo.toml` dependencies when forging Rust tools.
  * Updated `fetch_webpage` to extract academic meta tags (`citation_title`, `citation_author`, `citation_doi`, `dc.*`, `og:*`) into a `[PAGE METADATA]` header before stripping the DOM.
  * Updated `search_web` to unwrap DuckDuckGo `//duckduckgo.com/l/?uddg=...` tracking links into clean direct URLs.
  * Added 0-byte and >10MB pre-flight guards for image files in `analyze_files`.
* **Container Local Timezone Sync (`--tz=local`) (v25.5)**: Added `--tz=local` to `podman run` so container log timestamps matched the host clock.
* **Idempotent Session Prefixing (v25.6)**: Fixed `-s` session naming so passing `-s Session_ID_123` or `-s 123` resolved cleanly without doubling the `Session_ID_` prefix.

---

## Step 10 (v26 – v28.5): Automated WSL2 Installer (`setup_wsl2.sh`), Long-Task Refinements, Behavioral Smoke Tests & `llm_config.py` Split

### Main Milestones
* **Two-Phase Automated WSL2 Provisioning Script (`setup_wsl2.sh`) (v26 – v26.2)**:
  * Created a self-contained installer automating the entire Ubuntu WSL2 + Rootless Podman + NVIDIA Container Toolkit (CDI) + Pixi + LiteLLM proxy setup, complete with a `--check` read-only diagnostic mode and idempotent version checks.
* **Long-Horizon Task Refinements (v27)**:
  * Following stress-testing on long autonomous scientific workflows, introduced five major stability upgrades:
    1. **Historical Tool Output Aging (`compact_historical_tool_outputs`)**: Automatically replaced verbose tool outputs older than 8 turns with compact `[HISTORICAL TOOL OUTPUT COMPACTED]` notices to slow context growth between full compactions.
    2. **Dynamic Tokenizer Calibration (`token_calibration_ratio`)**: Calibrated local `tiktoken` estimates against actual `usage.prompt_tokens` reported by the serving LLM API (supporting Qwen, Llama, and DeepSeek tokenizers accurately).
    3. **Structured State Ledger in Summarizer**: Required `compress_and_store_context` to maintain an explicit **State Ledger** tracking created files, database schemas, verified metrics, and failed hypotheses.
    4. **Behavioral Pre-Registration Smoke Tests (`--help`)**: Upgraded `forge_and_register_plugin` so newly forged Python, JS, and TS tools are executed with `--help` (5s timeout) before registration—catching broken imports or top-level runtime crashes that static syntax checks (`py_compile`) miss.
    5. **Near-Match Context Hints in `surgical_code_edit`**: When a Coder's `search` block failed to match, `difflib.SequenceMatcher` located the closest matching lines in the target file and fed the surrounding 10-line snippet back to the Coder for an immediate self-corrected retry.
* **Dedicated Model & Endpoint Configuration (`llm_config.py`) (v28)**:
  * Extracted `LLM_PROFILES`, role assignments (`BRAIN_CONFIG`, `CODER_CONFIG`, etc.), `EMBEDDING_CONFIG`, and `UNIVERSAL_LLM_CONFIG` out of `config.py` into a dedicated, user-friendly `llm_config.py` file while keeping `config.py` focused on prompts and system guardrails.

### Tweaks & Refinements
* **Zero-Argument Skill Discovery (v26.1)**: Allowed calling `load_skill()` with no arguments to return a formatted directory of all installed skills.
* **Smarter Micro-Breaker Heuristics (v27)**: Taught the Micro-Breaker that exit code `1` from search/comparison tools (`grep`, `rg`, `diff`, `test`) means "no match found" rather than a command crash, and reset the failure streak whenever the Brain modified the command arguments to debug the issue.
* **Podman `--init` Binary Auto-Detection & UID 1000 Alignment (v28.3 – v28.5)**: Added host detection for `catatonit` / `tini` before passing `--init` to Podman, and updated `Containerfile` (`userdel -r ubuntu && useradd -u 1000 -m agent`) so UID 1000 inside the container mapped cleanly to the rootless WSL2 user.

---

## Step 11 (v29 – v32.4): KV-Cache Prefix Preservation, Failure Forensics, Dynamic File Slicing/Regex Filtering, Continuous SHA-1 Tool Pruning & 2-Step Targeted Handoff Compaction

### Main Milestones
* **LLM KV-Cache Prefix Preservation (v29)**:
  * Discovered that mutating the `[SYSTEM CLOCK]` inside the system prompt or earlier user turns invalidated the LLM server's KV-cache prefix on every single turn. Refactored clock injection so it is appended transiently to the *very last message* right before inference and stripped immediately after—**preserving 100% KV-cache hits across multi-turn sessions and dramatically reducing time-to-first-token**.
* **Automated Sub-Agent Failure Forensics (`save_failure_trace`) (v30)**:
  * Whenever any sub-agent (`Coder`, `surgical_code_edit`, `Adviser`, `Analyst`, `Architect`, `Summarizer`, `Universal`) exhausted its retries or threw an exception, the harness automatically dumped a structured post-mortem JSON file (`failure_<role>_<timestamp>.json`) capturing the exact inputs, raw model output, and error traceback.
* **Advanced File Slicing, Tail Mode & Regex Line Filtering in `analyze_files` (v30.3 & v32)**:
  * Upgraded `analyze_files` with:
    * `start_line` and `max_lines` for paginated window inspection.
    * `tail_mode=True` to read the *end* of massive log files where recent errors live.
    * `filter_pattern` (regex) and `context_lines` (powered by `filter_file_lines`, up to 500 matches) to pre-filter gigabyte-scale files down to matching lines before sending them to the Analyst.
    * Dynamic context scaling: raised the Analyst's payload limit to 80% of its configured context window (~280,000 chars) and added explicit `[COVERAGE BOUNDARY]` and `[COVERAGE WARNING]` banners so the Brain always knew exact line ranges read.
* **Continuous SHA-1 Archival Pruning & 2-Step Targeted Handoff Compaction (v32)**:
  * **Continuous SHA-1 Pruning (`compact_historical_tool_outputs`)**: Aged tool outputs (>10 turns old and >1,000 chars) are written to `/app/workspace/histories/tool_outputs/tool_<turn>_<name>_<sha1>.txt` (indexed in `manifest.json`) and replaced in-context with a 300-char preview + file pointer.
  * **2-Step Targeted Handoff Summarization**: Replaced slow multi-chunk recursive summarization with a fast, deterministic 2-Step Handoff anchored to the **Original Turn 1 User Objective**, the live **`active_plan.md`**, and an **Automated Workspace File Inventory**, keeping the last 10 turns verbatim.

### Tweaks & Refinements
* **Graceful `Ctrl+C` (`KeyboardInterrupt`) History Reconciliation (v29)**: Added a post-interruption check in `chat_overseer.py` that scans the last assistant message for dangling `tool_calls` lacking matching `tool` responses and injects `[SYSTEM: Tool execution was aborted by the user (KeyboardInterrupt)]` placeholders so the session history never becomes corrupted after `Ctrl+C`.
* **Playwright Route-Level Redirect SSRF Blocking (v29.1)**: Attached `page.route("**/*", handle_route)` inside `fetch_webpage` and `search_web` so every sub-request and HTTP 301/302 redirect is validated against `is_safe_web_url()` before Playwright connects.
* **Anti-Hallucination `status` Enum in `surgical_code_edit` (v30.1)**: Added `"status": "success" | "target_not_found" | "error"` and `"explanation"` to `surgical_code_edit`'s schema so the Coder could report missing targets cleanly instead of hallucinating dummy edits.
* **Append Mode & 40% Size-Shrink Guard in `write_file` (v30.2)**: Added `mode="a"` (append) to `write_file` and blocked `mode="w"` overwrites that shrink an existing file (>500 chars) by more than 40% (`allow_shrink=False`), directing the Brain to use `surgical_code_edit` instead of accidentally clobbering existing reports or code.
* **Head + Tail Bash Previews (v30.3)**: Upgraded `execute_bash` truncation (>4,000 chars) to show both the **first 1,500 characters (Head)** and **last 1,500 characters (Tail)** so final summary metrics and exit errors at the bottom of CLI output are immediately visible.
* **Headless `-x` Autonomous Audit Nudge (v30.8)**: In `-x` batch mode, if the Brain stops calling tools, the harness injects a one-time audit verification prompt forcing the Brain to verify all deliverables on disk before exiting.
* **Soft Loop Advisory & Bioinformatics Suite (v31)**: Added a gentle `[SYSTEM ADVISORY]` when the exact same tool + arguments are called 5 times in a row, and pre-installed **`mafft`**, **`hmmer`**, **`pyhmmer`**, and **`psmisc`** in `Containerfile`.
* **Markdown-Safe JSON Extractor (`extract_json_payload`) (v32.1)**: Replaced naive `.split("```")` JSON parsing with a regex wrapper stripper that safely preserves nested markdown code blocks inside JSON string values.

---

## Step 12 (v33 – v34.9): The Decider (`fast_decision`), External Host MCP Ecosystem (`mcp_config.py`, `manage_external_mcp`) & Hardened UCSF ChimeraX Bridge

### Main Milestones
* **The Decider Sub-Agent (`fast_decision` / System-1 Rulings) (v33)**:
  * Added a 7th specialized agent role—**The Decider (`DECIDER_CONFIG`)**—interfacing with ultra-low-latency non-autoregressive / classification endpoints (such as `clef-flash:9b` via `/v1/systemone` with standard `/v1/chat/completions` fallback).
  * Supported four deterministic modes: `choice` (pick from options), `boolean` (`true`/`false`), `score` (`1`–`5` rating), and `custom` regex grammar, plus `context_filepaths` and `image_filepaths` (Vision) attachments.
  * **What this allowed**: Sub-second triage, rubric grading, and binary quality gates without invoking slow, high-token autoregressive reasoning models.
* **External Host MCP Ecosystem & Dynamic Meta-Tool (`mcp_config.py` & `manage_external_mcp`) (v34 – v34.4)**:
  * Created `mcp_config.py` (`EXTERNAL_MCP_SERVERS`) supporting external **SSE** and **stdio** MCP servers running on the host or remote endpoints.
  * Engineered an isolated async worker task architecture (`mcp_worker` + `call_external_tool`) in `chat_overseer.py` so each external MCP connection lives in its own dedicated `asyncio.Task` (preventing AnyIO cancel-scope cross-task crashes), complete with `require_confirmation` human-in-the-loop safety gates.
  * Exposed external MCP servers across three control planes:
    1. **Startup CLI flags**: `--mcp <name>` and `--all-mcp`.
    2. **Interactive slash commands**: `/mcp list`, `/mcp on <name>`, `/mcp off <name>`.
    3. **Autonomous Brain Meta-Tool (`manage_external_mcp`)**: Allowed the Brain itself to discover, connect (`enable`), and disconnect (`disable`) external MCP servers mid-turn, persisting active connections in `state/active_mcp_servers.json` and dynamically refreshing its own tool schema without restarting the session!
* **Hardened UCSF ChimeraX Structural Biology MCP Bridge (`chimerax_mcp_server.py`) (v34.5 – v34.9)**:
  * Built a dedicated FastMCP SSE bridge (`port 21050`) connecting the WSL2 agent to **UCSF ChimeraX** running natively on the Windows 11 host (`remotecontrol rest start port 21049`), auto-detecting the WSL2 default gateway IP (`ip route show default`).
  * **Host Protection Security Firewall (v34.6)**: Because ChimeraX runs unsandboxed on the Windows host, hardened `chimerax_mcp_server.py` into a strict security firewall:
    * Completely removed arbitrary Python execution (`cx_run_python`).
    * Enforced a strict **22-verb whitelist** of safe visualization/structural biology commands (`open`, `close`, `view`, `turn`, `zoom`, `color`, `rainbow`, `style`, `show`, `hide`, `cartoon`, `ribbon`, `surface`, `lighting`, `graphics`, `set`, `select`, `split`, `combine`, `align`, `matchmaker`, `measure`, `distance`, ` clash`, `hbonds`, `contacts`, `alphafold`, `esmfold`, `modeller`, `volume`, `molmap`, `fitmap`, `label`, `2dlabels`, `key`, ` camera`, `windowsize`, `info`, `version`, `log`, `help`).
    * Blocked command chaining (`;`, `|`, `&`, `` ` ``, `$`, newlines), banned file-writing/network verbs (`save`, `runscript`, `shell`, `cd`), and blocked script extensions (`.py`, `.cmd`, `.bat`, `.ps1`, `.exe`).
    * Confined `cx_save_image` strictly to rendering `.png`/`.jpg` into the session's `sandbox/` directory via WSL2 UNC paths (`//wsl.localhost/<Distro>/...`) with an automatic Base64 REST transfer fallback (v34.7–v34.8).
  * **What this allowed**: The sandboxed Linux agent could autonomously load PDB/AlphaFold structures, run `matchmaker` alignments, color domains, render ray-traced publication figures on the Windows GPU, and inspect the resulting PNGs via `analyze_files`—with zero risk to the Windows 11 host filesystem.

### Tweaks & Refinements
* **Multi-Line Interactive Terminal Input (`\`) (v33.1)**: Added trailing backslash (`\`) continuation support in the interactive terminal prompt so users can compose multi-line prompts cleanly.
* **ChimeraX Command Cheatsheet Injection (v34.9)**: Embedded a concise ChimeraX CLI syntax reference (`#1/A:10-50@CA`, `color byattribute bfactor`, `matchmaker #2 to #1`) directly into `cx_run_command`'s docstring to prevent PyMOL syntax confusion.

---

## Step 13 (v35 – v38.1): Generalized REST Escape Hatch (`execute_rest_call`), Full Multimodal Pipelines, Telemetry & Fuel Gauge, and Concurrent Multi-Tool Execution Waves

### Main Milestones
* **Generalized HTTP REST Escape Hatch (`execute_rest_call`) (v35 – v35.2)**:
  * Added `execute_rest_call(url, method, headers_json, payload_json, request_filepath, files, output_filepath, timeout)` to interact with any local or remote REST microservice (transcription servers, OCR APIs, custom ML endpoints, bioinformatics web services).
  * Supported streaming multipart file uploads (`files={"audio": "/app/workspace/..."}`), automatic `localhost`/`127.0.0.1` $\rightarrow$ `host.containers.internal` translation, automatic paired request/response archiving in `state/rest_calls/`, and a hard firewall block on port `21049` so the Brain could never bypass the ChimeraX MCP security filter.
* **Full Multimodal Support Across `analyze_files` & `query_universal_llm` (v35.3 – v35.4)**:
  * Expanded `query_universal_llm` with named `UNIVERSAL_ENDPOINTS` (`ollama`, `litellm_proxy`), full sampling controls (`temperature`, `top_p`, `max_tokens`, `extra_body_json`), and `media_filepaths` capable of encoding images, audio (`.wav`, `.mp3`), video (`.mp4`, `.webm`), and PDFs as Base64 data URLs.
  * Upgraded `analyze_files` to natively handle:
    * **PDF documents (`.pdf`)**: Automatic text extraction via `pdftotext -layout` (with `pypdf` fallback).
    * **Video files (`.mp4`, `.mov`, `.avi`, `.mkv`, `.webm`)**: Automatic duration inspection (`ffprobe`) + extraction of up to 5 evenly-spaced keyframes via `ffmpeg` passed directly into the Analyst's Vision pipeline!
    * **Audio files (`.mp3`, `.wav`, `.ogg`, `.flac`, `.m4a`)**: Automatic `ffprobe` stream/codec metadata inspection with guidance to route transcription to `query_universal_llm` or `execute_rest_call`.
* **Tool Usage Telemetry, Sequential Call Numbering & Live Context Fuel Gauge (v36.7 – v37.0)**:
  * Added atomic tool call counting (`state/tool_usage.json`), wall-clock execution timers (`1.42s`) on every LLM call and tool invocation, sequential call counters (`Brain #3`, `Tool #5`, `CODER #2`), and a live **Context Fuel Gauge** (`Context: 28,450 / 120,000 (23.7%)`) in the terminal UI.
* **Concurrent Multi-Tool Execution Waves (`asyncio.gather`) (v38.0)**:
  * Replaced sequential `for tool_call in msg.tool_calls:` execution in `chat_overseer.py` with a dependency-aware **Wave Execution Planner**:
    * Partitioned multi-tool turns into parallel execution waves—running read-only and independent tool calls concurrently via `asyncio.gather` while automatically serializing waves when a tool writes to a file (`write_file`, `surgical_code_edit`, `execute_bash`) that a subsequent tool in the same turn reads.
    * Added `_WORKSPACE_STATE_LOCK` (`asyncio.Lock`) inside `god_tools.py` around `registry.json`, `memory_index.json`, and Git commits, and offloaded synchronous SQLite queries to `asyncio.to_thread()` so concurrent waves never block the event loop or race on Git locks.
  * **What this allowed**: Dramatic wall-clock speedups whenever the Brain emitted parallel web searches, multi-file reads, or batch sub-agent queries in a single turn.

### Tweaks & Refinements
* **Dynamic NVIDIA GPU CDI Detection (`has_gpu_cdi`) (v35.7)**: Added automatic pre-flight detection (`podman info` / `/etc/cdi` / `/var/run/cdi`) so if a host lacks an NVIDIA GPU or CDI configuration, the harness automatically omits `--device=nvidia.com/gpu=all` and boots cleanly in CPU mode instead of failing to start the container.
* **Template-Driven Embedding Formatting & Custom Vector Tables (v36.1)**: Added `text_template` (e.g., `"Title: {title}\nAbstract: {abstract}"`) and `target_table` to `batch_generate_embeddings`.
* **Compact Tool Registry Mode & Fast TypeScript Bundling (v36.2)**: Added `detailed=False` compact view to `view_tool_registry` and switched TypeScript validation in `forge_and_register_plugin` from slow `tsc` typechecking to ultra-fast `esbuild` transpilation piped into `node --check`.
* **Multi-Layer Anti-Deletion Hardening & `SQLITE_ATTACH` Block (v36.4)**: Upgraded `execute_bash` with an unquoted token parser blocking `rm`, `rmdir`, `unlink`, `shred`, `truncate`, `find -delete`, `git clean`, and inline `os.remove`/`shutil.rmtree`, injected runtime bash function overrides (`rm() { ... }`), and blocked `SQLITE_ATTACH` in `sqlite_authorizer` so a query could never attach and corrupt another database file.
* **Direct-to-Disk `output_filepath` in `query_universal_llm` (v38.1)**: Added `output_filepath` to `query_universal_llm` so long generated datasets or translations are written directly to disk inside the container without round-tripping through the Brain's context window.

---

## Step 14 (v39 – v42.3): SMRTk Architecture Refactor, Automated Benchmark Suite (`run_tests.sh`), On-Demand Host Skills Library & Asynchronous Background Execution

### Main Milestones
* **Clean Domain-Agnostic Refactor & Rebrand to SMRTk (v39 – v40)**:
  * Standardized all filenames, container images, directories, and tool names into clean, self-documenting nomenclature:
    * `chat_overseer.py` $\rightarrow$ `overseer.py` $\rightarrow$ **`smrtk.py`** (*Sandboxed Multi-Agent Research Toolkit*)
    * `god_tools.py` $\rightarrow$ **`sandbox_tools.py`**
    * `ai-forge` image $\rightarrow$ **`sandbox-image`**
    * `plugins/` $\rightarrow$ **`tools/`**
    * `forge_and_register_plugin` $\rightarrow$ **`create_tool`**
    * `surgical_code_edit` $\rightarrow$ **`edit_code`**
    * `commission_architect` $\rightarrow$ **`create_skill`**
    * `compress_and_store_context` $\rightarrow$ **`compact_context`**
    * `query_universal_llm` $\rightarrow$ **`query_auxiliary_llm`**
* **Automated End-to-End Verification Suite (`run_tests.sh` & `tests.tsv`) (v40.1 – v40.2)**:
  * Built a 22-scenario automated test runner (`run_tests.sh` + `tests.tsv`) with `--test <ID>`, `--list`, `--keep`, per-test timeouts, and automated report generation to benchmark every sub-agent, security boundary, polyglot compiler, vector DB workflow, and multimodal pipeline.
* **On-Demand Host Skills Library (`./skills/`, `/skills` & `manage_skills`) (v41 – v41.1)**:
  * Bundled a curated host-side `./skills/` repository (including the 17-skill `Anthropic` pack covering `docx`, `pdf`, `pptx`, `xlsx`, `frontend-design`, `mcp-builder`, `webapp-testing`, etc.).
  * Engineered **Copy-on-Load Session Isolation**: host skills stay unmounted by default (keeping the Brain's startup prompt lean) and can be mounted on demand into `/app/workspace/skills/` via:
    1. **CLI flags**: `--skills <name>` or `--all-skills`.
    2. **Interactive slash commands**: `/skills list`, `/skills on <name>`, `/skills off <name>`, `/skills save <name>` (exporting a newly created session skill back to the host library!).
    3. **Autonomous Brain Meta-Tool (`manage_skills`)**: Allowing the Brain to `list`, `mount`, `unmount`, or `save` skills mid-session, plus auto-mounting when `load_skill` is called.
  * Upgraded `load_skill` (v41.1) to automatically inventory all bundled helper files (`scripts/`, `references/`, `assets/`) inside a skill folder so the Brain immediately sees executable helper scripts alongside `SKILL.md`.
* **Asynchronous Background Execution & Sub-Agent Delegation (`execute_bash(background=True)`, `delegate_task`, `manage_jobs`, `/jobs`) (v42 – v42.3)**:
  * Unlocked non-blocking parallel background execution across both system commands and sub-agent workflows:
    * **Background Bash Jobs (`execute_bash(cmd, background=True, timeout=3600)`)**: Spawns long-running simulations, training runs, or builds in the background (up to **9 concurrent bash jobs**, configurable up to 10 hours) with live log capture in `/app/workspace/state/jobs/<job_id>.log`.
    * **General Sub-Agent Task Delegation (`delegate_task`)**: Allows the Brain to offload complex instructions to `analyst`, `adviser`, `coder`, or `auxiliary` either synchronously (`background=False`) or asynchronously (`background=True`, up to **9 concurrent sub-agent jobs**).
    * **Job Lifecycle Management (`manage_jobs` & `/jobs`)**: Added the `manage_jobs(action="list"|"status"|"wait"|"kill", job_ids=...)` tool for the Brain and the `/jobs` interactive slash command for the user.
    * **Non-Interleaved Turn-Boundary Harvesting (`harvest_background_jobs`)**: Completed background jobs are automatically harvested at the start of the next turn and injected as a clean `[SYSTEM NOTIFICATION — BACKGROUND JOBS COMPLETED]` message—never corrupting mid-stream output.
  * **Organized `/state` Subdirectory Hierarchy (v42)**: Segregated `/app/workspace/state/` into clean subdirectories (`state/reports/`, `state/failures/`, `state/jobs/`, `state/rest_calls/`, and `histories/tool_outputs/`).

### Tweaks & Refinements
* **Direct-to-Disk `output_filepath` for `analyze_files` (v42.2)**: Added `output_filepath` to `analyze_files` so the Analyst can write custom markdown reports, extracted tables, or code reviews directly to a user-specified file path on disk.
* **Auto-Extracted File Pointers in `delegate_task` (v42.3)**: Added automatic regex path extraction (`r'/app/workspace/[^\s\'"<>()\[\],;]+'`) inside `delegate_task` so even if the Brain mentions a workspace file path inside `task_instructions` and forgets to populate `context_filepaths`, the harness automatically attaches the file for `adviser`, `coder`, and `auxiliary` tasks.

---

## Complete Reference: Agent Roles, Tools & Safety Layers

### 1. The 8 Specialized Agent Roles (`llm_config.py`)

| Role | Config Key | Primary Responsibility |
| :--- | :--- | :--- |
| **The Brain** | `BRAIN_CONFIG` | Primary orchestrator, planning, user interaction, and tool dispatch. |
| **The Coder** | `CODER_CONFIG` | Polyglot tool engineering (`create_tool`), surgical patching (`edit_code`), and delegated coding tasks (`delegate_task`). |
| **The Summarizer** | `SUMMARIZER_CONFIG` | 2-step targeted handoff context compaction (`compact_context`) preserving the State Ledger. |
| **The Adviser** | `ADVISER_CONFIG` | High-level scientific, mathematical, and architectural strategy (`consult_adviser`). |
| **The Analyst** | `ANALYST_CONFIG` | High-context multimodal inspection of images, PDFs, video keyframes, and massive logs (`analyze_files`). |
| **The Architect** | `ARCHITECT_CONFIG` | Codifying complex multi-step workflows into reusable `SKILL.md` playbooks (`create_skill`). |
| **The Decider** | `DECIDER_CONFIG` | Ultra-fast System-1 classification, boolean gating, and 1–5 rubric scoring (`fast_decision`). |
| **Auxiliary LLM** | `AUXILIARY_LLM_CONFIG` | Open-ended secondary model queries, translations, and multimodal prompts (`query_auxiliary_llm`). |

*(Plus `EMBEDDING_CONFIG` for 4096-dimensional vector embeddings in `query_sqlite_db` and `batch_generate_embeddings`.)*

---

### 2. Complete Sandbox Tool Inventory (`sandbox_tools.py` & `smrtk.py`)

| Category | Tool Name | Purpose |
| :--- | :--- | :--- |
| **Execution & Jobs** | `execute_bash` | Run foreground (120s) or background (up to 10h) Linux commands with Head+Tail pointer logs. |
| | `delegate_task` | Offload tasks to `analyst`, `adviser`, `coder`, or `auxiliary` in foreground or background. |
| | `manage_jobs` | Inspect `list`, `status`, `wait` (blocking barrier), or `kill` active background jobs. |
| **Tool & Code Engineering** | `create_tool` | Forge, compile, smoke-test (`--help`), and register Python, JS, TS, Rust, or C++ CLI tools. |
| | `edit_code` | Multi-block surgical search-and-replace code editing with pre-flight `compile()` & Git commit. |
| | `write_file` | Create or append non-Python files with path-traversal and 40% size-shrink protection. |
| | `view_tool_registry` | View registered custom tools in compact or detailed mode. |
| **Inspection & Research** | `analyze_files` | Multimodal Vision + PDF/Video/Audio + sliced/regex-filtered inspection of massive files. |
| | `search_web` | Headless Playwright-Stealth DuckDuckGo search with direct URL unwrapping. |
| | `fetch_webpage` | Headless Chromium DOM renderer with scholarly metadata extraction and pointer logging. |
| | `execute_rest_call` | Generalized HTTP REST client with multipart file uploads and paired JSON/response archiving. |
| **Sub-Agent Consultations** | `consult_adviser` | Deep strategic/scientific consultation saved to `state/reports/`. |
| | `fast_decision` | Sub-second System-1 `choice`, `boolean`, `score`, or `custom` regex ruling. |
| | `query_auxiliary_llm` | Query any model on Ollama or LiteLLM proxy with full sampling & multimodal controls. |
| **Database & Vector RAG** | `query_sqlite_db` | Relational SQL + `sqlite-vec` k-NN search, `executemany` bulk inserts, and C-level authorizer. |
| | `batch_generate_embeddings` | Zero-context SQL-driven bulk vector embedding generator (chunked in batches of 128). |
| **Memory & Planning** | `manage_plan` | Read or update `/app/workspace/state/active_plan.md`. |
| | `store_memory` | Proactively store permanent facts/findings to `/app/workspace/memories/`. |
| | `view_memory_registry` | List all saved long-term memory records. |
| | `read_memory` | Retrieve a specific archival memory JSON record. |
| | `compact_context` | Trigger 2-step targeted handoff context compaction. |
| **Skills & External MCP** | `create_skill` | Commission the Architect to author a new `SKILL.md` workflow. |
| | `load_skill` | Load a `SKILL.md` playbook and its directory tree inventory into context. |
| | `manage_skills` | Dynamically `list`, `mount`, `unmount`, or `save` skills between host and session. |
| | `manage_external_mcp` | Dynamically `list`, `enable`, or `disable` external host MCP servers mid-session. |

---

### 3. Defense-in-Depth Security & Reliability Layers

1. **Container & Kernel Isolation**: Rootless Podman (`--cap-drop=ALL`, `--security-opt=no-new-privileges`, `--pids-limit=1000`, `nosuid,nodev` mounts, masked `/proc/kallsyms` & `/proc/modules`, read-only `/app` and Pixi site-packages).
2. **Zero-Trust Credential Proxy**: Fake sandbox API key (`sk-sandbox-fake-key`) routed exclusively to host LiteLLM proxy on port 4000.
3. **SSRF Network Firewall**: `--network=slirp4netns:allow_host_loopback=false` + `is_safe_web_url` DNS/IP validation + Playwright route-level redirect interception + port 21049 REST block.
4. **Anti-Deletion & Data Integrity**: Unquoted bash parser + runtime bash function overrides (`rm`, `unlink`, `truncate`), 40% file size-shrink guard in `write_file`, C-level `sqlite_authorizer` blocking `DELETE`/`DROP`/`ATTACH`, automaticworkspace Git commits, and atomic `.tmp` $\rightarrow$ `os.replace` JSON saves.
5. **Host ChimeraX Firewall**: Strict 22-verb whitelist, zero Python execution, blocked shell/chaining tokens, and sandbox-confined UNC image rendering.
6. **Self-Healing Execution Guards**: Macro-Breaker (100/300 turns), Micro-Breaker (3 consecutive failures), Soft Loop Advisory (5 identical calls), Stream Repetition Circuit Breaker (`detect_text_loop`), and Host MCP JSON-RPC stdout monkey-patch.
