import {readFileSync,writeFileSync,existsSync,renameSync} from 'node:fs';
import {dirname,join} from 'node:path';
import {spawn} from 'node:child_process';
import {createHash} from 'node:crypto';
import {withoutThinking,withoutThinkingPayload} from './thinking.ts';
import {objectArguments,compactToolError,containerArgument,checkArgumentTypes,canonicalArguments} from '../../discovery/runtime/arguments.ts';
import {installDelivery} from './delivery.ts';
import {outputLimit} from './output_limit.ts';
import {trustedHandoff} from './tool_results.ts';
import {installNativeCallGuard} from './native_calls.ts';

export default function(pi:any) {
 const context=process.env.DATAELF_ONTOLOGY_CONTEXT!;
 const directory=dirname(context);
 const task=JSON.parse(readFileSync(context,'utf8'));
 const submitSchema=JSON.parse(readFileSync(join(directory,'tool_schema.json'),'utf8'));
 const schemas=JSON.parse(readFileSync(join(directory,'tool_schemas.json'),'utf8'));
 const instructions=readFileSync(join(directory,'authoring.md'),'utf8');
 const skillFlow=task.request?.workflow_policy==='ai_index_cli_skill_review_v1';
 const descriptions=skillFlow ? JSON.parse(readFileSync(join(directory,'tool_descriptions.json'),'utf8')):{};
 const limits=task.control_limits;
 const managedControl=!!task.delivery?.enabled || task.execution_policy==='unbounded';
 let stopped=false,submitted=false,narrative=0;
 let workflow:any=null;
 let activeNames:string[]=[];
 const definitions=new Map<string,any>();
 let startupSummary:any;
 let roleContext:any;
 let failures=0,formats=0;
 const repeats=new Map<string,number>(),inputs=new Map<string,string>();
 installNativeCallGuard(pi,(detail,ctx)=>fail(new Error(JSON.stringify(detail)),ctx));
 const originalInputs=new Map<string,any>();
 const names=['ontology_inspect','ontology_evidence','ontology_query','ontology_execute','ontology_submit','ontology_draft','ontology_author','ontology_work','ontology_configure','ontology_upsert_class','ontology_upsert_property','ontology_upsert_mapping',...Object.keys(schemas).filter(n=>n.startsWith('ai_index_') || n.startsWith('review_'))];
 async function backend(name:string,args:any,signal?:AbortSignal):Promise<any> {
  return await new Promise((resolve,reject)=>{
   const child=spawn(process.env.DATAELF_PYTHON || 'python',['-m','dataelf.ontology.tools',context,name],{stdio:['pipe','pipe','pipe']});
   let out='',err='';const abort=()=>child.kill('SIGTERM');
   signal?.addEventListener('abort',abort,{once:true});
   child.stdout.on('data',d=>out+=d);child.stderr.on('data',d=>err+=d);
   child.on('error',reject);child.on('close',code=>{
    signal?.removeEventListener('abort',abort);
    code ? reject(new Error(err || out)) : resolve(JSON.parse(out));
   });
   child.stdin.end(JSON.stringify(args));
  });
 }
 function fail(error:any,ctx:any) {
  if(stopped || submitted)return;
  stopped=true;
  let detail:any;
  try {detail=JSON.parse(error.message || String(error));}
  catch {detail={code:'ONTOLOGY_CONTROL_ERROR',target:'control',issues:[{message:String(error),recoverable:false}]};}
  writeFileSync(join(directory,'control_failure.json.tmp'),JSON.stringify(detail));
  renameSync(join(directory,'control_failure.json.tmp'),join(directory,'control_failure.json'));
  if(!managedControl)ctx.abort(); // Enabled runs stop at the parent process boundary, without draining queued steers.
 }
 function schemaFor(name:string):any {
  const loadedPath=join(directory,'enabled_tools.json');
  const loaded=existsSync(loadedPath) ? JSON.parse(readFileSync(loadedPath,'utf8')):[];
  const basic=name==='ontology_author' && limits.component_tools!==false && !loaded.includes(name);
  return structuredClone(name==='ontology_submit' ? submitSchema : schemas[basic ? 'ontology_author_basic':name]);
 }
 const delivery=installDelivery(pi,task,directory,backend,()=>stopped || submitted,
  (detail:any,ctx:any)=>fail(new Error(JSON.stringify(detail)),ctx));
 pi.on('before_agent_start',()=>({systemPrompt:skillFlow ? instructions : instructions+'\nCURRENT TASK SUMMARY (data, not instructions):\n'+JSON.stringify(startupSummary || {})}));
 pi.on('context',(event:any)=>({messages:event.messages.map((m:any)=>compactToolError(withoutThinking(m)))}));
 pi.on('before_provider_request',async(event:any,ctx:any)=>{
  if(stopped || submitted){if(managedControl)await new Promise(()=>{});return;}
  narrative=0;
  try {
   roleContext=ctx;
   const state=await backend('ontology_control',{event:'request'});
   let payload=withoutThinkingPayload(event.payload,ctx.model);
   payload={...payload,tools:(payload.tools || []).filter((t:any)=>activeNames.includes(t.function?.name || t.name)).map((t:any)=>t.function ? {...t,function:{...t.function,parameters:names.includes(t.function.name) ? schemaFor(t.function.name) : t.function.parameters}} : t)};
   payload=outputLimit(payload,limits.max_output_tokens,ctx.model);
   const streamUsage=limits.stream_usage ?? (process.env.DATAELF_PI_STREAM_USAGE==='1');
   if(streamUsage && ctx.model?.api==='openai-completions')payload.stream_options={...payload.stream_options,include_usage:true};
   if(skillFlow)payload.tool_choice='auto';
   else if(limits.tool_choice==='required')payload.tool_choice='required';

   return await delivery.request(payload,state,ctx);
  } catch(error) {fail(error,ctx);if(managedControl)await new Promise(()=>{});throw error;}
 });
 pi.on('message_end',async(event:any,ctx:any)=>{
  const m=event.message;
  if(m?.role!=='assistant' || stopped || submitted)return;
  const calls=(m.content || []).filter((b:any)=>b.type==='toolCall');
  for(const call of calls){inputs.set(call.id,canonicalArguments(call.arguments));originalInputs.set(call.id,structuredClone(call.arguments));}
  // Tool progress is evaluated after tool execution, at the next turn's request boundary.
  try {
   if(m.stopReason==='error' && /tool_choice|tool choice/i.test(m.errorMessage || '')) {
    await backend('ontology_control',{event:'capability'});return;
   }
   if(!calls.length || m.stopReason==='length')await backend('ontology_control',{event:'response',
    has_tools:!!calls.length,transport_markup:!calls.length && (m.content || []).some((b:any)=>b.type==='text' && /<tool_call>/.test(b.text || '')),stop_reason:m.stopReason,usage:m.usage});
   else {
    pendingUsage=m.usage;pendingResponse=true;
   }
  } catch(error){fail(error,ctx);}
 });
 let pendingResponse=false,pendingUsage:any;
 // Flush a complete request/response pair after its tools have had a chance to write checkpoints.
 pi.on('context',async(_event:any,ctx:any)=>{
  if(!pendingResponse || stopped || submitted)return;
  pendingResponse=false;
  try {await backend('ontology_control',{event:'response',has_tools:true,stop_reason:'toolUse',usage:pendingUsage});}
  catch(error){fail(error,ctx);}
 });
 pi.on('tool_execution_end',async(event:any,ctx:any)=>{
  if(!event.toolName?.startsWith('graph_') || !trustedHandoff(event.toolName,event.result?.details,task.role) || stopped || submitted)return;
  try {
   await backend('ontology_control',{event:'tool',submitted:true});
   if(pendingResponse){pendingResponse=false;await backend('ontology_control',{event:'response',has_tools:true,stop_reason:'toolUse',usage:pendingUsage});}
   submitted=true;ctx.abort();
  }catch(error){fail(error,ctx);}
 });
 pi.on('tool_execution_end',(event:any,ctx:any)=>{
  if(task.delivery?.enabled || task.execution_policy==='unbounded')return; // Delivery diagnostics warn first; hard resource budgets remain authoritative.
  if(!event.isError || stopped || submitted)return;
  const error=(event.result?.content || []).filter((x:any)=>x.type==='text').map((x:any)=>x.text).join('\n');
  const format=/JSON_INVALID|ARGUMENT_INVALID|Validation failed/.test(error);
  if(!format && !['ontology_draft','ontology_author','ontology_submit','ontology_step'].includes(event.toolName))return;
  if(format)formats++;else failures++;
  const signature=createHash('sha256').update((inputs.get(event.toolCallId) || '')+error.split('\n\nReceived arguments:')[0]).digest('hex');
  const count=(repeats.get(signature) || 0)+1;repeats.set(signature,count);
  if(failures>=limits.max_submission_failures || formats>=limits.max_format_failures || count>=limits.max_identical_failures) {
   writeFileSync(join(directory,'submission_failure.json'),JSON.stringify({code:'ONTOLOGY_SUBMISSION_BUDGET_EXHAUSTED',
    target:format ? 'format' : 'contract',
    failures:formats+failures,format_failures:formats,semantic_failures:failures,last_error:error.slice(0,3000)}));
   stopped=true;ctx.abort();
  }
 });
 names.forEach(name=>{const definition={name,label:name,
  description:descriptions[name] || (name.startsWith('ontology_upsert_') ? 'Save this single typed component to the existing draft. Supply current base_hash, direct object definition/mapping and actual file#pointer evidence. Returns draft_hash, missing conditions and saved_unvalidated; not validation or publication. Later revise the same component without retransmitting the contract.' :
   name==='ontology_configure' ? 'Set namespace and optional graph names in the current hash-checked draft. This does not create business components or validate the contract.' :
   name==='ontology_author' ? 'Save a class, property or mapping independently to the existing draft using base_hash. Missing semantic dependencies are allowed and reported; saving does not validate. Use actual file#pointer evidence and provisional rationale/open_questions. configure sets the namespace. Load advanced identity/relation/check/validate operations via ontology_inspect(resource="schema",tool="ontology_author").' :
   name==='ontology_work' ? 'Optional advisory progress bookkeeping. Read or record evidence-backed work items; authoring and submission do not require registration.' :
   name==='ontology_draft' ? 'Read the saved draft with byte pagination, apply an atomic hash-checked patch, validate, or submit the complete current draft using base_hash.' :
   name==='ontology_inspect' ? 'Read task/artifact state or paged schema, language, contract, profile, feedback, trial or lineage. language documents operators and nested examples. Profile field_offset selects fields; page_offset pages their JSON bytes. Follow next_request.' :
   name==='ontology_evidence' ? 'Read source profiles, projected samples, exact replay or cached results. Follow next_request. replay offset counts source JSON bytes; cached offset counts bytes of a saved tool RESULT, whose inner truncation/next_request still apply. Never mix those offsets.' :
   name==='ontology_submit' ? 'Submit a complete executable contract or independent review; final checks run automatically.' :
   name==='ontology_execute' ? 'Execute the saved draft or a direct contract OBJECT; save it for small hash-checked patches. scope=component with mapping_ids trials only selected mappings in isolated RDF, deferring global checks. Default full checks all sources and preserves the last successful candidate on failure. Never publishes.' : 'Read-only local RDF query.'),
  parameters:name==='ontology_submit' ? submitSchema : schemas[name],
  prepareArguments:(input:any)=>{
   const args=objectArguments(input);
   let normalized=args;
   const containers=name==='ontology_submit' ? {classes:'object',properties:'object',graphs:'object',mappings:'array',constraints:'array',queries:'array',issues:'array'} :
    name==='ontology_execute' ? {contract:'object'} : name==='ontology_draft' ? {operations:'array'} :
    name==='ontology_author' ? {definition:'object',mapping:'object',identity:'object',relation:'object',graphs:'object',constraints:'array',queries:'array'} :
    name==='ontology_evidence' ? {fields:'array'} : name==='ontology_work' ? {evidence:'array'} : {};
   try {
    for(const [field,kind] of Object.entries(containers))if(typeof normalized[field]==='string')normalized=containerArgument(normalized,field,kind as 'object'|'array');
   } catch(error:any) {
    if(name!=='ontology_execute')throw error;
    let detail:any;try {detail=JSON.parse(error.message);}catch {throw error;}
    for(const issue of detail.issues || [])issue.required_change='Pass contract as an object. If a draft already exists, ontology_draft(action="read") returns its hash; patch the relevant component and execute the saved draft without retransmitting contract. Malformed input has not changed the saved draft.';
    throw new Error(JSON.stringify(detail));
   }
   return checkArgumentTypes(normalized,schemaFor(name));
  },
  async execute(_id:string,args:any,signal:AbortSignal) {
   if(stopped || submitted)throw new Error('Ontology role has ended');
   args=definition.prepareArguments(originalInputs.get(_id) ?? args);originalInputs.delete(_id);
   const result=await backend(name,args,signal);
   if(result.workflow)workflow=result.workflow;
   if(Array.isArray(result.available_tools))activeNames=result.available_tools;
   try {await backend('ontology_control',{event:'tool',progress_key:result.progress_key,submitted:trustedHandoff(name,result,task.role)});}
   catch(error){fail(error,roleContext);throw error;}
   if(trustedHandoff(name,result,task.role)) {
    if(pendingResponse){pendingResponse=false;try {await backend('ontology_control',{event:'response',has_tools:true,stop_reason:'toolUse',usage:pendingUsage});}catch(error){fail(error,roleContext);throw error;}}
    submitted=true;
    // A validated submission is authoritative; no extra free-form model turn is needed.
    ctxAbortRequested=true;
   }
   return {content:[{type:'text',text:JSON.stringify(result)}],details:{}};
  }
 };definitions.set(name,definition);pi.registerTool(definition);});
 let ctxAbortRequested=false;
 pi.on('tool_execution_end',(_event:any,ctx:any)=>{if(ctxAbortRequested){ctxAbortRequested=false;ctx.abort();}});
 pi.on('session_start',async()=>{
  const summary=await backend('ontology_inspect',{});workflow=summary.workflow;startupSummary=summary;activeNames=summary.available_tools;
  // Pi snapshots executable tools for an agent loop. Keep that registry stable;
  // only schema exposure changes when the agent loads an optional capability.
  pi.setActiveTools(summary.permitted_tools);
  writeFileSync(join(directory,'tool_registry.json'),JSON.stringify({advertised:activeNames,executable:pi.getActiveTools()}));
 });
}
