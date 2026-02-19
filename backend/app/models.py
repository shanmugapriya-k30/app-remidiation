from sqlalchemy import Column, Integer, String, DateTime, Text, ForeignKey
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from .database import Base


class File(Base):
    __tablename__ = "files"

    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String(255), nullable=False)
    path = Column(String(1024), nullable=False)
    size = Column(Integer, nullable=False)
    uploaded_at = Column(DateTime(timezone=True), server_default=func.now())

    cdr = relationship("Cdr", back_populates="file", uselist=False)


class Cdr(Base):
    __tablename__ = "cdrs"

    id = Column(Integer, primary_key=True, index=True)
    file_id = Column(Integer, ForeignKey("files.id"), nullable=False)
    parsed_text = Column(Text, nullable=True)
    parsed_json = Column(Text, nullable=True)
    status = Column(String(50), default="draft")
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    file = relationship("File", back_populates="cdr")
