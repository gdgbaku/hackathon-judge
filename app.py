"""
Hackathon Code Judge — Flask Web App
Powered by Claude (Anthropic)
"""

import os
import json
import time
import textwrap
from pathlib import Path
from typing import Optional, Union
from flask import Flask, render_template, request, jsonify, Response, stream_with_context
import requests as req
import anthropic

app = Flask(__name__)

# ── Scoring rubric ──────────────────────────────────────────────────────────────
SCORE_RUBRIC = [
    (0,  4,  "Non-Functional",  "Prototype is non-functional or severely unstable."),
    (5,  8,  "Basic",           "Functional but lacks key features or has significant bugs."),
    (9,  12, "Functional",      "Clear architecture with working core features."),
    (13, 16, "Robust",          "Reliable prototype that seamlessly integrates with the ecosystem."),
    (17, 20, "Exceptional",     "Technically sound, fully working, and feels native to the target app."),
]

CATEGORIES = {
    "prototype_quality": {
        "label": "Prototype Quality",
        "icon": "🔧",
        "description": "Functionality, stability, core feature completeness, and integration quality",
    },
    "code_quality": {
        "label": "Code Quality & Architecture",
        "icon": "🏗️",
        "description": "Code structure, design patterns, readability, modularity, error handling",
    },
    "innovation": {
        "label": "Innovation & Creativity",
        "icon": "💡",
        "description": "Novelty of the idea, creative problem solving, unique technical approaches",
    },
    "documentation": {
        "label": "Documentation & README",
        "icon": "📚",
        "description": "README clarity, setup instructions, inline comments, API docs",
    },
}

SKIP_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".bmp",
    ".mp4", ".mp3", ".wav", ".zip", ".tar", ".gz", ".lock", ".pdf",
    ".ttf", ".woff", ".woff2", ".eot", ".bin", ".exe", ".dll",
}
SKIP_DIRS = {
    "node_modules", ".git", "vendor", "dist", "build", "__pycache__",
    ".next", ".nuxt", "coverage", ".venv", "venv", "env",
}
PRIORITY_FILES = {
    "readme", "readme.md", "readme.txt", "main.py", "app.py", "index.js",
    "index.ts", "app.js", "app.ts", "server.py", "server.js", "main.go",
    "package.json", "requirements.txt", "pyproject.toml", "dockerfile",
}


def parse_github_url(url: str):
    url = url.strip().rstrip("/").replace(".git", "")
    # handle tree/branch paths
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
        headers["Authorization"] = f"token {token}"
    resp = req.get(f"https://api.github.com{path}", headers=headers, timeout=15)
    if resp.status_code in (404, 403):
        return {}
    resp.raise_for_status()
    return resp.json()


def fetch_file_content(url: str, token: Optional[str] = None) -> Optional[str]:
    headers = {}
    if token:
        headers["Authorization"] = f"token {token}"
    try:
        resp = req.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.text[:6000]
    except Exception:
        return None


def collect_repo_files(owner: str, repo: str, token: Optional[str], max_files: int = 15) -> dict:
    collected = {}
    priority_collected = {}

    def walk(path: str = ""):
        if len(collected) + len(priority_collected) >= max_files * 2:
            return
        endpoint = f"/repos/{owner}/{repo}/contents/{path}".rstrip("/")
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


def get_tier(score: int) -> dict:
    for lo, hi, label, desc in SCORE_RUBRIC:
        if lo <= score <= hi:
            return {"label": label, "desc": desc, "lo": lo, "hi": hi}
    return {"label": "Unknown", "desc": "", "lo": 0, "hi": 0}


SYSTEM_PROMPT = """You are an expert hackathon judge and senior software engineer.
Evaluate GitHub repositories with precision, fairness, and insight.
Return ONLY valid JSON — no markdown fences, no extra text."""


def build_prompt(repo_info: dict, files_text: str, repo_url: str) -> str:
    rubric = "\n".join(f"  {lo}-{hi}: {label} — {desc}" for lo, hi, label, desc in SCORE_RUBRIC)
    return f"""Judge this hackathon submission. Analyze the repository and return a JSON assessment.

## Repository
- URL: {repo_url}
- Name: {repo_info.get('name', 'Unknown')}
- Description: {repo_info.get('description', 'No description')}
- Language: {repo_info.get('language', 'Unknown')}
- Topics: {', '.join(repo_info.get('topics', [])) or 'None'}

## Scoring Rubric (0-20)
{rubric}

## Repository Files
{files_text}

Return EXACTLY this JSON structure:
{{
  "scores": {{
    "prototype_quality": <int 0-20>,
    "code_quality": <int 0-20>,
    "innovation": <int 0-20>,
    "documentation": <int 0-20>
  }},
  "strengths": ["<strength 1>", "<strength 2>", "<strength 3>"],
  "weaknesses": ["<weakness 1>", "<weakness 2>", "<weakness 3>"],
  "category_feedback": {{
    "prototype_quality": "<2-3 sentence feedback>",
    "code_quality": "<2-3 sentence feedback>",
    "innovation": "<2-3 sentence feedback>",
    "documentation": "<2-3 sentence feedback>"
  }},
  "overall_verdict": "<3-4 sentence overall judge verdict>",
  "judge_recommendation": "advance" | "borderline" | "reject",
  "tech_stack_detected": ["<tech1>", "<tech2>"]
}}"""


# ── Routes ──────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/config")
def config():
    """Tell the frontend which keys are configured server-side."""
    return jsonify({
        "has_anthropic_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "has_github_token": bool(os.environ.get("GITHUB_TOKEN")),
    })


@app.route("/analyze", methods=["POST"])
def analyze():
    data = request.get_json()
    repo_url = data.get("repo_url", "").strip()
    # Always prefer server-side env vars, user input is fallback only
    _user_github_token = (data.get("github_token") or "").strip()
    _user_api_key = (data.get("api_key") or "").strip()
    github_token = os.environ.get("GITHUB_TOKEN") or _user_github_token or None
    api_key = os.environ.get("ANTHROPIC_API_KEY") or _user_api_key or None
    max_files = int(data.get("max_files", 15))

    if not repo_url:
        return jsonify({"error": "Repository URL is required"}), 400
    if not api_key:
        return jsonify({"error": "Anthropic API key is required"}), 400

    owner, repo = parse_github_url(repo_url)
    if not owner or not repo:
        return jsonify({"error": "Invalid GitHub URL. Use: https://github.com/owner/repo"}), 400

    def generate():
        try:
            yield f"data: {json.dumps({'step': 'fetch_meta', 'msg': 'Fetching repository metadata…'})}\n\n"
            repo_info = github_api(f"/repos/{owner}/{repo}", github_token)
            if not repo_info:
                yield f"data: {json.dumps({'error': 'Repository not found or private. Add a GitHub token.'})}\n\n"
                return

            repo_display = repo_info.get('name', repo)
            yield f"data: {json.dumps({'step': 'fetch_files', 'msg': 'Collecting files from ' + repo_display + '\u2026'})}\n\n"
            files = collect_repo_files(owner, repo, github_token, max_files)
            if not files:
                yield f"data: {json.dumps({'error': 'No readable files found in repository.'})}\n\n"
                return

            yield f"data: {json.dumps({'step': 'analyzing', 'msg': f'Sending {len(files)} files to Claude for assessment…', 'file_count': len(files)})}\n\n"

            files_text = "\n\n".join(f"### {path}\n```\n{content}\n```" for path, content in files.items())
            prompt = build_prompt(repo_info, files_text, repo_url)

            client = anthropic.Anthropic(api_key=api_key)
            message = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=2000,
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
            avg = sum(scores.values()) / len(scores) if scores else 0

            # Enrich with tier info
            result["tier_info"] = {k: get_tier(v) for k, v in scores.items()}
            result["overall_score"] = round(avg, 1)
            result["overall_tier"] = get_tier(int(round(avg)))
            result["repo_info"] = {
                "name": repo_info.get("name", repo),
                "description": repo_info.get("description", ""),
                "language": repo_info.get("language", "Unknown"),
                "stars": repo_info.get("stargazers_count", 0),
                "url": repo_url,
                "file_count": len(files),
            }

            yield f"data: {json.dumps({'step': 'done', 'result': result})}\n\n"

        except anthropic.BadRequestError as e:
            if "credit" in str(e).lower():
                yield f"data: {json.dumps({'error': 'Insufficient Anthropic credits. Add credits at console.anthropic.com/settings/billing'})}\n\n"
            else:
                yield f"data: {json.dumps({'error': f'Anthropic API error: {str(e)}'})}\n\n"
        except anthropic.AuthenticationError:
            yield f"data: {json.dumps({'error': 'Invalid Anthropic API key. Check your key and try again.'})}\n\n"
        except json.JSONDecodeError:
            yield f"data: {json.dumps({'error': 'Failed to parse Claude response. Please try again.'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': f'Unexpected error: {str(e)}'})}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)
