import os
from fastapi import FastAPI, UploadFile, File, HTTPException, Depends
from fastapi.responses import JSONResponse, FileResponse
from sqlalchemy.orm import Session
from . import models, schemas
from .database import engine, Base, get_db
from .utils import extract_text_from_pdf, extract_text_from_image, parse_cdr_text, extract_apm_from_text, extract_technical_table

import shutil
import uuid
import json



app = FastAPI(title="CDR Extraction API")

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)


def _merge_dicts(orig: dict, new: dict) -> dict:
    """Deep-merge two dictionaries.

    Values from `new` overwrite values from `orig`. If both values are dicts,
    merge them recursively. Other types are replaced.
    Returns a new dict and does not modify the inputs.
    """
    if not isinstance(orig, dict):
        orig = {}
    if not isinstance(new, dict):
        return orig

    merged = dict(orig)  # shallow copy
    for k, v in new.items():
        if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
            merged[k] = _merge_dicts(merged[k], v)
        else:
            merged[k] = v
    return merged


# NOTE: the previous /upload endpoint has been removed. Use /extract-apm to upload
# and persist APM extraction results directly to the database.


@app.post("/extract-apm")
async def extract_apm(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Upload a file, extract APM details and persist the file + CDR (parsed_json).

    This replaces the old /upload endpoint: the extracted APM JSON is stored in
    the Cdr.parsed_json field and the full extracted text is stored in
    Cdr.parsed_text.
    """
    file_ext = os.path.splitext(file.filename)[1].lower()
    uid = str(uuid.uuid4())
    saved_name = f"{uid}{file_ext}"
    saved_path = os.path.join(UPLOAD_DIR, saved_name)

    # save uploaded file to uploads folder
    with open(saved_path, "wb") as out:
        content = await file.read()
        out.write(content)

    size = os.path.getsize(saved_path)

    # create File record
    db_file = models.File(filename=file.filename, path=saved_path, size=size)
    db.add(db_file)
    db.commit()
    db.refresh(db_file)

    try:
        # extract text for parsing
        text = ""
        if file_ext == ".pdf":
            text = extract_text_from_pdf(saved_path)
        else:
            text = extract_text_from_image(saved_path)

        # run APM extractor
        apm = extract_apm_from_text(text)

        # persist CDR with APM JSON
        cdr = models.Cdr(file_id=db_file.id, parsed_text=text, parsed_json=json.dumps(apm), status="draft")
        db.add(cdr)
        db.commit()
        db.refresh(cdr)

        return JSONResponse({"filename": file.filename, "file_id": db_file.id, "cdr_id": cdr.id, "apm": apm})
    except Exception as e:
        # If anything fails, cleanup DB file record and saved file
        try:
            db.delete(db_file)
            db.commit()
        except Exception:
            pass
        raise


@app.get("/files/{file_id}")
def get_file(file_id: int, db: Session = Depends(get_db)):
    f = db.query(models.File).filter(models.File.id == file_id).first()
    if not f:
        raise HTTPException(status_code=404, detail="File not found")
    cdr = f.cdr
    parsed = {}
    if cdr and cdr.parsed_json:
        try:
            parsed = json.loads(cdr.parsed_json)
        except Exception:
            parsed = {}

    return {
        "file": schemas.FileOut.from_orm(f),
        "cdr": {"id": cdr.id, "status": cdr.status, "parsed": parsed} if cdr else None,
    }


@app.get("/files/{file_id}/download")
def download_file(file_id: int, db: Session = Depends(get_db)):
    f = db.query(models.File).filter(models.File.id == file_id).first()
    if not f:
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(f.path, filename=f.filename)


@app.post("/cdr/{cdr_id}/confirm")
def confirm_cdr(cdr_id: int, payload: schemas.CdrConfirm, db: Session = Depends(get_db)):
    c = db.query(models.Cdr).filter(models.Cdr.id == cdr_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="CDR not found")

    # capture existing parsed JSON (before update) so caller can display/edit it
    existing_parsed = {}
    if c.parsed_json:
        try:
            existing_parsed = json.loads(c.parsed_json)
        except Exception:
            existing_parsed = {}
    # If the incoming payload parsed_json is empty ({}), preserve existing parsed JSON.
    # Otherwise merge existing and incoming so:
    #  - keys present in incoming overwrite existing
    #  - new keys are appended
    #  - dict values are merged recursively
    incoming = payload.parsed_json if isinstance(payload.parsed_json, dict) else {}
    if not incoming:
        # empty incoming means no edits -> keep existing
        new_parsed_json = existing_parsed
    else:
        new_parsed_json = _merge_dicts(existing_parsed, incoming)

    # store final JSON and mark confirmed
    c.parsed_json = json.dumps(new_parsed_json)
    c.status = payload.status or "confirmed"
    db.add(c)
    db.commit()
    db.refresh(c)

    saved_parsed = {}
    try:
        saved_parsed = json.loads(c.parsed_json) if c.parsed_json else {}
    except Exception:
        saved_parsed = {}

    return {
        "cdr_id": c.id,
        "status": c.status,
        "existing_parsed": existing_parsed,
        "parsed": saved_parsed,
    }


@app.get("/cdr/{cdr_id}/parsed")
def get_cdr_parsed(cdr_id: int, db: Session = Depends(get_db)):
    """Return the stored parsed JSON for a CDR so the client can prepopulate an editor."""
    c = db.query(models.Cdr).filter(models.Cdr.id == cdr_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="CDR not found")

    parsed = {}
    if c.parsed_json:
        try:
            parsed = json.loads(c.parsed_json)
        except Exception:
            parsed = {}

    return {"cdr_id": c.id, "parsed": parsed}


@app.post("/extract-techspec")
async def extract_techspec(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Upload a file, extract only the Technical Specifications and persist a CDR with
    parsed_json containing only the technical_spec key. Returns the extracted technical_spec.
    """
    file_ext = os.path.splitext(file.filename)[1].lower()
    uid = str(uuid.uuid4())
    saved_name = f"{uid}{file_ext}"
    saved_path = os.path.join(UPLOAD_DIR, saved_name)

    # save uploaded file to uploads folder
    with open(saved_path, "wb") as out:
        content = await file.read()
        out.write(content)

    size = os.path.getsize(saved_path)

    # create File record
    db_file = models.File(filename=file.filename, path=saved_path, size=size)
    db.add(db_file)
    db.commit()
    db.refresh(db_file)

    try:
        # extract text for parsing
        text = ""
        if file_ext == ".pdf":
            text = extract_text_from_pdf(saved_path)
        else:
            text = extract_text_from_image(saved_path)

        techspec = extract_technical_table(text)
        # techspec is expected to be a dict of rows; use it directly as the technical_spec
        tech = techspec if isinstance(techspec, dict) else {}

        # persist CDR with only technical_spec
        cdr = models.Cdr(file_id=db_file.id, parsed_text=text, parsed_json=json.dumps({"technical_spec": tech}), status="draft")
        db.add(cdr)
        db.commit()
        db.refresh(cdr)

        return JSONResponse({"filename": file.filename, "file_id": db_file.id, "cdr_id": cdr.id, "technical_spec": tech})

    except Exception:
        # If anything fails, cleanup DB file record and saved file
        try:
            db.delete(db_file)
            db.commit()
        except Exception:
            pass
        raise

Base.metadata.create_all(bind=engine)
