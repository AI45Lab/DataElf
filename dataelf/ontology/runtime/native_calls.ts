/** Validate raw argument deltas before Pi's tolerant display parser can repair them.
 * This guard is ontology-only. Text markup is never a source of arguments.
 */
export function installNativeCallGuard(pi:any,invalid:(detail:any,ctx:any)=>void) {
 let fragments=new Map<number,string>(),valid=new Set<string>(),invalidMessage=false;
 pi.on('message_start',(event:any)=>{if(event.message?.role==='assistant'){fragments.clear();valid.clear();invalidMessage=false;}});
 pi.on('message_update',(event:any)=>{
  const delta=event.assistantMessageEvent;
  if(delta?.type==='toolcall_start')fragments.set(delta.contentIndex,'');
  if(delta?.type==='toolcall_delta')fragments.set(delta.contentIndex,(fragments.get(delta.contentIndex) || '')+delta.delta);
 });
 pi.on('message_end',(event:any,ctx:any)=>{
  const m=event.message;if(m?.role!=='assistant')return;
  (m.content || []).forEach((block:any,index:number)=>{
   if(block.type!=='toolCall')return;
   // Other native APIs may not expose JSON deltas. This guard applies to the
   // OpenAI-completions transport, including captured deltas in test harnesses.
   if(!['length','error','aborted'].includes(m.stopReason) && m.api!=='openai-completions' && !fragments.has(index)){valid.add(block.id);return;}
   let parsed:any;
   try{parsed=JSON.parse(fragments.get(index) ?? '');}catch{/* Never repair JSON. */}
   if(!['length','error','aborted'].includes(m.stopReason) && parsed && typeof parsed==='object' && !Array.isArray(parsed))valid.add(block.id);
   else {invalidMessage=true;invalid({code:'ONTOLOGY_TOOL_ARGUMENTS_INCOMPLETE',target:'format',issues:[{
    message:'Native tool arguments were incomplete or the response was interrupted; no partial arguments were executed.',
    tool:block.name,stop_reason:m.stopReason,error_message:m.errorMessage || null,recoverable:true}]},ctx);}
  });
 });
 pi.on('tool_call',(event:any)=>!invalidMessage && valid.has(event.toolCallId) ? undefined : {block:true,reason:'Native argument JSON was not confirmed complete; partial arguments cannot execute.'});
}
