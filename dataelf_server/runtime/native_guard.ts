/** Raw native argument integrity guard; independent of ontology research control. */
import {existsSync,mkdirSync,writeFileSync} from 'node:fs';
import {join} from 'node:path';

export function installNativeGuard(pi:any, requiredTool?:()=>string|undefined) {
    const fragments=new Map<number,string>();let valid=new Set<string>(),invalid=false;
    pi.on('message_start',(event:any)=>{if(event.message?.role==='assistant'){fragments.clear();valid.clear();invalid=false;}});
    pi.on('message_update',(event:any)=>{
        const delta=event.assistantMessageEvent;
        if(delta?.type==='toolcall_start')fragments.set(delta.contentIndex,'');
        if(delta?.type==='toolcall_delta')fragments.set(delta.contentIndex,(fragments.get(delta.contentIndex) ?? '')+delta.delta);
    });
    pi.on('message_end',(event:any,ctx:any)=>{
        const message=event.message;if(message?.role!=='assistant')return;
        const calls=(message.content ?? []).filter((b:any)=>b.type==='toolCall');
        const required=requiredTool?.();
        if(new Set(calls.map((b:any)=>b.id)).size!==calls.length ||
            (required && calls.length && (calls.length!==1 || calls[0].name!==required)))invalid=true;
        (message.content ?? []).forEach((block:any,index:number)=>{
            if(block.type!=='toolCall')return;
            if(!['length','error','aborted'].includes(message.stopReason) && message.api!=='openai-completions' && !fragments.has(index)){valid.add(block.id);return;}
            let args:any;try{args=JSON.parse(fragments.get(index) ?? '');}catch{}
            if(!['length','error','aborted'].includes(message.stopReason) && args && typeof args==='object' && !Array.isArray(args))valid.add(block.id);
            else invalid=true;
        });
        const textOnlyMarkup=!(message.content ?? []).some((b:any)=>b.type==='toolCall') &&
            (message.content ?? []).some((b:any)=>b.type==='text' && /<tool_call>/.test(b.text || ''));
        const missingNative=(message.stopReason==='stop' && textOnlyMarkup) ||
            String(message.errorMessage || '').startsWith('PI_NATIVE_TOOL_CHANNEL_INCOMPATIBLE:');
        if(invalid || missingNative) {
            const workspace=process.env.DATAELF_JOB_WORKSPACE || process.env.DATAELF_WORKSPACE;
            if(workspace){mkdirSync(join(workspace,'logs'),{recursive:true});writeFileSync(join(workspace,'logs',(process.env.DATAELF_SERVER_LOG_PREFIX || 'pi')+'_tool_channel_error.json'),JSON.stringify({code:missingNative ? 'PI_NATIVE_TOOL_CHANNEL_INCOMPATIBLE':'PI_MODEL_ERROR',stop_reason:message.stopReason,
                protocol:process.env.DATAELF_SERVER_TOOL_PROTOCOL || 'native',native_call_count:calls.length,confirmed_call_ids:valid.size,text_tool_markup:textOnlyMarkup,
                message:missingNative ? 'Provider returned tool markup in text without native tool calls; no call was executed.':'Native tool argument batch was incomplete, repeated, or outside the current server phase; no call was executed.'}));}
            ctx.abort();
        }
    });
    pi.on('tool_call',(event:any)=>!invalid && valid.has(event.toolCallId) ? undefined : {block:true,reason:'Server native arguments were incomplete or interrupted.'});
}
