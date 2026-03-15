# GfG Campus Club Website Challenge Portal

Deployable Flask + SQLite website for the GeeksforGeeks Campus Club - RIT Website Building Challenge 2026.

## Stack

- Flask
- SQLite3
- HTML templates + CSS + JavaScript

## Run locally

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Then open `http://127.0.0.1:5000`.

## Deploy

This project is suitable for platforms that support Flask apps such as Render, Railway, PythonAnywhere, or a VPS.

The repository includes a `Procfile` for hosts that detect `gunicorn` automatically.

For a quick production start on Windows:

```powershell
pip install waitress
waitress-serve --listen=0.0.0.0:8080 app:app
```

For Linux-style deployment:

```bash
pip install -r requirements.txt
gunicorn app:app
```
