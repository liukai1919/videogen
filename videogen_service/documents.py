# Local text extraction for research documents: the 调研 input grows from
# "a YouTube URL" to "any file on this machine". Everything runs in-process
# (pypdf, python-docx, plain decoding) — no cloud parser is ever involved.

from io import BytesIO
from pathlib import Path


class DocumentError(RuntimeError):
    pass


_TEXT_SUFFIXES = frozenset({".txt", ".md"})


def extract_text(filename: str, data: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in _TEXT_SUFFIXES:
        return data.decode("utf-8", errors="replace")
    if suffix == ".pdf":
        return _pdf_text(data)
    if suffix == ".docx":
        return _docx_text(data)
    raise DocumentError(
        f"不支持的文档格式: {suffix or '(无后缀)'};支持 .pdf .docx .txt .md"
    )


def _pdf_text(data: bytes) -> str:
    # Deferred import: pypdf pulls in the crypto stack, which only documents
    # need — the render path should not pay for it at startup.
    from pypdf import PdfReader

    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            raise DocumentError("PDF 加密了,请先解密再上传")
        pages = [page.extract_text() or "" for page in reader.pages]
    except DocumentError:
        raise
    except Exception as error:
        raise DocumentError(f"PDF 解析失败: {error}") from error
    text = "\n".join(pages).strip()
    if not text:
        raise DocumentError("PDF 里提取不到文字,可能是扫描件")
    return text


def _docx_text(data: bytes) -> str:
    from docx import Document

    try:
        document = Document(BytesIO(data))
        paragraphs = [paragraph.text for paragraph in document.paragraphs]
    except Exception as error:
        raise DocumentError(f"Word 文档解析失败: {error}") from error
    text = "\n".join(paragraphs).strip()
    if not text:
        raise DocumentError("Word 文档里没有文字")
    return text
