/** Server-only declared protocol compatibility. Never infer or repair a call.
 * v2 additionally recognizes the captured gateway's </arg_val> terminator for
 * <arg_value>. This exact spelling is part of the wire grammar, not a missing
 * terminator to invent. Unknown/missing closes remain invalid.
 */
import { Compile } from 'typebox/compile';

type RecordValue = Record<string, any>;
export type OfferedTools = Map<string, RecordValue>;

export function parseTextualToolCalls(content: string | null | undefined) {
    const text = content?.trim() ?? '';
    const no = {matched:false, toolCalls:[] as any[]};
    if (!text.startsWith('<tool_call>') || !text.endsWith('</tool_call>')) return no;
    // This compatibility profile supports one exact envelope, not nested calls
    // or prose containing examples. Strings inside arguments stay verbatim.
    const body = text.slice(11, -12).trim();
    if (body.includes('<tool_call>') || body.includes('</tool_call>')) return no;
    const name = /^([A-Za-z_][A-Za-z0-9_.:-]*)\s*/.exec(body);
    if (!name) return no;
    const args:RecordValue = {};
    const pattern = /<arg_key>([\s\S]*?)<\/arg_key>\s*<arg_value>([\s\S]*?)<\/(?:arg_value|arg_val)>/g;
    let cursor = name[0].length, pairs = 0;
    for (let match = pattern.exec(body); match; match = pattern.exec(body)) {
        if (body.slice(cursor,match.index).trim()) return no;
        const key=match[1].trim();
        if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key) || Object.hasOwn(args,key)) return no;
        args[key]=match[2]; cursor=pattern.lastIndex; pairs++;
    }
    if (!pairs || body.slice(cursor).trim()) return no;
    return {matched:true,toolCalls:[{type:'function',function:{name:name[1],arguments:JSON.stringify(args)}}]};
}

export function validateNativeArguments(name:string, args:unknown, offered:OfferedTools, required?:string) {
    if (!offered.has(name)) throw new Error(`Tool ${name} was not offered in the current server phase`);
    if (required && name!==required) throw new Error(`Forced tool mismatch: requested ${required}, received ${name}`);
    if (!args || typeof args!=='object' || Array.isArray(args)) throw new Error(`Tool ${name} arguments must be a complete object`);
    if (!Compile(offered.get(name) as any).Check(args)) throw new Error(`Tool ${name} arguments violate its current schema`);
    return args as RecordValue;
}

export class ToolEnvelopeError extends Error {
    constructor(readonly responseText:string, readonly offered:OfferedTools, readonly required?:string) {
        const duplicate=/<\/arg_value>\s*<\/arg_value>/.test(responseText);
        super(duplicate ? 'Duplicate </arg_value> closing tag; each argument must have exactly one closing tag.'
            : 'Invalid tool envelope: expected one tool_call, named tool, unique arg_key/arg_value pairs, exact closing tags and no surrounding prose.');
    }
    diagnostic() {
        return {code:'SERVER_TOOL_ENVELOPE_INVALID', message:this.message, response_text:this.responseText,
            allowed_tools:[...this.offered].map(([name,schema])=>({name,required:schema.required ?? [],properties:schema.properties ?? {}})),
            required_tool:this.required ?? null};
    }
}

export function decodeTextualTools(text:string, offered:OfferedTools, finish:string, aborted=false, required?:string) {
    if (aborted || finish!=='stop') throw new Error('Textual tools require an uninterrupted, terminal stop response');
    const parsed=parseTextualToolCalls(text);
    if (!parsed.matched) {
        if (text.includes('<tool_call>')) throw new ToolEnvelopeError(text,offered,required);
        return [];
    }
    return parsed.toolCalls.map((call:any)=>{
        const name=call.function.name;
        if (!offered.has(name)) throw new Error(`Textual tool ${name} was not offered in the current server phase`);
        const args=JSON.parse(call.function.arguments);
        const schema=offered.get(name);
        for (const [key,value] of Object.entries(args)) {
            const type=schema?.properties?.[key]?.type;
            if (typeof value==='string' && typeof type==='string' && type!=='string') {
                // This wire protocol represents values as strings. Decode JSON
                // only when the declared parameter requires a non-string value.
                args[key]=JSON.parse(value);
            }
        }
        return {name,arguments:validateNativeArguments(name,args,offered,required)};
    });
}
