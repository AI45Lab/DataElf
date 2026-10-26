"""Thin, role-scoped source tools. The packaged shell script owns HTTP."""
import json
import sys
from pathlib import Path
import jsonschema

from dataelf.ontology.capabilities import skill_review
from dataelf.ontology.resources import json_page
from .evidence import COMMANDS, EvidenceCatalog, SKILL_ROOT

DESCRIPTIONS = {
    'ai_index_command_schema':'按 command 获取来源 API 参数 Schema，包含页码、字段和限制；与保存响应的字节分页不同。',
    'ai_index_query': '按数据源 Skill 的原命令和原参数查询真实 API，登记不可覆盖的响应及批次。凭据由系统注入。幂等重试使用同一 key；新查询或刷新使用新 key。返回成功、空结果或具体失败类别。',
    'ai_index_read': '分页读取已保存的完整响应。offset 是响应 JSON 的字节位置，不是 API 页码；遵循 next_request。',
    'ai_index_sources': '分页读取本任务来源目录，包括失败和空结果、查询时间、筛选条件、source_id、文件及 hash；目录不是查询白名单。',
    'ai_index_skill': '读取本任务数据源 Skill 或相对 references 文档；不提供任意文件或脚本执行权限。',
    'ai_index_register': '登记任务内已有 JSON、JSONL 或 CSV 文件为新的不可覆盖来源副本；原文件不覆盖，保留格式和来源说明。',
}


def schemas(role):
    text = {'type': 'string', 'minLength': 1}
    integer = {'type': 'integer', 'minimum': 0}
    def obj(props, required):
        return {'type': 'object', 'properties': props, 'required': required, 'additionalProperties': False}
    result = {
        'ai_index_command_schema':obj({'command':{'type':'string','enum':list(COMMANDS)}},['command']),
        'ai_index_query': obj({'command': {'type': 'string', 'enum': list(COMMANDS)},
                              'params': {'type': 'object', 'additionalProperties': True,'description':'Command-specific parameters; use ai_index_command_schema(command) for types, API page and field definitions.'},
                              'idempotency_key': text, 'refresh': {'type': 'boolean'}}, ['command', 'params']),
        'ai_index_read': obj({'query_id': text, 'offset': integer,
                             'max_bytes': {'type': 'integer', 'minimum': 256, 'maximum': 16000}}, ['query_id']),
        'ai_index_sources': obj({'offset': integer, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}}, []),
        'ai_index_skill': obj({'path': text, 'offset': integer,
                              'max_bytes': {'type': 'integer', 'minimum': 256, 'maximum': 16000}}, []),
    }
    if not role.endswith('reviewer'):
        result['ai_index_register'] = obj({'path': text}, ['path'])
    return result


def workspace(context):
    return Path(context.get('workspace') or context['request']['workspace']).resolve()


def dispatch(context_path, name, arguments):
    context = json.loads(Path(context_path).read_text())
    if not skill_review(context): raise ValueError('Skill capability not enabled by host')
    permitted = schemas(context['role'])
    if name not in permitted: raise ValueError('Source tool not permitted for this role')
    jsonschema.Draft202012Validator(permitted[name]).validate(arguments)
    role = ('ontology_' if 'request' in context else 'analysis_') + context['role']
    catalog = EvidenceCatalog(workspace(context))
    if name=='ai_index_command_schema':
        from .source_parameters import command_schema
        return {'schema_version':'ai-index-source-params.v1','command':arguments['command'],'schema':command_schema(arguments['command']),
            'field_reference':{'tool':'ai_index_skill','arguments':{'path':'references/api-reference.md'}},'pagination_unit':'api_page_number'}
    if name == 'ai_index_query':
        record = catalog.query(role=role, **arguments)
        return {'query': record, 'registered': record['outcome'].startswith('success_'),
                'next_request': {'tool': 'ai_index_read', 'arguments': {'query_id': record['query_id']}},
                'meaning': '原始响应已保存；成功不证明数据完整，失败不等于没有数据。'}
    if name == 'ai_index_register':
        return {'query': catalog.import_file(arguments['path'], role=role)}
    if name == 'ai_index_read': return catalog.read(**arguments)
    if name == 'ai_index_sources':
        offset = arguments.get('offset', 0); limit = arguments.get('limit', 20)
        records = catalog.records(); catalog.verify(records)
        end = offset + limit
        return {'records': records[offset:end], 'total': len(records),
                'next_request': {'tool': name, 'arguments': {'offset': end, 'limit': limit}} if end < len(records) else None}
    relative = arguments.get('path', 'SKILL.md')
    path = (SKILL_ROOT / relative).resolve()
    if not path.is_relative_to(SKILL_ROOT.resolve()) or path.suffix != '.md' or not path.is_file():
        raise ValueError('Only this Skill and its Markdown references may be read')
    page = json_page(path.read_text(), arguments.get('offset', 0), arguments.get('max_bytes', 8000))
    return {'resource': relative, **page, 'pagination_layer': 'skill_text_json_bytes',
            'next_request': {'tool': name, 'arguments': {**arguments, 'offset': page['next_offset']}} if page['next_offset'] is not None else None}


if __name__ == '__main__':
    try:
        context_path, name = sys.argv[1:3]
        if name == 'direct':
            command = sys.argv[3]
            params = {'institution_id': sys.argv[4]} if command == 'institution-funding' else json.loads(sys.argv[4] if len(sys.argv) > 4 else '{}')
            result = dispatch(context_path, 'ai_index_query', {'command': command, 'params': params})
        else:
            result = dispatch(context_path, name, json.load(sys.stdin))
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        # HTTP stderr and environment values never become model-visible errors.
        from dataelf.discovery.redaction import redact_text, secret_values
        import os
        print(json.dumps({'code': 'AI_INDEX_SOURCE_ERROR', 'message': redact_text(str(exc), secret_values(os.environ))}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
