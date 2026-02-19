CDR Extraction Backend

Quick start for the FastAPI backend that accepts PDF/image uploads, extracts text (pdfplumber / pytesseract), heuristically parses CDR fields, and stores metadata + parsed JSON in a database.

How to run (Windows PowerShell):

1. Create a virtual environment and install dependencies:

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

2. (Optional) Set DATABASE_URL env var. Defaults to SQLite `sqlite:///./cdr.db`.

3. Run the server:

```powershell
uvicorn app.main:app --reload --port 8000
```

Endpoints:
- POST /upload : multipart file -> returns file_id, cdr_id, parsed fields
- GET /files/{file_id} : metadata + parsed cdr
- GET /files/{file_id}/download : download stored file
- POST /cdr/{cdr_id}/confirm : send final parsed JSON (edited by UI) to mark confirmed

Notes / assumptions:
- For quick demo the default DB is SQLite. For production set DATABASE_URL to your MySQL URL (SQLAlchemy format).
- The parser is heuristic based on common CDR headings; it may need tuning for other documents.
