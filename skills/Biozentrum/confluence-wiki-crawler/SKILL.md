---
name: confluence-wiki-crawler
description: Space-guarded Atlassian Confluence Data Center / Server crawler skill with read-only spaces, elevated-access spaces (read-only pages + guarded own blogposts + page comments/replies + likes as DONE + guarded own/mentioned comment deletion), progressive 3-tier search starting from CONFLUENCE_DEFAULT_PAGE, 3-tier historical version & time-window diff/todo/grep analysis, and zero hardcoded instance details. Use whenever the user wants to read, search, diff, crawl, inspect revision history, track tasks/checklists, download attachments/images, or post guarded comments, likes, or blog posts on a Confluence wiki.
---

# Goal

Interact with an Atlassian Confluence Data Center / Server wiki instance (configured via `CONFLUENCE_BASE_URL` in `access_settings.env`) as an **interactive instruction-following crawler agent** that clearly separates **Space Permission Tiers** (`CONFLUENCE_ELEVATED_ACCESS_SPACES` and `CONFLUENCE_READONLY_SPACES`) from **Progressive Initial Search Scope** (`CONFLUENCE_DEFAULT_PAGE` and `CONFLUENCE_DEFAULT_SPACES`), with **strictly read-only access for all wiki pages and attachments**, plus **seven tightly guarded write operations** (permitted **only** in spaces listed in `CONFLUENCE_ELEVATED_ACCESS_SPACES`):
1. **Create a Blog Post** (`create-blogpost`: `POST /rest/api/content` with `type: "blogpost"`)
2. **Edit OWN Blog Post ONLY** (`update-blogpost`: `PUT /rest/api/content/{blogpost_id}` with `type: "blogpost"`, permitted **only** after a programmatic server-side pre-flight check verifies `type == "blogpost"`, verifies the space is in `CONFLUENCE_ELEVATED_ACCESS_SPACES`, AND double-checks that `history.createdBy` matches the crawler's identity in `access_settings.env`)
3. **Delete OWN Blog Post ONLY** (`delete-blogpost`: `DELETE /rest/api/content/{blogpost_id}`, permitted **only** after a programmatic server-side pre-flight check verifies `type == "blogpost"`, verifies the space is in `CONFLUENCE_ELEVATED_ACCESS_SPACES`, AND double-checks that `history.createdBy` matches the crawler's identity in `access_settings.env`)
4. **Add a Comment (or Threaded Reply) to a Page** (`add-comment`: `POST /rest/api/content` with `type: "comment"` and `container.type: "page"`, supporting `--parent-comment-id <COMMENT_ID>` for threaded replies)
5. **Edit OWN Comment ONLY** (`update-comment`: `PUT /rest/api/content/{comment_id}` with `type: "comment"`, permitted **only** after a programmatic server-side pre-flight check confirms `type == "comment"`, confirms the space is in `CONFLUENCE_ELEVATED_ACCESS_SPACES`, AND confirms `history.createdBy` matches the crawler's identity. Note: Confluence Server does not allow non-admin users to edit another user's comment; to respond to or mark another user's `@mention` comment as DONE, either reply to it, like it, or delete it)
6. **Delete an Existing Comment ONLY IF Owned by Crawler OR Mentioning Crawler** (`delete-comment`: `DELETE /rest/api/content/{comment_id}`, permitted **only** in `CONFLUENCE_ELEVATED_ACCESS_SPACES` after a programmatic server-side pre-flight check confirms `type == "comment"` AND either `history.createdBy` matches the crawler's identity OR the comment mentions the crawler)
7. **Like / Unlike a Comment, Page, or Blog Post** (`like` / `unlike`: `POST` or `DELETE /rest/likes/1.0/content/{id}/likes`, permitted **only** in `CONFLUENCE_ELEVATED_ACCESS_SPACES`, allowing the crawler to "Like" a user's `@mention` instruction comment to mark it as **DONE** without deleting it)

All other operations—including accessing spaces not listed in `CONFLUENCE_READONLY_SPACES` or `CONFLUENCE_ELEVATED_ACCESS_SPACES`, performing any write/comment/blogpost/like operation in a `CONFLUENCE_READONLY_SPACES` space, creating/editing/moving/deleting any wiki **page** (`type == "page"`), editing/deleting another user's blog post (even if it mentions the crawler), editing another user's comment, deleting another user's comment that does not mention the crawler, and uploading/deleting attachments—are strictly prohibited and blocked in Python code before any network call is made.

---

# Guarded Crawler Security Policy

1. **Never Read `access_settings.env` Directly or Use Unguarded HTTP Calls (`curl`, `wget`, etc.):**
   - **CRITICAL:** In Confluence Data Center, the `Blog -> Add` space permission grants server-side permission to edit *any* unrestricted blog post in the space, and the crawler account may have server-level visibility into spaces it should not touch. Therefore, the Python guards in `GuardedCrawlerConfluenceClient` (`scripts/confluence_wiki.py`) are a **load-bearing security boundary**, not just a convenience wrapper.
   - **NEVER** extract `CONFLUENCE_CRAWLER_API_KEY` from `access_settings.env` using `cat`, `grep`, or shell commands, and **NEVER** call Confluence using raw `curl`, `wget`, or unguarded HTTP clients.
   - **ALWAYS** execute Confluence operations through `scripts/confluence_wiki.py` (which includes `raw-get` for any custom read-only `GET` endpoint) or by importing `GuardedCrawlerConfluenceClient` from `scripts/confluence_wiki.py` in Python.
2. **Space Permission Tiers vs. Progressive Initial Search Scope (`access_settings.env`):**
   - **Space Permission Tiers (Where the crawler is *allowed* to read vs. write):**
     - **`CONFLUENCE_ELEVATED_ACCESS_SPACES` (Elevated Guarded Write + Read Access):** Comma-separated list of space keys where the crawler is allowed both read access and the 7 guarded crawler write operations above (`access_mode: "elevated-access"`). You can list one or multiple spaces here.
     - **`CONFLUENCE_READONLY_SPACES` (Strictly Read-Only Access):** Comma-separated list of space keys (or `*`) where the crawler may read pages, blogposts, comments, attachments, version histories, and likes (`access_mode: "read-only"`), but **all** writes (`create-blogpost`, `update-blogpost`, `delete-blogpost`, `add-comment`, `update-comment`, `delete-comment`, `like`, `unlike`) are blocked client-side with a `PermissionError`. If a space key is listed in both `CONFLUENCE_READONLY_SPACES` and `CONFLUENCE_ELEVATED_ACCESS_SPACES`, `CONFLUENCE_READONLY_SPACES` takes precedence.
   - **Progressive Initial Search Scope (Where the crawler searches *first* to minimize Confluence server query load):**
     - **`CONFLUENCE_DEFAULT_PAGE` (Tier 1 — Default Starting Page(s) & Subtree(s)):** Accepts one or more comma-separated page URLs, `/x/...` tiny links, or numeric page IDs (accessible via `get-page --default` and `crawl --default`). When `CONFLUENCE_DEFAULT_PAGE` is configured, initial unflagged `search` queries **first search only within the `CONFLUENCE_DEFAULT_PAGE` page/subtree(s)** (`(id IN (...) OR ancestor IN (...))`) so the server does minimal work! If `0` matches are found in the default page subtree(s) (and `--default-pages-only` was not passed), `search` **automatically widens to Tier 2** (`CONFLUENCE_DEFAULT_SPACES`).
     - **`CONFLUENCE_DEFAULT_SPACES` (Tier 2 — Default Space(s) for Initial Space-Level Searches):** Optional comma-separated list of space keys for default space-level searches (`search --default-spaces`, `recent-edits`, `find-mentions`, or automatic fallback when Tier 1 returns 0 hits). If left blank, defaults automatically to `CONFLUENCE_ELEVATED_ACCESS_SPACES`.
     - **Going Wider (`--all-spaces` / `--wider` or `--space <KEY>` — Tier 3):** Whenever you need to search beyond the default page(s)/space(s), pass `--all-spaces` (or `--wider`) or `--space <KEY>` to expand across all allowed spaces (`CONFLUENCE_ELEVATED_ACCESS_SPACES` + `CONFLUENCE_READONLY_SPACES`).
   - **Unlisted Spaces & Server Space Discovery (`spaces`):** Any space key not listed in `CONFLUENCE_ELEVATED_ACCESS_SPACES` or `CONFLUENCE_READONLY_SPACES` is blocked for reading and writing content. However, running `spaces` lists **all** spaces visible to the crawler token on the Confluence server (space-level metadata only, never page content) and tags each with `access_mode`: `"elevated-access"`, `"read-only"`, or `"blocked_by_local_settings"`. Furthermore, when an operation on an unlisted space is blocked, the client probes `/rest/api/space/<KEY>` so the `PermissionError` tells you whether the crawler token **already has server-side `View` access** (so the user only needs to add `<KEY>` to `access_settings.env`) or **also needs `View` permission granted by a Space Admin on the Confluence server**.
3. **Mandatory Live Token-to-`.env` Identity Cross-Check (`_verify_authenticated_token_matches_crawler_env`):**
   - Before any write (`POST`), edit (`PUT`), or delete (`DELETE`) operation, `GuardedCrawlerConfluenceClient` queries `/rest/api/user/current` (`whoami`) once per session and verifies that the authenticated token's `username` and `displayName` match `CONFLUENCE_CRAWLER_USERNAME` and `CONFLUENCE_CRAWLER_NAME` in `access_settings.env`.
   - If a human or admin PAT token is ever accidentally placed into `access_settings.env`, the client immediately raises a `PermissionError` and refuses all write/edit/delete operations.
4. **Mandatory Programmatic Pre-Flight Checks Before Any Edit or Delete:**
   - **For Blog Posts (`_verify_own_blogpost_on_server` — Load-Bearing Ownership Guard):**
     Loads `GET /rest/api/content/{blogpost_id}?expand=history,version,body.storage,body.view,space` and enforces:
     1. **Exact Numeric ID Match:** `str(existing["id"]) == str(blogpost_id)`.
     2. **Strict Content Type == `"blogpost"`:** `existing["type"] == "blogpost"`. If a page ID or comment ID is passed, it immediately raises a `PermissionError` (`Pages can NEVER be edited or deleted`).
     3. **Writable Space Check (`_assert_space_writable`):** Confirms `existing["space"]["key"]` is in `CONFLUENCE_ELEVATED_ACCESS_SPACES` and not in `CONFLUENCE_READONLY_SPACES`.
     4. **Double-Checked Ownership (`is_content_owned_by_crawler`):** Inspects `existing["history"]["createdBy"]` (the original creator of the blog post). When both `CONFLUENCE_CRAWLER_USERNAME` and `CONFLUENCE_CRAWLER_NAME` are set in `access_settings.env`, **both** `createdBy.username` AND `createdBy.displayName` must match (case-insensitively). Mentioning the crawler inside another user's blog post does **not** grant permission to edit or delete that blog post.
   - **For Comments (`_verify_comment_editable_or_deletable_on_server`):**
     Loads `GET /rest/api/content/{comment_id}?expand=history,version,body.storage,body.view,container,container.space,ancestors,space` and enforces:
     1. **Exact Numeric ID Match:** `str(existing["id"]) == str(comment_id)`.
     2. **Strict Content Type == `"comment"`:** `existing["type"] == "comment"`. A hallucinated `page` or `blogpost` ID can **never** be deleted or edited via comment commands.
     3. **Parent Page Cross-Check (`--page-id`):** When `--page-id <page_id>` is supplied, verifies `existing["container"]["id"] == str(page_id)`.
     4. **Writable Space Check (`_assert_space_writable`):** Confirms the comment's space is in `CONFLUENCE_ELEVATED_ACCESS_SPACES` and not in `CONFLUENCE_READONLY_SPACES`.
     5. **Action-Specific Authorization Check:**
        - **Editing (`update-comment`):** Requires `is_content_owned_by_crawler(existing) == True` (`history.createdBy` matches the crawler's `CONFLUENCE_CRAWLER_USERNAME` and `CONFLUENCE_CRAWLER_NAME`). Confluence Server forbids non-admins from editing another user's comment via REST (`HTTP 403`).
        - **Deleting (`delete-comment`):** Requires **either** `is_content_owned_by_crawler(existing) == True` **or** `comment_mentions_crawler(existing) == True` (`body.view` or `body.storage` mentions the crawler).
5. **Confluence Data Center Server Permission Semantics (Comments, Likes & Blog Posts):**
   - **Liking as `DONE` (`like <comment_id>`):** `/rest/likes/1.0/content/{id}/likes` requires only `View` permission on the server (and `CONFLUENCE_ELEVATED_ACCESS_SPACES` in the client guard). Liking a user's `@mention` comment sets `liked_by_crawler: true` and `is_done: true` in `find-mentions` and `comments`, making **Reply + Like** the universal, non-destructive way to complete instructions.
   - **Deleting Own vs. Other Users' Comments:**
     - **Own Unreplied Comments (`can_delete_own_unreplied: true`):** Confluence Data Center allows any comment author to delete their own comment **if it has no replies**, even when space permissions have `All -> Delete Own` = ❌ and `Comments -> Delete` = ❌.
     - **Other Users' `@mention` Comments (or Replied Own Comments) (`may_attempt_delete: true`):** Allowed by the Python guard in elevated-access spaces, but succeeds on the server **only** if the space admin has granted `Comments -> Delete` = ✅ to the crawler. If `Comments -> Delete` is ❌, Confluence Server returns `HTTP 403 Unable to purge content`—do **not** retry; use `like <comment_id>` and `add-comment --parent-comment-id <comment_id>` instead.
   - **How `@mentions` Are Stored (`body.storage` vs `body.view`):** In raw XHTML (`body.storage.value`), Confluence stores user mentions as `<ac:link><ri:user ri:userkey="..."/></ac:link>` (containing only an internal hex `userkey`). When queried with `expand=body.storage,body.view`, `body.view.value` expands that tag into `<a class="confluence-userlink user-mention" data-username="<crawler_username>" href=".../display/~<crawler_username>"><CrawlerDisplayName></a>`. `GuardedCrawlerConfluenceClient` always inspects **both** `body.view.value` and `body.storage.value`.

---

# Prerequisites & Configuration (`access_settings.env`)

All instance-specific settings, credentials, crawler identity tokens, space permission tiers, and default search scopes are stored in `access_settings.env` inside this skill folder (which is git-ignored via `*.env` so personal or institutional details are never committed to GitHub; copy `access_settings.env.example` to `access_settings.env` to get started).

- **Configuration Discovery Order:**
  1. Explicit CLI flag `--settings-file /path/to/access_settings.env` (or `CONFLUENCE_SETTINGS_FILE` environment variable)
  2. `<SKILL_DIR>/access_settings.env` (automatically resolved relative to `scripts/confluence_wiki.py`)
  3. Standard environment variables (`CONFLUENCE_BASE_URL`, `CONFLUENCE_CRAWLER_API_KEY`, `CONFLUENCE_CRAWLER_USERNAME`, `CONFLUENCE_CRAWLER_NAME`, `CONFLUENCE_ELEVATED_ACCESS_SPACES`, `CONFLUENCE_READONLY_SPACES`, `CONFLUENCE_DEFAULT_PAGE`, `CONFLUENCE_DEFAULT_SPACES`, `CONFLUENCE_THROTTLE_MS`)
- **Python Dependencies:** Python 3.8+ with `requests` (and optionally `beautifulsoup4` for enhanced XHTML-to-Markdown conversion; falls back automatically to a built-in stdlib regex converter if `beautifulsoup4` is not installed).
- **Settings in `access_settings.env`:**
  1. `CONFLUENCE_BASE_URL=https://wiki.example.org`
     - Base URL of your Confluence Data Center / Server instance (no trailing slash).
  2. `CONFLUENCE_CRAWLER_API_KEY=<your_pat_token_or_user:pass>`
     - Personal Access Token (PAT) or `username:password` for the dedicated crawler account.
  3. `CONFLUENCE_CRAWLER_USERNAME=<crawler_short_username>`
     - The crawler account's short Confluence username (as stored in `history.createdBy.username` or after `~` in `/display/~<username>`).
  4. `CONFLUENCE_CRAWLER_NAME=<crawler_full_display_name>`
     - The crawler account's full Confluence display name (as stored in `history.createdBy.displayName` and rendered `@mentions`).
     - Setting **both** `CONFLUENCE_CRAWLER_USERNAME` and `CONFLUENCE_CRAWLER_NAME` enables strict double-checking of both token identity (`whoami`) and content ownership (`createdBy.username` AND `createdBy.displayName` must both match) while matching either identifier in comment `@mentions`.
  5. `CONFLUENCE_ELEVATED_ACCESS_SPACES=SPACE_RW1, SPACE_RW2`
     - Comma-separated list of space keys where the crawler has **elevated guarded access** (read-only pages + create/edit/delete own blogposts + add comments/replies + edit own comments + delete own/mentioned comments + like/unlike).
  6. `CONFLUENCE_READONLY_SPACES=SPACE_RO1, SPACE_RO2`
     - Comma-separated list of space keys where the script enforces **strictly read-only** access (reading/searching/crawling allowed; all writes/comments/blogposts/likes blocked). Leave empty if you have no read-only spaces, or set to `*` to allow reading all spaces not listed in `CONFLUENCE_ELEVATED_ACCESS_SPACES`.
  7. `CONFLUENCE_DEFAULT_PAGE=https://wiki.example.org/x/AbCdEf`
     - Recommended starting page(s) (comma-separated list of page URLs, `/x/...` tiny links, or numeric page IDs). Used by `get-page --default` and `crawl --default`, and **searched first (with its descendant subtree) on initial `search` queries** to minimize Confluence server load before widening to `CONFLUENCE_DEFAULT_SPACES`.
  8. `CONFLUENCE_DEFAULT_SPACES=`
     - Optional comma-separated list of space keys for default space-level searches (`search --default-spaces`, `recent-edits`, `find-mentions`, or automatic fallback when `CONFLUENCE_DEFAULT_PAGE` subtrees return 0 matches). If left blank, defaults automatically to `CONFLUENCE_ELEVATED_ACCESS_SPACES`.
  9. `CONFLUENCE_THROTTLE_MS=0` (Optional)
     - Minimum polite delay in milliseconds between consecutive HTTP requests to reduce load on the Confluence server (can also be passed via `--throttle-ms <MS>` either before or after the CLI subcommand).

---

# Two Sanctioned Ways to Interact with Confluence

> **IMPORTANT SECURITY RULE:** Always use **Method 1** or **Method 2** below. Never read `access_settings.env` directly in shell commands and never use raw `curl` or unguarded HTTP clients.

## Method 1: Bundled Guarded Crawler CLI (`scripts/confluence_wiki.py` — Recommended First Choice)

Run `scripts/confluence_wiki.py` (resolved relative to this skill's directory) as a black-box CLI utility. For any custom read-only REST endpoint not covered by a dedicated subcommand, use the `raw-get` subcommand:

```bash
# Run from the skill directory (or prefix with the path to this skill directory):
python3 scripts/confluence_wiki.py --help
```

## Method 2: Custom Python Scripts (Importing `GuardedCrawlerConfluenceClient`)

For multi-step or bespoke Python workflows, add this skill's `scripts/` directory to `sys.path` and import `GuardedCrawlerConfluenceClient`, `load_crawler_identity`, `load_space_access_policy`, `is_content_owned_by_crawler`, `is_liked_by_crawler`, `comment_mentions_crawler`, `storage_to_markdown`, `markdown_to_storage`, `extract_tasks_from_storage_or_markdown`, `diff_tasks`, and `decode_tiny_link` from `confluence_wiki`:
- `client = GuardedCrawlerConfluenceClient(throttle_ms=None)` (loads `CONFLUENCE_BASE_URL`, `CONFLUENCE_CRAWLER_API_KEY`, `CONFLUENCE_CRAWLER_USERNAME`, `CONFLUENCE_CRAWLER_NAME`, `CONFLUENCE_ELEVATED_ACCESS_SPACES`, `CONFLUENCE_READONLY_SPACES`, `CONFLUENCE_DEFAULT_PAGE`, and `CONFLUENCE_DEFAULT_SPACES` from `access_settings.env` and enforces all space and ownership guards)
- `client.get_space_access_mode(space_key) -> str` (`"read-only"`, `"elevated-access"`, or `"blocked"`)
- `client.request(method, endpoint, params=None, json_body=None, stream=False) -> requests.Response` (**Note:** the JSON payload parameter is `json_body=...`, NOT `json=...`; returns a raw `requests.Response` object, whereas `client.get_json(...)` returns a parsed `dict`)
- `client.get_json(path, params=...) -> dict` (parses JSON response)
- `client.get_paginated(path, params=..., max_items=100, stop_when=None) -> list[dict]` (unwraps paginated `"results"` arrays; automatically respects server-side `limit` caps like `100 -> 50` and `_links.next`, and stops early if `stop_when(item)` returns `True`)
- `client.get_page(page_id, expand="body.storage,version,space", version=None) -> dict` (includes per-session in-memory cache so repeated fetches of the same `(page_id, expand, version)` cost zero extra HTTP requests, and normalizes historical revision `id` / `_links.webui` to the canonical `page_id`)
- `client.get_version_list(content_id, limit=50, since=None, until=None) -> dict` (**Returns a `dict`:** `{"version_source": "rest_experimental_version", "versions": [...]}` in newest-first order; when `since` is supplied, pagination stops early as soon as versions are older than `since`, avoiding fetching thousands of old revisions on high-version pages)
- `client.resolve_version_range_by_time(content_id, since=None, until=None) -> dict` (resolves `from_version` right before `since` and `to_version` inside `[since, until]` using early-exit version pagination)
- `client.create_blogpost(space_key, title, storage_body) -> dict`
- `client.update_own_blogpost(blogpost_id, title=None, new_storage_body=None, append_storage=None, prepend_storage=None, version_comment=None) -> dict`
- `client.delete_own_blogpost(blogpost_id) -> dict`
- `client.add_page_comment(page_id, storage_body, parent_comment_id=None) -> dict`
- `client.update_own_comment(comment_id, new_storage_body=None, mark_done=False, status_label="DONE", append_storage=None, prepend_storage=None, expected_page_id=None) -> dict`
- `client.delete_comment(comment_id, expected_page_id=None) -> dict`
- `client.get_likes(content_id) -> dict`
- `client.like_content(content_id) -> dict`
- `client.unlike_content(content_id) -> dict`
- `storage_to_markdown(xhtml_str) -> str` (converts Confluence `body.storage` XHTML—including tables, macros, and `<ac:task>` checkboxes—into clean Markdown)
- `markdown_to_storage(md_str) -> str` (converts Markdown into valid Confluence XHTML storage format)
- `extract_tasks_from_storage_or_markdown(raw_storage, md_text=None) -> list[dict]` (returns a list of task dicts: `[{"task_id": "...", "status": "complete"|"incomplete", "checked": bool, "text": "..."}]`)
- `diff_tasks(tasks_from, tasks_to) -> dict` (compares two task lists—or `(raw_from, md_from, raw_to, md_to)`—and returns `completed_in_window` `[ ] -> [x]`, `reopened_in_window` `[x] -> [ ]`, `added_open_in_window`, `added_completed_in_window`, `removed_in_window`, and `still_open_tasks`)

---

# Part I: Read, Search, Crawl & Inspection Operations (Allowed in `CONFLUENCE_READONLY_SPACES` & `CONFLUENCE_ELEVATED_ACCESS_SPACES`)

## 1. Resolving Any Confluence Wiki URL Format

Users share Confluence links in five common URL formats. Resolve any of them to a numeric `pageId` and metadata:

| URL Type | Example URL | Resolution Mechanism |
|---|---|---|
| **Page ID Action URL** | `https://wiki.example.org/pages/viewpage.action?pageId=123456` | Extract `pageId=123456` query parameter directly |
| **Pretty Space + Title URL** | `https://wiki.example.org/display/SPACEKEY/Page+Title` | `GET /rest/api/content?spaceKey=SPACEKEY&title=Page%20Title` |
| **Tiny Link (Short URL)** | `https://wiki.example.org/x/QOIB` | Little-endian Base64 decode of `QOIB` → numeric `pageId` (`123456`, or follow 302 redirect) |
| **Spaces Path URL** | `https://wiki.example.org/spaces/SPACEKEY/pages/123456/Title` | Extract numeric ID after `/pages/` |
| **Attachment Download URL** | `https://wiki.example.org/download/attachments/123456/architecture.pdf` | Parent `pageId=123456`, attachment `filename=architecture.pdf` |

```bash
# Resolve any URL against the live API
python3 scripts/confluence_wiki.py resolve-url \
  "https://wiki.example.org/display/SPACEKEY/Page+Title"

# Decode a /x/<code> tiny link offline (zero network calls)
python3 scripts/confluence_wiki.py resolve-url \
  "https://wiki.example.org/x/QOIB" --offline-tiny-only
```

## 2. Discovering Spaces, Loading the Recommended Default Page, Progressive Searching & Crawling

```bash
# Check authenticated crawler identity, token match, recommended_start_pages, and configured space access policy
python3 scripts/confluence_wiki.py whoami

# List all spaces visible to the token on the server (annotated with access_mode:
# "elevated-access", "read-only", or "blocked_by_local_settings"; pass --only-allowed to hide blocked ones)
python3 scripts/confluence_wiki.py spaces --type all --limit 200

# Read the recommended starting page (CONFLUENCE_DEFAULT_PAGE or homepage of first default space)
python3 scripts/confluence_wiki.py get-page --default --format markdown

# Progressive search (Tier 1 -> Tier 2 automatic):
# Tries CONFLUENCE_DEFAULT_PAGE subtree(s) first (minimal server work); if 0 hits, widens to CONFLUENCE_DEFAULT_SPACES
# Excerpts automatically strip Confluence @@@hl@@@ / @@@endhl@@@ highlight tags.
python3 scripts/confluence_wiki.py search \
  --query "architecture overview" --limit 20

# Exact contiguous phrase search (--phrase) and/or title-only search (--title-only) to avoid tokenized noise
# (e.g. prevents "project roadmap" from matching pages that merely contain "project" and "roadmap" in separate paragraphs)
python3 scripts/confluence_wiki.py search \
  --query "project roadmap" --phrase --title-only --limit 20

# Strict Tier 1 search (ONLY within CONFLUENCE_DEFAULT_PAGE subtree(s), without auto-widening)
python3 scripts/confluence_wiki.py search \
  --query "architecture overview" --default-pages-only --limit 20

# Direct Tier 2 search (across CONFLUENCE_DEFAULT_SPACES)
python3 scripts/confluence_wiki.py search \
  --query "architecture overview" --default-spaces --limit 20

# Tier 3 wider search across ALL allowed spaces (CONFLUENCE_ELEVATED_ACCESS_SPACES + CONFLUENCE_READONLY_SPACES)
python3 scripts/confluence_wiki.py search \
  --query "architecture overview" --all-spaces --limit 20

# Find pages or blogposts edited in the last 7 days, OR across an exact date range with per-page & per-author version tallies
# NOTE: Date-only --until 'YYYY-MM-DD' is INCLUSIVE through 23:59:59 of that date (so for Oct 10–15 use --since 2026-10-10 --until 2026-10-15)
python3 scripts/confluence_wiki.py recent-edits --days 7 --limit 30
python3 scripts/confluence_wiki.py recent-edits --since 2026-10-10 --until 2026-10-15 --expand-versions

# Read a specific page or blogpost (or a specific historical --version N) as clean Markdown (or --format storage|view|json)
python3 scripts/confluence_wiki.py get-page \
  --id 123456 --format markdown --output ./outputs/page_123456.md
python3 scripts/confluence_wiki.py get-page \
  --id 123456 --version 42 --format markdown

# Read a page AND auto-download its referenced image/attachment files, rewriting attachment://<filename> links to local paths
python3 scripts/confluence_wiki.py get-page \
  --id 123456 --with-images ./outputs/page_123456_assets --output ./outputs/page_123456.md

# Extract all Confluence <ac:task> and Markdown [ ]/[x] tasks from a page into structured JSON (open vs completed)
python3 scripts/confluence_wiki.py get-page \
  --id 123456 --todos

# Inspect revision history (uses 3-tier resolver with early-exit pagination when --since is given; emits "version_source" and "author_tally")
python3 scripts/confluence_wiki.py history 123456 --limit 50
python3 scripts/confluence_wiki.py history 123456 --since 2026-10-10 --until 2026-10-15

# Compute unified diff between two versions OR net diff across a --since / --until time window (--until YYYY-MM-DD is inclusive through 23:59:59 of that day)
python3 scripts/confluence_wiki.py diff 123456 --from-version 4 --to-version 5
python3 scripts/confluence_wiki.py diff 123456 --since today
python3 scripts/confluence_wiki.py diff 123456 --since 2026-10-10 --until 2026-10-15

# Prove whether a term/identifier was newly added, removed, modified, or pre-existing & unchanged in a time/version window (--grep)
python3 scripts/confluence_wiki.py diff 123456 \
  --since 2026-10-10 --until 2026-10-15 --grep "Protocol v2"

# Compare checkbox/task state flips ([ ] -> [x] completed, [x] -> [ ] reopened, newly added tasks) across a time/version window (--todos)
python3 scripts/confluence_wiki.py diff 123456 \
  --since 2026-10-10 --until 2026-10-15 --todos

# Execute a custom read-only GET request (--params accepts either a JSON object or a URL query string)
python3 scripts/confluence_wiki.py raw-get \
  /rest/api/content/search --params '{"cql": "title ~ \"project roadmap\" AND type = page", "limit": 10}'

# Crawl starting from the recommended default page (--default), a specific --root-id, or --space
python3 scripts/confluence_wiki.py crawl \
  --default --max-depth 3 --max-pages 150 \
  --include-body --output-dir ./outputs/wiki_crawl

# List and download attachments from a page
python3 scripts/confluence_wiki.py attachments 123456 \
  --glob "*.pdf" --download-dir ./outputs/attachments
```

---

# Part II: Creating, Editing & Deleting OWN Blog Posts (Allowed ONLY in `CONFLUENCE_ELEVATED_ACCESS_SPACES`)

In spaces listed in `CONFLUENCE_ELEVATED_ACCESS_SPACES`, the crawler can create new blog posts, and—if it makes a mistake or needs to update a report—edit or delete **only its own** blog posts (`update-blogpost` and `delete-blogpost` programmatically verify `type == "blogpost"`, verify the space is in `CONFLUENCE_ELEVATED_ACCESS_SPACES`, and double-check `history.createdBy` against `CONFLUENCE_CRAWLER_USERNAME` and `CONFLUENCE_CRAWLER_NAME`).

```bash
# 1. Create a new Blog Post in an elevated-access space
python3 scripts/confluence_wiki.py create-blogpost \
  --space "SPACEKEY" \
  --title "Automated Weekly Summary — Page 123456" \
  --body-file ./outputs/blog_report.md \
  --input-format markdown

# 2. Optional dry-run check to verify blogpost 777888 is a blogpost owned by the crawler
python3 scripts/confluence_wiki.py verify-blogpost 777888

# 3. Edit/update OWN Blog Post (append, prepend, replace body, or update title)
python3 scripts/confluence_wiki.py update-blogpost 777888 \
  --append "\n## Addendum\n- Updated summary metrics for Q4." \
  --input-format markdown \
  --comment "Appended addendum"

# 4. Delete OWN Blog Post (if created by mistake; blocks deleting any other user's blogpost or any page)
python3 scripts/confluence_wiki.py delete-blogpost 777888
```

---

# Part III: Discovering, Replying to, Liking (as DONE), Editing & Deleting Comments

Wiki users can leave instructions for the crawler by adding a comment on a page that mentions the crawler's name/handle:
> `[<CrawlerDisplayName>](https://wiki.example.org/display/~<crawler_username>) please review this section, summarize the recent changes, and check whether any action items are still open.`

Because Confluence Server does not allow non-admin users to edit another user's comment text in-place, the crawler has **three non-admin ways** to handle a user's `@mention` instruction comment (in `CONFLUENCE_ELEVATED_ACCESS_SPACES`):
1. **Post a Threaded Reply (`add-comment --parent-comment-id <COMMENT_ID>`)**: Answers directly underneath the user's comment.
2. **Like the Comment (`like <COMMENT_ID>`) — Primary `DONE` Marker**: Adds the crawler's 👍 reaction to the comment, which `find-mentions` and `comments` automatically recognize as `liked_by_crawler: true` and `is_done: true`!
3. **Delete the Mentioned Instruction Comment (`delete-comment <COMMENT_ID>`)**: Guarded by Python so it is only allowed when the comment mentions the crawler (or is the crawler's own comment). Note: On the server, deleting another user's `@mention` comment (or an own comment that already has replies) requires `Comments -> Delete` = ✅ in Space Permissions, whereas deleting the crawler's **own unreplied comment** (`can_delete_own_unreplied: true`) always succeeds even when `Comments -> Delete` is ❌.

In addition, the crawler can **edit its own comments** (`update-comment`) if it needs to correct a reply it posted.

## Step 1: Find Pending Comments Mentioning the Crawler (`find-mentions` or `comments`)

```bash
# Search across CONFLUENCE_DEFAULT_SPACES (or pass --all-spaces/--wider, --space, or --page-id) for pending comments mentioning the crawler
# (By default --exclude-done skips comments already liked by the crawler or marked [DONE])
python3 scripts/confluence_wiki.py find-mentions --exclude-done --limit 50

# Or inspect all comments on a specific page (123456); each includes:
#   "space_access_mode", "is_own_comment", "has_replies", "mentions_crawler", "liked_by_crawler",
#   "replied_by_crawler", "is_done", "can_edit", "can_delete_own_unreplied", "may_attempt_delete"
python3 scripts/confluence_wiki.py comments 123456
python3 scripts/confluence_wiki.py comments 123456 --only-mentions
```

## Step 2: Reply to a Comment (`add-comment`) and/or Like It as `DONE` (`like`)

```bash
# 1. Reply directly to the user's instruction comment (threaded reply) or post a top-level page comment
python3 scripts/confluence_wiki.py add-comment 123456 \
  --parent-comment-id 987654 \
  --body "## Section Review\n- Summarized the three recent revisions.\n- Two action items remain open." \
  --input-format markdown

# 2. Like the user's instruction comment (987654) to mark it as DONE without deleting it
python3 scripts/confluence_wiki.py like 987654

# Inspect likes on any comment, page, or blogpost (or remove the crawler's like with `unlike`)
python3 scripts/confluence_wiki.py likes 987654
python3 scripts/confluence_wiki.py unlike 987654
```

## Step 3: Edit an OWN Comment (`update-comment`) OR Delete an Own/Mentioned Comment (`delete-comment`)

```bash
# Optional dry-run programmatic check on comment 987654
python3 scripts/confluence_wiki.py verify-comment 987654 --page-id 123456

# Edit the crawler's OWN comment (e.g. comment 999111) to fix a mistake or prepend [DONE]
python3 scripts/confluence_wiki.py update-comment 999111 \
  --page-id 123456 \
  --body "## Section Review (Updated)\n- All action items have now been verified." \
  --input-format markdown

# Delete the crawler's own unreplied comment (or a user's @mention comment if Space 'Comments -> Delete' is enabled)
python3 scripts/confluence_wiki.py delete-comment 987654 --page-id 123456
```

---

# Verification

1. Verify `CONFLUENCE_BASE_URL`, token authentication, `token_matches_configured_crawler: true`, `recommended_start_pages`, and configured space access lists (`configured_elevated_access_spaces`, `configured_readonly_spaces`, and `configured_default_search_spaces`):
   ```bash
   python3 scripts/confluence_wiki.py whoami
   ```
2. Before editing or deleting a comment or blog post, you can run `verify-comment <COMMENT_ID> --page-id <PAGE_ID>` or `verify-blogpost <BLOGPOST_ID>` to inspect the programmatic space and ownership/mention check.

---

# Troubleshooting

| Symptom / Error | Cause | Resolution |
|---|---|---|
| `No valid Confluence Base URL found` | `CONFLUENCE_BASE_URL` in `access_settings.env` is empty or still set to `PASTE_YOUR_CONFLUENCE_BASE_URL_HERE` | Set `CONFLUENCE_BASE_URL=https://your-confluence-domain` in `access_settings.env`. |
| `No valid Crawler Confluence API key found` | `access_settings.env` still contains `PASTE_YOUR_CRAWLER_CONFLUENCE_PAT_HERE` | Set `CONFLUENCE_CRAWLER_API_KEY=<token>` in `access_settings.env`. |
| `[CRAWLER GUARD] Crawler identity is not configured` | `CONFLUENCE_CRAWLER_USERNAME` and `CONFLUENCE_CRAWLER_NAME` still have placeholders in `access_settings.env` | Set `CONFLUENCE_CRAWLER_USERNAME=<short_username>` and `CONFLUENCE_CRAWLER_NAME=<full_display_name>` in `access_settings.env`. |
| `[CRAWLER GUARD] Token-to-.env identity mismatch!` | The API token in `access_settings.env` authenticates (`whoami`) as a different user (e.g., an admin's personal token) than `CONFLUENCE_CRAWLER_USERNAME` / `CONFLUENCE_CRAWLER_NAME` | Replace the token in `access_settings.env` with the crawler account's own PAT token so personal/admin tokens are never used by the crawler. |
| `[CRAWLER SPACE GUARD] Refusing to ... space 'X' is configured as strictly READ-ONLY in CONFLUENCE_READONLY_SPACES` | Attempted a write operation (blogpost, comment, reply, edit, delete, or like) in a space listed in `CONFLUENCE_READONLY_SPACES` | Spaces in `CONFLUENCE_READONLY_SPACES` allow reading/searching/crawling only. Move the space key to `CONFLUENCE_ELEVATED_ACCESS_SPACES` in `access_settings.env` only if the crawler should have write access there. |
| `[CRAWLER SPACE GUARD] Refusing to ... space 'X' is NOT in CONFLUENCE_READONLY_SPACES or CONFLUENCE_ELEVATED_ACCESS_SPACES` | Attempted to read or modify a space not listed in either space setting | Add the space key to `CONFLUENCE_READONLY_SPACES` (for read-only access) or `CONFLUENCE_ELEVATED_ACCESS_SPACES` (for elevated guarded write access) in `access_settings.env`. |
| `[CRAWLER GUARD] Refusing to update/delete blogpost ... it is NOT owned by the crawler` | Target blogpost's `history.createdBy` does not match `CONFLUENCE_CRAWLER_USERNAME` and `CONFLUENCE_CRAWLER_NAME` | Only blog posts created by the crawler itself can be edited or deleted. |
| `[CRAWLER GUARD] Refusing to update comment ... although it mentions the crawler, it was authored by another user` | Tried to run `update-comment` on another user's comment (which Confluence Server blocks without Space Admin) | Do **not** edit another user's comment in-place; instead, reply to it (`add-comment <PAGE_ID> --parent-comment-id <COMMENT_ID>`) and/or like it (`like <COMMENT_ID>`). |
| `HTTP 403 ... Unable to purge content with id: ContentId{id=...}` | Confluence Server refused `delete-comment` because `Comments -> Delete` is disabled in Space Permissions (or the crawler's own comment already has replies) | **Do NOT retry.** Mark the instruction comment as DONE by liking it (`like <COMMENT_ID>`) and/or posting a threaded reply (`add-comment <PAGE_ID> --parent-comment-id <COMMENT_ID>`). |
| `HTTP 403 ... Unable to trash content with id: ContentId{id=...}` | Confluence Server refused `delete-blogpost` because `Blog -> Delete` and `Delete Own` are disabled in Space Permissions | **Do NOT retry.** If you need to retract or correct your own blog post, update its title/body via `update-blogpost <BLOGPOST_ID>` instead. |
| `HTTP 403 ... Could not update Content of type : class com.atlassian.confluence.pages.{Page\|Comment}` | Confluence Server refused a page or comment `PUT` (`"authorized": false, "valid": true`) | **Do NOT retry.** Pages are strictly read-only, and only the crawler's own comments and own blog posts may be updated. |
| `[CRAWLER GUARD] Refusing to ... its type on the server is 'page'` | Passed a page ID instead of a `comment_id` or `blogpost_id` | Wiki pages (`type == "page"`) are strictly read-only and can never be edited or deleted. |
| `HTTP 404 (sub-code: -1)` on `/rest/api/content/{id}/version`, `/rest/api/about`, or `/rest/api/serverInfo` | Confluence Data Center 10.x does not expose the Cloud-only `/rest/api/content/{id}/version` or `/rest/api/serverInfo` endpoints | Use the built-in `history <ID>`, `diff <ID> [--since ...]`, or `recent-edits --since X --until Y` commands, which automatically use `client.get_version_list()`'s 3-tier resolver (`GET /rest/api/content/{id}/version` → `GET /rest/experimental/content/{id}/version` → `?version=N` binary-search probe, falling back only on HTTP 404 and reporting `"version_source"`). Never probe `/rest/api/about` or `/rest/api/serverInfo`. |
| `[CRAWLER GUARD] Write operations (...) to '/rest/experimental/...' endpoints are strictly prohibited` | Attempted a write (`POST`/`PUT`/`DELETE`) against `/rest/experimental/...` | Experimental REST endpoints are strictly read-only in this skill (used only for `GET /rest/experimental/content/{id}/version` under the space guard). |
| `Confluence REST endpoint '/rest/api/content' silently ignores the 'cql' parameter` | Passed `cql=...` to `/rest/api/content` instead of `/rest/api/content/search` or `/rest/api/search` | Always use `/rest/api/content/search` or `/rest/api/search` (or the CLI `search` / `recent-edits` commands) when querying with CQL. |
| Off-by-one-day in `--since` / `--until` date ranges | Date-only `--until YYYY-MM-DD` is **inclusive through `23:59:59`** of that date | For edits on Oct 10–15, pass `--since 2026-10-10 --until 2026-10-15` (do not pass `--until 2026-10-16` unless specifying an exact timestamp like `2026-10-16T00:00:00`). Check `"resolved_window"` in the output to verify the exact window. |
