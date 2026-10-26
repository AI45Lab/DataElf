import { compatibleThinkingPayload, withoutThinking } from "./thinking.ts";
import { compactToolError } from "./arguments.ts";

/** Provider compatibility only; discovery tools and workflow remain unchanged. */
export default function (pi: any) {
    const graphTask = !!(process.env.DATAELF_GRAPH_CONTEXT || process.env.DATAELF_ONTOLOGY_CONTEXT);
    const analysisTask = process.env.DATAELF_WORKFLOW_PROFILE === 'research' && process.env.DATAELF_DOMAIN === 'ai_index';
    if (graphTask) {
        pi.on("message_end", (event: any) => ({ message: withoutThinking(event.message) }));
        pi.on("context", (event: any) => ({ messages: event.messages.map((message:any) => {
            const clean=withoutThinking(message);
            return process.env.DATAELF_WORKFLOW_PROFILE === "research" ? compactToolError(clean) : clean;
        }) }));
    }
    pi.on("before_provider_request", (event: any, ctx: any) => {
        if (!event.payload || typeof event.payload !== "object") return;
        const payload=compatibleThinkingPayload(event.payload, ctx.model);
        if((graphTask || analysisTask) && process.env.DATAELF_PI_STREAM_USAGE==='1' && ctx.model?.api==='openai-completions')payload.stream_options={...payload.stream_options,include_usage:true};
        return payload;
    });
}
