"""Domain-independent JSON output schemas and controlled file serialization."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

from jsonschema import Draft202012Validator

from .artifacts import ArtifactContractError, resolve_workspace_path
from .contracts import OutputContract


def output_schema(spec):
    schema = deepcopy(spec.json_schema or {})
    if spec.json_root:
        root = {'type':'object','required':[spec.json_root],
                'properties':{spec.json_root:{'type':'array'}}}
        schema = {'allOf':[schema,root]} if spec.json_schema else root
    return schema


def writable_json(spec):
    return spec.agent_writable and not spec.path.startswith(('raw/','ontology/')) and (
        spec.media_type == 'application/json' or spec.json_root is not None)


def submission_schema(contract):
    branches = []
    definitions = {}
    for i,spec in enumerate(contract.artifacts):
        if not writable_json(spec):
            continue
        schema = output_schema(spec)
        def move_refs(value):
            if isinstance(value,dict):
                for key,item in list(value.items()):
                    if key == '$ref' and isinstance(item,str) and item.startswith('#/$defs/'):
                        value[key] = '#/$defs/' + str(i) + '_' + item[len('#/$defs/'):]
                    else: move_refs(item)
            elif isinstance(value,list):
                for item in value:move_refs(item)
        move_refs(schema)
        def collect(value):
            if isinstance(value,dict):
                for key,item in value.pop('$defs',{}).items():definitions[str(i)+'_'+key] = item
                for item in value.values():collect(item)
            elif isinstance(value,list):
                for item in value:collect(item)
        collect(schema)
        branches.append({'type':'object','properties':{
            'artifact_id':{'type':'string','const':spec.artifact_id},'data':schema},
            'required':['artifact_id','data'],'additionalProperties':False})
    if not branches:
        return None
    return {'type':'object','oneOf':branches,**({'$defs':definitions} if definitions else {})}


def validate_data(spec, data):
    errors = list(Draft202012Validator(output_schema(spec)).iter_errors(data))
    if errors:
        raise ArtifactContractError(json.dumps({'code':'OUTPUT_SCHEMA_INVALID', 'artifact_id':spec.artifact_id,
            'issues':[{'path':'/'+'/'.join(map(str,e.absolute_path)),
                       'message':'does not satisfy '+e.validator,'expected':e.validator_value}
                      for e in errors[:20]]},ensure_ascii=False))


def submit_output(workspace, contract, artifact_id, data, protected_paths=()):
    spec = next((s for s in contract.artifacts if s.artifact_id == artifact_id),None)
    if spec is None or not writable_json(spec):
        raise ArtifactContractError('output is undeclared or not agent-writable JSON')
    target = resolve_workspace_path(workspace,spec.path)
    resolved_protected = {resolve_workspace_path(workspace,p) for p in protected_paths}
    relative = target.relative_to(workspace.resolve()).as_posix()
    if target in resolved_protected or relative.startswith(('raw/','ontology/')) or relative == 'job_spec.json':
        raise ArtifactContractError('output cannot overwrite input/evidence artifacts')
    validate_data(spec,data)
    target.parent.mkdir(parents=True,exist_ok=True)
    # Unique temporary file avoids following a model-created .tmp symlink.
    import tempfile
    with tempfile.NamedTemporaryFile('w',dir=target.parent,encoding='utf-8',delete=False) as stream:
        json.dump(data,stream,ensure_ascii=False,indent=2,allow_nan=False)
        stream.write('\n')
        temporary = Path(stream.name)
    temporary.replace(target)
    # Legacy file writers used submitted to mean saved, never a control event.
    return {'saved':True,'submitted':True,'status':'file_saved','artifact_id':artifact_id,'path':spec.path,
            'meaning':'One validated file saved; complete delivery requires analysis_submit.'}


def normalize_known_roots(workspace, contract, protected_paths=()):
    """Lossless legacy adaptation only: a declared array root gets its object envelope."""
    changed = []
    for spec in contract.artifacts:
        if not writable_json(spec) or not spec.json_root:
            continue
        path = resolve_workspace_path(workspace,spec.path)
        if path in {resolve_workspace_path(workspace,p) for p in protected_paths}:
            continue
        if not path.is_file():
            continue
        before = path.read_bytes()
        try:data=json.loads(before)
        except (ValueError,UnicodeError):continue
        if not isinstance(data,list):
            continue
        wrapped = {spec.json_root:data}
        validate_data(spec,wrapped)
        key = hashlib.sha256(spec.artifact_id.encode()).hexdigest()
        audit = resolve_workspace_path(workspace,'reviews/output_normalization/'+key+'.json')
        audit.parent.mkdir(parents=True,exist_ok=True)
        audit.write_text(json.dumps({'artifact_id':spec.artifact_id,'path':spec.path,
            'root':spec.json_root,'original':data,'original_sha256':hashlib.sha256(before).hexdigest()},
            ensure_ascii=False,indent=2),encoding='utf-8')
        submit_output(workspace,contract,spec.artifact_id,wrapped,protected_paths)
        changed.append(spec.artifact_id)
    return changed


if __name__ == '__main__':
    try:
        request = json.load(sys.stdin)
        result = submit_output(Path(sys.argv[1]),OutputContract.model_validate(request['contract']),
            request['artifact_id'],request['data'],request.get('protected_paths',[]))
        print(json.dumps(result,ensure_ascii=False))
    except (ValueError,OSError,KeyError) as exc:
        print(str(exc),file=sys.stderr)
        sys.exit(1)
