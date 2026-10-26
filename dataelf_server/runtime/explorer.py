"""Service-owned execution options and model-channel diagnostics."""
import json
from pathlib import Path
from .pi_explorer import ServerPiProcess


class ServerPiExplorer(ServerPiProcess):
    execution_policy = 'bounded'

    def _execution_timeout(self, job):
        if self.execution_policy == 'unbounded':
            return None
        return super()._execution_timeout(job)

    def _build_env(self, workspace, job, context):
        env = super()._build_env(workspace, job, context)
        env['DATAELF_SERVER_LOG_PREFIX'] = self.log_prefix
        return env

    def run(self, job, context):
        result = super().run(job, context)
        if result.error_code == 'RUN_CANCELLED':
            return result
        logs = Path(context.workspace_path)/'logs'
        error = logs/(self.log_prefix+'_tool_channel_error.json')
        if error.exists():
            detail = json.loads(error.read_text())
            code = 'PI_OUTPUT_TRUNCATED' if detail['stop_reason'] == 'length' else detail.get('code','PI_MODEL_ERROR')
            return result.model_copy(update={'status':'failed', 'error_code':code, 'error_message':detail['message']})
        events = logs/(self.log_prefix+'_events.jsonl')
        last = None
        if events.exists():
            for line in events.open():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get('type') == 'message_end' and event.get('message',{}).get('role') == 'assistant':
                    last = event['message'].get('stopReason')
        if last == 'length':
            return result.model_copy(update={'status':'failed', 'error_code':'PI_OUTPUT_TRUNCATED',
                'error_message':'Server model output was truncated; no incomplete call was executed.'})
        return result
