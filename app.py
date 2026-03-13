"""
Hackathon Code Judge — Flask Web App v6
5 unified scoring categories, all 0-20:
  1. prototype_quality
  2. code_quality
  3. innovation_doc_topic   (Innovation + Documentation + Topic Alignment)
  4. security               (hardcoded keys, injection risks, unvalidated inputs)
  5. performance_maintainability (nested loops, blocking ops, leaks, complexity, DRY)

Two modes:
  standard — above 5 categories, no cheat detection
  full      — above 5 categories + cheat detection
"""

import os
import json
import time
from pathlib import Path
from typing import Optional, Union
from flask import Flask, render_template, request, jsonify, Response, stream_with_context
import requests as req
import anthropic

app = Flask(__name__)

# ── Load hackathon config ──────────────────────────────────────────────────────
HACKATHON_CONFIG_FILE = Path(__file__).parent / "hackathon.json"

def load_hackathon_config() -> dict:
    defaults = {
        "name":          os.environ.get("HACKATHON_NAME", "Hackathon"),
        "edition":       os.environ.get("HACKATHON_EDITION", ""),
        "topic":         os.environ.get("HACKATHON_TOPIC", ""),
        "judging_notes": os.environ.get("HACKATHON_JUDGING_NOTES", ""),
    }
    if HACKATHON_CONFIG_FILE.exists():
        try:
            data = json.loads(HACKATHON_CONFIG_FILE.read_text())
            for key in defaults:
                env_val = os.environ.get("HACKATHON_" + key.upper())
                if env_val:
                    data[key] = env_val
            return {**defaults, **data}
        except Exception:
            pass
    return defaults

HACKATHON = load_hackathon_config()

# ── Constants ──────────────────────────────────────────────────────────────────
SKIP_EXTENSIONS = {
    ".png",".jpg",".jpeg",".gif",".svg",".ico",".webp",".bmp",
    ".mp4",".mp3",".wav",".zip",".tar",".gz",".lock",".pdf",
    ".ttf",".woff",".woff2",".eot",".bin",".exe",".dll",".map",
    ".min.js",".min.css",".d.ts",
}
SKIP_DIRS = {
    "node_modules",".git","vendor","dist","build","__pycache__",
    ".next",".nuxt","coverage",".venv","venv","env","migrations",
    "static","public","assets","images","fonts","test","tests",
    "__tests__","spec","specs","fixtures","mocks","stubs",
}

# Priority tiers — higher tier = fetched first, given more token budget
FILE_PRIORITY = {
    # Tier 1 — core entrypoints & config (always include, full content)
    1: {"readme.md","readme.txt","readme","main.py","app.py","server.py",
        "index.js","index.ts","app.js","app.ts","server.js","server.ts",
        "main.go","main.rs","main.java","package.json","requirements.txt",
        "pyproject.toml","go.mod","cargo.toml","dockerfile","docker-compose.yml",
        "docker-compose.yaml",".env.example","config.py","settings.py","config.js",
        "config.ts","next.config.js","vite.config.js","vite.config.ts"},
    # Tier 2 — routers, models, core logic
    2: {"routes.py","models.py","schema.py","schemas.py","views.py",
        "controllers.py","handlers.py","middleware.py","auth.py","db.py",
        "database.py","router.js","router.ts","routes.js","routes.ts",
        "models.js","models.ts","api.js","api.ts","store.js","store.ts"},
}

# Max characters to send per file depending on tier
CHAR_BUDGET = {
    1: 6000,   # entrypoints — generous
    2: 3000,   # supporting files — moderate
    3: 1500,   # everything else — just enough to detect patterns
}
TOTAL_CHAR_LIMIT = 80_000   # ~20k tokens — safe Claude context budget for analysis
SCORE_RUBRIC = [
    (0,  4,  "Non-Functional", "Prototype is non-functional or severely unstable."),
    (5,  8,  "Basic",          "Functional but lacks key features or has significant bugs."),
    (9,  12, "Functional",     "Clear architecture with working core features."),
    (13, 16, "Robust",         "Reliable prototype that seamlessly integrates with the ecosystem."),
    (17, 20, "Exceptional",    "Technically sound, fully working, and feels native to the target app."),
]

# ── GitHub helpers ─────────────────────────────────────────────────────────────
def parse_github_url(url: str):
    url = url.strip().rstrip("/").replace(".git", "")
    if "/tree/" in url:
        url = url.split("/tree/")[0]
    parts = url.split("github.com/")
    if len(parts) != 2:
        return None, None
    segments = parts[1].split("/")
    if len(segments) < 2:
        return None, None
    return segments[0], segments[1]


def github_api(path: str, token: Optional[str] = None) -> Union[dict, list]:
    headers = {"Accept": "application/vnd.github.v3+json"}
    if token:
        headers["Authorization"] = "token " + token
    resp = req.get("https://api.github.com" + path, headers=headers, timeout=15)
    if resp.status_code in (404, 403, 401):
        return {}
    resp.raise_for_status()
    return resp.json()


def fetch_file_content(url: str, token: Optional[str] = None, char_limit: int = 6000) -> Optional[str]:
    headers = {}
    if token:
        headers["Authorization"] = "token " + token
    try:
        resp = req.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        text = resp.text
        if len(text) > char_limit:
            # Keep first 80% and last 20% — head has imports/config, tail has main logic
            head = int(char_limit * 0.8)
            tail = char_limit - head
            text = text[:head] + "\n\n... [truncated " + str(len(resp.text) - char_limit) + " chars] ...\n\n" + text[-tail:]
        return text
    except Exception:
        return None


def get_file_tier(name: str, path: str) -> int:
    name_lower = name.lower()
    path_lower = path.lower()
    if name_lower in FILE_PRIORITY[1]:
        return 1
    if name_lower in FILE_PRIORITY[2]:
        return 2
    # Tier 1 by path pattern — entrypoint-like files anywhere
    if any(part in path_lower for part in ["/main.", "/app.", "/server.", "/index.", "/api/"]):
        return 1
    return 3


def collect_repo_files(owner: str, repo: str, token: Optional[str]) -> tuple[dict, dict]:
    """
    Returns (files_dict, stats_dict).
    Smart collection: two-pass approach.
      Pass 1 — crawl entire tree to build a manifest (no file downloads yet).
      Pass 2 — download files in priority order, stop when char budget is reached.
    This prevents wasting GitHub API calls on low-value files in big repos.
    """
    # ── Pass 1: Crawl tree, collect manifest ──────────────────────────────────
    manifest = []   # list of (tier, size, name, path, download_url)
    dirs_crawled = 0

    def walk(path: str = "", depth: int = 0):
        nonlocal dirs_crawled
        if dirs_crawled > 120:   # hard cap on API calls during crawl
            return
        try:
            items = github_api("/repos/" + owner + "/" + repo + "/contents/" + path.lstrip("/"), token)
        except Exception:
            return
        if isinstance(items, dict):
            items = [items]
        dirs_crawled += 1
        for item in items:
            name  = item.get("name", "")
            ipath = item.get("path", "")
            size  = item.get("size", 0)
            if item.get("type") == "dir":
                if name.lower() not in SKIP_DIRS and depth < 4:
                    walk(ipath, depth + 1)
            elif item.get("type") == "file":
                ext = Path(name).suffix.lower()
                # Skip known-bad extensions, minified files, huge files
                if ext in SKIP_EXTENSIONS:
                    continue
                if name.endswith(".min.js") or name.endswith(".min.css"):
                    continue
                if size > 200_000:
                    continue
                tier = get_file_tier(name, ipath)
                manifest.append((tier, size, name, ipath, item.get("download_url", "")))

    walk()

    # ── Pass 2: Download in priority order, enforce token budget ──────────────
    # Sort: tier ASC (1=best), then size ASC within tier (smaller = more signal/char)
    manifest.sort(key=lambda x: (x[0], x[1]))

    files = {}
    total_chars = 0
    skipped = []

    for tier, size, name, path, url in manifest:
        if total_chars >= TOTAL_CHAR_LIMIT:
            skipped.append(path)
            continue
        remaining_budget = TOTAL_CHAR_LIMIT - total_chars
        char_limit = min(CHAR_BUDGET[tier], remaining_budget)
        content = fetch_file_content(url, token, char_limit)
        if content is None:
            continue
        files[path] = content
        total_chars += len(content)

    stats = {
        "files_fetched":   len(files),
        "files_skipped":   len(skipped),
        "total_chars":     total_chars,
        "dirs_crawled":    dirs_crawled,
        "manifest_size":   len(manifest),
        "budget_hit":      total_chars >= TOTAL_CHAR_LIMIT,
    }
    return files, stats


def get_commit_history(owner: str, repo: str, token: Optional[str]) -> list:
    commits = github_api("/repos/" + owner + "/" + repo + "/commits?per_page=30", token)
    if not isinstance(commits, list):
        return []
    return [{
        "sha":     (c.get("sha") or "")[:7],
        "message": (c.get("commit", {}).get("message") or "")[:100],
        "author":  (c.get("commit", {}).get("author", {}).get("name") or "unknown"),
        "date":    (c.get("commit", {}).get("author", {}).get("date") or ""),
    } for c in commits]


def get_tier(score: int) -> dict:
    for lo, hi, label, desc in SCORE_RUBRIC:
        if lo <= score <= hi:
            return {"label": label, "desc": desc}
    return {"label": "Unknown", "desc": ""}


# ── Prompt builder ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are a senior software engineer and expert hackathon judge.
Return ONLY valid JSON — no markdown fences, no extra text."""

# Human-readable category names for the frontend
CATEGORY_META = {
    "prototype_quality":          {"label": "Prototype Quality",                        "icon": "🔧"},
    "code_quality":               {"label": "Code Quality & Architecture",               "icon": "🏗️"},
    "innovation_doc_topic":       {"label": "Innovation, Documentation & Topic Fit",     "icon": "💡"},
    "security":                   {"label": "Security",                                  "icon": "🔒"},
    "performance_maintainability":{"label": "Performance & Maintainability",             "icon": "⚡"},
}


def build_prompt(repo_info: dict, files_text: str, repo_url: str,
                 commit_history: list, mode: str) -> str:

    hackathon_name  = HACKATHON.get("name") or "Hackathon"
    hackathon_topic = HACKATHON.get("topic") or ""
    judging_notes   = HACKATHON.get("judging_notes") or ""

    rubric = "\n".join(
        "  " + str(lo) + "-" + str(hi) + ": " + lbl + " — " + desc
        for lo, hi, lbl, desc in SCORE_RUBRIC
    )
    commits_text = "\n".join(
        "  [" + (c["date"] or "")[:10] + "] " + (c["author"] or "") + ": " + (c["message"] or "")
        for c in commit_history[:20]
    ) or "  No commits found"

    topic_block = ""
    if hackathon_topic:
        topic_block = "\n## Hackathon Topic\n" + hackathon_topic + "\n"
        if judging_notes:
            topic_block += "\n## Judging Notes\n" + judging_notes + "\n"

    topic_instruction = (
        "Consider innovation of idea AND quality of documentation AND how well it fits the hackathon topic: " + hackathon_topic
        if hackathon_topic else
        "Consider innovation of idea AND quality of documentation (no specific topic defined)"
    )

    # ── Shared 5-category scoring block used by both modes ────────────────────
    scoring_tasks = """## Your Scoring Tasks — 5 Categories, all scored 0-20

### CATEGORY 1 — prototype_quality (0-20)
Assess: Is the prototype functional and stable? Does the core feature work end-to-end?
Are there critical bugs or crashes? Is the UX coherent? Is it a complete working demo?

### CATEGORY 2 — code_quality (0-20)
Assess: Code architecture, design patterns, readability, modularity, naming conventions,
separation of concerns, error handling, DRY principle.

### CATEGORY 3 — innovation_doc_topic (0-20)
Assess ALL THREE together as one combined score:
- Innovation: novelty of idea, creative technical approach, unique problem-solving
- Documentation: README quality, setup instructions, inline comments, clarity
- Topic Alignment: """ + topic_instruction + """

### CATEGORY 4 — security (0-20)
Assess security posture of the entire codebase:
- Hardcoded API keys, passwords, secrets in source code
- SQL injection risks or unsanitized query construction
- Unvalidated/unsanitized user inputs used in sensitive operations
- Exposed credentials in config files or environment handling
- Missing authentication or authorization checks
- Insecure HTTP usage where HTTPS is needed
Score 20 = no issues found. Deduct per issue severity.
List up to 5 specific issues found (file + line context if possible).

### CATEGORY 5 — performance_maintainability (0-20)
Assess BOTH performance AND maintainability as one combined score:
Performance issues:
- Nested loops O(n²) or worse on non-trivial data
- Blocking synchronous I/O calls that should be async
- Redundant DB/API queries inside loops
- Loading entire large datasets into memory unnecessarily
- Inefficient data structures for the use case
Maintainability issues:
- Deeply nested conditions (4+ levels)
- Functions over 100 lines with no decomposition
- Magic numbers/strings without constants
- Copy-pasted code blocks (DRY violations)
- Confusing or misleading variable/function names
Score 20 = clean, efficient, well-structured. Deduct per issue found.
List up to 5 specific issues found."""

    # ── Cheat detection block (full mode only) ─────────────────────────────────
    cheat_block = ""
    if mode == "full":
        cheat_block = """
## Cheat Detection Task

Analyze commit history and code carefully. Be a detective.

RED FLAGS (raise suspicion):
- Single giant initial commit containing all code (classic pre-built dump)
- Commit timestamps outside reasonable hackathon hours
- Code style is inconsistent — looks like multiple different projects merged
- README is too polished and comprehensive for a hackathon
- Core logic is thin wrappers around existing libraries — minimal original work
- Project structure matches a known boilerplate template exactly
- Repo is a fork of another project
- Git history was rewritten or force-pushed
- Comments or file names reference a different project name

GREEN FLAGS (genuine hackathon work):
- Multiple commits showing iterative development
- Commit messages reference debugging, fixing, trying things
- Code has TODOs, rough edges, commented-out experiments
- README has known issues or next steps section
- Evidence of learning or pivoting mid-hackathon

Authenticity scoring:
- 0-30: Almost certainly pre-built or plagiarized
- 31-55: Suspicious — significant pre-existing work
- 56-75: Mixed — some pre-built, some hackathon work
- 76-100: Genuine hackathon project"""

    # ── JSON schema ────────────────────────────────────────────────────────────
    originality_schema = """"originality": null""" if mode != "full" else """"originality": {
    "authenticity_score": <int 0-100>,
    "verdict": "genuine"|"suspicious"|"likely_prebuilt"|"plagiarized",
    "red_flags": ["<flag1>","<flag2>"],
    "green_flags": ["<flag1>","<flag2>"],
    "commit_pattern_analysis": "<2-3 sentences>",
    "explanation": "<3-4 sentences>"
  }"""

    disqualify = ('"disqualify_recommendation": true|false,\n  "disqualify_reason": "<reason or null>"'
                  if mode == "full" else
                  '"disqualify_recommendation": false,\n  "disqualify_reason": null')

    json_schema = """{
  "scores": {
    "prototype_quality": <int 0-20>,
    "code_quality": <int 0-20>,
    "innovation_doc_topic": <int 0-20>,
    "security": <int 0-20>,
    "performance_maintainability": <int 0-20>
  },
  "category_feedback": {
    "prototype_quality": "<2-3 sentences>",
    "code_quality": "<2-3 sentences>",
    "innovation_doc_topic": "<2-3 sentences covering innovation, docs, and topic fit>",
    "security": "<2-3 sentences>",
    "performance_maintainability": "<2-3 sentences>"
  },
  "security_issues": ["<specific issue with file/context>"],
  "performance_issues": ["<specific issue with file/context>"],
  "strengths": ["<s1>","<s2>","<s3>"],
  "weaknesses": ["<w1>","<w2>","<w3>"],
  """ + originality_schema + """,
  "overall_verdict": "<3-4 sentence overall judge verdict>",
  "judge_recommendation": "advance"|"borderline"|"reject",
  """ + disqualify + """,
  "tech_stack_detected": ["<tech1>","<tech2>"]
}"""

    return (
        "Judge this submission for the " + hackathon_name + ".\n\n"
        "## Repository\n"
        "- URL: " + repo_url + "\n"
        "- Name: " + (repo_info.get("name") or "Unknown") + "\n"
        "- Description: " + (repo_info.get("description") or "No description") + "\n"
        "- Language: " + (repo_info.get("language") or "Unknown") + "\n"
        "- Created: " + (repo_info.get("created_at") or "Unknown") + "\n"
        "- Last Push: " + (repo_info.get("pushed_at") or "Unknown") + "\n"
        "- Is Fork: " + str(repo_info.get("fork", False)) + "\n"
        + topic_block
        + "\n## Commit History\n" + commits_text + "\n"
        + "\n## Scoring Rubric (0-20)\n" + rubric + "\n"
        + "\n## Repository Files\n" + files_text + "\n\n"
        + scoring_tasks
        + cheat_block
        + "\n\nReturn EXACTLY this JSON:\n" + json_schema
    )


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/config")
def config():
    return jsonify({
        "has_anthropic_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "has_github_token":  bool(os.environ.get("GITHUB_TOKEN")),
        "hackathon_name":    HACKATHON.get("name") or "",
        "hackathon_edition": HACKATHON.get("edition") or "",
        "hackathon_topic":   HACKATHON.get("topic") or "",
        "category_meta":     CATEGORY_META,
    })


@app.route("/analyze", methods=["POST"])
def analyze():
    data         = request.get_json()
    repo_url     = (data.get("repo_url") or "").strip()
    mode         = (data.get("mode") or "standard").strip()
    _user_token  = (data.get("github_token") or "").strip()
    _user_key    = (data.get("api_key") or "").strip()
    github_token = os.environ.get("GITHUB_TOKEN") or _user_token or None
    api_key      = os.environ.get("ANTHROPIC_API_KEY") or _user_key or None

    if not repo_url:
        return jsonify({"error": "Repository URL is required"}), 400
    if not api_key:
        return jsonify({"error": "Anthropic API key is required"}), 400
    if mode not in ("standard", "full"):
        mode = "standard"

    owner, repo = parse_github_url(repo_url)
    if not owner or not repo:
        return jsonify({"error": "Invalid GitHub URL. Use: https://github.com/owner/repo"}), 400

    def generate():
        try:
            yield "data: " + json.dumps({"step": "fetch_meta", "msg": "Fetching repository metadata…"}) + "\n\n"
            repo_info = github_api("/repos/" + owner + "/" + repo, github_token)
            if not repo_info:
                yield "data: " + json.dumps({"error": "Repository not found or inaccessible."}) + "\n\n"
                return

            yield "data: " + json.dumps({"step": "fetch_commits", "msg": "Fetching commit history…"}) + "\n\n"
            commit_history = get_commit_history(owner, repo, github_token)

            repo_display = (repo_info.get("name") or repo)
            yield "data: " + json.dumps({"step": "fetch_files", "msg": "Scanning repo and selecting files from " + repo_display + "..."}) + "\n\n"
            files, file_stats = collect_repo_files(owner, repo, github_token)
            if not files:
                yield "data: " + json.dumps({"error": "No readable files found in repository."}) + "\n\n"
                return

            file_count   = file_stats["files_fetched"]
            commit_count = len(commit_history)
            mode_label   = "Standard (5 categories)" if mode == "standard" else "Full + Cheat Detection"
            budget_note  = " [budget cap, " + str(file_stats["files_skipped"]) + " skipped]" if file_stats["budget_hit"] else ""
            yield "data: " + json.dumps({
                "step": "analyzing",
                "msg": "Running " + mode_label + " on " + str(file_count) + " files" + budget_note + "...",
                "file_count":    file_count,
                "files_skipped": file_stats["files_skipped"],
                "budget_hit":    file_stats["budget_hit"],
            }) + "\n\n"

            files_text = "\n\n".join("### " + p + "\n```\n" + c + "\n```" for p, c in files.items())
            prompt = build_prompt(repo_info, files_text, repo_url, commit_history, mode)

            client    = anthropic.Anthropic(api_key=api_key)
            raw_chunks = []
            last_ping  = time.time()

            with client.messages.stream(
                model="claude-sonnet-4-20250514",
                max_tokens=4000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                for text_chunk in stream.text_stream:
                    raw_chunks.append(text_chunk)
                    now = time.time()
                    if now - last_ping >= 5:
                        yield "data: " + json.dumps({"step": "heartbeat", "msg": "Claude is thinking…"}) + "\n\n"
                        last_ping = now

            raw = "".join(raw_chunks).strip()
            if raw.startswith("```json"): raw = raw[7:]
            elif raw.startswith("```"):   raw = raw[3:]
            if raw.endswith("```"):       raw = raw[:-3]
            raw = raw.strip()

            result     = json.loads(raw)
            scores     = result.get("scores", {})
            score_vals = [v for v in scores.values() if v is not None]
            avg        = sum(score_vals) / len(score_vals) if score_vals else 0

            result["tier_info"]     = {k: get_tier(v) for k, v in scores.items() if v is not None}
            result["overall_score"] = round(avg, 1)
            result["overall_tier"]  = get_tier(int(round(avg)))
            result["mode"]          = mode
            result["category_meta"] = CATEGORY_META
            result["hackathon"]     = {
                "name":    HACKATHON.get("name") or "",
                "edition": HACKATHON.get("edition") or "",
                "topic":   HACKATHON.get("topic") or "",
            }
            result["repo_info"] = {
                "name":          (repo_info.get("name") or repo),
                "description":   (repo_info.get("description") or ""),
                "language":      (repo_info.get("language") or "Unknown"),
                "stars":         repo_info.get("stargazers_count", 0),
                "forks":         repo_info.get("forks_count", 0),
                "is_fork":       repo_info.get("fork", False),
                "created_at":    (repo_info.get("created_at") or ""),
                "pushed_at":     (repo_info.get("pushed_at") or ""),
                "url":           repo_url,
                "file_count":    file_count,
                "files_skipped": file_stats["files_skipped"],
                "manifest_size": file_stats["manifest_size"],
                "budget_hit":    file_stats["budget_hit"],
                "total_chars":   file_stats["total_chars"],
                "commit_count":  commit_count,
            }

            yield "data: " + json.dumps({"step": "done", "result": result}) + "\n\n"

        except anthropic.BadRequestError as e:
            msg = "Insufficient Anthropic credits." if "credit" in str(e).lower() else "Anthropic API error: " + str(e)
            yield "data: " + json.dumps({"error": msg}) + "\n\n"
        except anthropic.AuthenticationError:
            yield "data: " + json.dumps({"error": "Invalid Anthropic API key."}) + "\n\n"
        except json.JSONDecodeError:
            yield "data: " + json.dumps({"error": "Failed to parse Claude response. Please try again."}) + "\n\n"
        except Exception as e:
            yield "data: " + json.dumps({"error": "Unexpected error: " + str(e)}) + "\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)
