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
MAX_MEMORY_BYTES = 384 * 1024 * 1024
MEMORY_SAMPLE_INTERVAL_MS = 25
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
    memory_control = 'address_space'
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        try:
            resource.setrlimit(resource.RLIMIT_AS, (MAX_MEMORY_BYTES, MAX_MEMORY_BYTES))
        except (ValueError, OSError):
            if sys.platform != 'darwin' or sys.argv[1:] != ['--rss-guarded']:
                raise
            memory_control = 'sampled_rss'
    except (ImportError, ValueError, OSError):
        result = disposition('unsupported', 'parser_resource_limits_unavailable')
    else:
        # The guarded parent samples this PID before sending stdin. No parser
        # import or PDF decoding starts until that parent closes the pipe.
        result = _extract(sys.stdin.buffer.read(MAX_DOCUMENT_BYTES + 1))
        result.update(_memory_metadata(memory_control))
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=True).encode())


def _memory_metadata(control):
    return {'memory_control': control, 'memory_limit_bytes': MAX_MEMORY_BYTES,
            'memory_sample_interval_ms': MEMORY_SAMPLE_INTERVAL_MS if control == 'sampled_rss' else None,
            'hard_limit': control == 'address_space'}


class _MemoryGuardError(RuntimeError):
    pass


def _sample_rss(target):
    import psutil
    try:
        if not target.is_running():
            raise psutil.NoSuchProcess(target.pid)
        rss = target.memory_info().rss
        if type(rss) is not int or rss < 0:
            raise ValueError('invalid RSS')
    except psutil.NoSuchProcess as exc:
        raise _MemoryGuardError('parser_memory_monitor_exited') from exc
    except Exception as exc:
        raise _MemoryGuardError('parser_memory_monitor_failed') from exc
    if rss > MAX_MEMORY_BYTES:
        raise _MemoryGuardError('parser_memory_limit')


async def _watch_rss(process, target):
    # Best-effort resident memory samples; scheduling and allocations can
    # exceed this interval/threshold. This is not an OS memory allocation cap.
    while process.returncode is None:
        try:
            _sample_rss(target)
        except _MemoryGuardError as exc:
            if str(exc) == 'parser_memory_monitor_exited':
                await asyncio.sleep(0)  # Let a genuine exit notification settle.
                if process.returncode is not None:
                    return
            raise
        await asyncio.sleep(MEMORY_SAMPLE_INTERVAL_MS / 1000)


async def extract_document(data):
    """No model, OCR, fetch, embedded action, or file reference is executed."""
    guarded = sys.platform == 'darwin'
    if guarded:
        try:
            import psutil
        except ImportError:
            return disposition('unsupported', 'parser_memory_monitor_unavailable')
    process = await asyncio.create_subprocess_exec(
        sys.executable, '-I', str(Path(__file__).resolve()),
        *(['--rss-guarded'] if guarded else []),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)
    communication = watcher = target = None
    guard_metadata = {}
    try:
        async with asyncio.timeout(MAX_PARSE_SECONDS):
            if guarded:
                try:
                    target = psutil.Process(process.pid)
                except Exception as exc:
                    raise _MemoryGuardError('parser_memory_monitor_unavailable') from exc
                guard_metadata = _memory_metadata('sampled_rss')
                _sample_rss(target)  # Must succeed before any PDF bytes go in.
                watcher = asyncio.create_task(_watch_rss(process, target))
            communication = asyncio.create_task(process.communicate(data))
            if watcher is not None:
                await asyncio.wait((communication, watcher), return_when=asyncio.FIRST_COMPLETED)
                if watcher.done():
                    watcher.result()  # A breach takes precedence over output.
            output, _ = await asyncio.shield(communication)
        if process.returncode:
            return disposition('failed', 'parser_process_failed_or_resource_limit') | guard_metadata
        if len(output) > MAX_RESULT_BYTES:
            return disposition('failed', 'parser_output_exceeds_limit') | guard_metadata
        result = json.loads(output)
        if result.get('version') != VERSION or result.get('status') not in {'complete', 'partial', 'unsupported', 'failed'}:
            raise ValueError('invalid parser result')
        return result
    except asyncio.TimeoutError:
        return disposition('failed', 'parser_time_limit') | guard_metadata
    except _MemoryGuardError as exc:
        reason = 'parser_memory_monitor_failed' if str(exc) == 'parser_memory_monitor_exited' else str(exc)
        return disposition('unsupported', reason) | guard_metadata
    except (ValueError, TypeError):
        return disposition('failed', 'invalid_parser_result') | guard_metadata
    finally:
        try:
            if process.returncode is None:
                try:
                    if target is None:
                        process.kill()
                    else:
                        try:
                            target.kill()  # Checks this bound PID's identity.
                        except psutil.NoSuchProcess:
                            pass
                        except psutil.Error:
                            process.kill()  # Owned-child Popen also polls first.
                except ProcessLookupError:
                    pass
        finally:
            if watcher is not None:
                watcher.cancel()
            if communication is None:
                communication = asyncio.create_task(process.communicate())
            # Drain pipes while reaping, including cancellation during output.
            await asyncio.gather(communication, *([watcher] if watcher is not None else []), return_exceptions=True)
            await process.wait()


if __name__ == '__main__':
    _child()
