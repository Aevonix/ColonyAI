"""Bounded PDF extraction from owned bytes, never paths supplied by a caller.

This file also runs as a standalone isolated child. Keep imports at module load
in the standard library so resource limits precede loading the PDF parser.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
from pathlib import Path
import sys

MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
MAX_PAGES = 64
MAX_PAGE_STREAM_BYTES = 2 * 1024 * 1024
MAX_PAGE_CHARS = 32000
MAX_TEXT_CHARS = 200000
MAX_RESULT_BYTES = 6 * MAX_TEXT_CHARS + 65536
MAX_PARSE_SECONDS = 15
VERSION = 'source-pdf-text-v1'


def decode_document(block):
    """Only explicit inline PDF bytes are supported by this ingestion contract."""
    item = block.get('input_document')
    if (set(block) != {'type', 'input_document'} or not isinstance(item, dict)
            or set(item) != {'mime_type', 'data'} or item.get('mime_type') != 'application/pdf'
            or not isinstance(item.get('data'), str)):
        raise ValueError('unsupported_document_input')
    if len(item['data']) > ((MAX_DOCUMENT_BYTES + 2) // 3) * 4:
        raise ValueError('document_bytes_exceed_limit')
    try:
        data = base64.b64decode(item['data'], validate=True)
    except (ValueError, TypeError):
        raise ValueError('invalid_document_encoding') from None
    if len(data) > MAX_DOCUMENT_BYTES:
        raise ValueError('document_bytes_exceed_limit')
    # This is a bounded admission signature check, not a PDF parser. Malformed
    # PDF-looking originals remain retained with an honest extraction failure.
    if not data.startswith(b'%PDF-'):
        raise ValueError('unsupported_document_format')
    return data


def disposition(status, reason=None, *, parser_version=None, page_count=None, pages=None):
    return {'version': VERSION, 'parser': 'pypdf', 'parser_version': parser_version,
            'status': status, 'reason': reason, 'page_count': page_count,
            'pages': pages or [], 'ocr_performed': False,
            'epistemic_state': 'derived_unverified'}


def _extract(data):
    """Called only inside the resource-limited child, including in tests."""
    try:
        import pypdf
    except ImportError:
        return disposition('failed', 'parser_unavailable')
    version = pypdf.__version__
    if not data.startswith(b'%PDF-') or len(data) > MAX_DOCUMENT_BYTES:
        return disposition('failed', 'invalid_retained_document', parser_version=version)
    try:
        reader = pypdf.PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted:
            return disposition('unsupported', 'encrypted_pdf', parser_version=version)
        count = len(reader.pages)
        if not 0 < count <= MAX_PAGES:
            return disposition('unsupported', 'page_count_exceeds_limit' if count else 'empty_pdf',
                               parser_version=version, page_count=count)
        pages, total = [], 0
        for number, page in enumerate(reader.pages, 1):
            contents = page.get_contents()
            # Decompression itself is inside the memory/CPU fence.
            if contents is not None and len(contents.get_data()) > MAX_PAGE_STREAM_BYTES:
                return disposition('unsupported', 'page_stream_exceeds_limit', parser_version=version,
                                   page_count=count)
            text = page.extract_text() or ''
            total += len(text)
            if len(text) > MAX_PAGE_CHARS or total > MAX_TEXT_CHARS:
                return disposition('unsupported', 'extracted_text_exceeds_limit', parser_version=version,
                                   page_count=count)
            pages.append({'page': number, 'text': text,
                          'status': 'text' if text.strip() else 'no_extractable_text'})
        text_pages = sum(page['status'] == 'text' for page in pages)
        return disposition('complete' if text_pages == count else 'partial' if text_pages else 'unsupported',
                           None if text_pages == count else 'no_extractable_text_on_some_pages' if text_pages
                           else 'no_extractable_text', parser_version=version, page_count=count, pages=pages)
    except MemoryError:
        return disposition('unsupported', 'parser_memory_limit', parser_version=version)
    except Exception:
        # Parser errors can contain document text. Persist a code, not that text.
        return disposition('failed', 'invalid_or_unreadable_pdf', parser_version=version)


def _child():
    logging.disable(logging.CRITICAL)
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (384 * 1024 * 1024, 384 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ImportError, ValueError, OSError):
        result = disposition('unsupported', 'parser_resource_limits_unavailable')
    else:
        result = _extract(sys.stdin.buffer.read(MAX_DOCUMENT_BYTES + 1))
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=True).encode())


async def extract_document(data):
    """No model, OCR, fetch, embedded action, or file reference is executed."""
    process = await asyncio.create_subprocess_exec(
        sys.executable, '-I', str(Path(__file__).resolve()),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)
    try:
        output, _ = await asyncio.wait_for(process.communicate(data), MAX_PARSE_SECONDS)
        if process.returncode:
            return disposition('failed', 'parser_process_failed_or_resource_limit')
        if len(output) > MAX_RESULT_BYTES:
            return disposition('failed', 'parser_output_exceeds_limit')
        result = json.loads(output)
        if result.get('version') != VERSION or result.get('status') not in {'complete', 'partial', 'unsupported', 'failed'}:
            raise ValueError('invalid parser result')
        return result
    except asyncio.TimeoutError:
        return disposition('failed', 'parser_time_limit')
    except (ValueError, TypeError):
        return disposition('failed', 'invalid_parser_result')
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()


if __name__ == '__main__':
    _child()
