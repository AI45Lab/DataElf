/** Ontology/AI Index opt-in observer. No headers; never changes a request. */
import {readFileSync,writeFileSync,mkdirSync,existsSync} from 'node:fs';
import {dirname,join} from 'node:path';
import {createHash} from 'node:crypto';
export default function(pi:any) {
 const path=process.env.DATAELF_ONTOLOGY_CONTEXT || process.env.DATAELF_ANALYSIS_CONTEXT;
 if(!path || !existsSync(path))return;
 const task=JSON.parse(readFileSync(path,'utf8'));
 if((task.workflow_policy || task.request?.workflow_policy)!=='ai_index_cli_skill_review_v1')return;
 const folder=join(dirname(path),'effective_requests');mkdirSync(folder,{recursive:true,mode:0o700});
 const secrets=Object.entries(process.env).filter(([k,v])=>/KEY|TOKEN|PASSWORD|SECRET/.test(k) && (v || '').length>=8).map(([,v])=>v!);
 function clean(value:any):any {
  if(Array.isArray(value))return value.map(clean);
  if(value && typeof value==='object')return Object.fromEntries(Object.entries(value).map(([k,v])=>
   [k,/^(headers|authorization|api[_-]?key|password|access[_-]?token|refresh[_-]?token)$/i.test(k) ? '[REDACTED]':clean(v)]));
  if(typeof value==='string')for(const secret of secrets)value=value.split(secret).join('[REDACTED]');
  return value;
 }
 const hash=(v:any)=>createHash('sha256').update(JSON.stringify(v)).digest('hex');
 let sequence=0;
 pi.on('before_provider_request',(event:any)=>{
  const payload=clean(event.payload);const request=++sequence;
  writeFileSync(join(folder,`${String(request).padStart(4,'0')}.json`),JSON.stringify({request,at:new Date().toISOString(),
   boundary:'Pi before_provider_request after role extensions; transport wire capture is separate',
   role:task.role,review_mode:task.review_mode || 'formal',policy:'ai_index_cli_skill_review_v1',
   prompt_manifest:existsSync(join(dirname(path),'composed_input.json')) ? JSON.parse(readFileSync(join(dirname(path),'composed_input.json'),'utf8')):null,
   payload_hash:hash(payload),tools_hash:hash(payload.tools),messages_hash:hash(payload.messages),payload},null,2),{mode:0o600});
 });
}
