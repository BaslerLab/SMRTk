# SMRTk: Sandboxed Multi-agent Research Toolkit

A zero-trust AI architecture utilizing a multi-agent system to dynamically create local tools, featuring persistent sessions, WSL2 isolation, GPU acceleration, and auto-healing debugging loops.

Rather than relying on a static set of pre-programmed tools, this framework empowers an AI to write, compile, debug, and execute its own multi-language tools (Python, JavaScript, TypeScript, Rust, C++) on the fly using the Model Context Protocol (MCP). By separating the "Brain" (logical reasoning) from the "Coder" (code generation), the "Summarizer" (memory management), the "Adviser" (strategic correction), the "Analyst" (data/vision mining), the "Architect" (SOP codification), and the "Decider" (rapid reflex & snap rulings), the system acts as a persistent, self-expanding AI operating system capable of acting as an autonomous researcher—scraping the web, downloading massive datasets, testing hypotheses, training machine learning models, and rendering publication-ready reports while safely bridging massive LLMs with your local hardware.

---

## 🧠 Core Concepts

### Step 0: The Host Sandbox (WSL2 Hardening)

For ultimate security, this framework should be run inside a dedicated WSL2 (Windows Subsystem for Linux) instance under a heavily restricted user account. The steps below create a hardened WSL environment where the AI is completely isolated from your Windows host system.

**1. Install a Dedicated WSL Instance**
Install a fresh Ubuntu instance specifically for this project (named `Agents`). You can install from a downloaded file:

    wsl --install --from-file "C:\path\to\your\download\ubuntu-24.04-wsl-amd64.wsl" --name Agents
    
**2. Create the Restricted User (`agent`)**
Log into your new instance as your initial setup user (who has administrative `sudo` rights). Then, create a restricted, non-sudo user named `agent`. This guarantees that the process running the AI has zero administrative rights to the underlying Linux system:

    wsl -d Agents -u <your-initial-sudo-user>
    sudo useradd -m -s /bin/bash agent
    sudo passwd agent
    
**3. Harden the WSL Configuration**
We must sever the default connections between WSL and your Windows host. Open the WSL configuration file:

    sudo nano /etc/wsl.conf
    
Paste the following configuration. This does three critical things: it sets `agent` as the default user, blocks WSL from automatically mounting your Windows hard drives (disabling `/mnt/c/`), and prevents WSL from executing Windows binaries (like `cmd.exe` or `powershell.exe`).

    [boot]
    systemd=true

    [user]
    default=agent
    
    [automount]
    enabled = false
    
    [interop]
    enabled = false
    appendWindowsPath = false
    
**4. Reboot and Initialize**
Save the file (Ctrl+O, Enter, Ctrl+X) and exit the terminal. Open a standard Windows Command Prompt or PowerShell and restart the WSL instance to apply the hardened settings:

    wsl --terminate Agents
    wsl -d Agents
    
You will now automatically be logged in as the restricted `agent` user, trapped inside a secure, air-gapped Linux bubble. You are now ready to proceed to Step 1!

### 1. The Multi-Agent Architecture
This project utilizes multiple distinct Large Language Models (LLMs) to maximize efficiency and accuracy:
* **The Brain (The Overseer):** A fast, high-parameter reasoning model. It communicates with the user, plans out the necessary steps, and orchestrates the execution of tools.
* **The Coder (The Hands):** A specialized multi-language engineering model operating inside the sandbox. It operates invisibly in the background. When the Brain needs a new component, the Coder outputs the target source files, triggers automated syntax validation passes, and manages compilation schemas dynamically.
* **The Summarizer (The Memory Manager):** A background agent responsible for context compression using strict JSON schemas.
* **The Adviser (The Strategist):** A slow, extremely heavy reasoning model (e.g., DeepSeek-R1, Qwen 397B). It is invoked exclusively when the Brain encounters critical bottlenecks or requires strategic redirection.
* **The Analyst (The Eyes & Data Miner):** A specialized context-isolation and multimodal sensory agent. When the Brain needs to read a massive error log, parse a raw data dump, or inspect multimodal media (images, audio .mp3/.wav, video .mp4, or PDF documents), it delegates the file to the Analyst via `analyze_files`. PDF documents are natively extracted to text (via `pdftotext` with `pypdf` fallback) enabling full regex filtering, slicing, and tail scanning; video files have representative keyframes sampled at 1s via `ffmpeg` along with stream metadata; audio files have metadata extracted with Whisper transcription routing notices; and images are routed directly to vision models. The Analyst features a dynamically scaling budget up to an 80% safe ceiling of the model's context window (~280,000 characters / ~70,000 tokens for a single file), native pattern filtering (`filter_pattern="<regex>"`, `context_lines=N`) to instantly extract matching lines with surrounding context and line numbers without sequential token blowup, targeted line slicing (`start_line`, `max_lines`), reverse tail scanning (`tail_mode=True` to inspect recent tracebacks at the end of huge logs), optional direct file output (`output_filepath` to save structured JSON/Markdown extracts directly to disk without truncating them in chat history), and honest coverage boundary reporting. It processes heavy payloads and returns a concentrated summary, keeping the Brain's context window clean, fast, and immune to data-bloat.
* **The Architect (The Methodologist & Skill Builder):** A specialized agent dedicated to Standard Operating Procedure (SOP) codification. When the Brain solves a novel or complex engineering task, it calls `create_skill`. The Architect synthesizes the full workflow (prerequisites, setup commands, code patterns, edge cases, troubleshooting table, and verification checks) into a permanent, reusable `SKILL.md` file saved to `/app/workspace/skills/<skill_name>/`. The Brain dynamically loads and follows these skills via `load_skill`.
* **The Decider (The Reflex & Snap Judgement):** A specialized, ultra-fast decision-making subagent powered by a local non-autoregressive decision model (e.g., Clef Flash 9B via Ollama's `/v1/systemone` endpoint). While the Adviser provides deep, multi-step strategic reflection, the Decider acts as the Brain's System-1 reflex. It delivers instantaneous single-step rulings—such as categorical selections (`choice`), boolean validity/safety gates (`boolean`/`noul`), or ordinal confidence ratings (`score`)—with sub-second latency and mathematically calibrated probability distributions without consuming large generation budgets.
* **Auxiliary LLM (`query_auxiliary_llm`):** An auxiliary backup tool allowing the Brain to dynamically spawn external LLM sub-agents when primary specialized sub-agents (Coder, Analyst, Adviser, Architect, Decider) cannot handle the requirement—such as requesting second opinions, comparing model outputs, or targeting external cloud models (Gemini, GPT-4o, Claude) via LiteLLM proxy for specialized reasoning or multimodal tasks.
  * *Preset & Custom Endpoints:* Can pick from pre-configured zero-trust endpoint presets defined in [`llm_config.py`](./llm_config.py) (`ollama` on local port 64165, `litellm_proxy` on port 4000 for cloud models via host LiteLLM gateway) or connect to any custom URL. The fallback default is set by `DEFAULT_AUXILIARY_ENDPOINT = "ollama"`. When routing through `litellm_proxy`, supply provider-namespaced model IDs matching the proxy's routes: Google directly (`gemini/gemini-3.8-flash`), OpenAI directly (`openai/gpt-6-luna`), and other providers via OpenRouter (`openrouter/anthropic/claude-haiku-5.5`).
  * *Authentication & Long-Running Timeouts:* Supports inserting custom API keys on the fly and configuring request timeouts up to 1 hour (`timeout_seconds=3600`) for deep reasoning or heavy generation workloads.
  * *Multimodal, File Output & Parameter Tunability:* Supports arbitrary system and user prompts with background context injection (`context_filepaths`), multimodal file attachments via `media_filepaths` (images, audio, video, PDFs), direct file saving (`output_filepath` into `/app/workspace/outputs/` or `/app/workspace/sandbox/` with inline preview truncation to prevent context bloat), and full tuning across sampling parameters (`temperature`, `top_p`, `max_tokens`, `reasoning_effort`, `response_format={'type': 'json_object'}`, `presence_penalty`, `frequency_penalty`, `stop`, `seed`, `extra_body`, and `extra_headers`).
  * *Endpoint Introspection:* Includes `action="list_endpoints"` to view pre-configured preset URLs and `action="list_models"` to discover available models on any target server.
* **Dynamic Payload Airlocks:** The framework utilizes a pre-flight token calculator (via `tiktoken`) for all sub-agent delegations. If the Brain attempts to send a 100,000-token log file to the Analyst or Adviser that exceeds 90% of their specific context window, the framework intercepts the request, blocks the API call, and bounces a `SYSTEM ERROR` back to the Brain instructing it to use `grep` or chunk the files first. This completely eliminates silent API truncation and OOM crashes.
* **Pointer-Based Context Delegation & Continuous Tool Pruning:** To prevent the Overseer from running out of working memory, the system uses an architectural pattern where large codebaselines, historical logs, or environment files are never read directly into the main chat window. Instead, the Overseer passes absolute string paths via the `context_filepaths` parameter available across the `Coder`, `Adviser`, `Architect`, `Decider`, and `Auxiliary LLM` sub-agents. The background tool engine processes the files ephemerally, shielding the main orchestration loop from context bloat and avoiding unexpected memory wipeouts. Furthermore, prunable tool outputs older than the 10 most recent prunable tool calls that exceed the size threshold (`TOOL_PRUNE_MIN_CHARS = 1500`) are automatically aged via `compact_historical_tool_outputs`. They are archived to disk at `/app/workspace/histories/tool_outputs/tool_output_<sha1>.txt` with full metadata recorded in `manifest.json`, and replaced in active memory with lean pointers and 300-character Head + Tail previews. When context limits are reached, the background Summarizer executes **Targeted Handoff Summarization**, synthesizing a transition report anchored to the user's goal, `active_plan.md`, and deliverables on disk, alongside a Transition Handoff Directive resetting the Brain into a pristine context without post-compaction cycling or redundant compaction calls.

### 2. Polyglot Auto-Retry & Error Transparency
LLMs occasionally make syntax errors when generating tools. The framework isolates this through automated language-specific validation pipelines:
* **Python**: Validated via `py_compile`. Custom PyPI packages declared via `# REQUIRES: <package>` comments are automatically installed into the persistent `custom_packages/` directory before execution.
* **JavaScript / TypeScript**: Syntax-checked via `node --check` (and `esbuild` transpilation + `node --check --input-type=module` for TypeScript), followed by a `--help` behavioral startup smoke test.
* **Rust**: Verified in an isolated Cargo scratchpad (`cargo check -q`). Optional crate dependencies declared via `// REQUIRES: crate = "version"` comments are automatically parsed, added to `[dependencies]`, and propagated into the execution blueprint.
* **C++**: Pre-flight parsed using `g++ -fsyntax-only -std=c++17` before linking.
* **Multi-Language Tool Creation (`create_tool`)**: The Overseer delegates code generation to the Coder by explicitly specifying `language` (`'python'`, `'rust'`, `'cpp'`, `'javascript'`, or `'typescript'`). The tool prunes raw source echoes from the MCP response to protect context budget, automatically commits verified tools to the session git repository, and returns a concise confirmation with an exact runtime Execution Blueprint.
* **Compiler Error Transparency**: If code validation exhausts retries (`config.MAX_TOOL_RETRIES`), the exact compiler or linter `stderr` (`last_validation_error`) is returned directly to the Brain, eliminating blind debugging.
* **Pre-Flight Surgical Syntax Checks**: The `edit_code` tool supports sequential multi-block search/replace and validates syntax (via `compile()` for Python, `node --check` for JS, and `g++ -fsyntax-only` for C++) before writing to disk, preventing syntax corruption.

### 3. Rich Hierarchical Discovery (The Registries)
To prevent overwhelming the AI with hundreds of tools and memories, knowledge is split into compact JSON indexes and detailed physical files:
* **Tool Registry:** Custom tools are categorized in `/app/workspace/state/tool_registry.json` (stored in the persistent `state/` directory, keeping tool metadata strictly separated from code in `tools/`). The Brain navigates this in two steps: checking high-level categories via `view_tool_registry`, then diving into specific category descriptions to learn exact CLI usage (by default returns concise paths, descriptions, and file existence checks, reserving full authoring objectives for `detailed=True` to minimize context overhead).
* **Memory Registry:** Long-term facts and summaries are indexed in `/app/workspace/state/memory_registry.json` with short descriptions and timestamps. The exhaustive details are saved as individual Markdown files (`.md`) in the `memories/` folder. The Brain can browse the lightweight index and only spend tokens to read the full file when explicitly needed via `read_memory`.
* **Skills Registry & Host Skill Packs:** Standard operating procedures (SOPs) are organized as self-contained skill folders (`SKILL.md` with YAML frontmatter plus optional `scripts/`, `references/`, and `assets/` folders). Inside a session, active skills live under `/app/workspace/skills/<skill_name>/SKILL.md` and are dynamically cataloged in the Brain's system prompt (`AVAILABLE SKILLS MENU`) so the Brain can activate full instructions on demand via `load_skill` without bloating initial context. Reusable skill collections on the host live in `./skills/<source>/<category>/` (with source attributions and licenses maintained inside each respective subfolder). By default, no host skills are loaded into a new session; you can selectively copy skill packs or individual skills into a session at startup (`--skills`, `--all-skills`) or live in chat (`/skills on <pack|skill|all>`, `/skills off <pack|skill|all>`, `/skills save <skill> <pack>`), giving the container an isolated writable copy while keeping your master `./skills/` library untouched on the host.

### 4. Persistent Sessions & Isolated Environments
Workspaces are strictly isolated. The system dynamically generates unique `Session_ID` folders. 
* **State-Driven Restart:** The framework operates entirely on state files, not temporary RAM. When you resume a session, the Brain instantly re-loads its `current_history.json` and perfectly remembers exactly where it left off, alongside all its custom tools, stored memories, and master plans. 
* **Layered Micro-Environments (Base + Delta):** The heavy, complex dependencies (like C++ libraries and Chromium) are locked safely in the container's base Pixi image. When the AI installs a new third-party library for a specific tool, it uses a background `pip --target` injection. This places the tiny Python files directly into a `custom_packages` folder inside that specific session's mapped workspace, keeping your hard drive completely free of 2GB redundant bloat while retaining perfect session persistence.
* **Bioconda Injection:** Every new workspace is automatically configured to pull from the `bioconda` conda channel, giving the AI immediate access to thousands of pre-compiled bioinformatics tools.

### 5. Fully Asynchronous Streaming & Concurrent Multi-Tool Waves
The system utilizes AsyncOpenAI for fully asynchronous streaming across all agents. Both the main Brain on the host and the background FastMCP tools (Coder, Summarizer, Adviser, Analyst, Architect, Decider) use `async/await` (with synchronous SQLite queries offloaded via `asyncio.to_thread` and workspace state mutations guarded by `_WORKSPACE_STATE_LOCK`). When the Brain dispatches multiple tool calls in a single turn, the Overseer automatically partitions them into dependency-safe execution waves (`asyncio.gather` capped at 30 concurrent tools per wave via `MAX_CONCURRENT_TOOLS = 30`)—isolating foreground `execute_bash`, producer $\leftrightarrow$ consumer chains (`batch_generate_embeddings` $\leftrightarrow$ `query_sqlite_db`, `create_tool` $\leftrightarrow$ `view_tool_registry`), and overlapping file targets into sequential waves while running independent I/O, web, background bash, and sub-agent calls in true parallel. When a massive model takes up to 90 seconds to "think," the Python event loop remains unblocked, ensuring the background stdio connection to the Podman container never starves or drops.

### 6. Strict JSON Schema Pipelines
To guarantee system stability, background agents (like the Summarizer) do not rely on standard prompting. They use OpenAI's native Structured Outputs (`"strict": True`). This enforces mathematical compliance at the API level, ensuring the AI cannot hallucinate a broken comma or invalid schema that would corrupt the `current_history.json` or registry files.

### 7. The "AI Scientist" Super-Suite
To prevent the AI from reinventing the wheel with complex Python scripts, the sandbox natively includes powerful system-level capabilities:
* **Hardware Acceleration:** Native GPU passthrough (via NVIDIA CDI) allows the AI to dynamically install CUDA toolkits via Pixi and execute hardware-accelerated machine learning scripts.
* **Native Vector Database (Context-Bypass Architecture):** Equipped with `sqlite3` and the C-based `sqlite-vec` extension. The framework uses a strict Two-Table relational schema to link metadata directly to high-speed vector tables.
* *Zero Context Bloat:* The framework uses dedicated tools to shield the LLM from massive vector arrays. To ingest bulk data, the Brain uses the `batch_generate_embeddings` tool (`vec_table`, alias `target_table`, and `source_query`), which generates embeddings and natively injects them into the two-table schema in milliseconds. To query the database, it passes a `search_text_to_embed` parameter to the SQL tool. The massive floating-point arrays never touch the LLM's context window, drastically reducing token costs and preventing hallucination.
* **Zero-Cost Web Search & Stealth Scraping (`search_web`, `fetch_webpage`):** The framework natively routes queries through a headless Chromium browser. By patching the browser with **Playwright-Stealth** and querying legacy HTML search portals, the AI visually scrapes live search results (`search_web`) and reads full JavaScript-rendered pages (`fetch_webpage`) as a spoofed human user, effortlessly bypassing Cloudflare, API shadow-bans, and WAF bot-protections. DuckDuckGo `uddg` redirect URLs are cleanly extracted, and academic papers/articles automatically have scholarly metadata (`citation_*`, `dc.*`, OpenGraph, DOI, authors) extracted and prepended as structured headers before layout stripping.
* **Native Multi-Modal Vision & Dynamic Log/File Slicing (`analyze_files`):** The framework isn't just limited to text. By pointing the Analyst profile to a Vision-Language Model, the AI framework can "see." The Brain can autonomously ask the Analyst to review generated charts, describe UI screenshots, or inspect visual data. For documents and media, `analyze_files` automatically pre-processes payloads: PDFs are extracted to text via `pdftotext` (with `pypdf` fallback, enabling full regex filtering and line slicing), video files have representative keyframes sampled via `ffmpeg` alongside `ffprobe` stream metadata, and audio files have metadata extracted with guidance routing to Whisper via `execute_rest_call`. For text analysis and debugging, `analyze_files` dynamically scales up to 80% of the Analyst's context window (~280,000 characters / ~70,000 tokens per file), features native regex filtering (`filter_pattern="<regex>"`, `context_lines=N`) for instant targeted grep-like scanning across massive files with surrounding context and line numbers, pre-flight fast-failing for 0-byte files, evaluates API `finish_reason` to flag token-limit truncations, reports exact coverage boundaries, and provides surgical line slicing (`start_line`, `max_lines`) as well as reverse tail scanning (`tail_mode=True` to inspect the end of huge logs up to the budget).
* **Sub-Second Reflex & Fast Decision Gating (`fast_decision`):** For tactical branch routing, hypothesis evaluation, safety checks, or parameter triage, the Overseer can trigger instantaneous non-autoregressive rulings via `fast_decision` rather than spending heavy tokens on autoregressive reasoning. Powered by Ollama's `/v1/systemone` endpoint (e.g., `clef-flash:9b`), it accepts typed evaluation requests (`choice` categorization with custom options, `boolean` yes/no criteria, `score` severity levels, or raw multi-question schemas). It supports pointer-based context ingestion (`context_filepaths`) and multimodal image evaluation (`image_filepaths`), returning deterministic verdicts, confidence probability distributions, and categorical argmax level labels for ordinal scores (e.g., `2.41 → [High]`) in a single forward pass.
* **Context-Isolation Pattern (Universal Ingestion):** Large source assets, configuration mappings, and logs are strictly airlocked away from the Overseer's active working memory. While the `analyze_files` tool routes data structures to the Analyst, the framework features native file extraction helpers (`gather_agent_context`) wired directly into tool triggers. When files are referenced via `context_filepaths`, the host reads up to 40,000 characters per asset, calculates the exact token footprint via `tiktoken` to guard against hardware boundaries, and prepends the codebase seamlessly into the sub-agent’s prompt under a specialized `=== ATTACHED AGENT CONTEXT BACKGROUND ENVIRONMENT ===` header.
* **Massive Data Processing:** Equipped with `aria2c` for high-speed parallel downloads and `pigz`/`zstd` for instantaneous decompression of massive genomic/data payloads.
* **Media & Documents:** Pre-loaded with `tesseract-ocr`, `poppler-utils` (for direct PDF extraction), and `ffmpeg`.
* **Scientific Rendering & Reporting:** Uses `graphviz` to render complex networks and `pandoc` to compile the AI's markdown findings directly into HTML, Word, or scientific PDF formats.

### 8. Master Planning & Self-Correction
To prevent the Brain from getting lost in the weeds of complex multi-step tasks, the framework utilizes persistent strategic states:
* **The Master Plan:** The Brain maintains a persistent Markdown file (`active_plan.md`) in its state folder to track overall objectives and checklists. It reads this plan upon waking up and seamlessly updates it when milestones are reached.
* **The Red Team Loop (Adviser):** If the Brain gets stuck on a coding error or logical dead end, it uses the `consult_adviser` tool. This packages the current plan, registries, and the encountered problem, and sends it to a Senior Adviser LLM. The Adviser generates a timestamped strategic advisory report saved to disk, enabling true autonomous self-correction without user hand-holding. If the report exceeds 8,000 characters, context guardrails automatically truncate the feedback preview to the first 3,000 characters in conversation memory while keeping the full document on disk.

### 9. Intelligent Loop Detection & Circuit Breakers
To prevent the AI from burning tokens in infinite error loops, the Overseer utilizes four layered failsafes:
* **The Micro-Breaker (Failure Streaks):** The script tracks consecutive errors per tool. If the AI goes "blind" and fails to use the exact same tool 3 times in a row, the system forcefully intercepts the prompt with a `[CRITICAL SYSTEM ALERT]`, snapping the AI out of its apology loop and commanding it to pivot its strategy.
* **The Soft Tool-Call Loop Advisory (Repeated Identical Calls):** The script fingerprints `(tool_name, arguments)` across every invocation. If the AI calls the exact same tool with byte-identical arguments 5 consecutive times (regardless of success, such as repeated redundant plan reads or status checks), a non-blocking `[SYSTEM LOOP ADVISORY]` is appended to the tool result every 5 repetitions. This prompts self-reflection without restricting legitimate polling or repeated edits.
* **The Macro-Breaker (Runaway Trains):** The script tracks uninterrupted tool chains. Every 100 tool calls, it triggers a "Soft Pause" by injecting a system prompt that forces the Brain to self-evaluate. If the AI hits 300 consecutive tool chains, the system executes a "Hard Stop," forcing the AI to await human intervention.
* **The Real-Time Stream Circuit Breaker (Anti-Trapping):** Large Language Models occasionally encounter attention loops where they repeat multi-paragraph blocks of text or internal logic sequences. To arrest this, the framework evaluates incoming data streams chunk-by-chunk across two independent channels: the standard text content channel (`full_content`) and the model's internal reasoning phase (`full_thinking`). 
  * *Alphanumeric Normalization:* The detector normalizes text in real time by stripping all whitespaces, newlines, and punctuation formatting noise (`re.sub(r'[^a-zA-Z0-9]', '', text)`), preventing the model from bypassing detection loops by altering paragraph layouts or spaces.
  * *Threshold & Backtracking:* The circuit breaker allows exactly 3 identical sequences to execute (original text plus 2 loops) and instantly trips on the first token of the 4th attempt. Rather than cutting text blindly by percentage slices, a dedicated helper (`clean_tail_by_alphanumeric_count`) backtracks from the end of the string, stripping out character bytes until it hits the exact boundary of the third iteration, preserving pristine trailing word boundaries without splitting words in half.
  * *Autonomous Self-Healing Retries:* The moment a loop is aborted, the system appends a hard `[CRITICAL INTERVENTION]` warning statement directly into the history array, resets the local turn boundaries, and issues an asynchronous loop `continue`. This forces an immediate, automatic turn re-generation on the spot—intercepting the loop, injecting strategic course corrections, and executing a structural fallback tool (such as `consult_adviser` or `manage_plan`) without dropping the execution stream or stopping to wait for manual human keyboard inputs.
* **Autonomous Batch Guard & Two-Turn Termination (`-x` mode):** When running in non-interactive batch mode (`-x`), the system prevents premature exits caused by single transitional status statements (e.g. *"Now writing the interpretation document"*). If the Brain outputs a turn without invoking any tools, an autonomous audit nudge (`[SYSTEM AUTONOMOUS AUDIT]`) is injected into the context. The process terminates only upon receiving two consecutive no-tool turns, ensuring multi-step pipelines drive relentlessly until all deliverables are verified on disk.
 
### 10. Perfect Temporal Awareness (The Live Clock)
Traditional agents lose track of time during long workflows. This framework utilizes a "Sweep & Replace" dynamic time injection. On every single API turn, the AI's context is injected with a Live System Clock down to the exact second. This allows the AI to accurately timestamp files and memories, without bloating the permanent `current_history.json` log with stale timestamps.

### 11. Self-Healing JSON Interception & Robust Payload Extraction
Smaller or quantized models occasionally hallucinate malformed JSON or wrap payloads in markdown code blocks. The framework features an intelligent JSON extraction engine (`extract_json_payload`) across all subagents:
* Parses pure valid JSON directly without destructive regex stripping.
* Strips outer markdown wrappers (` ```json ... ``` `) cleanly while preserving nested code fences inside JSON string fields (e.g. Coder edits touching markdown or Analyst reports quoting HTML/code).
* Extracts outermost object/array structures from conversational commentary.
* The MCP server intercepts downstream `JSONDecodeError` failures and returns a structured `SYSTEM ERROR` directly to the LLM, prompting autonomous syntax repair without script crashes. Furthermore, if an active stream trips the repetition circuit breaker, incomplete structural fragments are omitted to maintain pristine state tracking.

### 12. On-Demand Observability & Container Logging
Standard agents crash silently when background dependencies or subprocesses fail. This framework features unified, multi-layered observability:
* **Host Interaction Logs:** Stored at `sessions/<Session_ID>/logs/chat_log_<timestamp>.txt`, capturing full conversation turns, streaming tokens, tool calls, execution latencies, full LLM reasoning tokens (including Brain streaming fallback `<think>` extraction), intercepted subagent thoughts, Decider rulings (`<___DECIDER_RULING___>`), and full Coder generated code/edits archived verbatim via `<___CODER_CODE___>` tags under `=== CODER (HIDDEN) === --- GENERATED CODE / EDIT ---`.
* **Structured Container Debug Log:** Handled by a dedicated `SandboxMCP` logger (`level=logging.INFO`) writing directly to `/app/workspace/logs/container_debug.log`. All tool executions, subprocess commands, compilation checks, syntax validation errors, security enforcement events (destructive `rm` commands, path traversal, forbidden source file writes), search string mismatches, and database transactions are structured and timestamped.
* **Synchronized Clock Across Boundaries:** The Podman container runs with `--tz=local`, locking container log timestamps to the host system time to the second, making cross-boundary debugging instant.
* **Self-Debugging Pipeline:** If a tool fails unpredictably, the Brain delegates the log file to the Analyst sub-agent via `analyze_files(["/app/workspace/logs/container_debug.log"])`. The Analyst extracts the stack trace and pinpoint cause, enabling the Brain to patch the issue or install missing packages autonomously.

### 13. Dynamic UI & Verbosity Control
The Overseer natively decouples **Format** (how it looks) from **Verbosity** (how much it tells you). You can run it in `markdown` mode for a rich, color-coded terminal dashboard, or `text` mode for raw streaming. Furthermore, you have total runtime control: you can instantly change the UI by typing slash commands (`/silent`, `/minimal`, `/standard`, `/detailed`, `/text`, `/markdown`) directly into the chat prompt. This allows you to jump from a highly detailed matrix-style debugging view to a minimal, clean UI on the fly without ever restarting your container or dropping your session state!

### 14. External & Host MCP Servers (Intentional Tool Bridging)
While untrusted code creation and execution remain strictly quarantined inside the rootless Podman sandbox, the Overseer Brain can intentionally connect to external Model Context Protocol (MCP) servers outside the container (running on the host machine, in WSL2, or across local networks):
* **Dedicated Python Configuration (`mcp_config.py`):** External endpoints are registered cleanly in [`mcp_config.py`](./mcp_config.py) as a Python dictionary (`EXTERNAL_MCP_SERVERS`). It features automatic host IP resolution (`resolve_host_ip()`), transport options (`sse` or `stdio`), and custom tool prefixes without any external file parsing.
* **The `manage_external_mcp` Meta-Tool (Brain Self-Management):** The Brain possesses a built-in meta-tool (`manage_external_mcp`) allowing it to autonomously inspect available servers (`action="list"`), attach new tool suites (`action="connect"`), and detach them when finished (`action="disconnect"`).
* **Universal Custom MCP Connections:** Beyond pre-configured servers, the Brain can dynamically attach *any* universal MCP server on the fly by providing a `server_name`, `address` (for SSE, e.g. `http://{HOST_IP}:9000/sse` or `http://host:9000/sse`) or `command`/`args` (for stdio, e.g. `npx`), and an optional `prefix`. The harness automatically resolves `{HOST_IP}`, `{host}`, and `host.containers.internal` aliases to the real gateway IP.
* **Session Persistence:** When an MCP server is connected (either by the user via slash commands or by the Brain via `manage_external_mcp`), active connections are persisted to `state/active_mcp_servers.json`. Resuming the session later automatically reconnects all active tool suites.
* **Zero-Trust by Default & Offline Fail-Safe:** All external servers are disabled by default (`enabled: False`) and strictly optional (`optional: True`). If an external server is enabled but offline or unreachable, the harness gracefully skips it with a warning notice and continues without crashing.
* **Isolated Worker Concurrency Architecture:** External MCP connections run in dedicated, decoupled asyncio background tasks (`mcp_worker`). This isolates AnyIO cancel scopes, completely preventing concurrency conflicts or stream teardown issues with the main sandbox container's stdio stream.
* **Included Test Server (`mock_mcp_server.py`):** A lightweight FastMCP SSE server included directly in the workspace for instant end-to-end testing. Run `pixi run python mock_mcp_server.py` in a separate terminal (listens on port 8888), then connect via `--mcp mock` or `/mcp on mock` to verify external tool routing (`mock_get_host_time`, `mock_multiply_numbers`) without requiring external software like ChimeraX.
* **Included ChimeraX FastMCP Bridge (`chimerax_mcp_server.py`) & Security Firewall:** A lightweight FastMCP SSE server running directly in WSL2 that bridges the Brain to UCSF ChimeraX on the Windows 11 host (`remotecontrol rest start port 21049`).
  * **Host Protection Firewall:** ChimeraX's raw REST API inherently permits arbitrary Python execution on the host (`runscript` / `open script.py`). The included FastMCP bridge protects your Windows machine by acting as a strict security barrier:
    * Raw Python execution (`run_python`) is never exposed to the AI.
    * Command chaining (`;`, `&`, `|`, `` ` ``, `$`, newlines) is strictly blocked.
    * Dangerous keywords and execution terms (`python`, `runscript`, `system`, `powershell`, `cmd`, `bash`, `exec`, `eval`, `subprocess`, `os.`, `sys.`) are blocked via regex.
    * Executable script extensions (`.py`, `.exe`, `.bat`, `.cmd`, `.ps1`, `.sh`) on file targets are rejected.
    * All commands are strictly validated against a structural biology verb whitelist: `open`, `color`, `label`, `surface`, `hide`, `show`, `view`, `matchmaker`, `align`, `save`, `close`, `set`, `style`, `coulombic`, `alphafold`, `size`, `transparency`, `lighting`, `camera`, `info`, `select`, `sym`.
  * **Exposed Safe Tools:** `cx_run_command`, `cx_open_structure`, `cx_save_image` (renders snapshots directly into the shared host input directory `my_host_input` with automated base64 network bridge fallback), `cx_close_models`, `cx_get_status`.
  * **Security Boundary & Direct REST Bypass Mitigation:**
    * *The Threat Model:* ChimeraX's native REST API has **zero authentication** (no tokens, passwords, or origin verification). If the containerized Brain or a compromised script can send HTTP requests directly to port 21049 on the host, it could theoretically bypass the MCP whitelist by instructing ChimeraX to execute a script written to the shared WSL2 directory (`\\wsl.localhost\Ubuntu-26.04\...`).
    * *Defense-in-Depth Protections:*
      1. **Keep ChimeraX on Localhost:** Always leave ChimeraX bound to `127.0.0.1`. Never bind it to `0.0.0.0` or create external `netsh portproxy` rules that expose port 21049 across external interfaces.
      2. **Container Egress Filtering (Recommended):** To ensure the sandbox container can never bypass the FastMCP bridge, block container outbound access to port 21049 in WSL2:
         ```bash
         sudo iptables -I FORWARD -p tcp --dport 21049 -j DROP
         ```
         This ensures only host-level processes (like `chimerax_mcp_server.py`) can reach the ChimeraX REST server.
      3. **Confirmation Gate:** You can set `"require_confirmation": True` in `mcp_config.py` to require interactive `[y/N]` human approval before any ChimeraX action is dispatched.
      4. **Mandatory Procedural Stop:** Always stop the REST listener in ChimeraX as soon as your visualization tasks are done:
         ```
         remotecontrol rest stop
         ```
  * **Usage:** Run `pixi run python chimerax_mcp_server.py` in a separate terminal (listens on port 21050), then connect in chat via `/mcp on chimerax`. File exchanges use `my_host_input` (`\\wsl.localhost\Ubuntu-26.04\home\agent\ai_workspace\my_host_input`).
* **Security Gate & Interactive Confirmation:** External servers configured with `require_confirmation: True` (and all universal custom MCP connections) pause execution and require explicit user confirmation (`[y/N]`) in the terminal before establishing connections or running tools. In unattended batch mode (`-x`), dangerous actions are automatically denied to protect unattended runs.
* **Dynamic On-the-Fly Toggling:** In addition to startup CLI flags (`--mcp <name>`, `--all-mcp`), you can activate or deactivate external tool suites live during a chat using in-chat slash commands (`/mcp`, `/mcp on <name> [<url>]`, `/mcp off <name>`).
* **Full Transparency & Context Injection Logging:** Every external tool suite connection or disconnection displays a clean, non-duplicated status update in the terminal. In standard and detailed verbosity modes, the exact registered tool schemas (function names, signatures, and descriptions) are formatted and rendered in the console. Whenever external MCP state alters the Brain's prompt history, the exact text is rendered under `=== INJECTED CONTEXT TO BRAIN ===` and permanently recorded in the session chat log (`chat_log_*.txt`) alongside full tool schemas in `=== MCP REGISTRY ===`.

### 15. Universal REST API Dispatch (`execute_rest_call` - Auxiliary Tool)
Beyond native sub-agent delegation (`query_auxiliary_llm`), the framework features a generalized REST execution tool (`execute_rest_call`) that serves as an auxiliary escape hatch when the AI needs to query arbitrary local or remote models, Ollama endpoints, vLLM servers, or web APIs using file-based or inline JSON payloads (strictly for external API queries or custom microservices, not for routine agent orchestration):
* **File-Based Provenance & Script Integration:** Eliminates shell quotation escaping by reading request bodies directly from `.json` files on disk (`request_filepath`) or accepting inline dictionaries (`payload_json`), and optionally writing extracted responses directly to a target file (`response_filepath`). Sub-agents (like the Coder) or scripts can assemble multi-model prompt variations into files and dispatch them cleanly in a single turn.
* **Paired Timestamped Archiving:** Automatically pairs and archives both the request payload (`state/rest_calls/req_<timestamp>.json`) and the raw response (`state/rest_calls/resp_<timestamp>.json`), while continuously updating `state/rest_calls/latest_response.json` for effortless chaining across tools and sub-agents.
* **Automatic Host Network Translation:** Inside the rootless Podman sandbox, requests targeting `localhost` or `127.0.0.1` are automatically resolved to `host.containers.internal`, ensuring seamless connectivity to local Ollama, vLLM, LM Studio, or LiteLLM servers running on the host without loopback connection failures.
* **Smart Content & Thinking Extraction:** Automatically parses generated text and reasoning tokens from standard LLM schemas (OpenAI, vLLM, Ollama native, Anthropic, Gemini) and supports arbitrary dot-notation extraction (`extract_field="choices.0.message.content"` or `extract_field="data.embedding"`).
* **Multipart File Uploads (`files`):** Supports uploading files directly via `multipart/form-data` using a dictionary mapping field names to local paths (e.g. `files={"file": "/app/workspace/audio.wav"}`). Perfect for speech-to-text endpoints like Whisper (`POST /v1/audio/transcriptions`), Ollama blob endpoints, or file ingestion APIs.
* **Multimodal Chat Injection (`media_filepaths`):** When dispatching chat completions JSON payloads, passing `media_filepaths` automatically base64-encodes local images, audio, video, or PDFs and injects them into the message structure without requiring manual base64 scripting.
* **Token Usage Observability:** Automatically tracks input, output, and reasoning tokens in `state/token_usage.json` under the `rest_call` bucket when usage metrics are reported by the upstream API.

### 16. Host Skills Library & On-Demand Skill Packs (`/skills`)
SMRTk natively supports modular skill folders (`SKILL.md` with YAML frontmatter `name` & `description`, alongside optional `scripts/`, `references/`, and `assets/` subdirectories). Rather than loading every skill into every session by default (which would bloat the system prompt), SMRTk treats host skills similarly to external MCP servers:
* **Hierarchical Host Library (`./skills/`):** Master skill collections live on the host under `./skills/` (`config.HOST_SKILLS_DIR`), organized into `<source>/<category>/<skill_name>` subfolders (e.g., `development`, `creative-design`, `enterprise-comms`, `science`). Source attributions and open-source license notices for external skill packs are always documented directly inside each respective `./skills/<source>/` subfolder (`NOTICE.md` / `LICENSE.txt`).
* **Copy-on-Load Isolation (Host Library Untouched):** By default, a new session starts with zero pre-loaded host skills. When a pack or skill is mounted (via CLI flags, `/skills on`, or the Brain's `manage_skills` tool), the harness copies those skill directories into the session's isolated `./sessions/<Session_ID>/skills/` folder (visible inside the container at `/app/workspace/skills/`). The agent can freely execute bundled helper scripts, modify templates, or refine the `SKILL.md` inside its session without ever altering the master files in `./skills/` on the host.
* **The `manage_skills` Meta-Tool (Brain Self-Management):** Mirroring `manage_external_mcp`, the Brain possesses a built-in `manage_skills` tool (`action="list" | "mount" | "unmount" | "save"`) and sees a compact 1-line-per-pack summary of unmounted host packs at the bottom of its system prompt. The Brain can autonomously inspect host skill descriptions, mount needed packs/skills into `/app/workspace/skills/`, unmount them when finished, or even auto-mount an unmounted host skill in one step simply by calling `load_skill("<skill_name>")`.
* **Flexible Target Matching:** You (or the Brain) can load or unload an entire top-level source collection (`/skills on <source>`), a category subfolder (`/skills on <source>/<category>` or shorthand `/skills on development`), a single skill (`/skills on mcp-builder` or `/skills on webapp-testing`), a comma-separated list (`/skills on mcp-builder,webapp-testing`), or everything (`/skills on all`).
* **Exporting Session Skills to Host (`/skills save`):** When the Architect agent creates a new skill during a session via `create_skill` (or improves an existing one), you can permanently promote it from the session back into your host library using `/skills save <skill_name> <pack_subpath>` (e.g. `/skills save my_pipeline science/genomics`).

### 17. Background Execution & Asynchronous Sub-Task Delegation (`execute_bash(background=True)`, `delegate_task`, `manage_jobs`, `/jobs`)
While standard tasks run synchronously, the Overseer can launch long-running bash processes or asynchronous sub-agent tasks in the background while continuing to code, inspect files, or run foreground steps in parallel:
* **Background Bash Execution (`execute_bash(command, background=True)`):** Launches a non-blocking subprocess inside the sandbox with its own process group (`start_new_session=True`), streaming `stdout`/`stderr` line-buffered to `/app/workspace/state/jobs/job_<N>.log` and returning a `job_id` immediately. Defaults to a **60-minute (`3600s`) timeout** and allows up to **10 hours (`36000s`)** for long simulations, training runs, builds, or persistent background daemons/servers.
* **Asynchronous Sub-Agent Delegation (`delegate_task`):** Allows the Brain to dispatch independent research, analysis, code-drafting, or auxiliary subtasks to a sub-agent (`agent="analyst" | "adviser" | "coder" | "auxiliary"`) with `context_filepaths`, `output_filepath`, `timeout_seconds=3600` (up to `36000s` / 10h), and `background=True` (default). The sub-agent runs asynchronously in the background while the Brain continues working in the foreground.
* **Job Lifecycle Management (`manage_jobs` & `/jobs`):** Both the Brain (`manage_jobs(action="list" | "status" | "wait" | "kill", job_id=..., wait_seconds=...)`) and the user (`/jobs`, `/jobs status <id>`, `/jobs kill <id|all>`) can inspect live job states, tail progress logs, wait for a specific job or `"all"` running jobs at once, or terminate process groups (`os.killpg`).
* **Hard Concurrency Caps (`MAX_BACKGROUND_JOBS = 9`, `MAX_BACKGROUND_SUBTASKS = 9`) & Synchronous-by-Default Guardrails:** To prevent runaway spawning or resource starvation, the system enforces a hard limit of **9 concurrent background bash jobs** and **9 concurrent background delegated subtasks**, alongside a strict *Synchronous-by-Default Policy* in the Brain's system prompt prohibiting trivial commands or sequential dependencies from being backgrounded.
* **100% Observability & Non-Interleaved Turn Harvesting:** Background jobs never corrupt live terminal streaming. Every background job writes a dedicated log/trace file under `/app/workspace/state/jobs/` (`job_<N>.log` for bash; `job_<N>_<agent>.md` for sub-agents containing the full task prompt, complete `<thinking>` reasoning trace, full output, and token metrics). Sub-agent token usage is recorded atomically in `state/token_usage.json`. Between turns (`harvest_background_jobs`), the host orchestrator harvests completed jobs, appends their full `<thinking>` and output blocks cleanly into `logs/chat_log_<timestamp>.txt`, and injects a `[SYSTEM NOTICE: Background ...]` notification into the Brain's context (skipping duplicate previews if the Brain already retrieved the result via `manage_jobs(action="wait" | "status")`).

---

## 🛡️ Sandboxing & Workspace Structure (Podman)

**CRITICAL WARNING:** This AI has the ability to execute bash commands and write files. To prevent it from modifying your host system or deleting crucial files, the MCP environment runs inside a heavily hardened, rootless **Podman Container**.

* **Non-Root Execution:** The AI runs as a restricted, non-root `agent` user inside the container. Podman handles UID mapping dynamically (via `--userns=keep-id:uid=1000,gid=1000` and `:Z,nosuid,nodev` volume flags) to allow safe read/write access without root privileges.
* **Stripped Capabilities & Resource Limits:** The container drops all Linux capabilities (`--cap-drop=ALL`) and strictly limits the AI to 4 CPUs, 16GB RAM, and a hard limit of 1000 PIDs (`--pids-limit=1000`) to instantly neutralize bash fork bombs and resource-exhaustion attacks.
* **Anti-Zombie Process Hooks:** The container boots with the `--init` flag to perfectly manage PID 1 signals, and the host Python script uses `atexit` hooks. Even if you hard-crash the script via `Ctrl+D`, all nested bash processes, headless browsers, and stray daemons are instantly and cleanly slaughtered.
* **Stream Protection (Daemon Trapping):** The FastMCP server natively traps grandchild daemons (using `start_new_session=True` in subprocesses) and aggressively gags Python background logging. This ensures that stray warnings or detached background servers never corrupt the JSON-RPC communication streams.
* **The I/O Airlock (Surgical Mounts):** The container cannot see your host filesystem. It communicates via strict bind mounts:
  * `/app/host_input`: Mapped to your local input folder (`my_host_input`). **Strictly Read-Only (ro)**. The AI cannot delete or corrupt your source data. You must manually drop files (e.g., CSVs, scripts, or documents) into this host folder before asking the AI to analyze them. 
  * `/app/workspace`: The unified execution directory mapped to the active `Session_ID` folder with isolated subfolders (`logs/`, `tools/`, `histories/`, `histories/tool_outputs/`, `memories/`, `state/`, `state/reports/`, `state/failures/`, `state/jobs/`, `state/rest_calls/`, `sandbox/`, `outputs/`, `archive/`, `skills/`, `custom_packages/`).
  * `/app/*.py` & System Definitions: Core framework scripts, configurations, and documentation (`config.py`, `llm_config.py`, `mcp_config.py`, `sandbox_tools.py`, `smrtk.py`, `chimerax_mcp_server.py`, `mock_mcp_server.py`, `readme.md`, `Containerfile`, `setup_wsl2.sh`) are dynamically mapped **Strictly Read-Only (ro)** directly into `/app/` at runtime, and the `/app` directory itself is locked read-only (`chmod a-w /app`) at container boot to prevent file planting or module shadowing. This provides the containerized agents full self-awareness of the framework's configuration and tooling without exposing host files to tampering.
* **Workspace Isolation:** The `/app/workspace` is split to protect the AI's "mind" from its "hands":
  * `/sandbox`: The scratchpad where the AI actually executes bash commands and tests tools.
  * `/outputs`: The dedicated folder where the AI saves finished artifacts and generated files.
  * `/state`: Contains canonical framework state files at the root (`tool_registry.json`, `active_plan.md`, `memory_registry.json`, `token_usage.json`, `tool_usage.json`, `current_history.json`, `active_mcp_servers.json`) and organized subdirectories:
    * `/state/reports/`: Timestamped strategic & analytical markdown reports (`*_advice.md`, `*_analyst_report.md`).
    * `/state/failures/`: Structured sub-agent failure traces (`failure_*.json`).
    * `/state/jobs/`: Background job registry (`bg_jobs.json`) and live per-job logs/traces (`job_<N>.log`, `job_<N>_<agent>.md`).
    * `/state/rest_calls/`: Paired request/response archives for `execute_rest_call`.
  * `/histories`: Stores context compaction backups (`backup_history_*.json`) and `/histories/tool_outputs/` (aged verbatim tool outputs and `manifest.json`).
  * `/tools`: Where generated and registered tool source assets live (Python scripts, Rust/C++ code, TypeScript routines).
  * `/skills`: Where the Architect persists reusable SOP workflows (`SKILL.md`).
  * `/memories`: Where the detailed Markdown files live.
  * `/custom_packages`: Isolated directory for dynamically installed Python dependencies via pip.
  * `/archive`: The dedicated "Soft-Delete" trash bin.
* **Zero-Trust Architecture:** The container contains absolutely NO sensitive data, API keys, or `.env` files. The AI uses hardcoded dummy keys (e.g., `sk-sandbox-fake-key`) and routes all requests to a local `host.containers.internal` gateway. Authentication and routing are securely handled by a LiteLLM proxy running safely on the Windows/WSL host, making credential theft mathematically impossible.
* **Context Preservation (Head + Tail Truncation & Continuous Tool Pruning):** If the AI executes a bash command or scrapes a webpage that returns a massive payload (>10,000 characters), the system intercepts it. Instead of flooding the context window, the framework saves the complete verbatim output to a timestamped file inside the sandbox (`cmd_output_<timestamp>.txt`). It returns a balanced **Head + Tail preview** (first 1,500 characters and last 1,500 characters) showing both the initial invocation headers and final exit summaries/stack traces, alongside line counts and pointer instructions for `analyze_files`. Furthermore, as conversation turns progress, `compact_historical_tool_outputs` continuously prunes tool results older than the 10 most recent prunable tool calls that exceed the configurable threshold (`TOOL_PRUNE_MIN_CHARS = 1500`). It persists the complete verbatim output in `/app/workspace/histories/tool_outputs/tool_output_<sha1>.txt` and logs metadata in `manifest.json`, replacing in-memory text with a lean pointer and 300-character Head + Tail preview to eliminate quadratic context blowup while guaranteeing 100% data preservation.
* **Network Isolation:** The container runs on an isolated bridge network (`slirp4netns`). It communicates with local/tunneled LLMs strictly via the `host.containers.internal` gateway.
* **Anti-Deletion Protocol (Soft-Deletes):** The AI is strictly prompted against using destructive commands like `rm` or `DROP TABLE`. Instead, it is trained to use a robust "Soft-Delete" protocol. If it needs to rewrite a Python script or rebuild a database schema, it automatically uses the native `write_file` tool to create timestamped `.bak` copies, or uses bash `mv` to send old databases to the `/archive` folder. This provides a flawless audit trail and guarantees zero data loss during autonomous operations.
* **Strict Command Sanitization & Bash Runtime:** The `execute_bash` tool executes natively via `/bin/bash` inside `/app/workspace/sandbox` (ensuring full compatibility with bash built-ins, process substitutions, pipelines, and redirections). It employs token-level inspection and shell function overrides to block destructive file deletion (`rm`, `unlink`, `shred`, `find -delete`, and `git rm` without `--cached`), while appending a soft delegation advisory if inline scripts are detected. The `write_file` tool strictly limits file writes to `/app/workspace/outputs/` and `/app/workspace/sandbox/` and blocks path traversal (`../`) using `os.path.commonpath`.

---

## ⚡ Quick-Start: Automated WSL2 & Environment Setup (Recommended)

To avoid manually running dozens of terminal commands and editing system files, use the included automated setup script [`setup_wsl2.sh`](./setup_wsl2.sh). It provisions any Debian/Ubuntu-based WSL2 environment from start to finish:

1. **System Provisioning (Phase 1 / Root):** Installs system dependencies (`podman`, `catatonit`, `passt`, `slirp4netns`, `uidmap`, `git`, `jq`, `build-essential`), probes for NVIDIA GPU and configures CDI device passthrough (gracefully falls back to pure CPU mode on machines without a GPU), automates rootless `/etc/subuid` and `/etc/subgid` allocations for the execution user (defaulting to the dedicated isolated `agent` user), enables `loginctl` user lingering, and configures user isolation.
2. **User-Space Provisioning (Phase 2):** Installs Pixi (`~/.pixi/bin`), installs workspace dependencies via `pixi install`, interactively prompts for your primary LLM endpoint with provider presets (OpenRouter recommended for CPU machines, OpenAI, Google Gemini, local Ollama, or custom), generates LiteLLM proxy configuration (`~/litellm_proxy/config.yaml`), exports environment variables to `~/.bashrc`, updates [`llm_config.py`](./llm_config.py), auto-starts the LiteLLM proxy on port 4000, builds the rootless `sandbox-image` Podman container, and runs an automated sandbox verification self-test.

### WSL2 Prerequisites
* **WSL2 with systemd enabled (Required):** Rootless Podman, D-Bus session bus, and user cgroups require systemd. Verify or add to `/etc/wsl.conf`:
  ```ini
  [boot]
  systemd=true
  ```
  > **Note:** If you modify `/etc/wsl.conf`, you must execute `wsl --shutdown` from Windows PowerShell or Command Prompt, then reopen your WSL2 distribution to activate systemd as PID 1.
* **Script Execution Permissions:** Ensure the setup script is marked executable before running:
  ```bash
  chmod +x setup_wsl2.sh
  ```
* **Hardware Requirements:** Works on systems **with or without a GPU**. If an NVIDIA GPU is present, GPU passthrough is automatically configured; on systems without a GPU, the framework smoothly operates in CPU mode (ideal when pairing with OpenRouter or cloud LLM providers).

### Supported Setup Modes

The installer defaults to the isolated `agent` user and offers two installation paths:
* **Option A: Dedicated Hardened Environment (Default & Recommended):** Creates an isolated non-sudo `agent` user and hardens `/etc/wsl.conf` (disables Windows interop and automount) for maximum air-gapped sandboxing.
* **Option B: Existing User Account (Daily Driver):** Installs for your current user account (`ubuntu`, your custom username, etc.), configuring rootless Podman for your account while **preserving your existing `/etc/wsl.conf`, user login, and Windows `/mnt/c/` drives intact**.

### Running the Automated Setup:
Open your WSL2 terminal and run:

```bash
# Clone the repository and enter the directory
git clone <repo-url> ai_workspace
cd ai_workspace

# Ensure script is executable
chmod +x setup_wsl2.sh

# Run the complete automated setup (prompts for setup mode)
sudo bash setup_wsl2.sh
# (or: sudo ./setup_wsl2.sh)

# Or install explicitly for your existing user account without modifying wsl.conf:
sudo bash setup_wsl2.sh --user $(whoami)
```

### Script Commands & Diagnostics:
* `./setup_wsl2.sh --check`: Non-destructive diagnostics health check (reports live status for OS, WSL version, systemd PID 1, GPU/CDI, subuids, packages, Pixi, Podman image, and LiteLLM proxy port 4000).
* `sudo ./setup_wsl2.sh --system-only`: Runs only Phase 1 system provisioning (apt packages, GPU/CDI, subuids, user creation).
* `./setup_wsl2.sh --user-only`: Runs only Phase 2 user-space setup as current user (Pixi, dependencies, LiteLLM wizard, container build).
* `./setup_wsl2.sh --build-image`: Rebuilds the rootless Podman sandbox container (`sandbox-image`).
* `sudo ./setup_wsl2.sh -y`: Unattended installation accepting default air-gapped settings.

---

## 🛠️ Manual Step-by-Step Setup (Reference)

If you prefer to configure your environment manually rather than using the automated installer, follow the steps below:

### Step 1: Install System Dependencies & GPU Drivers
Your freshly installed Ubuntu instance needs a few core tools before we can begin. Log into your hardened WSL terminal and run the following commands to install Podman, Pixi, and the NVIDIA toolkit for GPU passthrough:

    # Update the system and install Podman with init and rootless networking tools
    sudo apt update && sudo apt install -y curl podman catatonit passt slirp4netns uidmap
    
    # Add NVIDIA Container Toolkit repository and install (For GPU Passthrough)
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg \
      && curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
        sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
        sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
    sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
    
    # Generate the CDI configuration so Podman can find the GPU
	sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml

    # Install Pixi
    curl -fsSL https://pixi.sh/install.sh | bash
    
    # Restart your terminal or source your profile to apply Pixi to your path
    source ~/.bashrc

### Step 2: Prepare Local Models
Ensure your local LLM server (like Ollama or vLLM) is running and you have pulled your designated Brain and Coder models. 
*(If using SSH tunnels to access remote GPUs, bind the tunnel to `0.0.0.0` so the Podman bridge can see it, e.g., `ssh -L 0.0.0.0:64100:127.0.0.1:8000 user@remote`).*

### Step 3: Initialize Host Pixi Environment (The Overseer)
If you cloned this repository, `pixi.toml` is already included in the workspace root. Simply install the pre-configured dependencies:

    cd ai_workspace
    pixi install

Alternatively, if configuring a fresh workspace manually from scratch, initialize Pixi and install all required dependencies (including `sqlite-vec` for vector operations):

    mkdir ai_workspace
    cd ai_workspace
    pixi init
    pixi add python openai mcp fastmcp prompt_toolkit rich tiktoken
    pixi add --pypi sqlite-vec

### Step 4: Set Up the Host Proxy (Zero-Trust Routing)
To keep secrets completely out of the sandbox, we run a LiteLLM proxy on the host machine. The AI uses fake keys, and the proxy attaches the real ones.

Open a terminal on your host machine (outside the sandbox) and initialize a dedicated routing environment:

    cd ~
    mkdir litellm_proxy && cd litellm_proxy
    pixi init
    pixi add python=3.12 litellm pip
    pixi run pip install 'litellm[proxy]'

Create a `config.yaml` file in that folder to map your models to their actual endpoints (Ollama, vLLM, a remote HPC cluster) and pull your real API keys from the host's environment variables. 
Example (includes option to remove unsupported flags):

    general_settings:
      master_key: sk-sandbox-fake-key  # Zero-Trust sandbox key matching llm_config.py

    litellm_settings:
      drop_params: true  # Strips unsupported model parameters dynamically
      check_provider_endpoint: false  # Only return models in config.yaml; prevents dumping 100+ upstream models
    model_list:
      # [1] Active Default Model (Configured by setup_wsl2.sh)
      - model_name: default-llm
        litellm_params:
          model: ollama/qwen2.5:latest
          # LiteLLM automatically pulls these from your ~/.bashrc exports!
          api_base: os.environ/DEFAULT_LLM_BASE_URL
          api_key: os.environ/DEFAULT_LLM_API_KEY

      # [2] Remote Model - SciCORE / Custom HPC Cluster
      - model_name: Qwen3.8-Flash-Next-FP8
        litellm_params:
          model: openai/Qwen3.8-Flash-Next-FP8
          # LiteLLM automatically pulls these from your ~/.bashrc exports!
          api_base: os.environ/LLM_PLATFORM_API_BASE
          api_key: os.environ/LLM_PLATFORM_API_KEY

      # [3] Google Gemini - Direct Universal Access (e.g. gemini/gemini-3.8-flash)
      - model_name: "gemini/*"
        litellm_params:
          model: "gemini/*"
          # LiteLLM automatically pulls these from your ~/.bashrc exports!
          api_key: os.environ/GEMINI_API_KEY

      # [4] OpenAI - Direct Universal Access (e.g. openai/gpt-6-luna)
      - model_name: "openai/*"
        litellm_params:
          model: "openai/*"
          # LiteLLM automatically pulls these from your ~/.bashrc exports!
          api_key: os.environ/OPENAI_API_KEY

      # [5] OpenRouter - Universal Access for all other models (e.g. openrouter/anthropic/claude-haiku-5.5)
      - model_name: "openrouter/*"
        litellm_params:
          model: "openrouter/*"
          # LiteLLM automatically pulls these from your ~/.bashrc exports!
          api_key: os.environ/OPENROUTER_API_KEY

> **Note on Model Naming & Provider Prefixes:**
> * In `config.yaml`, LiteLLM matches provider-namespaced wildcards (`gemini/*`, `openai/*`, `openrouter/*`). When delegating via `litellm_proxy`, prepend the provider namespace matching the proxy rule:
>   - **Google Direct:** `gemini/gemini-3.8-flash` routes directly to Google Gemini using `GEMINI_API_KEY`.
>   - **OpenAI Direct:** `openai/gpt-6-luna` routes directly to OpenAI using `OPENAI_API_KEY`.
>   - **OpenRouter Universal:** `openrouter/anthropic/claude-haiku-5.5` routes all other models via `OPENROUTER_API_KEY`.
> * In `llm_config.py` (and framework CLI overrides), use only the clean model identifier matching LiteLLM's `model_name` (e.g. `"model": "qwen3.8"`), **without** the `openai/` prefix. The setup script handles this separation automatically.

Run this in your standard WSL2 terminal:

    nano ~/.bashrc

Add your secret variables like this:

    export DEFAULT_LLM_BASE_URL="http://localhost:11434/v1"
    export DEFAULT_LLM_API_KEY="local-key"
    export LLM_PLATFORM_API_BASE="https://your-hpc-endpoint/v1"
    export LLM_PLATFORM_API_KEY="sk-platform-secret-key"
    export GEMINI_API_KEY="AIzaSy-your-gemini-key"
    export OPENAI_API_KEY="sk-proj-your-openai-key"
    export OPENROUTER_API_KEY="sk-or-v1-secret-key"

Add this auto-start script to the bottom of the `.bashrc` file:

    # ==========================================
    # ZERO-TRUST AI PROXY AUTO-START
    # ==========================================
    if [ -d "$HOME/litellm_proxy" ] && [ -f "$HOME/litellm_proxy/config.yaml" ]; then
        if ! pgrep -f "litellm.*--port 4000" > /dev/null; then
            echo "🛡️ Starting Zero-Trust LiteLLM Proxy on port 4000..."
            (cd "$HOME/litellm_proxy" && nohup pixi run litellm --config config.yaml --port 4000 > proxy.log 2>&1 &)
        fi
    fi

Then, reload your profile by running this in your standard WSL2 terminal:

    source ~/.bashrc

### Step 5: Add the Project Files
Save the core scripts into the root of your `ai_workspace` folder:
1. `llm_config.py` (LLM endpoints, model parameters, role assignments, and Decider configuration).
2. `config.py` (Core system prompts, memory limits, and runtime settings).
3. `sandbox_tools.py` (The FastMCP server handling tool creation, subagent execution, `execute_rest_call`, and native tools like `fast_decision`).
4. `smrtk.py` (The main interactive async loop and container harness).
5. `mcp_config.py` (External and host MCP server registrations, host IP discovery, and security gates).
6. `chimerax_mcp_server.py` (FastMCP bridge server for UCSF ChimeraX molecular visualization).
7. `mock_mcp_server.py` (Lightweight test FastMCP SSE server for verifying external MCP bridging).

### Step 6: Build the Hardened Podman Container
Create a file named `Containerfile` in your workspace root. Notice that it contains no secrets, environment variables, or python files. 

*(Note: We deliberately do not `COPY` the Python scripts into this image. They are mapped dynamically via volumes at runtime. This allows you to tweak your prompts and Python code on the host machine endlessly without ever needing to rebuild the container cache!)*

	FROM ghcr.io/prefix-dev/pixi:latest

	# ROOT LEVEL: Install system-level scientific & media dependencies
	RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
		xvfb git git-lfs curl wget unzip aria2 file jq pigz zstd \
		poppler-utils tesseract-ocr ffmpeg imagemagick graphviz pandoc sqlite3 \
		build-essential cmake gfortran libgl1 libglib2.0-0 libxml2-dev libxslt-dev \
		procps ripgrep tree bzip2 psmisc \
		&& rm -rf /var/lib/apt/lists/*
		
	# USER SETUP: Remove default base image user (UID 1000) and establish agent with UID 1000
	RUN touch /var/mail/ubuntu /var/spool/mail/ubuntu 2>/dev/null || true \
	    && userdel -r ubuntu 2>/dev/null || true \
	    && useradd -u 1000 -m -s /bin/bash agent
	WORKDIR /app

	# Disable the FastMCP ASCII Banner ---
	ENV FASTMCP_SHOW_SERVER_BANNER=0

	RUN mkdir /app/workspace && chown -R agent:agent /app

	# Switch to non-root user before installing Python tools
	USER agent

	# Explicitly provision Rust toolchains into the user path boundaries safely
	ENV RUSTUP_HOME=/home/agent/.rustup \
	    CARGO_HOME=/home/agent/.cargo \
	    PATH=/home/agent/.cargo/bin:$PATH

	# Install Rust toolchain natively for the non-root execution framework
	RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable --no-modify-path

	# We add conda-forge and bioconda to give the AI maximum scientific reach
	# Chained the Playwright install and cache cleanup into a single layer to minimize final image size.
	RUN pixi init && \
		pixi project channel add conda-forge && \
		pixi project channel add bioconda && \
		pixi add python pip openai mcp fastmcp httpx \
		pandas numpy scipy matplotlib pyarrow \
		requests beautifulsoup4 lxml \
		pypdf2 python-docx pillow tiktoken \
		biopython rdkit sqlalchemy networkx \
		scikit-learn seaborn statsmodels openpyxl \
		duckdb sympy pyyaml h5py \
		mafft hmmer pyhmmer \
		nodejs && \
		pixi run npm install -g tsx && \
		pixi add --pypi sqlite-vec playwright playwright-stealth && \
		pixi run playwright install chromium && \
		rm -rf ~/.cache/rattler ~/.cache/pip
	

Build the rootless image by running:

    podman build -t sandbox-image .
		
---

## 🚀 How to Run & Manage Sessions

### Starting a Chat & CLI Usage
SMRTk can be launched as a standard interactive chat or driven entirely via command-line arguments. To start a basic interactive session:

    pixi run python smrtk.py

**CLI Arguments & Headless Execution:**
You can override configurations, pipe in files, and trigger headless background tasks using the built-in CLI flags:
* `-p`, `--prompt`: Pass an initial prompt directly (e.g., `-p "Analyze the data"`).
* `-f`, `--format`: Override the visual format. Options: `markdown` (Rich Markdown panels), `text` (classic streaming text).
* `-v`, `--verbosity`: Override the console output level. Options: `silent` (pure background execution), `minimal` (hides massive JSON args and tool outputs), `standard` (shows AI thinking but hides JSON), `detailed` (shows all raw data).
* `-s`, `--session`: Override the `SESSION_ID` to load or create a specific workspace (e.g., `-s "Project_X"`). Must be 1–64 characters matching `^[A-Za-z0-9._-]+$` (hardened against shell injection).
* `-x`, `--exit`: Auto-exit back to your bash terminal when the AI finishes its tasks (perfect for single-shot scripts).
* `--brain`, `--coder`, `--summarizer`, `--adviser`, `--analyst`, `--architect`: Pass the index number of your LLM profiles to override the active models dynamically.
* `--mcp`: Pass a comma-separated list of external MCP servers to activate on startup (e.g., `--mcp chimerax`).
* `--all-mcp`: Activate all external MCP servers configured in `mcp_config.py`.
* `--skills`: Pass a comma-separated list of host skill packs, category folders, or individual skills from `./skills/` to copy into the session on startup (e.g., `--skills development,webapp-testing`).
* `--all-skills`: Copy all available skills from `./skills/` into the session on startup.

**Interactive Slash Commands:**
During a live chat, you can dynamically configure the session without restarting:
* **Session Lifecycle:** `/exit` or `/quit` to terminate the session and tear down the container.
* **UI Format:** `/markdown` (rich panels) or `/text` (raw streaming).
* **Verbosity:** `/silent`, `/minimal`, `/standard`, or `/detailed`.
* **MCP Management:** `/mcp` (list status of all configured servers), `/mcp on <name>` (connect server and inject tools), `/mcp off <name>` (disconnect server).
* **Skills Management:** `/skills` (list all host skill packs and active session skills), `/skills on <pack|skill|all>` (copy skill pack or skill into the session and update the Brain's system prompt), `/skills off <pack|skill|all>` (unload skill pack or skill from the session), `/skills save <skill> <pack>` (save a session skill permanently to `./skills/<pack>/<skill>` on the host).
* **Background Jobs Management:** `/jobs` or `/jobs list` (list all active and completed background bash processes and delegated sub-agent tasks), `/jobs status <job_id>` (inspect job metadata and tail its log/trace file), `/jobs kill <job_id|all>` (terminate a running background job or all active jobs).

**Advanced Usage Examples:**
* **Rich Interactive Mode with Specific Models:**
  `pixi run python smrtk.py -f markdown -v standard -s "Test_2" --brain 2 --coder 3`
* **Connecting Host Tools on Boot (e.g. ChimeraX):**
  `pixi run python smrtk.py -f markdown -v detailed --mcp chimerax`
* **Headless Background Task (Auto-Exits when done):**
  `pixi run python smrtk.py -v silent -s "Research" -x -p "Scrape the latest papers on CRISPR and save to outputs/summary.md"`
* **Unix Pipeline (Pipe files directly into the AI):**
  `cat error_log.txt | pixi run python smrtk.py -f text -v minimal -x -p "Find the bug and write a python fix to outputs/fix.py"`

### Session Management (Config Fallback & Idempotent Resumption)
If you don't use the `-s` flag, the framework falls back to your `config.py`. **Always use strings for Session IDs**.
* `SESSION_ID = None` -> Starts a brand new, uniquely timestamped workspace (`Session_ID_<timestamp>`).
* `SESSION_ID = "my_project"` or `SESSION_ID = "Session_ID_my_project"` -> Idempotently loads or restores that specific session without duplicate prefixing, seamlessly recovering all custom tools, plan checklists, and long-term state memory across separate process runs.

### Example Interaction Flow

Once the chat starts, try issuing a complex command:

> **YOU:** "Create a tool in the 'math' category that calculates the Fibonacci sequence up to a given number. Then use it to find the sequence up to 15."

**What happens behind the scenes:**
1. The **Brain** checks `view_tool_registry` and realizes the tool doesn't exist.
2. The Brain calls `create_tool` via the secure Podman MCP connection.
3. The **MCP Server** pings the **Coder LLM** through the `host.containers.internal` gateway to write `fibonacci.py`.
4. The system sanitizes the filename and runs a syntax check. If it passes, it updates `tool_registry.json`.
5. The Brain reads the usage instructions, then uses `execute_bash` to run it: `python /app/workspace/tools/fibonacci.py`.
6. The result is streamed back to your color-coded console.

### Context Compression & Observability
If the session grows too long, the system will inject a dynamic warning prompting the Brain to use the `compact_context` tool.

1. The Background **Summarizer LLM** activates.
2. It executes a strict 2-step JSON schema pipeline.
3. **Step 1:** Extracts facts into the Memory Registry and saves detailed `.md` files in `memories/`.
4. **Step 2:** Compresses the chat history into a pristine state and synthesizes an executive handoff anchored to the active plan and on-disk deliverables.
5. The system backs up the bloated history to `histories/backup_history_<timestamp>.json` and updates `current_history.json`.
6. The Brain wakes up instantly with a pristine context window, guided by a Transition Handoff Directive: it inspects disk to verify completed deliverables, updates `active_plan.md`, and concludes cleanly without re-running compaction or entering post-handoff loops.

* **Infinite Context Scaling (Calibrated Pruning & Targeted Handoff):** Standard agents suffer from permanent amnesia when their context window fills up. This framework continuously ages older tool outputs (`compact_historical_tool_outputs`) to disk pointers and monitors total context utilization via API-calibrated token estimation (warning at 80%, mandatory compaction alert at 90%). When `compact_context` is triggered, the Summarizer dynamically ingests as many recent execution turns as fit within its token budget alongside the original user goal, `active_plan.md`, and verified deliverables in `outputs/`, extracting persistent memories to `memories/` and resetting `current_history.json` into a clean handoff state.

---

### 🐝 Swarm Mode (Parallel Execution)
Because the framework strictly isolates state, memory, and Podman execution environments (dynamically generating container names via Session IDs and OS Process IDs), you can run multiple autonomous agents simultaneously on the same machine without them colliding.

You can use `tmux` to launch a multi-agent swarm in a 2x2 split-pane dashboard. Paste this chained command into your terminal to boot 4 independent researchers at once:

    tmux new-session -d -s smrtk_swarm \; \
      send-keys "pixi run python smrtk.py -f text -s 'Swarm_Text_1'" C-m \; \
      split-window -h \; \
      send-keys "pixi run python smrtk.py -f text -s 'Swarm_Text_2'" C-m \; \
      split-window -v \; \
      send-keys "pixi run python smrtk.py -f markdown -s 'Swarm_MD_2'" C-m \; \
      select-pane -t 0 \; \
      split-window -v \; \
      send-keys "pixi run python smrtk.py -f markdown -s 'Swarm_MD_1'" C-m \; \
      select-layout tiled \; \
      attach-session -t smrtk_swarm

**Pro-Tip for Swarm Management:** Enable mouse support in your tmux configuration (`echo "set -g mouse on" >> ~/.tmux.conf`) so you can simply click between your agents, drag the window borders to resize their panels, and use your mouse wheel to independently scroll through their individual reasoning logs!

---

## 🧪 Automated Testing & Benchmark Suite (`run_tests.sh`)

SMRTk includes an automated benchmark test runner ([`run_tests.sh`](run_tests.sh)) driven by a clean, human-maintainable tab-separated table ([`tests.tsv`](tests.tsv)). This allows you to run reproducible, multi-disciplinary benchmarks sequentially, inspect deterministic session artifacts, and optionally trigger post-task self-diagnostic reviews.

### 📋 TSV Table Format (`tests.tsv`)

Every row in `tests.tsv` defines an independent test case with 4 tab-separated columns:

| Column | Field Name | Description | Example |
| :---: | :--- | :--- | :--- |
| **1** | `Session Name` | Deterministic session name used for folder naming (`sessions/Session_ID_<name>`) and output filenames. | `Test01_Tool_Registry_Embeddings` |
| **2** | `Description` | Brief summary shown in `--list` output and CLI step headers. | `Tool registry inventory, adviser brainstorming...` |
| **3** | `Main Prompt` | The complete instruction sent to the AI Overseer (`\n` encodes embedded newlines). | `What tools do you have? ...` |
| **4** | `Follow-up Prompt` | Tailored evaluation or self-analysis prompt executed when `--analyze` is enabled. | `In sessions/{SESSION_ID}/logs/, find the log...` |

#### Dynamic Prompt Variables & Path Architecture
SMRTk mounts each session folder (`./sessions/Session_ID_<name>`) on the host directly to `/app/workspace` inside the rootless Podman sandbox container. This means:
* **Host Perspective:** Files live in `./sessions/Session_ID_<name>/` (e.g. `logs/`, `outputs/`, `sandbox/`, `tools/`).
* **Agent (Container) Perspective:** The agent operates inside `/app/workspace/` (e.g. `/app/workspace/logs/`, `/app/workspace/outputs/`, `/app/workspace/tools/`).

To make writing tests effortless, both the main prompt and the follow-up prompt can include runtime placeholders that `run_tests.sh` automatically expands:
* `{SESSION_NAME}` $\rightarrow$ The short name from column 1 (e.g. `Test01_Tool_Registry_Embeddings`), ideal for naming deliverables like `/app/workspace/outputs/{SESSION_NAME}.md`.
* `{SESSION_ID}` $\rightarrow$ The deterministic session ID (e.g. `Session_ID_Test01_Tool_Registry_Embeddings`).
* `{LOGS_DIR}` $\rightarrow$ `/app/workspace/logs` (native container log path).
* `{OUTPUTS_DIR}` $\rightarrow$ `/app/workspace/outputs` (native container outputs path).
* `{WORKSPACE_DIR}` $\rightarrow$ `/app/workspace` (container root workspace).
* `{HOST_SESSION_DIR}` $\rightarrow$ `sessions/Session_ID_<name>` (host-relative session folder).

> [!NOTE]
> `run_tests.sh` also includes automatic path safety translation: if a prompt accidentally mentions `sessions/<Session_ID>/`, the runner automatically rewrites it to `/app/workspace/` before handing it to the container, preventing agent path confusion!

Example follow-up prompt:
```text
In /app/workspace/logs/, find the log file describing past AI agent activity... Write your assessment to /app/workspace/outputs/analysis_{SESSION_NAME}.md.
```

### 🚀 How to Run Tests

* **List all available tests:**
  ```bash
  ./run_tests.sh --list
  ```

* **Run all tests sequentially:**
  ```bash
  ./run_tests.sh
  ```

* **Run specific tests by index number:**
  ```bash
  ./run_tests.sh 1 5 10
  ```

* **Run specific tests by name or wildcard pattern:**
  ```bash
  ./run_tests.sh Test01* Test10*
  ```

* **Run tests with automated post-task self-analysis:**
  ```bash
  ./run_tests.sh --analyze 1 9
  ```
  When `--analyze` (or `-a`) is passed, the runner executes the main task, and if successful, immediately follows up by executing the tailored evaluation prompt from Column 4 in the exact same session so the agent can inspect its own logs and outputs.

* **Dry-run preview (zero LLM token cost):**
  ```bash
  ./run_tests.sh --dry-run --analyze 1 2
  ```
  Previews the exact CLI commands, resolved session directories, and expanded prompts without starting containers or making API calls.

* **Use a custom TSV file:**
  ```bash
  ./run_tests.sh --tsv custom_experiments.tsv
  ```

* **Console format and verbosity overrides:**
  ```bash
  ./run_tests.sh -f text -v detailed 1
  ```

---

## 📖 Monitoring & Logs

* **Interaction Logs:** Every interaction is mirrored to a timestamped log file inside your active session folder (e.g., `sessions/Session_ID_.../logs/chat_log_<timestamp>.txt`). This includes streaming tokens, tool calls, full LLM reasoning/thinking tokens (captured across all roles including Brain streaming `<think>` tags and subagent reasoning), intercepted subagent thoughts, Decider rulings (`<___DECIDER_RULING___>`), and full Coder generated code/edits archived verbatim under `=== CODER (HIDDEN) === --- GENERATED CODE / EDIT ---` via `<___CODER_CODE___>` tags.
* **Token Usage Tracking:** Session-wide token consumption is logged atomically in `sessions/Session_ID_.../state/token_usage.json`. This tracks prompt, completion, and thinking tokens across all roles, including the Brain, Coder, Summarizer, Adviser, Analyst, Architect, Decider, and **Embedding generation** operations.
* **Tool Usage & Telemetry Tracking:** Tool calls, execution durations, caller origins, and failure metrics are recorded atomically in `sessions/Session_ID_.../state/tool_usage.json`. The harness surfaces persistent sequential numbering across all UI headers and interaction logs (`BRAIN #N`, `TOOL CALL #N`, `TOOL RESULT #N`, `▶ Tool #N`, and sub-agent role headers such as `CODER #N`, `ANALYST #N`, `ADVISER #N`, `ARCHITECT #N`, `DECIDER #N`, `AUXILIARY LLM #N`), per-tool frequency distributions in turn/session summaries, inference vs. tool execution latency partitioning (`Turn Time: Xs (LLM: Ys, Tools: Zs)`), and a live context fuel gauge (`Context: X%`) measuring context window saturation against `config.MAX_CONTEXT_TOKENS`.
* **Context Warning Logic:** The system uses `tiktoken` to estimate the **total official context window** size dynamically, calibrated against real API usage receipts. This estimate accounts for the raw message payload, the size of all registered tool schemas, and chat wrapper template overhead. When total context usage reaches 80% of `config.MAX_CONTEXT_TOKENS` (120,000 tokens), an advisory warning is issued; at 90%, it escalates to a mandatory critical alert requiring `compact_context`. Consecutive warning deduplication prevents history clutter.

You can safely type `/exit` or `/quit` at any time to shut down the system and instantly destroy the temporary container.


### ⚠️ Disclaimer: Web Scraping & API Limits
The default web search tool in this framework utilizes Playwright to scrape DuckDuckGo's legacy HTML portal. This is provided for **educational and research purposes only**. Actively bypassing bot protections or aggressively scraping search engines may violate their Terms of Service. If you intend to use this framework for heavy, production-grade autonomous research, you have to replace the `search_web` tool with an official API like Serper.dev, Tavily, or the Google Custom Search API to ensure stability and compliance. The author assumes no liability for IP bans or ToS violations incurred by using this code.