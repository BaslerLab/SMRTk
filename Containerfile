FROM ghcr.io/prefix-dev/pixi:latest

# ROOT LEVEL: Install system-level scientific, media, and tool compilers
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

# Switch to non-root user before installing tools
USER agent

# Explicitly provision Rust toolchains into the user path boundaries safely
ENV RUSTUP_HOME=/home/agent/.rustup \
    CARGO_HOME=/home/agent/.cargo \
    PATH=/home/agent/.cargo/bin:$PATH

# Install Rust toolchain natively for the non-root execution framework
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable --no-modify-path

# Add conda-forge and bioconda to give the AI maximum scientific reach
# Chained the Playwright install and cache cleanup into a single layer to minimize final image size.
RUN pixi init && \
    pixi project channel add conda-forge && \
    pixi project channel add bioconda && \
    pixi add python pip openai mcp fastmcp \
    pandas numpy scipy matplotlib pyarrow \
    requests beautifulsoup4 lxml httpx \
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