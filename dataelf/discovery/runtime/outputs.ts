import {readFileSync,existsSync} from "node:fs";
import {join} from "node:path";
import {spawn} from "node:child_process";
import {objectArguments,containerArgument,checkArgumentTypes} from "./arguments.ts";

export default function(pi:any) {
    if (process.env.DATAELF_WORKFLOW_PROFILE !== "research") return;
    if (process.env.DATAELF_ANALYSIS_ROLE === 'reviewer') return;
    if (!process.env.DATAELF_GRAPH_CONTEXT && process.env.DATAELF_DOMAIN !== 'ai_index') return;
    const workspace = process.env.DATAELF_JOB_WORKSPACE!;
    const contractPath = join(workspace,"prompts/output_contract.json");
    const schemaPath = join(workspace,"prompts/output_tool_schema.json");
    if (!existsSync(contractPath) || !existsSync(schemaPath)) return;
    const envelope = JSON.parse(readFileSync(contractPath,"utf8"));
    const schema = JSON.parse(readFileSync(schemaPath,"utf8"));
    const modern=process.env.DATAELF_WORKFLOW_POLICY==='ai_index_cli_skill_review_v1';
    const originalInputs=new Map<string,any>();
    pi.on('message_end',(event:any)=>{for(const call of event.message?.content || [])if(call.type==='toolCall')originalInputs.set(call.id,structuredClone(call.arguments));});
    pi.on("before_agent_start",(event:any) => modern ? {systemPrompt:event.systemPrompt} : ({systemPrompt:event.systemPrompt +
        '\nOUTPUT CONTRACT: Submit final JSON artifacts through dataelf_submit_output. '
        + 'Pass direct artifact_id and data object arguments, never JSON strings. '
        + 'The following declared file roots and schemas are authoritative:\n' + JSON.stringify(schema)}));
    const prepare=(input:any) => {
            let args=objectArguments(input);
            const spec=envelope.contract.artifacts.find((s:any)=>s.artifact_id===args.artifact_id);
            // Decode only a whole output whose declared root excludes strings.
            // Individual fields and unrestricted JSON outputs retain their types.
            if (typeof args.data === "string" && spec && (spec.json_root ||
                ["object","array"].includes(spec.json_schema?.type))) {
                args=containerArgument(args,"data",spec.json_root ? "object" : spec.json_schema.type);
            }
            return checkArgumentTypes(args,schema);
        };
    pi.registerTool({name:"dataelf_submit_output",label:"Submit validated output",
        description:modern ? "校验并保存一个声明的 JSON 文件，data 为直接对象；不写原始来源，也不代表整套分析提交。" : "Validate a declared final JSON output and serialize it to its controlled workspace path. Never writes source evidence.",
        parameters:schema,prepareArguments:prepare,
        async execute(_id:string,args:any,signal:AbortSignal) {
            args=prepare(originalInputs.get(_id) ?? args);originalInputs.delete(_id);
            const text = await new Promise<string>((resolve,reject) => {
                const child = spawn(process.env.DATAELF_PYTHON || "python",[
                    "-m","dataelf.discovery.output_submission",workspace],{stdio:["pipe","pipe","pipe"]});
                let out="",err="";
                const abort=()=>child.kill("SIGTERM");
                signal?.addEventListener("abort",abort,{once:true});
                child.stdout.on("data",d=>out+=d);child.stderr.on("data",d=>err+=d);
                child.on("error",reject);child.on("close",code=>{
                    signal?.removeEventListener("abort",abort);
                    code ? reject(new Error(err || out)) : resolve(out);
                });
                child.stdin.end(JSON.stringify({...envelope,artifact_id:args.artifact_id,data:args.data}));
            });
            return {content:[{type:"text",text}],details:{}};
        }});
}
