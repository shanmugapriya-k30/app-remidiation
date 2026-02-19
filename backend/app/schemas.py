from pydantic import BaseModel
from typing import Optional, Dict, Any
from datetime import datetime


class FileCreate(BaseModel):
    filename: str
    path: str
    size: int


class FileOut(BaseModel):
    id: int
    filename: str
    path: str
    size: int
    uploaded_at: datetime

    class Config:
        # Pydantic v2 renamed `orm_mode` -> `from_attributes`.
        # Use the v2 key so we avoid the runtime warning in environments using Pydantic v2.
        from_attributes = True


class CdrOut(BaseModel):
    id: int
    file_id: int
    parsed_text: Optional[str]
    parsed_json: Optional[Dict[str, Any]]
    status: str
    updated_at: Optional[datetime]

    class Config:
        from_attributes = True


class CdrConfirm(BaseModel):
    parsed_json: Dict[str, Any]
    status: Optional[str] = "confirmed"
