"""
Hackathon Code Judge — Flask Web App v4
Three evaluation modes:
  standard   — 4 core categories only
  advanced   — + deep code analysis (security, performance, maintainability)
  full       — + cheat / originality detection on top of advanced
Optional: success criteria, hackathon topic
"""

import os
import json
from pathlib import Path
from typing import Optional, Union
from flask import Flask, render_template, request, jsonify, Response, stream_with_context
import requests as req
import anthropic

app = Flask(__name__)

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
    collected = {}
    priority_collected = {}

    def walk(path: str = ""):
        if len(collected) + len(priority_collected) >= max_files * 2:
            return
        endpoint = "/repos/" + owner + "/" + repo + "/contents/" + path
        endpoint = endpoint.rstrip("/")
        try:
            items = github_api(endpoint, token)
        except Exception:
            return
        if isinstance(items, dict):
            items = [items]
        for item in items:
            if len(collected) + len(priority_collected) >= max_files * 2:
                break
            name = item.get("name", "")
            item_path = item.get("path", "")
            if item.get("type") == "dir":
                if name.lower() not in SKIP_DIRS:
                    walk(item_path)
            elif item.get("type") == "file":
                ext = Path(name).suffix.lower()
                if ext in SKIP_EXTENSIONS:
                    continue
                if item.get("size", 0) > 150_000:
                    continue
                content = fetch_file_content(item.get("download_url", ""), token)
                if content is None:
                    continue
                is_priority = name.lower() in PRIORITY_FILES or item_path.lower() in PRIORITY_FILES
                if is_priority:
                    priority_collected[item_path] = content
                else:
                    collected[item_path] = content

    walk()
    result = dict(priority_collected)
    remaining = max_files - len(result)
    for k, v in list(collected.items())[:remaining]:
        result[k] = v
    return result


def get_commit_history(owner: str, repo: str, token: Optional[str]) -> list:
    commits = github_api("/repos/" + owner + "/" + repo + "/commits?per_page=30", token)
    if not isinstance(commits, list):
        return []
    result = []
    for c in commits:
        result.append({
            "sha": c.get("sha", "")[:7],
            "message": (c.get("commit", {}).get("message") or "")[:100],
            "author": (c.get("commit", {}).get("author", {}).get("name") or ""),
            "date": (c.get("commit", {}).get("author", {}).get("date") or ""),
        })
    return result


def get_tier(score: int) -> dict:
    for lo, hi, label, desc in SCORE_RUBRIC:
        if lo <= score <= hi:
            return {"label": label, "desc": desc, "lo": lo, "hi": hi}
    return {"label": "Unknown", "desc": "", "lo": 0, "hi": 0}


SYSTEM_PROMPT = """You are a senior software engineer and expert hackathon judge.
Return ONLY valid JSON — no markdown fences, no extra text."""


def build_prompt(repo_info: dict, files_text: str, repo_url: str,
                 commit_history: list, hackathon_topic: str,
                 success_criteria: str, mode: str) -> str:

    rubric = "\n".join(
        "  " + str(lo) + "-" + str(hi) + ": " + label + " — " + desc
        for lo, hi, label, desc in SCORE_RUBRIC
    )

    commits_text = "\n".join(
        "  [" + (c["date"] or "")[:10] + "] " + (c["author"] or "unknown") + ": " + (c["message"] or "")
        for c in commit_history[:20]
    ) or "  No commits found"

    topic_section = ("\n## Hackathon Topic & Theme\n" + hackathon_topic + "\n") if hackathon_topic.strip() else ""
    criteria_section = ("\n## Success Criteria\n" + success_criteria + "\n") if success_criteria.strip() else ""

    # ── Score keys change based on whether criteria/topic are provided ──
    topic_score_note = "Score 10 (neutral) if no topic was provided." if not hackathon_topic.strip() else "Score based on how well the project aligns with the provided topic."
    criteria_score_note = "Score 10 (neutral) if no criteria were provided." if not success_criteria.strip() else "Score based on how many criteria are met."

    # ── Mode-specific task instructions ──
    if mode == "standard":
        task_block = """## Your Task
Score on 4 standard hackathon categories (0-20 each):
- prototype_quality: Is it functional, stable, complete?
- code_quality: Architecture, patterns, readability, modularity, error handling
- innovation: Novelty of idea, creative technical approaches
- documentation: README clarity, setup instructions, inline comments
- topic_alignment: """ + topic_score_note + """
- criteria_match: """ + criteria_score_note

        json_template = """{
  "scores": {
    "prototype_quality": <int 0-20>,
    "code_quality": <int 0-20>,
    "innovation": <int 0-20>,
    "documentation": <int 0-20>,
    "topic_alignment": <int 0-20>,
    "criteria_match": <int 0-20>
  },
  "strengths": ["<s1>", "<s2>", "<s3>"],
  "weaknesses": ["<w1>", "<w2>", "<w3>"],
  "category_feedback": {
    "prototype_quality": "<2-3 sentence feedback>",
    "code_quality": "<2-3 sentence feedback>",
    "innovation": "<2-3 sentence feedback>",
    "documentation": "<2-3 sentence feedback>",
    "topic_alignment": "<2-3 sentence feedback>",
    "criteria_match": "<2-3 sentence feedback>"
  },
  "criteria_evaluation": [{"criterion": "<text>", "met": true, "notes": "<explanation>"}],
  "overall_verdict": "<3-4 sentence overall judge verdict>",
  "judge_recommendation": "advance" | "borderline" | "reject",
  "tech_stack_detected": ["<tech1>", "<tech2>"]
}"""

    elif mode == "advanced":
        task_block = """## Your Task

### 1. STANDARD SCORING (0-20 each)
- prototype_quality, code_quality, innovation, documentation
- topic_alignment: """ + topic_score_note + """
- criteria_match: """ + criteria_score_note + """

### 2. DEEP CODE ANALYSIS (scores 0-10 each)
Analyze every file carefully:
- SECURITY (0-10): hardcoded secrets, SQL injection, unvalidated inputs, exposed credentials, insecure HTTP, missing auth
- PERFORMANCE (0-10): O(n²)+ nested loops, blocking calls, redundant DB queries, memory leaks, inefficient data structures
- MAINTAINABILITY (0-10): deeply nested conditions, 100+ line functions, magic numbers, copy-pasted blocks, poor naming"""

        json_template = """{
  "scores": {
    "prototype_quality": <int 0-20>,
    "code_quality": <int 0-20>,
    "innovation": <int 0-20>,
    "documentation": <int 0-20>,
    "topic_alignment": <int 0-20>,
    "criteria_match": <int 0-20>
  },
  "strengths": ["<s1>", "<s2>", "<s3>"],
  "weaknesses": ["<w1>", "<w2>", "<w3>"],
  "category_feedback": {
    "prototype_quality": "<2-3 sentence feedback>",
    "code_quality": "<2-3 sentence feedback>",
    "innovation": "<2-3 sentence feedback>",
    "documentation": "<2-3 sentence feedback>",
    "topic_alignment": "<2-3 sentence feedback>",
    "criteria_match": "<2-3 sentence feedback>"
  },
  "code_analysis": {
    "security_issues": ["<issue1>", "<issue2>"],
    "performance_issues": ["<issue1>", "<issue2>"],
    "complexity_issues": ["<issue1>", "<issue2>"],
    "security_score": <int 0-10>,
    "performance_score": <int 0-10>,
    "maintainability_score": <int 0-10>
  },
  "criteria_evaluation": [{"criterion": "<text>", "met": true, "notes": "<explanation>"}],
  "overall_verdict": "<3-4 sentence overall judge verdict>",
  "judge_recommendation": "advance" | "borderline" | "reject",
  "tech_stack_detected": ["<tech1>", "<tech2>"]
}"""

    else:  # full — advanced + cheat detection
        task_block = """## Your Task

### 1. STANDARD SCORING (0-20 each)
- prototype_quality, code_quality, innovation, documentation
- topic_alignment: """ + topic_score_note + """
- criteria_match: """ + criteria_score_note + """

### 2. DEEP CODE ANALYSIS (scores 0-10 each)
- SECURITY: hardcoded secrets, SQL injection, unvalidated inputs, exposed credentials, insecure HTTP, missing auth
- PERFORMANCE: O(n²)+ nested loops, blocking calls, redundant DB queries, memory leaks
- MAINTAINABILITY: deeply nested conditions, huge functions, magic numbers, copy-paste code

### 3. ORIGINALITY & CHEAT DETECTION — be a detective:
RED FLAGS (raise suspicion):
- Single giant initial commit with all code (pre-built dump)
- Commits outside reasonable hackathon hours
- Inconsistent code style — looks like multiple projects merged together
- Overly polished README for a hackathon
- Repo is a fork of another project
- File names / comments reference a different project name
- Core logic is thin wrappers — minimal original work
- Project matches a well-known boilerplate template exactly

GREEN FLAGS (genuine work):
- Multiple commits showing iterative development
- Commit messages mention debugging, fixing, pivoting
- Rough edges, TODOs, commented-out experiments
- Evidence of learning mid-hackathon

Authenticity score 0-100:
  0-30: Almost certainly pre-built or plagiarized
  31-55: Suspicious — significant pre-existing work likely
  56-75: Mixed — some pre-built, some hackathon work
  76-100: Genuine hackathon project"""

        json_template = """{
  "scores": {
    "prototype_quality": <int 0-20>,
    "code_quality": <int 0-20>,
    "innovation": <int 0-20>,
    "documentation": <int 0-20>,
    "topic_alignment": <int 0-20>,
    "criteria_match": <int 0-20>
  },
  "strengths": ["<s1>", "<s2>", "<s3>"],
  "weaknesses": ["<w1>", "<w2>", "<w3>"],
  "category_feedback": {
    "prototype_quality": "<2-3 sentence feedback>",
    "code_quality": "<2-3 sentence feedback>",
    "innovation": "<2-3 sentence feedback>",
    "documentation": "<2-3 sentence feedback>",
    "topic_alignment": "<2-3 sentence feedback>",
    "criteria_match": "<2-3 sentence feedback>"
  },
  "code_analysis": {
    "security_issues": ["<issue1>", "<issue2>"],
    "performance_issues": ["<issue1>", "<issue2>"],
    "complexity_issues": ["<issue1>", "<issue2>"],
    "security_score": <int 0-10>,
    "performance_score": <int 0-10>,
    "maintainability_score": <int 0-10>
  },
  "originality": {
    "authenticity_score": <int 0-100>,
    "verdict": "genuine" | "suspicious" | "likely_prebuilt" | "plagiarized",
    "red_flags": ["<flag1>", "<flag2>"],
    "green_flags": ["<flag1>", "<flag2>"],
    "commit_pattern_analysis": "<2-3 sentences about what commit history reveals>",
    "explanation": "<3-4 sentences on your overall originality assessment>"
  },
  "criteria_evaluation": [{"criterion": "<text>", "met": true, "notes": "<explanation>"}],
  "overall_verdict": "<3-4 sentence overall judge verdict>",
  "judge_recommendation": "advance" | "borderline" | "reject",
  "disqualify_recommendation": true | false,
  "disqualify_reason": "<reason or null>",
  "tech_stack_detected": ["<tech1>", "<tech2>"]
}"""

    return """Evaluate this hackathon submission.

## Repository
- URL: """ + repo_url + """
- Name: """ + (repo_info.get("name") or "Unknown") + """
- Description: """ + (repo_info.get("description") or "No description") + """
- Language: """ + (repo_info.get("language") or "Unknown") + """
- Created: """ + (repo_info.get("created_at") or "Unknown") + """
- Last Push: """ + (repo_info.get("pushed_at") or "Unknown") + """
- Is Fork: """ + str(repo_info.get("fork", False)) + """
""" + topic_section + criteria_section + """
## Commit History
""" + commits_text + """

## Scoring Rubric (0-20)
""" + rubric + """

## Repository Files
""" + files_text + """

""" + task_block + """

Return EXACTLY this JSON:
""" + json_template


# ── Routes ───────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/config")
def config():
    return jsonify({
        "has_anthropic_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "has_github_token": bool(os.environ.get("GITHUB_TOKEN")),
    })


@app.route("/analyze", methods=["POST"])
def analyze():
    data = request.get_json()
    repo_url         = (data.get("repo_url") or "").strip()
    hackathon_topic  = (data.get("hackathon_topic") or "").strip()
    success_criteria = (data.get("success_criteria") or "").strip()
    mode             = (data.get("mode") or "standard").strip()   # standard | advanced | full
    _user_github_token = (data.get("github_token") or "").strip()
    _user_api_key      = (data.get("api_key") or "").strip()
    github_token = os.environ.get("GITHUB_TOKEN") or _user_github_token or None
    api_key      = os.environ.get("ANTHROPIC_API_KEY") or _user_api_key or None
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

            yield "data: " + json.dumps({"step": "fetch_commits", "msg": "Analysing commit history…"}) + "\n\n"
            commit_history = get_commit_history(owner, repo, github_token)

            repo_display = (repo_info.get("name") or repo)
            yield "data: " + json.dumps({"step": "fetch_files", "msg": "Collecting files from " + repo_display + "…"}) + "\n\n"
            files = collect_repo_files(owner, repo, github_token, max_files)
            if not files:
                yield "data: " + json.dumps({"error": "No readable files found in repository."}) + "\n\n"
                return

            file_count   = len(files)
            commit_count = len(commit_history)
            mode_label   = {"standard": "Standard", "advanced": "Advanced", "full": "Full (+ Cheat Detection)"}[mode]
            yield "data: " + json.dumps({
                "step": "analyzing",
                "msg": "Running " + mode_label + " assessment on " + str(file_count) + " files…",
                "file_count": file_count,
            }) + "\n\n"

            files_text = "\n\n".join("### " + p + "\n```\n" + c + "\n```" for p, c in files.items())
            prompt = build_prompt(repo_info, files_text, repo_url, commit_history,
                                  hackathon_topic, success_criteria, mode)

            client = anthropic.Anthropic(api_key=api_key)
            message = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=4000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )

            raw = message.content[0].text.strip()
            if raw.startswith("```json"):
                raw = raw[7:]
            elif raw.startswith("```"):
                raw = raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()

            result = json.loads(raw)
            scores = result.get("scores", {})
            score_vals = list(scores.values())
            avg = sum(score_vals) / len(score_vals) if score_vals else 0

            result["tier_info"]     = {k: get_tier(v) for k, v in scores.items()}
            result["overall_score"] = round(avg, 1)
            result["overall_tier"]  = get_tier(int(round(avg)))
            result["mode"]          = mode
            result["repo_info"] = {
                "name":        (repo_info.get("name") or repo),
                "description": (repo_info.get("description") or ""),
                "language":    (repo_info.get("language") or "Unknown"),
                "stars":       repo_info.get("stargazers_count", 0),
                "forks":       repo_info.get("forks_count", 0),
                "is_fork":     repo_info.get("fork", False),
                "created_at":  (repo_info.get("created_at") or ""),
                "pushed_at":   (repo_info.get("pushed_at") or ""),
                "url":         repo_url,
                "file_count":  file_count,
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
