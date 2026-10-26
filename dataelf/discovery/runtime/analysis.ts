import {readFileSync,existsSync,writeFileSync} from 'node:fs';
import {dirname,join} from 'node:path';
import {spawn} from 'node:child_process';
import {installNativeCallGuard} from '../../ontology/runtime/native_calls.ts';
import {outputLimit} from '../../ontology/runtime/output_limit.ts';
import {trustedHandoff} from '../../ontology/runtime/tool_results.ts';
import {withoutThinkingPayload} from '../../ontology/runtime/thinking.ts';

export default function(pi:any) {
 if(process.env.DATAELF_WORKFLOW_PROFILE!=='research' || process.env.DATAELF_DOMAIN!=='ai_index')return;
 const path=process.env.DATAELF_ANALYSIS_CONTEXT;
 if(!path || !existsSync(path))return;
 const task=JSON.parse(readFileSync(path,'utf8'));
 const modern=task.workflow_policy==='ai_index_cli_skill_review_v1';
 let handedOff=false;
 const registry=JSON.parse(readFileSync(join(dirname(path),'tools.json'),'utf8'));
 installNativeCallGuard(pi,(detail:any,ctx:any)=>{
  writeFileSync(join(dirname(path),'analysis_transport.json'),JSON.stringify(detail));ctx.abort();
 });
 for(const [name,entry] of Object.entries(registry) as any) {
  pi.registerTool({name,label:name,description:entry.description,parameters:entry.schema,
   async execute(_id:string,args:any,signal:AbortSignal) {
    const result=await new Promise<any>((resolve,reject)=>{
     const child=spawn(process.env.DATAELF_PYTHON!,['-m','dataelf.discovery.analysis_review.tools',path],{stdio:['pipe','pipe','pipe']});
     let out='',err='';const abort=()=>child.kill('SIGTERM');signal?.addEventListener('abort',abort,{once:true});
     child.stdout.on('data',d=>out+=d);child.stderr.on('data',d=>err+=d);child.on('error',reject);
     child.on('close',code=>{signal?.removeEventListener('abort',abort);try {
      const value=JSON.parse(out);code?reject(new Error(JSON.stringify(value))):resolve(value);
     }catch(error){reject(new Error(err||out||String(error)));}});
     child.stdin.end(JSON.stringify({tool:name,arguments:args}));
    });
    return {content:[{type:'text',text:JSON.stringify(result)}],details:result};
   }
  });
 }
 if(task.role==='reviewer')pi.on('session_start',()=>pi.setActiveTools(Object.keys(registry)));
 if(modern && task.role!=='reviewer')pi.on('session_start',()=>{
  const allowed=new Set(['read','bash','edit','write','dataelf_submit_output',...Object.keys(registry),
   ...((task.graph_refs || []).length ? ['graph_workspace','graph_inspect','graph_query','graph_explore','graph_materialize','graph_update','graph_diff','graph_validate','graph_commit']:[])]);
  pi.setActiveTools(pi.getAllTools().map((t:any)=>t.name).filter((n:string)=>allowed.has(n)));
 });
 if(modern)pi.on('tool_execution_end',(event:any,ctx:any)=>{
  if(!handedOff && !event.isError && trustedHandoff(event.toolName,event.result?.details,task.role)){
   handedOff=true;ctx.abort();
  }
 });
 pi.on('before_provider_request',(event:any,ctx:any)=>{
  if(handedOff)return new Promise(()=>{});
  if(event.payload && typeof event.payload==='object') {
   const result={...(modern ? withoutThinkingPayload(event.payload,ctx.model):event.payload),tool_choice:'auto'};
   if(modern)result.tools=(result.tools || []).map((t:any)=>{
    const chinese:Record<string,string>={read:'读取任务内文件内容。',bash:'运行任务内分析命令；来源查询须登记，不修改系统代码或私有配置。',
     edit:'准确修改任务内分析文件，不覆盖冻结来源或已发布图谱。',write:'保存任务内分析文件，不覆盖冻结来源或已发布图谱。'};
    return t.function && chinese[t.function.name] ? {...t,function:{...t.function,description:chinese[t.function.name]}}:t;
   });
   return modern ? outputLimit(result,task.max_output_tokens,ctx.model) : result;
  }
 });
 pi.on('before_agent_start',(event:any)=>modern ? {systemPrompt:readFileSync(join(dirname(path),'system.md'),'utf8')} : task.role==='reviewer' ? {systemPrompt:
  'You are the independent AI Index research reviewer. Read the exact immutable submission and original goal. '+
  'Check substantive Insight thesis, key figures, denominators, time scope, inference strength, sources and counterevidence. '+
  'Use analysis_read and isolated analysis_verify for evidence and independent checks; when a graph is supplied its tools are read-only. '+
  'Do not edit reports, sources or graphs. Do not invent additional task requirements or block on style preferences. '+
  'Account for every previous blocker, including analyst counterevidence. Return actionable evidenced issues with stable IDs. '+
  'A negative finding may fulfill the objective; missing requested scope may be limited or unfulfilled. '+
  'Only analysis_review_submit is a verdict. No budget or iteration count justifies approval. '+
  'Use analysis_inspect(contract/files/submission/previous_review) for details. '+
  JSON.stringify({submission_id:task.submission.submission_id,submission_hash:task.submission.submission_hash,policy:task.submission.policy_version})}
  : {systemPrompt:event.systemPrompt+'\nAI Index research has no cumulative task time, request, token or revision limit. '+
   'Choose your own research and tool order. Save useful work as files; submit the complete saved delivery with analysis_submit. '+
   'dataelf_submit_output writes one JSON file; analysis_submit freezes all outputs for independent review. '+
   'Invalid format is repairable; ordinary prose is not submission. Keep full Insight thesis, evidence, counterarguments and all 11 contract fields. '+
   'Independent reviewer feedback can be addressed by revision, additional evidence, narrower justified claims or counterevidence. '+
   'Do not alter previously frozen raw sources or published revisions; add new evidence in new files. '+
   'A saved file, local test or graph approval alone does not complete this analysis task.'});
}
