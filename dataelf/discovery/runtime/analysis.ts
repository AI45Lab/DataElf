import {readFileSync,existsSync,writeFileSync} from 'node:fs';
import {dirname,join} from 'node:path';
import {spawn} from 'node:child_process';
import {installNativeCallGuard} from '../../ontology/runtime/native_calls.ts';
import {outputLimit} from '../../ontology/runtime/output_limit.ts';
import {trustedHandoff} from '../../ontology/runtime/tool_results.ts';
import {withoutThinkingPayload} from '../../ontology/runtime/thinking.ts';

export default function(pi:any) {
 if(process.env.DATAELF_REVIEWED_WORKFLOW!=='1')return;
 const path=process.env.DATAELF_ANALYSIS_CONTEXT;
 if(!path || !existsSync(path))return;
 const task=JSON.parse(readFileSync(path,'utf8'));
 const modern=process.env.DATAELF_REVIEWED_WORKFLOW==='1';
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
 pi.on('before_agent_start',()=>({systemPrompt:readFileSync(join(dirname(path),'system.md'),'utf8')}));
}
