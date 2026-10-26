"""Explicit continuation into a new job; old failures and revisions stay intact."""
import json
import shutil
from pathlib import Path

from dataelf.discovery.contracts import ArtifactRef, ModelingStageResult
from .graph import GraphService
from .graph.store import write_json
from .sources import file_hash


def rebound_sources(state,workspace):
    """Resolve imported input handles to copied, hash-bound bytes; never edit a revision."""
    from .contracts import SourceInput
    workspace=Path(workspace).resolve();result=[]
    for raw in state.get('source_inputs',[]):
        source=SourceInput.model_validate(raw);path=source.path
        path=(workspace/path).resolve() if not path.is_absolute() else path.resolve()
        if not path.is_relative_to(workspace):
            matches=[relative for relative in state['source_hashes'] if path.as_posix().endswith('/'+relative)]
            if len(matches)!=1:raise ValueError('Cannot resolve imported source to exactly one frozen task file')
            path=workspace/matches[0]
        relative=str(path.relative_to(workspace))
        if relative not in state['source_hashes'] or file_hash(path)!=state['source_hashes'][relative]:
            raise ValueError('Imported source does not match the frozen dependency')
        result.append(source.model_copy(update={'path':path}))
    return result


def resume_published_graph(workspace, manifest_path, *, workflow_policy='legacy'):
    from .capabilities import skill_review
    modern=skill_review({'workflow_policy':workflow_policy})
    workspace = Path(workspace).resolve()
    manifest_path = Path(manifest_path).expanduser().resolve()
    manifest = json.loads(manifest_path.read_text())
    if manifest.get('domain') != 'ai_index' or manifest.get('status') != 'completed':
        raise ValueError('Resume requires a completed AI Index graph manifest')
    bundle = manifest_path.parent
    origin = next((p for p in bundle.parents if (p/'job_spec.json').is_file()), None)
    if origin is None or origin == workspace:
        raise ValueError('Resume requires a separate original job workspace')
    ref = {k: manifest[k] for k in ('graph_id', 'revision_id')}
    old = GraphService(bundle.parents[2], origin)
    if old.store.path(old.ref(ref)).resolve() != bundle:
        raise ValueError('Manifest does not identify its immutable revision')
    state = old.state(ref)  # Checks every published artifact hash.
    review = state.get('review') or {}
    if (review.get('candidate_hash') != old.binding(state)
            or review.get('result', {}).get('verdict') != 'approve'):
        raise ValueError('Resume requires an independent approval bound to this candidate')
    report = old.validate(ref)
    if report.status != 'valid':
        raise ValueError('Resume graph or its frozen sources failed validation')
    for relative, expected in state['source_hashes'].items():
        source = (origin/relative).resolve()
        target = (workspace/relative).resolve()
        if not source.is_relative_to(origin) or not target.is_relative_to(workspace):
            raise ValueError('Resume evidence escapes workspace')
        if target.exists() and file_hash(target) != expected:
            raise ValueError('Resume evidence conflicts with existing workspace')
    # Copy bytes, not links. Original failed job and approved revision are read-only.
    if (origin/'raw').exists():
        shutil.copytree(origin/'raw', workspace/'raw', dirs_exist_ok=True)
    for relative in state['source_hashes']:
        target = workspace/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origin/relative, target)
    root = workspace/'ontology/graphs'
    dest = root/ref['graph_id']/'revisions'/ref['revision_id']
    if dest.exists():
        raise ValueError('Resume destination already exists')
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(bundle, dest)
    if modern:
        parent=state.get('base_revision')
        while parent:
            parent_ref=old.ref({'graph_id':ref['graph_id'],'revision_id':parent});parent_state=old.state(parent_ref)
            target=root/ref['graph_id']/'revisions'/parent
            if not target.exists():shutil.copytree(old.store.path(parent_ref),target)
            for relative,expected in parent_state['source_hashes'].items():
                source=(origin/relative).resolve();output=(workspace/relative).resolve()
                if not source.is_relative_to(origin) or not output.is_relative_to(workspace) or file_hash(source)!=expected:
                    raise ValueError('Previous revision evidence changed or escapes workspace')
                if output.exists() and file_hash(output)!=expected:raise ValueError('Previous revision evidence conflict')
                output.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,output)
            parent=parent_state.get('base_revision')
    write_json(dest.parents[1]/'head.json', {'revision_id': ref['revision_id']})
    service = GraphService(root, workspace)
    if service.validate(ref).status != 'valid':
        raise ValueError('Copied revision or sources failed verification')
    parent_job = origin.name
    write_json(workspace/'artifacts/analysis_continuation.json', {
        'original_job_id': parent_job, 'original_workspace': str(origin),
        'original_manifest': str(manifest_path), 'graph_ref': ref,
        'source_hashes': state['source_hashes'], 'modeling_model_calls': 0})
    artifacts = [ArtifactRef(artifact_id='ontology_'+label, kind=kind,
        path=str((dest/name).relative_to(workspace)), role='evidence', producer_stage='domain_modeling')
        for label, name, kind in [('rdf','graph.nq','ontology_rdf'),
            ('contract','contract.json','modeling_contract'), ('manifest','manifest.json','modeling_manifest'),
            ('validation','validation.json','modeling_validation')]]
    previous_delivery=None
    if modern:
        approved_path=origin/'reviews/analysis/approved.json'
        if approved_path.exists():
            from dataelf.discovery.analysis_review.submissions import verify
            from dataelf.discovery.analysis_review.contracts import check_decision
            approved=json.loads(approved_path.read_text())
            submission=json.loads((origin/'submissions'/approved['submission_id']/'manifest.json').read_text())
            if submission['submission_hash']!=approved['submission_hash']:raise ValueError('Previous delivery binding changed')
            check_decision(approved['decision'],submission,submission.get('previous_review'))
            snapshot=verify(origin,submission,live=False)
            for relative,expected in approved.get('reviewer_evidence',{}).items():
                if file_hash(origin/relative)!=expected:raise ValueError('Previous analysis reviewer evidence changed')
            copied={}
            for relative,expected in submission['files'].items():
                if relative.split('/')[0] not in {'insights','tables','notes','deep_dives','scripts'}:continue
                target=workspace/'notes/previous_delivery'/relative;target.parent.mkdir(parents=True,exist_ok=True)
                if target.exists() and file_hash(target)!=expected:raise ValueError('Previous delivery copy conflict')
                shutil.copyfile(snapshot/relative,target)
                if file_hash(target)!=expected:raise ValueError('Previous delivery copy changed')
                copied[str(target.relative_to(workspace))]=expected
            previous_delivery={'original_objective':submission['spec']['objective'],
                'original_parameters':submission['spec']['parameters'],'submission_hash':submission['submission_hash'],
                'graph_refs':submission['graph_refs'],'files':copied,'meaning':'Previously approved delivery; current task may require new evidence and recalculation.'}
        else:
            previous_delivery={'meaning':'Published graph is available; no approved prior analysis is available.',
                'original_objective':json.loads((origin/'job_spec.json').read_text()).get('objective')}
        write_json(workspace/'notes/previous_delivery/context.json',previous_delivery)
    return ModelingStageResult(status='completed', artifacts=artifacts, graph_ref=ref,
        metrics={'modelCalls': 0, 'resumed': True}, context={
            'graph_ref': ref, 'graph_root': str(root), 'resumed_from_job': parent_job,
            **({'previous_delivery':previous_delivery} if previous_delivery is not None else {}),
            'ontology_metrics': {'modelCalls': 0, 'original_metrics': manifest.get('metrics', {})}})
