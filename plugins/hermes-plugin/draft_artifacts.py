"""The shared source/report contract for accepted local drafts."""
import hashlib
import json
import os
from pathlib import Path


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
    with path.open('x') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(0o600)
    sync_directory(path.parent)


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
    write(directory/'report.md', report)
    result = {'status': 'draft_created', 'summary': 'Unverified local draft: '+interpretation['draft'][:1500],
              'report_path': str(directory/'report.md'), 'report_sha256': digest(directory/'report.md'),
              'sources': {source['path']: source['sha256'] for source in work.sources.values()},
              'model': str(model or '')}
    write(directory/'draft-receipt.json', {'initiative_id': work.assignment['id'],
          **receipt_context, 'result': result})
    return result
