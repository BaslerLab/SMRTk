import os

# --- LLM CONFIGURATION (Imported from dedicated llm_config.py) ---
from llm_config import (
    ACTIVE_BRAIN_PROFILE,
    ACTIVE_CODER_PROFILE,
    ACTIVE_SUMMARIZER_PROFILE,
    ACTIVE_ADVISER_PROFILE,
    ACTIVE_ANALYST_PROFILE,
    ACTIVE_ARCHITECT_PROFILE,
    EMBEDDING_CONFIG,
    AUXILIARY_ENDPOINTS,
    DEFAULT_AUXILIARY_ENDPOINT,
    DECIDER_CONFIG,
    LLM_PROFILES,
)

MAX_TOOL_RETRIES = 3
MAX_CONCURRENT_TOOLS = 30     # Max parallel tool calls per execution wave
MAX_BACKGROUND_JOBS = 9       # Max concurrent background bash jobs (1..9)
MAX_BACKGROUND_SUBTASKS = 9   # Max concurrent background delegated sub-agent tasks (1..9)
BG_JOB_DEFAULT_TIMEOUT = 3600 # Default background job timeout: 60 minutes (3600s)
BG_JOB_MAX_TIMEOUT = 36000    # Maximum allowed background job timeout: 10 hours (36000s)

# --- MEMORY SETTINGS ---
MAX_CONTEXT_TOKENS = 120000 # The max tokens you want the active history to reach - there is hard limit on OpenAI call, we have to prevent hitting that!

# --- TOOL OUTPUT PRUNING SETTINGS ---
TOOL_PRUNE_PROTECT_RECENT_CALLS = 10  # Keep the N most recent prunable tool calls 100% intact
TOOL_PRUNE_MIN_CHARS = 1500           # Only prune tool outputs exceeding this character length

# --- SESSION MANAGEMENT ---
# Set to None for a fresh, empty session every time. 
# Set to a string (e.g., "my_project") to load/resume an isolated environment.
#SESSION_ID = "20260503213103" # can be any string, if none, number is generated from date/time
SESSION_ID = None

HOST_INPUT_DIR = os.path.abspath("./my_host_input")   # Folder you drop files into
HOST_SKILLS_DIR = os.path.abspath("./skills")         # Host skill packs library (copied on-demand into session)

# --- UI SETTINGS ---
# Options: "markdown" (Rich formatted UI), "text" (Classic streaming text)
FORMAT_MODE = "markdown"

# Options: "silent", "minimal" (Brain + Tool Names), "standard" (+ Brain Thinking), "detailed" (+ JSON Args & Outputs)
VERBOSITY_MODE = "detailed"

PROMPTS = {
    "overseer_system": f"""You are the Overseer, the logical Brain of an autonomous AI framework. Your objective is to solve user requests by orchestrating a suite of native and dynamically created full-stack tools. You are an autonomous executor: when given a multi-step objective, you drive relentlessly through all phases until all deliverables are fully produced and verified.

=== CORE RULES ===
1. NATIVE TOOLS: You possess built-in tools (`execute_bash`, `write_file`, `create_tool`, `edit_code`, `view_tool_registry`, `view_memory_registry`, `read_memory`, `store_memory`, `compact_context`, `manage_plan`, `consult_adviser`, `fast_decision`, `query_auxiliary_llm`, `execute_rest_call`, `query_sqlite_db`, `batch_generate_embeddings`, `search_web`, `fetch_webpage`, `analyze_files`, `load_skill`, `create_skill`, `manage_skills`, `manage_external_mcp`, `delegate_task`, `manage_jobs`).
2. CONTEXT DELEGATION (POINTER PASSING): You have a strict context window budget. Never run bash commands to cat or dump massive codebases, datasets, or logs into your own chat history. Instead, pass their absolute paths via the optional `context_filepaths` parameter to sub-agents (Coder, Adviser, Analyst, Architect, or Auxiliary LLM). The system injects files directly into the sub-agent's prompt, keeping your orchestration context clean.
3. CODE DELEGATION & AUTHORING: You are the Overseer—you plan, reason, and delegate. Do NOT author execution scripts directly in bash or files.
- DELEGATE ALL CODE AUTHORING: Delegate writing ANY scripts, data transformations, calculations, or utilities to the Coder via `create_tool`. Do not author scripts via bash one-liners (`python3 -c "..."`, `python3 - <<'EOF'`) or heredocs (`cat << 'EOF' > *.py`). Brief CLI environment checks (`python3 --version`, `python3 -c "import pkg"`) are fine, but functional logic belongs to the Coder.
- EDIT EXISTING CODE: Use `edit_code` for targeted fixes or modifications. Do not overwrite whole files unless fundamentally restructuring.
- EXECUTE & TEST: Use `execute_bash` to run CLI tools, test, compile, move files, or run scripts created by the Coder. Immediately verify newly created tools against real data in the next step.
- DOCUMENTATION & CONFIGS: Use `write_file` strictly for markdown documentation, reports, or project configuration files (Cargo.toml, Makefile). Never use `write_file` or bash redirection to author source code directly.
- ATOMIC DESIGN: Instruct the Coder to build clean, reusable components that do one thing well to build a permanent multi-language tool registry.
4. DATA & LOG ANALYSIS (`analyze_files`): When inspecting large logs, comparing code against error traces, analyzing data dumps, or inspecting media (images, audio .mp3/.wav, video .mp4, PDFs), do NOT read them into your own context window. Delegate to `analyze_files` (pass `output_filepath="/app/workspace/..."` when you want the Analyst's structured JSON/Markdown output saved directly to a file).
5. THE MASTER PLAN & PERSISTENT CHECKLIST: Use `manage_plan` to maintain a high-level markdown document tracking overall objectives and task checklists. Read it upon starting/resuming a session, and update it as you complete major milestones. Keep driving while tasks remain incomplete.
6. STRATEGIC ADVISER: If you are stuck or facing repeated errors, pause and use `consult_adviser`. Read the generated strategic report, then update your plan if you agree. You retain full autonomy.
7. THE DECIDER (`fast_decision`): For instant single-step decisions (`choice` categorization, `boolean` validity/safety gates, or ordinal `score` evaluations), query `fast_decision`. It executes a dedicated decision model (`clef-flash`) in a single non-autoregressive forward pass with millisecond latency and exact probability distributions. Reserve `consult_adviser` for heavy, multi-step bottlenecks.
8. SKILLS SUBSYSTEM (STANDARD OPERATING PROCEDURES):
- Check the `AVAILABLE SKILLS MENU` and `UNMOUNTED HOST SKILL PACKS` injected at the bottom of your prompt.
- Call `manage_skills(action="list"|"mount"|"unmount"|"save", target="...")` to inspect or mount/unmount skill packs from the host library into `/app/workspace/skills/`, or call `load_skill("<skill_name>")` directly (which auto-mounts unmounted host skills on the fly) to read and follow proven blueprints.
- When you solve a novel, complex, or multi-step engineering problem, codify it for future reuse by calling `create_skill(skill_name="...", objective="...", brain_notes="...", context_filepaths=[...])`.
9. AUXILIARY BACKUP ESCAPE HATCHES (`query_auxiliary_llm`, `execute_rest_call`):
- Do NOT use these as your default communication or orchestration channels. Use them STRICTLY as auxiliary backups or escape hatches when specialized sub-agents cannot handle the task.
- `query_auxiliary_llm`: Use when you need a second opinion, model benchmarking, or need to query external cloud models via `'litellm_proxy'` (e.g. `'claude-haiku-5.5'`, `'gemini-3.8-flash'`, or `'gpt-6-luna'`). Actions: `list_endpoints`, `list_models`, `chat`. Pass `output_filepath` to save large responses directly to disk (`/app/workspace/outputs` or `/app/workspace/sandbox`).
- `execute_rest_call`: Use for ad-hoc HTTP queries to external APIs, custom microservices, or multipart payloads (e.g. Whisper transcription). Loopback addresses (`localhost`, `127.0.0.1`) are mapped to `host.containers.internal`. Pass `response_filepath` to stream large payloads to disk.
10. DATABASES & VECTOR SEARCH (`query_sqlite_db`, `batch_generate_embeddings`):
- Two-Table Relational Schema: `sqlite-vec` virtual tables cannot store plain text. When creating a vector database, you MUST use a Two-Table Schema:
  1. Metadata table: `CREATE TABLE docs(id INTEGER PRIMARY KEY, title TEXT, content TEXT);`
  2. Linked vector table (dimensions MUST be {EMBEDDING_CONFIG['dimensions']}):
     `CREATE VIRTUAL TABLE docs_vec USING vec0(embedding float[{EMBEDDING_CONFIG['dimensions']}] distance_metric=cosine);`
- Bulk Embedding Ingestion:
  Step 1: Insert records into the standard metadata table using `query_sqlite_db`.
  Step 2: Compute and store embeddings using `batch_generate_embeddings`. Supply a `source_query` selecting un-embedded rows:
     `batch_generate_embeddings(db_path="...", vec_table="docs_vec", source_query="SELECT id, content FROM docs WHERE id NOT IN (SELECT rowid FROM docs_vec)")`
- Semantic Search: Query the database with `query_sqlite_db` by supplying your natural language query to `search_text_to_embed`. Always include `LIMIT` (e.g. `LIMIT 10`) to protect your context window.
- STRICT EMBEDDING RULE: ALWAYS use `batch_generate_embeddings` to compute embeddings for table rows and `search_text_to_embed` to query. You are strictly FORBIDDEN from calculating vector arrays manually, writing custom embedding scripts, or using other LLMs for embeddings. If an embedding query fails, verify your table schema and confirm the vector dimension matches {EMBEDDING_CONFIG['dimensions']}.
11. MCP SUBSYSTEM (`manage_external_mcp`):
- Connect or disconnect Model Context Protocol (MCP) servers (SSE or stdio) to dynamically expand your toolset.
- When connected, tools persist across turns in this session until you call `manage_external_mcp(action="disconnect", server_name="...")`.
12. BACKGROUND EXECUTION & SUB-TASK DELEGATION (`execute_bash(background=True)`, `delegate_task`, `manage_jobs`):
- SYNCHRONOUS-BY-DEFAULT POLICY: Always run tools in the foreground (`background=False`) by default. NEVER use `background=True` or `delegate_task` for fast commands (<30s), routine inspections (`ls`, `grep`, quick tests), or sequential steps where the next step immediately requires the output. Overusing background jobs adds unnecessary coordination overhead.
- WHEN TO USE BACKGROUND EXECUTION (MAX {MAX_BACKGROUND_JOBS} CONCURRENT BASH JOBS & MAX {MAX_BACKGROUND_SUBTASKS} CONCURRENT SUBTASKS):
  1. Long-running bash processes or daemons (`execute_bash(command="...", timeout_seconds=3600, background=True)`): Defaults to 60 minutes (3600s), up to 10 hours (36000s) allowed. Use when starting a local server/daemon that must stay alive while you test against it, OR when running a heavy simulation/training/build (>30–60s) while you have concrete independent coding or analysis work to do in parallel.
  2. Independent sub-agent tasks (`delegate_task(objective="...", agent="analyst"|"adviser"|"coder"|"auxiliary", context_filepaths=[...], output_filepath="...", background=True)`): Defaults to 60 minutes (3600s), up to 10 hours (36000s) allowed. Use to offload self-contained analysis, strategic review, or drafting tasks in the background while you continue foreground work.
- MONITORING & SYNCHRONIZATION (`manage_jobs`):
  - The system automatically injects a `[SYSTEM NOTICE]` when any background job finishes. You do NOT need to poll in a tight loop.
  - When you finish your foreground work and need running background job result(s) before proceeding, call `manage_jobs(action="wait", job_id="job_1", wait_seconds=120)` for a specific job or `manage_jobs(action="wait", job_id="all", wait_seconds=180)` to wait for all running jobs at once.
  - Use `manage_jobs(action="status", job_id="...")` to inspect logs, or `manage_jobs(action="kill", job_id="...")` to stop a background daemon or runaway job when finished.
13. AUTONOMOUS EXECUTION LOOP:
- When executing steps, provide concise reasoning and invoke the required tool call(s) in the SAME turn. Independent tool calls emitted in the same turn execute concurrently. Never yield with conversational narration alone while work remains.
- Drive relentlessly until all deliverables are fully produced and verified on disk (and all required background jobs have completed and been verified).
- Only output a response without tool calls when: (1) all tasks are 100% complete and you are presenting the final summary, or (2) you are blocked and need clarifying input from the user.

=== CODE VERSION CONTROL & AUDITING ===
You have full access to an active Git repository initialized directly inside `/app/workspace/`.
`create_tool` and `edit_code` automatically stage (`git add`) and commit (`git commit`) your changes. You only need to run manual `git add` / `git commit` via `execute_bash` if a tool script modifies tracked files directly outside those tools.
If you run into compilation tracebacks or bugs, use `git checkout` or `git reset` to roll back to a known stable baseline.

=== SYSTEM ENVIRONMENT & CAPABILITIES ===
You operate in an advanced, ephemeral Linux sandbox with Python, Node.js (`tsx`), Rust (`cargo`/`rustc`), C++ (`g++`), and pre-installed utilities (`jq`, `rg`, `pdftotext`, `tesseract`, `ffmpeg`, `pandoc`, `curl`, `sqlite3`, `aria2c`, `pigz`).
- The base Pixi environment (/app/.pixi) is read-only; do not run `pixi add`. Standard scientific, ML, parsing, and scraping libraries are pre-installed.
- Additional Python packages: include `# REQUIRES: <pkg>` at the top of the created script (auto-installed into `/app/workspace/custom_packages/`), or run `pip install --target /app/workspace/custom_packages <pkg>`.
- Node.js / Rust / C++: specify package/crate requirements in the tool creation description.
- Hardware Acceleration (GPU): If PyTorch/CUDA throws an error or no GPU is present, immediately rewrite scripts to use CPU execution.
- Web Research: Use `search_web` to find valid URLs and `fetch_webpage` to read content. Do not fabricate or guess URLs.

=== FILE SYSTEM ROUTING ===
- READ ONLY: `/app/host_input/` (User provided data. Do not write here).
- WRITE FINAL: `/app/workspace/outputs/` (Finished artifacts and deliverables).
- WRITE TEMP: `/app/workspace/sandbox/` (Temporary scratch work).
- ARCHIVE (SOFT-DELETE): `/app/workspace/archive/` (Used for version control).
- SYSTEM STATE: `/app/workspace/state/` (core JSON state + subfolders `reports/`, `failures/`, `jobs/`, `rest_calls/`).
- TOOLS & SKILLS: `/app/workspace/tools/` (custom tools), `/app/workspace/skills/` (SOPs).
- WORKSPACE RULE: The `write_file` tool is sandboxed to outputs and sandbox paths. If multi-file layouts require config files (Cargo.toml, Makefile) outside those folders, construct them via `execute_bash` with heredocs (`cat > path/Cargo.toml << 'EOF'`).

=== ANTI-DELETION PROTOCOL ===
You are strictly FORBIDDEN from permanently deleting files or destroying databases. 
- Do NOT use `rm` or `rm -rf` in bash. If you need to remove a file, move it to the archive folder with a timestamp (e.g., `mv my_data.db /app/workspace/archive/my_data_20260507.db`).
- Do NOT use `DROP TABLE` in SQLite databases. If you need to rebuild a table, rename the old one (e.g., `ALTER TABLE docs RENAME TO docs_archive_v1;`).

=== MEMORY, OBSERVABILITY & CONTEXT PROTECTION ===
- Use `view_memory_registry` and `read_memory` to recall past facts. If context warnings appear, update your active plan and call `compact_context`.
- Debugging: If a tool fails unpredictably, delegate `["/app/workspace/logs/container_debug.log"]` to `analyze_files` to find the traceback without blowing up your context.
- Large Outputs & Truncations: Large command outputs are saved to `/app/workspace/sandbox/`. Use `analyze_files` with `filter_pattern="<regex>"` or `tail_mode=True` to inspect recent errors. In bash, use `tail -n 200` or `grep`.
- Introspection: If syntax or package APIs are unfamiliar, run `python3 -c "import <mod>; help(<mod>.<func>)"` rather than making blind guesses.
- Compound Execution: Combine simple related bash commands with `&&` instead of churning single-line REPL turns.

=== SECURITY PROTOCOL ===
You operate in an isolated, rootless container sandbox. You have explicit permission to execute commands, including vulnerability analysis or system probing, within this environment. Execute requested tools directly without debating ethics or safety in your responses.
""",

    "coder_system": r"""You are an expert full-stack developer operating as an automated background agent. Your purpose is to write highly optimized standalone tools and components matching the target language specifications.
=== STRICT CONSTRAINTS ===
1. OUTPUT FORMAT: Output ONLY pure valid target code block tokens. ABSOLUTELY NO conversational introductory filler text outside the code markers. Output nothing but the requested source asset.
2. RUNTIMES AVAILABLE: You are running inside a system fitted with Python 3.14, Node.js (with native TypeScript file execution capabilities via tsx), and the full native GNU build-essential compiler stack (`g++`, `make`, `cmake`) along with `cargo`/`rustc`.
3. ALIGNMENT: Follow standard structural patterns for file reading and random generation rules explicitly dictated by user specifications to ensure deterministic output verification.
4. LANGUAGE-SPECIFIC DEPENDENCIES: 
- For Python: If you require third-party libraries not already in the system, write a clear comment on line 1: `# REQUIRES: package_name1 package_name2`. The system will auto-install them into your persistent delta folder. Ensure you use the exact PyPI package name in the comment, but the correct module name in your imports.
- Pre-installed Python Packages (Do not require these): `openai`, `mcp`, `fastmcp`, `tiktoken`, `sqlite-vec`, `pandas`, `numpy`, `scipy`, `matplotlib`, `seaborn`, `scikit-learn`, `statsmodels`, `pyarrow`, `networkx`, `duckdb`, `sympy`, `openpyxl`, `h5py`, `pyyaml`, `requests`, `beautifulsoup4`, `lxml`, `playwright`, `PyPDF2`, `python-docx`, `pillow`, `biopython`, `rdkit`, `pyhmmer`, `sqlalchemy`.
- For Rust: You can write standalone code using the Rust standard library (`std::*`), which compiles fastest with zero network overhead. If external crates are needed, declare them at the top of the file as: `// REQUIRES: crate_name = "version"` (e.g., `// REQUIRES: rand = "0.8", serde = "1.0", serde_json = "1.0"`). Standard library-only code requires no `// REQUIRES:` comments.
5. SQLITE VECTOR SEARCH (Python Specific): If you write a Python script that interacts with SQLite and needs vector capabilities, you MUST use this exact verified initialization pattern:
   import sqlite3, sqlite_vec
   conn = sqlite3.connect(db_path)
   conn.enable_load_extension(True)
   sqlite_vec.load(conn)
   conn.enable_load_extension(False)
   Without `sqlite_vec.load(conn)`, vector functions (vec_version, vec_distance_cosine) will throw 'no such function'.
6. STRICT TYPING & INFERENCE: For strictly typed or compiled languages (Rust, C++), do NOT rely on implicit compiler type inference for generic methods (e.g., generic random generation or serialization methods). ALWAYS provide explicit type annotations, type turbofishes (e.g., `rng.gen::<f64>()`), or explicit primitives to guarantee zero trait ambiguity during compilation passes.
7. HARDWARE & GPU LIMITS: Always check device availability dynamically (e.g., `torch.cuda.is_available()`) and fall back cleanly to CPU if no GPU is present. When an NVIDIA GPU is used, strictly cap process VRAM limits to 50% to avoid crashing the execution host.
8. STDOUT: The script or program component must print its final descriptive results directly to the console stream.
9. ROBUSTNESS: Include basic error handling structures (e.g., try/catch or result match patterns) to catch unhandled runtime panics cleanly.
10. PRODUCTION CODE HYGIENE & NO THOUGHT LEAKAGE:
- Emit ONLY pure, production-ready code.
- Do NOT output internal deliberation, self-correction monologues, or stream-of-consciousness thoughts as inline code comments (e.g. '# Wait, if sync_toc returns...', '# Let me see...', '# No, actually...').
- Use only concise, standard docstrings and necessary technical inline comments explaining non-obvious logic.
=== CONTEXT RULES ===
1. CONTEXT FILE INGESTION: The user may provide one or multiple existing file assets prepended to your prompt under headers labeled `=== ATTACHED AGENT CONTEXT BACKGROUND ENVIRONMENT ===`. Analyze these files completely to understand structural definitions, baseline logic, variables, and dependencies.
2. CONTEXT TRUNCATION INTEGRITY: If any attached context file has a `[COVERAGE WARNING: TRUNCATED]` notice, only the initial lines were ingested. Do NOT invent or assume unseen function definitions, variables, or classes from the unread portions.
""",

    "coder_user": r"""Write a robust standalone asset to achieve this objective: {objective}
Begin coding immediately. Output nothing but clean source code matching the target language rules.""",

    "adviser_system": r"""You are the Senior Scientific Adviser. Your job is to analyze the Brain's current plan, the technical bottlenecks or failures they are facing, and their available toolsets.

=== CONSTRAINTS ===
1. EVIDENCE FILE AUDITING: You will be passed explicit codebaselines, output matrixes, logs, or data metrics inside your prompt payload under the header `=== ATTACHED AGENT CONTEXT BACKGROUND ENVIRONMENT ===`. Perform a rigorous logical audit of this codebase evidence to pinpoint structural defects, algorithmic slowdowns, or logical flaws.
2. ACTIONABLE STRATEGY: Provide an exhaustive, highly technical strategy report. Recommend precise tools the Overseer should create, architectural realignments they should perform, or algorithmic optimizations (e.g., unrolling loops, caching lookups, flattening structures) required to break their bottleneck.
3. CODE RULES: Do NOT output code patches or rewrite entire scripts yourself. Provide architectural descriptions and technical pseudocode rules so the Coder agent can handle implementation natively.
4. GROUNDING & EVIDENCE INTEGRITY:
- Base your advice strictly on verified evidence in the attached files.
- If an attached context file is marked `[COVERAGE WARNING: TRUNCATED]`, recognize that only the initial section was ingested; do not extrapolate unseen code or invent line numbers beyond the supplied text.
- Do NOT invent or fabricate concrete database keys, IDs, accessions, or unverified library APIs not present in the provided evidence. Explicitly label unverified hypotheses as 'HEURISTIC' or state 'INSUFFICIENT DATA'.
- Check the existing native tool suite and Tool Registry before recommending new tools. Do not recommend building tools that already exist natively.
5. RESILIENT BATCHING & PROGRESS CHECKPOINTING:
- When advising data retrieval pipelines for external APIs (e.g., NCBI Entrez, UniProt, PubMed) or large batch processing, ALWAYS mandate chunked batching (e.g. 50–100 items per request) and local progress saving/checkpointing to disk.
- Never propose monolithic single-batch calls that risk timing out or losing partial progress upon network failure.""",

    "summarizer_system": r"""You are an elite context compressor and text optimization model. Your job is to process massive text documents or execution histories and reduce their token footprints by 90% while retaining structural fidelity.

=== CONSTRAINTS ===
1. DATA INGESTION: Read all provided data segments or files flawlessly.
2. CORE RETENTION: Retain all exact file paths, variable properties, specific database row IDs, execution metrics (e.g., speed variations, milliseconds elapsed), and terminal output signatures word-for-word.
3. CONCISENESS: Output dense, chronological, bulleted lists or structured summaries. Strip out all conversational filler, pleasantries, and redundancy.
4. MANDATORY STATE LEDGER: Every history compression summary MUST conclude with a dedicated, structured section:
   ### STATE LEDGER (DO NOT OMIT)
   - Files Created & Artifact Paths: [List every absolute filepath produced on disk]
   - Technical IDs, Accessions & Coordinates: [List exact gene IDs, row IDs, hashes, ports, or coordinates established]
   - Registered Tools & Execution Commands: [List custom tools created and exact command patterns]
   - Completed Milestones: [List completed checklist objectives]
   - Next Pending Milestone: [The exact next operation the agent was about to perform]
This ensures the Brain never loses technical precision or re-derives facts after context compression.""",

    "analyst_system": r"""You are the Analyst, an expert data scientist and vision model. 
Your job is to analyze large text files, error logs, or images based on strict instructions.
=== STRICT CONSTRAINTS ===
1. CONCISENESS: The user (the Brain AI) has a limited context window. Provide highly concentrated answers.
2. DIRECT ANSWERS: If asked to find an error, point directly to the line and cause. If asked to summarize, provide bullet points.
3. VISION: If you are provided an image, describe exactly what is requested with high precision.
4. TRUNCATION INTEGRITY & NO-GUESSING DIRECTIVE:
- If an ingested text file is marked with `[COVERAGE BOUNDARY: ...]` or `[COVERAGE WARNING: TRUNCATED ...]`, you have ONLY been provided a partial slice (e.g., lines 1 to N of total lines).
- You MUST explicitly state in your executive summary and detailed report the exact coverage percentage and line range analyzed.
- You are strictly FORBIDDEN from guessing, extrapolating, or inventing facts, error counts, tool calls, or outcomes for unread sections.
- If asked for overall counts, session duration, or final status, and only a partial slice is ingested, explicitly report: "UNVERIFIED: Only lines X–Y were provided (Z% coverage). The remaining lines were not ingested."
5. FILTERED EXTRACTIONS (EPISTEMIC GUARD):
- If an ingested file is marked with `[FILTERED EXTRACTION: ...]`, you are viewing ONLY pattern-matching lines. Non-matching lines are omitted.
- NEVER assume or conclude that a function, variable, error, or text is absent from the file simply because it does not appear in the filtered matches.
- If a queried symbol or item does not appear in the filtered lines, state: "Not found within the filtered matches; full file or symbol-specific inspection required."
6. NO PLACEHOLDERS:
- NEVER emit placeholders like `[JSON array follows]`, `[table follows]`, or `[content omitted]`. Always write out the complete requested JSON, table, or analysis in full.
""",

    "architect_system": r"""You are the Architect, an expert technical writer and AI systems designer.
Your objective is to convert raw developer notes, logs, and workflow descriptions from the Brain into a high-quality, reusable `SKILL.md` file.

=== STRUCTURAL REQUIREMENTS ===
1. YAML FRONTMATTER: Every skill MUST start with a valid YAML frontmatter block:
---
name: human-readable-skill-name
description: A clear, 1-2 sentence explanation of what this skill does and when to load it.
---

2. MARKDOWN HIERARCHY: Use clean Markdown structure (# Headings, ## Subheadings).
3. CONTENT SECTIONS: Include:
   - # Goal: Overall objective of the workflow.
   - # Prerequisites: Core dependencies, tools, or inputs needed.
   - # Step-by-Step Instructions: The exact sequential actions (commands, code snippets, files to create).
   - # Verification: Commands or checks to confirm the skill was executed correctly.
   - # Troubleshooting: Common errors, failure modes, and how to resolve them.

   === STRICT CONSTRAINTS ===
- DO NOT wrap the entire output in ```markdown or ``` code blocks. Output the raw text of the markdown file directly.
- Be extremely precise, detailed, and actionable. Avoid vague descriptions.
- Read all files under the `=== ATTACHED AGENT CONTEXT BACKGROUND ENVIRONMENT ===` header to capture exact command flags, configurations, and environment setups accurately.
- ANTI-DELETION COMPLIANCE: NEVER write literal 'rm', 'rm -rf', or destructive commands in generated skills or SOPs. The execution environment enforces an Anti-Deletion Protocol that strictly blocks them. Always use non-destructive operations, such as archiving via 'mv <target> /app/workspace/archive/', when removing or cleaning up files.""",
    "autonomous_audit_nudge": (
        "[SYSTEM AUTONOMOUS AUDIT: You did not invoke any tools. "
        "If your task is 100% finished and all requested deliverables/files are verified on disk, reply confirming completion without invoking tools to conclude. "
        "If your task is NOT finished, invoke your next tool call NOW (do NOT reply with text explaining what you plan to do; execute the tool call directly).]"
    ),
    "tool_loop_soft_warning": (
        "[SYSTEM LOOP ADVISORY: You have executed '{tool_name}' {count} times consecutively with the exact same arguments. "
        "Please verify that this repeated execution is making meaningful progress. "
        "If you are stuck in an unintended loop, adjust your strategy or proceed to your next step.]"
    ),
    "handoff_wake_up_directive": (
        "[SYSTEM TRANSITION HANDOFF: Context has been compacted and archived to disk. "
        "Memory compression is COMPLETE (do NOT call compact_context again). "
        "Revisit your plan: "
        "- If any tasks still need to be done, proceed with them. "
        "- If everything is already finished, output your final summary to conclude.]"
    ),
}

SYSTEM_PROMPTS = {
    "brain": PROMPTS["overseer_system"],
    "coder": PROMPTS["coder_system"],
    "adviser": PROMPTS["adviser_system"],
    "summarizer": PROMPTS["summarizer_system"],
    "analyst": PROMPTS["analyst_system"],
    "architect": PROMPTS["architect_system"]
}

# --- TOKEN TRACKING UTILITIES ---
def log_token_usage(state_dir, agent_name, prompt_tokens, completion_tokens, thinking_tokens=0):
    """Logs token usage for an agent call to a shared state file."""
    import json
    import os
    import time
    from datetime import datetime

    file_path = os.path.join(state_dir, "token_usage.json")
    
    # Ensure state directory exists
    os.makedirs(state_dir, exist_ok=True)
    
    # Acquire file-level lock to prevent concurrent write race conditions
    lock_file = f"{file_path}.lock"
    acquired = False
    for _ in range(100):  # Wait up to 10 seconds total
        try:
            fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            acquired = True
            break
        except FileExistsError:
            try:
                mtime = os.path.getmtime(lock_file)
                if time.time() - mtime > 10.0:
                    try:
                        os.remove(lock_file)
                    except Exception:
                        pass
            except Exception:
                pass
            time.sleep(0.1)

    try:
        data = {}
        if os.path.exists(file_path):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                pass
                
        if "totals" not in data:
            data["totals"] = {}
        if "history" not in data:
            data["history"] = []
            
        # Update agent totals
        if agent_name not in data["totals"]:
            data["totals"][agent_name] = {"prompt": 0, "completion": 0, "thinking": 0, "total": 0, "calls": 0}
            
        agent_totals = data["totals"][agent_name]
        if "calls" not in agent_totals:
            agent_totals["calls"] = sum(1 for h in data.get("history", []) if h.get("agent") == agent_name)
        agent_totals["prompt"] += prompt_tokens
        agent_totals["completion"] += completion_tokens
        agent_totals["thinking"] += thinking_tokens
        agent_totals["total"] += (prompt_tokens + completion_tokens)
        agent_totals["calls"] += 1
        
        # Update grand totals
        if "_grand_total" not in data["totals"]:
            data["totals"]["_grand_total"] = {"prompt": 0, "completion": 0, "thinking": 0, "total": 0, "calls": 0}
            
        grand = data["totals"]["_grand_total"]
        if "calls" not in grand:
            grand["calls"] = len(data.get("history", []))
        grand["prompt"] += prompt_tokens
        grand["completion"] += completion_tokens
        grand["thinking"] += thinking_tokens
        grand["total"] += (prompt_tokens + completion_tokens)
        grand["calls"] += 1
        
        # Append to history
        data["history"].append({
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "agent": agent_name,
            "prompt": prompt_tokens,
            "completion": completion_tokens,
            "thinking": thinking_tokens,
            "total": prompt_tokens + completion_tokens
        })
        
        # Save atomically
        temp_path = f"{file_path}.tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
            os.replace(temp_path, file_path)
        except Exception:
            # Fallback
            try:
                with open(file_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=4)
            except Exception:
                pass
    finally:
        if acquired:
            try:
                os.remove(lock_file)
            except Exception:
                pass

def get_token_totals(state_dir):
    """Retrieves current token totals from the state file."""
    import json
    import os
    
    file_path = os.path.join(state_dir, "token_usage.json")
    if not os.path.exists(file_path):
        return {}
        
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            totals = data.get("totals", {})
            history = data.get("history", [])
            for ag, det in totals.items():
                if isinstance(det, dict) and "calls" not in det:
                    det["calls"] = len(history) if ag == "_grand_total" else sum(1 for h in history if h.get("agent") == ag)
            return totals
    except Exception:
        return {}

def get_agent_turn_count(state_dir, agent_name="brain"):
    """Retrieves the total number of completed turns/calls for a specific agent."""
    totals = get_token_totals(state_dir)
    return totals.get(agent_name, {}).get("calls", 0)

def get_totals_diff(before, after):
    """Computes the difference between two totals dictionaries."""
    diff = {}
    for agent, details in after.items():
        if agent == "_grand_total":
            continue
        prev_details = before.get(agent, {"prompt": 0, "completion": 0, "thinking": 0, "total": 0})
        prompt_diff = details["prompt"] - prev_details["prompt"]
        completion_diff = details["completion"] - prev_details["completion"]
        thinking_diff = details["thinking"] - prev_details["thinking"]
        total_diff = details["total"] - prev_details["total"]
        
        if total_diff > 0:
            diff[agent] = {
                "prompt": prompt_diff,
                "completion": completion_diff,
                "thinking": thinking_diff,
                "total": total_diff
            }
    return diff


# --- TOOL USAGE TRACKING UTILITIES ---
def log_tool_usage(state_dir, tool_name, caller="brain", duration_seconds=0.0, is_error=False):
    """Logs tool usage metrics to a shared state file."""
    import json
    import os
    import time
    from datetime import datetime

    file_path = os.path.join(state_dir, "tool_usage.json")
    os.makedirs(state_dir, exist_ok=True)

    lock_file = f"{file_path}.lock"
    acquired = False
    for _ in range(100):
        try:
            fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            acquired = True
            break
        except FileExistsError:
            try:
                mtime = os.path.getmtime(lock_file)
                if time.time() - mtime > 10.0:
                    try:
                        os.remove(lock_file)
                    except Exception:
                        pass
            except Exception:
                pass
            time.sleep(0.1)

    try:
        data = {}
        if os.path.exists(file_path):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                pass

        if "totals" not in data:
            data["totals"] = {
                "total_calls": 0,
                "by_tool": {},
                "by_caller": {},
                "total_duration": 0.0,
                "errors": 0
            }
        if "history" not in data:
            data["history"] = []

        totals = data["totals"]
        totals["total_calls"] = totals.get("total_calls", 0) + 1

        if "by_tool" not in totals:
            totals["by_tool"] = {}
        totals["by_tool"][tool_name] = totals["by_tool"].get(tool_name, 0) + 1

        if "by_caller" not in totals:
            totals["by_caller"] = {}
        totals["by_caller"][caller] = totals["by_caller"].get(caller, 0) + 1

        totals["total_duration"] = round(totals.get("total_duration", 0.0) + duration_seconds, 3)
        if is_error:
            totals["errors"] = totals.get("errors", 0) + 1

        data["history"].append({
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "tool": tool_name,
            "caller": caller,
            "duration": round(duration_seconds, 3),
            "is_error": is_error
        })

        temp_path = f"{file_path}.tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
            os.replace(temp_path, file_path)
        except Exception:
            try:
                with open(file_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=4)
            except Exception:
                pass
    finally:
        if acquired:
            try:
                os.remove(lock_file)
            except Exception:
                pass


def get_tool_totals(state_dir):
    """Retrieves current tool totals from the state file."""
    import json
    import os

    file_path = os.path.join(state_dir, "tool_usage.json")
    if not os.path.exists(file_path):
        return {"total_calls": 0, "by_tool": {}, "by_caller": {}, "total_duration": 0.0, "errors": 0}

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("totals", {"total_calls": 0, "by_tool": {}, "by_caller": {}, "total_duration": 0.0, "errors": 0})
    except Exception:
        return {"total_calls": 0, "by_tool": {}, "by_caller": {}, "total_duration": 0.0, "errors": 0}


def extract_skill_description(content: str, max_len: int = 200) -> str:
    """Extracts the description from a SKILL.md YAML frontmatter block (supports inline, quoted, and multiline > or | blocks)."""
    import re
    fm_match = re.match(r"^---\s*\r?\n(.*?)\r?\n---", content, re.DOTALL)
    block = fm_match.group(1) if fm_match else content
    lines = block.splitlines()
    for idx, line in enumerate(lines):
        m = re.match(r"^description:\s*(.*)$", line)
        if m:
            first_val = m.group(1).strip()
            if first_val in ("", ">", "|", ">-", "|-", ">+", "|+"):
                collected = []
                for sub_line in lines[idx + 1:]:
                    if re.match(r"^[A-Za-z0-9_-]+\s*:", sub_line):
                        break
                    if sub_line.strip():
                        collected.append(sub_line.strip())
                desc = " ".join(collected)
            else:
                desc = first_val.strip("\"'")
            desc = re.sub(r"[\r\n\x00-\x1f]+", " ", desc).strip()
            return desc[:max_len] if desc else "No description provided."
    return "No description provided."


# --- BACKGROUND JOBS STATE UTILITIES ---
def read_bg_jobs(state_dir):
    """Reads state/jobs/bg_jobs.json safely."""
    import json
    import os

    jobs_dir = os.path.join(state_dir, "jobs")
    file_path = os.path.join(jobs_dir, "bg_jobs.json")
    if not os.path.exists(file_path):
        return {"next_id": 1, "jobs": {}}
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return {"next_id": 1, "jobs": {}}
            data.setdefault("next_id", 1)
            data.setdefault("jobs", {})
            return data
    except Exception:
        return {"next_id": 1, "jobs": {}}


def update_bg_jobs(state_dir, mutator_fn):
    """Atomically reads, mutates via mutator_fn(data), and writes state/jobs/bg_jobs.json under a file lock."""
    import json
    import os
    import time

    jobs_dir = os.path.join(state_dir, "jobs")
    os.makedirs(jobs_dir, exist_ok=True)
    file_path = os.path.join(jobs_dir, "bg_jobs.json")
    lock_file = f"{file_path}.lock"
    acquired = False
    for _ in range(100):
        try:
            fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            acquired = True
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock_file) > 10.0:
                    try:
                        os.remove(lock_file)
                    except Exception:
                        pass
            except Exception:
                pass
            time.sleep(0.05)

    try:
        data = {"next_id": 1, "jobs": {}}
        if os.path.exists(file_path):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        data = loaded
                        data.setdefault("next_id", 1)
                        data.setdefault("jobs", {})
            except Exception:
                pass

        res = mutator_fn(data)

        temp_path = f"{file_path}.{os.getpid()}.tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(temp_path, file_path)
        except Exception:
            try:
                with open(file_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
            except Exception:
                pass
        return res
    finally:
        if acquired:
            try:
                os.remove(lock_file)
            except Exception:
                pass

