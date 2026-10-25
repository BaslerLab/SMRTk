#!/usr/bin/env bash
# ==============================================================================
# SMRTk: Sandboxed Multi-agent Research Toolkit — Automated WSL2 & Environment Setup Script
# ==============================================================================
# This script automates the complete provisioning of a fresh WSL2 instance:
#   Phase 1 (System/Root): Packages, NVIDIA GPU CDI, Agent user, subuid/gid, wsl.conf
#   Phase 2 (User/Agent):  Pixi, Workspace env, LiteLLM Proxy, Podman build, Self-test
#
# Usage:
#   sudo ./setup_wsl2.sh           # Full setup from fresh WSL2 (runs Phase 1 -> Phase 2)
#   ./setup_wsl2.sh --user-only    # User-space only (Pixi, LiteLLM, Container build)
#   ./setup_wsl2.sh --check        # Non-destructive environment diagnostics
#   ./setup_wsl2.sh --build-image  # Rebuild the rootless Podman container image
#   ./setup_wsl2.sh --help         # Show all available options
# ==============================================================================

set -eo pipefail

# --- Color formatting ---
BOLD="\033[1m"
GREEN="\033[0;32m"
YELLOW="\033[0;33m"
RED="\033[0;31m"
BLUE="\033[0;34m"
CYAN="\033[0;36m"
RESET="\033[0m"

log_info()  { echo -e "${BLUE}[INFO]${RESET} $1"; }
log_step()  { echo -e "\n${BOLD}${CYAN}==>${RESET} ${BOLD}$1${RESET}"; }
log_done()  { echo -e "${GREEN}[✓]${RESET} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${RESET} $1"; }
log_err()   { echo -e "${RED}[ERROR]${RESET} $1" >&2; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_USER="agent"
CUSTOM_USER=""
IMAGE_NAME="sandbox-image"
AUTO_CONFIRM=false

# Detect calling user if run with sudo
if [ -n "$SUDO_USER" ] && [ "$SUDO_USER" != "root" ]; then
    CALLING_USER="$SUDO_USER"
else
    CALLING_USER="$(whoami)"
fi

# --- Parse Arguments ---
MODE="auto"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --check)
            MODE="check"
            shift
            ;;
        --system-only|--phase1)
            MODE="system"
            shift
            ;;
        --user-only|--phase2)
            MODE="user"
            shift
            ;;
        --build-image)
            MODE="build-image"
            shift
            ;;
        --user)
            CUSTOM_USER="$2"
            shift 2
            ;;
        -y|--yes)
            AUTO_CONFIRM=true
            shift
            ;;
        -h|--help)
            echo -e "${BOLD}SMRTk WSL2 Automated Installer${RESET}"
            echo ""
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  (no args)           Auto-detect: runs system setup if root, user setup if normal user"
            echo "  --system-only       Run Phase 1 (system packages, agent user, subuids, wsl.conf)"
            echo "  --user-only         Run Phase 2 (Pixi, dependencies, LiteLLM proxy, Podman build)"
            echo "  --build-image       Rebuild the rootless Podman container image only"
            echo "  --check             Run non-destructive diagnostics and report status"
            echo "  --user <username>   Set target non-root user (default: 'agent', or current user)"
            echo "  -y, --yes           Auto-confirm prompts with default answers"
            echo "  -h, --help          Show this help message"
            echo ""
            echo "Examples:"
            echo "  sudo ./setup_wsl2.sh                    # Full setup (offers isolated 'agent' user or current user)"
            echo "  sudo ./setup_wsl2.sh --user ubuntu      # Install for existing 'ubuntu' account"
            echo "  ./setup_wsl2.sh --user-only             # Run user-space setup as current user"
            echo "  ./setup_wsl2.sh --check                 # Non-destructive environment health check"
            exit 0
            ;;
        *)
            log_err "Unknown argument: $1"
            echo "Run '$0 --help' for usage."
            exit 1
            ;;
    esac
done

if [ -n "$CUSTOM_USER" ]; then
    TARGET_USER="$CUSTOM_USER"
fi

get_user_home() {
    local u="$1"
    if [ "$u" = "root" ]; then
        echo "/root"
    else
        eval echo "~$u"
    fi
}

PROXY_DIR="$(get_user_home "$TARGET_USER")/litellm_proxy"

prompt_input() {
    local prompt_msg="$1"
    local default_ans="$2"
    local var_name="$3"
    if [ "$AUTO_CONFIRM" = true ]; then
        eval "$var_name=\"\$default_ans\""
        return 0
    fi
    local answer=""
    if [ -t 0 ]; then
        read -r -p "$prompt_msg [$default_ans]: " answer
    elif [ -r /dev/tty ]; then
        read -r -p "$prompt_msg [$default_ans]: " answer < /dev/tty
    fi
    answer="${answer:-$default_ans}"
    eval "$var_name=\"\$answer\""
}

prompt_yn() {
    local prompt_msg="$1"
    local default_ans="$2"
    if [ "$AUTO_CONFIRM" = true ]; then
        return 0
    fi
    local answer=""
    if [ -t 0 ]; then
        read -r -p "$prompt_msg [$default_ans]: " answer
    elif [ -r /dev/tty ]; then
        read -r -p "$prompt_msg [$default_ans]: " answer < /dev/tty
    fi
    answer="${answer:-$default_ans}"
    [[ "$answer" =~ ^[Yy]$ ]]
}

# ==============================================================================
# DIAGNOSTICS & HEALTH CHECK
# ==============================================================================
run_diagnostics() {
    log_step "SMRTk Environment Diagnostics"
    echo "--------------------------------------------------------"

    # 1. OS & Architecture
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        echo -e "OS Distribution:     ${GREEN}${PRETTY_NAME}${RESET}"
    else
        echo -e "OS Distribution:     ${RED}Unknown${RESET}"
    fi
    if grep -qi microsoft /proc/version 2>/dev/null; then
        local is_systemd=false
        [ "$(ps -p 1 -o comm= 2>/dev/null)" = "systemd" ] && is_systemd=true
        echo -e "WSL2 Detection:      ${GREEN}Detected${RESET} (systemd: $($is_systemd && echo -e "${GREEN}Active (PID 1)${RESET}" || echo -e "${YELLOW}Inactive - add [boot] systemd=true in /etc/wsl.conf and run 'wsl --shutdown'${RESET}"))"
    else
        echo -e "WSL2 Detection:      ${YELLOW}Standard Linux (Not WSL2)${RESET}"
    fi

    # 2. Target Execution User
    echo -e "Current User:        ${CYAN}$(whoami)${RESET} (UID: $(id -u))"
    if id "$TARGET_USER" &>/dev/null; then
        echo -e "Execution User:      ${GREEN}Configured ('$TARGET_USER' UID: $(id -u "$TARGET_USER"))${RESET}"
    else
        echo -e "Execution User:      ${RED}Missing ('$TARGET_USER' does not exist)${RESET}"
    fi

    # 3. Subuid / Subgid
    local u_subuid=false
    local u_subgid=false
    grep -q "^${TARGET_USER}:" /etc/subuid 2>/dev/null && u_subuid=true || true
    grep -q "^${TARGET_USER}:" /etc/subgid 2>/dev/null && u_subgid=true || true
    echo -e "SubUID [${TARGET_USER}]:     $($u_subuid && echo -e "${GREEN}Configured in /etc/subuid${RESET}" || echo -e "${RED}Missing in /etc/subuid${RESET}")"
    echo -e "SubGID [${TARGET_USER}]:     $($u_subgid && echo -e "${GREEN}Configured in /etc/subgid${RESET}" || echo -e "${RED}Missing in /etc/subgid${RESET}")"

    # 4. Core System Packages
    for pkg in podman catatonit passt slirp4netns newuidmap curl git jq; do
        if command -v "$pkg" &>/dev/null; then
            echo -e "Package [${pkg}]:       ${GREEN}Installed${RESET} ($(command -v "$pkg"))"
        else
            echo -e "Package [${pkg}]:       ${RED}Not Found${RESET}"
        fi
    done

    # 5. GPU & NVIDIA CDI
    if command -v nvidia-smi &>/dev/null; then
        echo -e "NVIDIA Driver:       ${GREEN}Available${RESET} ($(nvidia-smi --query-gpu=gpu_name --format=csv,noheader 2>/dev/null | head -n1 || echo 'Detected'))"
    elif [ -e /dev/dxg ]; then
        echo -e "WSL GPU Bus (/dev/dxg): ${GREEN}Available${RESET}"
    else
        echo -e "GPU Passthrough:     ${YELLOW}No NVIDIA GPU detected (CPU mode)${RESET}"
    fi
    if [ -f /etc/cdi/nvidia.yaml ]; then
        echo -e "NVIDIA CDI Spec:     ${GREEN}Configured (/etc/cdi/nvidia.yaml)${RESET}"
    else
        echo -e "NVIDIA CDI Spec:     ${YELLOW}Not generated${RESET}"
    fi

    # 6. Pixi Package Manager
    if command -v pixi &>/dev/null || [ -f "$HOME/.pixi/bin/pixi" ] || [ -f "/home/${TARGET_USER}/.pixi/bin/pixi" ]; then
        echo -e "Pixi Manager:        ${GREEN}Installed${RESET}"
    else
        echo -e "Pixi Manager:        ${RED}Not Installed${RESET}"
    fi

    # 7. Podman Image
    if podman image exists "${IMAGE_NAME}" 2>/dev/null; then
        echo -e "Sandbox Container:   ${GREEN}Image '${IMAGE_NAME}' Ready${RESET}"
    else
        echo -e "Sandbox Container:   ${RED}Image '${IMAGE_NAME}' not built${RESET}"
    fi

    # 8. LiteLLM Proxy
    if [ -d "$PROXY_DIR" ] && [ -f "$PROXY_DIR/config.yaml" ]; then
        echo -e "LiteLLM Proxy:       ${GREEN}Configured ($PROXY_DIR)${RESET}"
    else
        echo -e "LiteLLM Proxy:       ${YELLOW}Not Configured${RESET}"
    fi
    if pgrep -f "litellm.*--port 4000" &>/dev/null || ss -tulpn 2>/dev/null | grep -q ":4000\b"; then
        echo -e "Proxy Port 4000:     ${GREEN}Running (Port 4000 Listening)${RESET}"
    else
        echo -e "Proxy Port 4000:     ${YELLOW}Not currently active${RESET}"
    fi
    echo "--------------------------------------------------------"
}

# ==============================================================================
# PHASE 1: SYSTEM SETUP (ROOT / SUDO)
# ==============================================================================
run_phase1_system() {
    log_step "Phase 1: System-Level Hardening & Package Installation (Root/Sudo)"

    if [ "$EUID" -ne 0 ]; then
        log_err "Phase 1 requires administrative privileges. Please run with sudo:"
        echo "    sudo $0"
        exit 1
    fi

    # Check package manager (Debian/Ubuntu)
    if ! command -v apt-get &>/dev/null; then
        log_err "Unsupported package manager. Automated installation currently targets Debian/Ubuntu-based WSL2 instances (apt-get)."
        echo "For Fedora, Arch, or Alpine WSL2, please consult the manual installation guide in readme.md."
        exit 1
    fi

    # Check for legacy WSL1
    if grep -qi microsoft /proc/version 2>/dev/null; then
        if ! uname -r | grep -qi "WSL2" && [ ! -e /dev/dxg ]; then
            log_err "Legacy WSL1 detected! Rootless Podman and GPU passthrough require WSL2."
            echo "Please upgrade your WSL instance to version 2 from Windows PowerShell:"
            echo "    wsl --set-version <distro-name> 2"
            exit 1
        fi
    fi

    # User isolation selection if not explicitly set via --user
    if [ -z "$CUSTOM_USER" ] && [ -n "$CALLING_USER" ] && [ "$CALLING_USER" != "root" ] && [ "$CALLING_USER" != "agent" ]; then
        echo ""
        log_info "Detected calling user: '${CALLING_USER}'."
        if [ "$AUTO_CONFIRM" != true ]; then
            echo -e "You can choose your setup mode:"
            echo -e "  [1] Dedicated '${GREEN}agent${RESET}' user (Default & Recommended for maximum sandbox isolation)"
            echo -e "  [2] Install SMRTk for your existing account ('${CYAN}${CALLING_USER}${RESET}')"
            local setup_mode="1"
            prompt_input "Select setup mode [1 or 2]" "1" setup_mode
            if [ "$setup_mode" = "2" ]; then
                TARGET_USER="$CALLING_USER"
            else
                TARGET_USER="agent"
            fi
        else
            TARGET_USER="agent"
        fi
        PROXY_DIR="$(get_user_home "$TARGET_USER")/litellm_proxy"
    fi

    # 1. Update and install core system utilities
    log_info "Updating apt package index..."
    apt-get update -y

    log_info "Installing core dependencies (podman, catatonit, passt, slirp4netns, uidmap, git, curl, jq, build-essential)..."
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        curl \
        wget \
        podman \
        catatonit \
        passt \
        slirp4netns \
        uidmap \
        git \
        jq \
        ca-certificates \
        gnupg \
        build-essential

    log_done "Core system packages installed."

    # 2. NVIDIA GPU Passthrough & CDI Setup (if GPU present)
    log_step "Probing for NVIDIA GPU hardware..."
    local has_gpu=false
    if command -v nvidia-smi &>/dev/null || [ -e /dev/dxg ] || lspci 2>/dev/null | grep -qi nvidia; then
        has_gpu=true
    fi

    if [ "$has_gpu" = true ]; then
        log_info "NVIDIA hardware detected. Configuring NVIDIA Container Toolkit..."
        if ! command -v nvidia-ctk &>/dev/null; then
            mkdir -p /etc/apt/keyrings
            curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor -o /etc/apt/keyrings/nvidia-container-toolkit-keyring.gpg --yes
            curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
                sed 's#deb https://#deb [signed-by=/etc/apt/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
                tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
            apt-get update -y
            DEBIAN_FRONTEND=noninteractive apt-get install -y nvidia-container-toolkit
        fi

        log_info "Generating NVIDIA Container Device Interface (CDI) specification..."
        mkdir -p /etc/cdi
        nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml || log_warn "CDI generation warning; container will still boot."
        log_done "NVIDIA Container Toolkit & CDI configured."
    else
        log_info "No NVIDIA GPU detected; skipping NVIDIA Container Toolkit (CPU mode ready)."
    fi

    # 3. Configure target execution user
    log_step "Configuring execution user '${TARGET_USER}'..."
    if ! id -u "$TARGET_USER" &>/dev/null; then
        useradd -m -s /bin/bash "$TARGET_USER"
        log_done "Created execution user '${TARGET_USER}'."
    else
        log_info "Execution user '${TARGET_USER}' already exists."
    fi

    # 4. Configure subuid and subgid for rootless Podman
    log_info "Configuring rootless Podman subuid and subgid mappings for ${TARGET_USER}..."
    local subuid_start=100000
    [ "$TARGET_USER" = "agent" ] && subuid_start=200000
    local subuid_end=$((subuid_start + 65535))

    if ! grep -q "^${TARGET_USER}:" /etc/subuid 2>/dev/null; then
        usermod --add-subuids "${subuid_start}-${subuid_end}" "$TARGET_USER" 2>/dev/null || echo "${TARGET_USER}:${subuid_start}:65536" >> /etc/subuid
        log_done "Provisioned subuids for ${TARGET_USER} (${subuid_start}-${subuid_end})."
    else
        log_info "Subuids already configured for ${TARGET_USER}."
    fi

    if ! grep -q "^${TARGET_USER}:" /etc/subgid 2>/dev/null; then
        usermod --add-subgids "${subuid_start}-${subuid_end}" "$TARGET_USER" 2>/dev/null || echo "${TARGET_USER}:${subuid_start}:65536" >> /etc/subgid
        log_done "Provisioned subgids for ${TARGET_USER} (${subuid_start}-${subuid_end})."
    else
        log_info "Subgids already configured for ${TARGET_USER}."
    fi

    # Enable lingering so user cgroups, D-Bus session, and rootless Podman daemons persist
    if command -v loginctl &>/dev/null; then
        loginctl enable-linger "$TARGET_USER" 2>/dev/null || true
        log_done "Enabled user lingering for ${TARGET_USER} via loginctl."
    fi

    # Register rootless system mappings
    if command -v podman &>/dev/null; then
        su - "${TARGET_USER}" -c "podman system migrate" 2>/dev/null || true
    fi

    # 5. WSL2 Systemd Configuration & Hardening (/etc/wsl.conf)
    if grep -qi microsoft /proc/version 2>/dev/null; then
        log_step "Configuring WSL2 systemd and /etc/wsl.conf..."
        local wsl_needs_shutdown=false

        # Ensure [boot] section with systemd=true exists
        if [ ! -f /etc/wsl.conf ] || ! grep -q "systemd\s*=\s*true" /etc/wsl.conf 2>/dev/null; then
            mkdir -p /etc
            if [ -f /etc/wsl.conf ]; then
                cp /etc/wsl.conf "/etc/wsl.conf.bak.$(date +%Y%m%d%H%M%S)"
            fi
            if grep -q "^\[boot\]" /etc/wsl.conf 2>/dev/null; then
                sed -i '/^\[boot\]/a systemd=true' /etc/wsl.conf
            else
                cat <<'EOF' >> /etc/wsl.conf

[boot]
systemd=true
EOF
            fi
            wsl_needs_shutdown=true
            log_done "Configured systemd=true in /etc/wsl.conf."
        else
            log_info "WSL2 systemd=true is already present in /etc/wsl.conf."
        fi

        # Optional hardening for dedicated agent user
        if [ "$TARGET_USER" = "agent" ]; then
            local apply_wsl=true
            if [ "$AUTO_CONFIRM" != true ]; then
                if ! prompt_yn "Configure /etc/wsl.conf for 'agent' (set default user: agent, disable automount/interop)?" "Y"; then
                    apply_wsl=false
                fi
            fi

            if [ "$apply_wsl" = true ]; then
                if ! grep -q "^\[automount\]" /etc/wsl.conf 2>/dev/null; then
                    cat <<EOF >> /etc/wsl.conf

[user]
default=${TARGET_USER}

[automount]
enabled = false

[interop]
enabled = false
appendWindowsPath = false
EOF
                    wsl_needs_shutdown=true
                    log_done "Wrote security hardening to /etc/wsl.conf."
                else
                    log_done "/etc/wsl.conf already contains security hardening."
                fi
            fi
        else
            log_info "Installing for standard account '${TARGET_USER}'. Skipping automount/interop hardening to preserve existing user login and Windows mounts."
        fi

        # Warn if systemd is not yet running as PID 1
        if [ "$(ps -p 1 -o comm= 2>/dev/null)" != "systemd" ] || [ "$wsl_needs_shutdown" = true ]; then
            echo ""
            log_warn "*******************************************************************************"
            log_warn "WSL2 systemd is configured in /etc/wsl.conf but is not currently active (PID 1)."
            log_warn "Rootless Podman, D-Bus session buses, and user cgroups require systemd."
            log_warn "Once this script finishes, you MUST execute in Windows PowerShell/CMD:"
            log_warn "    wsl --shutdown"
            log_warn "Then reopen your WSL2 terminal to activate systemd."
            log_warn "*******************************************************************************"
            echo ""
        fi
    fi

    # 6. Ensure proper workspace ownership & isolation
    log_info "Ensuring workspace permissions for ${TARGET_USER}..."
    local target_home="$(get_user_home "$TARGET_USER")"
    if [ "$TARGET_USER" = "agent" ] && [[ "$SCRIPT_DIR" != "$target_home"/* ]] && [ "$SCRIPT_DIR" != "$target_home" ]; then
        mkdir -p "$target_home/ai_workspace"
        cp -a "$SCRIPT_DIR/." "$target_home/ai_workspace/"
        SCRIPT_DIR="$target_home/ai_workspace"
        chown -R "${TARGET_USER}:${TARGET_USER}" "$SCRIPT_DIR" 2>/dev/null || true
        log_done "Copied workspace into isolated $SCRIPT_DIR for user '${TARGET_USER}'."
    else
        chown -R "${TARGET_USER}:${TARGET_USER}" "$SCRIPT_DIR" 2>/dev/null || true
    fi

    log_done "Phase 1 (System Configuration) completed successfully!"
}

# ==============================================================================
# PHASE 2: USER-SPACE SETUP (PIXI, LITELLM, CONTAINER IMAGE)
# ==============================================================================
run_phase2_user() {
    log_step "Phase 2: User-Space Setup & Container Build"

    if [ "$EUID" -eq 0 ]; then
        log_warn "Phase 2 is running as root. It is recommended to run user setup as '${TARGET_USER}'."
        if [ "$AUTO_CONFIRM" != true ]; then
            if prompt_yn "Switch to user '${TARGET_USER}' to complete setup?" "Y"; then
                su - "${TARGET_USER}" -c "bash '$SCRIPT_DIR/setup_wsl2.sh' --user-only --user '${TARGET_USER}'"
                return
            fi
        fi
    fi

    # When running as a standard non-root user, synchronize target user with current execution user unless overridden
    if [ "$EUID" -ne 0 ] && [ -z "$CUSTOM_USER" ]; then
        TARGET_USER="$(whoami)"
    fi

    local USER_HOME="$(get_user_home "$TARGET_USER")"
    PROXY_DIR="${USER_HOME}/litellm_proxy"

    # 1. Install Pixi package manager
    log_step "Checking Pixi package manager installation..."
    local PIXI_BIN="$USER_HOME/.pixi/bin/pixi"
    if ! command -v pixi &>/dev/null && [ ! -x "$PIXI_BIN" ]; then
        log_info "Downloading and installing Pixi for $(whoami)..."
        curl -fsSL https://pixi.sh/install.sh | bash
        export PATH="$USER_HOME/.pixi/bin:$PATH"
    else
        log_done "Pixi is already installed."
    fi

    # Ensure PATH is exported in ~/.bashrc
    if [ -f "$USER_HOME/.bashrc" ] && ! grep -q 'pixi/bin' "$USER_HOME/.bashrc"; then
        echo 'export PATH="$HOME/.pixi/bin:$PATH"' >> "$USER_HOME/.bashrc"
        log_done "Added Pixi to ~/.bashrc PATH."
    fi
    export PATH="$USER_HOME/.pixi/bin:$PATH"

    # 2. Install workspace dependencies
    log_step "Installing SMRTk workspace dependencies with Pixi..."
    cd "$SCRIPT_DIR"
    if [ -f "pixi.toml" ]; then
        "$PIXI_BIN" install
        log_done "Workspace Pixi environment installed."
    else
        log_warn "pixi.toml not found in $SCRIPT_DIR; skipping workspace pixi install."
    fi

    # 3. Setup Zero-Trust LiteLLM Proxy
    log_step "Configuring Zero-Trust LiteLLM Proxy in ${PROXY_DIR}..."
    mkdir -p "$PROXY_DIR"
    cd "$PROXY_DIR"

    if [ ! -f "pixi.toml" ]; then
        cat <<'EOF' > pixi.toml
[workspace]
channels = ["conda-forge"]
name = "litellm_proxy"
platforms = ["linux-64"]
version = "0.1.0"

[tasks]

[dependencies]
python = "3.12.*"
litellm = ">=1.83.14,<2"
pip = ">=26.0.1,<27"
EOF
        log_done "Created $PROXY_DIR/pixi.toml."
    fi

    log_info "Installing LiteLLM environment dependencies..."
    "$PIXI_BIN" install
    "$PIXI_BIN" run pip install 'litellm[proxy]' 2>/dev/null || true

    # 3b. Interactive Primary Default LLM Configuration
    log_step "Configuring Default LLM Endpoint (Out-of-the-Box Setup)..."
    echo -e "Configure your primary LLM endpoint so the framework is fully functional out of the box."

    local llm_name="claude-haiku-5.5"
    local llm_base_url="https://openrouter.ai/api/v1"
    local llm_api_key=""
    local llm_model="openrouter/anthropic/claude-haiku-5.5"
    local provider_choice="1"
    local prompt_user_for_llm=true

    if [ "$AUTO_CONFIRM" = true ]; then
        prompt_user_for_llm=false
    elif ! prompt_yn "Configure default LLM endpoint now?" "Y"; then
        prompt_user_for_llm=false
    fi

    if [ "$prompt_user_for_llm" = true ]; then
        echo ""
        echo -e "${BOLD}Select your primary default LLM provider:${RESET}"
        echo -e "  [1] ${CYAN}OpenRouter${RESET} (Recommended for CPU & multi-model routing: Claude, Llama 3, DeepSeek, etc.)"
        echo -e "      Default: Anthropic Claude Haiku 5.5 ('openrouter/anthropic/claude-haiku-5.5')"
        echo -e "  [2] ${CYAN}Direct OpenAI${RESET} (api.openai.com)"
        echo -e "      Default: GPT-6 Luna ('openai/gpt-6-luna')"
        echo -e "  [3] ${CYAN}Direct Google Gemini${RESET} (generativelanguage.googleapis.com)"
        echo -e "      Default: Gemini 3.8 Flash ('gemini/gemini-3.8-flash')"
        echo -e "  [4] ${CYAN}Local Ollama / vLLM${RESET} (Self-hosted on localhost)"
        echo -e "      Default: Qwen 2.5 ('ollama/qwen2.5:latest' on http://localhost:11434/v1)"
        echo -e "  [5] ${CYAN}Custom Endpoint${RESET} (Custom OpenAI-compatible base URL / model)"
        echo ""
        prompt_input "Select provider [1-5]" "1" provider_choice

        case "$provider_choice" in
            1)
                llm_name="claude-haiku-5.5"
                llm_base_url="https://openrouter.ai/api/v1"
                llm_model="openrouter/anthropic/claude-haiku-5.5"
                echo ""
                echo -e "${BOLD}Configuring OpenRouter:${RESET}"
                prompt_input "  [1/3] Overseer Model Alias" "claude-haiku-5.5" llm_name
                prompt_input "  [2/3] OpenRouter Upstream Model" "openrouter/anthropic/claude-haiku-5.5" llm_model
                prompt_input "  [3/3] OpenRouter API Key (sk-or-v1-...)" "${OPENROUTER_API_KEY:-sk-or-v1-placeholder}" llm_api_key
                ;;
            2)
                llm_name="gpt-6-luna"
                llm_base_url="https://api.openai.com/v1"
                llm_model="openai/gpt-6-luna"
                echo ""
                echo -e "${BOLD}Configuring Direct OpenAI:${RESET}"
                prompt_input "  [1/3] Overseer Model Alias" "gpt-6-luna" llm_name
                prompt_input "  [2/3] Upstream Model" "openai/gpt-6-luna" llm_model
                prompt_input "  [3/3] OpenAI API Key (sk-...)" "${OPENAI_API_KEY:-sk-placeholder}" llm_api_key
                ;;
            3)
                llm_name="gemini-3.8-flash"
                llm_base_url="https://generativelanguage.googleapis.com/v1beta"
                llm_model="gemini/gemini-3.8-flash"
                echo ""
                echo -e "${BOLD}Configuring Direct Google Gemini:${RESET}"
                prompt_input "  [1/3] Overseer Model Alias" "gemini-3.8-flash" llm_name
                prompt_input "  [2/3] Upstream Model" "gemini/gemini-3.8-flash" llm_model
                prompt_input "  [3/3] Gemini API Key" "${GEMINI_API_KEY:-AIzaSy-placeholder}" llm_api_key
                ;;
            4)
                llm_name="qwen2.5"
                llm_base_url="http://localhost:11434/v1"
                llm_api_key="local-key"
                llm_model="ollama/qwen2.5:latest"
                echo ""
                echo -e "${BOLD}Configuring Local Ollama / vLLM:${RESET}"
                prompt_input "  [1/4] Overseer Model Alias" "qwen2.5" llm_name
                prompt_input "  [2/4] Base URL" "http://localhost:11434/v1" llm_base_url
                prompt_input "  [3/4] API Key (dummy value for local)" "local-key" llm_api_key
                prompt_input "  [4/4] Upstream Model" "ollama/qwen2.5:latest" llm_model
                ;;
            *)
                llm_name="default-llm"
                llm_base_url="http://localhost:11434/v1"
                llm_api_key="local-key"
                llm_model="openai/qwen3.8"
                echo ""
                echo -e "${BOLD}Configuring Custom LLM Endpoint:${RESET}"
                prompt_input "  [1/4] Overseer Model Alias" "default-llm" llm_name
                prompt_input "  [2/4] Base URL" "http://localhost:11434/v1" llm_base_url
                prompt_input "  [3/4] API Key" "local-key" llm_api_key
                prompt_input "  [4/4] Upstream Model (with provider prefix)" "openai/qwen3.8" llm_model
                ;;
        esac
        echo ""
    else
        # Unattended / non-interactive defaults: check environment variables
        if [ -n "$OPENROUTER_API_KEY" ]; then
            llm_name="claude-haiku-5.5"
            llm_base_url="https://openrouter.ai/api/v1"
            llm_api_key="$OPENROUTER_API_KEY"
            llm_model="openrouter/anthropic/claude-haiku-5.5"
        elif [ -n "$OPENAI_API_KEY" ]; then
            llm_name="gpt-6-luna"
            llm_base_url="https://api.openai.com/v1"
            llm_api_key="$OPENAI_API_KEY"
            llm_model="openai/gpt-6-luna"
        elif [ -n "$GEMINI_API_KEY" ]; then
            llm_name="gemini-3.8-flash"
            llm_base_url="https://generativelanguage.googleapis.com/v1beta"
            llm_api_key="$GEMINI_API_KEY"
            llm_model="gemini/gemini-3.8-flash"
        else
            llm_name="claude-haiku-5.5"
            llm_base_url="https://openrouter.ai/api/v1"
            llm_api_key="sk-or-v1-placeholder"
            llm_model="openrouter/anthropic/claude-haiku-5.5"
        fi
    fi

    # Strip provider prefix for Overseer / llm_config.py if user typed one (e.g. 'openai/qwen3.8' -> 'qwen3.8')
    local clean_model_name="${llm_name}"
    clean_model_name="${clean_model_name#openai/}"
    clean_model_name="${clean_model_name#openrouter/}"
    clean_model_name="${clean_model_name#ollama/}"
    clean_model_name="${clean_model_name#anthropic/}"
    clean_model_name="${clean_model_name#gemini/}"

    # Ensure LiteLLM upstream model identifier has a provider prefix
    local litellm_upstream_model="${llm_model}"
    if [[ "$litellm_upstream_model" != *"/"* ]]; then
        # Default to 'openai/' prefix for OpenAI-compatible endpoints
        litellm_upstream_model="openai/${litellm_upstream_model}"
    fi

    # Create/update LiteLLM config.yaml
    local write_yaml=true
    if [ -f "config.yaml" ]; then
        if [ "$prompt_user_for_llm" = true ]; then
            if ! prompt_yn "config.yaml already exists. Overwrite with newly configured default model?" "Y"; then
                write_yaml=false
            fi
        else
            write_yaml=false
            log_info "$PROXY_DIR/config.yaml already exists; keeping existing configuration."
        fi
    fi

    if [ "$write_yaml" = true ]; then
        local default_alias_block=""
        if [ "$clean_model_name" != "default-llm" ]; then
            default_alias_block=$(cat <<EOF

  - model_name: default-llm
    litellm_params:
      model: ${litellm_upstream_model}
      api_base: os.environ/DEFAULT_LLM_BASE_URL
      api_key: os.environ/DEFAULT_LLM_API_KEY
EOF
)
        fi

        cat <<EOF > config.yaml
general_settings:
  master_key: sk-sandbox-fake-key  # Zero-Trust sandbox key matching llm_config.py

litellm_settings:
  drop_params: true  # Strips unsupported model parameters dynamically
  check_provider_endpoint: false  # Only return models in config.yaml; prevents dumping 100+ upstream models

model_list:
  # ============================================================================
  # [1] Active Default Model (Credentials safely stored in ~/.bashrc environment)
  # ============================================================================
  - model_name: ${clean_model_name}
    litellm_params:
      model: ${litellm_upstream_model}
      # LiteLLM automatically pulls these from your ~/.bashrc exports!
      api_base: os.environ/DEFAULT_LLM_BASE_URL
      api_key: os.environ/DEFAULT_LLM_API_KEY${default_alias_block}

  # ============================================================================
  # [2] OpenRouter Universal & Curated Examples
  #     Routes any 'openrouter/<provider>/<model>' cleanly via OPENROUTER_API_KEY
  # ============================================================================
  - model_name: "openrouter/*"
    litellm_params:
      model: "openrouter/*"
      api_key: os.environ/OPENROUTER_API_KEY

  # Example curated alias for latest Claude Haiku via OpenRouter:
  - model_name: "claude-haiku-5.5"
    litellm_params:
      model: "openrouter/anthropic/claude-haiku-5.5"
      api_key: os.environ/OPENROUTER_API_KEY

  # ============================================================================
  # [3] Google Gemini Direct (Google models directly via GEMINI_API_KEY)
  # ============================================================================
  - model_name: "gemini/*"
    litellm_params:
      model: "gemini/*"
      api_key: os.environ/GEMINI_API_KEY

  # Example curated alias for latest Google Flash direct:
  - model_name: "gemini-3.8-flash"
    litellm_params:
      model: "gemini/gemini-3.8-flash"
      api_key: os.environ/GEMINI_API_KEY

  # ============================================================================
  # [4] OpenAI Direct (OpenAI models directly via OPENAI_API_KEY)
  # ============================================================================
  - model_name: "openai/*"
    litellm_params:
      model: "openai/*"
      api_key: os.environ/OPENAI_API_KEY

  # Example curated alias for OpenAI's latest cost-efficient model:
  - model_name: "gpt-6-luna"
    litellm_params:
      model: "openai/gpt-6-luna"
      api_key: os.environ/OPENAI_API_KEY
EOF
        log_done "Generated $PROXY_DIR/config.yaml with default model '${clean_model_name}' (upstream: '${litellm_upstream_model}')."
    fi

    # Export LLM environment variables in ~/.bashrc
    if [ -f "$USER_HOME/.bashrc" ]; then
        sed -i '/# SMRTK DEFAULT LLM CONFIGURATION/,/export DEFAULT_LLM_MODEL=/d' "$USER_HOME/.bashrc" 2>/dev/null || true
        cat <<EOF >> "$USER_HOME/.bashrc"

# ==========================================
# SMRTK DEFAULT LLM CONFIGURATION
# ==========================================
export DEFAULT_LLM_NAME="${clean_model_name}"
export DEFAULT_LLM_BASE_URL="${llm_base_url}"
export DEFAULT_LLM_API_KEY="${llm_api_key}"
export DEFAULT_LLM_MODEL="${litellm_upstream_model}"
EOF
        export DEFAULT_LLM_NAME="${clean_model_name}"
        export DEFAULT_LLM_BASE_URL="${llm_base_url}"
        export DEFAULT_LLM_API_KEY="${llm_api_key}"
        export DEFAULT_LLM_MODEL="${litellm_upstream_model}"

        # Export provider keys if configured or provide safe defaults
        if [[ "$litellm_upstream_model" == openrouter/* ]] || [ "$provider_choice" = "1" ]; then
            sed -i '/export OPENROUTER_API_KEY=/d' "$USER_HOME/.bashrc" 2>/dev/null || true
            echo "export OPENROUTER_API_KEY=\"${llm_api_key}\"" >> "$USER_HOME/.bashrc"
            export OPENROUTER_API_KEY="${llm_api_key}"
        fi
        if [[ "$litellm_upstream_model" == openai/* ]] || [ "$provider_choice" = "2" ]; then
            sed -i '/export OPENAI_API_KEY=/d' "$USER_HOME/.bashrc" 2>/dev/null || true
            echo "export OPENAI_API_KEY=\"${llm_api_key}\"" >> "$USER_HOME/.bashrc"
            export OPENAI_API_KEY="${llm_api_key}"
        fi
        if [[ "$litellm_upstream_model" == gemini/* ]] || [ "$provider_choice" = "3" ]; then
            sed -i '/export GEMINI_API_KEY=/d' "$USER_HOME/.bashrc" 2>/dev/null || true
            echo "export GEMINI_API_KEY=\"${llm_api_key}\"" >> "$USER_HOME/.bashrc"
            export GEMINI_API_KEY="${llm_api_key}"
        fi

        # Ensure safe fallback exports so LiteLLM proxy starts without missing env var errors
        grep -q "OPENROUTER_API_KEY" "$USER_HOME/.bashrc" || echo 'export OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-}"' >> "$USER_HOME/.bashrc"
        grep -q "GEMINI_API_KEY" "$USER_HOME/.bashrc" || echo 'export GEMINI_API_KEY="${GEMINI_API_KEY:-}"' >> "$USER_HOME/.bashrc"
        grep -q "OPENAI_API_KEY" "$USER_HOME/.bashrc" || echo 'export OPENAI_API_KEY="${OPENAI_API_KEY:-}"' >> "$USER_HOME/.bashrc"
        log_done "Exported default LLM environment variables in ~/.bashrc."
    fi

    # Update llm_config.py with this default LLM profile (clean name without provider prefix)
    log_step "Updating framework LLM configuration ($SCRIPT_DIR/llm_config.py)..."
    python3 - "$SCRIPT_DIR/llm_config.py" "$clean_model_name" <<'PYEOF'
import sys, re

config_path = sys.argv[1]
model_alias = sys.argv[2]

try:
    with open(config_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Point all active agent profiles to 0 (Host) and 1 (Container)
    content = re.sub(r'ACTIVE_BRAIN_PROFILE\s*=\s*\d+', 'ACTIVE_BRAIN_PROFILE = 0', content)
    content = re.sub(r'ACTIVE_CODER_PROFILE\s*=\s*\d+', 'ACTIVE_CODER_PROFILE = 1', content)
    content = re.sub(r'ACTIVE_SUMMARIZER_PROFILE\s*=\s*\d+', 'ACTIVE_SUMMARIZER_PROFILE = 1', content)
    content = re.sub(r'ACTIVE_ADVISER_PROFILE\s*=\s*\d+', 'ACTIVE_ADVISER_PROFILE = 1', content)
    content = re.sub(r'ACTIVE_ANALYST_PROFILE\s*=\s*\d+', 'ACTIVE_ANALYST_PROFILE = 1', content)
    content = re.sub(r'ACTIVE_ARCHITECT_PROFILE\s*=\s*\d+', 'ACTIVE_ARCHITECT_PROFILE = 1', content)

    profile_marker = "# [0] Configured Default Model - WSL2 Host"
    new_profiles = f"""    # [0] Configured Default Model - WSL2 Host
    {{
        "name": "{model_alias}",
        "base_url": "http://localhost:4000/v1", 
        "api_key": "sk-sandbox-fake-key",
        "model": "{model_alias}",
        "api_params": {{
            "temperature": 0.7,
            "top_p": 0.95,
            "timeout": 300.0,
            "max_tokens": 16384,
            "extra_body": {{
                "chat_template_kwargs": {{"enable_thinking": True}}
            }}
        }}
    }},
    # [1] Configured Default Model - Podman Container
    {{
        "name": "{model_alias}",
        "base_url": "http://host.containers.internal:4000/v1", 
        "api_key": "sk-sandbox-fake-key",
        "model": "{model_alias}",
        "api_params": {{
            "temperature": 0.7,
            "top_p": 0.95,
            "timeout": 300.0,
            "max_tokens": 16384,
            "extra_body": {{
                "chat_template_kwargs": {{"enable_thinking": True}}
            }}
        }}
    }},
"""

    if profile_marker in content:
        pattern = r"    # \[0\] Configured Default Model - WSL2 Host.*?    # \[1\] Configured Default Model - Podman Container.*?\n    \},\n"
        content = re.sub(pattern, new_profiles, content, flags=re.DOTALL)
    else:
        content = content.replace("LLM_PROFILES = [\n", f"LLM_PROFILES = [\n{new_profiles}")

    with open(config_path, "w", encoding="utf-8") as f:
        f.write(content)
except Exception as e:
    print(f"Notice: llm_config.py profile update error: {e}", file=sys.stderr)
PYEOF
    log_done "Updated $SCRIPT_DIR/llm_config.py with active default profiles (0 & 1)."

    # LiteLLM Proxy Auto-Start helper in ~/.bashrc
    if [ -f "$USER_HOME/.bashrc" ] && ! grep -q "ZERO-TRUST AI PROXY AUTO-START" "$USER_HOME/.bashrc"; then
        cat <<'EOF' >> "$USER_HOME/.bashrc"

# ==========================================
# ZERO-TRUST AI PROXY AUTO-START
# ==========================================
if [ -d "$HOME/litellm_proxy" ] && [ -f "$HOME/litellm_proxy/config.yaml" ]; then
    if ! pgrep -f "litellm.*--port 4000" > /dev/null; then
        echo "🛡️ Starting Zero-Trust LiteLLM Proxy on port 4000..."
        (cd "$HOME/litellm_proxy" && nohup "$HOME/.pixi/bin/pixi" run litellm --config config.yaml --host 0.0.0.0 --port 4000 > proxy.log 2>&1 &)
    fi
fi
EOF
        log_done "Configured LiteLLM background auto-start in ~/.bashrc."
    fi

    # Start or restart LiteLLM Proxy in the background now so it's active immediately
    log_step "Starting LiteLLM proxy in the background on port 4000..."
    pkill -f "litellm.*--port 4000" 2>/dev/null || true
    sleep 1
    (cd "$PROXY_DIR" && nohup "$PIXI_BIN" run litellm --config config.yaml --host 0.0.0.0 --port 4000 > proxy.log 2>&1 &) || true
    sleep 2
    if pgrep -f "litellm.*--port 4000" > /dev/null; then
        log_done "LiteLLM Proxy is active and listening on port 4000."
    else
        log_info "LiteLLM proxy will start automatically on shell login (logs at $PROXY_DIR/proxy.log)."
    fi

    # 4. Ensure Rootless Podman Network Provider Fallback
    mkdir -p "$USER_HOME/.config/containers"
    local containers_conf="$USER_HOME/.config/containers/containers.conf"
    if ! command -v pasta &>/dev/null; then
        if [ ! -f "$containers_conf" ]; then
            cat <<'EOF' > "$containers_conf"
[network]
default_rootless_network_cmd = "slirp4netns"
EOF
            log_info "Configured slirp4netns fallback in ~/.config/containers/containers.conf (pasta not found)."
        elif ! grep -q "default_rootless_network_cmd" "$containers_conf" 2>/dev/null; then
            if grep -q "^\[network\]" "$containers_conf" 2>/dev/null; then
                sed -i '/^\[network\]/a default_rootless_network_cmd = "slirp4netns"' "$containers_conf"
            else
                cat <<'EOF' >> "$containers_conf"

[network]
default_rootless_network_cmd = "slirp4netns"
EOF
            fi
            log_info "Added slirp4netns fallback to ~/.config/containers/containers.conf."
        fi
    fi

    # 5. Build Rootless Podman Sandbox Container
    log_step "Building rootless Podman image '${IMAGE_NAME}' from Containerfile..."
    cd "$SCRIPT_DIR"
    if [ -f "Containerfile" ]; then
        podman build -t "${IMAGE_NAME}" -f Containerfile .
        log_done "Podman image '${IMAGE_NAME}' built successfully."
    else
        log_err "Containerfile not found in $SCRIPT_DIR!"
        exit 1
    fi

    # 6. Pre-create required workspace folders
    log_step "Creating necessary session and IO folders..."
    mkdir -p "$SCRIPT_DIR/my_host_input"
    mkdir -p "$SCRIPT_DIR/sessions"
    mkdir -p "$SCRIPT_DIR/logs"
    log_done "Folders initialized."

    # 7. Self-Test / Verification
    log_step "Running sandbox container self-verification test..."
    if podman run --rm "${IMAGE_NAME}" pixi run python -c "import sys, fastmcp, sqlite_vec; print('Sandbox verification passed! Python:', sys.version)" 2>&1; then
        log_done "Rootless Podman sandbox self-test passed!"
    else
        log_warn "Sandbox verification test returned non-zero exit; please inspect Podman logs."
    fi

    log_done "Phase 2 (User-Space Setup) completed successfully!"
}

# ==============================================================================
# MAIN ENTRYPOINT
# ==============================================================================

echo -e "${BOLD}${CYAN}"
cat <<'EOF'
   _____ __  _______  ________    
  / ___//  |/  / __ \/_  __/ /__  
  \__ \/ /|_/ / /_/ / / / / //_/  
 ___/ / /  / / _, _/ / / / ,<     
/____/_/  /_/_/ |_| /_/ /_/|_|    
                                  
  Sandboxed Multi-agent Research Toolkit - Setup
EOF
echo -e "${RESET}"

case "$MODE" in
    check)
        run_diagnostics
        exit 0
        ;;
    system)
        run_phase1_system
        exit 0
        ;;
    user)
        run_phase2_user
        exit 0
        ;;
    build-image)
        log_step "Rebuilding container image '${IMAGE_NAME}'..."
        cd "$SCRIPT_DIR"
        podman build -t "${IMAGE_NAME}" -f Containerfile .
        log_done "Image build complete."
        exit 0
        ;;
    auto)
        if [ "$EUID" -eq 0 ]; then
            log_info "Detected root/sudo privileges. Executing full setup (Phase 1 -> Phase 2)..."
            run_phase1_system
            echo ""
            log_step "Handing off to user '${TARGET_USER}' for Phase 2..."
            su - "${TARGET_USER}" -c "bash '$SCRIPT_DIR/setup_wsl2.sh' --user-only --user '${TARGET_USER}'"
        else
            log_info "Running as standard user ($(whoami)). Checking if system packages are installed..."
            if ! command -v podman &>/dev/null || ! command -v slirp4netns &>/dev/null; then
                log_warn "System packages (podman, slirp4netns) are missing. Administrative privileges required."
                echo "Please run Phase 1 with sudo first:"
                echo "    sudo $0"
                exit 1
            fi
            run_phase2_user
        fi
        ;;
esac

# ==============================================================================
# INSTRUCTIONS & NEXT STEPS
# ==============================================================================
echo ""
echo -e "${BOLD}${GREEN}======================================================================${RESET}"
echo -e "${BOLD}${GREEN}           🎉 SMRTk ENVIRONMENT SETUP COMPLETE!                       ${RESET}"
echo -e "${BOLD}${GREEN}======================================================================${RESET}"
echo ""
echo -e "${BOLD}Next Steps:${RESET}"
echo ""
echo -e "  ${BOLD}1. Launch SMRTk (The AI Scientist):${RESET}"
echo -e "     (Your default LLM is already configured in ~/.bashrc and LiteLLM is running on port 4000)"
echo -e "     Interactive Rich UI:"
echo -e "       ${CYAN}cd $SCRIPT_DIR && pixi run python smrtk.py${RESET}"
echo ""
echo -e "     Run a single headless task and auto-exit:"
echo -e "       ${CYAN}cd $SCRIPT_DIR && pixi run python smrtk.py -x -p \"Check environment and view tool registry\"${RESET}"
echo ""
echo -e "  ${BOLD}2. Run Automated Benchmarks & Tests:${RESET}"
echo -e "       ${CYAN}./run_tests.sh --list${RESET}"
echo -e "       ${CYAN}./run_tests.sh 1${RESET}"
echo ""
echo -e "  ${BOLD}3. Managing Models & Diagnostics (Optional):${RESET}"
echo -e "     Add additional LLM endpoints anytime:"
echo -e "       ${CYAN}nano ~/litellm_proxy/config.yaml${RESET}"
echo -e "     Run environment health check anytime:"
echo -e "       ${CYAN}./setup_wsl2.sh --check${RESET}"
echo -e "${BOLD}${GREEN}======================================================================${RESET}"
