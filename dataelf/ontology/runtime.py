"""Fresh Pi CLI sessions using the same process/model infrastructure as Explorer."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from dataelf.config import DataElfConfig
from dataelf.discovery.pi_runtime import configured_binary, pi_cwd, pi_agent_dir, pi_cache_dir, managed_pi_resources, is_managed_binary
from dataelf.discovery.pi_cli_explorer import _run_pi_process, _ENV_ALLOWLIST, _model_event_errors
from dataelf.discovery.run_control import RunCancelled
from .contracts import OntologyContract, ExtractionPlan, Review, SourceInput
from .sources import write_json
from .guidance import authoring_guidance, task_message
from .tool_contracts import ADAPTERS, ComponentAction
from .expressions import CONTEXT_FIELDS, contract_schema
from .sources import SourceCatalog
from .wire_schema import provider_schema


def compact_contract_schema(schema):
    """Transport only the root shape; the full strict schema is available on demand.

    Submission still runs OntologyContract, expression preflight and graph checks.
    Avoid repeating the execution language in every provider tool declaration.
    """
    out={'type':'object','additionalProperties':False,'required':schema.get('required',[]),'properties':{}}
    for name,value in schema['properties'].items():
        if name in ('classes','properties','graphs'):
            out['properties'][name]={'type':'object','additionalProperties':True}
        elif name in ('mappings','constraints','queries'):
            out['properties'][name]={'type':'array','items':{'type':'object','additionalProperties':True}}
        else:out['properties'][name]=value
    return out


class PiOntologyAgent:
    def __init__(self, config: DataElfConfig | None = None, *, runtime_env=None, agent_resources=None):
        self.config = config or DataElfConfig.from_env()
        self.runtime_env = runtime_env or {}
        self.agent_resources = agent_resources

    def __call__(self, role, context, directory: Path, role_config):
        binary = configured_binary(self.config)
        if binary is None:
            raise RuntimeError("Pi runtime is unavailable; run dataelf setup")
        directory.mkdir(parents=True, exist_ok=True)
        context_path = directory / "context.json"
        context={**context,'role':role,'authoring_protocol':'autonomous',
            'control_limits':role_config.model_dump(mode='json')}
        unbounded=context.get('execution_policy')=='unbounded'
        if unbounded:
            context.update(session_deadline_monotonic=None,deadline_monotonic=None,max_llm_requests=None,max_llm_tokens=None,delivery={**context.get('delivery',{}),'enabled':False,'component_delivery_boundary':False})
        else:context.setdefault('session_deadline_monotonic',time.monotonic()+role_config.timeout_seconds)
        if role.endswith('reviewer') and context.get('candidate_hash'):
            from .graph.review_checkpoints import previous_checks
            context['review_checks']=previous_checks(directory.parent,context['candidate_hash'],context['source_fingerprint'])
        write_json(context_path,context)
        if context.get("graph_ref"):
            from .graph.tools import write_context
            write_context(context_path,context)
        from .control import start_session
        start_session(context_path,context)
        schema = Review if role.endswith("reviewer") else OntologyContract
        from .task_sources import task_catalog
        _,catalog = task_catalog(context)
        context_keys = set(CONTEXT_FIELDS) | {k for r in catalog.records for k in r.metadata}
        wire_schema = schema.model_json_schema()
        from .capabilities import skill_review
        if skill_review(context) and schema is Review:
            issue=wire_schema.get('$defs',{}).get('ReviewIssue')
            if issue:
                issue['required']=list(dict.fromkeys([*issue.get('required',[]),'nature','blocking','issue_id','location','impact']))
                for field in ['issue_id','location','impact']:issue['properties'][field]={'type':'string','minLength':1}
        elif schema is Review:
            definitions=wire_schema.get('$defs',{})
            definitions.get('ReviewIssue',{}).get('properties',{}).pop('nature',None)
            status=definitions.get('ReviewUpdate',{}).get('properties',{}).get('status',{})
            if 'enum' in status:status['enum']=[v for v in status['enum'] if v!='withdrawn']
        if skill_review(context) and schema is Review:
            from .review_records import reference_schema
            wire_schema=reference_schema(wire_schema,checks=False)
        if schema is OntologyContract:
            wire_schema = contract_schema(wire_schema,context_keys=context_keys)
        write_json(directory / "schema.json", wire_schema)
        write_json(directory / "tool_schema.json", provider_schema(compact_contract_schema(wire_schema) if schema is OntologyContract else wire_schema))
        tool_schemas={name: provider_schema({'type':'object',**adapter.json_schema()}) for name,adapter in ADAPTERS.items()}
        from pydantic import TypeAdapter
        from .component_tools import schemas as component_schemas
        tool_schemas.update(component_schemas())
        from .capabilities import skill_review
        if skill_review(context):
            from dataelf.domains.ai_index.skill_tools import schemas as source_schemas
            tool_schemas.update(source_schemas(role))
            from .collaboration_tools import schemas as collaboration_schemas
            tool_schemas.update(collaboration_schemas(context))
        tool_schemas['ontology_author_basic']=provider_schema(TypeAdapter(ComponentAction).json_schema())
        trial_schema=ADAPTERS['ontology_execute'].json_schema()
        tool_schemas['ontology_execute']={'type':'object','properties':{
            **{k:v for k,v in trial_schema['properties'].items() if k!='contract'},
            'contract':{'type':'object','additionalProperties':True}},'additionalProperties':False}
        # Remove the compatibility-only field-index alias from provider exposure.
        def remove_profile_alias(value):
            if isinstance(value,dict):
                if value.get('properties',{}).get('operation',{}).get('const')=='profile':value['properties'].pop('offset',None)
                for child in value.values():remove_profile_alias(child)
            elif isinstance(value,list):
                for child in value:remove_profile_alias(child)
        remove_profile_alias(tool_schemas['ontology_evidence'])
        write_json(directory / "tool_schemas.json",tool_schemas)
        (directory / "authoring.md").write_text(authoring_guidance(role, schema.model_json_schema(), role_config.max_submission_failures,author_interface=role_config.author_interface,execution_policy=context.get('execution_policy','bounded')), encoding="utf-8")
        prompt_path = directory / "prompt.md"
        prompt_path.write_text(task_message(role), encoding="utf-8")
        if skill_review(context):
            from dataelf.domains.ai_index.role_prompts import compose,input_manifest,descriptions
            from .tools import available_tools,dispatch
            summary=dispatch(context_path,'ontology_inspect',{})
            from .recovery import compact_artifacts
            composed={**context,'workspace':context['request']['workspace'],
                'live_artifacts':compact_artifacts(context_path,context), 'source_summary':summary.get('sources')}
            if role.endswith('reviewer'):
                composed['live_artifacts']['working_draft']['read']={'tool':'ontology_inspect','arguments':{'resource':'draft'}}
            role_name='ontology_reviewer' if role.endswith('reviewer') else 'ontology_author'
            active=available_tools(context,directory)
            system,task=compose(role_name,composed,active)
            (directory/'authoring.md').write_text(system)
            prompt_path.write_text(task)
            write_json(directory/'composed_input.json',input_manifest(role_name,system,task,{n:tool_schemas.get(n) for n in active},context))
            write_json(directory/'tool_descriptions.json',descriptions())
        from .guidance import PROMPT_VERSION
        from .sources import digest
        write_json(directory/'input_manifest.json',{'prompt_version':PROMPT_VERSION,
            'guidance_sha256':__import__('hashlib').sha256((directory/'authoring.md').read_bytes()).hexdigest(),
            'task_sha256':__import__('hashlib').sha256(prompt_path.read_bytes()).hexdigest(),
            'tool_schemas_hash':digest(tool_schemas),'contract_schema_hash':digest(wire_schema),
            'role_config_hash':digest(role_config.model_dump(mode='json')),
            'composition':'Pi before_agent_start REPLACES systemPrompt with authoring.md plus CURRENT TASK SUMMARY. before_provider_request replaces visible schemas and injects budget/events; these hashes alone are not the final wire payload.'})
        if skill_review(context):
            manifest=json.loads((directory/'composed_input.json').read_text())
            write_json(directory/'input_manifest.json',{**manifest,
                'composition':'Single Chinese composer; actual provider-bound input recorded by inputs.ts after role extensions. Transport wire capture remains a separate boundary.'})
        env = {key: value for key, value in os.environ.items() if key in _ENV_ALLOWLIST}
        env.update(self.config.env)
        env.update(self.runtime_env)
        from .capabilities import network_environment
        for key,value in network_environment(context).items():env.setdefault(key,value)
        from .capabilities import skill_review
        if skill_review(context):
            env['DATAELF_AI_INDEX_QUERY_CONTEXT']=str(context_path)
            env['DATAELF_WORKFLOW_POLICY']=context['request']['workflow_policy']
        env["PYTHONDONTWRITEBYTECODE"]="1"
        env.setdefault("PI_CODING_AGENT_DIR", str(pi_agent_dir(self.config)))
        env.setdefault("NPM_CONFIG_CACHE", str(pi_cache_dir(self.config)))
        env.setdefault("PI_SKIP_VERSION_CHECK", "1")
        env.setdefault("PI_TELEMETRY", "0")
        env.update(DATAELF_ONTOLOGY_CONTEXT=str(context_path), DATAELF_PYTHON=sys.executable,
                   DATAELF_ONTOLOGY_MAX_SUBMISSION_FAILURES=str(role_config.max_submission_failures),
                   DATAELF_ONTOLOGY_MAX_FORMAT_FAILURES=str(role_config.max_format_failures),
                   DATAELF_ONTOLOGY_MAX_IDENTICAL_FAILURES=str(role_config.max_identical_failures))
        root = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        command = [str(binary), "--thinking", "off", "--mode", "json", "--no-session", "--approve", "--no-extensions", "--no-skills"]
        if skill_review(context):
            from dataelf.domains.ai_index.evidence import SKILL_ROOT
            skills=list(dict.fromkeys([*(self.agent_resources.skills if self.agent_resources else []),SKILL_ROOT/'SKILL.md']))
            for skill in skills:command.extend(['--skill',str(skill)])
        from .tools import available_tools
        # CLI --tools is a hard execution allowlist, not merely prompt exposure.
        # Keep all role-permitted tools executable; the extension advertises the
        # compact default subset and exposes advanced schemas on demand.
        active = available_tools(context,directory,all_tools=True)
        command.extend(["--tools", ",".join(active)])
        cwd = pi_cwd(self.config)
        # Provider registration is shared; ontology tools remain an explicit allowlist.
        if is_managed_binary(binary):
            resources = managed_pi_resources(cwd, env, required_packages=("@quarkos/pi-fusion",))
            for extension in resources.extensions:
                command.extend(["--extension", str(extension)])
        command.extend(["--extension", str(Path(__file__).with_name("runtime") / "ontology.ts")])
        command.extend(["--extension", str(Path(__file__).with_name("runtime") / "graph.ts")])
        if skill_review(context):command.extend(['--extension',str(Path(__file__).with_name('runtime')/'inputs.ts')])
        env["DATAELF_GRAPH_CONTEXT"]=str(context_path)
        model = role_config.model or self.config.explorer.pi.model
        if model:
            command.extend(["--model", model])
        command.extend([f"@{prompt_path}", "执行当前角色任务，并通过实际工具保存或交接结果。" if skill_review(context) else "Perform your ontology role and submit the structured result."])
        started=time.monotonic()
        try:
            options={'stop_files':(directory/'control_failure.json',directory/'submission.json',
                *((directory/'handoff.json',) if skill_review(context) else ()))} if context.get('delivery',{}).get('enabled') or unbounded else {}
            result = _run_pi_process(command, cwd=cwd, env=env, timeout=None if unbounded else role_config.timeout_seconds,
                                     log_mode=self.config.explorer.pi.log_mode, compact_json_events=True,**options)
        except (subprocess.TimeoutExpired, RunCancelled, KeyboardInterrupt) as exc:
            def output_text(value):
                return value.decode('utf-8',errors='replace') if isinstance(value,bytes) else value or ''
            (directory / "events.jsonl").write_text(output_text(getattr(exc, "stdout", None) or getattr(exc, "output", None)), encoding="utf-8")
            (directory / "stderr.log").write_text(output_text(getattr(exc, "stderr", None)), encoding="utf-8")
            from .control import record_interruption
            record_interruption(context_path,context)
            raise
        finally:
            from .control import record_duration
            record_duration(context_path,context,time.monotonic()-started)
        (directory / "events.jsonl").write_text(result.stdout, encoding="utf-8")
        (directory / "stderr.log").write_text(result.stderr, encoding="utf-8")
        if skill_review(context) and (directory/'handoff.json').exists():
            from .control import record_interruption
            record_interruption(context_path,context)
            from .collaboration import ReviewHandoff,ReviewLedger
            marker=json.loads((directory/'handoff.json').read_text())
            if marker['kind']=='local_feedback':
                return json.loads((directory/'local_feedback.json').read_text())
            raise ReviewHandoff(ReviewLedger(context['request']['workspace'],marker['family']).load(marker['request_id']))
        control_failure=directory/'control_failure.json'
        output=directory/'submission.json'
        from .control import control_path
        state=json.loads(control_path(context_path,context).read_text())
        completed=state.get('roles',{}).get(role,{}).get('completed',False)
        detail=json.loads(control_failure.read_text()) if control_failure.exists() else {}
        if output.exists() and (completed or detail.get('code')=='ONTOLOGY_CLOSING_INTERRUPT' or context.get('delivery',{}).get('enabled') or unbounded):
            value=schema.model_validate_json(output.read_text()).model_dump(mode='json')
            if detail or context.get('delivery',{}).get('enabled') or unbounded:
                from .control import record_interruption
                record_interruption(context_path,context)
                from .control import control_event,budget_status,failure
                control_event(context_path,{'event':'tool','submitted':True})
                usage=budget_status(context_path,context)
                if context.get('max_llm_tokens') is not None and (usage['knownTokens']>context['max_llm_tokens'] or usage['usageUnknownRequests']):
                    failure('ONTOLOGY_TOKEN_BUDGET_EXHAUSTED','Submitted artifact retained; total usage exceeded or cannot be measured within the configured token budget')
            return value
        if control_failure.exists():
            from .errors import OntologyError
            from .control import record_interruption
            record_interruption(context_path,context)
            raise OntologyError(detail['code'],detail.get('target','control'),detail.get('issues',[]))
        failure = directory / "submission_failure.json"
        if failure.is_file():
            from .errors import OntologyError
            detail = json.loads(failure.read_text())
            if detail.get('target') == 'format':
                raise OntologyError('ONTOLOGY_FORMAT_BUDGET_EXHAUSTED','format',[detail])
            raise ValueError(f"ONTOLOGY_SUBMISSION_BUDGET_EXHAUSTED: {failure.read_text(encoding='utf-8')}")
        errors = _model_event_errors(result.stdout)
        if errors:
            from .errors import classify_model_error
            raise classify_model_error(errors,role)
        if result.returncode:raise RuntimeError(f"Pi {role} exited with status {result.returncode}; see {directory}")
        from .errors import OntologyError
        raise OntologyError('ONTOLOGY_NO_SUBMISSION','control',[{'message':'Session ended without a validated submission; prose is not a published artifact','recoverable':True}])
