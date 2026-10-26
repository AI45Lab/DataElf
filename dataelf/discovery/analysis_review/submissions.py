"""Byte snapshots, dependency binding and idempotent approved installation."""
import hashlib
import json
from pathlib import Path
import shutil
import uuid

from dataelf.discovery.artifacts import validate_outputs, resolve_workspace_path, ArtifactContractError
from dataelf.discovery.contracts import OutputContract
from dataelf.ontology.sources import digest, file_hash
from dataelf.ontology.graph.store import write_json
from .contracts import POLICY_VERSION


def source_references(workspace):
    refs=set()
    def visit(value):
        if isinstance(value,dict):
            for key,item in value.items():
                if (key=='id' or key.endswith('_id') or key in {'url','source_url','homepage','landing_page_url','link','html_url'}) and isinstance(item,(str,int)):
                    refs.add(str(item))
                visit(item)
        elif isinstance(value,list):
            for item in value:visit(item)
    for path in (workspace/'raw').rglob('*.json'):
        refs.add(path.relative_to(workspace).as_posix())
        try:visit(json.loads(path.read_text()))
        except (ValueError,UnicodeError):pass
    return refs


def locator_exists(workspace,locator,refs):
    if locator in refs:return True
    relative,_,pointer=locator.partition('#')
    try:path=resolve_workspace_path(workspace,relative)
    except ArtifactContractError:return False
    if not path.is_file():return False
    if pointer:
        try:
            value=json.loads(path.read_text())
            if not pointer.startswith('/'):return False
            for token in pointer[1:].split('/'):
                token=token.replace('~1','/').replace('~0','~')
                value=value[int(token)] if isinstance(value,list) else value[token]
        except (ValueError,KeyError,IndexError,TypeError):return False
    return True


def validate_submission(workspace, contract, spec):
    workspace=Path(workspace).resolve()
    outputs,warnings=validate_outputs(workspace,contract)
    signals=json.loads((workspace/'insights/candidate_signals.json').read_text())['candidate_signals']
    ids=[]
    for index,signal in enumerate(signals):
        if not isinstance(signal,dict) or not isinstance(signal.get('signal_id'),str) or not signal['signal_id'].strip():
            raise ArtifactContractError(f'candidate_signals[{index}].signal_id must be nonempty')
        if not isinstance(signal.get('summary'),str) or not signal['summary'].strip():
            raise ArtifactContractError(f'candidate_signals[{index}].summary must contain the actual signal')
        ids.append(signal['signal_id'])
    if len(set(ids))!=len(ids):raise ArtifactContractError('candidate_signals signal_id values must be unique')
    insights=json.loads((workspace/'insights/insight_candidates.json').read_text())['insight_candidates']
    refs=source_references(workspace)
    maximum=spec['parameters'].get('expected_outputs')
    if maximum is not None and len(insights)>int(maximum):
        raise ArtifactContractError('insight_candidates exceeds the user requested maximum')
    insight_ids=[]
    for index,item in enumerate(insights):
        insight_ids.append(item['insight_id'])
        for ident in item['supporting_signals']:
            if ident not in ids:raise ArtifactContractError(f'insight_candidates[{index}].supporting_signals references unknown signal {ident}')
        for artifact in item['analysis_artifacts']:
            relative=artifact if isinstance(artifact,str) else artifact['path']
            path=resolve_workspace_path(workspace,relative)
            if not path.is_file() or not path.stat().st_size:
                raise ArtifactContractError(f'insight_candidates[{index}].analysis_artifacts missing file: {relative}')
        for support in item['external_support']:
            locators=[support] if isinstance(support,str) else [str(value) for key,value in support.items()
                if value and (key in {'path','id','url','source_url','link','html_url'} or key.endswith('_id'))]
            if not locators or not any(locator_exists(workspace,loc,refs) for loc in locators):
                raise ArtifactContractError(f'insight_candidates[{index}].external_support needs a resolvable original ID, saved URL or source file pointer')
    if len(set(insight_ids))!=len(insight_ids):raise ArtifactContractError('insight_id values must be unique')
    return outputs,warnings


def selected_files(workspace, graph_root=None, graph_refs=()):
    workspace=Path(workspace).resolve();files={}
    for folder in ['raw','tables','scripts','notes','deep_dives','insights']:
        for path in (workspace/folder).rglob('*'):
            if path.is_file():
                if path.is_symlink() or not path.resolve().is_relative_to(workspace):
                    raise ValueError('Submission may not contain symlinks or external paths')
                files[path.relative_to(workspace).as_posix()]=file_hash(path)
    if graph_refs:
        from dataelf.ontology.graph import GraphService
        service=GraphService(graph_root,workspace)
        for ref in graph_refs:
            if not ref.get('revision_id') or ref.get('workspace_id'):raise ValueError('Analysis binds published revisions only')
            state=service.state(ref)
            if service.validate(ref).status!='valid':raise ValueError('Bound graph revision failed validation')
            review=state.get('review') or {}
            if review.get('candidate_hash')!=service.binding(state) or review.get('result',{}).get('verdict')!='approve':
                raise ValueError('AI Index analysis requires the approved graph candidate')
            bundle=service.store.path(service.ref(ref))
            for path in bundle.rglob('*'):
                if path.is_file():files[path.relative_to(workspace).as_posix()]=file_hash(path)
    return files


def freeze(context, idempotency_key, response_path=None, graph_refs=None):
    import fcntl
    root=Path(context['workspace'])/'submissions';root.mkdir(parents=True,exist_ok=True)
    with (root/'.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        return _freeze(context,idempotency_key,response_path,graph_refs)


def _freeze(context, idempotency_key, response_path=None, graph_refs=None):
    workspace=Path(context['workspace']).resolve();contract=OutputContract.model_validate(context['contract'])
    validate_submission(workspace,contract,context['spec'])
    from dataelf.ontology.graph.contracts import GraphRef
    refs=[GraphRef.model_validate(ref).model_dump(mode='json',exclude_none=True)
          for ref in (graph_refs if graph_refs is not None else context.get('graph_refs',[]))]
    if refs and not context.get('graph_root'):raise ValueError('No graph service supplied')
    files=selected_files(workspace,context.get('graph_root'),refs)
    for relative,expected in context.get('protected_hashes',{}).items():
        if files.get(relative)!=expected:raise ValueError('Previously frozen evidence changed: '+relative)
    response=None
    if response_path:
        path=resolve_workspace_path(workspace,response_path);response=path.read_text()
        files[path.relative_to(workspace).as_posix()]=file_hash(path)
    body={'files':files,'spec':context['spec'],'contract':context['contract'],
          'graph_refs':refs,'policy_version':POLICY_VERSION,'response':response,'previous_review':context.get('previous_review')}
    binding=digest(body);root=workspace/'submissions';root.mkdir(exist_ok=True)
    key=hashlib.sha256(idempotency_key.encode()).hexdigest();index=root/('key-'+key+'.json')
    if index.exists():
        previous=json.loads(index.read_text())
        if previous['submission_hash']!=binding:raise ValueError('Submission idempotency key conflict')
        return previous
    ident='submission_'+uuid.uuid4().hex[:16];staging=root/('.'+ident);staging.mkdir()
    try:
        for relative,expected in files.items():
            source=workspace/relative;target=staging/'files'/relative;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(source,target)
            if file_hash(target)!=expected or file_hash(source)!=expected:raise ValueError('File changed while freezing: '+relative)
        validate_submission(staging/'files',contract,context['spec'])
        submission={**body,'submission_id':ident,'submission_hash':binding,
                    'parent_submission':context.get('parent_submission'),'idempotency_key':idempotency_key}
        write_json(staging/'manifest.json',submission)
        staging.rename(root/ident);write_json(index,submission)
        return submission
    finally:
        if staging.exists():shutil.rmtree(staging)


def verify(workspace, submission, *, live=True):
    workspace=Path(workspace);snapshot=workspace/'submissions'/submission['submission_id']/'files'
    body={k:submission[k] for k in ['files','spec','contract','graph_refs','policy_version','response','previous_review']}
    if digest(body)!=submission['submission_hash']:raise ValueError('Submission manifest hash changed')
    for relative,expected in submission['files'].items():
        if file_hash(snapshot/relative)!=expected:raise ValueError('Frozen submission changed: '+relative)
        if live and (not (workspace/relative).is_file() or file_hash(workspace/relative)!=expected):
            raise ValueError('Live report/evidence changed after submission: '+relative)
    return snapshot


def install(workspace, submission, decision, *, reviewer_evidence=None):
    from .contracts import check_decision
    workspace=Path(workspace);decision=check_decision(decision,submission,submission.get('previous_review'))
    if decision.verdict!='approve' or decision.task_assessment!='fulfilled':raise ValueError('Only fulfilled approval may complete')
    snapshot=verify(workspace,submission,live=True)
    if reviewer_evidence is not None:
        from dataelf.ontology.capabilities import skill_review
        if not skill_review(submission['spec']):raise ValueError('Supplemental review evidence requires the host policy')
        for relative,expected in reviewer_evidence.items():
            if file_hash(resolve_workspace_path(workspace,relative))!=expected:raise ValueError('Reviewer evidence changed')
    contract=OutputContract.model_validate(submission['contract'])
    validate_submission(snapshot,contract,submission['spec'])
    # All roles have stopped. Replace from approved bytes; a crash cannot mark
    # the job completed until every installed file hash is rechecked.
    for relative in submission['files']:
        if relative.split('/')[0] not in {'insights','notes','deep_dives','tables','scripts'}:continue
        target=workspace/relative;temporary=target.with_name('.'+target.name+'.approved')
        shutil.copyfile(snapshot/relative,temporary);temporary.replace(target)
    verify(workspace,submission,live=True)
    write_json(workspace/'reviews/analysis/approved.json',{
        'submission_id':submission['submission_id'],'submission_hash':submission['submission_hash'],
        'decision':decision.model_dump(mode='json'),'files':submission['files'],'graph_refs':submission['graph_refs'],
        **({'reviewer_evidence':reviewer_evidence} if reviewer_evidence is not None else {})})
    return validate_outputs(workspace,contract)[0]
