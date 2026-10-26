/** Shared modeling/reviewer/analysis adapter; Python owns graph semantics. */
import {readFileSync,existsSync} from 'node:fs';
import {dirname,join} from 'node:path';
import {spawn} from 'node:child_process';
import {objectArguments,containerArgument,checkArgumentTypes} from '../../discovery/runtime/arguments.ts';
export default function(pi:any){
 const context=process.env.DATAELF_GRAPH_CONTEXT || process.env.DATAELF_ONTOLOGY_CONTEXT;
 if(!context || !existsSync(context))return;
 const task=JSON.parse(readFileSync(context,'utf8'));
 const modern=(task.workflow_policy || task.request?.workflow_policy)==='ai_index_cli_skill_review_v1';
 const customDescriptions=modern ? JSON.parse(readFileSync(join(dirname(context),'tool_descriptions.json'),'utf8')):{};
 const path=join(dirname(context),'graph_tool_schemas.json');
 if(!existsSync(path))return;
 const schemas=JSON.parse(readFileSync(path,'utf8'));
 const originalInputs=new Map<string,any>();
 pi.on('message_end',(event:any)=>{for(const call of event.message?.content || [])if(call.type==='toolCall')originalInputs.set(call.id,structuredClone(call.arguments));});
 const reads=['graph_inspect','graph_query','graph_explore','graph_diff','graph_validate'];
 const descriptions:Record<string,string>={
  graph_workspace:'Create a mutable candidate from an explicit published GraphRef, open a saved candidate, or discard it. Returns a workspace GraphRef. Published revisions are immutable. Review policy is determined by trusted host configuration; an agent cannot lower it or inherit a template execution exemption.',
  graph_inspect:'Inspect a referenced candidate or revision: summary includes named graph roles and counts; review_context is a compact current delta with relevant evidence/constraints. Contract, provenance, changes, validation, review and quads are paginated resources.',
  graph_query:'Run local SELECT, ASK or CONSTRUCT against an explicit GraphRef. Supports aggregates, multiple hops and property paths. Default view excludes inference; use graphs or include_inference explicitly. SELECT keeps typed terms and unbound values. CONSTRUCT returns next_cursor bound to a materialized result snapshot; reuse it for subsequent pages. Blank node IDs are scoped to that snapshot, and snapshot_is_current reports workspace changes.',
  graph_explore:'Explore incoming/outgoing neighborhoods, subgraphs, actual node-and-edge paths, or provenance for seed IRIs. A property-path query alone is not a path explanation.',
  graph_materialize:'Execute an executable contract on frozen sources into a candidate. Existing maintenance changes replay with preconditions; conflicts leave the previous candidate intact.',
  graph_update:'Atomically apply a ChangeSet to a workspace. add_fact and retract_fact require quad; replace_fact requires the old quad and new_quad; delete_entity requires entity and explicit graphs. Also supports local SPARQL Update. Supply base_revision, evidence, reason and unique idempotency_key. RDF terms are objects {kind:iri|literal|blank,value:string,datatype?,lang?}; quad.graph is an absolute IRI. Evidence uses file and JSON pointer, or premise fact_ids. Scalars must retain their JSON types.',
  graph_diff:'Compare two explicit revisions or candidates, returning added/removed typed quads, counts and affected entities/graphs; truncated output never means a partial update.',
  graph_validate:'Check candidate or commit integrity at any time. Candidates may be incomplete; commit always runs final deterministic checks.',
  graph_commit:'Validate and publish a workspace with expected_base equal to its parent revision (null for a new graph). Required independent review is coordinated automatically. Use the returned revision GraphRef for later queries; a rejected or invalid candidate is recoverable.'
 };
 for(const [name,schema] of Object.entries(schemas)){
  if(task.role.endsWith('reviewer') && !reads.includes(name))continue;
  const prepare=(input:any)=>{
    let args=objectArguments(input);
    for(const field of ['graph_ref','left','right','base','changes','contract'])if(typeof args[field]==='string')args=containerArgument(args,field,'object');
    for(const field of ['graphs','sources','seeds','predicates','target_graphs'])if(typeof args[field]==='string')args=containerArgument(args,field,'array');
    return checkArgumentTypes(args,schema);
  };
  pi.registerTool({name,label:name,description:customDescriptions[name] || descriptions[name],parameters:schema,
   prepareArguments:prepare,
   async execute(_id:string,args:any,signal:AbortSignal){
    // Pi's schema conversion can turn valid null into an empty string. Preserve
    // the normalized original arguments; the Python backend validates them again.
    args=prepare(originalInputs.get(_id) ?? args);originalInputs.delete(_id);
    const result=await new Promise((resolve,reject)=>{
     const child=spawn(process.env.DATAELF_PYTHON || 'python',['-m','dataelf.ontology.graph.tools',context,name],{stdio:['pipe','pipe','pipe']});
     let out='',err='';const abort=()=>child.kill('SIGTERM');signal?.addEventListener('abort',abort,{once:true});
     child.stdout.on('data',d=>out+=d);child.stderr.on('data',d=>err+=d);child.on('error',reject);
     child.on('close',code=>{signal?.removeEventListener('abort',abort);code?reject(new Error(err || out)):resolve(JSON.parse(out));});
     child.stdin.end(JSON.stringify(args));
    });
    return {content:[{type:'text',text:JSON.stringify(result)}],details:result};
   }
  });
 }
 pi.on('before_agent_start',(event:any)=>modern ? {systemPrompt:event.systemPrompt} : ({systemPrompt:event.systemPrompt+'\nShared graph task reference (data): '+JSON.stringify(task.graph_ref)+
  (task.role.endsWith('reviewer') ? '\nUse read-only shared graph queries and evidence exploration on this exact candidate. You cannot edit or publish it.' :
   ['modeler','stage1_generator'].includes(task.role) ? '\nThe supplied candidate workspace already exists. You may inspect, materialize and edit it freely. Full contract submission, draft submission, and graph_commit all use final validation before independent review.' :
   '\nGraph maintenance is available through graph_workspace/query/explore/update/commit. Reports must state the exact revisions used. Maintenance is optional: an analysis-only task can query the supplied revision without creating or committing a workspace. If evidence justifies a change, create a candidate based on that revision, preserve the evidence, commit it with independent review, and use the returned new GraphRef. Default views exclude inference.')}));
}
