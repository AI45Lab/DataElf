from __future__ import annotations

import json
import sys
from pathlib import Path

from dataelf.discovery.artifacts import resolve_workspace_path
from dataelf.discovery.contracts import (
    ArtifactRef,
    OutputArtifactSpec,
    OutputContract,
    StageResult,
)

from .analysis import ANALYSIS, FailureAnalysis, review_analysis
from .config import ConfigurationError, TrajectoryConfig
from .connector import METADATA, RAW, read_json
from .prompt import failure_analysis_prompt


class TrajectoryAnalysisPlugin:
    def __init__(self, config, manifest):
        self.manifest = manifest
        self.config = TrajectoryConfig.from_mapping(config.domain_config(manifest.domain))

    def normalize_spec(self, spec):
        parameters = dict(spec.parameters)
        for key, default in {'reward': 0, 'limit': 1, 'fields': ['chosen_trace']}.items():
            parameters.setdefault(key, default)
        if (set(parameters) != {'reward', 'limit', 'fields'}
                or type(parameters['reward']) not in (int, float) or parameters['reward'] != 0
                or type(parameters['limit']) is not int or parameters['limit'] != 1
                or parameters['fields'] != ['chosen_trace']):
            raise ValueError('TRAJECTORY_PARAMETERS_UNSUPPORTED')
        allowed_inputs = {'fixture_file'} if self.config.mode == 'fixture' else set()
        if set(spec.inputs) - allowed_inputs:
            raise ValueError('TRAJECTORY_INPUTS_UNSUPPORTED')
        if spec.inputs and (not isinstance(spec.inputs.get('fixture_file'), str) or not spec.inputs['fixture_file']):
            raise ValueError('TRAJECTORY_FIXTURE_INPUT_INVALID')
        if set(spec.constraints) - {'max_runtime_minutes'}:
            raise ValueError('TRAJECTORY_CONSTRAINTS_UNSUPPORTED')
        budget = spec.constraints.get('max_runtime_minutes', 30)
        if type(budget) not in (int, float) or not 0 < budget <= 120:
            raise ValueError('TRAJECTORY_RUNTIME_BUDGET_INVALID')
        if spec.requested_outputs not in ([], ['failure_analysis']):
            raise ValueError('TRAJECTORY_OUTPUTS_UNSUPPORTED')
        return spec.model_copy(update={'parameters': parameters,
                                      'requested_outputs': spec.requested_outputs or ['failure_analysis']})

    def prepare(self, spec, workspace_path, config):
        if self.config.modeling.enabled:
            return StageResult(status='failed', error_code='TRAJECTORY_MODELING_UNSUPPORTED',
                               error_message='Trajectory modeling is not implemented.')
        workspace = Path(workspace_path)
        try:
            for relative in self.manifest.workspace_dirs:
                resolve_workspace_path(workspace, relative).mkdir(parents=True, exist_ok=True)
            if self.config.mode == 'fixture':
                source = spec.inputs.get('fixture_file')
                if not source:
                    raise ConfigurationError('fixture_file: required')
                payload = json.loads(Path(source).read_text(encoding='utf-8'))
                target = resolve_workspace_path(workspace, 'raw/trajectory_analysis/fixture_input.json')
                target.write_text(json.dumps(payload, ensure_ascii=False) + '\n', encoding='utf-8')
                return StageResult(status='completed', context={'mode': 'fixture'}, artifacts=[ArtifactRef(
                    artifact_id='trajectory_fixture', kind='synthetic_input', path='raw/trajectory_analysis/fixture_input.json',
                    role='input', producer_stage='domain_prepare', media_type='application/json')])
            env = self.config.tool_environment()
            env.update(DATAELF_TRAJECTORY_CAPTURE='1', DATAELF_TRAJECTORY_PYTHON=sys.executable,
                       DATAELF_TRAJECTORY_SKILL=str(Path(self.config.skill_path).resolve()))
            return StageResult(status='completed', context={'mode': 'tool',
                'client': 'dataelf.domains.trajectory_analysis.client:TrajectoryClient',
                'python_env': 'DATAELF_TRAJECTORY_TOOL_PYTHON',
                'skill_env': 'DATAELF_TRAJECTORY_SKILL'}, env=env)
        except ConfigurationError as exc:
            return StageResult(status='failed', error_code='TRAJECTORY_PREFLIGHT_FAILED', error_message=str(exc))
        except (OSError, ValueError, TypeError):
            return StageResult(status='failed', error_code='TRAJECTORY_PREPARE_FAILED',
                               error_message='Check local input files and workspace containment.')

    def create_modeler(self, spec, config):
        if self.config.modeling.enabled:
            raise ValueError('TRAJECTORY_MODELING_UNSUPPORTED')

    def build_prompt(self, job, context):
        return failure_analysis_prompt()

    def output_contract(self, spec):
        return OutputContract(contract_id='trajectory_analysis.failure_analysis', version='2', artifacts=[
            OutputArtifactSpec(artifact_id=ident, path=path, kind=kind, media_type='application/json', json_root=root)
            for ident, path, kind, root in [
                ('trajectory_raw', RAW, 'bounded_tool_evidence', 'calls'),
                ('trajectory_metadata', METADATA, 'query_metadata', 'calls'),
                ('failure_analysis', ANALYSIS, 'failure_localization_report', None)]])

    def review(self, job, workspace_path):
        return review_analysis(job, Path(workspace_path))

    def result_ids(self, workspace_path):
        try:
            report = FailureAnalysis.model_validate(read_json(Path(workspace_path), ANALYSIS))
            if report.status in {'located', 'insufficient_evidence', 'no_records'}:
                return [report.result_id]
        except (OSError, ValueError, TypeError, KeyError):
            pass
        return []


def create_plugin(config, manifest):
    return TrajectoryAnalysisPlugin(config, manifest)
