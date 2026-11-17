"""Raw stream integrity must survive removal/reordering of display-only blocks."""
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("fault", ["none", "truncated", "length", "error", "aborted", "unknown_id", "missing_end", "array", "empty"])
def test_native_calls_bind_raw_json_to_call_id_after_context_filter(fault):
    script = r'''
import {installNativeCallGuard} from './dataelf/ontology/runtime/native_calls.ts';
import {withoutThinking} from './dataelf/discovery/runtime/thinking.ts';
const handlers=new Map(), failures=[];
const pi={on:(name,fn)=>{const list=handlers.get(name)||[];list.push(fn);handlers.set(name,list);}};
// Same ordering as production: compatibility hook is registered before guard.
pi.on('message_end',event=>({message:withoutThinking(event.message)}));
installNativeCallGuard(pi,detail=>failures.push(detail));
async function emit(name,event){let result;for(const fn of handlers.get(name)||[]){const value=await fn(event,{});if(value?.message)event={...event,message:value.message};if(value?.block)result=value;}return result;}
const fault=process.argv[1];
await emit('message_start',{message:{role:'assistant'}});
const args=[{resource:'contract'},{resource:'status'},{limit:100}];
for(let n=0;n<3;n++){
 const index=n+2;
 await emit('message_update',{assistantMessageEvent:{type:'toolcall_start',contentIndex:index}});
 let raw=JSON.stringify(args[n]);
 if(n===0){if(fault==='truncated')raw=raw.slice(0,-1);if(fault==='array')raw='[]';if(fault==='empty')raw='';}
 for(const delta of [raw.slice(0,4),raw.slice(4)])await emit('message_update',{assistantMessageEvent:{type:'toolcall_delta',contentIndex:index,delta}});
 if(fault!=='missing_end')await emit('message_update',{assistantMessageEvent:{type:'toolcall_end',contentIndex:index,toolCall:{id:'c'+n}}});
}
await emit('message_end',{message:{role:'assistant',api:'openai-completions',stopReason:['length','error','aborted'].includes(fault)?fault:'toolUse',content:[
 {type:'thinking',thinking:'not public'}, {type:'text',text:'<think>also filtered</think>'},
 ...args.map((arguments_,n)=>({type:'toolCall',id:fault==='unknown_id'&&n===0?'unknown':'c'+n,name:'analysis_inspect',arguments:arguments_}))
]}});
const blocks=[];for(let n=0;n<3;n++)blocks.push(Boolean((await emit('tool_call',{toolCallId:'c'+n}))?.block));
console.log(JSON.stringify({failures:failures.length,blocks}));
'''
    result = subprocess.run(["node", "--input-type=module", "-e", script, fault], cwd=ROOT, text=True, capture_output=True, check=True)
    value = json.loads(result.stdout)
    if fault == "none":
        assert value["failures"] == 0
    else:
        assert value["failures"] > 0
    assert value["blocks"] == ([False] * 3 if fault == "none" else [True] * 3)
