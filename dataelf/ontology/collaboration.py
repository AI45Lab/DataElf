"""AI Index handoffs and review memory in existing task review storage.

Local snapshots never validate or publish a complete delivery. Only reviewers
update issue status; authors' responses remain separate, immutable evidence.
"""
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import shutil
import uuid
from datetime import datetime,timezone

from dataelf.discovery.artifacts import resolve_workspace_path
from .sources import digest, file_hash, select
from .graph.store import write_json


class ReviewHandoff(Exception):
    def __init__(self, request):
        self.request = request
        super().__init__('Persisted review handoff: ' + request['request_id'])


class ReviewLedger:
    def __init__(self, workspace, family):
        if family not in {'graph', 'analysis'}: raise ValueError('Unknown review family')
        self.workspace = Path(workspace).resolve(); self.family = family
        self.root = self.workspace / 'reviews' / family / 'collaboration'
        self.root.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def lock(self):
        with (self.root / '.lock').open('a') as f:
            fcntl.flock(f, fcntl.LOCK_EX); yield

    def load(self, ident):
        if not ident.startswith('review-') or not ident[7:].isalnum(): raise ValueError('Invalid review request ID')
        return json.loads((self.root / 'requests' / ident / 'request.json').read_text())

    def requests(self):
        rows = [json.loads(p.read_text()) for p in self.root.glob('requests/*/request.json')]
        return sorted(rows, key=lambda r: r['sequence'])

    def request(self, context, *, object_kind, question, evidence, idempotency_key,
                paths=(), graph_ref=None, mode='local', commit_args=None):
        if context['role'].endswith('reviewer'): raise ValueError('Reviewers cannot recursively request review')
        if not question.strip(): raise ValueError('A concrete question or maintenance purpose is required')
        files = {}; document = None; candidate_hash = None;graph_service=None
        if object_kind == 'draft':
            from .draft import draft_state
            if 'request' not in context: raise ValueError('No ontology draft is supplied')
            document = draft_state(Path(context['_context_path']), context)
        elif object_kind == 'candidate':
            from .graph import GraphService
            if not context.get('graph_root') or not graph_ref: raise ValueError('No candidate graph supplied')
            permitted=context.get('graph_ref') or next(iter(context.get('graph_refs') or []),{})
            if graph_ref.get('graph_id') != permitted.get('graph_id'):
                raise ValueError('Candidate is outside the supplied graph')
            service = GraphService(context['graph_root'], self.workspace)
            graph_service=service
            state = service.state(graph_ref); candidate_hash = service.binding(state)
            document = state.get('contract')
            files.update(state.get('source_hashes', {}))
        elif object_kind == 'report':
            if self.family != 'analysis' or not paths: raise ValueError('Report feedback requires explicit files')
            for relative in paths:
                path = resolve_workspace_path(self.workspace, relative)
                if Path(relative).parts[0] not in {'insights', 'tables', 'notes', 'scripts', 'deep_dives'}:
                    raise ValueError('Review object must be a report or supporting analysis file')
                files[relative] = file_hash(path)
        else: raise ValueError('Unsupported local review object')
        for ref in evidence:
            relative, sep, pointer = ref.partition('#')
            path = resolve_workspace_path(self.workspace, relative)
            if not sep or (pointer and not select(json.loads(path.read_text()), pointer)):
                raise ValueError('Evidence must resolve to file#pointer')
            files[relative] = file_hash(path)
        from dataelf.domains.ai_index.evidence import EvidenceCatalog
        catalog=EvidenceCatalog(self.workspace)
        for row in catalog.records():
            if row['response_file'] in files:
                metadata=catalog.root/row['query_id']/'record.json'
                files[str(metadata.relative_to(self.workspace))]=file_hash(metadata)
        binding = digest({'kind': object_kind, 'document': document, 'files': files,
                          'candidate_hash': candidate_hash, 'question': question, 'mode': mode})
        with self.lock():
            key = self.root / ('key-' + digest(idempotency_key) + '.json')
            if key.exists():
                old = self.load(json.loads(key.read_text())['request_id'])
                if old['object_hash'] != binding: raise ValueError('Review idempotency key conflict')
                return old
            ident = 'review-' + uuid.uuid4().hex
            folder = self.root / 'requests' / ident
            snapshot = folder / 'files'; snapshot.mkdir(parents=True)
            for relative, expected in files.items():
                source = resolve_workspace_path(self.workspace, relative)
                target = snapshot / relative; target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                if file_hash(target) != expected or file_hash(source) != expected:
                    raise ValueError('Review dependency changed while freezing')
            if document is not None: write_json(snapshot / 'review_object.json', document)
            extra_files={};snapshot_graph_root=None
            if graph_service:
                from .graph.store import GraphStore
                snapshot_graph_root=str(snapshot/'graphs')
                graph_copy=GraphStore(snapshot_graph_root).path(graph_service.ref(graph_ref))
                if graph_ref.get('workspace_id'):
                    write_json(graph_copy/'state.json',state)
                else:shutil.copytree(graph_service.store.path(graph_service.ref(graph_ref)),graph_copy)
                parent=state.get('base_revision')
                while parent:
                    prior_ref=graph_service.ref({'graph_id':graph_ref['graph_id'],'revision_id':parent})
                    target=GraphStore(snapshot_graph_root).path(prior_ref)
                    if not target.exists():shutil.copytree(graph_service.store.path(prior_ref),target)
                    parent=graph_service.state(prior_ref).get('base_revision')
                graph_service.dataset(graph_ref).serialize(destination=snapshot/'review_dataset.nq',format='nquads')
                extra_files={str(p.relative_to(snapshot)):file_hash(p) for p in snapshot.rglob('*') if p.is_file() and str(p.relative_to(snapshot)) not in files}
            value = {'request_id': ident, 'family': self.family, 'sequence': len(self.requests()) + 1,
                     'mode': mode, 'status': 'requested', 'author_role': context['role'],
                     'object_kind': object_kind, 'object_hash': binding, 'question': question,
                     'files': files, 'candidate_hash': candidate_hash, 'graph_ref': graph_ref,
                     'object_file_hash':file_hash(snapshot/'review_object.json') if document is not None else None,
                     'snapshot_graph_root':snapshot_graph_root,'extra_snapshot_files':extra_files,
                     'snapshot_path': str(snapshot), 'evidence': evidence,
                     'commit_args': commit_args, 'feedback': None}
            write_json(folder / 'request.json', value); write_json(key, {'request_id': ident})
            return value

    def verify(self, request):
        snapshot = Path(request['snapshot_path'])
        for relative, expected in request['files'].items():
            if file_hash(snapshot / relative) != expected: raise ValueError('Review snapshot changed')
        if request.get('object_file_hash') and file_hash(snapshot/'review_object.json')!=request['object_file_hash']:
            raise ValueError('Review draft snapshot changed')
        for relative,expected in request.get('extra_snapshot_files',{}).items():
            if file_hash(snapshot/relative)!=expected:raise ValueError('Review graph snapshot changed')
        return snapshot

    def stage(self, ident, status):
        if status not in {'reviewing', 'feedback_saved', 'author_scheduled', 'delivered'}:
            raise ValueError('Unknown handoff status')
        with self.lock():
            value = self.load(ident); value['status'] = status
            write_json(self.root / 'requests' / ident / 'request.json', value)

    def published(self,ident,result):
        with self.lock():
            value=self.load(ident);value['publication']=result.model_dump(mode='json')
            write_json(self.root/'requests'/ident/'request.json',value)

    def issues(self):
        path = self.root / 'issues.json'
        return json.loads(path.read_text()) if path.exists() else {}

    def prior(self):
        return {'issues': [r['issue'] for r in self.issues().values()
                           if r['status'] in {'unresolved', 'regressed'}]}

    def record_review(self, result, *, request_id=None):
        with self.lock():
            state = self.issues()
            for update in result.get('issue_updates', []):
                ident = update['issue_id']
                if ident not in state: raise ValueError('Review update references unknown issue')
                state[ident].update(status=update['status'], update=update)
            for issue in result.get('issues', []):
                ident = issue['issue_id']
                state[ident] = {'issue': issue, 'status': 'unresolved', 'request_id': request_id}
            write_json(self.root / 'issues.json', state)
            if request_id:
                value = self.load(request_id); value.update(status='feedback_saved', feedback=result)
                from dataelf.domains.ai_index.evidence import EvidenceCatalog
                value['reviewer_evidence']=EvidenceCatalog(self.workspace).reviewer_dependencies(self.family)
                write_json(self.root / 'requests' / request_id / 'feedback.json', result)
                write_json(self.root / 'requests' / request_id / 'request.json', value)

    def respond(self, issue_id, response, explanation, evidence, change_refs=()):
        if issue_id not in self.issues(): raise ValueError('Unknown issue ID')
        # This never changes the reviewer-owned issue status.
        value = {'issue_id': issue_id, 'response': response, 'explanation': explanation,'at':datetime.now(timezone.utc).isoformat(),
                 'evidence': evidence, 'change_refs': list(change_refs)}
        write_json(self.root / 'responses' / (uuid.uuid4().hex + '.json'), value)
        return value

    def responses(self):
        return sorted([json.loads(p.read_text()) for p in self.root.glob('responses/*.json')],key=lambda r:r.get('at',''))

    def checks(self):
        rows=[json.loads(p.read_text()) for p in self.root.glob('checks/*.json')]
        for row in rows:row.setdefault('ordering_known',bool(row.get('sequence')))
        return sorted(rows,key=lambda r:(r.get('sequence') is not None,r.get('sequence',0),r['check_id']))

    def dependency(self,context,relative):
        scratch=relative.startswith('scratch/')
        root=Path(context.get('review_scratch') or self.root/'scratch') if scratch else Path(context.get('review_snapshot') or self.workspace)
        path=resolve_workspace_path(root,relative[len('scratch/'):] if scratch else relative)
        if not path.is_file() and not scratch:
            from dataelf.domains.ai_index.evidence import EvidenceCatalog
            catalog=EvidenceCatalog(self.workspace);rows=catalog.records()
            allowed={r['response_file'] for r in rows}|{str((catalog.root/r['query_id']/'record.json').relative_to(self.workspace)) for r in rows}
            if relative not in allowed:raise ValueError('Check dependency is outside review visibility')
            catalog.verify([r for r in rows if relative==r['response_file']])
            path=resolve_workspace_path(self.workspace,relative)
        if not path.is_file():raise ValueError('Review dependency does not exist: '+relative)
        return path

    def validate_checks(self,context,identifiers):
        known={c['check_id']:c for c in self.checks()}
        for ident in identifiers:
            if ident not in known:raise ValueError('Feedback references unknown reviewer checks')
            check=known[ident]
            expected=context.get('review_object_hash') or (context.get('submission') or {}).get('submission_hash')
            if (expected is not None or check.get('object_hash') is not None) and expected!=check.get('object_hash'):
                raise ValueError('Reviewer check belongs to a different review object')
            if (context.get('candidate_hash') is not None or check.get('candidate_hash') is not None) and check.get('candidate_hash')!=context.get('candidate_hash'):
                raise ValueError('Reviewer check belongs to a different graph candidate')
            for relative,expected in check['dependencies'].items():
                if file_hash(self.dependency(context,relative))!=expected:
                    raise ValueError('Reviewer check dependency changed: '+relative)

    def save_check(self, context, *, method, result, conclusion, coverage, dependencies):
        if not context['role'].endswith('reviewer'): raise ValueError('Only reviewers save independent checks')
        if not dependencies: raise ValueError('Checks require exact dependencies')
        bound = {}
        from dataelf.domains.ai_index.evidence import EvidenceCatalog
        catalog=EvidenceCatalog(self.workspace)
        records={r['response_file']:r for r in catalog.records()}
        for relative in dependencies:
            path = self.dependency(context,relative)
            bound[relative] = file_hash(path)
            if relative in records:
                record=str((catalog.root/records[relative]['query_id']/'record.json').relative_to(self.workspace))
                bound[record]=file_hash(self.dependency(context,record))
        value = {'role': context['role'], 'family': self.family, 'object_hash': context.get('review_object_hash') or (context.get('submission') or {}).get('submission_hash'),
                 'candidate_hash': context.get('candidate_hash'), 'dependencies': bound,
                 'method': method, 'result': result, 'conclusion': conclusion, 'coverage': coverage,
                 'status': 'reviewer_recorded'}
        ident = 'check-' + digest(value)
        path=self.root/'checks'/(ident+'.json')
        with self.lock():
            if path.exists():saved=json.loads(path.read_text())
            else:
                sequence=max((r.get('sequence',0) for r in self.checks()),default=0)+1
                saved={'check_id':ident,**value,'sequence':sequence,'created_at':datetime.now(timezone.utc).isoformat(),'ordering_known':True}
                write_json(path,saved)
        return self.check_index(saved)

    @staticmethod
    def check_index(check):
        return {k:check.get(k) for k in ('check_id','status','object_hash','candidate_hash','sequence','created_at','ordering_known')} | {
            'conclusion_excerpt':check['conclusion'][:200],'coverage_excerpt':check['coverage'][:200],
            'dependency_count':len(check['dependencies']),
            'next_request':{'tool':'review_inspect','arguments':{'resource':'record','collection':'checks','record_id':check['check_id']}}}

    def save_record(self,context,kind,value,dependencies):
        if not context['role'].endswith('reviewer'):raise ValueError('Only reviewers save review records')
        if kind not in {'issue','issue_update'}:raise ValueError('Unknown review record kind')
        # Reuse dependency resolution and binding validation from check storage.
        check=self.save_check(context,method='review '+kind,result=value,conclusion=value.get('message',value.get('explanation','')),
            coverage=value.get('location','Prior issue '+value['issue_id']),dependencies=dependencies)
        return {**check,'record_kind':kind,'issue_id':value['issue_id'],'saved':True,'status':'saved_unsubmitted'}

    def assemble(self,context,payload):
        value=dict(payload)
        for refs,target,kind in [('check_ids','checks','check'),('issue_ids','issues','issue'),('issue_update_ids','issue_updates','issue_update')]:
            ids=value.pop(refs,[])
            if len(ids)!=len(set(ids)):raise ValueError('Duplicate reviewer record references')
            self.validate_checks(context,ids)
            rows={c['check_id']:c for c in self.checks()}
            assembled=[]
            for ident in ids:
                row=rows[ident]
                if kind=='check':
                    if row['method'].startswith('review '):raise ValueError('An issue record is not an independent check')
                    assembled.append({'location':row['coverage'],'method':row['method'],'evidence':list(row['dependencies']),
                        'result':row['result'],'coverage':row['coverage']})
                else:
                    if row['method']!='review '+kind:raise ValueError('Wrong reviewer record kind')
                    assembled.append(row['result'])
            if assembled and not (self.family=='graph' and target=='checks'):value[target]=[*value.get(target,[]),*assembled]
            if ids and self.family=='graph' and target=='checks' and context.get('_context_path'):
                write_json(Path(context['_context_path']).with_name('review_check_references.json'),
                    {'candidate_hash':context.get('candidate_hash'),'checks':ids,'dependencies':{i:rows[i]['dependencies'] for i in ids}})
        return value

    def summary(self):
        requests=self.requests();responses=self.responses();checks=self.checks();issues=self.issues()
        def read(resource,offset=0):return {'tool':'review_inspect','arguments':{'family':self.family,'resource':resource,'offset':offset}}
        return {'representation':'Bounded index and excerpts; complete records remain available through review_inspect.',
                'requests':[{'request_id':r['request_id'],'status':r['status'],'question_excerpt':r['question'][:400]} for r in requests[-5:]],
                'unresolved_issue_count':sum(i['status'] in {'unresolved','regressed'} for i in issues.values()),
                'author_responses':[{'issue_id':r['issue_id'],'response':r['response'],'explanation_excerpt':r['explanation'][:600]} for r in responses[-3:]],
                'historical_checks_without_order':sum(not c.get('ordering_known') for c in checks),
                'check_index':[{'check_id':c['check_id'],'coverage_excerpt':c['coverage'][:300],
                    'conclusion_excerpt':c['conclusion'][:300],'dependency_count':len(c['dependencies'])} for c in [c for c in checks if c.get('ordering_known')][-5:]],
                'read_requests':read('requests'),'read_issues':read('issues'),
                'read_responses':read('responses',max(0,len(responses)-3)),
                'read_checks':read('checks',max(0,len(checks)-5))}
