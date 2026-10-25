"""
LLM Configuration
=================
This file defines:
  1. Role assignments (which profile each agent role uses).
  2. Embeddings endpoint & vector dimension configuration.
  3. Auxiliary LLM sandbox endpoint.
  4. LLM_PROFILES list of available models and endpoints.

Profiles can be addressed from:
  - WSL2 Host (using base_url="http://localhost:4000/v1")
  - Podman Container (using base_url="http://host.containers.internal:4000/v1")
"""

# --- ROLE ASSIGNMENTS ---
# Quick Profile Index Reference (see LLM_PROFILES below):
#   [0] Default Model (WSL2 Host)    | [1] Default Model (Podman Container)
#   [2] Qwen 3.6 35B vLLM (Host)     | [3] Qwen 3.6 35B vLLM (Container)
#   [4] Qwen 3.8 Flash Next (Host)   | [5] Qwen 3.8 Flash Next (Container)
#   [6] Qwen 3.6 35B test (Container)
#   [7] Gemini 3.8 Flash (Host)      | [8] Gemini 3.8 Flash (Container)
#   [9] Laguna S 2.1 vLLM (Host)     | [10] Laguna S 2.1 vLLM (Container)
ACTIVE_BRAIN_PROFILE = 4
ACTIVE_CODER_PROFILE = 5  # must be reachable from Podman container
ACTIVE_SUMMARIZER_PROFILE = 5  # can be the same as coder, or a cheaper fast model
ACTIVE_ADVISER_PROFILE = 5
ACTIVE_ANALYST_PROFILE = 5  # point this to your vision model
ACTIVE_ARCHITECT_PROFILE = 5

# --- EMBEDDING CONFIGURATION ---
# Hardcoded to prevent dimension mismatch in the vector database.
EMBEDDING_CONFIG = {
    "base_url": "http://host.containers.internal:64165/v1",  # Point to Ollama/vLLM
    "api_key": "Ollama",
    "model": "qwen3-embedding:8b-q8_0",  # high-end 4096-dimension model
    "dimensions": 4096,  # The Brain needs to know this for the SQL schema!
    "timeout": 120.0,
}

# --- AUXILIARY LLM SANDBOX & ENDPOINT PRESETS ---
# Default endpoint used by the Auxiliary LLM tool when 'endpoint' is omitted by the Brain.
DEFAULT_AUXILIARY_ENDPOINT = "ollama"

# Pre-configured zero-trust endpoints reachable from within the container.
# Real cloud API keys live safely on the host proxy (LiteLLM) or local Ollama.
AUXILIARY_ENDPOINTS = {
    "ollama": {
        "url": "http://host.containers.internal:64165/v1",
        "default_api_key": "Ollama",
        "description": "Dedicated local Ollama server (port 64165)",
    },
    "litellm_proxy": {
        "url": "http://host.containers.internal:4000/v1",
        "default_api_key": "sk-sandbox-fake-key",
        "description": "Host LiteLLM zero-trust proxy gateway (port 4000 from container)",
    },
}

# --- DECIDER (DECISION MODEL) CONFIGURATION ---
# Dedicated endpoint for non-autoregressive decision models (e.g. clef-flash) via Ollama /v1/systemone
DECIDER_CONFIG = {
    "name": "Clef Flash 9B", #"Clef 27B"
    "base_url": "http://host.containers.internal:64165",  # Ollama base endpoint
    "model": "clef-flash:9b", #"clef:27b"
    "timeout": 60.0,
}


# --- LLM PARAMETERS ---
LLM_PROFILES = [
    # [0] Configured Default Model - WSL2 Host
    {
        "name": "default-llm",
        "base_url": "http://localhost:4000/v1",
        "api_key": "sk-sandbox-fake-key",
        "model": "default-llm",
        "api_params": {
            "temperature": 0.7,
            "top_p": 0.95,
            "timeout": 300.0,
            "max_tokens": 16384,
            "extra_body": {
                "chat_template_kwargs": {"enable_thinking": True}
            },
        },
    },
    # [1] Configured Default Model - Podman Container
    {
        "name": "default-llm",
        "base_url": "http://host.containers.internal:4000/v1",
        "api_key": "sk-sandbox-fake-key",
        "model": "default-llm",
        "api_params": {
            "temperature": 0.7,
            "top_p": 0.95,
            "timeout": 300.0,
            "max_tokens": 16384,
            "extra_body": {
                "chat_template_kwargs": {"enable_thinking": True}
            },
        },
    },
    # [2] Local Model - vLLM - from WSL2
    {
        "name": "Qwen3.6 35B-A3B - vLLM",
        "base_url": "http://localhost:4000/v1",
        "api_key": "sk-sandbox-fake-key",
        "model": "Qwen/Qwen3.6-35B-A3B-FP8",
        "api_params": {
            "temperature": 1.0,
            "top_p": 0.95,
            "presence_penalty": 0.0,
            "frequency_penalty": 0.0,
            "timeout": 1800.0,
            "max_tokens": 65536,
            "extra_body": {
                "top_k": 20,
                "min_p": 0.0,
                "repetition_penalty": 1.0,
                "mm_processor_kwargs": {"fps": 1, "max_frames": 1200, "do_sample_frames": True},
                "chat_template_kwargs": {"enable_thinking": True},
            },
            "seed": None,
        },
    },
    # [3] Local Model - vLLM - from Podman
    {
        "name": "Qwen3.6 35B-A3B - vLLM",
        "base_url": "http://host.containers.internal:4000/v1",
        "api_key": "sk-sandbox-fake-key",
        "model": "Qwen/Qwen3.6-35B-A3B-FP8",
        "api_params": {
            "temperature": 1.0,
            "top_p": 0.95,
            "presence_penalty": 0.0,
            "frequency_penalty": 0.0,
            "timeout": 1800.0,
            "max_tokens": 65536,
            "extra_body": {
                "top_k": 20,
                "min_p": 0.0,
                "repetition_penalty": 1.0,
                "mm_processor_kwargs": {"fps": 1, "max_frames": 1200, "do_sample_frames": True},
                "chat_template_kwargs": {"enable_thinking": True},
            },
            "seed": None,
        },
    },
    # [4] Secondary Remote Server - from WSL2
    {
        "name": "Qwen 3.8 Flash Next",
        "base_url": "http://localhost:4000/v1",
        "api_key": "sk-sandbox-fake-key",
        "model": "Qwen3.8-Flash-Next-FP8",
        "api_params": {
            "temperature": 1.0, # 1.0 for thinking, 0.7 non-reasoning
            "top_p": 0.95, # 0.95 for thinking, 0.8 non-reasoning
            "reasoning_effort": "low",
            "max_tokens": 65536,
            "presence_penalty": 0.0, # 0.0 for thinking, 1.5 non-reasoning
            "timeout": 600.0,
            "extra_body": {
                "top_k": 20,
                "min_p": 0.0,
                "repetition_penalty": 1.00,
                "chat_template_kwargs": {"enable_thinking": True},
            },
            "seed": None,
        },
    },
    # [5] Secondary Remote Server - from Podman
    {
        "name": "Qwen 3.8 Flash Next",
        "base_url": "http://host.containers.internal:4000/v1",
        "api_key": "sk-sandbox-fake-key",
        "model": "Qwen3.8-Flash-Next-FP8",
        "api_params": {
            "temperature": 1.0, # 1.0 for thinking, 0.7 non-reasoning
            "top_p": 0.95, # 0.95 for thinking, 0.8 non-reasoning
            "reasoning_effort": "low",
            "max_tokens": 65536,
            "presence_penalty": 0.0, # 0.0 for thinking, 1.5 non-reasoning
            "timeout": 600.0,
            "extra_body": {
                "top_k": 20,
                "min_p": 0.0,
                "repetition_penalty": 1.00,
                "chat_template_kwargs": {"enable_thinking": True},
            },
            "seed": None,
        },
    },
    # [6] Local Model - vLLM - from Podman - testing LLM settings like small context window
    {
        "name": "Qwen3.6 35B - vLLM",
        "base_url": "http://host.containers.internal:4000/v1",
        "api_key": "sk-sandbox-fake-key",
        "model": "Qwen/Qwen3.6-35B-A3B-FP8",
        "api_params": {
            "temperature": 0.2,
            "top_p": 0.2,
            "presence_penalty": 0.0,
            "frequency_penalty": 0.0,
            "timeout": 180.0,
            "max_tokens": 16384,
            "extra_body": {
                "top_k": 20,
                "min_p": 0.0,
                "repetition_penalty": 1.05,
                "mm_processor_kwargs": {"fps": 1, "max_frames": 1200, "do_sample_frames": True},
                "chat_template_kwargs": {"enable_thinking": True},
            },
            "seed": None,
        },
    },
    # [7] OpenRouter - example of Gemini 3.8 Flash - from WSL2
    {
        "name": "Gemini 3.8 Flash",
        "base_url": "http://localhost:4000/v1",
        "api_key": "sk-sandbox-fake-key",
        "model": "google/gemini-3.8-flash",
        "api_params": {
            "temperature": 1,
            "top_p": 0.95,
            "presence_penalty": 0.0,
            "frequency_penalty": 0.0,
            "timeout": 180.0,
            "max_tokens": 16384,
        },
    },
    # [8] OpenRouter - example of Gemini 3.8 Flash - from Podman
    {
        "name": "Gemini 3.8 Flash",
        "base_url": "http://host.containers.internal:4000/v1",
        "api_key": "sk-sandbox-fake-key",
        "model": "google/gemini-3.8-flash",
        "api_params": {
            "temperature": 1,
            "top_p": 0.95,
            "presence_penalty": 0.0,
            "frequency_penalty": 0.0,
            "timeout": 180.0,
            "max_tokens": 16384,
        },
    },
    # [9] Local Model - vLLM - from WSL2
    {
        "name": "Laguna S 2.1 - vLLM",
        "base_url": "http://localhost:4000/v1",
        "api_key": "sk-sandbox-fake-key",
        "model": "poolside/Laguna-S-2.1-NVFP4",
        "api_params": {
            "temperature": 0.7,
            "top_p": 0.95,
            "timeout": 600.0,
            "max_tokens": 65536,
            "seed": None,
        },
    },
    # [10] Local Model - vLLM - from Podman
    {
        "name": "Laguna S 2.1 - vLLM",
        "base_url": "http://host.containers.internal:4000/v1",
        "api_key": "sk-sandbox-fake-key",
        "model": "poolside/Laguna-S-2.1-NVFP4",
        "api_params": {
            "temperature": 0.7,
            "top_p": 0.95,
            "timeout": 600.0,
            "max_tokens": 65536,
            "seed": None,
        },
    },
]
