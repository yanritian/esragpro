"""
Document processing service for various file formats
"""
import os
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

import aiofiles
from docx import Document
from fastapi import UploadFile
import fitz  # PyMuPDF

from app.core.config import settings


class DocumentProcessor:
    """Process various document formats"""

    SUPPORTED_FORMATS = {".pdf", ".docx", ".doc", ".txt", ".md"}

    async def save_upload(self, file: UploadFile) -> str:
        """Save uploaded file and return file path"""
        os.makedirs(settings.UPLOAD_DIR, exist_ok=True)

        file_id = str(uuid.uuid4())
        filename = file.filename or "upload"
        file_ext = os.path.splitext(filename)[1].lower()
        file_path = os.path.join(settings.UPLOAD_DIR, f"{file_id}{file_ext}")

        async with aiofiles.open(file_path, "wb") as out_file:
            content = await file.read()
            await out_file.write(content)

        return file_path

    def extract_text(self, file_path: str) -> str:
        """Extract text from various document formats"""
        ext = os.path.splitext(file_path)[1].lower()

        if ext == ".pdf":
            return self._extract_pdf(file_path)
        elif ext in {".docx", ".doc"}:
            return self._extract_docx(file_path)
        elif ext in {".txt", ".md"}:
            return self._extract_text_file(file_path)
        else:
            raise ValueError(f"Unsupported file format: {ext}")

    def _extract_pdf(self, file_path: str) -> str:
        """Extract text from PDF using PyMuPDF"""
        try:
            doc = fitz.open(file_path)
            text_parts = []
            for page in doc:
                text = page.get_text()
                if text:
                    text_parts.append(text)
            doc.close()
            return "\n\n".join(text_parts)
        except Exception as e:
            raise RuntimeError(f"Error extracting PDF: {e}")

    def _extract_docx(self, file_path: str) -> str:
        """Extract text from Word document"""
        try:
            doc = Document(file_path)
            paragraphs = [para.text for para in doc.paragraphs if para.text.strip()]
            return "\n\n".join(paragraphs)
        except Exception as e:
            raise RuntimeError(f"Error extracting Word document: {e}")

    def _extract_text_file(self, file_path: str) -> str:
        """Extract text from plain text file"""
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return f.read()
        except UnicodeDecodeError:
            with open(file_path, "r", encoding="gbk") as f:
                return f.read()

    def chunk_text(
        self,
        text: str,
        chunk_size: int = 500,
        overlap: int = 50,
    ) -> List[Dict[str, Any]]:
        """
        Split text into overlapping chunks

        Args:
            text: Full text to chunk
            chunk_size: Maximum characters per chunk
            overlap: Overlap characters between chunks

        Returns:
            List of chunk dictionaries
        """
        chunks = []
        start = 0
        chunk_index = 0

        while start < len(text):
            end = start + chunk_size
            chunk_text = text[start:end]

            if chunk_text.strip():
                chunks.append({
                    "chunk_id": str(uuid.uuid4()),
                    "chunk_index": chunk_index,
                    "content": chunk_text.strip(),
                    "start_char": start,
                    "end_char": min(end, len(text)),
                })
                chunk_index += 1

            start = end - overlap

        return chunks

    async def process_upload(
        self,
        file: UploadFile,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Process an uploaded file

        Returns:
            Document dictionary with chunks
        """
        # Save file
        file_path = await self.save_upload(file)

        # Extract text
        text = self.extract_text(file_path)

        # Create chunks
        chunks = self.chunk_text(text)

        # Create document record
        doc_id = str(uuid.uuid4())
        filename = file.filename or "upload"
        document = {
            "doc_id": doc_id,
            "title": os.path.splitext(filename)[0],
            "file_path": file_path,
            "doc_type": os.path.splitext(filename)[1].lower(),
            "source": file.filename,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
            "metadata": metadata or {},
            "chunks": chunks,
            "full_text": text,
        }

        return document


# Global document processor instance
document_processor = DocumentProcessor()