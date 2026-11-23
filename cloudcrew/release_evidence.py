"""Export actual pipeline and agent records without turning fixtures into evidence."""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path

from .core import ROOT, redact
from .release_agents import execution_id, read_execution


def export_execution(config, aws, execution, destination, agent_runs=()):
    execution_id(execution)
    records = read_execution(config, aws, execution)
    files = {'execution.json': json.dumps(redact(records), indent=2).encode()}
    for build in records['builds']:
        arn = build.get('logs', {}).get('s3LogsArn', '')
        prefix = 'arn:aws:s3:::' + config['artifact_bucket'] + '/'
        if not arn.startswith(prefix): continue
        key = arn[len(prefix):]
        if not key.startswith('build-logs/'): raise ValueError('Build log is outside the configured log prefix')
        data, _ = aws.get(config['artifact_bucket'], key)
        if data.startswith(b'\x1f\x8b'):
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as log:
                data = log.read(10_000_001)
        if len(data) > 10_000_000: raise ValueError('Build log exceeds the export size limit')
        identifier = build['id'].split(':')[-1]
        if not re.fullmatch(r'[A-Za-z0-9-]{1,128}', identifier): raise ValueError('Invalid recorded build ID')
        files['build-logs/' + identifier + '.txt'] = redact(data.decode('utf-8', errors='replace')).encode()
    for stage in ('validate', 'release'):
        index, _ = aws.get_json(config['artifact_bucket'], f'evidence/{execution}/index-{stage}.json', optional=True)
        if not index: continue
        if index.get('execution_id') != execution or not index['prefix'].startswith(f'evidence/{execution}/'):
            raise ValueError('Evidence index points to another execution')
        for name in index['files']:
            from .release_config import object_path
            object_path(name)
            data, _ = aws.get(config['artifact_bucket'], index['prefix'] + '/' + name)
            files['pipeline/' + stage + '/' + name] = data if name.endswith('.png') else redact(data.decode('utf-8')).encode()
    for run_id in agent_runs:
        if not re.fullmatch(r'\d{8}T\d{6}-[a-f0-9]{8}', run_id): raise ValueError('Use a recorded agent run ID')
        run = ROOT / 'runtime/runs' / run_id
        if run.is_symlink(): raise ValueError('Agent evidence cannot be a symlink')
        events = [json.loads(line) for line in (run / 'events.jsonl').read_text().splitlines()]
        if not any(e['event'] == 'model_response' and str(e['data'].get('response_id', '')).startswith('resp_') for e in events):
            raise ValueError('Agent evidence must include an actual model API response, not a test fixture')
        if not any(e['event'] in ('run_completed', 'run_failed') for e in events): raise ValueError('Wait for the agent run to close before export')
        linked = [e for e in events if e['event'] == 'agent_message' and e['data'].get('context', {}).get('execution_id') == execution]
        if not linked: raise ValueError('Agent run is not linked to the requested pipeline execution')
        for path in run.iterdir():
            if path.is_file() and not path.is_symlink() and path.suffix in ('.json', '.jsonl'):
                files['agents/' + run_id + '/' + path.name] = redact(path.read_text()).encode()
    files['README.txt'] = (f'GBM Cloud Project 6 actual execution evidence\n\nPipeline execution: {execution}\n'
        'execution.json contains AWS execution metadata and retained reports. Pipeline files and S3 build logs are actual saved output.\n'
        'Included agent runs are linked by execution ID and include model response IDs.\n'
        'Failed, rolled-back, blocked, and incomplete phases retain their actual statuses.\n'
        'This export does not claim that missing experiments were performed. Configured secrets and known credential formats are redacted.\n').encode()
    manifest = [{'path': name, 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)} for name, data in sorted(files.items())]
    files['manifest.json'] = json.dumps(manifest, indent=2).encode()
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()): archive.writestr('project6-evidence/' + name, data)
    return {'status': 'exported', 'execution_id': execution, 'files': len(files), 'path': str(destination),
            'sha256': hashlib.sha256(destination.read_bytes()).hexdigest()}
