/** Keep provider reasoning separate from public text and executable tool calls. */
export function stripThinkingPrefix(text: string): string {
    let value = text;
    while (true) {
        const open = value.match(/^\s*<(think|thinking)>/i);
        if (!open) return value;
        const close = new RegExp(`</${open[1]}>`, "i").exec(value.slice(open[0].length));
        // An incomplete reasoning block is never a final answer.
        if (!close) return "";
        value = value.slice(open[0].length + close.index + close[0].length).trimStart();
    }
}

export function withoutThinking<T extends { role: string }>(message: T): T {
    if (message.role !== "assistant") return message;
    const assistant = message as T & { content: any[] };
    return { ...assistant, content: assistant.content.flatMap((block: any) => {
        if (block.type === "thinking") return [];
        if (block.type !== "text") return [block]; // Preserve real tool calls verbatim.
        const text = stripThinkingPrefix(block.text);
        return text ? [{ ...block, text }] : [];
    }) };
}

/** The local GLM gateway needs both the ZAI switch and its vLLM template flag. */
export function compatibleThinkingPayload(payload: Record<string, any>, model: any): Record<string, any> {
    if (model?.api !== "openai-completions" || model.compat?.thinkingFormat !== "zai") return payload;
    const enabled = payload.thinking?.type === "enabled";
    return { ...payload, thinking: { ...payload.thinking, type: enabled ? "enabled" : "disabled" },
        chat_template_kwargs: { ...payload.chat_template_kwargs, enable_thinking: enabled } };
}
