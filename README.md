# Paper Fetcher Web App

A Streamlit web app that accepts one or many journal-paper **DOIs or URLs**, attempts to locate a downloadable PDF, and bundles successful downloads into a ZIP.

## What it does

For each input, the app tries:

1. Direct PDF download
2. DOI metadata lookup via Crossref
3. Open-access lookup via Unpaywall (when a contact email is supplied)
4. Publisher/DOI landing-page metadata such as `citation_pdf_url`
5. Clearly labelled PDF/full-text links on the landing page

It **does not bypass paywalls, authentication, CAPTCHAs, DRM, or publisher access controls**.

## Run locally

```bash
python -m venv .venv
```

Windows:

```bash
.venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

macOS/Linux:

```bash
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

Then open the local Streamlit URL shown in the terminal.

## Optional: set your contact email

Unpaywall expects a contact email. You can enter it in the app, or set:

Windows PowerShell:

```powershell
$env:UNPAYWALL_EMAIL="you@example.com"
streamlit run app.py
```

macOS/Linux:

```bash
export UNPAYWALL_EMAIL="you@example.com"
streamlit run app.py
```

## Docker

```bash
docker build -t paper-fetcher .
docker run --rm -p 8501:8501 -e UNPAYWALL_EMAIL="you@example.com" paper-fetcher
```

Then browse to `http://localhost:8501`.

## Notes

- DOI input is generally more reliable than publisher landing-page URLs.
- Some publisher sites require JavaScript, institutional login, session cookies, or publisher-specific APIs. Those papers will be reported as unavailable rather than bypassing those controls.
- The app blocks localhost/private-network destinations to reduce SSRF risk when deployed publicly.
- Default maximum file size is 80 MB per PDF. Change `MAX_PDF_MB` in `app.py` if needed.
