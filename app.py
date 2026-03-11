"""
Hackathon Code Judge — Flask Web App v5
- Hackathon topic/name loaded from hackathon.json (set once by organizer)
- No success criteria input — topic is static and pre-defined
- Three modes: standard | advanced | full
"""

import os
import json
from pathlib import Path
from typing import Optional, Union
from flask import Flask, render_template, request, jsonify, Response, stream_with_context
import requests as req
import anthropic

app = Flask(__name__)

# ── Load hackathon config (set once, never changed by users) ─────────────────
HACKATHON_CONFIG_FILE = Path(__file__).parent / "hackathon.json"

def load_hackathon_config() -> dict:
    """Load from hackathon.json, fall back to env vars, then empty defaults."""
    defaults = {
        "name": os.environ.get("HACKATHON_NAME", "Hackathon"),
        "edition": os.environ.get("HACKATHON_EDITION", ""),
        "topic": os.environ.get("HACKATHON_TOPIC", ""),
        "judging_notes": os.environ.get("HACKATHON_JUDGING_NOTES", ""),
    }
    if HACKATHON_CONFIG_FILE.exists():
        try:
            data = json.loads(HACKATHON_CONFIG_FILE.read_text())
            # env vars override file if set
            for key in defaults:
                if os.environ.get("HACKATHON_" + key.upper()):
                    data[key] = os.environ.get("HACKATHON_" + key.upper())
            return {**defaults, **data}
        except Exception:
            pass
    return defaults

HACKATHON = load_hackathon_config()

# ── Constants ─────────────────────────────────────────────────────────────────
SKIP_EXTENSIONS = {
    ".png",".jpg",".jpeg",".gif",".svg",".ico",".webp",".bmp",
    ".mp4",".mp3",".wav",".zip",".tar",".gz",".lock",".pdf",
    ".ttf",".woff",".woff2",".eot",".bin",".exe",".dll",
}
SKIP_DIRS = {
    "node_modules",".git","vendor","dist","build","__pycache__",
    ".next",".nuxt","coverage",".venv","venv","env",
}
PRIORITY_FILES = {
    "readme","readme.md","readme.txt","main.py","app.py","index.js",
    "index.ts","app.js","app.ts","server.py","server.js","main.go",
    "package.json","requirements.txt","pyproject.toml","dockerfile",
}
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


def fetch_file_content(url: str, token: Optional[str] = None) -> Optional[str]:
    headers = {}
    if token:
        headers["Authorization"] = "token " + token
    try:
        resp = req.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.text[:8000]
    except Exception:
        return None


def collect_repo_files(owner: str, repo: str, token: Optional[str], max_files: int = 20) -> dict:
    collected, priority = {}, {}

    def walk(path: str = ""):
        if len(collected) + len(priority) >= max_files * 2:
            return
        try:
            items = github_api("/repos/" + owner + "/" + repo + "/contents/" + path.lstrip("/"), token)
        except Exception:
            return
        if isinstance(items, dict):
            items = [items]
        for item in items:
            if len(collected) + len(priority) >= max_files * 2:
                break
            name  = item.get("name", "")
            ipath = item.get("path", "")
            if item.get("type") == "dir":
                if name.lower() not in SKIP_DIRS:
                    walk(ipath)
            elif item.get("type") == "file":
                if Path(name).suffix.lower() in SKIP_EXTENSIONS:
                    continue
                if item.get("size", 0) > 150_000:
                    continue
                content = fetch_file_content(item.get("download_url", ""), token)
                if content is None:
                    continue
                if name.lower() in PRIORITY_FILES or ipath.lower() in PRIORITY_FILES:
                    priority[ipath] = content
                else:
                    collected[ipath] = content

    walk()
    result = dict(priority)
    for k, v in list(collected.items())[:max_files - len(result)]:
        result[k] = v
    return result


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


def build_prompt(repo_info: dict, files_text: str, repo_url: str,
                 commit_history: list, mode: str) -> str:

    hackathon_name    = HACKATHON.get("name") or "Hackathon"
    hackathon_topic   = HACKATHON.get("topic") or ""
    judging_notes     = HACKATHON.get("judging_notes") or ""

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

    # ── Mode-specific task instructions ──────────────────────────────────────
    if mode == "standard":
        task_block = """## Your Tasks

### TASK 1 — STANDARD SCORING (score 0-20 each)
- prototype_quality: Is it functional, stable, and complete?
- code_quality: Architecture, patterns, readability, modularity, error handling
- innovation: Novelty of idea, creative technical approaches
- documentation: README clarity, setup instructions, inline comments

### TASK 2 — TOPIC ALIGNMENT
""" + ("- topic_alignment: How well does this project match the hackathon topic? (score 0-20)" if hackathon_topic else "- topic_alignment: No topic defined. Set to null.") + """

Return this JSON:
{
  "scores": {
    "prototype_quality": <int 0-20>,
    "code_quality": <int 0-20>,
    "innovation": <int 0-20>,
    "documentation": <int 0-20>,
    "topic_alignment": <int 0-20 or null>
  },
  "strengths": ["<s1>","<s2>","<s3>"],
  "weaknesses": ["<w1>","<w2>","<w3>"],
  "category_feedback": {
    "prototype_quality": "<2-3 sentences>",
    "code_quality": "<2-3 sentences>",
    "innovation": "<2-3 sentences>",
    "documentation": "<2-3 sentences>",
    "topic_alignment": "<2-3 sentences or null>"
  },
  "code_analysis": null,
  "originality": null,
  "overall_verdict": "<3-4 sentence verdict>",
  "judge_recommendation": "advance"|"borderline"|"reject",
  "disqualify_recommendation": false,
  "disqualify_reason": null,
  "tech_stack_detected": ["<tech>"]
}"""

    elif mode == "advanced":
        task_block = """## Your Tasks

### TASK 1 — STANDARD SCORING (score 0-20 each)
- prototype_quality, code_quality, innovation, documentation

### TASK 2 — TOPIC ALIGNMENT
""" + ("- topic_alignment: How well does this match the hackathon topic? (0-20)" if hackathon_topic else "- topic_alignment: null") + """

### TASK 3 — DEEP CODE ANALYSIS (score each 0-10)
Examine every file:
- SECURITY: hardcoded secrets/API keys, SQL injection, unvalidated inputs, exposed credentials
- PERFORMANCE: O(n²)+ nested loops, blocking calls, redundant DB queries in loops, memory leaks
- MAINTAINABILITY: deeply nested conditions, functions >100 lines, magic numbers, copy-paste blocks

Return this JSON:
{
  "scores": {
    "prototype_quality": <int 0-20>,
    "code_quality": <int 0-20>,
    "innovation": <int 0-20>,
    "documentation": <int 0-20>,
    "topic_alignment": <int 0-20 or null>
  },
  "strengths": ["<s1>","<s2>","<s3>"],
  "weaknesses": ["<w1>","<w2>","<w3>"],
  "category_feedback": {
    "prototype_quality": "<2-3 sentences>",
    "code_quality": "<2-3 sentences>",
    "innovation": "<2-3 sentences>",
    "documentation": "<2-3 sentences>",
    "topic_alignment": "<2-3 sentences or null>"
  },
  "code_analysis": {
    "security_issues": ["<issue>"],
    "performance_issues": ["<issue>"],
    "complexity_issues": ["<issue>"],
    "security_score": <int 0-10>,
    "performance_score": <int 0-10>,
    "maintainability_score": <int 0-10>
  },
  "originality": null,
  "overall_verdict": "<3-4 sentence verdict>",
  "judge_recommendation": "advance"|"borderline"|"reject",
  "disqualify_recommendation": false,
  "disqualify_reason": null,
  "tech_stack_detected": ["<tech>"]
}"""

    else:  # full
        task_block = """## Your Tasks

### TASK 1 — STANDARD SCORING (score 0-20 each)
- prototype_quality, code_quality, innovation, documentation

### TASK 2 — TOPIC ALIGNMENT
""" + ("- topic_alignment: How well does this match the hackathon topic? (0-20)" if hackathon_topic else "- topic_alignment: null") + """

### TASK 3 — DEEP CODE ANALYSIS (score each 0-10)
- SECURITY: hardcoded secrets, SQL injection, unvalidated inputs, exposed credentials
- PERFORMANCE: O(n²)+ loops, blocking calls, redundant queries, memory leaks
- MAINTAINABILITY: deeply nested conditions, functions >100 lines, copy-paste blocks

### TASK 4 — CHEAT DETECTION (authenticity_score 0-100)
RED FLAGS: single giant initial commit, timestamps outside hackathon hours, inconsistent
code style suggesting merged projects, overly polished README, core logic is thin wrapper,
repo is a fork, git history rewritten, comments reference different project names.

GREEN FLAGS: multiple incremental commits, commit messages show debugging/experimenting,
code has TODOs and rough edges, README has known issues section, evidence of pivoting.

Scoring: 0-30 = plagiarized, 31-55 = suspicious, 56-75 = mixed, 76-100 = genuine.

Return this JSON:
{
  "scores": {
    "prototype_quality": <int 0-20>,
    "code_quality": <int 0-20>,
    "innovation": <int 0-20>,
    "documentation": <int 0-20>,
    "topic_alignment": <int 0-20 or null>
  },
  "strengths": ["<s1>","<s2>","<s3>"],
  "weaknesses": ["<w1>","<w2>","<w3>"],
  "category_feedback": {
    "prototype_quality": "<2-3 sentences>",
    "code_quality": "<2-3 sentences>",
    "innovation": "<2-3 sentences>",
    "documentation": "<2-3 sentences>",
    "topic_alignment": "<2-3 sentences or null>"
  },
  "code_analysis": {
    "security_issues": ["<issue>"],
    "performance_issues": ["<issue>"],
    "complexity_issues": ["<issue>"],
    "security_score": <int 0-10>,
    "performance_score": <int 0-10>,
    "maintainability_score": <int 0-10>
  },
  "originality": {
    "authenticity_score": <int 0-100>,
    "verdict": "genuine"|"suspicious"|"likely_prebuilt"|"plagiarized",
    "red_flags": ["<flag>"],
    "green_flags": ["<flag>"],
    "commit_pattern_analysis": "<2-3 sentences>",
    "explanation": "<3-4 sentences>"
  },
  "overall_verdict": "<3-4 sentence verdict>",
  "judge_recommendation": "advance"|"borderline"|"reject",
  "disqualify_recommendation": true|false,
  "disqualify_reason": "<reason or null>",
  "tech_stack_detected": ["<tech>"]
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
        + topic_block +
        "\n## Commit History\n" + commits_text + "\n"
        "\n## Scoring Rubric (0-20)\n" + rubric + "\n"
        "\n## Repository Files\n" + files_text + "\n\n"
        + task_block
    )


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/config")
def config():
    """Return server config to the frontend (no secrets)."""
    return jsonify({
        "has_anthropic_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "has_github_token":  bool(os.environ.get("GITHUB_TOKEN")),
        "hackathon_name":    HACKATHON.get("name") or "",
        "hackathon_edition": HACKATHON.get("edition") or "",
        "hackathon_topic":   HACKATHON.get("topic") or "",
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
    max_files    = int(data.get("max_files", 20))

    if not repo_url:
        return jsonify({"error": "Repository URL is required"}), 400
    if not api_key:
        return jsonify({"error": "Anthropic API key is required"}), 400
    if mode not in ("standard", "advanced", "full"):
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
            yield "data: " + json.dumps({"step": "fetch_files", "msg": "Collecting files from " + repo_display + "…"}) + "\n\n"
            files = collect_repo_files(owner, repo, github_token, max_files)
            if not files:
                yield "data: " + json.dumps({"error": "No readable files found in repository."}) + "\n\n"
                return

            file_count   = len(files)
            commit_count = len(commit_history)
            mode_labels  = {"standard": "Standard", "advanced": "Advanced + Deep Code", "full": "Full + Cheat Detection"}
            yield "data: " + json.dumps({
                "step": "analyzing",
                "msg": "Running " + mode_labels.get(mode, mode) + " assessment on " + str(file_count) + " files…",
                "file_count": file_count,
            }) + "\n\n"

            files_text = "\n\n".join("### " + p + "\n```\n" + c + "\n```" for p, c in files.items())
            prompt = build_prompt(repo_info, files_text, repo_url, commit_history, mode)

            client = anthropic.Anthropic(api_key=api_key)
            message = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=4000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )

            raw = message.content[0].text.strip()
            if raw.startswith("```json"): raw = raw[7:]
            elif raw.startswith("```"):   raw = raw[3:]
            if raw.endswith("```"):       raw = raw[:-3]
            raw = raw.strip()

            result       = json.loads(raw)
            scores       = result.get("scores", {})
            score_vals   = [v for v in scores.values() if v is not None]
            avg          = sum(score_vals) / len(score_vals) if score_vals else 0

            result["tier_info"]     = {k: get_tier(v) for k, v in scores.items() if v is not None}
            result["overall_score"] = round(avg, 1)
            result["overall_tier"]  = get_tier(int(round(avg)))
            result["mode"]          = mode
            result["hackathon"]     = {
                "name":    HACKATHON.get("name") or "",
                "edition": HACKATHON.get("edition") or "",
                "topic":   HACKATHON.get("topic") or "",
            }
            result["repo_info"] = {
                "name":         (repo_info.get("name") or repo),
                "description":  (repo_info.get("description") or ""),
                "language":     (repo_info.get("language") or "Unknown"),
                "stars":        repo_info.get("stargazers_count", 0),
                "forks":        repo_info.get("forks_count", 0),
                "is_fork":      repo_info.get("fork", False),
                "created_at":   (repo_info.get("created_at") or ""),
                "pushed_at":    (repo_info.get("pushed_at") or ""),
                "url":          repo_url,
                "file_count":   file_count,
                "commit_count": commit_count,
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
