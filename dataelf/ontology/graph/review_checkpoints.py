"""Recover actual completed read-only checks, never an inferred verdict."""
import json
from pathlib import Path


def previous_checks(reviews,candidate_hash,source_fingerprint):
    checks=[]
    for folder in sorted(Path(reviews).glob('*'),key=lambda p:p.stat().st_mtime):
        context=folder/'context.json';events=folder/'events.jsonl'
        if not context.is_file() or not events.is_file():continue
        task=json.loads(context.read_text())
        if task.get('candidate_hash')!=candidate_hash or task.get('source_fingerprint')!=source_fingerprint:continue
        calls={}
        for line in events.read_text().splitlines():
            try:event=json.loads(line)
            except ValueError:continue
            if not isinstance(event,dict):continue
            if event.get('type')=='message_end':
                content=event.get('message',{}).get('content',[])
                for call in content if isinstance(content,list) else []:
                    if not isinstance(call,dict):continue
                    if call.get('type')=='toolCall':calls[call['id']]=call
            if event.get('type')!='tool_execution_end' or event.get('isError'):continue
            call=calls.get(event.get('toolCallId'))
            if not call or call['name'] not in {'graph_query','graph_diff','graph_validate','ontology_evidence'}:continue
            args=call.get('arguments',{})
            if isinstance(args,str):
                try:args=json.loads(args)
                except ValueError:continue
            if not isinstance(args,dict):continue
            if call['name']=='ontology_evidence' and args.get('operation')!='replay':continue
            blocks=event.get('result',{}).get('content',[])
            try:result=json.loads(''.join(b.get('text','') for b in blocks if b.get('type')=='text'))
            except ValueError:continue
            if not isinstance(result,dict):continue
            if result.get('truncated') or result.get('snapshot_is_current') is False:continue
            # Remove advisory authoring metadata; keep actual evidence/query
            # results, their arguments and scope. No assistant prose is trusted.
            result={k:v for k,v in result.items() if k not in {'available_tools','workflow','progress_key'}}
            if result.get('encoding')=='json' and not result.get('truncated'):
                try:result['exact_value']=json.loads(result.pop('text'))
                except (ValueError,KeyError):continue
            check={'tool':call['name'],'arguments':args,'result':result}
            if len(json.dumps(check).encode())>64000:continue
            if check not in checks:checks.append(check)
    return checks[-24:]
