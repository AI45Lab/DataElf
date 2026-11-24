/** Lossless compatibility decoding. Never repair JSON or infer semantic fields. */
export function objectArguments(args: any): any {
    let value = args;
    if (value && typeof value === "object" && !Array.isArray(value) &&
        Object.keys(value).length === 1 && "payload" in value) value = value.payload;
    if (typeof value === "string") {
        try { value = JSON.parse(value); }
        catch (error: any) {
            const position = /position (\d+)/.exec(error.message)?.[1];
            const coordinates = /line (\d+) column (\d+)/.exec(error.message);
            throw new Error(JSON.stringify({code:"STRUCTURED_ARGUMENT_JSON_INVALID", target:"format",
                issues:[{path:"/", message:"Invalid JSON text. Pass an object directly instead of serializing it.",
                         position: position ? Number(position) : null,
                         line: coordinates ? Number(coordinates[1]) : null,
                         column: coordinates ? Number(coordinates[2]) : null}]}));
        }
    }
    if (!value || typeof value !== "object" || Array.isArray(value)) {
        throw new Error('STRUCTURED_ARGUMENT_INVALID: expected a JSON object; no arrays, scalars or double serialization.');
    }
    return value;
}

export function compactToolError(message: any): any {
    if (message?.role !== "toolResult" || !message.isError || !Array.isArray(message.content)) return message;
    return {...message, content: message.content.map((part: any) => part.type === "text"
        ? {...part, text: part.text.split("\n\nReceived arguments:")[0].slice(0, 6000)} : part)};
}

/** Known container fields only; no recursive guesses about business expressions. */
export function containerArgument(args:any,key:string,kind:"object"|"array"):any {
    if (typeof args[key] !== "string") return args;
    let decoded:any;
    try { decoded=JSON.parse(args[key]); }
    catch (error:any) {
        const position=/position (\d+)/.exec(error.message)?.[1];
        const coordinates=/line (\d+) column (\d+)/.exec(error.message);
        throw new Error(JSON.stringify({code:"STRUCTURED_ARGUMENT_JSON_INVALID",target:"format",
            issues:[{path:"/"+key,message:"Pass the declared container directly; malformed JSON text is not repaired.",
                position:position ? Number(position) : null,line:coordinates ? Number(coordinates[1]) : null,
                column:coordinates ? Number(coordinates[2]) : null}]}));
    }
    if (!decoded || typeof decoded !== "object" || Array.isArray(decoded) !== (kind === "array"))
        throw new Error("STRUCTURED_ARGUMENT_INVALID /"+key+": expected "+kind+"; no scalars or double serialization.");
    return {...args,[key]:decoded};
}

/** Give focused type feedback before union-schema validation lists unrelated branches. */
export function checkArgumentTypes(args:any,schema:any):any {
    const resolve=(value:any)=>value?.$ref?.startsWith('#/$defs/')
        ? schema.$defs?.[value.$ref.slice('#/$defs/'.length)] || value : value;
    let branch=schema;
    const discriminator=schema.discriminator;
    if(discriminator && discriminator.mapping?.[args[discriminator.propertyName]])
        branch=resolve({$ref:discriminator.mapping[args[discriminator.propertyName]]});
    for(const [key,definition] of Object.entries(branch.properties || {})) {
        if(!(key in args))continue;
        const field=resolve(definition);
        const types=typeof field?.type === 'string' ? [field.type] : field?.type;
        if(!Array.isArray(types))continue;
        const value=args[key];
        const matches=types.some((type:string)=>type==='null' ? value===null :
            type==='array' ? Array.isArray(value) : type==='object' ? value!==null && typeof value==='object' && !Array.isArray(value) :
            type==='integer' ? typeof value==='number' && Number.isInteger(value) :
            type==='number' ? typeof value==='number' && Number.isFinite(value) : typeof value===type);
        if(!matches)throw new Error(JSON.stringify({code:'STRUCTURED_ARGUMENT_INVALID',target:'format',issues:[{
            path:'/'+key,message:'Expected '+types.join(' or ')+', received '+(value===null ? 'null' : Array.isArray(value) ? 'array' : typeof value)+
                '. Supply the JSON type declared by the schema; do not quote numbers or booleans.'}]}));
    }
    return args;
}

export function canonicalArguments(input:any):string {
    let value=input;
    try {value=objectArguments(input);} catch { /* Invalid JSON remains byte-identifiable. */ }
    const sorted=(item:any):any=>Array.isArray(item) ? item.map(sorted) :
        item && typeof item==='object' ? Object.fromEntries(Object.keys(item).sort().map(key=>[key,sorted(item[key])])) : item;
    return JSON.stringify(sorted(value));
}
