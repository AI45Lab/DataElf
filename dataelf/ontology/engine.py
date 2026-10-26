"""Deterministic interpreter. All business vocabulary and mappings are inputs."""
from __future__ import annotations

import re
import unicodedata
from typing import Any
from urllib.parse import quote

from rdflib import Dataset, Graph, Literal, RDF, RDFS, URIRef
from rdflib.namespace import OWL, XSD

from dataelf.discovery.run_control import check_cancelled
from .contracts import ExtractionPlan, Group, OntologyContract
from .sources import SourceCatalog, digest, select
from .errors import OntologyError
from .expressions import OPS, CONTEXT_FIELDS, absolute_iri


def expression(value: Any, data: Any, context: dict[str, Any]) -> Any:
    if not isinstance(value, dict):
        return value
    if "path" in value:
        found = select(data, value["path"])
        selected = found[0][1] if found else None
        return None if selected == "" else selected
    if "all" in value:
        return [v for _, v in select(data, value["all"])]
    if "context" in value:
        key = value['context']
        if key not in context and key not in context.get('_context_keys', ()):
            raise ValueError(f'unknown/unavailable context key: {key}; index exists only inside for_each')
        return context.get(value["context"])
    if "node" in value:
        if value["node"] not in context["nodes"]:
            raise ValueError(f"undefined node: {value['node']}")
        return context["nodes"][value["node"]]
    op = value.get("op")
    if op not in OPS:
        raise ValueError(f"unsupported expression operation: {op}")
    if op == "object":
        return {k: expression(v, data, context) for k, v in value.get("fields", {}).items()}
    if op == "if":
        args = value.get("args", [])
        return expression(args[1] if expression(args[0], data, context) else args[2], data, context)
    if op == 'if_null':
        args = value['args']
        first = expression(args[0], data, context)
        return expression(args[1], data, context) if first is None else first
    args = [expression(v, data, context) for v in value.get("args", [])]
    if op == "coalesce":
        return next((v for v in args if v is not None and v != ""), None)
    if op == "is_object":
        return isinstance(args[0], dict)
    if op == 'is_present':
        return args[0] is not None
    if op == 'as_array':
        return [] if args[0] is None else args[0] if isinstance(args[0], list) else [args[0]]
    if any(v is None for v in args):
        return None
    if op == "concat":
        return "".join(map(str, args))
    if op == "string":
        return str(args[0])
    if op == "flatten":
        def flatten(items):
            for item in items:
                if isinstance(item, list):
                    yield from flatten(item)
                else:
                    yield item
        return list(flatten(args[0]))
    if op == "hash":
        return digest(args[0])[:value.get("length", 64)]
    if op == "quote":
        return quote(str(args[0]), safe=value.get("safe", ""))
    if op == "lower":
        return str(args[0]).lower()
    if op == "strip":
        return str(args[0]).strip()
    if op == "normalize":
        return unicodedata.normalize("NFKC", str(args[0])).strip().casefold()
    if op == "add":
        return sum(args)
    if op == "eq":
        return args[0] == args[1]
    raise ValueError(f"unsupported expression: {op}")


def iri(namespace: str, value: str) -> URIRef:
    if value.startswith("xsd:"):
        return URIRef(str(XSD) + value[4:])
    if value.startswith("rdf:"):
        return URIRef(str(RDF) + value[4:])
    if value.startswith("rdfs:"):
        return URIRef(str(RDFS) + value[5:])
    return URIRef(value if ":" in value else namespace + value)


def validate_contract(contract: OntologyContract, sources: set[str]) -> None:
    from .submission_validation import validate_authoring
    validate_authoring(contract)
    if not contract.namespace or ":" not in contract.namespace:
        raise ValueError("namespace must be an absolute IRI")
    if not contract.mappings:
        raise ValueError("contract must define mappings")
    if "schema" not in contract.graphs:
        raise ValueError("schema graph is required")
    for name, definition in contract.classes.items():
        parent = definition.get("subClassOf")
        if parent is not None and (not isinstance(parent, str) or parent not in contract.classes):
            raise ValueError(f"undefined/invalid parent class: {name}")
    ids = [m.id for m in contract.mappings]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate mapping identifiers")
    def check_group(group, inherited):
        names = set(inherited)
        for node in group.nodes:
            if node.id in names or node.class_id not in contract.classes or node.graph not in contract.graphs:
                raise ValueError(f"invalid/duplicate node definition: {node.id}")
            names.add(node.id)
        for triple in group.triples:
            if triple.subject not in names or triple.graph not in contract.graphs:
                raise ValueError("triple references undefined subject or graph")
            prop = contract.properties.get(triple.predicate)
            if not prop and not triple.predicate.startswith(("rdf:", "rdfs:")):
                raise ValueError(f"undefined predicate: {triple.predicate}")
            if prop and prop.kind != triple.kind:
                raise ValueError(f"property kind mismatch: {triple.predicate}")
        for child in group.children:
            check_group(child, names)
    for prop in contract.properties.values():
        if prop.domain and prop.domain not in contract.classes:
            raise ValueError(f"undefined property domain: {prop.domain}")
        if prop.kind == "iri" and prop.range and prop.range not in contract.classes:
            raise ValueError(f"undefined property range: {prop.range}")
    for mapping in contract.mappings:
        if mapping.source not in sources:
            raise ValueError(f"missing input source: {mapping.source}")
        select({}, mapping.selector)
        check_group(mapping, set())
    missing = sources - {m.source for m in contract.mappings}
    if missing:
        raise ValueError(f"unmapped sources: {sorted(missing)}")
    for constraint in contract.constraints:
        if constraint.class_id not in contract.classes or constraint.predicate not in contract.properties:
            raise ValueError("constraint references undefined vocabulary")
        if constraint.max_count is not None and constraint.max_count < constraint.min_count:
            raise ValueError("constraint maximum is less than its minimum")
    for query in contract.queries:
        validate_query(query.query)
        if query.expect is None and query.min_rows is None:
            raise ValueError(f"query must specify expected result: {query.id}")


def validate_query(query: str) -> None:
    from .graph.query import validate_query as parsed_query
    parsed_query(query, construct=False)


def _leaves(value, prefix=""):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _leaves(child, prefix + "/" + key.replace("~", "~0").replace("/", "~1"))
    elif isinstance(value, list):
        for child in value:
            yield from _leaves(child, prefix + "/*")
    else:
        yield prefix


def validate_plan(contract, plan):
    from .submission_validation import pointer_error
    issues = []
    if plan.contract_hash != digest(contract.model_dump(mode="json")):
        issues.append({"path": "/contract_hash", "message": "plan was built for a different contract"})
    mapping_ids = {m.id for m in contract.mappings}
    for key, pointer in plan.selectors.items():
        path = "/selectors/" + key.replace("~", "~0").replace("/", "~1")
        if key not in mapping_ids:
            issues.append({"path": path, "message": "unknown mapping ID; do not use source IDs", "expected": sorted(mapping_ids)})
        if message := pointer_error(pointer):
            issues.append({"path": path, "message": message})
    if plan.contract_revision and plan.contract_revision.target != "contract":
        issues.append({"path": "/contract_revision/target", "message": "must target contract"})
    if issues:
        raise OntologyError("ONTOLOGY_PLAN_INVALID", "plan", issues)


def require_valid_graph(validation, target="contract"):
    if validation["status"] != "valid":
        raise OntologyError("ONTOLOGY_GRAPH_INVALID", target, validation.get("issues") or [
            {"path": "/", "message": message} for message in validation["errors"]])


def preflight_contract(contract, catalog):
    # Full local snapshot, no models and no persisted RDF: also catches rare bad rows.
    _, _, validation = materialize(contract, catalog)
    require_valid_graph(validation)
    return validation


def materialize(contract: OntologyContract, catalog: SourceCatalog, plan: ExtractionPlan | None = None):
    validate_contract(contract, {s.source_id for s in catalog.sources})
    from .submission_validation import validate_authoring
    context_keys = set(CONTEXT_FIELDS) | {key for r in catalog.records for key in r.metadata}
    validate_authoring(contract, context_keys=context_keys)
    catalog.verify()
    if plan:
        validate_plan(contract, plan)
        if plan.contract_revision:
            raise OntologyError("ONTOLOGY_PLAN_INVALID", "plan", [{"path": "/contract_revision", "message": "contract revision must be reviewed before execution"}])
    dataset = Dataset(default_union=True)
    ns = contract.namespace
    graphs = {key: dataset.graph(iri(ns, value)) for key, value in contract.graphs.items()}
    occurrence_ids = {}
    schema = graphs["schema"]
    for name, definition in contract.classes.items():
        subject = iri(ns, name)
        schema.add((subject, RDF.type, OWL.Class))
        schema.add((subject, RDFS.label, Literal(definition.get("label") or name)))
        if definition.get("comment"):
            schema.add((subject, RDFS.comment, Literal(definition["comment"])))
        if definition.get("subClassOf"):
            if definition["subClassOf"] not in contract.classes:
                raise ValueError(f"undefined parent class: {name}")
            schema.add((subject, RDFS.subClassOf, iri(ns, definition["subClassOf"])))
    for name, definition in contract.properties.items():
        if not definition.emit_schema:
            continue
        subject = iri(ns, name)
        schema.add((subject, RDF.type, OWL.ObjectProperty if definition.kind == "iri" else OWL.DatatypeProperty))
        if definition.domain:
            schema.add((subject, RDFS.domain, iri(ns, definition.domain)))
        if definition.range:
            schema.add((subject, RDFS.range, iri(ns, definition.range)))
    lineage, conflicts, fact_lineage, identities = [], [], [], {}
    skipped_groups = []
    values: dict[tuple, set] = {}
    policies: dict[tuple, str] = {}

    def execute(group: Group, data, context, emitted, path, selection_path=None, indices=()):
        active_indices = indices
        def fail(field, message, expected=None):
            issue = {"path": selection_path or field, "contract_path": field, "message": message,
                     "evidence": [context["file"] + "#" + context["pointer"]], "array_indices": list(active_indices)}
            if expected is not None:
                issue["expected"] = expected
            raise OntologyError("ONTOLOGY_EXECUTION_INVALID", "plan" if selection_path else "contract", [issue])

        def evaluate(value, field):
            try:
                return expression(value, data, context)
            except (ValueError, TypeError, IndexError, KeyError) as exc:
                fail(field, str(exc))

        iterations = evaluate(group.for_each, path + "/for_each") if group.for_each is not None else [data]
        if iterations is None:
            return
        if not isinstance(iterations, list):
            fail(path + "/for_each", "for_each must resolve to an array; got " + type(iterations).__name__, '{"path":"/calls"} or {"all":"/calls/*"}; optional array: {"op":"coalesce","args":[{"path":"/calls"},[]]}')
        for index, current in enumerate(iterations):
            check_cancelled()
            active_indices = (*indices, index) if group.for_each is not None else indices
            if group.for_each is not None and (current is None or current == ""):
                continue
            local = {**context, "nodes": dict(context["nodes"])}
            if group.for_each is not None:
                local["index"] = index
            def evaluate_current(value, field):
                try:
                    return expression(value, current, local)
                except (ValueError, TypeError, IndexError, KeyError) as exc:
                    fail(field, str(exc))

            if group.when is not None:
                condition = evaluate_current(group.when, path + '/when')
                if type(condition) is not bool:
                    fail(path + '/when', 'when must evaluate to a boolean; got ' + type(condition).__name__)
                if not condition:
                    skipped_groups.append({'contract_path': path + '/when', 'source': local['source'],
                        'file': local['file'], 'pointer': local['pointer'], 'array_indices': list(active_indices)})
                    continue

            for node_index, node in enumerate(group.nodes):
                field = path + f"/nodes/{node_index}/iri"
                identifier = evaluate_current(node.iri, field)
                if not isinstance(identifier, str) or not identifier or ":" not in identifier:
                    fail(field, f"missing/invalid identity for {node.id}", "expression returning an absolute IRI")
                if not absolute_iri(identifier):
                    fail(field, f"invalid IRI for {node.id}")
                if node.identity_scope == 'occurrence' and group.for_each is not None:
                    scope = (path, node.id, context['file'], context['pointer'], tuple(indices))
                    seen = occurrence_ids.setdefault(scope, set())
                    if identifier in seen:
                        fail(field, 'duplicate occurrence identity; use inherited parent node and context.index')
                    seen.add(identifier)
                local["nodes"][node.id] = URIRef(identifier)
                if node.identity_scope=='occurrence':
                    identities[identifier]={'scope':'occurrence','record_hash':digest(current),'mapping':path,
                        'file':local['file'],'pointer':local['pointer']}
                graphs[node.graph].add((URIRef(identifier), RDF.type, iri(ns, node.class_id)))
                emitted.add(identifier)
                from .graph.contracts import Quad
                fact_lineage.append({"quad":Quad.from_nodes(URIRef(identifier),RDF.type,iri(ns,node.class_id),graphs[node.graph].identifier).model_dump(mode="json"),"source":local["source"],"file":local["file"],"pointer":local["pointer"],"mapping":path.split("/children/")[0]})
            for triple_index, triple in enumerate(group.triples):
                field = path + f"/triples/{triple_index}/value"
                value = evaluate_current(triple.value, field)
                if value is None or value == "":
                    if triple.required:
                        fail(field, f"missing required {triple.predicate}")
                    continue
                if isinstance(value, (list, dict)):
                    fail(field, f"non-scalar value for {triple.predicate}; use for_each")
                if triple.kind == "iri":
                    if not absolute_iri(value):
                        fail(field, f"invalid object IRI: {value}", 'use {"node":"alias"} for node references')
                    obj = URIRef(value)
                else:
                    datatype = triple.datatype if triple.datatype is not None else (contract.properties.get(triple.predicate).range if triple.predicate in contract.properties else None)
                    obj = Literal(value, datatype=iri(ns, datatype) if datatype else None)
                    if getattr(obj, "ill_typed", False):
                        fail(field, f"invalid {datatype} value for {triple.predicate}: {value!r}")
                subject, predicate = local["nodes"][triple.subject], iri(ns, triple.predicate)
                key = (triple.graph, subject, predicate)
                previous_policy = policies.setdefault(key, triple.conflict)
                if previous_policy != triple.conflict:
                    fail(path + f"/triples/{triple_index}/conflict", f"inconsistent conflict policy: {triple.predicate}")
                previous = values.setdefault(key, set())
                previous.add(obj)
                if len(previous) > 1 and triple.conflict == "error":
                    fail(field, f"conflicting values for {subject} {predicate}")
                if len(previous) > 1 and triple.conflict == "consensus":
                    graphs[triple.graph].remove((subject, predicate, None))
                    conflicts.append({"subject": str(subject), "predicate": str(predicate), "values": sorted(map(str, previous))})
                else:
                    graphs[triple.graph].add((subject, predicate, obj))
                from .graph.contracts import Quad
                fact_lineage.append({"quad": Quad.from_nodes(subject,predicate,obj,graphs[triple.graph].identifier).model_dump(mode="json"), "source":local["source"], "file":local["file"], "pointer":local["pointer"], "mapping":path.split("/children/")[0]})
            for child_index, child in enumerate(group.children):
                execute(child, current, local, emitted, path + f"/children/{child_index}", selection_path,
                        (*indices, index) if group.for_each is not None else indices)

    for mapping_index, mapping in enumerate(contract.mappings):
        matched = 0
        overridden = bool(plan and mapping.id in plan.selectors and plan.selectors[mapping.id] != mapping.selector)
        selection_path = "/selectors/" + mapping.id.replace("~", "~0").replace("/", "~1") if overridden else None
        for rank, record in enumerate((r for r in catalog.records if r.source == mapping.source), start=1):
            check_cancelled()
            selector = plan.selectors.get(mapping.id, mapping.selector) if plan else mapping.selector
            for suffix, data in select(record.data, selector):
                matched += 1
                context = {**record.metadata, "namespace": ns, "source": record.source,
                           "file": record.file, "pointer": record.pointer + suffix,
                           "record_hash": digest(data), "file_hash": catalog.hashes[record.file], "rank": rank,
                           '_context_keys': context_keys - {'index'}, "nodes": {}}
                context.pop('index',None)  # Reserved for actual for_each iteration.
                emitted: set[str] = set()
                execute(mapping, data, context, emitted, f"/mappings/{mapping_index}", selection_path)
                lineage.append({"mapping": mapping.id, "source": record.source, "file": record.file,
                                "pointer": record.pointer + suffix, "recordHash": digest(data),
                                "nodes": sorted(emitted)})
        if matched < mapping.min_records:
            raise OntologyError("ONTOLOGY_SELECTION_INVALID", "plan" if overridden else "contract", [{"path": selection_path or f"/mappings/{mapping_index}/selector", "message": f"mapping selects too few records: {mapping.id}"}])
    errors, queries, issues = validate_dataset(dataset, contract)
    if not lineage:
        errors.append("no records materialized")
        issues.append({"path": "/mappings", "message": "no records materialized"})
    # Every input must participate; physical plans cannot silently drop rows.
    for record in catalog.records:
        if not any(row["source"] == record.source and row["file"] == record.file and
                   (row["pointer"] == record.pointer or row["pointer"].startswith(record.pointer + "/")) for row in lineage):
            message = f"uncovered input record: {record.file}#{record.pointer}"
            errors.append(message)
            issues.append({"path": "/mappings", "message": message, "evidence": [record.file + "#" + record.pointer]})
    used_paths: dict[str, set[str]] = {}
    def paths(value, prefix, found):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"path", "all"} and isinstance(child, str):
                    if prefix + child:
                        found.add(prefix + child)
                else:
                    paths(child, prefix, found)
        elif isinstance(value, list):
            for child in value:
                paths(child, prefix, found)
    def group_paths(group, prefix, found):
        if group.for_each is not None:
            selectors = set()
            paths(group.for_each, "", selectors)
            if selectors:
                selected = sorted(selectors)[0]
                prefix += selected + ("" if selected.endswith("/*") else "/*")
        paths(group.when, prefix, found)
        for item in [*group.nodes, *group.triples]:
            paths(item.model_dump(mode="json"), prefix, found)
        for child in group.children:
            group_paths(child, prefix, found)
    for mapping in contract.mappings:
        group_paths(mapping, mapping.selector, used_paths.setdefault(mapping.source, set()))
    unmapped = {source: sorted({p for r in catalog.records if r.source == source for p in _leaves(r.data)
                              if not any(p == q or p.startswith(q + "/") for q in used_paths.get(source, set()))})
                for source in {r.source for r in catalog.records}}
    return dataset, {"records": lineage, "facts":fact_lineage, "identities":identities,"conflicts": conflicts, "unmappedFields": unmapped,
                     **({'skippedGroups': skipped_groups} if skipped_groups else {})}, {
        "status": "invalid" if errors else "valid", "errors": errors, "queries": queries, "issues": issues,
        "recordCount": len(lineage), "quadCount": sum(len(g) for g in dataset.graphs())}


def validate_dataset(dataset: Dataset, contract: OntologyContract):
    union = Graph()
    for graph in dataset.graphs():
        for triple in graph:
            union.add(triple)
    ns = contract.namespace
    parents={iri(ns,k):iri(ns,v['subClassOf']) for k,v in contract.classes.items() if v.get('subClassOf')}
    def types(subject,controlled=False):
        found = set(union.objects(subject, RDF.type))
        pending = list(found)
        while pending:
            child=pending.pop()
            for parent in [*([] if controlled else union.objects(child,RDFS.subClassOf)),*([parents[child]] if child in parents else [])]:
                if parent not in found:
                    found.add(parent)
                    pending.append(parent)
        return found
    errors, issues = [], []
    property_keys = {iri(ns, key): key.replace("~", "~0").replace("/", "~1") for key in contract.properties}
    def invalid(path, message):
        errors.append(message)
        issues.append({"path": path, "message": message,"scope":"graph"})
    schema_id = iri(ns, contract.graphs["schema"])
    declarations={iri(ns,key):(key,definition) for key,definition in contract.properties.items()}
    for subject, predicate, obj, graph in dataset.quads((None, None, None, None)):
        if predicate in declarations:
            key,definition=declarations[predicate];path='/properties/'+property_keys[predicate]
            if graph!=schema_id and definition.domain and iri(ns,definition.domain) not in types(subject,True):
                invalid(path+'/domain',f'contract domain mismatch: {subject} {predicate}')
            if definition.kind=='iri':
                # V1 executable nodes and relation values are stable IRIs.
                # Blank nodes remain legal RDF for undeclared/imported predicates.
                if not isinstance(obj,URIRef):invalid(path+'/kind',f'IRI property requires an IRI object: {predicate} {obj}')
                elif graph!=schema_id and definition.range and iri(ns,definition.range) not in types(obj,True):
                    invalid(path+'/range',f'contract range mismatch: {predicate} {obj}')
            elif not isinstance(obj,Literal):invalid(path+'/kind',f'literal property requires a literal object: {predicate} {obj}')
            elif definition.range:
                expected=iri(ns,definition.range)
                actual=obj.datatype or (RDF.langString if obj.language else XSD.string)
                if actual!=expected:invalid(path+'/range',f'contract datatype mismatch: {predicate}: expected {expected}, got {actual}')
            if isinstance(obj,Literal) and getattr(obj,'ill_typed',False):invalid(path+'/range',f'ill-typed literal: {predicate} {obj}')
        if predicate==RDF.type and not isinstance(obj,URIRef):invalid('/classes',f'rdf:type requires an IRI class: {subject}')
        if graph==schema_id:continue
        for domain in union.objects(predicate, RDFS.domain):
            if domain not in types(subject):
                invalid("/properties/" + property_keys.get(predicate, str(predicate)) + "/domain", f"domain mismatch: {subject} {predicate}")
        if isinstance(obj, URIRef):
            for target in union.objects(predicate, RDFS.range):
                if target not in types(obj):
                    invalid("/properties/" + property_keys.get(predicate, str(predicate)) + "/range", f"range mismatch: {predicate} {obj}")
    for index, constraint in enumerate(contract.constraints):
        for subject in set(union.subjects(RDF.type, None)):
            if iri(ns, constraint.class_id) not in types(subject,True):
                continue
            count = len(set(union.objects(subject, iri(ns, constraint.predicate))))
            if count < constraint.min_count or (constraint.max_count is not None and count > constraint.max_count):
                invalid(f"/constraints/{index}", f"cardinality mismatch: {subject} {constraint.predicate}: {count}")
    queries = []
    for index, check in enumerate(contract.queries):
        validate_query(check.query)
        from .graph.query import execute as bounded_query
        if check.min_rows is not None and check.min_rows>1000:
            raise ValueError('validation SELECT min_rows exceeds the bounded 1000-row budget; use an aggregate ASK check')
        result=bounded_query(dataset,check.query,limit=max(1,check.min_rows or 1))
        if (result['type']=='ASK' and (check.expect is None or check.min_rows is not None)) or (result['type']=='SELECT' and (check.min_rows is None or check.expect is not None)):
            raise ValueError(f"ASK requires expect; SELECT requires min_rows: {check.id}")
        answer=result['answer'] if result['type']=='ASK' else len(result['rows'])
        valid = (check.expect is None or answer == check.expect) and (check.min_rows is None or answer >= check.min_rows)
        queries.append({"id": check.id, "result": answer, "passed": valid})
        if not valid:
            invalid(f"/queries/{index}", f"query check failed: {check.id}")
    return sorted(set(errors)), queries, issues
