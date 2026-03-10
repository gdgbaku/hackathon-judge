# ⚡ Hackathon Code Judge — Web App

AI-powered web app to review GitHub repos for hackathons. Powered by **Claude (Anthropic)**.

## 🚀 Deploy Free in 2 Minutes

### Option 1 — Railway (Recommended, easiest)
1. Push this folder to a GitHub repo
2. Go to → https://railway.app
3. Click **"New Project" → "Deploy from GitHub"**
4. Select your repo
5. Add environment variable: `ANTHROPIC_API_KEY = sk-ant-xxx`
6. Done — Railway gives you a live URL instantly ✅

### Option 2 — Render
1. Push to GitHub
2. Go to → https://render.com
3. Click **"New Web Service"** → connect your repo
4. Set: Build `pip install -r requirements.txt` · Start `gunicorn app:app --bind 0.0.0.0:$PORT`
5. Add env var: `ANTHROPIC_API_KEY`
6. Deploy ✅

### Option 3 — Run Locally
```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY="sk-ant-xxx"
python app.py
# open http://localhost:5000
```

## 📊 What It Does
- Enter any public GitHub repo URL
- Claude fetches and analyzes up to 15 files
- Scores across 4 categories (0–20 each)
- Shows strengths, weaknesses, tech stack
- Judge recommendation: ADVANCE / BORDERLINE / REJECT
