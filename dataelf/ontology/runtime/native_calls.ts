/** Validate raw argument deltas before Pi's tolerant display parser can repair them.
 * Used by reviewed ontology and analysis roles. Text markup is never executable.
 */
export function installNativeCallGuard(pi:any,invalid:(detail:any,ctx:any)=>void) {
 let fragments=new Map<number,string>(),byId=new Map<string,string>(),valid=new Set<string>(),invalidMessage=false;
 pi.on('message_start',(event:any)=>{if(event.message?.role==='assistant'){fragments.clear();byId.clear();valid.clear();invalidMessage=false;}});
 pi.on('message_update',(event:any)=>{
  const delta=event.assistantMessageEvent;
  if(delta?.type==='toolcall_start')fragments.set(delta.contentIndex,'');
  if(delta?.type==='toolcall_delta')fragments.set(delta.contentIndex,(fragments.get(delta.contentIndex) || '')+delta.delta);
  // Earlier message_end hooks may remove reasoning/text blocks, changing indexes.
  // Bind the original raw stream to the stable native call ID before that happens.
  if(delta?.type==='toolcall_end' && delta.toolCall?.id && fragments.has(delta.contentIndex))
   byId.set(delta.toolCall.id,fragments.get(delta.contentIndex)!);
 });
 pi.on('message_end',(event:any,ctx:any)=>{
  const m=event.message;if(m?.role!=='assistant')return;
  (m.content || []).forEach((block:any)=>{
   if(block.type!=='toolCall')return;
   // Other native APIs may not expose JSON deltas. This guard applies to the
   // OpenAI-completions transport, including captured deltas in test harnesses.
   if(!['length','error','aborted'].includes(m.stopReason) && m.api!=='openai-completions' && !byId.has(block.id) && fragments.size===0){valid.add(block.id);return;}
   let parsed:any;
   try{parsed=JSON.parse(byId.get(block.id) ?? '');}catch{/* Never repair JSON. */}
   if(!['length','error','aborted'].includes(m.stopReason) && parsed && typeof parsed==='object' && !Array.isArray(parsed))valid.add(block.id);
   else {invalidMessage=true;invalid({code:'ONTOLOGY_TOOL_ARGUMENTS_INCOMPLETE',target:'format',issues:[{
    message:'Native tool arguments were incomplete or the response was interrupted; no partial arguments were executed.',
    tool:block.name,stop_reason:m.stopReason,error_message:m.errorMessage || null,recoverable:true}]},ctx);}
  });
 });
 pi.on('tool_call',(event:any)=>!invalidMessage && valid.has(event.toolCallId) ? undefined : {block:true,reason:'Native argument JSON was not confirmed complete; partial arguments cannot execute.'});
}
