"""Intent extraction with finite transient retries under one cancellable deadline."""
from __future__ import annotations

import json
import os
import random
import re
import time
import uuid
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo
from email.utils import parsedate_to_datetime
from pathlib import Path
from dataelf.discovery.run_control import check_cancelled, current_run, RunCancelled
from dataelf.discovery.redaction import redact_text, secret_values
from .transport import perform, MAX_RESPONSE_BYTES

from .profile import Profile, SERVE_PROFILE
from .prompts import build_prompt_components, render_system_prompt
from .config import IntentModelConfig
from .schema import Intent, Output, INTENT_SCHEMA_VERSION
from .layout import parse_sections, has_default_content


class IntentError(RuntimeError):
    """Transport, response or extraction-contract failure; never an empty success."""

    code = "INTENT_MODEL_FAILED"


class IntentRecognizer:
    def __init__(self, profile: Profile = SERVE_PROFILE, *, config: IntentModelConfig | None = None,
                 timeout_seconds: float | None = None, monotonic=time.monotonic,
                 sleeper=time.sleep, jitter=random.random, wall_clock=time.time, opener=None):
        self.profile = profile
        self.config = config.resolve() if config is not None else IntentModelConfig.from_env()
        if timeout_seconds is not None:
            self.config = IntentModelConfig.model_validate({**self.config.model_dump(), "timeout_seconds": timeout_seconds})
        self.config.validate_for_run()
        self.timeout_seconds = self.config.timeout_seconds
        self._clock, self._sleep, self._jitter = monotonic, sleeper, jitter
        self._wall_clock, self._opener = wall_clock, opener
        self.attempts = []
        self.diagnostics = {}

    def extract(self, query: str, *, reference_time: datetime | None = None,
                timezone: str = "Asia/Shanghai") -> Intent:
        self.attempts = []
        started = self._clock()
        self._deadline = started + self.timeout_seconds
        self.diagnostics = {'local_request_id':uuid.uuid4().hex, 'model':self.config.model_name,
            'timeout_budget_seconds':self.timeout_seconds, 'max_attempts':self.config.max_attempts,
            'attempts':self.attempts, 'status':'preparing'}
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a nonblank string")
        zone = ZoneInfo(timezone)
        if reference_time is not None and reference_time.utcoffset() is None:
            raise ValueError("reference_time must include a timezone offset")
        local_time = reference_time.astimezone(zone) if reference_time is not None else datetime.now(zone)
        components = build_prompt_components(self.profile, local_time, timezone)
        sections = parse_sections(query)
        heading_ids = [section["id"] for section in sections[1:]]
        has_heading = bool(heading_ids)
        components["scene_context"]["has_markdown_heading"] = has_heading
        components["scene_context"]["section_headers"] = [{key: section[key] for key in ("id", "title", "level")} for section in sections[1:]]
        schema = self.profile.json_schema()
        if has_heading:
            schema["properties"] = {"_writing_sections": {
                "type": "array", "items": {"type": "string", "enum": heading_ids},
                "description": "先仅按标题语义选择写作/总结等输出要求段落的ID；其他标题不得选，未找到则[]。",
            }, **schema["properties"]}
            schema["required"] = ["_writing_sections", *schema["required"]]
        if not has_heading:
            # Format gate only; heading semantics are still decided by the model.
            schema["$defs"]["Output"]["const"] = Output.unspecified().model_dump()
        payload = {
            "model": self.config.model_name,
            "messages": [
                {"role": "system", "content": render_system_prompt(components)},
                {"role": "user", "content": json.dumps({"input_sections": sections}, ensure_ascii=False) if has_heading else query},
            ],
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "dataelf_intent_v" + INTENT_SCHEMA_VERSION, "strict": True, "schema": schema,
            }},
            "temperature": 0,
            **({"max_tokens":self.config.max_tokens} if self.config.max_tokens is not None else {}),
            "stream": False,
            "thinking": {"type": "disabled"},
            "chat_template_kwargs": {"enable_thinking": False},
        }
        try:
            result = self._validate_response(self._request(payload), sections=sections)
            if self._clock() >= self._deadline:raise IntentError('Intent total deadline exhausted during validation')
            self.diagnostics['status'] = 'completed'
            return result
        except RunCancelled:
            self.diagnostics['status'] = 'cancelled'
            raise
        except IntentError as error:
            self.diagnostics.update(status='failed',error=self._safe_summary(str(error)))
            raise
        finally:
            self.diagnostics['elapsed_seconds'] = max(0,self._clock()-started)
            self._save_diagnostics()

    def _request(self, payload: dict):
        request = urllib.request.Request(self.config.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", **({"Authorization": "Bearer " + self.config.api_key} if self.config.api_key else {})},
            method="POST")
        deadline = getattr(self,'_deadline',self._clock()+self.timeout_seconds)
        self.diagnostics['status'] = 'requesting'
        for index in range(1,self.config.max_attempts+1):
            check_cancelled()
            remaining = deadline-self._clock()
            if remaining <= 0:raise IntentError('Intent total deadline exhausted before request')
            began = self._clock()
            attempt = {'attempt':index,'http_status':None,'request_id':None,'elapsed_seconds':0,
                       'remaining_seconds_at_start':remaining,'status':'requesting'}
            self.attempts.append(attempt)
            self.diagnostics['status'] = 'requesting'
            self._save_diagnostics()
            try:
                response = perform(request,timeout=remaining,deadline=deadline,clock=self._clock,
                                   check_cancelled=check_cancelled,opener=self._opener)
            except RunCancelled:
                attempt['status'] = 'cancelled'
                raise
            except TimeoutError:
                attempt['status'] = 'deadline_exhausted'
                raise IntentError('Intent total deadline exhausted during request') from None
            finally:
                attempt['elapsed_seconds'] = max(0,self._clock()-began)
            if response.get('transport_error'):
                attempt.update(status='transport_failed',error_type=response['transport_error'])
                raise IntentError('Model endpoint request failed or timed out')
            status, body = response['status'], response['body']
            attempt.update(http_status=status,request_id=self._safe_identifier(response.get('request_id')))
            if status >= 400:
                attempt.update(status='http_error',error_summary=self._error_summary(body))
                try:
                    error_document=json.loads(body.decode('utf-8'))
                    error_fields=error_document.get('error',error_document)
                    upstream_id=error_document.get('request_id') or (error_fields.get('request_id') if isinstance(error_fields,dict) else None)
                    attempt['request_id']=attempt['request_id'] or self._safe_identifier(upstream_id)
                except (ValueError,AttributeError):pass
                retryable = status in {429,502,503,504}
                if not retryable or index == self.config.max_attempts:
                    raise IntentError(f'Intent model endpoint returned HTTP {status}')
                delay = self._retry_delay(response.get('retry_after'), index)
                attempt['retry_delay_seconds'] = delay
                if delay >= deadline-self._clock():
                    attempt['retry_stopped'] = 'insufficient_remaining_deadline'
                    raise IntentError(f'Intent HTTP {status}; insufficient remaining deadline for retry')
                self.diagnostics['status'] = 'retry_wait'
                self._save_diagnostics()
                self._wait(delay, deadline)
                continue
            if len(body)>MAX_RESPONSE_BYTES:
                attempt['status']='response_too_large'
                raise IntentError('Model endpoint response exceeds size limit')
            try:
                value=json.loads(body.decode('utf-8'))
            except (ValueError,UnicodeError):
                attempt['status']='invalid_json'
                raise IntentError('Model endpoint returned invalid JSON') from None
            attempt['status']='received'
            if isinstance(value,dict):
                attempt['request_id']=attempt['request_id'] or self._safe_identifier(value.get('request_id') or value.get('id'))
                usage=value.get('usage')
                if isinstance(usage,dict):attempt['usage']={k:v for k,v in usage.items() if k in {'prompt_tokens','completion_tokens','total_tokens'} and type(v) is int and v>=0}
            self._save_diagnostics()
            return value

    def _wait(self, delay, deadline):
        until=self._clock()+delay
        while self._clock()<until:
            check_cancelled()
            remaining=deadline-self._clock()
            if remaining<=0:raise IntentError('Intent total deadline exhausted during retry wait')
            self._sleep(min(0.1,until-self._clock(),remaining))
        check_cancelled()

    def _retry_delay(self, value, attempt):
        if isinstance(value,str):
            if re.fullmatch(r'[0-9]+',value.strip()):
                try:return int(value.strip())
                except ValueError:return self.timeout_seconds+1
            try:
                parsed=parsedate_to_datetime(value)
                if parsed.tzinfo is not None:return max(0,parsed.timestamp()-self._wall_clock())
            except (ValueError,TypeError,OverflowError):pass
        base=self.config.retry_backoff_seconds*2**(attempt-1)
        return base*(1+max(0,min(1,self._jitter())))

    def _safe_summary(self,value):
        secrets=secret_values(os.environ)
        if self.config.api_key:secrets.add(self.config.api_key)
        text=redact_text(value,secrets)
        text=re.sub(r'(?i)Bearer\s+[^\s"<>]+','Bearer <redacted>',text)
        return ' '.join(text.split())[:360]

    def _safe_identifier(self,value):
        return self._safe_summary(value)[:120] if isinstance(value,str) else None

    def _error_summary(self,body):
        try:
            value=json.loads(body.decode('utf-8',errors='replace'))
            error=value.get('error',value)
            if isinstance(error,dict):return self._safe_summary(error.get('message') or error.get('code') or 'upstream HTTP error')
        except (ValueError,AttributeError):pass
        return 'upstream HTTP error (no structured error message)'

    def _save_diagnostics(self):
        self.diagnostics['attempt_count'] = len(self.attempts)
        control=current_run()
        if not control or not control.workspace_path:return
        path=Path(control.workspace_path)/'logs/intent_requests.json'
        try:
            path.parent.mkdir(parents=True,exist_ok=True)
            temp=path.with_suffix('.json.tmp')
            temp.write_text(json.dumps(self.diagnostics,ensure_ascii=False,indent=2)+'\n')
            temp.replace(path)
        except OSError:
            # A logging failure must not turn cancellation or an upstream failure
            # into a different business outcome.
            pass

    def _validate_response(self, envelope, *, sections: list[dict] | None = None) -> Intent:
        try:
            choice = envelope["choices"][0]
            if choice["finish_reason"] != "stop":
                raise IntentError("Model response did not complete normally")
            message = choice["message"]
            if message.get("refusal"):
                raise IntentError("Model refused extraction")
            content = message["content"]
            if not isinstance(content, str):
                raise IntentError("Model response has no JSON text")
            document = json.loads(content)
            if not isinstance(document, dict) or 'research_topics' not in document or 'title_keywords' not in document.get('retrieval', {}):
                raise IntentError('Model response violates the v3 intent contract: missing topic/strict-condition classification')
            writing_ids = []
            if sections and len(sections) > 1:
                document = dict(document)
                writing_ids = document.pop("_writing_sections")
                allowed = {section["id"] for section in sections[1:]}
                if not isinstance(writing_ids, list) or any(not isinstance(item, str) or item not in allowed for item in writing_ids) or len(set(writing_ids)) != len(writing_ids):
                    raise IntentError("Model response violates the section contract")
            result = self.profile.validate(document)
            if sections is not None:
                if not writing_ids:
                    result = result.model_copy(update={"output": Output.unspecified()})
                if not has_default_content(sections, writing_ids):
                    defaults = self.profile.defaults()
                    result = result.model_copy(update={"retrieval": defaults.retrieval, "research_topics": [], "time_range": defaults.time_range, "domains": defaults.domains})
            return result
        except IntentError:
            raise
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise IntentError("Model response violates the intent contract") from None
