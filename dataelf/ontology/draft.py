"""Role-local contract drafts. Atomic JSON Pointer edits never modify evidence."""
from copy import deepcopy
from contextlib import contextmanager
import fcntl
import re

from jsonschema import Draft202012Validator

from .contracts import OntologyContract
from .errors import OntologyError
from .sources import digest, write_json
from .submission_validation import pointer_error


@contextmanager
def draft_lock(context_path):
    with context_path.with_name('draft.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock,fcntl.LOCK_UN)


def edit_document(document, operations):
    result = deepcopy(document)
    allowed = set(OntologyContract.model_fields) - {'domain', 'schema_version'}
    for operation in operations:
        pointer = operation['path']
        if pointer_error(pointer) or not pointer or pointer.split('/')[1] not in allowed:
            raise ValueError('patch path must address a contract field; domain/version are immutable')
        tokens = [s.replace('~1','/').replace('~0','~') for s in pointer.split('/')[1:]]
        target = result
        def array_index(token):
            if not re.fullmatch(r'0|[1-9][0-9]*',token):
                raise ValueError('patch array index must be a canonical non-negative integer')
            return int(token)
        for token in tokens[:-1]:
            target = target[array_index(token)] if isinstance(target,list) else target[token]
        key = tokens[-1]
        op = operation['op']
        value = deepcopy(operation.get('value'))
        if isinstance(target,list):
            index = len(target) if key == '-' and op == 'add' else array_index(key)
            if index < 0 or index > len(target) or (op != 'add' and index == len(target)):
                raise ValueError('patch array index out of range')
            if op == 'add': target.insert(index,value)
            elif op == 'replace': target[index] = value
            else: del target[index]
        elif isinstance(target,dict):
            if op != 'add' and key not in target:
                raise ValueError('replace/remove must address an existing field')
            if op == 'remove': del target[key]
            else: target[key] = value
        else:
            raise ValueError('patch parent must be an object or array')
    # Partial root is allowed; present components must have their actual shape.
    schema = OntologyContract.model_json_schema()
    schema['required'] = []
    errors = list(Draft202012Validator(schema).iter_errors(result))
    if errors:
        raise OntologyError('ONTOLOGY_ARGUMENT_INVALID','arguments',[
            {'path':'/'+'/'.join(map(str,e.absolute_path)),
             'message':'does not satisfy '+e.validator,'expected':e.validator_value} for e in errors[:20]])
    return result


def draft_state(context_path, context):
    from .workflow_state import checkpoint_directory
    path = checkpoint_directory(context_path,context)/'draft.json'
    if path.exists():
        import json
        return json.loads(path.read_text())
    return deepcopy(context.get('previous_draft') or context.get('previous_contract') or {
        'schema_version':'dataelf.ontology.v1', 'domain':context['request']['domain'],
        'classes':{}, 'properties':{}, 'mappings':[]})


def save_draft(context_path, document):
    import json
    from .workflow_state import checkpoint_directory
    context=json.loads(context_path.read_text())
    write_json(checkpoint_directory(context_path,context)/'draft.json',document)
    write_json(context_path.with_name('draft.json'), document)
    return digest(document)
