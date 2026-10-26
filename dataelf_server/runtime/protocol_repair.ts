/** Service-only, finite correction of a rejected terminal envelope. No tool executes
 * until the adapter validates the complete batch. Transport failures are not repairs.
 */
import {createAssistantMessageEventStream} from '@earendil-works/pi-ai';
import {appendFileSync,mkdirSync,writeFileSync,existsSync,readFileSync,renameSync} from 'node:fs';
import {join} from 'node:path';

export function protocolRepair(factory:any, maximum:number, workspace?:string, prefix='pi') {
    let used=0, sequence=0;
    const statePath=workspace ? join(workspace,'logs','server_protocol_repair_state.json') : undefined;
    if(statePath && existsSync(statePath)) {
        used=JSON.parse(readFileSync(statePath,'utf8')).used;
        if(!Number.isInteger(used) || used<0)throw Error('Invalid persisted protocol repair allowance');
    }
    if(!Number.isInteger(maximum) || maximum<0) throw Error('Invalid protocol repair allowance');
    return (model:any,context:any,options:any={})=>{
        const stream=createAssistantMessageEventStream();
        const request=`${Date.now()}_${++sequence}`;
        void(async()=>{
            let feedback='';
            const settled:any[]=[];
            for(;;) {
                const events:any[]=[];
                const source=factory(model,context,{...options,onPayload:async(payload:any,selected:any)=>{
                    const effective=await options.onPayload?.(payload,selected) ?? payload;
                    if(!feedback)return effective;
                    return {...effective,messages:[...(effective.messages ?? []),{role:'user',content:feedback}]};
                }});
                for await(const event of source) {
                    if(event.type==='error' || event.type==='done')events.push(event);
                    else stream.push(event);
                }
                const message:any=await source.result();
                const failure=message.protocolFailure;
                if(failure && !options.signal?.aborted) {
                    settled.push(message.usage);
                    const retry=used<maximum;
                    if(workspace) {
                        const logs=join(workspace,'logs');mkdirSync(logs,{recursive:true});
                        const path=join(logs,`${prefix}_protocol_failure_${request}_${settled.length}.json`);
                        writeFileSync(path,JSON.stringify({request,repair_used:used,retry,diagnostic:failure,usage:message.usage}),{mode:0o600});
                        appendFileSync(join(logs,`${prefix}_protocol_repairs.jsonl`),JSON.stringify({request,repair_used:used,retry,path,usage:message.usage})+'\n',{mode:0o600});
                        if(!retry)writeFileSync(join(logs,`${prefix}_tool_channel_error.json`),JSON.stringify({
                            code:'SERVER_TOOL_PROTOCOL_REPAIR_EXHAUSTED',stop_reason:'error',message:`Protocol repair allowance exhausted (${maximum}): ${failure.message}`}));
                    }
                    if(retry) {
                        used++;
                        if(statePath) {
                            const temporary=statePath+'.tmp';
                            writeFileSync(temporary,JSON.stringify({version:1,used}),{mode:0o600});
                            renameSync(temporary,statePath);
                        }
                        feedback=`The previous response was rejected before any tool execution: ${failure.message}\nResubmit one complete legal tool call. Do not resend an earlier successful call. Existing graph, scripts and artifacts are retained. Allowed tools and required parameters: ${JSON.stringify(failure.allowed_tools)}. Required tool: ${failure.required_tool ?? 'any offered tool'}. Use native tool calls, or the declared complete tool_call/arg_key/arg_value protocol; no prose or duplicate closing tags. Prior script errors remain applicable.`;
                        continue;
                    }
                    message.errorMessage=`SERVER_TOOL_PROTOCOL_REPAIR_EXHAUSTED: ${failure.message}`;
                }
                // Failed responses have no executable events. Account for their
                // settled usage once in the final result, also retain per-attempt logs.
                const previous=failure ? settled.slice(0,-1) : settled;
                for(const usage of previous) {
                    for(const key of ['input','output','cacheRead','cacheWrite','totalTokens'])
                        message.usage[key]=(message.usage[key] ?? 0)+(usage?.[key] ?? 0);
                    for(const key of ['input','output','cacheRead','cacheWrite','total'])
                        message.usage.cost[key]=(message.usage.cost[key] ?? 0)+(usage?.cost?.[key] ?? 0);
                }
                for(const event of events)stream.push(event);
                stream.end(message);return;
            }
        })().catch(error=>{
            const message:any={role:'assistant',content:[],api:model.api,provider:model.provider,model:model.id,
                timestamp:Date.now(),stopReason:options.signal?.aborted ? 'aborted':'error',errorMessage:String(error),
                usage:{input:0,output:0,cacheRead:0,cacheWrite:0,totalTokens:0,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}}};
            stream.push({type:'error',reason:message.stopReason,error:message});stream.end(message);
        });
        return stream;
    };
}
