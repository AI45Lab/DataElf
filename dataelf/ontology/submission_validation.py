"""Validate executable expression structure and scoped references before review."""
import re

from .errors import OntologyError
from .expressions import ARITY, OPS, absolute_iri


def pointer_error(value):
    if not isinstance(value, str) or (value and not value.startswith('/')):
        return 'expected JSON pointer: empty string or a path starting with /'
    if re.search(r'~(?![01])', value):
        return 'invalid JSON Pointer escape; use ~0 for ~ and ~1 for /'
    if any(token in ('.', '..') for token in value.split('/')):
        return 'parent navigation is unsupported; child groups inherit parent nodes via {"node":"alias"}'
    return None


def validate_authoring(contract, context_keys=None):
    issues = []

    def issue(path, message, expected=None):
        item = {'path': path, 'message': message}
        if expected is not None:
            item['expected'] = expected
        issues.append(item)

    def pointer(value, path):
        if message := pointer_error(value):
            issue(path, message)

    def expression(value, path, names):
        if not isinstance(value, dict):
            return  # Scalars and arrays are literal constants, never implicitly paths.
        selectors = set(value) & {'path', 'all', 'context', 'node', 'op'}
        if len(selectors) != 1:
            issue(path, 'expression must contain exactly one of path/all/context/node/op')
            return
        key = next(iter(selectors))
        op = value.get('op')
        allowed = {key}
        if key == 'op':
            if not isinstance(op, str) or op not in OPS:
                issue(path + '/op', 'unsupported expression operation', sorted(OPS))
                return
            allowed |= {'args'}
            allowed |= {'fields'} if op == 'object' else {'length'} if op == 'hash' else {'safe'} if op == 'quote' else set()
        for extra in sorted(set(value) - allowed):
            issue(path + '/' + extra, 'unsupported expression field', sorted(allowed))
        if key in ('path', 'all'):
            pointer(value[key], path + '/' + key)
        elif key in ('node', 'context'):
            if not isinstance(value[key], str):
                issue(path + '/' + key, 'expected a string')
            elif key == 'node' and value[key] not in names:
                issue(path + '/node', 'undefined node alias in this scope', sorted(names))
            elif key == 'context' and context_keys is not None and value[key] not in context_keys:
                issue(path + '/context', 'unknown context key', sorted(context_keys))
        else:
            args = value.get('args', [])
            if not isinstance(args, list):
                issue(path + '/args', 'expression args must be an array')
            else:
                count = ARITY.get(op)
                if (count is not None and len(args) != count) or (count is None and not args):
                    issue(path + '/args', 'invalid argument count for ' + op, count if count is not None else 'at least one')
                for i, arg in enumerate(args):
                    expression(arg, path + '/args/' + str(i), names)
            fields = value.get('fields', {})
            if not isinstance(fields, dict):
                issue(path + '/fields', 'object fields must be a dictionary')
            else:
                for name, arg in fields.items():
                    expression(arg, path + '/fields/' + name.replace('~', '~0').replace('/', '~1'), names)
            if 'length' in value and (type(value['length']) is not int or not 1 <= value['length'] <= 64):
                issue(path + '/length', 'hash length must be between 1 and 64')
            if 'safe' in value and not isinstance(value['safe'], str):
                issue(path + '/safe', 'quote safe must be a string')

    def iri_expression(value, path, names):
        expression(value, path, names)
        if not isinstance(value, dict) and not absolute_iri(value):
            issue(path, 'expected an absolute IRI or an IRI-producing expression; a bare node alias is a string constant',
                  {'node': value} if isinstance(value, str) and value in names else 'absolute IRI or {"node":"existing_alias"}')

    if 'schema' not in contract.graphs:
        issue('/graphs/schema', 'schema graph is required')
    for key, prop in contract.properties.items():
        for field in ('domain', 'range'):
            value = getattr(prop, field)
            if value is not None and (field == 'domain' or prop.kind == 'iri') and value not in contract.classes:
                issue('/properties/' + key + '/' + field, 'undefined property ' + field + ': ' + value, list(contract.classes))

    def group(g, path, inherited):
        expression(g.for_each, path + '/for_each', inherited)
        expression(g.when, path + '/when', inherited)
        if g.when is not None and not isinstance(g.when, (bool, dict)):
            issue(path + '/when', 'when must be a boolean or a boolean-producing expression')
        if g.for_each is not None and not isinstance(g.for_each, (dict, list)):
            issue(path + '/for_each', 'for_each must be an array expression or a literal array, not a scalar',
                  {'op': 'coalesce', 'args': [{'path': g.for_each}, []]} if isinstance(g.for_each, str) and g.for_each.startswith('/') else {'path': '/array_field'})
        names = set(inherited)
        for i, node in enumerate(g.nodes):
            where = path + '/nodes/' + str(i)
            if node.id in names:
                issue(where + '/id', 'duplicate node alias: ' + node.id)
            if node.class_id not in contract.classes:
                issue(where + '/class_id', 'undefined class key ' + node.class_id, list(contract.classes))
            if node.graph not in contract.graphs:
                issue(where + '/graph', 'undefined graph key', list(contract.graphs))
            iri_expression(node.iri, where + '/iri', names)
            names.add(node.id)
        for i, triple in enumerate(g.triples):
            where = path + '/triples/' + str(i)
            if triple.subject not in names:
                issue(where + '/subject', 'undefined subject alias', sorted(names))
            if triple.kind == 'iri':
                iri_expression(triple.value, where + '/value', names)
            else:
                expression(triple.value, where + '/value', names)
        for i, child in enumerate(g.children):
            group(child, path + '/children/' + str(i), names)

    for i, mapping in enumerate(contract.mappings):
        path = '/mappings/' + str(i)
        pointer(mapping.selector, path + '/selector')
        group(mapping, path, set())
    if issues:
        raise OntologyError('ONTOLOGY_CONTRACT_INVALID', 'contract', issues)
