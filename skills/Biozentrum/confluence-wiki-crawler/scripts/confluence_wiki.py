#!/usr/bin/env python3
"""
Confluence Wiki — Guarded Crawler CLI & Client Library
Target Instance: Configured via CONFLUENCE_BASE_URL in access_settings.env

SECURITY & ARCHITECTURAL GUARD (`confluence-wiki-crawler`):
This script enforces a strict least-privilege policy in `GuardedCrawlerConfluenceClient`:
  1. SPACE PERMISSION TIERS & PROGRESSIVE INITIAL SEARCH SCOPE (`access_settings.env`):
     - `CONFLUENCE_ELEVATED_ACCESS_SPACES`: Comma-separated list of space keys where the crawler has
       ELEVATED ACCESS (read-only pages + guarded blogpost/comment/like writes below).
       (Backward-compatible alias: `CONFLUENCE_DEFAULT_ACCESS_SPACES`.)
     - `CONFLUENCE_READONLY_SPACES`: Comma-separated list of space keys where ONLY read/search/crawl
       operations are permitted; ALL writes (blogposts, comments, replies, likes/unlikes, edits, deletes)
       are strictly blocked by the script. (Takes precedence if a space is listed in both variables.)
     - `CONFLUENCE_DEFAULT_PAGE`: Optional comma-separated starting page(s) (URL, `/x/...` tiny link, or
       numeric page ID). Used by `get-page --default`, `crawl --default`, and tried FIRST (page + subtree)
       on initial `search` queries to minimize server load before widening. If left blank, falls back to
       the homepage of the first space in `CONFLUENCE_DEFAULT_SPACES` / `CONFLUENCE_ELEVATED_ACCESS_SPACES`.
     - `CONFLUENCE_DEFAULT_SPACES`: Optional comma-separated list of default space keys for initial searches
       when no `--space` or `--all-spaces` (`--wider`) flag is passed. Defaults to `CONFLUENCE_ELEVATED_ACCESS_SPACES`.
     - Any space not listed in `CONFLUENCE_ELEVATED_ACCESS_SPACES` or `CONFLUENCE_READONLY_SPACES` is blocked
       for reading and writing page content, while `spaces` lists all server-visible spaces (annotated with
       `access_mode`) so the agent can advise the user which space key to add to `access_settings.env`
       (or request `View` access for) when needed.
  2. All HTTP GET requests within readable spaces (reading pages/blogposts, CQL search, recent edits,
     history, version diffs, crawling, attachment listing/downloading, PDF export) are permitted.
     Pages (`type == "page"`) and attachments (`type == "attachment"`) are STRICTLY READ-ONLY
     and can NEVER be created, edited, or deleted.
  3. Creating a new Blog Post (`POST /rest/api/content` with `type == "blogpost"`) is permitted
     ONLY in spaces listed in `CONFLUENCE_ELEVATED_ACCESS_SPACES`.
  4. GUARDED OWN BLOG POST EDIT / DELETE (`PUT` or `DELETE /rest/api/content/{blogpost_id}`):
     Permitted ONLY in `CONFLUENCE_ELEVATED_ACCESS_SPACES` when a mandatory programmatic server-side
     pre-flight GET confirms BOTH:
       (a) The target content ID is strictly a blog post (`type == "blogpost"`, NEVER a page), AND
       (b) The blog post is OWNED/CREATED by the crawler (`history.createdBy` matches
           `CONFLUENCE_CRAWLER_USERNAME` and `CONFLUENCE_CRAWLER_NAME` in `access_settings.env`).
  5. Adding a new Comment or Threaded Reply to a Page (`POST /rest/api/content` with
     `type == "comment"` and `container.type == "page"`) is permitted ONLY in `CONFLUENCE_ELEVATED_ACCESS_SPACES`.
  6. GUARDED OWN COMMENT EDIT (`PUT /rest/api/content/{comment_id}`):
     Permitted ONLY in `CONFLUENCE_ELEVATED_ACCESS_SPACES` when a mandatory programmatic server-side
     pre-flight GET confirms:
       (a) The target content ID is strictly a comment (`type == "comment"`, NEVER a page/blogpost), AND
       (b) The comment is OWNED/CREATED by the crawler (`history.createdBy` matches the
           crawler's identity in `access_settings.env`). (Confluence Server requires Space Admin
           to edit another user's comment; to respond/mark another user's comment as DONE, reply to it,
           like it, or delete it.)
  7. GUARDED COMMENT DELETE (`DELETE /rest/api/content/{comment_id}`):
     Permitted ONLY in `CONFLUENCE_ELEVATED_ACCESS_SPACES` when a mandatory programmatic server-side
     pre-flight GET confirms:
       (a) The target content ID is strictly a comment (`type == "comment"`, NEVER a page/blogpost), AND
       (b) EITHER the comment is OWNED/CREATED by the crawler OR the comment's body (`body.view` or
           `body.storage`) mentions the crawler (`CONFLUENCE_CRAWLER_NAME` / `CONFLUENCE_CRAWLER_USERNAME`).
  8. LIKES (`POST` or `DELETE /rest/likes/1.0/content/{id}/likes`):
     Permitted ONLY in `CONFLUENCE_ELEVATED_ACCESS_SPACES` so the crawler can "Like" a comment
     (e.g., to mark a user's `@mention` instruction comment as DONE without deleting it) or like a page/blogpost.
  9. All other write/mutation operations raise a `PermissionError` before any network call is made.
"""

import argparse
import base64
import difflib
import fnmatch
import html
import json
import os
import re
import struct
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

try:
    from bs4 import BeautifulSoup, NavigableString
    HAS_BS4 = True
except ImportError:
    BeautifulSoup = None  # type: ignore[assignment]
    NavigableString = None  # type: ignore[assignment]
    HAS_BS4 = False

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
DEFAULT_SETTINGS_FILE = SKILL_DIR / "access_settings.env"
EXAMPLE_SETTINGS_FILE = SKILL_DIR / "access_settings.env.example"
LEGACY_KEY_FILE = SKILL_DIR / "confluence_api_key.env"
DEFAULT_KEY_FILE = DEFAULT_SETTINGS_FILE

SETTINGS_FILE_ENV_VAR = "CONFLUENCE_SETTINGS_FILE"
BASE_URL_ENV_VAR = "CONFLUENCE_BASE_URL"
ENV_VAR_NAME = "CONFLUENCE_CRAWLER_API_KEY"
CRAWLER_USERNAME_ENV_VAR = "CONFLUENCE_CRAWLER_USERNAME"
CRAWLER_NAME_ENV_VAR = "CONFLUENCE_CRAWLER_NAME"
READONLY_SPACES_ENV_VAR = "CONFLUENCE_READONLY_SPACES"
ELEVATED_ACCESS_SPACES_ENV_VAR = "CONFLUENCE_ELEVATED_ACCESS_SPACES"
DEFAULT_ACCESS_SPACES_ENV_VAR = "CONFLUENCE_DEFAULT_ACCESS_SPACES"
DEFAULT_SPACES_ENV_VAR = "CONFLUENCE_DEFAULT_SPACES"
DEFAULT_PAGE_ENV_VAR = "CONFLUENCE_DEFAULT_PAGE"

PLACEHOLDER_BASE_URLS = {
    "PASTE_YOUR_CONFLUENCE_BASE_URL_HERE",
    "YOUR_CONFLUENCE_BASE_URL_HERE",
    "",
}
PLACEHOLDER_TOKENS = {
    "PASTE_YOUR_CRAWLER_CONFLUENCE_PAT_HERE",
    "PASTE_YOUR_CONFLUENCE_PAT_HERE",
    "YOUR_TOKEN_HERE",
    "",
}
PLACEHOLDER_CRAWLER_NAMES = {
    "PASTE_YOUR_CRAWLER_NAME_HERE",
    "PASTE_YOUR_CRAWLER_DISPLAY_NAME_HERE",
    "PASTE_YOUR_CRAWLER_USERNAME_HERE",
    "YOUR_CRAWLER_NAME_HERE",
    "YOUR_CRAWLER_USERNAME_HERE",
    "",
}
PLACEHOLDER_SPACE_LISTS = {
    "PASTE_COMMA_SEPARATED_READONLY_SPACE_KEYS_HERE",
    "PASTE_COMMA_SEPARATED_ELEVATED_SPACE_KEYS_HERE",
    "PASTE_COMMA_SEPARATED_DEFAULT_ACCESS_SPACE_KEYS_HERE",
    "PASTE_COMMA_SEPARATED_SPACE_KEYS_HERE",
    "PASTE_YOUR_ALLOWED_SPACES_HERE",
    "YOUR_READONLY_SPACES_HERE",
    "YOUR_ELEVATED_ACCESS_SPACES_HERE",
    "YOUR_DEFAULT_ACCESS_SPACES_HERE",
}
PLACEHOLDER_DEFAULT_PAGES = {
    "PASTE_OPTIONAL_DEFAULT_PAGE_URL_OR_ID_HERE",
    "PASTE_YOUR_DEFAULT_PAGE_HERE",
    "YOUR_DEFAULT_PAGE_HERE",
    "",
}


# =============================================================================
# 1. SETTINGS, TOKEN, IDENTITY & SPACE POLICY DISCOVERY (ISOLATED TO THIS SKILL)
# =============================================================================

def _resolve_settings_file(explicit_settings_file: Optional[str] = None) -> Path:
    """Resolves `access_settings.env` (via CLI flag, `CONFLUENCE_SETTINGS_FILE` env var, or skill directory)."""
    if explicit_settings_file:
        return Path(explicit_settings_file)
    env_file = os.environ.get(SETTINGS_FILE_ENV_VAR, "").strip()
    if env_file:
        return Path(env_file)
    if DEFAULT_SETTINGS_FILE.is_file():
        return DEFAULT_SETTINGS_FILE
    if LEGACY_KEY_FILE.is_file():
        return LEGACY_KEY_FILE
    return DEFAULT_SETTINGS_FILE


def _parse_env_file_dict(path: Path) -> Dict[str, str]:
    """Parses KEY=VALUE pairs (and optional bare token lines) from access_settings.env."""
    kv: Dict[str, str] = {}
    if not path.is_file():
        return kv
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "=" in s and not s.startswith("http"):
            k, v = s.split("=", 1)
            kv[k.strip().upper()] = v.strip().strip("\"'")
        else:
            kv.setdefault("_BARE_LINE", s)
    return kv


def load_base_url(
    explicit_settings_file: Optional[str] = None,
    explicit_base_url: Optional[str] = None,
) -> Tuple[str, str]:
    """
    Loads the Confluence Base URL (`CONFLUENCE_BASE_URL`) from `access_settings.env`
    (or `--base-url` / `os.environ["CONFLUENCE_BASE_URL"]`).
    Returns (normalized_base_url, source).
    """
    if explicit_base_url and explicit_base_url.strip() not in PLACEHOLDER_BASE_URLS:
        return explicit_base_url.strip().rstrip("/"), "cli:--base-url"

    target_path = _resolve_settings_file(explicit_settings_file)
    if target_path.is_file():
        kv = _parse_env_file_dict(target_path)
        for key_name in ("CONFLUENCE_BASE_URL", "CONFLUENCE_URL", "BASE_URL"):
            raw_val = kv.get(key_name, "").strip()
            if raw_val and raw_val not in PLACEHOLDER_BASE_URLS:
                return raw_val.rstrip("/"), str(target_path)

    env_val = os.environ.get(BASE_URL_ENV_VAR, "").strip()
    if env_val and env_val not in PLACEHOLDER_BASE_URLS:
        return env_val.rstrip("/"), f"env:{BASE_URL_ENV_VAR}"

    raise RuntimeError(
        f"No valid Confluence Base URL found. Please set {BASE_URL_ENV_VAR} "
        f"(e.g. CONFLUENCE_BASE_URL=https://wiki.example.org) inside '{target_path}' "
        f"(or pass --base-url / set system environment variable '{BASE_URL_ENV_VAR}')."
    )


def load_api_token(explicit_token_file: Optional[str] = None) -> Tuple[str, str, str]:
    """
    Locates and parses the Crawler Confluence API token strictly from this skill's own
    `access_settings.env` file, falling back only to `CONFLUENCE_CRAWLER_API_KEY` in os.environ.
    Never reads from other skill folders or shared folders.
    """
    target_path = _resolve_settings_file(explicit_token_file)
    if target_path.is_file():
        kv = _parse_env_file_dict(target_path)
        for key_name in (
            "CONFLUENCE_CRAWLER_API_KEY",
            "CONFLUENCE_API_KEY",
            "CONFLUENCE_TOKEN",
            "CONFLUENCE_PAT",
            "API_KEY",
            "TOKEN",
            "_BARE_LINE",
        ):
            raw_val = kv.get(key_name, "").strip()
            if raw_val.lower().startswith("bearer "):
                raw_val = raw_val[7:].strip()
            if raw_val and raw_val not in PLACEHOLDER_TOKENS:
                auth_type, cred = _classify_credential(raw_val)
                return auth_type, cred, str(target_path)

    env_val = os.environ.get(ENV_VAR_NAME, "").strip()
    if env_val.lower().startswith("bearer "):
        env_val = env_val[7:].strip()
    if env_val and env_val not in PLACEHOLDER_TOKENS:
        auth_type, cred = _classify_credential(env_val)
        return auth_type, cred, f"env:{ENV_VAR_NAME}"

    raise RuntimeError(
        f"No valid Crawler Confluence API key found. Please set {ENV_VAR_NAME} "
        f"inside '{target_path}' (or in system environment variable '{ENV_VAR_NAME}')."
    )


def load_crawler_identity(explicit_token_file: Optional[str] = None) -> Dict[str, Any]:
    """
    Loads the crawler's identity from `access_settings.env` (or backup environment variables):
      - `CONFLUENCE_CRAWLER_USERNAME`: short Confluence account username (e.g. what appears in
        `history.createdBy.username` or `/display/~<username>`)
      - `CONFLUENCE_CRAWLER_NAME`: full Confluence displayName (e.g. what appears in
        `history.createdBy.displayName` and rendered `@mentions`)
    Returns a dict with:
      - `username`: cleaned short username (without leading `~` or `@`)
      - `display_name`: primary display name
      - `all_names`: list of all configured mention/identity tokens
      - `source`: configuration source string
    """
    target_path = _resolve_settings_file(explicit_token_file)
    raw_username = ""
    raw_display_name = ""
    sources: List[str] = []

    if target_path.is_file():
        kv = _parse_env_file_dict(target_path)
        for uk in ("CONFLUENCE_CRAWLER_USERNAME", "CRAWLER_USERNAME"):
            val = kv.get(uk, "").strip()
            if val and val not in PLACEHOLDER_CRAWLER_NAMES:
                raw_username = val
                sources.append(str(target_path))
                break
        for nk in ("CONFLUENCE_CRAWLER_NAME", "CONFLUENCE_CRAWLER_DISPLAY_NAME", "CRAWLER_NAME", "AGENT_NAME"):
            val = kv.get(nk, "").strip()
            if val and val not in PLACEHOLDER_CRAWLER_NAMES:
                raw_display_name = val
                if str(target_path) not in sources:
                    sources.append(str(target_path))
                break

    if not raw_username:
        env_u = os.environ.get(CRAWLER_USERNAME_ENV_VAR, "").strip()
        if env_u and env_u not in PLACEHOLDER_CRAWLER_NAMES:
            raw_username = env_u
            sources.append(f"env:{CRAWLER_USERNAME_ENV_VAR}")

    if not raw_display_name:
        env_n = os.environ.get(CRAWLER_NAME_ENV_VAR, "").strip()
        if env_n and env_n not in PLACEHOLDER_CRAWLER_NAMES:
            raw_display_name = env_n
            sources.append(f"env:{CRAWLER_NAME_ENV_VAR}")

    name_tokens = [n.strip().lstrip("@") for n in raw_display_name.split(",") if n.strip().lstrip("@")]

    # If CONFLUENCE_CRAWLER_USERNAME wasn't set separately, check if a ~username token was included in CONFLUENCE_CRAWLER_NAME
    clean_username = raw_username.strip().lstrip("~@")
    primary_display_name = ""
    for tok in name_tokens:
        if tok.startswith("~") and not clean_username:
            clean_username = tok.lstrip("~@").strip()
        elif not primary_display_name and not tok.startswith("~"):
            primary_display_name = tok

    all_names: List[str] = []
    for tok in name_tokens:
        if tok not in all_names:
            all_names.append(tok)
    if clean_username:
        if clean_username not in all_names:
            all_names.append(clean_username)
        tilde_form = f"~{clean_username}"
        if tilde_form not in all_names:
            all_names.append(tilde_form)

    return {
        "username": clean_username,
        "display_name": primary_display_name,
        "all_names": all_names,
        "source": ", ".join(sources),
    }


def load_crawler_names(explicit_token_file: Optional[str] = None) -> Tuple[List[str], str]:
    """
    Returns (all_crawler_names, source) from `load_crawler_identity` for mention & ownership checks.
    """
    ident = load_crawler_identity(explicit_token_file)
    return ident["all_names"], ident["source"]


def _parse_space_list(raw_str: str) -> Tuple[List[str], bool]:
    """
    Parses a comma-separated list of Confluence space keys.
    Returns (normalized_upper_space_keys, has_wildcard).
    """
    if not raw_str or raw_str.strip() in PLACEHOLDER_SPACE_LISTS:
        return [], False
    spaces: List[str] = []
    wildcard = False
    for part in raw_str.split(","):
        tok = part.strip().strip("\"'")
        if not tok or tok in PLACEHOLDER_SPACE_LISTS:
            continue
        if tok in ("*", "ALL", "all"):
            wildcard = True
        else:
            up = tok.upper()
            if up not in spaces:
                spaces.append(up)
    return spaces, wildcard


def _parse_default_page_list(raw_str: str) -> List[str]:
    if not raw_str or raw_str.strip() in PLACEHOLDER_DEFAULT_PAGES:
        return []
    items: List[str] = []
    for part in raw_str.split(","):
        tok = part.strip().strip("\"'")
        if tok and tok not in PLACEHOLDER_DEFAULT_PAGES and tok not in items:
            items.append(tok)
    return items


def load_space_access_policy(explicit_settings_file: Optional[str] = None) -> Dict[str, Any]:
    """
    Loads the space permission tiers and initial search/starting scope from
    `access_settings.env` (or environment variables):
      - `CONFLUENCE_ELEVATED_ACCESS_SPACES` (aliases: `CONFLUENCE_DEFAULT_ACCESS_SPACES`,
        `CONFLUENCE_ALLOWED_SPACES`): comma-separated space keys where elevated guarded crawler
        access is permitted (read-only pages + guarded blogpost/comment/like writes).
      - `CONFLUENCE_READONLY_SPACES`: comma-separated space keys where ONLY read/search/crawl
        is permitted (all writes/blogposts/comments/likes are blocked by the script).
      - `CONFLUENCE_DEFAULT_PAGE` (alias: `CONFLUENCE_DEFAULT_PAGES`): optional comma-separated
        starting page(s) (numeric page ID, `/x/...` tiny link, or full URL) tried first on
        initial searches and used by `get-page --default` / `crawl --default`.
      - `CONFLUENCE_DEFAULT_SPACES` (alias: `CONFLUENCE_DEFAULT_SEARCH_SPACES`): optional
        comma-separated space keys for initial searches (defaults to `CONFLUENCE_ELEVATED_ACCESS_SPACES`).
    Precedence rule:
      If a space key appears in both `CONFLUENCE_READONLY_SPACES` and `CONFLUENCE_ELEVATED_ACCESS_SPACES`,
      `CONFLUENCE_READONLY_SPACES` (the stricter policy) wins and the space is strictly read-only.
    """
    target_path = _resolve_settings_file(explicit_settings_file)
    raw_ro = ""
    raw_rw = ""
    raw_def_spaces = ""
    raw_default_page = ""
    default_page_source = ""
    sources: List[str] = []

    if target_path.is_file():
        kv = _parse_env_file_dict(target_path)
        for rk in ("CONFLUENCE_READONLY_SPACES", "READONLY_SPACES"):
            val = kv.get(rk, "").strip()
            if val and val not in PLACEHOLDER_SPACE_LISTS:
                raw_ro = val
                sources.append(str(target_path))
                break
        for ek in (
            "CONFLUENCE_ELEVATED_ACCESS_SPACES",
            "ELEVATED_ACCESS_SPACES",
            "CONFLUENCE_DEFAULT_ACCESS_SPACES",
            "CONFLUENCE_ALLOWED_SPACES",
            "DEFAULT_ACCESS_SPACES",
            "ALLOWED_SPACES",
        ):
            val = kv.get(ek, "").strip()
            if val and val not in PLACEHOLDER_SPACE_LISTS:
                raw_rw = val
                if str(target_path) not in sources:
                    sources.append(str(target_path))
                break
        for dsk in (
            "CONFLUENCE_DEFAULT_SPACES",
            "CONFLUENCE_DEFAULT_SEARCH_SPACES",
            "DEFAULT_SPACES",
            "DEFAULT_SEARCH_SPACES",
        ):
            val = kv.get(dsk, "").strip()
            if val and val not in PLACEHOLDER_SPACE_LISTS:
                raw_def_spaces = val
                break
        for pk in (
            "CONFLUENCE_DEFAULT_PAGE",
            "CONFLUENCE_DEFAULT_PAGES",
            "DEFAULT_PAGE",
            "DEFAULT_PAGES",
            "CONFLUENCE_START_PAGE",
        ):
            val = kv.get(pk, "").strip()
            if val and val not in PLACEHOLDER_DEFAULT_PAGES:
                raw_default_page = val
                default_page_source = str(target_path)
                break

    if not raw_ro:
        env_ro = os.environ.get(READONLY_SPACES_ENV_VAR, "").strip()
        if env_ro and env_ro not in PLACEHOLDER_SPACE_LISTS:
            raw_ro = env_ro
            sources.append(f"env:{READONLY_SPACES_ENV_VAR}")

    if not raw_rw:
        for env_k in (ELEVATED_ACCESS_SPACES_ENV_VAR, DEFAULT_ACCESS_SPACES_ENV_VAR, "CONFLUENCE_ALLOWED_SPACES"):
            env_rw = os.environ.get(env_k, "").strip()
            if env_rw and env_rw not in PLACEHOLDER_SPACE_LISTS:
                raw_rw = env_rw
                sources.append(f"env:{env_k}")
                break

    if not raw_def_spaces:
        for env_dsk in (DEFAULT_SPACES_ENV_VAR, "CONFLUENCE_DEFAULT_SEARCH_SPACES"):
            env_ds = os.environ.get(env_dsk, "").strip()
            if env_ds and env_ds not in PLACEHOLDER_SPACE_LISTS:
                raw_def_spaces = env_ds
                break

    if not raw_default_page:
        for env_pk in (DEFAULT_PAGE_ENV_VAR, "CONFLUENCE_DEFAULT_PAGES"):
            env_dp = os.environ.get(env_pk, "").strip()
            if env_dp and env_dp not in PLACEHOLDER_DEFAULT_PAGES:
                raw_default_page = env_dp
                default_page_source = f"env:{env_pk}"
                break

    ro_spaces, ro_wildcard = _parse_space_list(raw_ro)
    rw_spaces_raw, rw_wildcard = _parse_space_list(raw_rw)
    # Explicit read-only spaces take precedence over explicit elevated-access spaces
    rw_spaces = [s for s in rw_spaces_raw if s not in ro_spaces]

    explicit_def_spaces, _ = _parse_space_list(raw_def_spaces)
    if explicit_def_spaces:
        default_spaces = explicit_def_spaces
    elif rw_spaces:
        default_spaces = list(rw_spaces)
    else:
        default_spaces = list(ro_spaces)

    default_pages = _parse_default_page_list(raw_default_page)

    configured = bool(ro_spaces or rw_spaces or ro_wildcard or rw_wildcard)
    return {
        "readonly_spaces": ro_spaces,
        "readonly_wildcard": ro_wildcard,
        "elevated_access_spaces": rw_spaces,
        "elevated_access_wildcard": rw_wildcard,
        "default_access_spaces": rw_spaces,
        "default_access_wildcard": rw_wildcard,
        "default_spaces": default_spaces,
        "explicit_default_spaces": explicit_def_spaces,
        "default_page": ", ".join(default_pages),
        "default_pages": default_pages,
        "default_page_source": default_page_source,
        "configured": configured,
        "source": ", ".join(sources),
    }


def _classify_credential(raw_val: str) -> Tuple[str, str]:
    """Determines whether a credential string is a Bearer PAT or Basic Auth (user:pass)."""
    if raw_val.lower().startswith("basic "):
        return "basic", raw_val[6:].strip()
    if ":" in raw_val and not raw_val.startswith("http"):
        b64 = base64.b64encode(raw_val.encode("utf-8")).decode("ascii")
        return "basic", b64
    return "bearer", raw_val


# =============================================================================
# 2. TINY LINK (/x/...) & URL RESOLUTION UTILITIES
# =============================================================================

def decode_tiny_link(tiny_code: str) -> int:
    clean_code = tiny_code.strip().split("/")[-1].split("?")[0]
    standard_b64 = clean_code.replace("-", "/").replace("_", "+")
    padded_b64 = (standard_b64 + "A" * 11)[:11] + "="
    decoded_bytes = base64.b64decode(padded_b64)
    if len(decoded_bytes) <= 4:
        return struct.unpack("<I", decoded_bytes.ljust(4, b"\x00"))[0]
    return struct.unpack("<Q", decoded_bytes.ljust(8, b"\x00"))[0]


def encode_tiny_link(page_id: int) -> str:
    try:
        raw_bytes = struct.pack("<I", int(page_id))
    except struct.error:
        raw_bytes = struct.pack("<Q", int(page_id))
    b64_encoded = base64.b64encode(raw_bytes).decode("ascii")
    return b64_encoded.rstrip("=").replace("/", "-").replace("+", "_").rstrip("A") or "A"


# =============================================================================
# 3. STORAGE XHTML <-> MARKDOWN CONVERTERS & MENTION MATCHING
# =============================================================================

def _storage_to_markdown_regex(xhtml_or_html: str) -> str:
    """Stdlib-only fallback converter when BeautifulSoup4 is not installed."""
    text = xhtml_or_html
    text = re.sub(
        r"<ac:structured-macro[^>]*ac:name=[\"'](?:code|noformat)[\"'][^>]*>.*?<ac:plain-text-body><!\[CDATA\[(.*?)\]\]></ac:plain-text-body>.*?</ac:structured-macro>",
        lambda m: f"\n```\n{m.group(1).strip()}\n```\n",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    for lvl in range(6, 0, -1):
        text = re.sub(
            rf"<h{lvl}[^>]*>(.*?)</h{lvl}>",
            lambda m, l=lvl: f"\n{'#' * l} {re.sub(r'<[^>]+>', '', m.group(1)).strip()}\n",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
    text = re.sub(
        r"<ac:task>\s*(?:<ac:task-id>.*?</ac:task-id>\s*)?(?:<ac:task-uuid>.*?</ac:task-uuid>\s*)?<ac:task-status>(.*?)</ac:task-status>\s*<ac:task-body>(.*?)</ac:task-body>\s*</ac:task>",
        lambda m: f"\n- [{'x' if m.group(1).strip().lower() == 'complete' else ' '}] {re.sub(r'<[^>]+>', ' ', m.group(2)).strip()}\n",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(r"<li[^>]*>(.*?)</li>", lambda m: f"\n- {re.sub(r'<[^>]+>', ' ', m.group(1)).strip()}", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<strong[^>]*>(.*?)</strong>|<b[^>]*>(.*?)</b>", lambda m: f"**{m.group(1) or m.group(2)}**", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<em[^>]*>(.*?)</em>|<i[^>]*>(.*?)</i>", lambda m: f"*{m.group(1) or m.group(2)}*", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<code[^>]*>(.*?)</code>", lambda m: f"`{m.group(1)}`", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<a[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", lambda m: f"[{re.sub(r'<[^>]+>', '', m.group(2)).strip()}]({m.group(1)})", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<(?:br|p|/p|/div|/tr)[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<(?:td|th)[^>]*>", " | ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", html.unescape(text)).strip()


def storage_to_markdown(xhtml_or_html: str) -> str:
    """
    Converts Confluence XHTML storage format (`body.storage`) or rendered HTML (`body.view`)
    into clean, readable Markdown while preserving Confluence macros (code, info, note, warning,
    expand, status lozenges, attachments, internal links, task checkboxes, and tables).
    """
    if not xhtml_or_html or not xhtml_or_html.strip():
        return ""
    if not HAS_BS4:
        return _storage_to_markdown_regex(xhtml_or_html)

    soup = BeautifulSoup(xhtml_or_html, "html.parser")

    for macro in soup.find_all(re.compile(r"^(ac:structured-macro|structured-macro)$")):
        macro_name = (macro.get("ac:name") or macro.get("name") or "macro").lower()
        params = {}
        for p in macro.find_all(re.compile(r"^(ac:parameter|parameter)$")):
            p_name = p.get("ac:name") or p.get("name") or ""
            params[p_name] = p.get_text(strip=True)

        plain_body_tag = macro.find(re.compile(r"^(ac:plain-text-body|plain-text-body)$"))
        rich_body_tag = macro.find(re.compile(r"^(ac:rich-text-body|rich-text-body)$"))

        if macro_name in ("code", "noformat"):
            lang = params.get("language", "")
            title = params.get("title", "")
            code_text = plain_body_tag.get_text() if plain_body_tag else macro.get_text()
            header = f"**Code ({title}):**\n" if title else ""
            replacement = f"\n{header}```{lang}\n{code_text.strip()}\n```\n"
            macro.replace_with(NavigableString(replacement))
        elif macro_name in ("info", "note", "warning", "tip", "panel"):
            title = params.get("title", macro_name.upper())
            inner_md = storage_to_markdown(str(rich_body_tag)) if rich_body_tag else macro.get_text(strip=True)
            quoted = "\n".join(f"> {line}" for line in f"**[{title}]** {inner_md.strip()}".splitlines())
            macro.replace_with(NavigableString(f"\n{quoted}\n"))
        elif macro_name == "expand":
            title = params.get("title", "Details")
            inner_md = storage_to_markdown(str(rich_body_tag)) if rich_body_tag else macro.get_text(strip=True)
            macro.replace_with(NavigableString(f"\n<details><summary>{title}</summary>\n\n{inner_md.strip()}\n</details>\n"))
        elif macro_name == "status":
            colour = params.get("colour", "")
            title = params.get("title", "STATUS")
            macro.replace_with(NavigableString(f" `[{title}{':' + colour if colour else ''}]` "))
        elif macro_name in ("toc", "children", "attachments", "recently-updated"):
            macro.replace_with(NavigableString(f"\n*[Confluence Macro: {macro_name}]*\n"))
        else:
            inner = storage_to_markdown(str(rich_body_tag)) if rich_body_tag else (plain_body_tag.get_text() if plain_body_tag else "")
            macro.replace_with(NavigableString(f"\n*[Macro: {macro_name}]*\n{inner}\n"))

    for link in soup.find_all(re.compile(r"^(ac:link|link)$")):
        page_ref = link.find(re.compile(r"^(ri:page|page)$"))
        att_ref = link.find(re.compile(r"^(ri:attachment|attachment)$"))
        user_ref = link.find(re.compile(r"^(ri:user|user)$"))
        label_text = link.get_text(strip=True)
        if page_ref:
            target_title = page_ref.get("ri:content-title") or page_ref.get("content-title") or "Page"
            space_key = page_ref.get("ri:space-key") or page_ref.get("space-key") or ""
            display = label_text or target_title
            ref_str = f"{space_key}:{target_title}" if space_key else target_title
            link.replace_with(NavigableString(f"[{display}](wiki://{ref_str})"))
        elif att_ref:
            filename = att_ref.get("ri:filename") or att_ref.get("filename") or "attachment"
            display = label_text or filename
            link.replace_with(NavigableString(f"[{display}](attachment://{filename})"))
        elif user_ref:
            ukey = user_ref.get("ri:username") or user_ref.get("ri:userkey") or "user"
            link.replace_with(NavigableString(f"@{label_text or ukey}"))

    for img in soup.find_all(re.compile(r"^(ac:image|image)$")):
        att_ref = img.find(re.compile(r"^(ri:attachment|attachment)$"))
        url_ref = img.find(re.compile(r"^(ri:url|url)$"))
        if att_ref:
            filename = att_ref.get("ri:filename") or att_ref.get("filename") or "image"
            img.replace_with(NavigableString(f"![{filename}](attachment://{filename})"))
        elif url_ref:
            val = url_ref.get("ri:value") or url_ref.get("value") or ""
            img.replace_with(NavigableString(f"![image]({val})"))

    for level in range(6, 0, -1):
        for h in soup.find_all(f"h{level}"):
            h.replace_with(NavigableString(f"\n{'#' * level} {h.get_text(' ', strip=True)}\n"))

    for pre in soup.find_all("pre"):
        pre.replace_with(NavigableString(f"\n```\n{pre.get_text()}\n```\n"))

    for code in soup.find_all("code"):
        if code.parent and code.parent.name != "pre":
            code.replace_with(NavigableString(f"`{code.get_text()}`"))

    for a in soup.find_all("a"):
        href = a.get("href", "")
        text = a.get_text(" ", strip=True) or href
        if href:
            a.replace_with(NavigableString(f"[{text}]({href})"))

    for strong in soup.find_all(["strong", "b"]):
        txt = strong.get_text(" ", strip=True)
        if txt:
            strong.replace_with(NavigableString(f"**{txt}**"))

    for em in soup.find_all(["em", "i"]):
        txt = em.get_text(" ", strip=True)
        if txt:
            em.replace_with(NavigableString(f"*{txt}*"))

    for table in soup.find_all("table"):
        rows = []
        for tr in table.find_all("tr"):
            cells = [
                re.sub(r"\s+", " ", cell.get_text(" ", strip=True)).replace("|", "\\|")
                for cell in tr.find_all(["th", "td"])
            ]
            if cells:
                rows.append(cells)
        if rows:
            max_cols = max(len(r) for r in rows)
            norm_rows = [r + [""] * (max_cols - len(r)) for r in rows]
            md_table = ["| " + " | ".join(norm_rows[0]) + " |"]
            md_table.append("| " + " | ".join(["---"] * max_cols) + " |")
            for r in norm_rows[1:]:
                md_table.append("| " + " | ".join(r) + " |")
            table.replace_with(NavigableString("\n" + "\n".join(md_table) + "\n"))

    # Convert Confluence task lists (<ac:task>) into clean Markdown checkboxes (- [x] / - [ ])
    for task in soup.find_all(re.compile(r"^(ac:task|task)$")):
        status_tag = task.find(re.compile(r"^(ac:task-status|task-status)$"))
        body_tag = task.find(re.compile(r"^(ac:task-body|task-body)$"))
        status_txt = (status_tag.get_text(strip=True) if status_tag else "").lower()
        marker = "[x]" if status_txt == "complete" else "[ ]"
        body_txt = re.sub(r"\s+", " ", body_tag.get_text(" ", strip=True)) if body_tag else ""
        task.replace_with(NavigableString(f"\n- {marker} {body_txt}\n"))

    for ul in soup.find_all("ul"):
        items = [f"- {li.get_text(' ', strip=True)}" for li in ul.find_all("li", recursive=False)]
        if items:
            ul.replace_with(NavigableString("\n" + "\n".join(items) + "\n"))

    for ol in soup.find_all("ol"):
        items = [f"{idx}. {li.get_text(' ', strip=True)}" for idx, li in enumerate(ol.find_all("li", recursive=False), 1)]
        if items:
            ol.replace_with(NavigableString("\n" + "\n".join(items) + "\n"))

    for br in soup.find_all("br"):
        br.replace_with(NavigableString("\n"))

    for p in soup.find_all(["p", "div"]):
        p.append(NavigableString("\n"))

    raw_text = html.unescape(soup.get_text())
    return re.sub(r"\n{3,}", "\n\n", raw_text).strip()


def extract_tasks_from_storage_or_markdown(
    raw_storage: str,
    md_text: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Extracts Confluence `<ac:task>` items (and any plain Markdown `- [ ]` / `- [x]` checkbox items)
    from a page or version body into a uniform list of task objects:
      `{"task_id": Optional[str], "status": "complete" | "incomplete", "checked": bool, "text": str}`
    """
    tasks: List[Dict[str, Any]] = []
    seen_texts = set()

    if raw_storage and "<ac:task" in raw_storage:
        for m in re.finditer(r"<ac:task\b[^>]*>(.*?)</ac:task>", raw_storage, flags=re.DOTALL | re.IGNORECASE):
            inner = m.group(1)
            m_id = re.search(r"<ac:task-id>\s*(.*?)\s*</ac:task-id>", inner, flags=re.DOTALL | re.IGNORECASE)
            m_st = re.search(r"<ac:task-status>\s*(.*?)\s*</ac:task-status>", inner, flags=re.DOTALL | re.IGNORECASE)
            m_bd = re.search(r"<ac:task-body>(.*?)</ac:task-body>", inner, flags=re.DOTALL | re.IGNORECASE)
            task_id = m_id.group(1).strip() if m_id else None
            status_raw = (m_st.group(1).strip().lower() if m_st else "incomplete")
            is_complete = status_raw == "complete"
            body_html = m_bd.group(1) if m_bd else inner
            clean_txt = re.sub(r"\s+", " ", storage_to_markdown(body_html)).strip()
            if not clean_txt:
                clean_txt = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", body_html))).strip()
            if clean_txt:
                tasks.append({
                    "task_id": task_id,
                    "status": "complete" if is_complete else "incomplete",
                    "checked": is_complete,
                    "text": clean_txt,
                })
                seen_texts.add(clean_txt.lower())

    rendered = md_text if md_text is not None else (storage_to_markdown(raw_storage) if raw_storage else "")
    for line in rendered.splitlines():
        m_cb = re.match(r"^\s*(?:[-*+]|\d+\.)?\s*\[([ xX])\]\s+(.+?)\s*$", line)
        if m_cb:
            checked = m_cb.group(1).lower() == "x"
            txt = re.sub(r"\s+", " ", m_cb.group(2)).strip()
            if txt and txt.lower() not in seen_texts:
                tasks.append({
                    "task_id": None,
                    "status": "complete" if checked else "incomplete",
                    "checked": checked,
                    "text": txt,
                })
                seen_texts.add(txt.lower())

    return tasks


def diff_tasks(
    tasks_from: Any,
    tasks_to: Any,
    raw_to_opt: Optional[str] = None,
    md_to_opt: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Compares tasks/checkboxes between two versions of a page (`tasks_from` -> `tasks_to`,
    or `(raw_from, md_from, raw_to, md_to)`) and categorizes state flips (`[ ] -> [x]` completed,
    `[x] -> [ ]` reopened, newly added open/completed tasks, removed tasks, and all remaining open tasks).
    """
    if isinstance(tasks_from, str):
        if raw_to_opt is not None:
            tasks_from = extract_tasks_from_storage_or_markdown(tasks_from, str(tasks_to) if tasks_to is not None else None)
            tasks_to = extract_tasks_from_storage_or_markdown(raw_to_opt, md_to_opt)
        else:
            tasks_from = extract_tasks_from_storage_or_markdown(tasks_from)
            tasks_to = extract_tasks_from_storage_or_markdown(str(tasks_to or ""))
    def _norm_key(t: Dict[str, Any]) -> str:
        return re.sub(r"\s+", " ", str(t.get("text") or "")).strip().lower()

    from_by_text: Dict[str, Dict[str, Any]] = {}
    from_by_id: Dict[str, Dict[str, Any]] = {}
    for t in tasks_from:
        k = _norm_key(t)
        if k and k not in from_by_text:
            from_by_text[k] = t
        if t.get("task_id"):
            from_by_id[str(t["task_id"])] = t

    matched_from_keys = set()
    matched_from_ids = set()

    completed_in_window: List[Dict[str, Any]] = []
    reopened_in_window: List[Dict[str, Any]] = []
    added_open_in_window: List[Dict[str, Any]] = []
    added_completed_in_window: List[Dict[str, Any]] = []
    unchanged_open: List[Dict[str, Any]] = []
    unchanged_completed: List[Dict[str, Any]] = []

    for t_to in tasks_to:
        k = _norm_key(t_to)
        tid = str(t_to.get("task_id")) if t_to.get("task_id") else None
        t_from = None
        if k in from_by_text:
            t_from = from_by_text[k]
            matched_from_keys.add(k)
            if t_from.get("task_id"):
                matched_from_ids.add(str(t_from["task_id"]))
        elif tid and tid in from_by_id:
            t_from = from_by_id[tid]
            matched_from_ids.add(tid)
            matched_from_keys.add(_norm_key(t_from))

        if t_from is None:
            if t_to.get("checked"):
                added_completed_in_window.append(t_to)
            else:
                added_open_in_window.append(t_to)
        else:
            was_checked = bool(t_from.get("checked"))
            now_checked = bool(t_to.get("checked"))
            entry = dict(t_to)
            if _norm_key(t_from) != k:
                entry["previous_text"] = t_from.get("text")
            if not was_checked and now_checked:
                completed_in_window.append(entry)
            elif was_checked and not now_checked:
                reopened_in_window.append(entry)
            elif now_checked:
                unchanged_completed.append(entry)
            else:
                unchanged_open.append(entry)

    removed_in_window: List[Dict[str, Any]] = []
    for t_from in tasks_from:
        k = _norm_key(t_from)
        tid = str(t_from.get("task_id")) if t_from.get("task_id") else None
        if k not in matched_from_keys and (not tid or tid not in matched_from_ids):
            removed_in_window.append(t_from)

    still_open = [t for t in tasks_to if not t.get("checked")]
    return {
        "completed_in_window_count": len(completed_in_window),
        "reopened_in_window_count": len(reopened_in_window),
        "added_open_in_window_count": len(added_open_in_window),
        "added_completed_in_window_count": len(added_completed_in_window),
        "removed_in_window_count": len(removed_in_window),
        "still_open_count": len(still_open),
        "completed_in_window": completed_in_window,
        "reopened_in_window": reopened_in_window,
        "added_open_in_window": added_open_in_window,
        "added_completed_in_window": added_completed_in_window,
        "removed_in_window": removed_in_window,
        "still_open_tasks": still_open,
    }


def markdown_to_storage(md_text: str) -> str:
    """
    Converts Markdown (headings, fenced code blocks -> Confluence code macro,
    bullet/numbered lists, bold, italic, inline code, links, paragraphs) into valid
    Confluence XHTML storage format (`body.storage`).
    """
    lines = md_text.splitlines()
    out: List[str] = []
    in_code = False
    code_lang = ""
    code_buf: List[str] = []
    in_ul = False
    in_ol = False

    def close_lists() -> None:
        nonlocal in_ul, in_ol
        if in_ul:
            out.append("</ul>")
            in_ul = False
        if in_ol:
            out.append("</ol>")
            in_ol = False

    def format_inline(text: str) -> str:
        escaped = html.escape(text)
        escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
        escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
        escaped = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", escaped)
        escaped = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<a href="\2">\1</a>', escaped)
        return escaped

    for line in lines:
        if line.strip().startswith("```"):
            close_lists()
            if not in_code:
                in_code = True
                code_lang = line.strip()[3:].strip()
                code_buf = []
            else:
                in_code = False
                lang_param = (
                    f'<ac:parameter ac:name="language">{html.escape(code_lang)}</ac:parameter>'
                    if code_lang
                    else ""
                )
                code_body = "\n".join(code_buf).replace("]]>", "]]]]><![CDATA[>")
                out.append(
                    f'<ac:structured-macro ac:name="code">{lang_param}'
                    f"<ac:plain-text-body><![CDATA[{code_body}]]></ac:plain-text-body>"
                    f"</ac:structured-macro>"
                )
            continue

        if in_code:
            code_buf.append(line)
            continue

        s = line.strip()
        if not s:
            close_lists()
            continue

        m_h = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m_h:
            close_lists()
            lvl = len(m_h.group(1))
            out.append(f"<h{lvl}>{format_inline(m_h.group(2))}</h{lvl}>")
            continue

        m_ul = re.match(r"^[-*]\s+(.*)$", s)
        if m_ul:
            if in_ol:
                out.append("</ol>")
                in_ol = False
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{format_inline(m_ul.group(1))}</li>")
            continue

        m_ol = re.match(r"^\d+\.\s+(.*)$", s)
        if m_ol:
            if in_ul:
                out.append("</ul>")
                in_ul = False
            if not in_ol:
                out.append("<ol>")
                in_ol = True
            out.append(f"<li>{format_inline(m_ol.group(1))}</li>")
            continue

        close_lists()
        out.append(f"<p>{format_inline(s)}</p>")

    close_lists()
    return "\n".join(out)


def comment_mentions_crawler(comment_obj: Dict[str, Any], crawler_names: List[str]) -> Tuple[bool, Optional[str]]:
    """
    Checks whether a Confluence comment object mentions any configured crawler name/handle.
    Inspects BOTH:
      - `body.view.value` (rendered HTML, where Confluence expands `<ri:user ri:userkey="..."/>`
        into `<a class="confluence-userlink user-mention" data-username="..." href="/display/~...">DisplayName</a>`)
      - `body.storage.value` (raw XHTML storage format)
    Returns (True, matched_name) if found, otherwise (False, None).
    """
    if not crawler_names:
        return False, None

    raw_view = comment_obj.get("body", {}).get("view", {}).get("value", "") or ""
    raw_storage = comment_obj.get("body", {}).get("storage", {}).get("value", "") or ""
    combined_haystack = f"{raw_view}\n{raw_storage}\n{html.unescape(raw_view)}\n{html.unescape(raw_storage)}".lower()

    for name in crawler_names:
        clean_name = name.strip().lstrip("@").lower()
        if clean_name and clean_name in combined_haystack:
            return True, name.strip()
    return False, None


def is_content_owned_by_crawler(
    content_obj: Dict[str, Any],
    crawler_username: str = "",
    crawler_display_name: str = "",
    crawler_names: Optional[List[str]] = None,
) -> Tuple[bool, Dict[str, Any]]:
    """
    Strictly verifies whether a Confluence content object (`blogpost` or `comment`) was
    created/owned by the crawler.
    In Confluence Data Center REST API v1 (`expand=history,version`):
      - `history.createdBy` stores the original creator (`username`, `displayName`, `userKey`).
      - `version.by` stores the author of the current revision (used as fallback only when
        `history.createdBy` is not present and `version.number == 1`).
    Double-Check Policy:
      - When BOTH `CONFLUENCE_CRAWLER_USERNAME` and `CONFLUENCE_CRAWLER_NAME` are configured in
        `access_settings.env` (recommended), BOTH `createdBy.username` AND `createdBy.displayName`
        must match (case-insensitively) for ownership to be granted.
      - When only one is configured, that field must match `createdBy`.
    """
    history_obj = content_obj.get("history") or {}
    created_by = history_obj.get("createdBy") or {}
    version_obj = content_obj.get("version") or {}
    if not created_by and int(version_obj.get("number", 1)) == 1:
        created_by = version_obj.get("by") or {}

    actual_username = str(created_by.get("username") or "").strip().lstrip("~@")
    actual_display_name = str(created_by.get("displayName") or "").strip().lstrip("@")

    exp_username = crawler_username.strip().lstrip("~@")
    exp_display_name = crawler_display_name.strip().lstrip("@")
    exp_tokens_lower = [n.strip().lstrip("~@").lower() for n in (crawler_names or []) if n.strip().lstrip("~@")]

    details: Dict[str, Any] = {
        "creator_username": actual_username or None,
        "creator_display_name": actual_display_name or None,
        "configured_username": exp_username or None,
        "configured_display_name": exp_display_name or None,
    }

    if not exp_username and not exp_display_name and not exp_tokens_lower:
        details["owned"] = False
        details["reason"] = "Neither CONFLUENCE_CRAWLER_USERNAME nor CONFLUENCE_CRAWLER_NAME is configured."
        return False, details

    if not actual_username and not actual_display_name:
        details["owned"] = False
        details["reason"] = "Content object has no creator metadata in history.createdBy (or version.by)."
        return False, details

    # Case 1 (Tight Double-Check): Both CONFLUENCE_CRAWLER_USERNAME and CONFLUENCE_CRAWLER_NAME are configured
    if exp_username and exp_display_name:
        username_match = bool(actual_username) and (actual_username.lower() == exp_username.lower())
        display_match = bool(actual_display_name) and (
            actual_display_name.lower() == exp_display_name.lower()
            or actual_display_name.lower() in exp_tokens_lower
        )
        owned = username_match and display_match
        details["owned"] = owned
        details["verification_mode"] = "double_check_username_and_display_name"
        details["username_matched"] = username_match
        details["display_name_matched"] = display_match
        return owned, details

    # Case 2: Only CONFLUENCE_CRAWLER_USERNAME is configured
    if exp_username:
        username_match = bool(actual_username) and (actual_username.lower() == exp_username.lower())
        details["owned"] = username_match
        details["verification_mode"] = "username_only"
        details["username_matched"] = username_match
        return username_match, details

    # Case 3: Only CONFLUENCE_CRAWLER_NAME is configured
    display_match = bool(actual_display_name) and (
        actual_display_name.lower() == exp_display_name.lower()
        or actual_display_name.lower() in exp_tokens_lower
    )
    username_fallback = bool(actual_username) and (actual_username.lower() in exp_tokens_lower)
    owned = display_match or username_fallback
    details["owned"] = owned
    details["verification_mode"] = "display_name_or_token"
    details["display_name_matched"] = display_match
    return owned, details


def is_liked_by_crawler(
    likes_payload: Any,
    crawler_username: str = "",
    crawler_display_name: str = "",
    crawler_names: Optional[List[str]] = None,
) -> Tuple[bool, List[Dict[str, Any]]]:
    """
    Parses the JSON response from `GET /rest/likes/1.0/content/{id}/likes` and checks
    whether the crawler has liked the content item (e.g., to mark a comment as DONE).
    Returns (liked_by_crawler, formatted_likes_list).
    """
    raw_likes: List[Dict[str, Any]] = []
    if isinstance(likes_payload, dict):
        raw_likes = likes_payload.get("likes") or likes_payload.get("results") or []
    elif isinstance(likes_payload, list):
        raw_likes = likes_payload

    exp_u = crawler_username.strip().lstrip("~@").lower()
    exp_d = crawler_display_name.strip().lstrip("@").lower()
    exp_tokens = {n.strip().lstrip("~@").lower() for n in (crawler_names or []) if n.strip().lstrip("~@")}
    if exp_u:
        exp_tokens.add(exp_u)
    if exp_d:
        exp_tokens.add(exp_d)

    formatted: List[Dict[str, Any]] = []
    crawler_liked = False

    for entry in raw_likes:
        if not isinstance(entry, dict):
            continue
        user_obj = entry.get("user") if isinstance(entry.get("user"), dict) else entry
        uname = str(user_obj.get("name") or user_obj.get("username") or "").strip().lstrip("~@")
        dname = str(user_obj.get("fullName") or user_obj.get("displayName") or "").strip().lstrip("@")
        formatted.append({
            "username": uname or None,
            "displayName": dname or None,
            "creationDate": entry.get("creationDate"),
        })
        if exp_tokens and (
            (uname and uname.lower() in exp_tokens)
            or (dname and dname.lower() in exp_tokens)
        ):
            crawler_liked = True

    return crawler_liked, formatted


def _parse_timestamp_to_epoch(ts_str: str, is_until_bound: bool = False) -> float:
    """
    Parses an ISO-8601 timestamp (including Confluence's 'Z' or '+0100' suffixes),
    a date-only string ('YYYY-MM-DD'), 'today'/'yesterday', or a relative offset
    ('-1d', '-7d', '-12h', '-30m') into a float Unix epoch timestamp.
    """
    s = (ts_str or "").strip()
    if not s:
        raise ValueError("Empty timestamp string.")
    local_tz = datetime.now().astimezone().tzinfo or timezone.utc
    now_dt = datetime.now(tz=local_tz)
    low = s.lower()
    if low in ("today", "yesterday"):
        base_date = now_dt.date() if low == "today" else (now_dt - timedelta(days=1)).date()
        if is_until_bound:
            dt = datetime(base_date.year, base_date.month, base_date.day, 23, 59, 59, 999999, tzinfo=local_tz)
        else:
            dt = datetime(base_date.year, base_date.month, base_date.day, 0, 0, 0, 0, tzinfo=local_tz)
        return dt.timestamp()
    if low == "now":
        return now_dt.timestamp()
    m_rel = re.match(r"^-(\d+(?:\.\d+)?)\s*([dhms])$", low)
    if m_rel:
        val = float(m_rel.group(1))
        unit = m_rel.group(2)
        mult = {"d": 86400.0, "h": 3600.0, "m": 60.0, "s": 1.0}[unit]
        return now_dt.timestamp() - (val * mult)
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        s = f"{s}T23:59:59.999999" if is_until_bound else f"{s}T00:00:00"
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    else:
        s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=local_tz)
    return dt.timestamp()


def _describe_resolved_time_window(since: Optional[str], until: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Returns a human- and machine-readable description of the resolved `[since, until]`
    time window (explicitly showing that a date-only `--until YYYY-MM-DD` is inclusive
    through `23:59:59.999999` of that date).
    """
    if not since and not until:
        return None
    local_tz = datetime.now().astimezone().tzinfo or timezone.utc
    since_ep = _parse_timestamp_to_epoch(since, is_until_bound=False) if since else None
    until_ep = _parse_timestamp_to_epoch(until, is_until_bound=True) if until else None
    since_iso = datetime.fromtimestamp(since_ep, tz=local_tz).isoformat(timespec="seconds") if since_ep is not None else None
    until_iso = datetime.fromtimestamp(until_ep, tz=local_tz).isoformat(timespec="seconds") if until_ep is not None else None
    return {
        "since_input": since,
        "until_input": until,
        "since_resolved": since_iso,
        "until_resolved_inclusive": until_iso,
        "note": "Date-only --until 'YYYY-MM-DD' is inclusive through 23:59:59 of that date (e.g. for Oct 10–15, pass --since 2026-10-10 --until 2026-10-15).",
    }


def _normalize_version_entry(v: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalizes a version dict from `/rest/api/content/{id}/version`,
    `/rest/experimental/content/{id}/version`, `/rest/api/content/{id}/history`,
    or `/rest/api/content/{id}?version=N` into a uniform schema.
    """
    ver_obj = v.get("version") if isinstance(v.get("version"), dict) and "number" not in v else v
    by_raw = ver_obj.get("by")
    if isinstance(by_raw, dict):
        by_display = by_raw.get("displayName") or by_raw.get("username")
        by_username = by_raw.get("username")
    else:
        by_display = by_raw
        by_username = ver_obj.get("username")
    num = ver_obj.get("number")
    return {
        "number": int(num) if num is not None else None,
        "when": ver_obj.get("when"),
        "by": by_display,
        "username": by_username,
        "minorEdit": ver_obj.get("minorEdit"),
        "message": ver_obj.get("message", "") or "",
    }


def _is_http_404_error(exc: Exception) -> bool:
    """
    Returns True ONLY for HTTP 404 Not Found errors (e.g. Confluence Data Center 10.x
    where `/rest/api/content/{id}/version` is not a registered REST resource).
    Never returns True for PermissionError or HTTP 401/403 errors.
    """
    if isinstance(exc, PermissionError):
        return False
    if getattr(exc, "status_code", None) == 404:
        return True
    msg = str(exc)
    if "HTTP 401" in msg or "HTTP 403" in msg:
        return False
    return "HTTP 404" in msg


# =============================================================================
# 4. GUARDED CRAWLER CONFLUENCE CLIENT
# =============================================================================

class GuardedCrawlerConfluenceClient:
    """
    Scoped client for Confluence Data Center / Server REST API v1.
    Separates Space Permission Tiers (`CONFLUENCE_ELEVATED_ACCESS_SPACES` and
    `CONFLUENCE_READONLY_SPACES`) from Initial Search & Starting Scope
    (`CONFLUENCE_DEFAULT_PAGE` and `CONFLUENCE_DEFAULT_SPACES`):
      1. Space Permission Guard:
         - Spaces in `CONFLUENCE_READONLY_SPACES` allow ONLY read/search/crawl operations;
           ALL writes (blogposts, comments, replies, likes/unlikes, edits, deletes) are blocked.
         - Spaces in `CONFLUENCE_ELEVATED_ACCESS_SPACES` (alias: `CONFLUENCE_DEFAULT_ACCESS_SPACES`)
           allow read/search/crawl plus the scoped write operations listed below.
         - Any space not listed in either setting is blocked for both reading and writing.
      2. Progressive Initial Search Scope (to minimize Confluence server load):
         - `CONFLUENCE_DEFAULT_PAGE` (alias: `CONFLUENCE_DEFAULT_PAGES`): one or more starting page
           URLs, `/x/...` tiny links, or page IDs tried first on initial searches before widening.
         - `CONFLUENCE_DEFAULT_SPACES`: space key(s) used for default space-level searches
           (defaults automatically to `CONFLUENCE_ELEVATED_ACCESS_SPACES` if not set).
         - Passing `--all-spaces` (`--wider`) or `--space <KEY>` widens searches across all
           allowed spaces (`CONFLUENCE_ELEVATED_ACCESS_SPACES` + `CONFLUENCE_READONLY_SPACES`).
      3. All HTTP GET requests in readable spaces (pages, blogposts, spaces, attachments, search, history, likes).
         Pages (`type == "page"`) and attachments (`type == "attachment"`) are strictly read-only.
      4. POST /rest/api/content ONLY in `CONFLUENCE_ELEVATED_ACCESS_SPACES` when creating a blogpost
         (`type == "blogpost"`) or adding a comment/reply to a page (`type == "comment"` and `container.type == "page"`).
      5. PUT or DELETE /rest/api/content/{blogpost_id} ONLY in `CONFLUENCE_ELEVATED_ACCESS_SPACES` when a
         programmatic pre-flight GET confirms that `{blogpost_id}` is strictly a blogpost (`type == "blogpost"`)
         AND is OWNED/CREATED by the crawler (`is_content_owned_by_crawler`).
      6. PUT /rest/api/content/{comment_id} ONLY in `CONFLUENCE_ELEVATED_ACCESS_SPACES` when a
         programmatic pre-flight GET confirms that `{comment_id}` is strictly a comment (`type == "comment"`)
         AND is OWNED/CREATED by the crawler (`is_content_owned_by_crawler`).
      7. DELETE /rest/api/content/{comment_id} ONLY in `CONFLUENCE_ELEVATED_ACCESS_SPACES` when a
         programmatic pre-flight GET confirms that `{comment_id}` is strictly a comment (`type == "comment"`)
         AND is EITHER owned/created by the crawler (`is_content_owned_by_crawler`) OR mentions the crawler
         (`comment_mentions_crawler`).
      8. POST or DELETE /rest/likes/1.0/content/{id}/likes ONLY in `CONFLUENCE_ELEVATED_ACCESS_SPACES` so the
         crawler can like/unlike a comment (e.g. to mark a mentioned instruction comment as DONE) or a page/blogpost.
    All other operations raise a PermissionError.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        token_file: Optional[str] = None,
        settings_file: Optional[str] = None,
        timeout: float = 30.0,
        verify_ssl: bool = True,
        throttle_ms: Optional[float] = None,
    ):
        cfg_file = settings_file or token_file
        self.base_url, self.base_url_source = load_base_url(cfg_file, explicit_base_url=base_url)
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        cfg_path = _resolve_settings_file(cfg_file)
        file_kv = _parse_env_file_dict(cfg_path) if cfg_path.is_file() else {}
        env_throttle = (
            file_kv.get("CONFLUENCE_THROTTLE_MS", "").strip()
            or os.environ.get("CONFLUENCE_THROTTLE_MS", "").strip()
        )
        self.throttle_ms: float = (
            float(throttle_ms)
            if throttle_ms is not None
            else (float(env_throttle) if env_throttle.replace(".", "", 1).isdigit() else 0.0)
        )
        self._last_request_ts: float = 0.0
        self.auth_type, self.credential, self.token_source = load_api_token(cfg_file)
        ident = load_crawler_identity(cfg_file)
        self.crawler_username: str = ident["username"]
        self.crawler_display_name: str = ident["display_name"]
        self.crawler_names: List[str] = ident["all_names"]
        self.crawler_name_source: str = ident["source"]
        self._verified_token_identity: Optional[Dict[str, Any]] = None

        space_policy = load_space_access_policy(cfg_file)
        self.readonly_spaces: List[str] = space_policy["readonly_spaces"]
        self.readonly_wildcard: bool = space_policy["readonly_wildcard"]
        self.elevated_access_spaces: List[str] = space_policy["elevated_access_spaces"]
        self.elevated_access_wildcard: bool = space_policy["elevated_access_wildcard"]
        # Backwards-compatible aliases
        self.default_access_spaces: List[str] = self.elevated_access_spaces
        self.default_access_wildcard: bool = self.elevated_access_wildcard
        # Initial search scope settings (distinct from write-permission tiers)
        self.default_spaces: List[str] = space_policy.get("default_spaces", list(self.elevated_access_spaces))
        self.default_spaces_wildcard: bool = space_policy.get("default_spaces_wildcard", False)
        self.default_spaces_source: str = space_policy.get("default_spaces_source", "")
        self.default_page: str = space_policy.get("default_page", "")
        self.default_pages: List[str] = space_policy.get("default_pages", [])
        self.default_page_source: str = space_policy.get("default_page_source", "")
        self.space_policy_configured: bool = space_policy["configured"]
        self.space_policy_source: str = space_policy["source"]
        self._content_space_cache: Dict[str, str] = {}
        self._page_cache: Dict[Tuple[str, str, Optional[int]], Dict[str, Any]] = {}
        self._version_endpoint_tier: Optional[str] = None

        self.session = requests.Session()
        if self.auth_type == "bearer":
            self.session.headers["Authorization"] = f"Bearer {self.credential}"
        else:
            self.session.headers["Authorization"] = f"Basic {self.credential}"
        self.session.headers["Accept"] = "application/json"
        self.session.headers["X-Atlassian-Token"] = "no-check"

    def _require_crawler_names(self) -> List[str]:
        if not self.crawler_names and not self.crawler_username and not self.crawler_display_name:
            raise PermissionError(
                f"[CRAWLER GUARD] Crawler identity is not configured. "
                f"Please set CONFLUENCE_CRAWLER_USERNAME and CONFLUENCE_CRAWLER_NAME in '{DEFAULT_SETTINGS_FILE}' "
                f"(or in environment variables '{CRAWLER_USERNAME_ENV_VAR}' / '{CRAWLER_NAME_ENV_VAR}') "
                "before editing or deleting comments or own blog posts."
            )
        return self.crawler_names

    # --- Space-Level Access Guard Helpers ---

    def _get_elevated_spaces_list(self) -> List[str]:
        raw = getattr(self, "elevated_access_spaces", None)
        if raw is None:
            raw = getattr(self, "default_access_spaces", [])
        return [s.strip().upper() for s in (raw or []) if s.strip()]

    def _get_elevated_wildcard(self) -> bool:
        return bool(
            getattr(self, "elevated_access_wildcard", False)
            or getattr(self, "default_access_wildcard", False)
        )

    def _get_default_search_spaces_list(self) -> List[str]:
        raw = getattr(self, "default_spaces", None)
        if raw:
            return [s.strip().upper() for s in raw if s.strip()]
        return self._get_elevated_spaces_list()

    def _is_space_guard_active(self) -> bool:
        """
        Returns True if space policy is configured or if the client was initialized normally via __init__.
        (Allows bare `__new__` unit test instances that do not configure space attributes to test
        comment/blogpost ownership in isolation unless they set `readonly_spaces` / `elevated_access_spaces`.)
        """
        if hasattr(self, "space_policy_configured"):
            return True
        if (
            getattr(self, "readonly_spaces", None)
            or getattr(self, "elevated_access_spaces", None)
            or getattr(self, "default_access_spaces", None)
        ):
            return True
        if (
            getattr(self, "readonly_wildcard", False)
            or getattr(self, "elevated_access_wildcard", False)
            or getattr(self, "default_access_wildcard", False)
        ):
            return True
        return False

    def get_space_access_mode(self, space_key: Optional[str]) -> str:
        """
        Evaluates the configured access mode for `space_key`:
          - `"read-only"`: space is in `CONFLUENCE_READONLY_SPACES` (reads allowed; all writes blocked).
          - `"elevated-access"`: space is in `CONFLUENCE_ELEVATED_ACCESS_SPACES` and not in `CONFLUENCE_READONLY_SPACES`.
          - `"blocked"`: space is not in either list.
          - `"unconfigured"`: neither `CONFLUENCE_READONLY_SPACES` nor `CONFLUENCE_ELEVATED_ACCESS_SPACES` is set.
        """
        if not self._is_space_guard_active():
            return "elevated-access"

        ro_list = [s.strip().upper() for s in getattr(self, "readonly_spaces", []) if s.strip()]
        rw_list = self._get_elevated_spaces_list()
        ro_wild = bool(getattr(self, "readonly_wildcard", False))
        rw_wild = self._get_elevated_wildcard()
        configured = bool(getattr(self, "space_policy_configured", bool(ro_list or rw_list or ro_wild or rw_wild)))

        if not configured:
            return "unconfigured"

        clean_key = (space_key or "").strip().upper()
        if not clean_key:
            return "blocked"

        # Explicit CONFLUENCE_READONLY_SPACES always takes precedence over CONFLUENCE_ELEVATED_ACCESS_SPACES
        if clean_key in ro_list:
            return "read-only"
        if clean_key in rw_list:
            return "elevated-access"
        if rw_wild:
            return "elevated-access"
        if ro_wild:
            return "read-only"
        return "blocked"

    def is_space_readable(self, space_key: Optional[str]) -> bool:
        return self.get_space_access_mode(space_key) in ("read-only", "elevated-access", "default-access")

    def is_space_writable(self, space_key: Optional[str]) -> bool:
        return self.get_space_access_mode(space_key) in ("elevated-access", "default-access")

    def _probe_server_space_info(self, space_key: Optional[str]) -> Optional[Dict[str, Any]]:
        """
        Best-effort lightweight check (`GET /rest/api/space/<key>`) to see whether the
        authenticated crawler token has server-side `View` access to `space_key` (and retrieve
        its human-readable space name), so Space Guard errors can tell the agent whether the user
        only needs to add the space key to `access_settings.env` or also needs to grant `View`
        permission on the Confluence server.
        """
        clean_key = (space_key or "").strip()
        if not clean_key or not hasattr(self, "session") or not getattr(self, "base_url", None):
            return None
        try:
            url = f"{self.base_url}/rest/api/space/{urllib.parse.quote(clean_key)}"
            resp = self.session.request(
                method="GET",
                url=url,
                timeout=getattr(self, "timeout", 15.0),
                verify=getattr(self, "verify_ssl", True),
            )
            if resp.ok:
                data = resp.json()
                return {
                    "key": data.get("key") or clean_key,
                    "name": data.get("name"),
                    "server_view_granted": True,
                }
            if resp.status_code in (401, 403, 404):
                return {
                    "key": clean_key,
                    "name": None,
                    "server_view_granted": False,
                    "http_status": resp.status_code,
                }
        except Exception:
            pass
        return None

    def _build_space_access_hint(self, space_key: Optional[str], for_write: bool = False) -> str:
        clean_key = (space_key or "").strip()
        if not clean_key:
            return (
                f" Run 'spaces' to list available Confluence spaces and configure "
                f"CONFLUENCE_ELEVATED_ACCESS_SPACES / CONFLUENCE_READONLY_SPACES in '{DEFAULT_SETTINGS_FILE}'."
            )
        probe = self._probe_server_space_info(clean_key)
        target_setting = (
            "CONFLUENCE_ELEVATED_ACCESS_SPACES"
            if for_write
            else "CONFLUENCE_READONLY_SPACES (for read-only access) or CONFLUENCE_ELEVATED_ACCESS_SPACES (for read + guarded-write access)"
        )
        if probe and probe.get("server_view_granted") is True:
            sp_name = f" ({probe['name']!r})" if probe.get("name") else ""
            return (
                f" [ACCESS HINT: The crawler account ALREADY has server-side 'View' access to space "
                f"{clean_key!r}{sp_name} on Confluence. To allow this operation, ask the user to add "
                f"'{clean_key}' to {target_setting} in '{DEFAULT_SETTINGS_FILE}'.]"
            )
        if probe and probe.get("server_view_granted") is False:
            return (
                f" [ACCESS HINT: The crawler account also does NOT have server-side 'View' permission "
                f"for space {clean_key!r} on Confluence (HTTP {probe.get('http_status')}). "
                f"To allow access, a Space Admin must grant 'View' permission to the crawler account "
                f"in Confluence Space Permissions AND add '{clean_key}' to {target_setting} in '{DEFAULT_SETTINGS_FILE}'. "
                f"Run 'spaces' to see all spaces currently visible to the crawler token.]"
            )
        return (
            f" [ACCESS HINT: To allow access to space {clean_key!r}, ask the user to add '{clean_key}' "
            f"to {target_setting} in '{DEFAULT_SETTINGS_FILE}' (and ensure the crawler account has "
            f"Confluence Space Permissions for '{clean_key}'). Run 'spaces' to inspect all spaces visible to the token.]"
        )

    def _assert_space_readable(self, space_key: Optional[str], action_label: str = "read content") -> str:
        if not self._is_space_guard_active():
            return "elevated-access"
        mode = self.get_space_access_mode(space_key)
        if mode == "unconfigured":
            raise PermissionError(
                f"[CRAWLER SPACE GUARD] Refusing to {action_label}: no allowed Confluence spaces are configured. "
                f"Please set CONFLUENCE_ELEVATED_ACCESS_SPACES and/or CONFLUENCE_READONLY_SPACES in '{DEFAULT_SETTINGS_FILE}' "
                "(or run 'spaces' to see all spaces visible to the crawler token on the server)."
            )
        loc_suffix = "" if (space_key and str(space_key) in action_label) else f" in space {space_key!r}"
        if mode not in ("read-only", "elevated-access", "default-access"):
            hint = self._build_space_access_hint(space_key, for_write=False)
            raise PermissionError(
                f"[CRAWLER SPACE GUARD] Refusing to {action_label}{loc_suffix}: "
                f"space {space_key!r} is NOT in CONFLUENCE_READONLY_SPACES ({getattr(self, 'readonly_spaces', [])}) "
                f"or CONFLUENCE_ELEVATED_ACCESS_SPACES ({self._get_elevated_spaces_list()}).{hint}"
            )
        return mode

    def _assert_space_writable(self, space_key: Optional[str], action_label: str = "modify content") -> str:
        if not self._is_space_guard_active():
            return "elevated-access"
        mode = self.get_space_access_mode(space_key)
        if mode == "unconfigured":
            raise PermissionError(
                f"[CRAWLER SPACE GUARD] Refusing to {action_label}: no allowed Confluence spaces are configured. "
                f"Please set CONFLUENCE_ELEVATED_ACCESS_SPACES in '{DEFAULT_SETTINGS_FILE}'."
            )
        loc_suffix = "" if (space_key and str(space_key) in action_label) else f" in space {space_key!r}"
        if mode == "read-only":
            raise PermissionError(
                f"[CRAWLER SPACE GUARD] Refusing to {action_label}{loc_suffix}: "
                f"space {space_key!r} is configured as strictly READ-ONLY in CONFLUENCE_READONLY_SPACES "
                f"({getattr(self, 'readonly_spaces', [])}). Reading, searching, and crawling are allowed, "
                "but all blogpost, comment, reply, edit, delete, and like/unlike operations are blocked in read-only spaces. "
                f"[ACCESS HINT: If guarded write access is needed in space {space_key!r}, ask the user to move "
                f"'{space_key}' from CONFLUENCE_READONLY_SPACES to CONFLUENCE_ELEVATED_ACCESS_SPACES in '{DEFAULT_SETTINGS_FILE}'.]"
            )
        if mode not in ("elevated-access", "default-access"):
            hint = self._build_space_access_hint(space_key, for_write=True)
            raise PermissionError(
                f"[CRAWLER SPACE GUARD] Refusing to {action_label}{loc_suffix}: "
                f"space {space_key!r} is NOT in CONFLUENCE_ELEVATED_ACCESS_SPACES ({self._get_elevated_spaces_list()}).{hint}"
            )
        return mode

    @staticmethod
    def _extract_space_key_from_obj(content_obj: Optional[Dict[str, Any]]) -> str:
        if not isinstance(content_obj, dict):
            return ""
        space_obj = content_obj.get("space")
        if isinstance(space_obj, dict) and space_obj.get("key"):
            return str(space_obj["key"]).strip()
        container_obj = content_obj.get("container")
        if isinstance(container_obj, dict):
            c_space = container_obj.get("space")
            if isinstance(c_space, dict) and c_space.get("key"):
                return str(c_space["key"]).strip()
            c_exp = container_obj.get("_expandable")
            if isinstance(c_exp, dict) and c_exp.get("space"):
                m = re.search(r"/rest/api/space/([^/?#]+)", str(c_exp["space"]))
                if m:
                    return urllib.parse.unquote(m.group(1)).strip()
        exp_obj = content_obj.get("_expandable")
        if isinstance(exp_obj, dict) and exp_obj.get("space"):
            m = re.search(r"/rest/api/space/([^/?#]+)", str(exp_obj["space"]))
            if m:
                return urllib.parse.unquote(m.group(1)).strip()
        return ""

    def _get_content_space_key(
        self,
        content_id: Optional[str],
        content_obj: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Determines the Confluence space key for a content object (`page`, `blogpost`, `comment`, or `attachment`),
        using cached metadata or a pre-flight GET when needed.
        """
        cache: Dict[str, str] = getattr(self, "_content_space_cache", {})
        if not hasattr(self, "_content_space_cache"):
            self._content_space_cache = cache

        clean_id = str(content_id or (content_obj.get("id") if isinstance(content_obj, dict) else "") or "").strip()

        extracted = self._extract_space_key_from_obj(content_obj)
        if extracted:
            if clean_id:
                cache[clean_id] = extracted
            return extracted

        if clean_id and clean_id in cache:
            return cache[clean_id]

        if isinstance(content_obj, dict):
            container = content_obj.get("container")
            if isinstance(container, dict) and container.get("id"):
                cid = str(container["id"]).strip()
                if cid in cache:
                    cache[clean_id] = cache[cid]
                    return cache[cid]

        if clean_id and clean_id.isdigit():
            fetched = self.get_json(
                f"/rest/api/content/{clean_id}",
                params={"expand": "space,container,container.space"},
            )
            extracted = self._extract_space_key_from_obj(fetched)
            if not extracted and isinstance(fetched.get("container"), dict) and fetched["container"].get("id"):
                parent_id = str(fetched["container"]["id"]).strip()
                if parent_id and parent_id != clean_id:
                    parent_obj = self.get_json(f"/rest/api/content/{parent_id}", params={"expand": "space"})
                    extracted = self._extract_space_key_from_obj(parent_obj)
                    if extracted:
                        cache[parent_id] = extracted
            if extracted:
                cache[clean_id] = extracted
                return extracted

        return ""

    def _scope_cql_to_allowed_spaces(
        self,
        cql: str,
        require_writable: bool = False,
        include_readonly: bool = False,
    ) -> str:
        """
        Validates any explicit `space = ...` or `space IN (...)` clauses in `cql` against the
        configured space policy, and automatically scopes `cql` when no explicit space clause is present:
          - By default (`include_readonly=False`), scopes searches to `CONFLUENCE_DEFAULT_SPACES`
            (which defaults to `CONFLUENCE_ELEVATED_ACCESS_SPACES`, falling back to `CONFLUENCE_READONLY_SPACES`
            only if no default/elevated spaces are configured).
          - When `include_readonly=True` (e.g. `--all-spaces` / `--wider`), scopes searches across both
            `CONFLUENCE_ELEVATED_ACCESS_SPACES` and `CONFLUENCE_READONLY_SPACES`.
        """
        if not self._is_space_guard_active():
            return cql

        if not getattr(self, "space_policy_configured", False) and not (
            getattr(self, "readonly_spaces", None) or self._get_elevated_spaces_list()
        ):
            self._assert_space_readable(None, "execute CQL search")

        referenced_spaces: List[str] = []
        for m_eq in re.finditer(r'\bspace\s*=\s*(?:"([^"]+)"|\'([^\']+)\'|([A-Za-z0-9_~-]+))', cql, flags=re.IGNORECASE):
            sp = (m_eq.group(1) or m_eq.group(2) or m_eq.group(3) or "").strip()
            if sp:
                referenced_spaces.append(sp)

        for m_in in re.finditer(r"\bspace\s+in\s*\(([^)]+)\)", cql, flags=re.IGNORECASE):
            inner = m_in.group(1)
            for part in inner.split(","):
                sp = part.strip().strip("\"'")
                if sp and not sp.lower().startswith("current"):
                    referenced_spaces.append(sp)

        for sp in referenced_spaces:
            if require_writable:
                self._assert_space_writable(sp, f"search/modify in space {sp!r}")
            else:
                self._assert_space_readable(sp, f"search in space {sp!r}")

        if referenced_spaces:
            return cql

        ro_wild = bool(getattr(self, "readonly_wildcard", False))
        rw_wild = self._get_elevated_wildcard()
        ro_list = [s.strip().upper() for s in getattr(self, "readonly_spaces", []) if s.strip()]
        rw_list = self._get_elevated_spaces_list()
        def_list = [s for s in self._get_default_search_spaces_list() if self.is_space_readable(s)]

        if require_writable:
            if rw_wild:
                return cql
            allowed_spaces = rw_list
        elif include_readonly:
            if ro_wild or rw_wild:
                return cql
            allowed_spaces = list(dict.fromkeys(rw_list + ro_list))
        else:
            # Default space-level search scope: prefer CONFLUENCE_DEFAULT_SPACES (which defaults to
            # CONFLUENCE_ELEVATED_ACCESS_SPACES), falling back to CONFLUENCE_READONLY_SPACES only if neither is set.
            if def_list:
                allowed_spaces = def_list
            elif rw_wild:
                return cql
            elif rw_list:
                allowed_spaces = rw_list
            elif ro_wild:
                return cql
            else:
                allowed_spaces = ro_list

        if not allowed_spaces:
            if require_writable:
                self._assert_space_writable(None, "execute CQL query")
            else:
                self._assert_space_readable(None, "execute CQL query")
            return cql

        if len(allowed_spaces) == 1:
            space_clause = f'space = "{allowed_spaces[0]}"'
        else:
            quoted = ", ".join(f'"{s}"' for s in allowed_spaces)
            space_clause = f"space IN ({quoted})"

        m_order = re.search(r"\bORDER\s+BY\b", cql, flags=re.IGNORECASE)
        if m_order:
            body_part = cql[: m_order.start()].strip()
            order_part = cql[m_order.start() :].strip()
            if body_part:
                return f"{space_clause} AND ({body_part}) {order_part}"
            return f"{space_clause} {order_part}"
        return f"{space_clause} AND ({cql})" if cql.strip() else space_clause

    def _verify_authenticated_token_matches_crawler_env(self) -> Optional[Dict[str, Any]]:
        """
        Cross-checks (and caches once per client session) that the live authenticated API token
        (`GET /rest/api/user/current`) belongs to the same Confluence user configured in
        `CONFLUENCE_CRAWLER_USERNAME` and `CONFLUENCE_CRAWLER_NAME`.
        This prevents accidental writes/edits/deletes if a personal or admin PAT token is ever
        mistakenly placed into the crawler's `access_settings.env`.
        """
        if getattr(self, "_verified_token_identity", None) is not None:
            return self._verified_token_identity

        self._require_crawler_names()
        if not hasattr(self, "session"):
            return None

        user = self.whoami()
        if not isinstance(user, dict) or (not user.get("username") and not user.get("displayName")):
            return None

        token_matched, match_details = is_content_owned_by_crawler(
            {"history": {"createdBy": user}},
            crawler_username=self.crawler_username,
            crawler_display_name=self.crawler_display_name,
            crawler_names=self.crawler_names,
        )
        if not token_matched:
            raise PermissionError(
                f"[CRAWLER GUARD] Token-to-.env identity mismatch! The API token in '{getattr(self, 'token_source', 'env')}' "
                f"authenticates as Confluence user username={user.get('username')!r}, displayName={user.get('displayName')!r}, "
                f"which does NOT match the configured crawler identity in '{getattr(self, 'crawler_name_source', 'env')}' "
                f"(CONFLUENCE_CRAWLER_USERNAME={self.crawler_username!r}, CONFLUENCE_CRAWLER_NAME={self.crawler_display_name!r}). "
                "Refusing all write/edit/delete operations to prevent accidental use of a personal/admin token."
            )

        self._verified_token_identity = {
            "username": user.get("username"),
            "displayName": user.get("displayName"),
            "userKey": user.get("userKey"),
            "verification_mode": match_details.get("verification_mode"),
        }
        return self._verified_token_identity

    def _verify_own_blogpost_on_server(
        self,
        blogpost_id: str,
        action_label: str,
    ) -> Dict[str, Any]:
        """
        Programmatic pre-flight server check before ANY blogpost edit (PUT) or deletion (DELETE).
        Loads `/rest/api/content/{blogpost_id}?expand=history,version,body.storage,body.view,space`
        directly from Confluence and enforces five programmatic assertions:
          1. The live API token (`whoami`) matches the configured crawler identity in `access_settings.env`.
          2. `blogpost_id` is a valid numeric Confluence ID and matches `existing["id"]`.
          3. `existing["type"] == "blogpost"` (so a hallucinated `page` or `comment` ID can NEVER
             be edited or deleted as a blogpost).
          4. Space Guard: the blogpost's space is in `CONFLUENCE_ELEVATED_ACCESS_SPACES` (and NOT in
             `CONFLUENCE_READONLY_SPACES`).
          5. `is_content_owned_by_crawler(...)` is True (confirming `history.createdBy` matches
             the crawler's configured `CONFLUENCE_CRAWLER_USERNAME` and `CONFLUENCE_CRAWLER_NAME`).
             Note: Mentioning the crawler in another user's blogpost does NOT grant edit/delete rights!
        """
        clean_id = str(blogpost_id).strip()
        if not clean_id.isdigit():
            raise PermissionError(
                f"[CRAWLER GUARD] Refusing to {action_label} invalid non-numeric blogpost_id '{blogpost_id}'."
            )

        self._require_crawler_names()
        if action_label != "verify":
            self._verify_authenticated_token_matches_crawler_env()
        existing = self.get_json(
            f"/rest/api/content/{clean_id}",
            params={"expand": "history,version,body.storage,body.view,space"},
        )

        returned_id = str(existing.get("id", "")).strip()
        if returned_id != clean_id:
            raise PermissionError(
                f"[CRAWLER GUARD] Server returned ID '{returned_id}' which does not match requested blogpost_id '{clean_id}'."
            )

        actual_type = str(existing.get("type", "")).strip().lower()
        if actual_type != "blogpost":
            raise PermissionError(
                f"[CRAWLER GUARD] Refusing to {action_label} content ID {clean_id}: "
                f"its type on the server is '{actual_type}' (title: {existing.get('title')!r}), NOT 'blogpost'. "
                "Pages can NEVER be edited or deleted."
            )

        space_key = self._extract_space_key_from_obj(existing)
        if self._is_space_guard_active() and (space_key or getattr(self, "space_policy_configured", False) is False):
            if action_label == "verify":
                self._assert_space_readable(space_key, f"verify blogpost ID {clean_id}")
            else:
                self._assert_space_writable(space_key, f"{action_label} blogpost ID {clean_id}")

        owned, owner_details = is_content_owned_by_crawler(
            existing,
            crawler_username=self.crawler_username,
            crawler_display_name=self.crawler_display_name,
            crawler_names=self.crawler_names,
        )
        if not owned:
            raise PermissionError(
                f"[CRAWLER GUARD] Refusing to {action_label} blogpost ID {clean_id} ({existing.get('title')!r}): "
                f"it is NOT owned by the crawler (creator username={owner_details.get('creator_username')!r}, "
                f"displayName={owner_details.get('creator_display_name')!r}; expected "
                f"username={owner_details.get('configured_username')!r}, "
                f"displayName={owner_details.get('configured_display_name')!r}). "
                "Only blog posts created by the crawler itself may be edited or deleted."
            )

        existing["_owner_check"] = owner_details
        existing["_space_key"] = space_key
        existing["_space_access_mode"] = self.get_space_access_mode(space_key) if space_key else "elevated-access"
        return existing

    def _verify_comment_editable_or_deletable_on_server(
        self,
        comment_id: str,
        action_label: str,
        expected_page_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Programmatic pre-flight server check before ANY comment edit (PUT), deletion (DELETE), or verification.
        Loads `/rest/api/content/{comment_id}?expand=history,version,body.storage,body.view,container,container.space,ancestors,space`
        directly from Confluence and enforces five programmatic assertions:
          1. `comment_id` is a valid numeric Confluence ID and matches `existing["id"]`.
          2. `existing["type"] == "comment"` (so a hallucinated `page`, `blogpost`, or `attachment` ID
             can NEVER be deleted or edited via comment operations).
          3. If `expected_page_id` is provided, `existing["container"]["id"] == str(expected_page_id)`
             (ensuring the comment belongs to the expected parent page).
          4. Space Guard: the comment's space is in `CONFLUENCE_ELEVATED_ACCESS_SPACES` (and NOT in
             `CONFLUENCE_READONLY_SPACES`).
          5. Authorization Check:
               - For `action_label == "update"`: REQUIRES `is_content_owned_by_crawler(existing, ...)` to be True.
                 (Confluence Server blocks editing another user's comment without Space Admin rights;
                 if the comment mentions the crawler, the crawler should reply to it, like it, or delete it.)
               - For `action_label in ("delete", "verify")`: REQUIRES EITHER `is_content_owned_by_crawler` is True
                 OR `comment_mentions_crawler(existing, self.crawler_names)` is True.
        """
        clean_id = str(comment_id).strip()
        if not clean_id.isdigit():
            raise PermissionError(
                f"[CRAWLER GUARD] Refusing to {action_label} invalid non-numeric comment_id '{comment_id}'."
            )

        crawler_names = self._require_crawler_names()
        if action_label != "verify":
            self._verify_authenticated_token_matches_crawler_env()
        existing = self.get_json(
            f"/rest/api/content/{clean_id}",
            params={"expand": "history,version,body.storage,body.view,container,container.space,ancestors,space"},
        )

        returned_id = str(existing.get("id", "")).strip()
        if returned_id != clean_id:
            raise PermissionError(
                f"[CRAWLER GUARD] Server returned ID '{returned_id}' which does not match requested comment_id '{clean_id}'."
            )

        actual_type = str(existing.get("type", "")).strip().lower()
        if actual_type != "comment":
            raise PermissionError(
                f"[CRAWLER GUARD] Refusing to {action_label} content ID {clean_id}: "
                f"its type on the server is '{actual_type}' (title: {existing.get('title')!r}), NOT 'comment'. "
                "Pages, blogposts, and attachments can NEVER be edited or deleted as comments."
            )

        container = existing.get("container", {}) or {}
        actual_page_id = str(container.get("id", "")).strip()
        if expected_page_id is not None and str(expected_page_id).strip() != actual_page_id:
            raise PermissionError(
                f"[CRAWLER GUARD] Refusing to {action_label} comment ID {clean_id}: "
                f"it belongs to page ID '{actual_page_id}' ({container.get('title')!r}), "
                f"not the expected --page-id '{expected_page_id}'."
            )

        space_key = self._extract_space_key_from_obj(existing)
        if not space_key and self._is_space_guard_active() and actual_page_id:
            space_key = getattr(self, "_content_space_cache", {}).get(actual_page_id, "")
        if self._is_space_guard_active() and (space_key or getattr(self, "space_policy_configured", False) is False):
            if action_label == "verify":
                self._assert_space_readable(space_key, f"verify comment ID {clean_id}")
            else:
                self._assert_space_writable(space_key, f"{action_label} comment ID {clean_id}")

        owned, owner_details = is_content_owned_by_crawler(
            existing,
            crawler_username=self.crawler_username,
            crawler_display_name=self.crawler_display_name,
            crawler_names=crawler_names,
        )
        matched_mention, matched_name = comment_mentions_crawler(existing, crawler_names)

        if action_label == "update" and not owned:
            if matched_mention:
                raise PermissionError(
                    f"[CRAWLER GUARD] Refusing to update comment ID {clean_id}: "
                    f"although it mentions the crawler ({matched_name!r}), it was authored by another user "
                    f"(username={owner_details.get('creator_username')!r}, displayName={owner_details.get('creator_display_name')!r}), "
                    "and Confluence blocks editing another user's comment without Space Admin rights. "
                    f"To act on or mark this instruction comment as DONE, either reply to it "
                    f"('add-comment {actual_page_id or '<PAGE_ID>'} --parent-comment-id {clean_id} ...'), "
                    f"like it ('like {clean_id}'), or delete it ('delete-comment {clean_id}')."
                )
            raise PermissionError(
                f"[CRAWLER GUARD] Refusing to update comment ID {clean_id}: "
                f"it is NOT owned by the crawler (creator username={owner_details.get('creator_username')!r}, "
                f"displayName={owner_details.get('creator_display_name')!r}). "
                "Only comments created by the crawler itself may be edited."
            )

        if not (owned or matched_mention):
            raise PermissionError(
                f"[CRAWLER GUARD] Refusing to {action_label} comment ID {clean_id}: "
                f"the comment is NEITHER owned by the crawler (creator username={owner_details.get('creator_username')!r}, "
                f"displayName={owner_details.get('creator_display_name')!r}) "
                f"NOR mentions any configured crawler identifier ({crawler_names}). "
                "Only the crawler's own comments or comments mentioning the crawler may be deleted."
            )

        if owned and matched_mention:
            auth_basis = "own_comment_and_mentions_crawler"
        elif owned:
            auth_basis = "own_comment"
        else:
            auth_basis = "mentions_crawler"

        existing["_is_own_comment"] = owned
        existing["_owner_check"] = owner_details
        existing["_matched_crawler_name"] = matched_name
        existing["_authorization_basis"] = auth_basis
        existing["_space_key"] = space_key
        existing["_space_access_mode"] = self.get_space_access_mode(space_key) if space_key else "elevated-access"
        return existing

    # Backwards-compatible alias
    _verify_comment_mentions_crawler_on_server = _verify_comment_editable_or_deletable_on_server

    def _validate_scoped_write(
        self,
        method: str,
        url: str,
        json_body: Optional[Any],
        files: Optional[Any],
        data: Optional[Any],
    ) -> None:
        m = method.upper()
        if m == "GET":
            return

        if files is not None or data is not None:
            raise PermissionError(
                "[CRAWLER GUARD] Multipart/form-data uploads (e.g. attachments) are strictly prohibited in this skill."
            )

        # Ensure the live API token matches the configured crawler identity in access_settings.env
        self._verify_authenticated_token_matches_crawler_env()

        parsed_path = urllib.parse.urlparse(url).path.rstrip("/")

        # Experimental REST endpoints (/rest/experimental/...) are strictly read-only (e.g. GET /version)
        if "/rest/experimental/" in parsed_path or parsed_path.endswith("/rest/experimental"):
            raise PermissionError(
                f"[CRAWLER GUARD] Write operations ({m}) to '/rest/experimental/...' endpoints ('{parsed_path}') "
                "are strictly prohibited. Experimental endpoints are read-only in this skill."
            )

        # Case 0: POST or DELETE /rest/likes/1.0/content/{id}/likes (Like / Unlike content)
        m_like = re.search(r"/rest/likes/1\.0/content/(\d+)/likes$", parsed_path)
        if m in ("POST", "DELETE") and m_like:
            like_target_id = m_like.group(1)
            if self._is_space_guard_active():
                space_key = self._get_content_space_key(like_target_id)
                self._assert_space_writable(
                    space_key,
                    f"{'like' if m == 'POST' else 'unlike'} content ID {like_target_id}",
                )
            return

        # Case 1: POST /rest/api/content (Create Blogpost OR Add Page Comment)
        if m == "POST":
            if not parsed_path.endswith("/rest/api/content"):
                raise PermissionError(
                    f"[CRAWLER GUARD] POST to '{parsed_path}' is strictly prohibited. "
                    "Only POST to '/rest/api/content' (for blogpost creation or page comments) "
                    "or '/rest/likes/1.0/content/<id>/likes' (for liking) is allowed."
                )
            if not isinstance(json_body, dict):
                raise PermissionError(
                    "[CRAWLER GUARD] POST to '/rest/api/content' requires a JSON object payload."
                )
            content_type = str(json_body.get("type", "")).strip().lower()
            if content_type == "blogpost":
                if "id" in json_body:
                    raise PermissionError(
                        "[CRAWLER GUARD] Use PUT /rest/api/content/<blogpost_id> (update-blogpost) to update an own blogpost."
                    )
                if self._is_space_guard_active():
                    target_space = str((json_body.get("space") or {}).get("key") or "").strip()
                    self._assert_space_writable(target_space, f"create blogpost in space {target_space!r}")
                return
            if content_type == "comment":
                container = json_body.get("container")
                if (
                    isinstance(container, dict)
                    and str(container.get("type", "")).strip().lower() == "page"
                    and container.get("id")
                ):
                    if self._is_space_guard_active():
                        target_page_id = str(container.get("id")).strip()
                        target_space = self._get_content_space_key(target_page_id, container)
                        self._assert_space_writable(
                            target_space,
                            f"add comment on page ID {target_page_id}",
                        )
                    return
                raise PermissionError(
                    "[CRAWLER GUARD] Comments in this skill must target a page "
                    "(`container: {'id': '<page_id>', 'type': 'page'}`)."
                )
            raise PermissionError(
                f"[CRAWLER GUARD] Creating content of type '{content_type}' is strictly prohibited. "
                "Pages are read-only in this skill; only `type='blogpost'` and `type='comment'` (on a page) are permitted."
            )

        # Case 2: PUT or DELETE /rest/api/content/{id}
        # Permitted ONLY in CONFLUENCE_ELEVATED_ACCESS_SPACES for:
        #   (a) PUT on comments strictly OWNED by the crawler
        #   (b) DELETE on comments that are OWNED by the crawler OR mention the crawler
        #   (c) PUT or DELETE on blog posts that are strictly OWNED by the crawler
        # Pages (`type == "page"`) and attachments are NEVER allowed.
        if m in ("PUT", "DELETE"):
            m_id = re.search(r"/rest/api/content/(\d+)$", parsed_path)
            if not m_id:
                raise PermissionError(
                    f"[CRAWLER GUARD] {m} to '{parsed_path}' is strictly prohibited. "
                    "Only guarded updates/deletions on '/rest/api/content/<id>' are allowed."
                )
            target_id = m_id.group(1)

            if m == "PUT":
                if not isinstance(json_body, dict):
                    raise PermissionError(
                        "[CRAWLER GUARD] PUT on '/rest/api/content/<id>' requires a JSON object payload."
                    )
                put_type = str(json_body.get("type", "")).strip().lower()
                if put_type == "comment":
                    self._verify_comment_editable_or_deletable_on_server(
                        comment_id=target_id,
                        action_label="update",
                    )
                    return
                if put_type == "blogpost":
                    self._verify_own_blogpost_on_server(
                        blogpost_id=target_id,
                        action_label="update",
                    )
                    return
                raise PermissionError(
                    f"[CRAWLER GUARD] PUT on '/rest/api/content/{target_id}' with type='{put_type}' is strictly prohibited. "
                    "Pages cannot be edited; only `type='comment'` (own only) and `type='blogpost'` (own only) are allowed."
                )

            # DELETE: Inspect the server object's actual type first so we apply the right programmatic guard
            existing_Peek = self.get_json(
                f"/rest/api/content/{target_id}",
                params={"expand": "history,version,body.storage,body.view,container,container.space,ancestors,space"},
            )
            server_type = str(existing_Peek.get("type", "")).strip().lower()
            if server_type == "comment":
                self._verify_comment_editable_or_deletable_on_server(
                    comment_id=target_id,
                    action_label="delete",
                )
                return
            if server_type == "blogpost":
                self._verify_own_blogpost_on_server(
                    blogpost_id=target_id,
                    action_label="delete",
                )
                return
            raise PermissionError(
                f"[CRAWLER GUARD] Refusing to delete content ID {target_id}: "
                f"its type on the server is '{server_type}' (title: {existing_Peek.get('title')!r}). "
                "Pages and attachments can NEVER be deleted."
            )

        raise PermissionError(
            f"[CRAWLER GUARD] HTTP method '{m}' is strictly prohibited in confluence-wiki-crawler."
        )

    def request(
        self,
        method: str,
        path_or_url: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Any] = None,
        files: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        stream: bool = False,
        allow_redirects: bool = True,
    ) -> requests.Response:
        """Executes a strictly validated HTTP request against Confluence."""
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            url = path_or_url
        else:
            url = f"{self.base_url}/{path_or_url.lstrip('/')}"

        self._validate_scoped_write(method, url, json_body, files, data)

        if method.upper() != "GET" and hasattr(self, "_page_cache"):
            self._page_cache.clear()

        throttle_ms = float(getattr(self, "throttle_ms", 0.0) or 0.0)
        if throttle_ms > 0.0:
            last_ts = float(getattr(self, "_last_request_ts", 0.0) or 0.0)
            elapsed = time.time() - last_ts
            wait_sec = (throttle_ms / 1000.0) - elapsed
            if wait_sec > 0.0:
                time.sleep(wait_sec)
        self._last_request_ts = time.time()

        headers: Dict[str, str] = {}
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        resp = self.session.request(
            method=method.upper(),
            url=url,
            params=params,
            json=json_body,
            headers=headers,
            timeout=self.timeout,
            verify=self.verify_ssl,
            stream=stream,
            allow_redirects=allow_redirects,
        )
        if not resp.ok:
            snippet = ""
            try:
                snippet = resp.text[:600]
            except Exception:
                pass
            hint = ""
            if resp.status_code == 403:
                if "Unable to purge content" in snippet:
                    hint = (
                        " [SERVER PERMISSION HINT: Confluence Server refused comment deletion because "
                        "'Comments -> Delete' is disabled in Space Permissions (or the own comment already has replies). "
                        "Do NOT retry; use 'like <comment_id>' or 'add-comment <page_id> --parent-comment-id <comment_id>' "
                        "to mark the instruction comment as DONE instead.]"
                    )
                elif "Unable to trash content" in snippet:
                    hint = (
                        " [SERVER PERMISSION HINT: Confluence Server refused blogpost deletion because "
                        "'Blog -> Delete' and 'Delete Own' are disabled in Space Permissions. "
                        "Do NOT retry; use 'update-blogpost <blogpost_id>' to revise or mark your blog post instead.]"
                    )
                elif "Could not update Content" in snippet:
                    hint = (
                        " [SERVER PERMISSION HINT: Confluence Server refused content update (HTTP 403). "
                        "Pages are read-only and only the crawler's own comments/blogposts can be edited. Do NOT retry.]"
                    )
            raise RuntimeError(
                f"Confluence API {method.upper()} {url} failed with HTTP {resp.status_code}: {snippet}{hint}"
            )
        return resp

    def get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        parsed_req_path = urllib.parse.urlparse(str(path)).path.rstrip("/")
        if params and "cql" in params and parsed_req_path.endswith("/rest/api/content"):
            raise ValueError(
                "Confluence REST endpoint '/rest/api/content' silently ignores the 'cql' parameter and returns "
                "unfiltered pages across spaces. Use '/rest/api/content/search' or '/rest/api/search' "
                "(or the CLI 'search' / 'recent-edits' commands) when querying with CQL."
            )
        if self._is_space_guard_active():
            m_exp = re.search(r"/rest/experimental/content/(\d+)", str(path))
            if m_exp:
                exp_cid = m_exp.group(1)
                space_key = self._get_content_space_key(exp_cid)
                self._assert_space_readable(space_key, f"access experimental content endpoint for ID {exp_cid}")
        return self.request("GET", path, params=params).json()

    def get_paginated(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        max_items: int = 100,
        page_size: int = 50,
        stop_when: Optional[Callable[[Dict[str, Any]], bool]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Paginates a Confluence REST collection endpoint using the server's actual returned item count
        (`len(results)` and `data.get('limit')`) rather than assuming the requested `batch_limit` was
        honored (handling Confluence Data Center endpoints that cap `limit=100` down to `50`).
        Supports `stop_when(item)` for early-exit pagination on newest-first endpoints (such as
        `get_version_list(since=...)`) to minimize server load.
        """
        collected: List[Dict[str, Any]] = []
        query = dict(params or {})
        start = int(query.get("start", 0))
        while len(collected) < max_items:
            batch_limit = min(page_size, max_items - len(collected))
            query["start"] = start
            query["limit"] = batch_limit
            data = self.get_json(path, params=query)
            results = data.get("results", [])
            if not results:
                break
            stop_early = False
            for item in results:
                collected.append(item)
                if stop_when is not None and stop_when(item):
                    stop_early = True
            if stop_early:
                break
            links = data.get("_links") or {}
            has_next_link = "next" in links
            server_limit = int(data.get("limit") or batch_limit)
            if not has_next_link:
                # If the server explicitly capped `limit` below `batch_limit` (e.g. requested 100, capped at 50)
                # and returned a full `server_limit` batch without a `_links` block, continue paging; otherwise stop.
                if "limit" not in data or len(results) < server_limit or "_links" in data:
                    break
            start += len(results)
        return collected[:max_items]

    # --- High-Level Read Operations ---

    def whoami(self) -> Dict[str, Any]:
        return self.get_json("/rest/api/user/current", params={"expand": "status"})

    def resolve_url(self, url: str) -> Dict[str, Any]:
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)

        if "pageId" in qs and qs["pageId"]:
            return self.get_page(page_id=qs["pageId"][0], expand="space,version,ancestors")

        m_tiny = re.search(r"/x/([A-Za-z0-9_-]+)", parsed.path)
        if m_tiny:
            tiny_code = m_tiny.group(1)
            decoded_id = decode_tiny_link(tiny_code)
            try:
                page = self.get_page(page_id=str(decoded_id), expand="space,version,ancestors")
                page["_resolved_from_tiny_code"] = tiny_code
                return page
            except PermissionError:
                raise
            except Exception:
                resp = self.request("GET", url, allow_redirects=True)
                if resp.url != url:
                    return self.resolve_url(resp.url)
                raise

        m_disp = re.search(r"/display/([^/]+)/([^/?#]+)", parsed.path)
        if m_disp:
            space_key = urllib.parse.unquote(m_disp.group(1))
            title = urllib.parse.unquote_plus(m_disp.group(2))
            return self.get_page_by_title(space_key=space_key, title=title, expand="space,version,ancestors")

        m_spaces = re.search(r"/spaces/([^/]+)/pages/(\d+)", parsed.path)
        if m_spaces:
            space_key = urllib.parse.unquote(m_spaces.group(1))
            self._assert_space_readable(space_key, f"resolve URL in space {space_key!r}")
            return self.get_page(page_id=m_spaces.group(2), expand="space,version,ancestors")

        m_att = re.search(r"/download/(?:attachments|thumbnails)/(\d+)/([^/?#]+)", parsed.path)
        if m_att:
            page_id = m_att.group(1)
            filename = urllib.parse.unquote(m_att.group(2))
            if self._is_space_guard_active():
                space_key = self._get_content_space_key(page_id)
                self._assert_space_readable(space_key, f"resolve attachment on page {page_id}")
            atts = self.get_json(
                f"/rest/api/content/{page_id}/child/attachment",
                params={"filename": filename, "expand": "version,container,metadata.mediaType"},
            )
            return {
                "resolved_type": "attachment",
                "page_id": page_id,
                "filename": filename,
                "matches": atts.get("results", []),
            }

        raise ValueError(f"Unrecognized Confluence URL structure: {url}")

    def get_page(
        self,
        page_id: str,
        expand: str = "body.storage,version,space,ancestors,metadata.labels",
        version: Optional[int] = None,
    ) -> Dict[str, Any]:
        clean_pid = str(page_id).strip()
        expand_parts = [p.strip() for p in expand.split(",") if p.strip()]
        if "space" not in expand_parts:
            expand_parts.append("space")
        norm_expand = ",".join(expand_parts)
        ver_int = int(version) if version is not None else None
        cache_key = (clean_pid, norm_expand, ver_int)
        page_cache: Optional[Dict[Tuple[str, str, Optional[int]], Dict[str, Any]]] = getattr(self, "_page_cache", None)
        if isinstance(page_cache, dict) and cache_key in page_cache:
            cached_page = page_cache[cache_key]
            s_key = self._extract_space_key_from_obj(cached_page)
            if self._is_space_guard_active() and (s_key or getattr(self, "space_policy_configured", False) is False):
                self._assert_space_readable(s_key, f"read page/content ID {page_id}")
            return cached_page

        params: Dict[str, Any] = {"expand": norm_expand}
        if ver_int is not None:
            params["version"] = ver_int
        page = self.get_json(f"/rest/api/content/{clean_pid}", params=params)
        if ver_int is not None and isinstance(page, dict):
            # On Confluence Data Center, `GET /rest/api/content/{id}?version=N` returns the historical
            # revision's internal CONTENT row ID (and `?pageId=<historical_content_id>` in `_links.webui`).
            # Normalize `page["id"]` and `_links.webui` to the canonical `clean_pid` while preserving
            # the historical snapshot ID/link in metadata.
            returned_hist_id = str(page.get("id") or "").strip()
            if returned_hist_id and returned_hist_id != clean_pid:
                page["_historical_version_content_id"] = returned_hist_id
                page["id"] = clean_pid
            links_obj = page.get("_links")
            if isinstance(links_obj, dict):
                raw_webui = str(links_obj.get("webui") or "")
                m_webui_pid = re.search(r"([?&]pageId=)(\d+)", raw_webui)
                if m_webui_pid and m_webui_pid.group(2) != clean_pid:
                    page.setdefault("_historical_version_content_id", m_webui_pid.group(2))
                    links_obj["historical_version_webui"] = raw_webui
                    links_obj["webui"] = re.sub(r"([?&]pageId=)\d+", rf"\g<1>{clean_pid}", raw_webui)
        space_key = self._extract_space_key_from_obj(page)
        if space_key and hasattr(self, "_content_space_cache"):
            self._content_space_cache[clean_pid] = space_key
        if self._is_space_guard_active() and (space_key or getattr(self, "space_policy_configured", False) is False):
            self._assert_space_readable(space_key, f"read page/content ID {page_id}")
        if isinstance(page_cache, dict):
            page_cache[cache_key] = page
            actual_ver = page.get("version", {}).get("number")
            if ver_int is None and actual_ver is not None:
                try:
                    page_cache[(clean_pid, norm_expand, int(actual_ver))] = page
                except (TypeError, ValueError):
                    pass
        return page

    def get_page_by_title(
        self,
        space_key: str,
        title: str,
        expand: str = "body.storage,version,space,ancestors,metadata.labels",
    ) -> Dict[str, Any]:
        self._assert_space_readable(space_key, f"read page {title!r} in space {space_key!r}")
        data = self.get_json(
            "/rest/api/content",
            params={"spaceKey": space_key, "title": title, "expand": expand, "limit": 5},
        )
        results = data.get("results", [])
        if not results:
            raise LookupError(f"No page found in space '{space_key}' with exact title '{title}'.")
        page = results[0]
        if page.get("id") and hasattr(self, "_content_space_cache"):
            self._content_space_cache[str(page["id"])] = space_key
        return page

    def get_version_list(
        self,
        content_id: str,
        max_items: int = 50,
        since: Optional[str] = None,
        until: Optional[str] = None,
        history_obj: Optional[Dict[str, Any]] = None,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Retrieves the historical version list for `content_id` using a 3-tier resolver:
          - Tier 1 (`"rest_api_version"`): `GET /rest/api/content/{id}/version`
          - Tier 2 (`"rest_experimental_version"`): `GET /rest/experimental/content/{id}/version`
            (supported on Confluence Data Center 10.x where `/rest/api/content/{id}/version` returns 404)
          - Tier 3 (`"version_query_param_probe"`): Fallback using `/rest/api/content/{id}/history` +
            `GET /rest/api/content/{id}?version=N` (with binary search over timestamps when `since`/`until` are set).

        Rules:
          - Enforces the local space guard BEFORE probing any version endpoint.
          - Falls back to the next tier ONLY on HTTP 404; never falls back on HTTP 401/403 or `PermissionError`.
          - Caches the winning tier per client session (`self._version_endpoint_tier`).
          - Uses early-exit pagination (`stop_when`) when `since` is set so newest-first version paging stops
            immediately once a version's timestamp predates `since`, minimizing server load on high-revision pages.
          - Normalizes version order to newest-first (`number` descending) across all tiers and returns
            `{"version_source": <tier>, "versions": [...]}`.
        """
        if limit is not None:
            max_items = int(limit)
        clean_id = str(content_id).strip()
        if self._is_space_guard_active():
            space_key = self._get_content_space_key(clean_id)
            self._assert_space_readable(space_key, f"read version history of content ID {clean_id}")

        since_epoch = _parse_timestamp_to_epoch(since, is_until_bound=False) if since else None
        until_epoch = _parse_timestamp_to_epoch(until, is_until_bound=True) if until else None

        cached_tier = getattr(self, "_version_endpoint_tier", None)
        if cached_tier == "rest_experimental_version":
            tier_sequence = ["rest_experimental_version", "version_query_param_probe"]
        elif cached_tier == "version_query_param_probe":
            tier_sequence = ["version_query_param_probe"]
        else:
            tier_sequence = ["rest_api_version", "rest_experimental_version", "version_query_param_probe"]

        def _stop_at_older_than_since(raw_v: Dict[str, Any]) -> bool:
            if since_epoch is None:
                return False
            norm_v = _normalize_version_entry(raw_v)
            w_val = norm_v.get("when")
            if not w_val:
                return False
            try:
                return _parse_timestamp_to_epoch(str(w_val)) < since_epoch
            except Exception:
                return False

        for tier in tier_sequence:
            if tier in ("rest_api_version", "rest_experimental_version"):
                endpoint = (
                    f"/rest/api/content/{clean_id}/version"
                    if tier == "rest_api_version"
                    else f"/rest/experimental/content/{clean_id}/version"
                )
                fetch_limit = max(max_items, 1000) if (since_epoch is not None or until_epoch is not None) else max_items
                try:
                    try:
                        raw_items = self.get_paginated(
                            endpoint,
                            max_items=fetch_limit,
                            stop_when=_stop_at_older_than_since if since_epoch is not None else None,
                        )
                    except TypeError:
                        raw_items = self.get_paginated(endpoint, max_items=fetch_limit)
                except Exception as exc:
                    if _is_http_404_error(exc):
                        continue
                    raise
                self._version_endpoint_tier = tier
                normalized = [_normalize_version_entry(v) for v in raw_items]
                normalized.sort(key=lambda item: (item.get("number") or 0), reverse=True)
                if since_epoch is not None or until_epoch is not None:
                    filtered: List[Dict[str, Any]] = []
                    for item in normalized:
                        w = item.get("when")
                        if not w:
                            continue
                        try:
                            ep = _parse_timestamp_to_epoch(str(w))
                        except Exception:
                            continue
                        if since_epoch is not None and ep < since_epoch:
                            continue
                        if until_epoch is not None and ep > until_epoch:
                            continue
                        filtered.append(item)
                    normalized = filtered
                return {
                    "version_source": tier,
                    "versions": normalized[:max_items],
                }

            if tier == "version_query_param_probe":
                self._version_endpoint_tier = "version_query_param_probe"
                probed = self._probe_versions_via_query_param(
                    clean_id=clean_id,
                    max_items=max_items,
                    since_epoch=since_epoch,
                    until_epoch=until_epoch,
                    history_obj=history_obj,
                )
                return {
                    "version_source": "version_query_param_probe",
                    "versions": probed,
                }

        return {"version_source": "version_query_param_probe", "versions": []}

    def _probe_versions_via_query_param(
        self,
        clean_id: str,
        max_items: int = 50,
        since_epoch: Optional[float] = None,
        until_epoch: Optional[float] = None,
        history_obj: Optional[Dict[str, Any]] = None,
        boundaries_only: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Tier-3 fallback when both `/rest/api/content/{id}/version` and
        `/rest/experimental/content/{id}/version` return HTTP 404.
        Uses `/rest/api/content/{id}/history` + `GET /rest/api/content/{id}?version=N`
        (and binary-searches over `?version=N` timestamps when `since_epoch` / `until_epoch` are provided).
        """
        ver_cache: Dict[int, Dict[str, Any]] = {}
        latest_num: Optional[int] = None

        def _seed_from_history(h_obj: Dict[str, Any]) -> None:
            nonlocal latest_num
            for key in ("lastUpdated", "previousVersion"):
                node = h_obj.get(key)
                if isinstance(node, dict) and node.get("number") is not None:
                    norm = _normalize_version_entry(node)
                    v_n = norm.get("number")
                    if v_n is not None:
                        ver_cache[v_n] = norm
                        if key == "lastUpdated" or latest_num is None or v_n > latest_num:
                            latest_num = v_n

        if isinstance(history_obj, dict):
            _seed_from_history(history_obj)

        if latest_num is None:
            try:
                hist_fetched = self.get_json(
                    f"/rest/api/content/{clean_id}/history",
                    params={"expand": "lastUpdated,previousVersion"},
                )
                if isinstance(hist_fetched, dict):
                    _seed_from_history(hist_fetched)
            except Exception as exc:
                if not _is_http_404_error(exc):
                    raise

        if latest_num is None:
            latest_page = self.get_page(clean_id, expand="version,space")
            norm_latest = _normalize_version_entry(latest_page.get("version") or latest_page)
            latest_num = int(norm_latest.get("number") or 1)
            ver_cache[latest_num] = norm_latest

        def _get_ver(v_num: int) -> Dict[str, Any]:
            if v_num not in ver_cache:
                p = self.get_page(clean_id, expand="version,space", version=v_num)
                ver_cache[v_num] = _normalize_version_entry(p.get("version") or p)
                if ver_cache[v_num].get("number") is None:
                    ver_cache[v_num]["number"] = v_num
            return ver_cache[v_num]

        def _ver_epoch(v_num: int) -> Optional[float]:
            w = _get_ver(v_num).get("when")
            if not w:
                return None
            try:
                return _parse_timestamp_to_epoch(str(w))
            except Exception:
                return None

        # Determine upper version bound `hi_ver` (via binary search if `until_epoch` is set)
        if until_epoch is not None:
            ep_latest = _ver_epoch(latest_num)
            if ep_latest is not None and ep_latest <= until_epoch:
                hi_ver = latest_num
            else:
                ep_first = _ver_epoch(1)
                if ep_first is not None and ep_first > until_epoch:
                    return []
                lo, hi = 1, latest_num
                best_hi = 1
                while lo <= hi:
                    mid = (lo + hi) // 2
                    ep_mid = _ver_epoch(mid)
                    if ep_mid is not None and ep_mid <= until_epoch:
                        best_hi = mid
                        lo = mid + 1
                    else:
                        hi = mid - 1
                hi_ver = best_hi
        else:
            hi_ver = latest_num

        # Determine lower version bound `lo_ver` (via binary search if `since_epoch` is set)
        if since_epoch is not None:
            ep_hi = _ver_epoch(hi_ver)
            if ep_hi is not None and ep_hi < since_epoch:
                return []
            ep_first = _ver_epoch(1)
            if ep_first is not None and ep_first >= since_epoch:
                lo_ver = 1
            else:
                lo, hi = 1, hi_ver
                best_lo = hi_ver
                while lo <= hi:
                    mid = (lo + hi) // 2
                    ep_mid = _ver_epoch(mid)
                    if ep_mid is not None and ep_mid >= since_epoch:
                        best_lo = mid
                        hi = mid - 1
                    else:
                        lo = mid + 1
                lo_ver = best_lo
        else:
            lo_ver = max(1, hi_ver - max_items + 1)

        if lo_ver > hi_ver:
            return []

        if boundaries_only:
            boundary_nums = [hi_ver] if hi_ver == lo_ver else [hi_ver, lo_ver]
            return [_get_ver(n) for n in boundary_nums]

        stop_ver = max(lo_ver, hi_ver - max_items + 1)
        collected = [_get_ver(v_num) for v_num in range(hi_ver, stop_ver - 1, -1)]
        collected.sort(key=lambda item: (item.get("number") or 0), reverse=True)
        return collected[:max_items]

    def resolve_version_range_by_time(
        self,
        content_id: str,
        since: Optional[str] = None,
        until: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Resolves the `(from_version, to_version)` range for `diff --since ... [--until ...]`:
          - Finds the earliest version (`first_in_window`) and latest version (`last_in_window`)
            modified within `[since, until]`.
          - Sets `from_version = max(1, first_in_window - 1)` (the baseline immediately preceding
            the window's first edit) and `to_version = last_in_window` so the diff captures the
            full net change of all edits made in `[since, until]`.
          - Uses `get_version_list` when Tier 1 or Tier 2 is available, or Tier-3 binary search
            (`boundaries_only=True`) over `?version=N` timestamps when Tier 1 & Tier 2 return 404.
        """
        clean_id = str(content_id).strip()
        if self._is_space_guard_active():
            space_key = self._get_content_space_key(clean_id)
            self._assert_space_readable(space_key, f"resolve version range for content ID {clean_id}")

        since_epoch = _parse_timestamp_to_epoch(since, is_until_bound=False) if since else None
        until_epoch = _parse_timestamp_to_epoch(until, is_until_bound=True) if until else None

        # If Tier 3 is already known to be the active tier, use binary search with boundaries_only=True
        if getattr(self, "_version_endpoint_tier", None) == "version_query_param_probe":
            bounds = self._probe_versions_via_query_param(
                clean_id=clean_id,
                max_items=200,
                since_epoch=since_epoch,
                until_epoch=until_epoch,
                boundaries_only=True,
            )
            ver_source = "version_query_param_probe"
        else:
            ver_res = self.get_version_list(clean_id, max_items=200, since=since, until=until)
            ver_source = ver_res["version_source"]
            bounds = ver_res["versions"]

        if not bounds:
            return {
                "version_source": ver_source,
                "matched_versions_count": 0,
                "first_version_in_window": None,
                "last_version_in_window": None,
                "from_version": None,
                "to_version": None,
            }

        nums = [int(v["number"]) for v in bounds if v.get("number") is not None]
        first_in_window = min(nums)
        last_in_window = max(nums)
        count = (last_in_window - first_in_window + 1) if ver_source == "version_query_param_probe" else len(nums)
        return {
            "version_source": ver_source,
            "matched_versions_count": count,
            "first_version_in_window": first_in_window,
            "last_version_in_window": last_in_window,
            "from_version": max(1, first_in_window - 1),
            "to_version": last_in_window,
        }

    def _get_configured_default_page_entries(self) -> List[str]:
        raw_list = getattr(self, "default_pages", None)
        if raw_list:
            return [str(x).strip() for x in raw_list if str(x).strip()]
        raw_single = (getattr(self, "default_page", "") or "").strip()
        if raw_single:
            return _parse_default_page_list(raw_single)
        return []

    def _extract_default_page_ids_for_cql(self, fallback_to_space_homepage: bool = False) -> List[str]:
        """
        Resolves configured `CONFLUENCE_DEFAULT_PAGE` entries to numeric page IDs for CQL
        `(id IN (...) OR ancestor IN (...))` queries.
        Uses offline extraction for numeric IDs, `?pageId=...`, `/pages/<id>`, and `/x/<code>` tiny links
        so initial searches do not require an extra HTTP roundtrip before running CQL!
        When `fallback_to_space_homepage=True` (used by `--default-pages-only` when `CONFLUENCE_DEFAULT_PAGE`
        is blank), falls back to resolving the homepage ID of the first default space.
        """
        entries = self._get_configured_default_page_entries()
        page_ids: List[str] = []
        for entry in entries:
            clean = entry.strip()
            if not clean:
                continue
            if clean.isdigit():
                page_ids.append(clean)
                continue
            parsed = urllib.parse.urlparse(clean)
            qs = urllib.parse.parse_qs(parsed.query)
            if "pageId" in qs and qs["pageId"] and str(qs["pageId"][0]).isdigit():
                page_ids.append(str(qs["pageId"][0]))
                continue
            m_spaces = re.search(r"/spaces/[^/]+/pages/(\d+)", parsed.path)
            if m_spaces:
                page_ids.append(m_spaces.group(1))
                continue
            m_tiny = re.search(r"(?:^|/x/)([A-Za-z0-9_-]+)$", parsed.path or clean)
            if m_tiny and ("/x/" in clean or not clean.startswith("http")):
                try:
                    decoded = decode_tiny_link(m_tiny.group(1))
                    if decoded > 0:
                        page_ids.append(str(decoded))
                        continue
                except Exception:
                    pass
            # Fallback to live URL resolution (e.g. /display/SPACE/Title)
            try:
                full_url = clean if clean.startswith(("http://", "https://")) else f"{self.base_url}/{clean.lstrip('/')}"
                resolved = self.resolve_url(full_url)
                if resolved.get("id"):
                    page_ids.append(str(resolved["id"]))
            except Exception:
                pass
        if not page_ids and fallback_to_space_homepage:
            for item in self.resolve_default_pages(strict=False):
                if item.get("id") and str(item["id"]).isdigit():
                    page_ids.append(str(item["id"]))
        return list(dict.fromkeys(page_ids))

    def resolve_default_pages(self, strict: bool = False) -> List[Dict[str, Any]]:
        """
        Resolves all recommended starting page(s) for this skill:
          1. If `CONFLUENCE_DEFAULT_PAGE` (or `CONFLUENCE_DEFAULT_PAGES`) is configured in `access_settings.env`
             (one or more comma-separated numeric page IDs, `/x/...` tiny links, or full page URLs),
             resolves and returns each page's metadata.
          2. Otherwise, if `CONFLUENCE_DEFAULT_SPACES` (or `CONFLUENCE_ELEVATED_ACCESS_SPACES`) has at least
             one space key, fetches the homepage of the first default space.
        """
        entries = self._get_configured_default_page_entries()
        resolved_list: List[Dict[str, Any]] = []
        if entries:
            for cfg_page in entries:
                try:
                    if cfg_page.isdigit():
                        page = self.get_page(cfg_page, expand="space,version,ancestors")
                        source_desc = "CONFLUENCE_DEFAULT_PAGE (page_id)"
                    else:
                        if cfg_page.startswith("http://") or cfg_page.startswith("https://"):
                            full_url = cfg_page
                        elif cfg_page.startswith("/"):
                            full_url = f"{self.base_url}{cfg_page}"
                        else:
                            full_url = f"{self.base_url}/x/{cfg_page}"
                        page = self.resolve_url(full_url)
                        source_desc = "CONFLUENCE_DEFAULT_PAGE (url)"
                    s_key = self._extract_space_key_from_obj(page)
                    webui = page.get("_links", {}).get("webui", "")
                    resolved_list.append({
                        "id": str(page.get("id") or ""),
                        "title": page.get("title"),
                        "space": s_key,
                        "space_access_mode": self.get_space_access_mode(s_key) if s_key else None,
                        "url": f"{self.base_url}{webui}" if webui else None,
                        "source": source_desc,
                    })
                except Exception as e:
                    if strict:
                        raise
                    resolved_list.append({
                        "configured_default_page": cfg_page,
                        "error": f"Could not resolve CONFLUENCE_DEFAULT_PAGE ({cfg_page!r}): {e}",
                    })
            return resolved_list

        def_spaces = self._get_default_search_spaces_list()
        if def_spaces:
            primary_space = def_spaces[0]
            try:
                sp_data = self.get_json(f"/rest/api/space/{urllib.parse.quote(primary_space)}", params={"expand": "homepage"})
                hp = sp_data.get("homepage") or {}
                if hp.get("id"):
                    hp_id = str(hp["id"])
                    if hasattr(self, "_content_space_cache"):
                        self._content_space_cache[hp_id] = primary_space
                    webui = hp.get("_links", {}).get("webui") or sp_data.get("_links", {}).get("webui", "")
                    return [{
                        "id": hp_id,
                        "title": hp.get("title") or sp_data.get("name"),
                        "space": sp_data.get("key") or primary_space,
                        "space_access_mode": self.get_space_access_mode(primary_space),
                        "url": f"{self.base_url}{webui}" if webui else None,
                        "source": f"homepage of first default space ({primary_space})",
                    }]
            except Exception as e:
                if strict:
                    raise ValueError(
                        f"CONFLUENCE_DEFAULT_PAGE is not set and could not resolve homepage of default space {primary_space!r}: {e}"
                    ) from e
                return []

        if strict:
            raise ValueError(
                f"No default starting page is available: set CONFLUENCE_DEFAULT_PAGE (or CONFLUENCE_ELEVATED_ACCESS_SPACES) in '{DEFAULT_SETTINGS_FILE}'."
            )
        return []

    def resolve_default_page(self, strict: bool = False) -> Optional[Dict[str, Any]]:
        pages = self.resolve_default_pages(strict=strict)
        return pages[0] if pages else None

    # --- High-Level Scoped Write Operations ---

    def create_blogpost(
        self,
        space_key: str,
        title: str,
        storage_body: str,
    ) -> Dict[str, Any]:
        """Creates a new Blog Post (`type='blogpost'`) in the specified Confluence space."""
        self._assert_space_writable(space_key, f"create blogpost in space {space_key!r}")
        payload: Dict[str, Any] = {
            "type": "blogpost",
            "title": title,
            "space": {"key": space_key},
            "body": {
                "storage": {
                    "value": storage_body,
                    "representation": "storage",
                }
            },
        }
        return self.request("POST", "/rest/api/content", json_body=payload).json()

    def update_own_blogpost(
        self,
        blogpost_id: str,
        title: Optional[str] = None,
        new_storage_body: Optional[str] = None,
        append_storage: Optional[str] = None,
        prepend_storage: Optional[str] = None,
        version_comment: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Programmatically loads the target blogpost from Confluence, verifies that:
          1. Its content `type` on the server is strictly `"blogpost"` (never `"page"`),
          2. Its space is in `CONFLUENCE_ELEVATED_ACCESS_SPACES` (not read-only), and
          3. It is OWNED/CREATED by the crawler (`history.createdBy` matches configured
             `CONFLUENCE_CRAWLER_USERNAME` and `CONFLUENCE_CRAWLER_NAME`),
        and ONLY THEN updates the blogpost.
        """
        existing = self._verify_own_blogpost_on_server(blogpost_id, action_label="update")
        cur_ver = int(existing.get("version", {}).get("number", 1))
        cur_storage = existing.get("body", {}).get("storage", {}).get("value", "")
        cur_title = existing.get("title", "")
        space_key = existing.get("space", {}).get("key")

        if new_storage_body is not None:
            updated_storage = new_storage_body
        else:
            updated_storage = cur_storage
            if prepend_storage:
                updated_storage = f"{prepend_storage}\n{updated_storage}"
            if append_storage:
                updated_storage = f"{updated_storage}\n{append_storage}"

        version_payload: Dict[str, Any] = {"number": cur_ver + 1}
        if version_comment:
            version_payload["message"] = version_comment

        payload: Dict[str, Any] = {
            "id": str(blogpost_id),
            "type": "blogpost",
            "title": title if title is not None else cur_title,
            "body": {
                "storage": {
                    "value": updated_storage,
                    "representation": "storage",
                }
            },
            "version": version_payload,
        }
        if space_key:
            payload["space"] = {"key": space_key}

        updated = self.request("PUT", f"/rest/api/content/{blogpost_id}", json_body=payload).json()
        updated["_programmatic_check"] = {
            "verified_blogpost_id": str(existing.get("id")),
            "verified_type": existing.get("type"),
            "verified_space": existing.get("_space_key"),
            "authorization_basis": "own_blogpost",
            "owner_check": existing.get("_owner_check"),
        }
        return updated

    def delete_own_blogpost(self, blogpost_id: str) -> Dict[str, Any]:
        """
        Programmatically loads the target blogpost from Confluence, verifies that:
          1. Its content `type` on the server is strictly `"blogpost"` (never `"page"`),
          2. Its space is in `CONFLUENCE_ELEVATED_ACCESS_SPACES` (not read-only), and
          3. It is OWNED/CREATED by the crawler (`history.createdBy` matches configured
             `CONFLUENCE_CRAWLER_USERNAME` and `CONFLUENCE_CRAWLER_NAME`),
        and ONLY THEN executes `DELETE /rest/api/content/{blogpost_id}`.
        """
        existing = self._verify_own_blogpost_on_server(blogpost_id, action_label="delete")
        self.request("DELETE", f"/rest/api/content/{blogpost_id}")
        return {
            "status": "blogpost_deleted",
            "blogpost_id": str(blogpost_id),
            "title": existing.get("title"),
            "space": existing.get("space", {}).get("key"),
            "programmatic_check": {
                "verified_blogpost_id": str(existing.get("id")),
                "verified_type": existing.get("type"),
                "verified_space": existing.get("_space_key"),
                "authorization_basis": "own_blogpost",
                "owner_check": existing.get("_owner_check"),
            },
        }

    def add_page_comment(
        self,
        page_id: str,
        storage_body: str,
        parent_comment_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Adds a comment (or threaded reply) to an existing Confluence page."""
        if self._is_space_guard_active():
            space_key = self._get_content_space_key(str(page_id))
            self._assert_space_writable(space_key, f"add comment on page ID {page_id}")
        payload: Dict[str, Any] = {
            "type": "comment",
            "container": {"id": str(page_id), "type": "page"},
            "body": {
                "storage": {
                    "value": storage_body,
                    "representation": "storage",
                }
            },
        }
        if parent_comment_id:
            payload["ancestors"] = [{"id": str(parent_comment_id)}]
        return self.request("POST", "/rest/api/content", json_body=payload).json()

    def update_own_comment(
        self,
        comment_id: str,
        new_storage_body: Optional[str] = None,
        mark_done: bool = False,
        status_label: str = "DONE",
        append_storage: Optional[str] = None,
        prepend_storage: Optional[str] = None,
        expected_page_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Programmatically loads the target comment from Confluence, verifies that:
          1. Its content `type` on the server is strictly `"comment"` (never `"page"` or `"blogpost"`),
          2. If `expected_page_id` is given, its parent page matches `expected_page_id`,
          3. Its space is in `CONFLUENCE_ELEVATED_ACCESS_SPACES` (not read-only), and
          4. The comment is OWNED/CREATED by the crawler (`history.createdBy`),
        and ONLY THEN allows updating the comment.
        """
        existing = self._verify_comment_editable_or_deletable_on_server(
            comment_id,
            action_label="update",
            expected_page_id=expected_page_id,
        )
        cur_ver = int(existing.get("version", {}).get("number", 1))
        cur_storage = existing.get("body", {}).get("storage", {}).get("value", "")
        container = existing.get("container", {})
        is_own = bool(existing.get("_is_own_comment"))

        if new_storage_body is not None:
            updated_storage = new_storage_body
        else:
            updated_storage = cur_storage
            if mark_done:
                status_macro = (
                    '<ac:structured-macro ac:name="status">'
                    '<ac:parameter ac:name="colour">Green</ac:parameter>'
                    f'<ac:parameter ac:name="title">{html.escape(status_label)}</ac:parameter>'
                    "</ac:structured-macro> "
                )
                if f">{html.escape(status_label)}</ac:parameter>" not in updated_storage:
                    updated_storage = f"<p>{status_macro}<strong>[{html.escape(status_label)}]</strong></p>\n{updated_storage}"
            if prepend_storage:
                updated_storage = f"{prepend_storage}\n{updated_storage}"
            if append_storage:
                updated_storage = f"{updated_storage}\n{append_storage}"

        payload: Dict[str, Any] = {
            "id": str(comment_id),
            "type": "comment",
            "container": {
                "id": str(container.get("id", "")),
                "type": container.get("type", "page"),
            },
            "body": {
                "storage": {
                    "value": updated_storage,
                    "representation": "storage",
                }
            },
            "version": {
                "number": cur_ver + 1,
            },
        }
        if existing.get("ancestors"):
            payload["ancestors"] = [{"id": str(existing["ancestors"][-1]["id"])}]

        updated = self.request("PUT", f"/rest/api/content/{comment_id}", json_body=payload).json()
        updated["_programmatic_check"] = {
            "verified_comment_id": str(existing.get("id")),
            "verified_type": existing.get("type"),
            "verified_space": existing.get("_space_key"),
            "verified_page_id": container.get("id"),
            "verified_page_title": container.get("title"),
            "authorization_basis": existing.get("_authorization_basis"),
            "is_own_comment": is_own,
            "owner_check": existing.get("_owner_check"),
            "matched_crawler_name": existing.get("_matched_crawler_name"),
        }
        return updated

    update_comment = update_own_comment
    update_mentioned_comment = update_own_comment

    def delete_mentioned_comment(
        self,
        comment_id: str,
        expected_page_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Programmatically loads the target comment from Confluence, verifies that:
          1. Its content `type` on the server is strictly `"comment"` (never `"page"` or `"blogpost"`),
          2. If `expected_page_id` is given, its parent page matches `expected_page_id`,
          3. Its space is in `CONFLUENCE_ELEVATED_ACCESS_SPACES` (not read-only), and
          4. EITHER the comment is OWNED by the crawler (`history.createdBy`) OR its body mentions
             the crawler (`CONFLUENCE_CRAWLER_NAME` / `CONFLUENCE_CRAWLER_USERNAME`),
        and ONLY THEN executes `DELETE /rest/api/content/{comment_id}`.
        """
        existing = self._verify_comment_editable_or_deletable_on_server(
            comment_id,
            action_label="delete",
            expected_page_id=expected_page_id,
        )
        raw_view = existing.get("body", {}).get("view", {}).get("value", "")
        raw_storage = existing.get("body", {}).get("storage", {}).get("value", "")
        excerpt = storage_to_markdown(raw_view or raw_storage)[:300]
        self.request("DELETE", f"/rest/api/content/{comment_id}")
        return {
            "status": "comment_deleted",
            "comment_id": str(comment_id),
            "programmatic_check": {
                "verified_comment_id": str(existing.get("id")),
                "verified_type": existing.get("type"),
                "verified_space": existing.get("_space_key"),
                "verified_page_id": existing.get("container", {}).get("id"),
                "verified_page_title": existing.get("container", {}).get("title"),
                "authorization_basis": existing.get("_authorization_basis"),
                "is_own_comment": bool(existing.get("_is_own_comment")),
                "owner_check": existing.get("_owner_check"),
                "matched_crawler_name": existing.get("_matched_crawler_name"),
                "deleted_comment_excerpt": excerpt,
            },
        }

    delete_comment = delete_mentioned_comment

    # --- Likes Operations (Can be used to mark a comment as "DONE" without deleting it) ---

    def get_likes(self, content_id: str) -> Dict[str, Any]:
        """
        Fetches all likes on a Confluence content item (`comment`, `page`, or `blogpost`) via
        `GET /rest/likes/1.0/content/{content_id}/likes` and checks whether the crawler has liked it.
        """
        clean_id = str(content_id).strip()
        if not clean_id.isdigit():
            raise ValueError(f"Invalid non-numeric content_id: '{content_id}'.")
        if self._is_space_guard_active():
            space_key = self._get_content_space_key(clean_id)
            self._assert_space_readable(space_key, f"read likes on content ID {clean_id}")
        payload = self.get_json(f"/rest/likes/1.0/content/{clean_id}/likes")
        crawler_liked, likes_list = is_liked_by_crawler(
            payload,
            crawler_username=self.crawler_username,
            crawler_display_name=self.crawler_display_name,
            crawler_names=self.crawler_names,
        )
        return {
            "content_id": clean_id,
            "count": len(likes_list),
            "liked_by_crawler": crawler_liked,
            "likes": likes_list,
        }

    def like_content(self, content_id: str) -> Dict[str, Any]:
        """
        Adds the crawler's 'Like' reaction to a Confluence content item (`comment`, `page`, or `blogpost`)
        via `POST /rest/likes/1.0/content/{content_id}/likes`.
        When used on a comment that mentions the crawler, this serves as a non-destructive 'DONE' marker.
        """
        clean_id = str(content_id).strip()
        if not clean_id.isdigit():
            raise PermissionError(f"[CRAWLER GUARD] Refusing to like invalid non-numeric content_id '{content_id}'.")
        resp = self.request("POST", f"/rest/likes/1.0/content/{clean_id}/likes", json_body={})
        try:
            payload = resp.json()
            crawler_liked, likes_list = is_liked_by_crawler(
                payload,
                crawler_username=self.crawler_username,
                crawler_display_name=self.crawler_display_name,
                crawler_names=self.crawler_names,
            )
        except Exception:
            summary = self.get_likes(clean_id)
            crawler_liked = summary["liked_by_crawler"]
            likes_list = summary["likes"]
        return {
            "status": "liked",
            "content_id": clean_id,
            "liked_by_crawler": True if crawler_liked else True,
            "total_likes": len(likes_list),
            "likes": likes_list,
        }

    def unlike_content(self, content_id: str) -> Dict[str, Any]:
        """
        Removes the crawler's 'Like' reaction from a Confluence content item (`comment`, `page`, or `blogpost`)
        via `DELETE /rest/likes/1.0/content/{content_id}/likes`.
        """
        clean_id = str(content_id).strip()
        if not clean_id.isdigit():
            raise PermissionError(f"[CRAWLER GUARD] Refusing to unlike invalid non-numeric content_id '{content_id}'.")
        self.request("DELETE", f"/rest/likes/1.0/content/{clean_id}/likes")
        try:
            summary = self.get_likes(clean_id)
            likes_list = summary["likes"]
        except Exception:
            likes_list = []
        return {
            "status": "unliked",
            "content_id": clean_id,
            "liked_by_crawler": False,
            "total_likes": len(likes_list),
            "likes": likes_list,
        }


# Convenience aliases for custom scripts importing this module
ConfluenceClient = GuardedCrawlerConfluenceClient
BlogCommentConfluenceClient = GuardedCrawlerConfluenceClient
ReadOnlyConfluenceClient = GuardedCrawlerConfluenceClient


# =============================================================================
# 5. CLI SUBCOMMAND HANDLERS
# =============================================================================

def cmd_whoami(client: GuardedCrawlerConfluenceClient, args: argparse.Namespace) -> None:
    user = client.whoami()
    token_matches_env, match_details = is_content_owned_by_crawler(
        {"history": {"createdBy": user}},
        crawler_username=client.crawler_username,
        crawler_display_name=client.crawler_display_name,
        crawler_names=client.crawler_names,
    )
    ro_spaces = ["*"] if getattr(client, "readonly_wildcard", False) else getattr(client, "readonly_spaces", [])
    rw_spaces = ["*"] if client._get_elevated_wildcard() else client._get_elevated_spaces_list()
    def_spaces = client._get_default_search_spaces_list()
    start_pages = client.resolve_default_pages(strict=False)
    print(json.dumps({
        "authenticated": True,
        "base_url": client.base_url,
        "base_url_source": getattr(client, "base_url_source", None),
        "auth_type": client.auth_type,
        "token_source": client.token_source,
        "configured_crawler_username": client.crawler_username or "(not configured yet)",
        "configured_crawler_display_name": client.crawler_display_name or "(not configured yet)",
        "configured_crawler_names": client.crawler_names,
        "crawler_name_source": client.crawler_name_source or "(not configured yet)",
        "token_matches_configured_crawler": token_matches_env,
        "token_identity_check": match_details,
        "configured_elevated_access_spaces": rw_spaces,
        "configured_readonly_spaces": ro_spaces,
        "configured_default_search_spaces": def_spaces,
        "configured_default_page": getattr(client, "default_page", "") or "(auto: homepage of first default space)",
        "recommended_start_page": start_pages[0] if start_pages else None,
        "recommended_start_pages": start_pages,
        "space_policy_configured": getattr(client, "space_policy_configured", False),
        "space_policy_source": getattr(client, "space_policy_source", "") or "(not configured yet)",
        "skill_scope": (
            "space-guarded (CONFLUENCE_ELEVATED_ACCESS_SPACES vs CONFLUENCE_READONLY_SPACES) + "
            "progressive-search (CONFLUENCE_DEFAULT_PAGE subtrees -> CONFLUENCE_DEFAULT_SPACES -> --all-spaces/--wider) + "
            "read-only-pages + create-blogpost + guarded-own-blogpost-edit-delete "
            "+ add-comment-and-reply + guarded-own-comment-edit + guarded-own-or-mentioned-comment-delete + like-unlike"
        ),
        "username": user.get("username"),
        "displayName": user.get("displayName"),
        "userKey": user.get("userKey"),
        "type": user.get("type"),
    }, indent=2))


def cmd_resolve_url(client: GuardedCrawlerConfluenceClient, args: argparse.Namespace) -> None:
    if args.offline_tiny_only:
        m = re.search(r"(?:/x/)?([A-Za-z0-9_-]+)$", args.url.strip())
        if not m:
            raise ValueError(f"Could not extract tiny code from: {args.url}")
        code = m.group(1)
        page_id = decode_tiny_link(code)
        print(json.dumps({"tiny_code": code, "page_id": page_id, "reencoded": encode_tiny_link(page_id)}, indent=2))
        return

    res = client.resolve_url(args.url)
    space_key = res.get("space", {}).get("key")
    print(json.dumps({
        "id": res.get("id") or res.get("page_id"),
        "type": res.get("type") or res.get("resolved_type"),
        "title": res.get("title") or res.get("filename"),
        "space": space_key,
        "space_access_mode": client.get_space_access_mode(space_key) if space_key else None,
        "version": res.get("version", {}).get("number"),
        "webui": f"{client.base_url}{res.get('_links', {}).get('webui', '')}" if res.get("_links") else None,
        "tinyui": f"{client.base_url}{res.get('_links', {}).get('tinyui', '')}" if res.get("_links") else None,
    }, indent=2))


def cmd_spaces(client: GuardedCrawlerConfluenceClient, args: argparse.Namespace) -> None:
    """
    Lists Confluence spaces visible to the crawler token on the server (`GET /rest/api/space` —
    space metadata only, never page content) and annotates each with its local `access_mode`:
      - `"elevated-access"`: in `CONFLUENCE_ELEVATED_ACCESS_SPACES` (read + guarded writes)
      - `"read-only"`: in `CONFLUENCE_READONLY_SPACES` (strictly read-only)
      - `"blocked_by_local_settings"`: visible to the crawler token on the server, but not listed in
        `access_settings.env`, so reading/searching content is blocked locally until added to `access_settings.env`.
    """
    params: Dict[str, Any] = {"expand": "description.plain,homepage"}
    if args.type != "all":
        params["type"] = args.type
    if args.status != "all":
        params["status"] = args.status

    spaces = client.get_paginated("/rest/api/space", params=params, max_items=args.limit)
    rows = []
    elevated_keys: List[str] = []
    readonly_keys: List[str] = []
    blocked_keys: List[str] = []
    default_search_set = {s.upper() for s in client._get_default_search_spaces_list()}

    for s in spaces:
        s_key = s.get("key")
        raw_mode = client.get_space_access_mode(s_key)
        if raw_mode in ("elevated-access", "default-access"):
            display_mode = "elevated-access"
            if s_key:
                elevated_keys.append(s_key)
        elif raw_mode == "read-only":
            display_mode = "read-only"
            if s_key:
                readonly_keys.append(s_key)
        else:
            display_mode = "blocked_by_local_settings"
            if s_key:
                blocked_keys.append(s_key)
            if getattr(args, "only_allowed", False):
                continue

        rows.append({
            "key": s_key,
            "name": s.get("name"),
            "type": s.get("type"),
            "status": s.get("status"),
            "access_mode": display_mode,
            "in_default_search_scope": bool(s_key and s_key.upper() in default_search_set),
            "readable_by_skill": display_mode in ("elevated-access", "read-only"),
            "writable_by_skill": display_mode == "elevated-access",
            "homepage_id": s.get("homepage", {}).get("id"),
            "description": (s.get("description", {}).get("plain", {}).get("value") or "").strip()[:200],
            "url": f"{client.base_url}{s.get('_links', {}).get('webui', '')}",
        })
    print(json.dumps({
        "count": len(rows),
        "elevated_access_spaces": elevated_keys,
        "readonly_spaces": readonly_keys,
        "default_search_spaces": client._get_default_search_spaces_list(),
        "server_visible_but_blocked_by_settings": blocked_keys,
        "how_to_grant_access_hint": (
            "Spaces with access_mode='blocked_by_local_settings' are already visible to the crawler token on the "
            "Confluence server, but reading/searching page content is blocked locally until the user adds the space key "
            f"to CONFLUENCE_READONLY_SPACES or CONFLUENCE_ELEVATED_ACCESS_SPACES in '{DEFAULT_SETTINGS_FILE}'. "
            "If a space does not appear in this list at all, the crawler account also needs 'View' permission "
            "granted by a Space Admin on the Confluence server."
        ),
        "spaces": rows,
    }, indent=2))


def cmd_get_page(client: GuardedCrawlerConfluenceClient, args: argparse.Namespace) -> None:
    body_field = "body.view" if args.format == "view" else "body.storage"
    expand = f"{body_field},version,space,ancestors,metadata.labels"

    if getattr(args, "default", False):
        def_info = client.resolve_default_page(strict=True)
        assert def_info is not None
        page = client.get_page(def_info["id"], expand=expand, version=args.version)
    elif args.url:
        page = client.resolve_url(args.url)
        page = client.get_page(page["id"], expand=expand, version=args.version)
    elif args.id:
        page = client.get_page(args.id, expand=expand, version=args.version)
    elif args.space and args.title:
        page = client.get_page_by_title(args.space, args.title, expand=expand)
        if args.version is not None:
            page = client.get_page(page["id"], expand=expand, version=args.version)
    else:
        raise ValueError("Specify --default, --id <PAGE_ID>, --url <URL>, or both --space <KEY> and --title <TITLE>.")

    raw_body = (
        page.get("body", {}).get("view", {}).get("value")
        if args.format == "view"
        else page.get("body", {}).get("storage", {}).get("value", "")
    )
    ancestors = [f"{a.get('title')} ({a.get('id')})" for a in page.get("ancestors", [])]
    labels = [lbl.get("name") for lbl in page.get("metadata", {}).get("labels", {}).get("results", [])]
    ver = page.get("version", {})
    space_key = page.get("space", {}).get("key")
    page_id_str = str(page.get("id") or "")

    # Optional --with-images <DIR>: download referenced attachments/images locally and rewrite attachment:// links
    downloaded_images: List[Dict[str, Any]] = []
    images_dir_arg = getattr(args, "with_images", None)
    if images_dir_arg and page_id_str:
        img_dir = Path(images_dir_arg).resolve()
        img_dir.mkdir(parents=True, exist_ok=True)
        referenced_files = set(re.findall(r"ri:filename=[\"']([^\"']+)[\"']", raw_body))
        atts = client.get_paginated(
            f"/rest/api/content/{page_id_str}/child/attachment",
            params={"expand": "version,metadata.mediaType,extensions"},
            max_items=200,
        )
        for a in atts:
            fname = a.get("title") or ""
            ext = a.get("extensions", {})
            mtype = str(ext.get("mediaType") or a.get("metadata", {}).get("mediaType") or "").lower()
            dl_link = a.get("_links", {}).get("download", "")
            if not fname or not dl_link:
                continue
            is_image_ext = fname.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".tiff", ".tif"))
            if fname not in referenced_files and not mtype.startswith("image/") and not is_image_ext:
                continue
            dl_url = f"{client.base_url}{dl_link}" if not dl_link.startswith("http") else dl_link
            target_file = img_dir / os.path.basename(fname)
            with client.request("GET", dl_url, stream=True) as r:
                with open(target_file, "wb") as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        if chunk:
                            f.write(chunk)
            downloaded_images.append({
                "filename": fname,
                "media_type": mtype,
                "saved_to": str(target_file),
            })

    if getattr(args, "todos", False):
        converted_for_tasks = storage_to_markdown(raw_body)
        tasks = extract_tasks_from_storage_or_markdown(raw_body, converted_for_tasks)
        open_tasks = [t for t in tasks if not t.get("checked")]
        completed_tasks = [t for t in tasks if t.get("checked")]
        todo_payload: Dict[str, Any] = {
            "page_id": page.get("id"),
            "title": page.get("title"),
            "space": space_key,
            "version": ver.get("number"),
            "last_modified": ver.get("when"),
            "total_tasks": len(tasks),
            "open_count": len(open_tasks),
            "open_tasks_count": len(open_tasks),
            "completed_count": len(completed_tasks),
            "completed_tasks_count": len(completed_tasks),
            "open_tasks": open_tasks,
            "completed_tasks": completed_tasks,
        }
        if downloaded_images:
            todo_payload["downloaded_images"] = downloaded_images
        out_str = json.dumps(todo_payload, indent=2)
    elif args.format == "json":
        if downloaded_images:
            page = dict(page)
            page["_downloaded_images"] = downloaded_images
        out_str = json.dumps(page, indent=2)
    elif args.format in ("storage", "view"):
        out_str = raw_body
    else:
        converted = storage_to_markdown(raw_body)
        for dl in downloaded_images:
            converted = converted.replace(f"(attachment://{dl['filename']})", f"({dl['saved_to']})")
        links_meta = page.get("_links", {}) if isinstance(page.get("_links"), dict) else {}
        canonical_url = f"{client.base_url}{links_meta.get('webui', '')}"
        hist_webui = links_meta.get("historical_version_webui")
        url_line = (
            f"- **URL:** {canonical_url} (historical v{ver.get('number')} snapshot: {client.base_url}{hist_webui})"
            if hist_webui
            else f"- **URL:** {canonical_url}"
        )
        header = [
            f"# {page.get('title')} (ID: {page.get('id')})",
            f"- **Type:** {page.get('type', 'page')}",
            f"- **Space:** {space_key} ({page.get('space', {}).get('name')}) [Access: {client.get_space_access_mode(space_key)}]",
            f"- **Version:** {ver.get('number')} by {ver.get('by', {}).get('displayName') or ver.get('by', {}).get('username')} at {ver.get('when')}",
            f"- **Ancestors:** {' > '.join(ancestors) if ancestors else '(Root)'}",
            f"- **Labels:** {', '.join(labels) if labels else '(None)'}",
            url_line,
        ]
        if downloaded_images:
            header.append(f"- **Downloaded Images ({len(downloaded_images)}):** " + ", ".join(d["saved_to"] for d in downloaded_images))
        header.extend([
            "",
            "---",
            "",
            converted,
        ])
        out_str = "\n".join(header)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(out_str, encoding="utf-8")
        res_summary: Dict[str, Any] = {"saved_to": str(out_path), "page_id": page.get("id"), "title": page.get("title"), "chars": len(out_str)}
        if downloaded_images:
            res_summary["downloaded_images"] = downloaded_images
        print(json.dumps(res_summary, indent=2))
    else:
        print(out_str)


def build_cql_from_args(args: argparse.Namespace, subtree_page_ids: Optional[List[str]] = None) -> str:
    if getattr(args, "cql", None):
        return args.cql
    clauses = []
    if getattr(args, "type", None) and args.type != "all":
        clauses.append(f'type = "{args.type}"')
    if getattr(args, "space", None):
        clauses.append(f'space = "{args.space}"')
    phrase_mode = bool(getattr(args, "phrase", False))
    title_only_mode = bool(getattr(args, "title_only", False))
    if getattr(args, "query", None):
        q_clean = args.query.strip().strip('"') if phrase_mode else args.query
        q_escaped = q_clean.replace('"', '\\"')
        if phrase_mode and " " in q_clean:
            q_escaped = f'\\"{q_escaped}\\"'
        if title_only_mode:
            clauses.append(f'title ~ "{q_escaped}"')
        else:
            clauses.append(f'(title ~ "{q_escaped}" OR text ~ "{q_escaped}")')
    if getattr(args, "title_contains", None):
        t_clean = args.title_contains.strip().strip('"') if phrase_mode else args.title_contains
        t_escaped = t_clean.replace('"', '\\"')
        if phrase_mode and " " in t_clean:
            t_escaped = f'\\"{t_escaped}\\"'
        clauses.append(f'title ~ "{t_escaped}"')
    if getattr(args, "label", None):
        clauses.append(f'label = "{args.label}"')
    if getattr(args, "ancestor", None):
        clauses.append(f"ancestor = {args.ancestor}")
    elif subtree_page_ids:
        clean_ids = [str(pid).strip() for pid in subtree_page_ids if str(pid).strip().isdigit()]
        if len(clean_ids) == 1:
            pid = clean_ids[0]
            clauses.append(f"(id = {pid} OR ancestor = {pid})")
        elif len(clean_ids) > 1:
            joined = ", ".join(clean_ids)
            clauses.append(f"(id IN ({joined}) OR ancestor IN ({joined}))")
    if getattr(args, "creator", None):
        clauses.append(f'creator = "{args.creator}"')
    if getattr(args, "contributor", None):
        clauses.append(f'contributor = "{args.contributor}"')
    if getattr(args, "modified_since", None):
        val = args.modified_since.strip()
        if val.startswith("-"):
            clauses.append(f'lastmodified >= now("{val}")')
        else:
            clauses.append(f'lastmodified >= "{val}"')
    if not clauses:
        clauses.append('type = "page"')
    cql = " AND ".join(clauses)
    order_by = getattr(args, "order_by", "lastmodified DESC")
    if order_by and "order by" not in cql.lower():
        cql += f" ORDER BY {order_by}"
    return cql


def _format_search_results(client: GuardedCrawlerConfluenceClient, results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    formatted = []
    for item in results:
        content = item.get("content", item)
        ver = content.get("version", {})
        space = content.get("space", {})
        s_key = space.get("key") or client._extract_space_key_from_obj(content)
        if client._is_space_guard_active() and s_key and not client.is_space_readable(s_key):
            continue
        raw_excerpt = (item.get("excerpt") or "").replace("@@@hl@@@", "").replace("@@@endhl@@@", "")
        clean_excerpt = html.unescape(re.sub(r"<[^>]+>", "", raw_excerpt)).strip()
        formatted.append({
            "id": content.get("id"),
            "type": content.get("type"),
            "title": content.get("title") or item.get("title"),
            "space_key": s_key,
            "space_access_mode": client.get_space_access_mode(s_key) if s_key else None,
            "last_modified": ver.get("when") or item.get("lastModified"),
            "modified_by": ver.get("by", {}).get("displayName") or ver.get("by", {}).get("username"),
            "version": ver.get("number"),
            "excerpt": clean_excerpt,
            "url": f"{client.base_url}{content.get('_links', {}).get('webui') or item.get('url', '')}",
        })
    return formatted


def cmd_search(client: GuardedCrawlerConfluenceClient, args: argparse.Namespace) -> None:
    """
    Progressive 3-tier search to minimize Confluence server query load:
      - Tier 1 (Default Page Subtree(s)): When `CONFLUENCE_DEFAULT_PAGE` is configured and no explicit
        scope flag (`--space`, `--all-spaces`/`--wider`, `--default-spaces`, `--ancestor`, `--cql`) is passed,
        searches ONLY within the configured `CONFLUENCE_DEFAULT_PAGE` page(s) and their descendants first.
        If `--default-pages-only` is passed, stops at Tier 1 even if 0 results are found.
      - Tier 2 (Default Space(s)): If Tier 1 returns 0 hits (or `--default-spaces` is passed, or
        `CONFLUENCE_DEFAULT_PAGE` is not configured), searches across `CONFLUENCE_DEFAULT_SPACES`
        (which defaults to `CONFLUENCE_ELEVATED_ACCESS_SPACES`).
      - Tier 3 (Wider / All Allowed Spaces): When `--all-spaces` (`--wider`) or `--space <KEY>` is passed,
        searches across `CONFLUENCE_ELEVATED_ACCESS_SPACES` + `CONFLUENCE_READONLY_SPACES`.
    """
    if getattr(args, "space", None):
        client._assert_space_readable(args.space, f"search in space {args.space!r}")
    all_spaces = bool(getattr(args, "all_spaces", False))
    default_pages_only = bool(getattr(args, "default_pages_only", False))
    default_spaces_flag = bool(getattr(args, "default_spaces", False))

    configured_def_page_ids: List[str] = []
    if not getattr(args, "space", None) and not all_spaces and not default_spaces_flag and not getattr(args, "ancestor", None) and not getattr(args, "cql", None):
        if default_pages_only or getattr(args, "type", "page") in ("page", "all", None):
            configured_def_page_ids = client._extract_default_page_ids_for_cql(
                fallback_to_space_homepage=default_pages_only
            )

    tried_tier1_cql: Optional[str] = None
    if configured_def_page_ids:
        tier1_cql = client._scope_cql_to_allowed_spaces(
            build_cql_from_args(args, subtree_page_ids=configured_def_page_ids),
            include_readonly=False,
        )
        tier1_params = {"cql": tier1_cql, "excerpt": "highlight", "expand": "content.space,content.version"}
        tier1_raw = client.get_paginated("/rest/api/search", params=tier1_params, max_items=args.limit)
        tier1_formatted = _format_search_results(client, tier1_raw)
        if tier1_formatted or default_pages_only:
            output = {
                "cql": tier1_cql,
                "search_scope": (
                    f"tier_1_default_page_subtrees (page_ids={configured_def_page_ids}; "
                    "pass --default-spaces or --all-spaces/--wider to search wider)"
                ),
                "additional_readonly_spaces_available": getattr(client, "readonly_spaces", []),
                "count": len(tier1_formatted),
                "results": tier1_formatted,
            }
            if args.output:
                Path(args.output).parent.mkdir(parents=True, exist_ok=True)
                Path(args.output).write_text(json.dumps(output, indent=2), encoding="utf-8")
            print(json.dumps(output, indent=2))
            return
        tried_tier1_cql = tier1_cql

    cql = client._scope_cql_to_allowed_spaces(build_cql_from_args(args), include_readonly=all_spaces)
    params = {"cql": cql, "excerpt": "highlight", "expand": "content.space,content.version"}
    results = client.get_paginated("/rest/api/search", params=params, max_items=args.limit)
    formatted = _format_search_results(client, results)

    if getattr(args, "space", None):
        scope_label = f"explicit_space ({args.space})"
    elif all_spaces:
        scope_label = "tier_3_all_allowed_spaces (CONFLUENCE_ELEVATED_ACCESS_SPACES + CONFLUENCE_READONLY_SPACES)"
    elif tried_tier1_cql:
        scope_label = (
            f"tier_2_default_spaces_after_0_hits_in_default_page_subtrees "
            f"(default_spaces={client._get_default_search_spaces_list()}; pass --all-spaces/--wider to include CONFLUENCE_READONLY_SPACES)"
        )
    else:
        reason = "--default-spaces requested" if default_spaces_flag else "CONFLUENCE_DEFAULT_PAGE not set, so space homepage covers full default space"
        scope_label = (
            f"tier_2_default_spaces_only ({reason}; default_spaces={client._get_default_search_spaces_list()}; "
            "pass --all-spaces/--wider or --space <KEY> to include CONFLUENCE_READONLY_SPACES)"
        )

    output = {
        "cql": cql,
        "search_scope": scope_label,
        "tried_default_page_subtrees_first_cql": tried_tier1_cql,
        "additional_readonly_spaces_available": getattr(client, "readonly_spaces", []) if not all_spaces and not getattr(args, "space", None) else [],
        "count": len(formatted),
        "results": formatted,
    }
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(json.dumps(output, indent=2))


def cmd_recent_edits(client: GuardedCrawlerConfluenceClient, args: argparse.Namespace) -> None:
    if args.space:
        client._assert_space_readable(args.space, f"list recent edits in space {args.space!r}")
    all_spaces = bool(getattr(args, "all_spaces", False))
    until_arg = getattr(args, "until", None)
    expand_versions = bool(getattr(args, "expand_versions", False))
    effective_since = args.since if args.since else f"-{args.days}d"

    since_epoch = _parse_timestamp_to_epoch(effective_since, is_until_bound=False)
    until_epoch = _parse_timestamp_to_epoch(until_arg, is_until_bound=True) if until_arg else None
    local_tz = datetime.now().astimezone().tzinfo or timezone.utc
    cql_since_date = (
        args.since
        if (args.since and re.match(r"^\d{4}-\d{2}-\d{2}$", args.since.strip()))
        else datetime.fromtimestamp(since_epoch, tz=local_tz).strftime("%Y-%m-%d")
    )

    clauses = []
    if args.type != "all":
        clauses.append(f'type = "{args.type}"')
    if args.space:
        clauses.append(f'space = "{args.space}"')
    if args.user:
        clauses.append(f'contributor = "{args.user}"')
    if args.since and re.match(r"^-\d+[dhms]$", args.since.strip().lower()):
        clauses.append(f'lastmodified >= now("{args.since.strip()}")')
    elif not args.since:
        clauses.append(f'lastmodified >= now("-{args.days}d")')
    else:
        clauses.append(f'lastmodified >= "{cql_since_date}"')

    cql = client._scope_cql_to_allowed_spaces(
        " AND ".join(clauses) + " ORDER BY lastmodified DESC",
        include_readonly=all_spaces,
    )
    inspect_versions = bool(until_arg or expand_versions)
    fetch_limit = max(args.limit * 3, 100) if until_arg else args.limit
    results = client.get_paginated(
        "/rest/api/content/search",
        params={"cql": cql, "expand": "space,version,history.lastUpdated"},
        max_items=fetch_limit,
    )
    edits = []
    author_totals: Dict[str, int] = {}
    space_totals: Dict[str, int] = {}

    for c in results:
        if len(edits) >= args.limit:
            break
        s_key = c.get("space", {}).get("key") or client._extract_space_key_from_obj(c)
        if client._is_space_guard_active() and s_key and not client.is_space_readable(s_key):
            continue
        cid = str(c.get("id") or "")
        ctype = c.get("type")
        if cid and s_key and hasattr(client, "_content_space_cache"):
            client._content_space_cache[cid] = s_key

        item: Dict[str, Any] = {
            "id": cid,
            "type": ctype,
            "title": c.get("title"),
            "space": s_key,
            "space_access_mode": client.get_space_access_mode(s_key) if s_key else None,
            "version": c.get("version", {}).get("number"),
            "when": c.get("version", {}).get("when"),
            "author_username": c.get("version", {}).get("by", {}).get("username"),
            "author_display_name": c.get("version", {}).get("by", {}).get("displayName"),
            "minor_edit": c.get("version", {}).get("minorEdit"),
            "edit_comment": c.get("version", {}).get("message", ""),
            "url": f"{client.base_url}{c.get('_links', {}).get('webui', '')}",
        }

        if inspect_versions and ctype in ("page", "blogpost") and cid.isdigit():
            ver_res = client.get_version_list(
                cid,
                max_items=500,
                since=effective_since,
                until=until_arg,
            )
            in_win_vers = ver_res.get("versions", [])
            if args.user:
                u_low = args.user.strip().lower()
                in_win_vers = [
                    v for v in in_win_vers
                    if u_low in (str(v.get("username") or "").lower(), str(v.get("by") or "").lower())
                ]
            if not in_win_vers:
                continue
            nums = [int(v["number"]) for v in in_win_vers if v.get("number") is not None]
            first_v = min(nums)
            last_v = max(nums)
            per_item_authors: Dict[str, int] = {}
            for v in in_win_vers:
                a_key = str(v.get("by") or v.get("username") or "unknown")
                per_item_authors[a_key] = per_item_authors.get(a_key, 0) + 1
                author_totals[a_key] = author_totals.get(a_key, 0) + 1
            if s_key:
                space_totals[s_key] = space_totals.get(s_key, 0) + len(in_win_vers)
            item["versions_in_window_count"] = len(in_win_vers)
            item["first_version_in_window"] = first_v
            item["last_version_in_window"] = last_v
            item["from_version_for_diff"] = max(1, first_v - 1)
            item["authors_in_window"] = per_item_authors
            item["version_source"] = ver_res.get("version_source")
            if expand_versions:
                item["versions_in_window"] = in_win_vers
        else:
            w_str = c.get("version", {}).get("when")
            if w_str:
                try:
                    w_ep = _parse_timestamp_to_epoch(str(w_str))
                    if w_ep < since_epoch:
                        continue
                    if until_epoch is not None and w_ep > until_epoch:
                        continue
                except Exception:
                    pass
            a_key = str(item.get("author_display_name") or item.get("author_username") or "unknown")
            author_totals[a_key] = author_totals.get(a_key, 0) + 1
            if s_key:
                space_totals[s_key] = space_totals.get(s_key, 0) + 1

        edits.append(item)

    print(json.dumps({
        "cql": cql,
        "resolved_window": _describe_resolved_time_window(effective_since, until_arg),
        "search_scope": (
            f"explicit_space ({args.space})"
            if args.space
            else ("all_allowed_spaces" if all_spaces else "default_spaces_only")
        ),
        "count": len(edits),
        "activity_summary": {
            "total_items": len(edits),
            "total_revisions_counted": sum(int(e.get("versions_in_window_count", 1)) for e in edits),
            "by_author": dict(sorted(author_totals.items(), key=lambda kv: (-kv[1], kv[0]))),
            "by_space": dict(sorted(space_totals.items(), key=lambda kv: (-kv[1], kv[0]))),
        },
        "recent_edits": edits,
    }, indent=2))


def cmd_history(client: GuardedCrawlerConfluenceClient, args: argparse.Namespace) -> None:
    if client._is_space_guard_active():
        space_key = client._get_content_space_key(args.id)
        client._assert_space_readable(space_key, f"read history of content ID {args.id}")
    hist = client.get_json(
        f"/rest/api/content/{args.id}/history",
        params={"expand": "lastUpdated,previousVersion,contributors.publishers.users"},
    )
    since_arg = getattr(args, "since", None)
    until_arg = getattr(args, "until", None)
    version_source: Optional[str] = None
    try:
        ver_res = client.get_version_list(
            args.id,
            max_items=args.limit,
            since=since_arg,
            until=until_arg,
            history_obj=hist,
        )
        version_source = ver_res.get("version_source")
        versions_list = ver_res.get("versions", [])
    except PermissionError:
        raise
    except Exception as e:
        if "HTTP 401" in str(e) or "HTTP 403" in str(e):
            raise
        version_source = "history_summary_fallback"
        versions_list = [{"note": f"/version list endpoints unavailable ({e}); showing history summary only."}]

    author_tally: Dict[str, int] = {}
    for v in versions_list:
        if isinstance(v, dict) and "number" in v:
            a_key = str(v.get("by") or v.get("username") or "unknown")
            author_tally[a_key] = author_tally.get(a_key, 0) + 1

    contributors = [
        {"username": u.get("username"), "displayName": u.get("displayName")}
        for u in hist.get("contributors", {}).get("publishers", {}).get("users", [])
    ]
    print(json.dumps({
        "page_id": args.id,
        "created_date": hist.get("createdDate"),
        "created_by": hist.get("createdBy", {}).get("displayName") or hist.get("createdBy", {}).get("username"),
        "latest_version": hist.get("lastUpdated", {}).get("number"),
        "last_updated_when": hist.get("lastUpdated", {}).get("when"),
        "version_source": version_source,
        "resolved_window": _describe_resolved_time_window(since_arg, until_arg),
        "versions_returned_count": len([v for v in versions_list if isinstance(v, dict) and "number" in v]),
        "author_tally": dict(sorted(author_tally.items(), key=lambda kv: (-kv[1], kv[0]))),
        "contributors": contributors,
        "versions": versions_list,
    }, indent=2))


def cmd_diff(client: GuardedCrawlerConfluenceClient, args: argparse.Namespace) -> None:
    since_arg = getattr(args, "since", None)
    until_arg = getattr(args, "until", None)
    todos_mode = bool(getattr(args, "todos", False))
    grep_term = getattr(args, "grep", None)
    range_header: Optional[str] = None
    win_desc: Optional[Dict[str, Any]] = None
    resolved: Dict[str, Any] = {}

    latest_page = client.get_page(args.id, expand="body.storage,version,space")
    latest_num = int(latest_page.get("version", {}).get("number", 1))

    if since_arg or until_arg:
        win_desc = _describe_resolved_time_window(since_arg, until_arg) or {}
        win_bounds_str = f"{win_desc.get('since_resolved') or '*'} .. {win_desc.get('until_resolved_inclusive') or 'now'} (inclusive)"
        resolved = client.resolve_version_range_by_time(args.id, since=since_arg, until=until_arg)
        if resolved.get("matched_versions_count", 0) == 0 and not todos_mode and not grep_term:
            print(
                f"[No versions of page {args.id} were modified in the requested window "
                f"({win_bounds_str}; latest version is v{latest_num}; "
                f"version_source={resolved.get('version_source')})]"
            )
            return
        if resolved.get("matched_versions_count", 0) == 0:
            to_ver = args.to_version if args.to_version is not None else latest_num
            from_ver = args.from_version if args.from_version is not None else to_ver
        else:
            to_ver = args.to_version if args.to_version is not None else int(resolved["to_version"])
            from_ver = args.from_version if args.from_version is not None else int(resolved["from_version"])
        range_header = (
            f"# Time-window diff ({win_bounds_str}) -> "
            f"v{from_ver} -> v{to_ver} ({resolved.get('matched_versions_count')} version(s) in window; "
            f"version_source={resolved.get('version_source')})"
        )
    else:
        to_ver = args.to_version if args.to_version is not None else latest_num
        from_ver = args.from_version if args.from_version is not None else max(1, to_ver - 1)

    p_from = latest_page if from_ver == latest_num else client.get_page(args.id, expand="body.storage,version,space", version=from_ver)
    p_to = latest_page if to_ver == latest_num else client.get_page(args.id, expand="body.storage,version,space", version=to_ver)

    raw_from = p_from.get("body", {}).get("storage", {}).get("value", "")
    raw_to = p_to.get("body", {}).get("storage", {}).get("value", "")

    md_from = storage_to_markdown(raw_from)
    md_to = storage_to_markdown(raw_to)

    if todos_mode:
        tasks_from = extract_tasks_from_storage_or_markdown(raw_from, md_from)
        tasks_to = extract_tasks_from_storage_or_markdown(raw_to, md_to)
        task_diff = diff_tasks(tasks_from, tasks_to)
        print(json.dumps({
            "id": str(args.id),
            "title": p_to.get("title"),
            "space": p_to.get("space", {}).get("key"),
            "from_version": from_ver,
            "from_when": p_from.get("version", {}).get("when"),
            "to_version": to_ver,
            "to_when": p_to.get("version", {}).get("when"),
            "time_window": win_desc,
            "version_source": resolved.get("version_source") if (since_arg or until_arg) else None,
            "matched_versions_in_window": resolved.get("matched_versions_count") if (since_arg or until_arg) else None,
            **task_diff,
        }, indent=2))
        return

    lines_from = raw_from.splitlines() if args.raw_storage else md_from.splitlines()
    lines_to = raw_to.splitlines() if args.raw_storage else md_to.splitlines()

    diff_lines = list(
        difflib.unified_diff(
            lines_from,
            lines_to,
            fromfile=f"page_{args.id}_v{from_ver} ({p_from.get('version', {}).get('when', '')})",
            tofile=f"page_{args.id}_v{to_ver} ({p_to.get('version', {}).get('when', '')})",
            lineterm="",
        )
    )

    if grep_term:
        needle = grep_term.lower()
        matching_lines_from = [line for line in lines_from if needle in line.lower()]
        matching_lines_to = [line for line in lines_to if needle in line.lower()]
        count_from = sum(line.lower().count(needle) for line in lines_from)
        count_to = sum(line.lower().count(needle) for line in lines_to)
        added_lines = [
            line[1:].strip()
            for line in diff_lines
            if line.startswith("+") and not line.startswith("+++") and needle in line.lower()
        ]
        removed_lines = [
            line[1:].strip()
            for line in diff_lines
            if line.startswith("-") and not line.startswith("---") and needle in line.lower()
        ]
        if count_from == 0 and count_to > 0:
            grep_status = "newly_added"
        elif count_from > 0 and count_to == 0:
            grep_status = "removed"
        elif added_lines or removed_lines:
            grep_status = "modified_or_expanded"
        elif count_to > 0:
            grep_status = "pre_existing_unchanged"
        else:
            grep_status = "absent"

        print(json.dumps({
            "id": str(args.id),
            "title": p_to.get("title"),
            "space": p_to.get("space", {}).get("key"),
            "grep_term": grep_term,
            "status": grep_status,
            "from_version": from_ver,
            "from_when": p_from.get("version", {}).get("when"),
            "to_version": to_ver,
            "to_when": p_to.get("version", {}).get("when"),
            "time_window": win_desc,
            "version_source": resolved.get("version_source") if (since_arg or until_arg) else None,
            "matched_versions_in_window": resolved.get("matched_versions_count") if (since_arg or until_arg) else None,
            "occurrences_in_from_version": count_from,
            "occurrences_in_to_version": count_to,
            "net_occurrence_delta": count_to - count_from,
            "added_matching_lines": added_lines,
            "removed_matching_lines": removed_lines,
            "matching_lines_in_from_version": matching_lines_from,
            "matching_lines_in_to_version": matching_lines_to,
        }, indent=2))
        return

    if range_header:
        print(range_header)
    if diff_lines:
        print("\n".join(diff_lines))
    else:
        title_from = p_from.get("title")
        title_to = p_to.get("title")
        title_note = f"; title changed from {title_from!r} to {title_to!r}" if title_from != title_to else ""
        if not args.raw_storage and raw_from != raw_to:
            print(
                f"[No rendered Markdown differences between version {from_ver} and version {to_ver}{title_note} "
                "(raw storage XHTML differs — pass --raw-storage to inspect macro/layout/attribute changes)]"
            )
        else:
            print(
                f"[No body differences between version {from_ver} and version {to_ver}{title_note} "
                "(storage XHTML is byte-identical; version bump was metadata, macro-parameter, or attachment-only)]"
            )


def cmd_crawl(client: GuardedCrawlerConfluenceClient, args: argparse.Namespace) -> None:
    out_dir = Path(args.output_dir) if args.output_dir else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    expand = "version,space,ancestors,metadata.labels"
    if args.include_body:
        expand += ",body.storage"

    crawled: List[Dict[str, Any]] = []
    root_id = getattr(args, "root_id", None)
    if getattr(args, "default", False) and not root_id and not getattr(args, "space", None):
        def_info = client.resolve_default_page(strict=True)
        assert def_info is not None
        root_id = def_info["id"]

    if root_id:
        root_page = client.get_page(root_id, expand=expand)
        pages_raw = [root_page]
        if args.max_depth is None or args.max_depth >= 1:
            parent_clause = f"parent = {root_id}" if args.max_depth == 1 else f"ancestor = {root_id}"
            cql = client._scope_cql_to_allowed_spaces(
                f"{parent_clause} AND type = page ORDER BY id ASC",
                include_readonly=True,
            )
            fetch_limit = max(1, args.max_pages - 1) if args.max_depth in (None, 1) else max(args.max_pages * 4, 100)
            descendants = client.get_paginated(
                "/rest/api/content/search",
                params={"cql": cql, "expand": expand},
                max_items=fetch_limit,
            )
            pages_raw.extend(descendants)
    elif args.space:
        client._assert_space_readable(args.space, f"crawl space {args.space!r}")
        cql = f'space = "{args.space}" AND type = page ORDER BY id ASC'
        fetch_limit = args.max_pages if args.max_depth is None else max(args.max_pages * 4, 100)
        pages_raw = client.get_paginated(
            "/rest/api/content/search",
            params={"cql": cql, "expand": expand},
            max_items=fetch_limit,
        )
    else:
        raise ValueError("Provide --default, --root-id <PAGE_ID>, or --space <SPACE_KEY> to crawl.")

    for p in pages_raw:
        if len(crawled) >= args.max_pages:
            break
        s_key = p.get("space", {}).get("key") or client._extract_space_key_from_obj(p)
        if client._is_space_guard_active() and s_key and not client.is_space_readable(s_key):
            continue
        ancestors = [{"id": a.get("id"), "title": a.get("title")} for a in p.get("ancestors", [])]
        depth = len(ancestors)
        rel_depth = 0 if (root_id and str(p.get("id")) == str(root_id)) else depth
        if root_id and str(p.get("id")) != str(root_id):
            anc_ids = [str(a["id"]) for a in ancestors]
            if str(root_id) in anc_ids:
                rel_depth = len(anc_ids) - anc_ids.index(str(root_id))
            else:
                # Confluence CQL /rest/api/content/search sometimes expands only the immediate parent in ancestors
                rel_depth = 1 if args.max_depth == 1 else max(1, depth)
        if args.max_depth is not None and rel_depth > args.max_depth:
            continue

        record: Dict[str, Any] = {
            "id": p.get("id"),
            "title": p.get("title"),
            "space": s_key,
            "space_access_mode": client.get_space_access_mode(s_key) if s_key else None,
            "version": p.get("version", {}).get("number"),
            "last_modified": p.get("version", {}).get("when"),
            "modified_by": p.get("version", {}).get("by", {}).get("displayName") or p.get("version", {}).get("by", {}).get("username"),
            "ancestors": ancestors,
            "depth": depth,
            "relative_depth": rel_depth,
            "labels": [lbl.get("name") for lbl in p.get("metadata", {}).get("labels", {}).get("results", [])],
            "url": f"{client.base_url}{p.get('_links', {}).get('webui', '')}",
        }

        if args.include_body:
            raw_storage = p.get("body", {}).get("storage", {}).get("value", "")
            md_body = storage_to_markdown(raw_storage)
            record["markdown_chars"] = len(md_body)
            if out_dir:
                safe_slug = re.sub(r"[^A-Za-z0-9._-]+", "_", p.get("title", "page"))[:80]
                md_file = out_dir / f"{p.get('id')}_{safe_slug}.md"
                md_file.write_text(f"# {p.get('title')} (ID: {p.get('id')})\n\n{md_body}\n", encoding="utf-8")
                record["saved_markdown"] = str(md_file)
            else:
                record["markdown"] = md_body

        crawled.append(record)
        if args.delay > 0:
            time.sleep(args.delay)

    manifest = {"total_crawled": len(crawled), "pages": crawled}
    if out_dir:
        manifest_path = out_dir / "crawl_index.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(json.dumps({"total_crawled": len(crawled), "index_file": str(manifest_path), "output_dir": str(out_dir)}, indent=2))
    else:
        print(json.dumps(manifest, indent=2))


def cmd_attachments(client: GuardedCrawlerConfluenceClient, args: argparse.Namespace) -> None:
    if client._is_space_guard_active():
        space_key = client._get_content_space_key(args.page_id)
        client._assert_space_readable(space_key, f"list attachments on page ID {args.page_id}")

    params: Dict[str, Any] = {"expand": "version,metadata.mediaType,extensions"}
    if args.filename:
        params["filename"] = args.filename
    if args.media_type:
        params["mediaType"] = args.media_type

    atts = client.get_paginated(
        f"/rest/api/content/{args.page_id}/child/attachment",
        params=params,
        max_items=args.limit,
    )
    items = []
    for a in atts:
        ext = a.get("extensions", {})
        ver = a.get("version", {})
        dl_link = a.get("_links", {}).get("download", "")
        items.append({
            "attachment_id": a.get("id"),
            "title": a.get("title"),
            "media_type": ext.get("mediaType") or a.get("metadata", {}).get("mediaType"),
            "file_size_bytes": ext.get("fileSize"),
            "version": ver.get("number"),
            "modified_when": ver.get("when"),
            "modified_by": ver.get("by", {}).get("displayName") or ver.get("by", {}).get("username"),
            "comment": ext.get("comment") or ver.get("message", ""),
            "download_url": f"{client.base_url}{dl_link}" if dl_link else None,
        })

    if args.download_dir:
        dl_dir = Path(args.download_dir)
        dl_dir.mkdir(parents=True, exist_ok=True)
        for item in items:
            if args.glob and not fnmatch.fnmatch(item["title"], args.glob):
                continue
            if not item["download_url"]:
                continue
            target_file = dl_dir / os.path.basename(item["title"])
            with client.request("GET", item["download_url"], stream=True) as r:
                with open(target_file, "wb") as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        if chunk:
                            f.write(chunk)
            item["saved_to"] = str(target_file)

    print(json.dumps({"page_id": args.page_id, "count": len(items), "attachments": items}, indent=2))


def cmd_comments(client: GuardedCrawlerConfluenceClient, args: argparse.Namespace) -> None:
    page_space_key = ""
    page_space_mode = "elevated-access"
    if client._is_space_guard_active():
        page_space_key = client._get_content_space_key(args.page_id)
        page_space_mode = client._assert_space_readable(page_space_key, f"read comments on page ID {args.page_id}")

    space_writable = page_space_mode in ("elevated-access", "default-access")
    comments = client.get_paginated(
        f"/rest/api/content/{args.page_id}/child/comment",
        params={"expand": "history,body.storage,body.view,version,extensions.inlineProperties,extensions.resolution,ancestors,space", "depth": "all"},
        max_items=args.limit,
    )
    # Pre-pass: identify which parent comment IDs have any replies and which have a reply authored by the crawler
    parent_ids_with_any_reply = set()
    replied_parent_ids = set()
    for c in comments:
        owned_c, _ = is_content_owned_by_crawler(
            c,
            crawler_username=client.crawler_username,
            crawler_display_name=client.crawler_display_name,
            crawler_names=client.crawler_names,
        )
        if c.get("ancestors"):
            parent_id = str(c["ancestors"][-1].get("id") or "")
            if parent_id:
                parent_ids_with_any_reply.add(parent_id)
                if owned_c:
                    replied_parent_ids.add(parent_id)

    items = []
    for c in comments:
        cid = str(c.get("id") or "")
        if cid and page_space_key and hasattr(client, "_content_space_cache"):
            client._content_space_cache[cid] = page_space_key
        ver = c.get("version", {})
        ext = c.get("extensions", {})
        raw_view = c.get("body", {}).get("view", {}).get("value", "")
        raw_storage = c.get("body", {}).get("storage", {}).get("value", "")
        matched, matched_name = comment_mentions_crawler(c, client.crawler_names)
        owned, _ = is_content_owned_by_crawler(
            c,
            crawler_username=client.crawler_username,
            crawler_display_name=client.crawler_display_name,
            crawler_names=client.crawler_names,
        )
        if getattr(args, "only_mentions", False) and not matched:
            continue
        # Prefer converting body.view to Markdown so @mentions display full user names & links
        md_text = storage_to_markdown(raw_view or raw_storage)
        has_any_reply = cid in parent_ids_with_any_reply
        replied_by_crawler = cid in replied_parent_ids
        liked_by_crawler = False
        if (matched or getattr(args, "include_likes", False)) and cid.isdigit():
            try:
                likes_info = client.get_likes(cid)
                liked_by_crawler = bool(likes_info.get("liked_by_crawler"))
            except Exception:
                liked_by_crawler = False
        is_done = bool(
            liked_by_crawler
            or replied_by_crawler
            or ("[DONE]" in md_text)
            or (">DONE</ac:parameter>" in raw_storage)
        )
        items.append({
            "comment_id": cid,
            "parent_comment_id": c.get("ancestors", [{}])[-1].get("id") if c.get("ancestors") else None,
            "author_username": (c.get("history", {}).get("createdBy") or ver.get("by", {})).get("username"),
            "author": ver.get("by", {}).get("displayName") or ver.get("by", {}).get("username"),
            "when": ver.get("when"),
            "version": ver.get("number"),
            "location": ext.get("location", "footer"),
            "inline_selection": ext.get("inlineProperties", {}).get("originalSelection"),
            "resolution_status": ext.get("resolution", {}).get("status"),
            "has_replies": has_any_reply,
            "is_own_comment": owned,
            "mentions_crawler": matched,
            "matched_crawler_name": matched_name,
            "liked_by_crawler": liked_by_crawler,
            "replied_by_crawler": replied_by_crawler,
            "is_done": is_done,
            "can_edit": bool(space_writable and owned),
            "can_delete_own_unreplied": bool(space_writable and owned and not has_any_reply),
            "may_attempt_delete": bool(space_writable and (owned or matched)),
            "can_delete": bool(space_writable and (owned or matched)),
            "can_edit_or_delete": bool(space_writable and (owned or matched)),
            "markdown": md_text,
        })
    print(json.dumps({
        "page_id": args.page_id,
        "space": page_space_key or None,
        "space_access_mode": page_space_mode,
        "configured_crawler_username": client.crawler_username,
        "configured_crawler_display_name": client.crawler_display_name,
        "configured_crawler_names": client.crawler_names,
        "count": len(items),
        "comments": items,
    }, indent=2))


def cmd_verify_comment(client: GuardedCrawlerConfluenceClient, args: argparse.Namespace) -> None:
    """
    Runs the programmatic pre-flight check on a comment_id (without modifying or deleting it)
    to verify it is strictly a comment (not a page/blogpost) and is either owned by the crawler
    or mentions CONFLUENCE_CRAWLER_NAME.
    """
    existing = client._verify_comment_editable_or_deletable_on_server(
        comment_id=args.comment_id,
        action_label="verify",
        expected_page_id=args.page_id,
    )
    raw_view = existing.get("body", {}).get("view", {}).get("value", "")
    raw_storage = existing.get("body", {}).get("storage", {}).get("value", "")
    is_own = bool(existing.get("_is_own_comment"))
    space_mode = existing.get("_space_access_mode", "elevated-access")
    space_writable = space_mode in ("elevated-access", "default-access")
    print(json.dumps({
        "status": "verified_ok",
        "comment_id": str(existing.get("id")),
        "verified_type": existing.get("type"),
        "space": existing.get("_space_key"),
        "space_access_mode": space_mode,
        "page_id": existing.get("container", {}).get("id"),
        "page_title": existing.get("container", {}).get("title"),
        "authorization_basis": existing.get("_authorization_basis"),
        "is_own_comment": is_own,
        "can_edit": bool(space_writable and is_own),
        "may_attempt_delete": bool(space_writable),
        "can_delete": bool(space_writable),
        "server_delete_note": (
            "Own unreplied comments can always be deleted in elevated-access spaces; deleting another user's "
            "@mention comment or a replied own comment requires 'Comments -> Delete' in Space Permissions."
        ),
        "owner_check": existing.get("_owner_check"),
        "matched_crawler_name": existing.get("_matched_crawler_name"),
        "version": existing.get("version", {}).get("number"),
        "markdown": storage_to_markdown(raw_view or raw_storage),
    }, indent=2))


def cmd_verify_blogpost(client: GuardedCrawlerConfluenceClient, args: argparse.Namespace) -> None:
    """
    Runs the programmatic pre-flight check on a blogpost_id (without modifying or deleting it)
    to verify it is strictly a blogpost (not a page/comment) AND is owned/created by the crawler.
    """
    existing = client._verify_own_blogpost_on_server(
        blogpost_id=args.blogpost_id,
        action_label="verify",
    )
    space_mode = existing.get("_space_access_mode", "elevated-access")
    print(json.dumps({
        "status": "verified_ok",
        "blogpost_id": str(existing.get("id")),
        "verified_type": existing.get("type"),
        "title": existing.get("title"),
        "space": existing.get("space", {}).get("key"),
        "space_access_mode": space_mode,
        "can_edit_or_delete": space_mode in ("elevated-access", "default-access"),
        "authorization_basis": "own_blogpost",
        "owner_check": existing.get("_owner_check"),
        "version": existing.get("version", {}).get("number"),
    }, indent=2))


def cmd_find_mentions(client: GuardedCrawlerConfluenceClient, args: argparse.Namespace) -> None:
    """
    Finds comments across the wiki (or within --space / --page-id) that mention
    the configured CONFLUENCE_CRAWLER_NAME.
    Automatically checks whether the crawler has already liked each comment (`liked_by_crawler`),
    allowing a Like reaction from the crawler to serve as a non-destructive 'DONE' marker.
    """
    crawler_names = client._require_crawler_names()

    if args.page_id:
        if client._is_space_guard_active():
            page_space = client._get_content_space_key(args.page_id)
            client._assert_space_readable(page_space, f"find mentions on page ID {args.page_id}")
        candidates = client.get_paginated(
            f"/rest/api/content/{args.page_id}/child/comment",
            params={"expand": "history,body.storage,body.view,version,container,container.space,ancestors,space", "depth": "all"},
            max_items=args.limit,
        )
        cql_used = f"page_id={args.page_id}"
    else:
        if args.space:
            client._assert_space_readable(args.space, f"find mentions in space {args.space!r}")
        all_spaces = bool(getattr(args, "all_spaces", False))
        name_clauses = [f'text ~ "{n.replace(chr(34), "")}"' for n in crawler_names]
        if args.include_current_user_mention:
            name_clauses.append("mention = currentUser()")
        or_expr = " OR ".join(name_clauses)
        clauses = ['type = "comment"', f"({or_expr})"]
        if args.space:
            clauses.append(f'space = "{args.space}"')
        cql_used = client._scope_cql_to_allowed_spaces(
            " AND ".join(clauses) + " ORDER BY lastmodified DESC",
            include_readonly=all_spaces,
        )
        candidates = client.get_paginated(
            "/rest/api/content/search",
            params={"cql": cql_used, "expand": "history,body.storage,body.view,version,container,container.space,ancestors,space"},
            max_items=args.limit,
        )

    matched_comments = []
    for c in candidates:
        s_key = client._extract_space_key_from_obj(c)
        if client._is_space_guard_active() and s_key and not client.is_space_readable(s_key):
            continue
        matched, matched_name = comment_mentions_crawler(c, crawler_names)
        if not matched:
            continue
        cid = str(c.get("id") or "")
        if cid and s_key and hasattr(client, "_content_space_cache"):
            client._content_space_cache[cid] = s_key
        raw_view = c.get("body", {}).get("view", {}).get("value", "")
        raw_storage = c.get("body", {}).get("storage", {}).get("value", "")
        md_text = storage_to_markdown(raw_view or raw_storage)
        liked_by_crawler = False
        if cid.isdigit():
            try:
                likes_info = client.get_likes(cid)
                liked_by_crawler = bool(likes_info.get("liked_by_crawler"))
            except Exception:
                liked_by_crawler = False
        is_done = bool(
            liked_by_crawler
            or ("[DONE]" in md_text)
            or (">DONE</ac:parameter>" in raw_storage)
        )
        if is_done and not args.include_done:
            continue
        ver = c.get("version", {})
        container = c.get("container", {})
        matched_comments.append({
            "comment_id": cid,
            "page_id": container.get("id") or args.page_id,
            "page_title": container.get("title"),
            "space": s_key,
            "space_access_mode": client.get_space_access_mode(s_key) if s_key else None,
            "author": ver.get("by", {}).get("displayName") or ver.get("by", {}).get("username"),
            "when": ver.get("when"),
            "version": ver.get("number"),
            "matched_crawler_name": matched_name,
            "liked_by_crawler": liked_by_crawler,
            "is_done": is_done,
            "markdown": md_text,
            "url": f"{client.base_url}{c.get('_links', {}).get('webui', '')}",
        })

    all_spaces = bool(getattr(args, "all_spaces", False))
    if args.page_id:
        mentions_scope = f"explicit_page ({args.page_id})"
    elif args.space:
        mentions_scope = f"explicit_space ({args.space})"
    elif all_spaces:
        mentions_scope = "all_allowed_spaces (CONFLUENCE_ELEVATED_ACCESS_SPACES + CONFLUENCE_READONLY_SPACES)"
    else:
        mentions_scope = (
            f"default_spaces_only (default_spaces={client._get_default_search_spaces_list()}; "
            "pass --all-spaces/--wider to include CONFLUENCE_READONLY_SPACES)"
        )

    print(json.dumps({
        "configured_crawler_names": crawler_names,
        "search_scope": mentions_scope,
        "query": cql_used,
        "count": len(matched_comments),
        "mentioned_comments": matched_comments,
    }, indent=2))


def cmd_likes(client: GuardedCrawlerConfluenceClient, args: argparse.Namespace) -> None:
    res = client.get_likes(args.content_id)
    print(json.dumps(res, indent=2))


def cmd_like(client: GuardedCrawlerConfluenceClient, args: argparse.Namespace) -> None:
    res = client.like_content(args.content_id)
    print(json.dumps(res, indent=2))


def cmd_unlike(client: GuardedCrawlerConfluenceClient, args: argparse.Namespace) -> None:
    res = client.unlike_content(args.content_id)
    print(json.dumps(res, indent=2))


def cmd_metadata(client: GuardedCrawlerConfluenceClient, args: argparse.Namespace) -> None:
    if client._is_space_guard_active():
        space_key = client._get_content_space_key(args.page_id)
        client._assert_space_readable(space_key, f"read metadata on page ID {args.page_id}")
    labels = client.get_paginated(f"/rest/api/content/{args.page_id}/label", max_items=200)
    props = client.get_paginated(f"/rest/api/content/{args.page_id}/property", params={"expand": "version"}, max_items=200)
    restrictions: Dict[str, Any] = {}
    is_restricted = False
    restriction_summary = {"read_restricted": False, "update_restricted": False}
    try:
        restrictions = client.get_json(
            f"/rest/api/content/{args.page_id}/restriction/byOperation",
            params={"expand": "read.restrictions.user,read.restrictions.group,update.restrictions.user,update.restrictions.group"},
        )
        for op in ("read", "update"):
            op_node = restrictions.get(op, {}).get("restrictions", {})
            u_res = op_node.get("user", {}).get("results", [])
            g_res = op_node.get("group", {}).get("results", [])
            op_restricted = bool(u_res or g_res)
            restriction_summary[f"{op}_restricted"] = op_restricted
            if isinstance(restrictions.get(op), dict):
                restrictions[op]["restricted"] = op_restricted
            if op_restricted:
                is_restricted = True
    except Exception as e:
        restrictions = {"note": f"Could not fetch restrictions: {e}"}

    print(json.dumps({
        "page_id": args.page_id,
        "is_restricted": is_restricted,
        "restriction_summary": restriction_summary,
        "labels": [{"id": l.get("id"), "prefix": l.get("prefix"), "name": l.get("name")} for l in labels],
        "properties": [{"key": p.get("key"), "value": p.get("value"), "version": p.get("version", {}).get("number")} for p in props],
        "restrictions": restrictions,
    }, indent=2))


def cmd_export_pdf(client: GuardedCrawlerConfluenceClient, args: argparse.Namespace) -> None:
    if client._is_space_guard_active():
        space_key = client._get_content_space_key(args.page_id)
        client._assert_space_readable(space_key, f"export PDF for page ID {args.page_id}")
    export_path = f"/spaces/flyingpdf/pdfpageexport.action?pageId={args.page_id}"
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with client.request("GET", export_path, stream=True, allow_redirects=True) as r:
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
    print(json.dumps({"page_id": args.page_id, "exported_pdf": str(out_path), "bytes": out_path.stat().st_size}, indent=2))


def _parse_raw_get_params(raw_params: Optional[str]) -> Optional[Dict[str, Any]]:
    if not raw_params or not raw_params.strip():
        return None
    s = raw_params.strip()
    if s.startswith("{"):
        try:
            parsed = json.loads(s)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"Invalid JSON passed to --params: {e}. "
                "Pass either valid JSON (e.g. '{\"cql\": \"type=page\", \"limit\": 10}') "
                "or a URL query string (e.g. 'expand=version,space&limit=10')."
            ) from e
        if not isinstance(parsed, dict):
            raise ValueError("--params JSON must be a JSON object (dictionary).")
        return {str(k): v for k, v in parsed.items() if v is not None}
    return dict(urllib.parse.parse_qsl(s, keep_blank_values=True))


def cmd_raw_get(client: GuardedCrawlerConfluenceClient, args: argparse.Namespace) -> None:
    params = _parse_raw_get_params(args.params)
    if client._is_space_guard_active():
        m_content = re.search(r"/rest/(?:api|experimental|likes/1\.0)/content/(\d+)", args.endpoint)
        if m_content:
            space_key = client._get_content_space_key(m_content.group(1))
            client._assert_space_readable(space_key, f"raw-get content ID {m_content.group(1)}")
        m_space = re.search(r"/rest/api/space/([^/?#]+)", args.endpoint)
        if m_space:
            client._assert_space_readable(urllib.parse.unquote(m_space.group(1)), f"raw-get space {m_space.group(1)!r}")
        if params:
            if "spaceKey" in params:
                client._assert_space_readable(str(params["spaceKey"]), f"raw-get in space {params['spaceKey']!r}")
            if "cql" in params:
                params["cql"] = client._scope_cql_to_allowed_spaces(str(params["cql"]))
    data = client.get_json(args.endpoint, params=params)
    print(json.dumps(data, indent=2))


# --- SCOPED WRITE HANDLERS ---

def _load_body_input(body_str: Optional[str], body_file: Optional[str], input_format: str) -> str:
    if body_file:
        raw = Path(body_file).read_text(encoding="utf-8")
    elif body_str is not None:
        raw = body_str
    else:
        raise ValueError("Provide either --body <TEXT> or --body-file <PATH>.")
    return markdown_to_storage(raw) if input_format == "markdown" else raw


def cmd_create_blogpost(client: GuardedCrawlerConfluenceClient, args: argparse.Namespace) -> None:
    storage_val = _load_body_input(args.body, args.body_file, args.input_format)
    created = client.create_blogpost(
        space_key=args.space,
        title=args.title,
        storage_body=storage_val,
    )
    print(json.dumps({
        "status": "blogpost_created",
        "id": created.get("id"),
        "type": created.get("type", "blogpost"),
        "title": created.get("title"),
        "space": created.get("space", {}).get("key"),
        "version": created.get("version", {}).get("number"),
        "url": f"{client.base_url}{created.get('_links', {}).get('webui', '')}",
    }, indent=2))


def cmd_update_blogpost(client: GuardedCrawlerConfluenceClient, args: argparse.Namespace) -> None:
    new_body: Optional[str] = None
    if args.body is not None or args.body_file is not None:
        new_body = _load_body_input(args.body, args.body_file, args.input_format)

    append_storage: Optional[str] = None
    if args.append:
        append_storage = markdown_to_storage(args.append) if args.input_format == "markdown" else args.append

    prepend_storage: Optional[str] = None
    if args.prepend:
        prepend_storage = markdown_to_storage(args.prepend) if args.input_format == "markdown" else args.prepend

    if new_body is None and args.title is None and not append_storage and not prepend_storage:
        raise ValueError(
            "Specify at least one update action: --title <TITLE>, --append <TEXT>, --prepend <TEXT>, --body <TEXT>, or --body-file <PATH>."
        )

    updated = client.update_own_blogpost(
        blogpost_id=args.blogpost_id,
        title=args.title,
        new_storage_body=new_body,
        append_storage=append_storage,
        prepend_storage=prepend_storage,
        version_comment=args.comment,
    )
    print(json.dumps({
        "status": "blogpost_updated",
        "blogpost_id": updated.get("id"),
        "title": updated.get("title"),
        "space": updated.get("space", {}).get("key"),
        "new_version": updated.get("version", {}).get("number"),
        "programmatic_check": updated.get("_programmatic_check"),
        "url": f"{client.base_url}{updated.get('_links', {}).get('webui', '')}" if updated.get("_links") else None,
    }, indent=2))


def cmd_delete_blogpost(client: GuardedCrawlerConfluenceClient, args: argparse.Namespace) -> None:
    res = client.delete_own_blogpost(args.blogpost_id)
    print(json.dumps(res, indent=2))


def cmd_add_comment(client: GuardedCrawlerConfluenceClient, args: argparse.Namespace) -> None:
    storage_val = _load_body_input(args.body, args.body_file, args.input_format)
    created = client.add_page_comment(
        page_id=args.page_id,
        storage_body=storage_val,
        parent_comment_id=args.parent_comment_id,
    )
    print(json.dumps({
        "status": "comment_created",
        "comment_id": created.get("id"),
        "page_id": args.page_id,
        "parent_comment_id": args.parent_comment_id,
        "url": f"{client.base_url}{created.get('_links', {}).get('webui', '')}" if created.get("_links") else None,
    }, indent=2))


def cmd_update_comment(client: GuardedCrawlerConfluenceClient, args: argparse.Namespace) -> None:
    new_body: Optional[str] = None
    if args.body is not None or args.body_file is not None:
        new_body = _load_body_input(args.body, args.body_file, args.input_format)

    append_storage: Optional[str] = None
    if args.append:
        append_storage = markdown_to_storage(args.append) if args.input_format == "markdown" else args.append

    prepend_storage: Optional[str] = None
    if args.prepend:
        prepend_storage = markdown_to_storage(args.prepend) if args.input_format == "markdown" else args.prepend

    if new_body is None and not args.mark_done and not append_storage and not prepend_storage:
        raise ValueError(
            "Specify at least one update action: --mark-done, --append <TEXT>, --prepend <TEXT>, --body <TEXT>, or --body-file <PATH>."
        )

    updated = client.update_mentioned_comment(
        comment_id=args.comment_id,
        new_storage_body=new_body,
        mark_done=args.mark_done,
        status_label=args.status_label,
        append_storage=append_storage,
        prepend_storage=prepend_storage,
        expected_page_id=args.page_id,
    )
    print(json.dumps({
        "status": "comment_updated",
        "comment_id": updated.get("id"),
        "new_version": updated.get("version", {}).get("number"),
        "marked_done": bool(args.mark_done),
        "programmatic_check": updated.get("_programmatic_check"),
        "url": f"{client.base_url}{updated.get('_links', {}).get('webui', '')}" if updated.get("_links") else None,
    }, indent=2))


def cmd_delete_comment(client: GuardedCrawlerConfluenceClient, args: argparse.Namespace) -> None:
    res = client.delete_mentioned_comment(
        comment_id=args.comment_id,
        expected_page_id=args.page_id,
    )
    print(json.dumps(res, indent=2))


# =============================================================================
# 6. ARGUMENT PARSER
# =============================================================================

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Confluence Wiki — Guarded Crawler CLI (configured via access_settings.env)"
    )
    p.add_argument("--base-url", default=None, help="Confluence base URL (defaults to CONFLUENCE_BASE_URL in access_settings.env)")
    p.add_argument("--settings-file", "--token-file", dest="settings_file", default=None, help="Path to access_settings.env (defaults strictly to this skill's folder)")
    p.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds")
    p.add_argument("--throttle-ms", type=int, default=None, help="Minimum polite delay in milliseconds between consecutive HTTP requests (or set CONFLUENCE_THROTTLE_MS in access_settings.env)")
    p.add_argument("--insecure", action="store_true", help="Disable SSL certificate verification")

    # Shared parent parser with SUPPRESS defaults so global flags (--throttle-ms, --timeout, --base-url,
    # --settings-file, --insecure) work whether placed BEFORE or AFTER the subcommand name.
    sub_global = argparse.ArgumentParser(add_help=False, argument_default=argparse.SUPPRESS)
    sub_global.add_argument("--base-url", help=argparse.SUPPRESS)
    sub_global.add_argument("--settings-file", "--token-file", dest="settings_file", help=argparse.SUPPRESS)
    sub_global.add_argument("--timeout", type=float, help=argparse.SUPPRESS)
    sub_global.add_argument("--throttle-ms", type=int, help="Minimum polite delay in milliseconds between consecutive HTTP requests (or set CONFLUENCE_THROTTLE_MS in access_settings.env)")
    sub_global.add_argument("--insecure", action="store_true", help=argparse.SUPPRESS)

    sub = p.add_subparsers(dest="command", required=True)

    # Read-Only commands
    sub.add_parser("whoami", parents=[sub_global], help="Verify API token, print authenticated user, crawler identity, and space access policy")

    p_res = sub.add_parser("resolve-url", parents=[sub_global], help="Resolve any Confluence URL (including /x/ tiny links) to page metadata")
    p_res.add_argument("url", help="Confluence URL or tiny link code")
    p_res.add_argument("--offline-tiny-only", action="store_true", help="Decode /x/<code> to numeric pageId offline without network call")

    p_sp = sub.add_parser("spaces", parents=[sub_global], help="List Confluence spaces visible to the token on the server, annotated with access_mode from access_settings.env")
    p_sp.add_argument("--type", choices=["global", "personal", "all"], default="all")
    p_sp.add_argument("--status", choices=["current", "archived", "all"], default="current")
    p_sp.add_argument("--only-allowed", action="store_true", help="Show only spaces currently enabled in CONFLUENCE_ELEVATED_ACCESS_SPACES or CONFLUENCE_READONLY_SPACES")
    p_sp.add_argument("--limit", type=int, default=100)

    p_gp = sub.add_parser("get-page", parents=[sub_global], help="Fetch a page or blogpost by --default, ID, URL, or Space+Title and convert to Markdown/Storage/JSON")
    p_gp.add_argument("--default", action="store_true", help="Fetch the recommended starting page (CONFLUENCE_DEFAULT_PAGE or homepage of first default space)")
    p_gp.add_argument("--id", help="Numeric Confluence page or blogpost ID")
    p_gp.add_argument("--url", help="Any Confluence page/blogpost URL or /x/ tiny link")
    p_gp.add_argument("--space", help="Space key (used with --title)")
    p_gp.add_argument("--title", help="Exact page title (used with --space)")
    p_gp.add_argument("--version", type=int, default=None, help="Specific historical version number to retrieve")
    p_gp.add_argument("--format", choices=["markdown", "storage", "view", "json"], default="markdown")
    p_gp.add_argument("--with-images", "--download-images", dest="with_images", default=None, metavar="DIR", help="Download referenced image/attachment files into DIR and rewrite attachment:// links in Markdown to local file paths")
    p_gp.add_argument("--todos", action="store_true", help="Extract all Confluence <ac:task> and Markdown [ ]/[x] tasks on the page into structured JSON (open vs completed)")
    p_gp.add_argument("--output", help="Optional file path to save output directly to disk")

    p_sr = sub.add_parser("search", parents=[sub_global], help="Progressive search: tries CONFLUENCE_DEFAULT_PAGE subtree(s) first, then CONFLUENCE_DEFAULT_SPACES, or wider with --all-spaces/--wider")
    p_sr.add_argument("--cql", help="Raw CQL query string (overrides individual filter flags)")
    p_sr.add_argument("--query", "-q", help="Full-text and title search term")
    p_sr.add_argument("--phrase", action="store_true", help="Force exact contiguous phrase matching for --query / --title-contains (wraps in escaped inner quotes so 'project roadmap' does not match 'project' + 'roadmap' separately)")
    p_sr.add_argument("--title-only", action="store_true", help="Restrict --query (-q) to page/blogpost titles only (title ~ ...) instead of full body text (text ~ ...)")
    p_sr.add_argument("--title-contains", help="Match title (~) specifically")
    p_sr.add_argument("--space", help="Filter by a specific space key (can be an elevated-access or read-only space)")
    p_sr.add_argument("--default-pages-only", action="store_true", help="Search ONLY within CONFLUENCE_DEFAULT_PAGE page/subtree(s) without auto-widening to space level")
    p_sr.add_argument("--default-spaces", action="store_true", help="Search directly across CONFLUENCE_DEFAULT_SPACES (skipping the initial CONFLUENCE_DEFAULT_PAGE subtree tier)")
    p_sr.add_argument("--all-spaces", "--wider", dest="all_spaces", action="store_true", help="Widen search across all allowed spaces (CONFLUENCE_ELEVATED_ACCESS_SPACES + CONFLUENCE_READONLY_SPACES)")
    p_sr.add_argument("--type", choices=["page", "blogpost", "attachment", "comment", "all"], default="page")
    p_sr.add_argument("--label", help="Filter by label")
    p_sr.add_argument("--ancestor", help="Filter by ancestor page ID (search within a subtree)")
    p_sr.add_argument("--creator", help="Filter by creator username")
    p_sr.add_argument("--contributor", help="Filter by contributor username")
    p_sr.add_argument("--modified-since", help="Relative offset like '-7d' or ISO date 'YYYY-MM-DD'")
    p_sr.add_argument("--order-by", default="lastmodified DESC", help="CQL ORDER BY clause")
    p_sr.add_argument("--limit", type=int, default=25)
    p_sr.add_argument("--output", help="Optional JSON file path to save search results")

    p_re = sub.add_parser("recent-edits", parents=[sub_global], help="Find recently edited pages, blogposts, attachments, or comments (supports --since/--until date windows & per-author/per-page version tallies)")
    p_re.add_argument("--space", help="Restrict to a specific space key")
    p_re.add_argument("--all-spaces", "--wider", dest="all_spaces", action="store_true", help="Include CONFLUENCE_READONLY_SPACES in addition to CONFLUENCE_DEFAULT_SPACES")
    p_re.add_argument("--days", type=int, default=7, help="Look back N days (default: 7)")
    p_re.add_argument("--since", help="Start of window: 'YYYY-MM-DD' (from 00:00:00), ISO-8601, 'today', 'yesterday', or '-7d' (overrides --days)")
    p_re.add_argument("--until", default=None, help="End of window: date-only 'YYYY-MM-DD' is INCLUSIVE through 23:59:59 of that date (e.g. for Oct 10–15 use --since 2026-10-10 --until 2026-10-15); automatically tallies per-page versions in [since, until]")
    p_re.add_argument("--expand-versions", action="store_true", help="Fetch and include the full list of versions modified inside [since, until] for each page/blogpost")
    p_re.add_argument("--user", help="Filter by contributor/author username")
    p_re.add_argument("--type", choices=["page", "blogpost", "attachment", "comment", "all"], default="page")
    p_re.add_argument("--limit", type=int, default=30)

    p_hi = sub.add_parser("history", parents=[sub_global], help="Inspect full version history, author tallies, and contributors of a page or blogpost (with 3-tier DC/Server version resolver)")
    p_hi.add_argument("id", help="Page or blogpost ID")
    p_hi.add_argument("--since", default=None, help="Optional filter: include versions modified at or after this time ('YYYY-MM-DD' from 00:00:00, ISO-8601, 'today', 'yesterday', '-1d', '-12h')")
    p_hi.add_argument("--until", default=None, help="Optional filter: include versions modified at or before this time (date-only 'YYYY-MM-DD' is INCLUSIVE through 23:59:59 of that date)")
    p_hi.add_argument("--limit", type=int, default=50)

    p_df = sub.add_parser("diff", parents=[sub_global], help="Compute unified diff between two historical versions (or across a --since/--until time window) of a page")
    p_df.add_argument("id", help="Page ID")
    p_df.add_argument("--from-version", type=int, default=None, help="Older version number (default: latest - 1, or version right before --since window)")
    p_df.add_argument("--to-version", type=int, default=None, help="Newer version number (default: latest, or latest version inside --until window)")
    p_df.add_argument("--since", default=None, help="Compute net diff of edits since this time ('YYYY-MM-DD' from 00:00:00, ISO-8601, 'today', 'yesterday', '-1d', '-12h')")
    p_df.add_argument("--until", default=None, help="Upper time bound when using --since (date-only 'YYYY-MM-DD' is INCLUSIVE through 23:59:59 of that date; e.g. for Oct 10–15 use --since 2026-10-10 --until 2026-10-15)")
    p_df.add_argument("--grep", default=None, metavar="TERM", help="Check whether TERM was newly_added, removed, modified_or_expanded, or pre_existing_unchanged across the version/time window and return matching diff lines as JSON")
    p_df.add_argument("--todos", action="store_true", help="Compare Confluence <ac:task> and Markdown [ ]/[x] tasks across the version/time window and report completed ([ ]->[x]), reopened, and newly added tasks as JSON")
    p_df.add_argument("--raw-storage", action="store_true", help="Diff raw XHTML storage format instead of converted Markdown")

    p_cr = sub.add_parser("crawl", parents=[sub_global], help="Crawl a page subtree (--default or --root-id) or entire space (--space)")
    p_cr.add_argument("--default", action="store_true", help="Crawl starting from the recommended default page (CONFLUENCE_DEFAULT_PAGE or homepage of first default space)")
    p_cr.add_argument("--root-id", help="Root page ID to crawl all descendants under")
    p_cr.add_argument("--space", help="Space key to crawl all pages in")
    p_cr.add_argument("--max-depth", type=int, default=None, help="Maximum depth relative to root-id")
    p_cr.add_argument("--max-pages", type=int, default=100, help="Maximum number of pages to fetch")
    p_cr.add_argument("--include-body", action="store_true", help="Fetch and convert page bodies to Markdown")
    p_cr.add_argument("--output-dir", help="Directory to save crawled Markdown files and crawl_index.json")
    p_cr.add_argument("--delay", type=float, default=0.1, help="Polite delay in seconds between pages")

    p_at = sub.add_parser("attachments", parents=[sub_global], help="List and optionally download attachments on a page")
    p_at.add_argument("page_id", help="Page ID")
    p_at.add_argument("--filename", help="Filter by exact filename")
    p_at.add_argument("--media-type", help="Filter by MIME mediaType (e.g. application/pdf)")
    p_at.add_argument("--glob", help="Filename glob filter when downloading (e.g. '*.pdf')")
    p_at.add_argument("--download-dir", help="Download matching attachments into this directory")
    p_at.add_argument("--limit", type=int, default=100)

    p_cm = sub.add_parser("comments", parents=[sub_global], help="List all page and inline comments (with is_own_comment, mentions_crawler, liked_by_crawler & replied_by_crawler flags) on a page")
    p_cm.add_argument("page_id", help="Page ID")
    p_cm.add_argument("--only-mentions", action="store_true", help="Return only comments that mention CONFLUENCE_CRAWLER_NAME")
    p_cm.add_argument("--include-likes", action="store_true", help="Check likes on all comments (mentioned comments are checked automatically)")
    p_cm.add_argument("--limit", type=int, default=100)

    p_fm = sub.add_parser("find-mentions", parents=[sub_global], help="Find comments across the wiki (or on a page/space) that mention CONFLUENCE_CRAWLER_NAME")
    p_fm.add_argument("--space", help="Optional space key filter")
    p_fm.add_argument("--all-spaces", "--wider", dest="all_spaces", action="store_true", help="Scan CONFLUENCE_READONLY_SPACES in addition to CONFLUENCE_DEFAULT_SPACES")
    p_fm.add_argument("--page-id", help="Optional specific page ID to scan")
    p_fm.add_argument("--exclude-done", action="store_true", default=True, help="Exclude comments already liked by crawler or marked [DONE] (default)")
    p_fm.add_argument("--include-done", action="store_true", help="Include comments already liked by crawler or marked [DONE]")
    p_fm.add_argument("--include-current-user-mention", action="store_true", help="Also include CQL mention=currentUser() in search")
    p_fm.add_argument("--limit", type=int, default=50)

    p_vc = sub.add_parser("verify-comment", parents=[sub_global], help="Run programmatic pre-flight check on a comment_id (verifies type=='comment' & own/mention)")
    p_vc.add_argument("comment_id", help="Comment ID to verify")
    p_vc.add_argument("--page-id", default=None, help="Optional expected parent page ID to cross-check")

    p_vb = sub.add_parser("verify-blogpost", parents=[sub_global], help="Run programmatic pre-flight check on a blogpost_id (verifies type=='blogpost' & crawler ownership)")
    p_vb.add_argument("blogpost_id", help="Blogpost ID to verify")

    p_lk = sub.add_parser("likes", parents=[sub_global], help="List users who liked a comment, page, or blogpost and check liked_by_crawler")
    p_lk.add_argument("content_id", help="Numeric comment ID, page ID, or blogpost ID")

    p_md = sub.add_parser("metadata", parents=[sub_global], help="Inspect labels, JSON content properties, and access restrictions on a page")
    p_md.add_argument("page_id", help="Page ID")

    p_pdf = sub.add_parser("export-pdf", parents=[sub_global], help="Export a page to PDF using Confluence's native FlyingPDF exporter")
    p_pdf.add_argument("page_id", help="Page ID")
    p_pdf.add_argument("--output", required=True, help="Output PDF file path")

    p_rg = sub.add_parser("raw-get", parents=[sub_global], help="Execute a raw authenticated GET request against any /rest/... endpoint")
    p_rg.add_argument("endpoint", help="REST path, e.g. /rest/api/space or /rest/api/content/12345")
    p_rg.add_argument("--params", help="Query parameters as a JSON object (e.g. '{\"cql\": \"type=page\", \"limit\": 10}') or URL query string (e.g. 'expand=version,space&limit=10')")

    # Strictly Scoped Write Commands (permitted ONLY in CONFLUENCE_ELEVATED_ACCESS_SPACES)
    p_cb = sub.add_parser("create-blogpost", parents=[sub_global], help="[WRITE RIGHT 1/7] Create a new Blog Post in an elevated-access Confluence space")
    p_cb.add_argument("--space", required=True, help="Target space key (must be in CONFLUENCE_ELEVATED_ACCESS_SPACES)")
    p_cb.add_argument("--title", required=True, help="Blog post title")
    p_cb.add_argument("--body", help="Blog post body string")
    p_cb.add_argument("--body-file", help="Path to file containing blog post body")
    p_cb.add_argument("--input-format", choices=["storage", "markdown"], default="storage", help="Input body format (default: storage XHTML)")

    p_ub = sub.add_parser("update-blogpost", parents=[sub_global], help="[WRITE RIGHT 2/7 — GUARDED OWN] Edit a Blog Post ONLY if created/owned by the crawler")
    p_ub.add_argument("blogpost_id", help="Blog post ID to update (programmatically checked for type=='blogpost', space & crawler ownership)")
    p_ub.add_argument("--title", default=None, help="Optional new title for the blog post")
    p_ub.add_argument("--append", help="Text/Markdown to append to the existing blog post body")
    p_ub.add_argument("--prepend", help="Text/Markdown to prepend to the existing blog post body")
    p_ub.add_argument("--body", help="Full replacement blog post body string")
    p_ub.add_argument("--body-file", help="File containing full replacement blog post body")
    p_ub.add_argument("--input-format", choices=["storage", "markdown"], default="storage", help="Format of --append/--prepend/--body")
    p_ub.add_argument("--comment", default=None, help="Optional version revision message")

    p_db = sub.add_parser("delete-blogpost", parents=[sub_global], help="[WRITE RIGHT 3/7 — GUARDED OWN] Delete a Blog Post ONLY if created/owned by the crawler")
    p_db.add_argument("blogpost_id", help="Blog post ID to delete (programmatically checked for type=='blogpost', space & crawler ownership)")

    p_ac = sub.add_parser("add-comment", parents=[sub_global], help="[WRITE RIGHT 4/7] Post a footer comment or threaded reply (--parent-comment-id) on a page")
    p_ac.add_argument("page_id", help="Target page ID")
    p_ac.add_argument("--body", help="Comment body string")
    p_ac.add_argument("--body-file", help="Path to file containing comment body")
    p_ac.add_argument("--input-format", choices=["storage", "markdown"], default="storage", help="Input body format (default: storage XHTML)")
    p_ac.add_argument("--parent-comment-id", help="Optional parent comment ID for threaded replies")

    p_uc = sub.add_parser("update-comment", parents=[sub_global], help="[WRITE RIGHT 5/7 — GUARDED OWN] Edit a comment ONLY if created/owned by the crawler")
    p_uc.add_argument("comment_id", help="Comment ID to update (programmatically checked for type=='comment', space & crawler ownership)")
    p_uc.add_argument("--page-id", default=None, help="Optional expected parent page ID to cross-check before updating")
    p_uc.add_argument("--mark-done", action="store_true", help="Prepend a Green [DONE] Confluence status badge to the own comment")
    p_uc.add_argument("--status-label", default="DONE", help="Custom status badge label when using --mark-done (default: DONE)")
    p_uc.add_argument("--append", help="Text/Markdown to append to the existing comment body")
    p_uc.add_argument("--prepend", help="Text/Markdown to prepend to the existing comment body")
    p_uc.add_argument("--body", help="Full replacement comment body string")
    p_uc.add_argument("--body-file", help="File containing full replacement comment body")
    p_uc.add_argument("--input-format", choices=["storage", "markdown"], default="storage", help="Format of --append/--prepend/--body")

    p_dc = sub.add_parser("delete-comment", parents=[sub_global], help="[WRITE RIGHT 6/7 — GUARDED] Delete a comment ONLY if owned by crawler OR mentions crawler")
    p_dc.add_argument("comment_id", help="Comment ID to delete (programmatically checked for type=='comment', space & own/mention)")
    p_dc.add_argument("--page-id", default=None, help="Optional expected parent page ID to cross-check before deleting")

    p_like = sub.add_parser("like", parents=[sub_global], help="[WRITE RIGHT 7/7] Add a 'Like' reaction to a comment (marks mentioned instruction as DONE) or page/blogpost")
    p_like.add_argument("content_id", help="Numeric comment ID, page ID, or blogpost ID to like")

    p_unlike = sub.add_parser("unlike", parents=[sub_global], help="Remove the crawler's 'Like' reaction from a comment, page, or blogpost")
    p_unlike.add_argument("content_id", help="Numeric comment ID, page ID, or blogpost ID to unlike")

    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        if args.command == "resolve-url" and getattr(args, "offline_tiny_only", False):
            cmd_resolve_url(None, args)  # type: ignore[arg-type]
            return

        client = GuardedCrawlerConfluenceClient(
            base_url=args.base_url,
            settings_file=args.settings_file,
            timeout=args.timeout,
            verify_ssl=not args.insecure,
            throttle_ms=args.throttle_ms,
        )

        dispatch = {
            "whoami": cmd_whoami,
            "resolve-url": cmd_resolve_url,
            "spaces": cmd_spaces,
            "get-page": cmd_get_page,
            "search": cmd_search,
            "recent-edits": cmd_recent_edits,
            "history": cmd_history,
            "diff": cmd_diff,
            "crawl": cmd_crawl,
            "attachments": cmd_attachments,
            "comments": cmd_comments,
            "find-mentions": cmd_find_mentions,
            "verify-comment": cmd_verify_comment,
            "verify-blogpost": cmd_verify_blogpost,
            "likes": cmd_likes,
            "metadata": cmd_metadata,
            "export-pdf": cmd_export_pdf,
            "raw-get": cmd_raw_get,
            "create-blogpost": cmd_create_blogpost,
            "update-blogpost": cmd_update_blogpost,
            "delete-blogpost": cmd_delete_blogpost,
            "add-comment": cmd_add_comment,
            "update-comment": cmd_update_comment,
            "delete-comment": cmd_delete_comment,
            "like": cmd_like,
            "unlike": cmd_unlike,
        }
        dispatch[args.command](client, args)
    except (PermissionError, ValueError, LookupError, RuntimeError) as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
