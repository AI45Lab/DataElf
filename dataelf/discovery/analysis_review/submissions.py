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


def validate_submission(workspace, contract, spec):
    from dataelf.discovery.domain_services import services
    return services(spec).validate_submission(workspace, contract, spec)


def selected_files(workspace, graph_root=None, graph_refs=(), *, folders):
    workspace=Path(workspace).resolve();files={}
    for folder in folders:
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
                raise ValueError('Analysis requires the approved graph candidate')
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
    from dataelf.discovery.domain_services import services
    files=selected_files(workspace,context.get('graph_root'),refs,folders=services(context['spec']).submission_dirs)
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
        from dataelf.discovery.policies import skill_review
        if not skill_review(submission['spec']):raise ValueError('Supplemental review evidence requires the host policy')
        for relative,expected in reviewer_evidence.items():
            if file_hash(resolve_workspace_path(workspace,relative))!=expected:raise ValueError('Reviewer evidence changed')
    contract=OutputContract.model_validate(submission['contract'])
    validate_submission(snapshot,contract,submission['spec'])
    # All roles have stopped. Replace from approved bytes; a crash cannot mark
    # the job completed until every installed file hash is rechecked.
    for relative in submission['files']:
        from dataelf.discovery.domain_services import services
        if not any(relative.startswith(folder.rstrip('/') + '/') for folder in services(submission['spec']).writable_dirs):continue
        target=workspace/relative;temporary=target.with_name('.'+target.name+'.approved')
        shutil.copyfile(snapshot/relative,temporary);temporary.replace(target)
    verify(workspace,submission,live=True)
    write_json(workspace/'reviews/analysis/approved.json',{
        'submission_id':submission['submission_id'],'submission_hash':submission['submission_hash'],
        'decision':decision.model_dump(mode='json'),'files':submission['files'],'graph_refs':submission['graph_refs'],
        **({'reviewer_evidence':reviewer_evidence} if reviewer_evidence is not None else {})})
    return validate_outputs(workspace,contract)[0]
