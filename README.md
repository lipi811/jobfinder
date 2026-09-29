# JobFinder India

## Run locally
    pip install -r requirements.txt
    export ADZUNA_APP_ID=... ADZUNA_APP_KEY=...   # free at developer.adzuna.com
    uvicorn main:app --reload
Open http://localhost:8000

## Deploy (Render / Railway / Fly.io)
Push this folder to GitHub, create a Web Service from the repo (Dockerfile is detected),
and add the environment variables from `.env.example`. Health check path: `/healthz`.

## Add more companies
Edit `COMPANIES` in `main.py` (Greenhouse and Lever slugs).
