"""Task-local, append-only source records around the packaged OpenAPI client."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import uuid

from dataelf.discovery.artifacts import resolve_workspace_path
from dataelf.ontology.contracts import SourceInput
from dataelf.ontology.sources import digest, file_hash, write_json

SKILL_ROOT = Path(__file__).parent / 'pi/skills/ai-index-search'
COMMANDS = ('scholar-search', 'paper-search', 'institution-search', 'institution-funding',
            'news-search', 'github-search', 'huggingface-search', 'twitter-search',
            'youtube-search', 'hackernews-search')
SUCCESS = {'success_empty', 'success_data'}


class EvidenceCatalog:
    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve()
        self.root = self.workspace / 'raw/ai_index/queries'
        self.root.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def lock(self):
        with (self.root / '.lock').open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def records(self):
        return sorted((json.loads(p.read_text()) for p in self.root.glob('*/record.json')),
                      key=lambda r: (r['queried_at'], r['query_id']))

    def verify(self, records=None):
        for item in self.records() if records is None else records:
            path = resolve_workspace_path(self.workspace, item['response_file'])
            if path.is_symlink() or file_hash(path) != item['response_hash']:
                raise ValueError('Registered evidence changed: ' + item['response_file'])

    def sources(self, ids=None):
        rows = [r for r in self.records() if r['outcome'] in SUCCESS]
        if ids is not None:
            wanted = set(ids)
            rows = [r for r in rows if r['source_id'] in wanted]
            if {r['source_id'] for r in rows} != wanted:
                raise ValueError('Unknown or failed source ID; inspect the evidence index')
        self.verify(rows)
        return [SourceInput(source_id=r['source_id'], path=self.workspace / r['response_file'],format=r.get('format','json'),
                            metadata={'query_id': r['query_id'], 'role': r['role'],
                                'query_record_file':str((self.root/r['query_id']/'record.json').relative_to(self.workspace)),
                                'query_record_hash':file_hash(self.root/r['query_id']/'record.json')}) for r in rows]

    def reviewer_dependencies(self, family):
        prefix='ontology_' if family=='graph' else 'analysis_'
        rows=[r for r in self.records() if r['role']==prefix+'reviewer']
        self.verify(rows)
        result={r['response_file']:r['response_hash'] for r in rows}
        for row in rows:
            path=self.root/row['query_id']/'record.json'
            result[path.relative_to(self.workspace).as_posix()]=file_hash(path)
        return result

    def import_file(self, path, *, role, source_id=None):
        path = resolve_workspace_path(self.workspace, str(path))
        if path.is_symlink():
            raise ValueError('Source imports may not be symlinks')
        raw = path.read_bytes();fmt=path.suffix.lower().lstrip('.')
        if fmt=='json':
            value=json.loads(raw);count=len(value) if isinstance(value,list) else 1
        elif fmt=='jsonl':
            count=0
            for line in raw.decode().splitlines():
                if line.strip():json.loads(line);count+=1
        elif fmt=='csv':
            import csv,io
            count=len(list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig')))))
        else:raise ValueError('Local sources must use json, jsonl or csv')
        with self.lock():
            return self._save(raw, role=role, command='local-file', params={'path': str(path.relative_to(self.workspace))},
                              outcome='success_data' if count else 'success_empty',source_id=source_id,
                              http_status=None,business_code=None,format=fmt,record_count=count,record_container='')

    def query(self, command, params, *, role, idempotency_key=None, refresh=False, env=None):
        if command not in COMMANDS or not isinstance(params, dict):
            raise ValueError('Use a supported command and a direct params object')
        clean = json.loads(json.dumps(params, allow_nan=False))
        sensitive={'key','apikey','token','accesstoken','refreshtoken','password','secret','clientsecret','authorization'}
        if any(re.sub(r'[^a-z0-9]','',k.lower()) in sensitive or
               re.search(r'(^|[_-])(key|token|password|secret|authorization)($|[_-])',k,re.I) for k in clean):
            raise ValueError('Credentials are not query parameters')
        for key in ('page', 'size'):
            if key in clean and (type(clean[key]) is not int or clean[key] < 1):
                raise ValueError(key + ' must be a positive integer')
        if clean.get('size', 1) > (20 if command == 'huggingface-search' else 50):
            raise ValueError('size exceeds the documented per-request API limit')
        argv = ['/bin/sh', str(SKILL_ROOT / 'scripts/ai_index_openapi.sh'), command]
        if command == 'institution-funding':
            if set(clean) != {'institution_id'} or not isinstance(clean['institution_id'], str):
                raise ValueError('institution-funding params requires only institution_id')
            argv.append(clean['institution_id'])
        else:
            argv.append(json.dumps(clean, ensure_ascii=False))
        signature = digest({'command': command, 'params': clean, 'role': role, 'refresh': refresh})
        with self.lock():
            if idempotency_key:
                previous = [r for r in self.records() if r.get('idempotency_key') == idempotency_key and r['role'] == role]
                if previous:
                    if previous[0]['request_hash'] != signature:
                        raise ValueError('Query idempotency key conflict; use a new key for a new batch')
                    self.verify(previous)
                    return previous[0]
            runtime = {**os.environ, **(env or {}), 'DATAELF_AI_INDEX_RAW_CLIENT': '1'}
            key = runtime.get('AI_INDEX_API_KEY', '')
            if key and key in json.dumps(clean):
                raise ValueError('Credential value must not appear in query parameters')
            # Connection/tool protection only; never a cumulative model deadline.
            from dataelf.discovery.run_control import current_run, check_cancelled
            control = current_run(); check_cancelled()
            process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       env=runtime, start_new_session=True)
            if control: control.register(process)
            try:
                raw, err = process.communicate(); check_cancelled()
            finally:
                if control: control.unregister(process)
            status = re.search(rb'DATAELF_HTTP_STATUS=(\d{3})', err)
            http = int(status[1]) if status else None
            if key:
                raw = raw.replace(key.encode(), b'[REDACTED]')
            code = None
            try:
                obj = json.loads(raw)
                code = obj.get('code') if isinstance(obj, dict) else None
            except (ValueError, UnicodeError):
                obj = None
            if http not in (None, 0) and not 200 <= http < 300:
                outcome = 'http_error'
            elif process.returncode:
                outcome = 'network_error' if http in (None, 0) and process.returncode in {5,6,7,18,28,35,52,55,56,60,92} else 'client_error'
            elif obj is None:
                outcome = 'non_json'
            elif type(code) is not int or code != 0:
                outcome = 'business_error'
            else:
                data = obj.get('data')
                items = data.get('list', data.get('items', data)) if isinstance(data, dict) else data
                outcome = 'success_empty' if items in ([], {}, None) else 'success_data'
            return self._save(raw, role=role, command=command, params=clean, outcome=outcome,
                              http_status=http, business_code=code, idempotency_key=idempotency_key,
                              request_hash=signature, refresh=refresh)

    def _save(self, raw, **values):
        ident = 'q-' + uuid.uuid4().hex
        folder = self.root / ident; folder.mkdir(mode=0o700)
        try:
            obj = json.loads(raw)
            is_json = True
        except (ValueError, UnicodeError):
            obj = {}; is_json = False
        fmt=values.get('format','json' if is_json else 'text')
        path = folder / ('response.'+fmt)
        path.write_bytes(raw); path.chmod(0o600)
        data = obj.get('data') if isinstance(obj, dict) else None
        items = data.get('list', data.get('items')) if isinstance(data, dict) else data
        container = '/data/list' if isinstance(data, dict) and isinstance(data.get('list'), list) else '/data/items' if isinstance(data, dict) and isinstance(data.get('items'), list) else '/data'
        record = {'query_id': ident, 'source_id': values.pop('source_id', None) or 'ai-index:' + ident,
                  'queried_at': datetime.now(timezone.utc).isoformat(),
                  'response_file': path.relative_to(self.workspace).as_posix(), 'response_hash': file_hash(path),
                  'record_container': container, 'record_count': len(items) if isinstance(items, list) else None,'format':fmt,
                  'pagination': {'page': values['params'].get('page'), 'size': values['params'].get('size'),
                                 'total': data.get('total') if isinstance(data, dict) else None,
                                 'coverage': 'This response only; further API pages may exist'}, **values}
        write_json(folder / 'record.json', record)
        return record

    def read(self, query_id, offset=0, max_bytes=8000):
        from dataelf.ontology.resources import json_page
        rows = [r for r in self.records() if r['query_id'] == query_id]
        if len(rows) != 1: raise ValueError('Unknown query_id')
        row = rows[0]; self.verify(rows)
        path = self.workspace / row['response_file']
        value = json.loads(path.read_bytes()) if path.suffix == '.json' else path.read_bytes().decode('utf8',errors='replace')
        page = json_page(value, offset, max_bytes)
        return {'query': row, **page, 'pagination_layer': 'saved_response_bytes',
                'representation':'decoded_json' if path.suffix=='.json' else 'utf8_text',
                'next_request': {'tool': 'ai_index_read', 'arguments': {'query_id': query_id,
                    'offset': page['next_offset'], 'max_bytes': max_bytes}} if page['next_offset'] is not None else None}
