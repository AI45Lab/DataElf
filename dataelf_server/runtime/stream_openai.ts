/** Reviewed server-only derivative of the reference worktree stream adapter.
 * Native calls take precedence. Every call is withheld until terminal validation.
 */
import { streamSimpleOpenAICompletions as nativeStream, createAssistantMessageEventStream,
    type AssistantMessage, type Context, type Model, type SimpleStreamOptions, type ToolCall } from '@earendil-works/pi-ai';
import {ToolEnvelopeError,decodeTextualTools,validateNativeArguments,type OfferedTools} from './textual_tools.ts';
import {stripThinkingPrefix} from './thinking.ts';

export function streamingOpenAI(model:Model<any>, context:Context, options?:SimpleStreamOptions) {
    const stream=createAssistantMessageEventStream();
    let last:AssistantMessage|undefined;
    let offered:OfferedTools=new Map(context.tools?.map(t=>[t.name,t.parameters]) ?? []);
    let required:string|undefined;
    const fragments=new Map<number,string>();
    void (async()=>{
        try {
            if(options?.signal?.aborted) throw new Error("Request was aborted before transport start");
            const source=nativeStream(model,context,{...options,onPayload:async(payload,selected)=>{
                const transformed=await options?.onPayload?.(payload,selected);
                const effective:any=transformed ?? payload;
                if (Array.isArray(effective.tools)) offered=new Map(effective.tools.map((t:any)=>[t.function.name,t.function.parameters]));
                required=effective.tool_choice?.function?.name;
                return transformed;
            }});
            for await (const event of source) {
                if (event.type==='toolcall_start') {fragments.set(event.contentIndex,'');last=event.partial;continue;}
                if (event.type==='toolcall_delta') {fragments.set(event.contentIndex,(fragments.get(event.contentIndex) ?? '')+event.delta);last=event.partial;continue;}
                if (event.type==='toolcall_end') {last=event.partial;continue;}
                if (event.type==='error') {last=event.error;throw new Error(event.error.errorMessage || 'Server stream aborted');}
                if (event.type!=='done') {last=event.partial;stream.push(event);continue;}
                let message=event.message;last=message;
                if (options?.signal?.aborted) throw new Error('Request was aborted');
                if (event.reason==='length') {
                    message={...message,content:message.content.filter(b=>b.type!=='toolCall')};
                    stream.push({type:'done',reason:'length',message});stream.end(message);return;
                }
                const calls:ToolCall[]=[];
                // Validate the entire batch before emitting any executable event.
                for (const [index,block] of message.content.entries()) {
                    if (block.type!=='toolCall') continue;
                    const args=JSON.parse(fragments.get(index) ?? '');
                    calls.push({...block,arguments:validateNativeArguments(block.name,args,offered,required)});
                }
                if (!calls.length && process.env.DATAELF_SERVER_TOOL_PROTOCOL==='glm_text') {
                    const text=stripThinkingPrefix(message.content.filter(b=>b.type==='text').map(b=>b.text).join(''));
                    calls.push(...decodeTextualTools(text,offered,event.reason,false,required).map((call,index)=>({
                        type:'toolCall' as const,id:`server_text_${Date.now()}_${index}`,name:call.name,arguments:call.arguments})));
                }
                if (new Set(calls.map(call=>call.id)).size!==calls.length) throw new Error('Duplicate native tool call IDs');
                if (calls.length) {
                    message={...message,content:calls,stopReason:'toolUse'};
                    for (const [contentIndex,toolCall] of calls.entries()) {
                        stream.push({type:'toolcall_start',contentIndex,partial:message});
                        stream.push({type:'toolcall_delta',contentIndex,delta:JSON.stringify(toolCall.arguments),partial:message});
                        stream.push({type:'toolcall_end',contentIndex,toolCall,partial:message});
                    }
                    stream.push({type:'done',reason:'toolUse',message});
                } else stream.push({...event,message});
                stream.end(message);return;
            }
            throw new Error('Server stream ended without a terminal result');
        } catch(error) {
            const message={...last,role:'assistant',content:[],api:model.api,provider:model.provider,model:model.id,
                timestamp:Date.now(),stopReason:options?.signal?.aborted ? 'aborted':'error',
                errorMessage:error instanceof Error ? error.message:String(error),
                usage:last?.usage ?? {input:0,output:0,cacheRead:0,cacheWrite:0,totalTokens:0,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}}} as AssistantMessage;
            if(error instanceof ToolEnvelopeError) (message as any).protocolFailure=error.diagnostic();
            stream.push({type:'error',reason:message.stopReason as 'aborted'|'error',error:message});stream.end(message);
        }
    })();
    return stream;
}
