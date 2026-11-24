/** Deadline-aware feedback. Steering is queued; a continuous stream is bounded separately. */
import {existsSync,writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {createHash} from 'node:crypto';

const PREFIX='ONTOLOGY_RUNTIME_STATUS\n';
export function deliveryInstruction(role:string,artifacts:any) {
 if(role?.endsWith('reviewer'))return 'This review is in its delivery reserve. Review only the exact candidate and bound evidence. Submit approve, revise or unusable with located issues and verifiable repair conditions. Insufficient evidence or budget cannot justify approval.';
 const draft=artifacts?.working_draft;
 const saved=Object.values(draft?.components || {}).some((n:any)=>n>0);
 const action=saved ? `The current draft is ${draft.hash}, status ${draft.status}. Prioritize decisive delivery gaps in the listed validation results and unresolved issues; save corrections. Submit only when complete.` :
  'No substantive component has been saved. Save modeling content supported by the evidence already available as an editable draft; retain unresolved associations as open questions. If a save is blocked, identify the concrete missing evidence or interface blocker.';
 return 'This stage has entered its delivery reserve. '+action+' Saving is not validation or publication. Local trials, full validation, independent review and publication have separate states; all final conditions remain required. Preserve recoverable work and specific blockers if completion is impossible.';
}
export function installDelivery(pi:any,task:any,directory:string,backend:any,ended:()=>boolean,stop:(detail:any,ctx:any)=>void) {
 const enabled=!!task.delivery?.enabled;
 const monitor=enabled || task.execution_policy==='unbounded';
 let timer:any,grace:any,firstTimer:any,boundaryRequest=false,generating=false,generation=0,inTool=0,phase='',notice='',artifacts:any;
 const inputs=new Map<string,any>();
 const clear=()=>{clearTimeout(timer);clearTimeout(grace);clearTimeout(firstTimer);};
 const delivered=async(kind:string,id?:string)=>backend('ontology_control',{event:'feedback_delivery',kind,notice_id:id});
 async function status() {
  const value=await backend('ontology_inspect',{resource:'delivery'});
  artifacts=value.artifacts;return value;
 }
 function text(budget:any,feedback?:any,first?:any,includeDelivery=true) {
  return PREFIX+JSON.stringify({budget,...(budget.phase==='closing' && includeDelivery ? {artifacts,
   delivery:deliveryInstruction(task.role,artifacts)}:{}),
   ...(feedback ? {observed_stagnation:feedback}:{}),...(first ? {first_artifact:first}:{})});
 }
 async function closing(ctx:any) {
  if(ended() || existsSync(join(directory,'submission.json')))return;
  const current=generation;
  const value=await status();
  if(ended() || existsSync(join(directory,'submission.json')))return;
  pi.sendMessage({customType:'ontology-budget',content:text({...value.budget,phase:'closing'}),display:false}, {deliverAs:'steer'});
  await delivered('closing_queued');
  // A queued steer cannot stop an active response. Interrupt only the SAME
  // response that crossed the boundary, never a new response or an active tool.
  grace=setTimeout(async()=>{
   if(ended() || !generating || generation!==current || inTool || existsSync(join(directory,'submission.json')))return;
   try {
    const result=await backend('ontology_control',{event:'closing_interrupt'});
    if(!result.interrupt || ended() || generation!==current || !generating || inTool || existsSync(join(directory,'submission.json')))return;
    await delivered('closing_interrupted');
    if(ended() || existsSync(join(directory,'submission.json')))return;
    stop({code:'ONTOLOGY_CLOSING_INTERRUPT',target:'control',issues:[{
     message:'Continuous generation crossed the closing reserve; unfinished arguments were discarded. Saved artifacts retained for one bounded closing session.',
     recoverable:true,reason:'internal_closing',session_deadline_monotonic:task.session_deadline_monotonic}]},ctx);
   }catch(error:any){stop({code:'ONTOLOGY_CONTROL_ERROR',target:'control',issues:[{message:String(error),recoverable:false}]},ctx);}
  },1000*task.delivery.stream_grace_seconds);
  grace.unref?.();
 }
 if(monitor) {
  pi.on('session_start',async(_event:any,ctx:any)=>{
   const value=await backend('ontology_control',{event:'budget'});
   const budget=value.budget;
   if(!enabled)return;
   if(task.role==='modeler') {
    firstTimer=setTimeout(async()=>{
     if(ended())return;
     try {
      const state=await backend('ontology_control',{event:'first_artifact_feedback'});
      if(!state.first_artifact_feedback || ended())return;
      pi.sendMessage({customType:'ontology-first-artifact',content:text(state.budget,undefined,state.first_artifact_feedback),display:false},{deliverAs:'steer'});
      await delivered('first_artifact_queued',state.first_artifact_feedback.id);
     }catch(error:any){stop({code:'ONTOLOGY_CONTROL_ERROR',target:'control',issues:[{message:String(error),recoverable:false}]},ctx);}
    },1000*Math.max(0,(task.delivery.first_artifact_notice_seconds ?? 180)-(value.first_artifact?.elapsed_seconds || 0)));
    firstTimer.unref?.();
   }
   if(budget.internal_interrupt_available && budget.effective_seconds_remaining!=null) {
    timer=setTimeout(()=>closing(ctx).catch((error:any)=>stop({code:'ONTOLOGY_CONTROL_ERROR',target:'control',issues:[{message:String(error),recoverable:false}]},ctx)),
     1000*Math.max(0,budget.effective_seconds_remaining-task.delivery.closing_reserve_seconds));
    timer.unref?.();
   }
  });
  pi.on('message_start',(event:any)=>{if(event.message?.role==='assistant'){generating=true;generation++;}});
  pi.on('message_end',async(event:any)=>{
   if(event.message?.role!=='assistant')return;
   generating=false;
   if(phase==='closing')await delivered('closing_response',JSON.stringify({stop_reason:event.message.stopReason,tools:(event.message.content || []).filter((c:any)=>c.type==='toolCall').map((c:any)=>c.name)}));
   if(boundaryRequest) {
    boundaryRequest=false;
    await backend('ontology_control',{event:'feedback_delivery',kind:'component_boundary_response',
      notice_id:JSON.stringify({stop_reason:event.message.stopReason,tools:(event.message.content || []).filter((c:any)=>c.type==='toolCall').map((c:any)=>c.name)})});
   }
   for(const block of event.message.content || [])if(block.type==='toolCall')inputs.set(block.id,block.arguments);
  });
  pi.on('tool_call',()=>ended() ? {block:true,reason:'Ontology session ended; incomplete or interrupted arguments cannot execute.'}:undefined);
  pi.on('tool_execution_start',()=>{inTool++;});
  pi.on('tool_execution_end',async(event:any)=>{
   inTool=Math.max(0,inTool-1);artifacts=undefined;
   if(ended())return;
   if(phase==='closing')await delivered('closing_tool_completed',JSON.stringify({tool:event.toolName,is_error:!!event.isError}));
   const raw=(event.result?.content || []).filter((b:any)=>b.type==='text').map((b:any)=>b.text).join('\n').split('\n\nReceived arguments:')[0];
   let result:any;try{result=JSON.parse(raw);}catch{result={code:'TOOL_ERROR',message:raw.slice(0,1500)};}
   const args=inputs.get(event.toolCallId);inputs.delete(event.toolCallId);
   const signature=createHash('sha256').update(JSON.stringify(args || {})+raw).digest('hex');
   await backend('ontology_control',{event:'observe',tool:event.toolName,result,is_error:!!event.isError,exact_signature:signature});
  });
  pi.on('agent_end',clear);pi.on('session_shutdown',clear);
 }
 return {
  async request(payload:any,state:any,ctx:any) {
   if(!enabled)return payload;
   const budget=state.budget;
   if(budget.phase==='closing' && (!artifacts || phase!==budget.phase))await status();
   const feedback=state.feedback && state.feedback.id!==notice ? state.feedback:undefined;
   if(feedback)notice=feedback.id;
   const phaseChanged=phase!==budget.phase;
   const content=text(budget,feedback,state.first_artifact_feedback,phaseChanged);
   if(phaseChanged && budget.phase==='closing')await delivered('closing_request_prepared');
   if(state.first_artifact_feedback)await delivered('first_artifact_request_prepared',state.first_artifact_feedback.id);
   const messages=(payload.messages || []).filter((m:any)=> !(m.role==='user' && (
    typeof m.content==='string' ? m.content.startsWith(PREFIX):Array.isArray(m.content) && m.content.some((b:any)=>b.type==='text' && b.text?.startsWith(PREFIX)))));
   messages.push({role:'user',content});
   await delivered(phaseChanged ? 'phase_request_prepared':'budget_refreshed',feedback?.id);
   phase=budget.phase;
   if(state.component_boundary) {
    const visible=(payload.tools || []).some((t:any)=>(t.function?.name || t.name)==='ontology_author');
    if(!visible)throw new Error('Component boundary requires visible ontology_author; no fallback is allowed');
    boundaryRequest=true;
    messages.push({role:'user',content:'Experimental component delivery boundary: make one evidence-backed component save through ontology_author now, using the current draft hash. You choose its semantics. This one-request constraint then ends; saving remains unvalidated. If evidence is insufficient, return the concrete blocker rather than inventing a component.'});
    return {...payload,messages,tool_choice:{type:'function',function:{name:'ontology_author'}}};
   }
   return {...payload,messages};
  },clear,
 };
}
