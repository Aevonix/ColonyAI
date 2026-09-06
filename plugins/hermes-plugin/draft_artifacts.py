"""The shared source/report contract for accepted local drafts."""
import hashlib
import json
import os
from pathlib import Path
import tempfile


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write(path, value):
    raw = value if isinstance(value, str) else json.dumps(value, sort_keys=True, indent=2)+'\n'
    descriptor, temporary = tempfile.mkstemp(prefix='.'+path.name+'-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(raw.encode('utf-8'))
            stream.flush()
            os.fsync(stream.fileno())
        # Publish complete bytes exclusively; existing artifacts remain intact.
        os.link(temporary, path)
        sync_directory(path.parent)
    finally:
        os.unlink(temporary)
        sync_directory(path.parent)


def restore_report(directory, receipt):
    """Materialize a receipt's validated report, preserving older saved pairs."""
    result = receipt['result']
    path = directory/'report.md'
    if Path(result['report_path']) != path:
        raise ValueError('saved_draft_receipt_mismatch')
    report = receipt.get('report_text')
    if report is not None and (not isinstance(report, str)
            or hashlib.sha256(report.encode('utf-8')).hexdigest() != result['report_sha256']):
        raise ValueError('saved_draft_receipt_mismatch')
    if not path.exists():
        if report is None:
            raise ValueError('saved_draft_receipt_mismatch')
        write(path, report)
    if digest(path) != result['report_sha256']:
        raise ValueError('saved_draft_receipt_mismatch')
    return result


def make_directory(path):
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        sync_directory(directory.parent)


def retain_draft(work, interpretation, directory, *, model, receipt_context):
    """Validate actual reads and save one report plus its recoverable receipt."""
    if (not work.bound or work.error
            or work.read != set(range(len(work.assignment['context']['sources'])))):
        raise ValueError('native_local_draft_incomplete')
    if (not isinstance(interpretation, dict) or set(interpretation) != {'draft', 'sources'}
            or not isinstance(interpretation['draft'], str) or not interpretation['draft'].strip()
            or len(interpretation['draft']) > 32000
            or interpretation['sources'] != sorted(work.read)
            or any('[source:'+str(index)+']' not in interpretation['draft'] for index in work.read)):
        raise ValueError('local_draft_reference_contract_failed')
    work.verify_holder()
    for source in work.sources.values():
        if digest(Path(source['path'])) != source['sha256']:
            raise ValueError('source_changed_during_draft')
    report = '# Unverified local source draft\n\n'+interpretation['draft'].strip()+'\n\nSources:\n'
    report += ''.join('[source:'+index+'] '+source['path']+' SHA256 '+source['sha256']+'\n'
                      for index, source in sorted(work.sources.items()))
    report_sha256 = hashlib.sha256(report.encode('utf-8')).hexdigest()
    if (directory/'report.md').exists() and digest(directory/'report.md') != report_sha256:
        raise ValueError('saved_draft_receipt_mismatch')
    result = {'status': 'draft_created', 'summary': 'Unverified local draft: '+interpretation['draft'][:1500],
              'report_path': str(directory/'report.md'), 'report_sha256': report_sha256,
              'sources': {source['path']: source['sha256'] for source in work.sources.values()},
              'model': str(model or '')}
    receipt = {'initiative_id': work.assignment['id'], **receipt_context,
               'report_text': report, 'result': result}
    # The receipt is the recoverable artifact. A retry can restore a report if
    # publication was interrupted after this durable receipt became visible.
    write(directory/'draft-receipt.json', receipt)
    restore_report(directory, receipt)
    return result
