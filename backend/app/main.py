import os
from fastapi import FastAPI, UploadFile, File, HTTPException, Depends
from fastapi.responses import JSONResponse, FileResponse
from sqlalchemy.orm import Session
from . import models, schemas
from .database import engine, Base, get_db
from .utils import extract_text_from_pdf, extract_text_from_image, parse_cdr_text, extract_apm_from_text
import shutil
import uuid
import json



app = FastAPI(title="CDR Extraction API")

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)


@app.post("/upload")
async def upload(file: UploadFile = File(...), db: Session = Depends(get_db)):
    # save file
    file_ext = os.path.splitext(file.filename)[1].lower()
    uid = str(uuid.uuid4())
    saved_name = f"{uid}{file_ext}"
    saved_path = os.path.join(UPLOAD_DIR, saved_name)

    with open(saved_path, "wb") as out:
        content = await file.read()
        out.write(content)

    size = os.path.getsize(saved_path)

    db_file = models.File(filename=file.filename, path=saved_path, size=size)
    db.add(db_file)
    db.commit()
    db.refresh(db_file)

    # extract text
    text = ""
    if file_ext in [".pdf"]:
        text = extract_text_from_pdf(saved_path)
    elif file_ext in [".png", ".jpg", ".jpeg", ".tiff", ".bmp"]:
        text = extract_text_from_image(saved_path)
    else:
        # try treating as PDF
        try:
            text = extract_text_from_pdf(saved_path)
        except Exception:
            text = ""

    parsed = parse_cdr_text(text)

    cdr = models.Cdr(file_id=db_file.id, parsed_text=text, parsed_json=json.dumps(parsed), status="draft")
    db.add(cdr)
    db.commit()
    db.refresh(cdr)

    return JSONResponse({"file_id": db_file.id, "cdr_id": cdr.id, "parsed": parsed})


@app.post("/extract-apm")
async def extract_apm(file: UploadFile = File(...)):
    #apm extractor 
    # Save uploaded file to temp path
    file_ext = os.path.splitext(file.filename)[1].lower()
    uid = str(uuid.uuid4())
    saved_name = f"{uid}{file_ext}"
    saved_path = os.path.join(UPLOAD_DIR, saved_name)

    with open(saved_path, "wb") as out:
        content = await file.read()
        out.write(content)

    try:
        text = ""
        if file_ext == ".pdf":
            text = extract_text_from_pdf(saved_path)
        else:
            text = extract_text_from_image(saved_path)

        # run extractor
        apm = extract_apm_from_text(text)

        return JSONResponse({"filename": file.filename, "apm": apm})
    finally:
        # cleanup temp file
        try:
            os.remove(saved_path)
        except Exception:
            pass


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

    # store final JSON and mark confirmed
    c.parsed_json = json.dumps(payload.parsed_json)
    c.status = payload.status or "confirmed"
    db.add(c)
    db.commit()
    db.refresh(c)

    return {"cdr_id": c.id, "status": c.status, "parsed": json.loads(c.parsed_json)}

Base.metadata.create_all(bind=engine)
